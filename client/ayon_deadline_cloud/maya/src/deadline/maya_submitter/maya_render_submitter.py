# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from __future__ import annotations

import os
from logging import getLogger
from pathlib import Path
from typing import Any, Optional, cast
import yaml  # type: ignore[import]
from copy import deepcopy
from dataclasses import dataclass

import maya.cmds  # pylint: disable=import-error

from deadline.client.api import (
    get_deadline_cloud_library_telemetry_client,
    get_queue_parameter_definitions,
)
from deadline.client.config import get_setting
from deadline.client.job_bundle.parameters import JobParameter
from deadline.client.job_bundle._yaml import deadline_yaml_dump
from deadline.client.ui.dialogs.submit_job_to_deadline_dialog import (  # pylint: disable=import-error
    SubmitJobToDeadlineDialog,
    JobBundlePurpose,
)
from deadline.client.exceptions import DeadlineOperationError
from qtpy.QtCore import Qt  # type: ignore

from . import Animation, Scene  # type: ignore
from .assets import AssetIntrospector
from .renderers import get_output_prefix_with_tokens, get_height, get_width
from .data_classes import (
    RenderSubmitterUISettings,
)
from .render_layers import (
    saved_current_render_layer,
    get_current_render_layer_name,
    get_render_layer_display_name,
    set_current_render_layer,
    get_all_renderable_render_layer_names,
    render_setup_include_all_lights,
    LayerSelection,
)
from .vray_export import (
    export_vrscene,
    vrscene_asset_paths,
    vrscene_path,
    vrscene_referenced_asset_paths,
)
from .cameras import get_renderable_camera_names, ALL_CAMERAS
from ._version import version
from .ui.components.scene_settings_tab import SceneSettingsWidget
from deadline.client.job_bundle.submission import AssetReferences
import time

logger = getLogger(__name__)


@dataclass
class RenderLayerData:
    name: str
    display_name: str
    renderer_name: str
    ui_group_label: str
    frames_parameter_name: Optional[str]
    frame_range: str
    renderable_camera_names: list[str]
    output_directories: set[str]
    output_file_prefix_parameter_name: Optional[str]
    output_file_prefix: str
    image_width_parameter_name: Optional[str]
    image_height_parameter_name: Optional[str]
    image_resolution: tuple[int, int]


def _vrscene_parameter_name(layer_data: RenderLayerData) -> str:
    """Return the OpenJD parameter name for a layer's exported scene."""
    return f"{layer_data.display_name}VRSceneFile"


def _get_job_template(
    default_job_template: dict[str, Any],
    settings: RenderSubmitterUISettings,
    renderers: set[str],
    render_layers: list[RenderLayerData],
    all_layer_selectable_cameras: list[str],
    current_layer_selectable_cameras: list[str],
) -> dict[str, Any]:
    job_template = deepcopy(default_job_template)

    # Set the job's name and description
    job_template["name"] = settings.name
    if settings.description:
        job_template["description"] = settings.description

    # If there are multiple frame ranges, split up the Frames parameter by layer
    if render_layers[0].frames_parameter_name:
        # Extract the Frames parameter definition
        frame_param = [
            param for param in job_template["parameterDefinitions"] if param["name"] == "Frames"
        ][0]
        job_template["parameterDefinitions"] = [
            param for param in job_template["parameterDefinitions"] if param["name"] != "Frames"
        ]

        # Create layer-specific Frames parameters
        for layer_data in render_layers:
            layer_frame_param = deepcopy(frame_param)
            layer_frame_param["name"] = layer_data.frames_parameter_name
            layer_frame_param["userInterface"]["groupLabel"] = layer_data.ui_group_label
            job_template["parameterDefinitions"].append(layer_frame_param)

    # If there are multiple output image formats, split that up by layer
    if render_layers[0].output_file_prefix_parameter_name:
        for layer_data in render_layers:
            job_template["parameterDefinitions"].append(
                {
                    "name": layer_data.output_file_prefix_parameter_name,
                    "type": "STRING",
                    "userInterface": {
                        "control": "LINE_EDIT",
                        "label": "Output File Prefix",
                        "groupLabel": layer_data.ui_group_label,
                    },
                    "description": f"The output filename prefix for layer {layer_data.display_name}",
                }
            )
    else:
        job_template["parameterDefinitions"].append(
            {
                "name": "OutputFilePrefix",
                "type": "STRING",
                "userInterface": {
                    "control": "LINE_EDIT",
                    "label": "Output File Prefix",
                    "groupLabel": "Maya Settings",
                },
                "description": "The output filename prefix.",
            }
        )

    # If there are multiple output image resolutions, split that up by layer
    if render_layers[0].image_width_parameter_name:
        for layer_data in render_layers:
            job_template["parameterDefinitions"].append(
                {
                    "name": layer_data.image_width_parameter_name,
                    "type": "INT",
                    "userInterface": {
                        "control": "SPIN_BOX",
                        "label": "Image Width",
                        "groupLabel": layer_data.ui_group_label,
                    },
                    "minValue": 1,
                    "description": f"The image width for layer {layer_data.display_name}.",
                }
            )
            job_template["parameterDefinitions"].append(
                {
                    "name": layer_data.image_height_parameter_name,
                    "type": "INT",
                    "userInterface": {
                        "control": "SPIN_BOX",
                        "label": "Image Height",
                        "groupLabel": layer_data.ui_group_label,
                    },
                    "minValue": 1,
                    "description": f"The image height for layer {layer_data.display_name}.",
                }
            )
    else:
        job_template["parameterDefinitions"].append(
            {
                "name": "ImageWidth",
                "type": "INT",
                "userInterface": {
                    "control": "SPIN_BOX",
                    "label": "Image Width",
                    "groupLabel": "Maya Settings",
                },
                "minValue": 1,
                "description": "The image width of the output.",
            }
        )
        job_template["parameterDefinitions"].append(
            {
                "name": "ImageHeight",
                "type": "INT",
                "userInterface": {
                    "control": "SPIN_BOX",
                    "label": "Image Height",
                    "groupLabel": "Maya Settings",
                },
                "minValue": 1,
                "description": "The image height of the output.",
            }
        )

    # If we're rendering a specific camera, add the Camera job parameter
    if settings.camera_selection != ALL_CAMERAS:
        selectable_cameras: list[str]
        if settings.render_layer_selection == LayerSelection.ALL:
            selectable_cameras = all_layer_selectable_cameras
        else:
            selectable_cameras = current_layer_selectable_cameras

        camera_param = {
            "name": "Camera",
            "type": "STRING",
            "userInterface": {
                "control": "DROPDOWN_LIST",
                "groupLabel": "Maya Settings",
            },
            "description": "Select which camera to render.",
            "allowedValues": selectable_cameras,
        }
        job_template["parameterDefinitions"].append(camera_param)

    for layer_data in render_layers:
        if layer_data.renderer_name != "vray":
            continue
        job_template["parameterDefinitions"].append(
            {
                "name": _vrscene_parameter_name(layer_data),
                "type": "PATH",
                "objectType": "FILE",
                "dataFlow": "IN",
                "userInterface": {"control": "HIDDEN"},
                "description": "Locally exported V-Ray scene.",
            }
        )

    # Replicate the default step, once per render layer, and adjust its settings
    default_step = job_template["steps"][0]
    job_template["steps"] = []
    for layer_data in render_layers:
        step = deepcopy(default_step)
        job_template["steps"].append(step)

        step["name"] = layer_data.display_name

        parameter_space = step["parameterSpace"]
        # Update the 'Param.Frames' reference in the Frame task parameter
        if layer_data.frames_parameter_name:
            parameter_space["taskParameterDefinitions"][0]["range"] = (
                "{{Param." + layer_data.frames_parameter_name + "}}"
            )

        if layer_data.renderer_name == "vray":
            vrscene_param_name = _vrscene_parameter_name(layer_data)
            remap_python_script = """import json
import os
import re
import xml.etree.ElementTree as ET

BS = chr(92)
mapping_file = os.environ.get("MAPPING_FILE", "")
vrscene = os.environ.get("VRSCENE_PATH", "")
remap_out = os.environ.get("REMAP_OUTPUT", "")

rules = []
if mapping_file and os.path.exists(mapping_file):
    try:
        with open(mapping_file, "r", encoding="utf-8", errors="surrogateescape") as f:
            rules = json.load(f).get("path_mapping_rules", [])
    except Exception as e:
        print(f"Warning: could not read path mapping rules: {e}")

print(f"Loaded {len(rules)} path mapping rules.")

root = ET.Element("RemapPaths")
norm_rules = []
for r in sorted(rules, key=lambda x: len(x.get("source_path", "")), reverse=True):
    src = r.get("source_path", "").strip()
    dst = r.get("destination_path", "").strip()
    if not src or not dst:
        continue
    s_norm = src.replace(BS, "/").rstrip("/")
    d_norm = dst.replace(BS, "/").rstrip("/")
    norm_rules.append((s_norm.lower(), s_norm, d_norm))
    variants = set()
    variants.add(src)
    variants.add(src.replace(BS, "/"))
    variants.add(src.replace("/", BS))
    for v in variants:
        item = ET.SubElement(root, "RemapItem")
        ET.SubElement(item, "From").text = v
        ET.SubElement(item, "To").text = dst

if remap_out:
    ET.ElementTree(root).write(remap_out, encoding="utf-8", xml_declaration=True)
    print(f"Generated V-Ray remap XML at: {remap_out}")

if vrscene and os.path.exists(vrscene) and norm_rules:
    try:
        with open(vrscene, "r", encoding="utf-8", errors="surrogateescape") as f:
            content = f.read()

        def repl(m):
            raw = m.group(1)
            if chr(10) in raw or chr(13) in raw:
                return m.group(0)
            raw_norm = raw.replace(BS, "/")
            raw_lower = raw_norm.lower()
            for s_lower, _, d in norm_rules:
                if raw_lower.startswith(s_lower):
                    return chr(34) + d + raw_norm[len(s_lower):] + chr(34)
            return m.group(0)

        patched = re.sub(r'"([^"]+)"', repl, content)
        with open(vrscene, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(patched)
        print("Successfully patched VRScene with local worker paths.")
    except Exception as e:
        print(f"Warning: failed to patch VRScene: {e}")
"""

            vray_script = """#!/bin/bash
set -euo pipefail

echo "=============================================="
echo "Starting V-Ray Standalone Task"
echo "=============================================="

VRSCENE_PATH="{{Param.__VRSCENE_PARAM__}}"
echo "Configured VRScene path: $VRSCENE_PATH"

if [ ! -f "$VRSCENE_PATH" ]; then
    VRSCENE_NAME="$(basename "$VRSCENE_PATH")"
    echo "VRScene not found at parameter path, searching in session directory..."
    FOUND="$(find "{{Session.WorkingDirectory}}" /sessions -name "$VRSCENE_NAME" 2>/dev/null | head -n 1 || true)"
    if [ -n "$FOUND" ] && [ -f "$FOUND" ]; then
        VRSCENE_PATH="$FOUND"
        echo "Found VRScene at: $VRSCENE_PATH"
    fi
fi

if [ -z "$VRSCENE_PATH" ] || [ ! -f "$VRSCENE_PATH" ]; then
    echo "ERROR: Could not locate VRScene file!" >&2
    exit 3
fi

REMAP_FILE="{{Session.WorkingDirectory}}/vray_remap.xml"
export MAPPING_FILE="{{Session.PathMappingRulesFile}}"
export VRSCENE_PATH="$VRSCENE_PATH"
export REMAP_OUTPUT="$REMAP_FILE"

# 1. Process path mapping rules: generate V-Ray remap XML and patch .vrscene in-place
python3 "{{Task.File.remapScript}}"

# 2. Export OCIO config path
OCIO_PARAM="{{Param.OCIOConfigFile}}"
if [ -n "$OCIO_PARAM" ] && [ -f "$OCIO_PARAM" ]; then
    export OCIO="$OCIO_PARAM"
    echo "Using OCIO from parameter: $OCIO"
elif [ -z "${OCIO:-}" ] || [ ! -f "$OCIO" ]; then
    FOUND_OCIO="$(find "{{Session.WorkingDirectory}}" /sessions -name "*.ocio" 2>/dev/null | head -n 1 || true)"
    if [ -n "$FOUND_OCIO" ] && [ -f "$FOUND_OCIO" ]; then
        export OCIO="$FOUND_OCIO"
        echo "Found staged OCIO config: $OCIO"
    fi
fi

export VRAY_PATH_REMAPPING_CASE_SENSITIVE=0
SCENE_DIR="$(cd "$(dirname "$VRSCENE_PATH")" && pwd)"
export VRAY_ASSETS_PATH="${VRAY_ASSETS_PATH:-}:$SCENE_DIR:{{Session.WorkingDirectory}}"

REMAP_ARGS=()
if [ -f "$REMAP_FILE" ] && grep -q "<RemapItem>" "$REMAP_FILE"; then
    export VRAY_PATH_REMAP_FILEPATH="$REMAP_FILE"
    REMAP_ARGS=("-remapPathFile=$REMAP_FILE")
fi

if ! command -v vray &>/dev/null; then
    echo "vray not found in PATH, searching in system and conda directories..."
    FOUND_VRAY="$(find /home/job-user/conda-envs /opt /usr/local -name "vray" -type f -perm /111 2>/dev/null | head -n 1 || true)"
    if [ -n "$FOUND_VRAY" ] && [ -x "$FOUND_VRAY" ]; then
        echo "Found vray at: $FOUND_VRAY"
        export PATH="$(dirname "$FOUND_VRAY"):$PATH"
    fi
fi

cd "$SCENE_DIR"
echo "Executing V-Ray Standalone..."
exec vray \
    -sceneFile="$VRSCENE_PATH" \
    -frames={{Task.Param.Frame}} \
    "${REMAP_ARGS[@]}" \
    -display=0 \
    -showProgress=2
""".replace("__VRSCENE_PARAM__", vrscene_param_name)
            step["stepEnvironments"] = [
                {
                    "name": "V-Ray Standalone",
                    "description": "Runs V-Ray Standalone without Maya.",
                    "variables": {"VRAY_RENDERER": "vray"},
                }
            ]
            step["script"] = {
                "embeddedFiles": [
                    {
                        "name": "runScript",
                        "filename": "run_vray.sh",
                        "type": "TEXT",
                        "data": vray_script,
                    },
                    {
                        "name": "remapScript",
                        "filename": "remap_vray.py",
                        "type": "TEXT",
                        "data": remap_python_script,
                    },
                ],
                "actions": {
                    "onRun": {
                        "command": "bash",
                        "args": [
                            "{{Task.File.runScript}}",
                        ],
                        "cancelation": {"mode": "NOTIFY_THEN_TERMINATE"},
                    }
                },
            }
            continue
        # If we're submitting all cameras, create another parameter space dimension
        if settings.camera_selection == ALL_CAMERAS:
            parameter_space["taskParameterDefinitions"].append(
                {
                    "name": "Camera",
                    "type": "STRING",
                    "range": layer_data.renderable_camera_names,
                }
            )
            run_data = step["script"]["embeddedFiles"][0]
            run_data["data"] += "camera: '{{Task.Param.Camera}}'\n"

        # Update the init data of the step
        init_data = step["stepEnvironments"][0]["script"]["embeddedFiles"][0]
        init_data["data"] = (
            f"renderer: {layer_data.renderer_name}\nrender_layer: {layer_data.display_name}\n"
            + init_data["data"]
            + "output_file_prefix: '{{Param."
            + (layer_data.output_file_prefix_parameter_name or "OutputFilePrefix")
            + "}}'\n"
            + "image_width: {{Param."
            + (layer_data.image_width_parameter_name or "ImageWidth")
            + "}}\n"
            + "image_height: {{Param."
            + (layer_data.image_height_parameter_name or "ImageHeight")
            + "}}\n"
            + "cache_pathmapping: true\n"
        )
        # If a specific camera is selected, link to the Camera parameter
        if settings.camera_selection != ALL_CAMERAS:
            init_data["data"] += "camera: '{{Param.Camera}}'\n"

        # If the renderer is Arnold, add specific parameters for it
        if layer_data.renderer_name == "arnold":
            init_data[
                "data"
            ] += "error_on_arnold_license_fail: {{Param.ArnoldErrorOnLicenseFailure}}\n"

    # If Arnold is one of the renderers, add Arnold-specific parameters
    if "arnold" in renderers:
        job_template["parameterDefinitions"].append(
            {
                "name": "ArnoldErrorOnLicenseFailure",
                "type": "STRING",
                "userInterface": {
                    "control": "CHECK_BOX",
                    "label": "Error on License Failure",
                    "groupLabel": "Arnold Renderer Settings",
                },
                "description": "Whether to produce an error when there is an Arnold license failure.",
                "default": "false",
                "allowedValues": ["true", "false"],
            }
        )

    # If this developer option is enabled, merge the adaptor_override_environment
    if settings.include_adaptor_wheels:
        with open(Path(__file__).parent / "adaptor_override_environment.yaml") as f:
            override_environment = yaml.safe_load(f)

        # Read DEVELOPMENT.md for instructions to create the wheels directory.
        wheels_path = Path(__file__).parent.parent.parent.parent / "wheels"
        if not wheels_path.exists() and wheels_path.is_dir():
            raise RuntimeError(
                "The Developer Option 'Include Adaptor Wheels' is enabled, but the wheels directory does not exist:\n"
                + str(wheels_path)
            )
        wheels_path_package_names = {
            path.split("-", 1)[0] for path in os.listdir(wheels_path) if path.endswith(".whl")
        }
        if wheels_path_package_names != {
            "openjd_adaptor_runtime",
            "deadline",
            "deadline_cloud_for_maya",
        }:
            raise RuntimeError(
                "The Developer Option 'Include Adaptor Wheels' is enabled, but the wheels directory contains the wrong wheels:\n"
                + "Expected: openjd_adaptor_runtime, deadline, and deadline_cloud_for_maya\n"
                + f"Actual: {wheels_path_package_names}"
            )

        override_adaptor_name_param = [
            param
            for param in override_environment["parameterDefinitions"]
            if param["name"] == "OverrideAdaptorName"
        ][0]
        override_adaptor_name_param["default"] = "MayaAdaptor"

        # There are no parameter conflicts between these two templates, so this works
        job_template["parameterDefinitions"].extend(override_environment["parameterDefinitions"])

        # Add the environment to the end of the template's job environments
        if "jobEnvironments" not in job_template:
            job_template["jobEnvironments"] = []
        job_template["jobEnvironments"].append(override_environment["environment"])

    return job_template


def _get_parameter_values(
    settings: RenderSubmitterUISettings,
    renderers: set[str],
    render_layers: list[RenderLayerData],
    queue_parameters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    parameter_values: list[dict[str, Any]] = []

    # Set the Maya scene file value
    parameter_values.append({"name": "MayaSceneFile", "value": Scene.name()})

    if render_layers[0].frames_parameter_name:
        for layer_data in render_layers:
            parameter_values.append(
                {
                    "name": layer_data.frames_parameter_name,
                    "value": layer_data.frame_range,
                }
            )
    else:
        if settings.override_frame_range:
            frame_list = settings.frame_list
        else:
            frame_list = render_layers[0].frame_range
        parameter_values.append({"name": "Frames", "value": frame_list})

    if render_layers[0].output_file_prefix_parameter_name:
        for layer_data in render_layers:
            parameter_values.append(
                {
                    "name": layer_data.output_file_prefix_parameter_name,
                    "value": layer_data.output_file_prefix,
                }
            )
    else:
        parameter_values.append(
            {"name": "OutputFilePrefix", "value": render_layers[0].output_file_prefix}
        )

    for layer_data in render_layers:
        if layer_data.renderer_name != "vray":
            continue
        parameter_values.append(
            {
                "name": _vrscene_parameter_name(layer_data),
                "value": vrscene_path(settings.output_path, layer_data.display_name).as_posix(),
            }
        )

    if render_layers[0].image_width_parameter_name:
        for layer_data in render_layers:
            parameter_values.append(
                {
                    "name": layer_data.image_width_parameter_name,
                    "value": layer_data.image_resolution[0],
                }
            )
            parameter_values.append(
                {
                    "name": layer_data.image_height_parameter_name,
                    "value": layer_data.image_resolution[1],
                }
            )
    else:
        parameter_values.append(
            {
                "name": "ImageWidth",
                "value": get_width(),
            }
        )
        parameter_values.append(
            {
                "name": "ImageHeight",
                "value": get_height(),
            }
        )

    # If we're rendering a specific camera, set the Camera parameter value
    if settings.camera_selection != ALL_CAMERAS:
        parameter_values.append({"name": "Camera", "value": settings.camera_selection})

    parameter_values.append({"name": "ProjectPath", "value": settings.project_path})
    parameter_values.append({"name": "OutputFilePath", "value": settings.output_path})
    parameter_values.append(
        {
            "name": "RenderSetupIncludeLights",
            "value": "true" if render_setup_include_all_lights() else "false",
        }
    )

    # Add OCIO config file parameter if present
    ocio_config = Scene.ocio_config_file()
    if ocio_config:
        parameter_values.append({"name": "OCIOConfigFile", "value": ocio_config})

    # Set the Arnold-specific parameter values
    if "arnold" in renderers:
        parameter_values.append(
            {
                "name": "ArnoldErrorOnLicenseFailure",
                "value": "true" if Scene.error_on_arnold_license_fail() else "false",
            }
        )

    # Check for any overlap between the job parameters we've defined and the
    # queue parameters. This is an error, as we weren't synchronizing the values
    # between the two different tabs where they came from.
    parameter_names = {param["name"] for param in parameter_values}
    queue_parameter_names = {param["name"] for param in queue_parameters}
    parameter_overlap = parameter_names.intersection(queue_parameter_names)
    if parameter_overlap:
        raise DeadlineOperationError(
            "The following queue parameters conflict with the Maya job parameters:\n"
            + f"{', '.join(parameter_overlap)}"
        )

    # If we're overriding the adaptor with wheels, remove the adaptor from the Packages parameters
    if settings.include_adaptor_wheels:
        wheels_path = str(Path(__file__).parent.parent.parent.parent / "wheels")
        parameter_values.append({"name": "OverrideAdaptorWheels", "value": wheels_path})

        rez_param = {}
        conda_param = {}
        # Find the Packages parameter definition
        for param in queue_parameters:
            if param["name"] == "RezPackages":
                rez_param = param
            if param["name"] == "CondaPackages":
                conda_param = param
        # Remove the deadline_cloud_for_maya/maya-openjd package
        if rez_param:
            current_value = rez_param.get("value", rez_param.get("default", ""))
            rez_param["value"] = " ".join(
                pkg
                for pkg in current_value.split()
                if not pkg.startswith("deadline_cloud_for_maya")
            )
        if conda_param:
            current_value = conda_param.get("value", conda_param.get("default", ""))
            conda_param["value"] = " ".join(
                pkg for pkg in current_value.split() if not pkg.startswith("maya-openjd")
            )

    # Only include queue parameters that have a value set (either explicitly or from default)
    parameter_values.extend(
        {"name": param["name"], "value": param.get("value", param.get("default", ""))}
        for param in queue_parameters
        if "value" in param or "default" in param
    )

    return parameter_values


def _set_render_setting(load_sticky_setting: bool = False) -> RenderSubmitterUISettings:
    render_settings = RenderSubmitterUISettings()

    # Set the setting defaults that come from the scene
    render_settings.name = Path(Scene.name()).name
    render_settings.frame_list = str(Animation.frame_list())
    render_settings.project_path = Scene.project_path()
    render_settings.output_path = Scene.output_path()

    # Load the sticky settings
    if load_sticky_setting:
        render_settings.load_sticky_settings(Scene.name())

    return render_settings


def _set_render_layer_data() -> list[RenderLayerData]:
    # Create a dictionary for the layers, and accumulate data about each layer
    print(f"_set_render_layer_data - get_all_renderable_render_layer_names {time.time()}")
    render_layer_names = get_all_renderable_render_layer_names()
    if not render_layer_names:
        raise DeadlineOperationError(
            "No render layer is set as renderable. At least one must be renderable to submit a job."
        )
    print(f"_set_render_layer_data processing layers {time.time()}")
    render_layers: list[RenderLayerData] = []
    with saved_current_render_layer():
        for render_layer_name in render_layer_names:
            set_current_render_layer(render_layer_name)

            display_name = get_render_layer_display_name(render_layer_name)
            print(f"_set_render_layer_data processing layer {display_name} {time.time()}")
            renderer_name = Scene.renderer()
            renderable_camera_names = get_renderable_camera_names()
            output_directories: set[str] = set()
            for camera_name in renderable_camera_names:
                output_directories.update(
                    Scene.get_output_directories(render_layer_name, camera_name)
                )
            output_file_prefix = get_output_prefix_with_tokens()
            image_resolution = (get_width(), get_height())

            render_layers.append(
                RenderLayerData(
                    name=render_layer_name,
                    display_name=display_name,
                    renderer_name=renderer_name,
                    ui_group_label=f"Layer {display_name} Settings ({renderer_name} renderer)",
                    frames_parameter_name=None,
                    frame_range=str(Animation.frame_list()),
                    renderable_camera_names=renderable_camera_names,
                    output_directories=output_directories,
                    output_file_prefix_parameter_name=None,
                    output_file_prefix=output_file_prefix,
                    image_width_parameter_name=None,
                    image_height_parameter_name=None,
                    image_resolution=image_resolution,
                )
            )
            print(f"_set_render_layer_data done processing layer {time.time()}")

    # Sort the layers by name
    render_layers.sort(key=lambda layer: layer.display_name)

    return render_layers

def get_submit_render_layers(use_current_layer: bool) -> list[RenderLayerData]:
    """Returns the list of RenderLayerData that should be submitted.
    
    Args:
        use_current_layer (bool): Whether to only submit the current render layer, or all render layers.

    Returns:
        list[RenderLayerData]: The list of RenderLayerData that should be submitted.

    Raises:
        DeadlineOperationError: If use_current_layer is True but the current render layer is not renderable.
    
    """
    render_layers: list[RenderLayerData] = _set_render_layer_data()
    # If we're only submitting the current layer, filter our list of layers by that
    if use_current_layer:
        current_render_layer_name = get_current_render_layer_name()
        submit_render_layers = [
            layer for layer in render_layers if layer.name == current_render_layer_name
        ]
        if not submit_render_layers:
            raise DeadlineOperationError(
                f"The current render layer, {current_render_layer_name}, is not set as renderable. It must be renderable to submit as a job."
            )
        return submit_render_layers
    
    return render_layers

def get_submit_renderers(use_current_layer: bool) -> set[str]:
    """Returns the set of renderers that should be submitted.
    
    Args:
        use_current_layer (bool): Whether to only submit the current render layer, or all render layers.

    Returns:
        set[str]: The set of renderers that should be submitted.
    
    """
    render_layers = get_submit_render_layers(use_current_layer)
    return {layer_data.renderer_name for layer_data in render_layers}

def get_default_job_template() -> dict[str, Any]:
    """Returns the default job template for Maya render submissions.
    
    Returns:
        dict[str, Any]: The default job template for Maya render submissions.
    
    """
    with open(Path(__file__).parent / "default_maya_job_template.yaml") as fh:
        default_job_template = yaml.safe_load(fh)

    return default_job_template


def _prepare_render_layers_for_submission(
    settings: RenderSubmitterUISettings,
) -> tuple[list[RenderLayerData], set[str], list[str], list[str]]:
    """Prepare render layers with per-layer parameter names set based on settings.

    Args:
        settings: The render submitter UI settings.

    Returns:
        A tuple containing:
        - submit_render_layers: The list of render layers to submit with parameter names set.
        - renderers: The set of renderer names used by the layers.
        - all_layer_selectable_cameras: Cameras selectable across all layers.
        - current_layer_selectable_cameras: Cameras selectable for the current layer.
    """
    render_layers: list[RenderLayerData] = _set_render_layer_data()

    # Get selectable cameras for current layer
    current_layer_selectable_cameras: list[str] = get_renderable_camera_names()

    # Get selectable cameras for all layers
    all_layer_selectable_cameras_set: set[str] = set(render_layers[0].renderable_camera_names)
    for layer in render_layers:
        all_layer_selectable_cameras_set = all_layer_selectable_cameras_set.intersection(
            layer.renderable_camera_names
        )
    all_layer_selectable_cameras: list[str] = list(sorted(all_layer_selectable_cameras_set))

    # Determine which render layers to submit based on the settings
    submit_render_layers = get_submit_render_layers(
        settings.render_layer_selection == LayerSelection.CURRENT
    )

    # Check if there are multiple frame ranges across the layers
    first_frame_range = submit_render_layers[0].frame_range
    per_layer_frames_parameters = not settings.override_frame_range and any(
        layer.frame_range != first_frame_range for layer in submit_render_layers
    )

    # If there are multiple frame ranges and we're not overriding the range,
    # then we create per-layer Frames parameters.
    if per_layer_frames_parameters:
        for layer_data in submit_render_layers:
            layer_data.frames_parameter_name = f"{layer_data.display_name}Frames"

    first_output_file_prefix = submit_render_layers[0].output_file_prefix
    per_layer_output_file_prefix = any(
        layer.output_file_prefix != first_output_file_prefix for layer in submit_render_layers
    )

    if per_layer_output_file_prefix:
        for layer_data in submit_render_layers:
            layer_data.output_file_prefix_parameter_name = (
                f"{layer_data.display_name}OutputFilePrefix"
            )

    first_image_resolution = submit_render_layers[0].image_resolution
    per_layer_image_resolution = any(
        layer.image_resolution != first_image_resolution for layer in submit_render_layers
    )

    if per_layer_image_resolution:
        for layer_data in submit_render_layers:
            layer_data.image_width_parameter_name = f"{layer_data.display_name}ImageWidth"
            layer_data.image_height_parameter_name = f"{layer_data.display_name}ImageHeight"

    renderers = get_submit_renderers(settings.render_layer_selection == LayerSelection.CURRENT)
    if "vray" in renderers and len(renderers) > 1:
        raise DeadlineOperationError(
            "V-Ray Standalone submissions cannot be mixed with Maya renderers. "
            "Submit V-Ray layers separately."
        )

    return (
        submit_render_layers,
        renderers,
        all_layer_selectable_cameras,
        current_layer_selectable_cameras,
    )


def get_job_template_for_submission(
    settings: RenderSubmitterUISettings,
    host_requirements: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Generate the job template for Maya render submissions.

    This function returns the job template in its final state, ready to be
    serialized to YAML. It can be used for external integrations.

    Args:
        settings: The render submitter UI settings.
        host_requirements: Optional host requirements to inject into job steps.

    Returns:
        The job template dictionary ready for serialization.
    """
    default_job_template = get_default_job_template()

    (
        submit_render_layers,
        renderers,
        all_layer_selectable_cameras,
        current_layer_selectable_cameras,
    ) = _prepare_render_layers_for_submission(settings)

    job_template = _get_job_template(
        default_job_template=default_job_template,
        settings=settings,
        renderers=renderers,
        render_layers=submit_render_layers,
        all_layer_selectable_cameras=all_layer_selectable_cameras,
        current_layer_selectable_cameras=current_layer_selectable_cameras,
    )

    # If "HostRequirements" is provided, inject it into each of the "Step"
    if host_requirements:
        for step in job_template["steps"]:
            step["hostRequirements"] = host_requirements

    return job_template


def get_parameter_values_for_submission(
    settings: RenderSubmitterUISettings,
    queue_parameters: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Generate the parameter values for Maya render submissions.

    This function returns the parameter values in their final state, ready to be
    serialized to YAML. It can be used for external integrations.

    Args:
        settings: The render submitter UI settings.
        queue_parameters: Optional queue parameters from the job bundle. These are
            typically provided by the SubmitJobToDeadlineDialog but can be omitted
            for external API usage. When omitted, no queue-specific parameters
            (like RezPackages or CondaPackages) will be included.

    Returns:
        The parameter values list ready for serialization.
    """
    if queue_parameters is None:
        queue_parameters = []

    (
        submit_render_layers,
        renderers,
        _all_layer_selectable_cameras,
        _current_layer_selectable_cameras,
    ) = _prepare_render_layers_for_submission(settings)

    return _get_parameter_values(
        settings, renderers, submit_render_layers, queue_parameters
    )


def get_asset_references_for_submission(
    asset_references: AssetReferences,
) -> dict[str, Any]:
    """Get the asset references in dictionary form for Maya render submissions.

    This function returns the asset references in their final state, ready to be
    serialized to YAML. It can be used for external integrations.

    Args:
        asset_references: The asset references object.

    Returns:
        The asset references dictionary ready for serialization.
    """
    return asset_references.to_dict()


def get_queue_parameters(
    farm_id: Optional[str] = None,
    queue_id: Optional[str] = None,
    initial_values: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Get queue parameters from Deadline Cloud for external API usage.

    This function retrieves queue parameter definitions from the Deadline Cloud API
    and optionally applies initial values. It can be used to construct queue_parameters
    for get_parameter_values_for_submission() without going through the UI.

    Args:
        farm_id: The farm ID. If not provided, uses the default from settings.
        queue_id: The queue ID. If not provided, uses the default from settings.
        initial_values: Optional dict of {parameter_name: value} to override
            default parameter values. For example:
            {"RezPackages": "maya-2024 deadline_cloud_for_maya"}

    Returns:
        A list of parameter definition dicts with "name" and "value" keys,
        suitable for passing to get_parameter_values_for_submission().

    Raises:
        DeadlineOperationError: If farm_id or queue_id are not configured.

    Example:
        >>> queue_params = get_queue_parameters(
        ...     initial_values={"RezPackages": "maya-2024"}
        ... )
        >>> param_values = get_parameter_values_for_submission(settings, queue_params)
    """
    if farm_id is None:
        farm_id = get_setting("defaults.farm_id")
    if queue_id is None:
        queue_id = get_setting("defaults.queue_id")

    if not farm_id or not queue_id:
        raise DeadlineOperationError(
            "Farm ID and Queue ID must be configured. "
            "Either provide them as arguments or configure them in Deadline Cloud settings."
        )

    # Fetch queue parameter definitions from the API
    queue_parameters = get_queue_parameter_definitions(farmId=farm_id, queueId=queue_id)

    # Apply initial values if provided
    if initial_values:
        for parameter in queue_parameters:
            if parameter["name"] in initial_values:
                parameter["value"] = initial_values[parameter["name"]]

    return cast(list[dict[str, Any]], queue_parameters)


def on_create_job_bundle_callback(
    widget: SubmitJobToDeadlineDialog,
    job_bundle_dir: str,
    settings: RenderSubmitterUISettings,
    queue_parameters: list[JobParameter],
    asset_references: AssetReferences,
    host_requirements: Optional[dict[str, Any]] = None,
    purpose: JobBundlePurpose = JobBundlePurpose.SUBMISSION,
) -> dict[str, Any]:
    render_settings = _set_render_setting()
    render_layers: list[RenderLayerData] = _set_render_layer_data()

    # Tell the settings tab the selectable cameras when only the current layer is in the job
    current_layer_selectable_cameras: list[str] = get_renderable_camera_names()
    render_settings.current_layer_selectable_cameras = [ALL_CAMERAS] + sorted(
        current_layer_selectable_cameras
    )

    # Tell the settings tab the selectable cameras when all layers are in the job
    all_layer_selectable_cameras_set: set[str] = set(render_layers[0].renderable_camera_names)
    for layer in render_layers:
        all_layer_selectable_cameras_set = all_layer_selectable_cameras_set.intersection(
            layer.renderable_camera_names
        )
    all_layer_selectable_cameras: list[str] = list(sorted(all_layer_selectable_cameras_set))
    render_settings.all_layer_selectable_cameras = [ALL_CAMERAS] + all_layer_selectable_cameras

    # if submitting, warn if the current scene has been modified
    scene_modified = maya.cmds.file(q=True, mf=True) == 1
    if scene_modified and purpose == JobBundlePurpose.SUBMISSION:
        scene_name = maya.cmds.file(q=True, sn=True)
        button = maya.cmds.confirmDialog(
            title="Warning: Scene Changes not Saved",
            message=(
                "The scene has unsaved local changes that will not be included in the job submission.\n\nDo you want to save the scene to %s before submitting?"
                % scene_name
            ),
            button=["Yes", "No"],
            defaultButton="No",
            cancelButton="No",
            dismissString="No",
        )
        if button == "Yes":
            maya.cmds.file(save=True)

    selected_layers = get_submit_render_layers(
        settings.render_layer_selection == LayerSelection.CURRENT
    )
    exported_vrscenes: list[str] = []
    exported_vrscene_assets: set[str] = set()
    exported_vrscene_directories: set[str] = set()
    for layer_data in selected_layers:
        if layer_data.renderer_name != "vray":
            continue
        scene_path = vrscene_path(settings.output_path, layer_data.display_name)
        camera_name = layer_data.renderable_camera_names[0]
        exported_vrscenes.append(
            export_vrscene(scene_path, layer_data.name, camera_name).as_posix()
        )
        exported_vrscene_assets.update(vrscene_asset_paths(scene_path))
        exported_vrscene_assets.update(vrscene_referenced_asset_paths(scene_path))
        exported_vrscene_directories.add(scene_path.parent.as_posix())

    job_bundle_path = Path(job_bundle_dir)

    # Get job bundle content using the extracted functions
    job_template = get_job_template_for_submission(settings, host_requirements)
    parameter_values = get_parameter_values_for_submission(
        settings, cast(list[dict[str, Any]], queue_parameters)
    )
    attachments: AssetReferences = widget.job_attachments.attachments
    attachments.input_filenames.update(exported_vrscenes)
    attachments.input_filenames.update(exported_vrscene_assets)
    attachments.input_directories.update(exported_vrscene_directories)
    ocio_config = Scene.ocio_config_file()
    if ocio_config and os.path.isfile(ocio_config):
        attachments.input_filenames.add(os.path.normpath(ocio_config))
    asset_references.input_filenames.update(attachments.input_filenames)
    asset_references.input_directories.update(attachments.input_directories)
    asset_references.output_directories.update(attachments.output_directories)
    asset_references_dict = get_asset_references_for_submission(asset_references)

    with open(job_bundle_path / "template.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(job_template, f, indent=1)

    with open(job_bundle_path / "parameter_values.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump({"parameterValues": parameter_values}, f, indent=1)

    with open(job_bundle_path / "asset_references.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(asset_references_dict, f, indent=1)

    # Save Sticky Settings
    settings.input_filenames = sorted(attachments.input_filenames)
    settings.input_directories = sorted(attachments.input_directories)
    settings.input_filenames = sorted(attachments.input_filenames)

    settings.save_sticky_settings(Scene.name())

    return {
        "job_parameters": parameter_values,
    }


def show_maya_render_submitter(
    parent, f=Qt.WindowFlags(), load_sticky_setting: bool = False
) -> Optional[SubmitJobToDeadlineDialog]:
    print("Starting Maya render submitter")

    # Create and show a progress dialog
    from qtpy.QtWidgets import QProgressDialog
    from qtpy.QtCore import Qt  # type: ignore

    progress_dialog = QProgressDialog("Initializing...", "", 0, 1, parent)
    progress_dialog.setWindowTitle("Asset Detection")
    progress_dialog.setWindowModality(Qt.WindowModal)
    progress_dialog.setCancelButton(None)  # Remove cancel button
    progress_dialog.setMinimumDuration(0)  # Show immediately
    progress_dialog.setValue(0)

    # Create a callback function to update the progress dialog
    def update_progress(message):
        progress_dialog.setLabelText(message)
        maya.cmds.refresh(force=True)
        time.sleep(0.05)  # Add a small sleep to ensure the UI has time to update

    # Initialize with first message
    update_progress("Loading render settings...")
    render_settings = _set_render_setting(load_sticky_setting)

    update_progress("Processing render layers...")
    render_layers: list[RenderLayerData] = _set_render_layer_data()
    print(f"Render layers processed at {time.time()}")
    all_renderers: set[str] = {layer_data.renderer_name for layer_data in render_layers}
    only_vray = all_renderers == {"vray"}
    exported_vrscenes: list[str] = []
    selected_layers = get_submit_render_layers(
        render_settings.render_layer_selection == LayerSelection.CURRENT
    )
    for layer_data in selected_layers:
        if layer_data.renderer_name != "vray":
            continue
        scene_path = vrscene_path(
            render_settings.output_path, layer_data.display_name
        )
        exported_vrscenes.append(
            export_vrscene(
                scene_path,
                layer_data.name,
                layer_data.renderable_camera_names[0],
            ).as_posix()
        )

    auto_detected_attachments = AssetReferences()
    introspector = AssetIntrospector()
    print(f"Asset introspector initialized at {time.time()}")
    update_progress("Analyzing scene assets...")
    scene_assets = list(introspector.parse_scene_assets(progress_callback=update_progress))
    total_assets = len(scene_assets)

    # Update progress dialog with total assets
    progress_dialog.setMaximum(total_assets)
    progress_dialog.setValue(0)

    # Process assets with progress updates
    processed_files = set()
    processed_directories = set()
    print(f"Starting to process {total_assets} scene assets...")

    for i, asset_path in enumerate(scene_assets):
        progress_dialog.setValue(i)
        normalized = os.path.normpath(asset_path)
        if not os.path.exists(normalized):
            continue
        if os.path.isdir(normalized):
            processed_directories.add(normalized)
        else:
            processed_files.add(normalized)
        # Process in larger batches to improve performance - refresh UI every 100 assets
        if i % 100 == 0 and i > 0:
            print(f"Processed {i+1}/{total_assets} assets at {time.time()}")
            update_progress(f"Processed {i+1}/{total_assets} assets")

    progress_dialog.setValue(total_assets)
    if only_vray:
        processed_files.discard(os.path.normpath(Scene.name()))
    auto_detected_attachments.input_filenames = processed_files
    auto_detected_attachments.input_directories = processed_directories
    auto_detected_attachments.input_filenames.update(exported_vrscenes)
    for vrscene in exported_vrscenes:
        auto_detected_attachments.input_filenames.update(
            vrscene_referenced_asset_paths(Path(vrscene))
        )
        auto_detected_attachments.input_filenames.update(
            vrscene_asset_paths(Path(vrscene))
        )
    print(f"All {total_assets} assets processed at {time.time()}")
    update_progress(f"All {total_assets} assets processed")

    update_progress("Adding output directories...")
    for layer_data in render_layers:
        auto_detected_attachments.output_directories.update(layer_data.output_directories)
    print(f"Output directories added at {time.time()}")

    attachments = AssetReferences(
        input_filenames=set(render_settings.input_filenames),
        input_directories=set(render_settings.input_directories),
        output_directories=set(render_settings.output_directories),
    )
    if only_vray:
        attachments.input_filenames.discard(os.path.normpath(Scene.name()))
        asset_references.input_filenames.discard(os.path.normpath(Scene.name()))
    asset_references.input_filenames.update(exported_vrscenes)
    attachments.input_filenames.update(exported_vrscenes)
    for vrscene in exported_vrscenes:
        vrscene_path_obj = Path(vrscene)
        vrscene_referenced_assets = vrscene_referenced_asset_paths(
            vrscene_path_obj
        )
        vrscene_assets = vrscene_asset_paths(vrscene_path_obj)
        asset_references.input_filenames.update(vrscene_referenced_assets)
        attachments.input_filenames.update(vrscene_referenced_assets)
        asset_references.input_filenames.update(vrscene_assets)
        attachments.input_filenames.update(vrscene_assets)
        attachments.input_directories.add(vrscene_path_obj.parent.as_posix())
    ocio_config = Scene.ocio_config_file()
    if ocio_config and os.path.isfile(ocio_config):
        attachments.input_filenames.add(os.path.normpath(ocio_config))
        asset_references.input_filenames.add(os.path.normpath(ocio_config))

    update_progress("Preparing submission parameters...")
    maya_version = maya.cmds.about(version=True)
    if only_vray:
        rez_packages = ""
        # Service-managed fleets provide V-Ray Standalone through this package.
        # For a customer-managed fleet this field can be cleared in the dialog.
        conda_packages = "vray-standalone=7.40.09"
    else:
        rez_packages = f"mayaIO-{maya_version} deadline_cloud_for_maya"
        conda_packages = f"maya={maya_version}.* maya-openjd"
    # Initialize telemetry client, opt-out is respected
    get_deadline_cloud_library_telemetry_client().update_common_details(
        {
            "deadline-cloud-for-maya-submitter-version": version,
            "maya-version": maya_version,
        }
    )
    # Add any additional renderers that are used
    if "arnold" in all_renderers:
        rez_packages += " mtoa"
        conda_packages += " maya-mtoa"
    if "vray" in all_renderers and not only_vray:
        conda_packages += " maya-vray"
    if "redshift" in all_renderers:
        conda_packages += " maya-redshift"

    # Close the progress dialog before creating the submission dialog
    progress_dialog.close()
    print("Progress dialog closed, creating submission dialog")

    submitter_dialog = SubmitJobToDeadlineDialog(
        job_setup_widget_type=SceneSettingsWidget,
        initial_job_settings=render_settings,
        initial_shared_parameter_values={
            "RezPackages": rez_packages,
            "CondaPackages": conda_packages,
        },
        auto_detected_attachments=auto_detected_attachments,
        attachments=attachments,
        on_create_job_bundle_callback=on_create_job_bundle_callback,
        parent=parent,
        f=f,
        show_host_requirements_tab=True,
    )
    submitter_dialog.show()
    return submitter_dialog
