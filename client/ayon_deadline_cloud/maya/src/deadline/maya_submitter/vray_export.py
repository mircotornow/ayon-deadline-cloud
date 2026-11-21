"""Local V-Ray scene export helpers."""
from __future__ import annotations

import os
import re
from pathlib import Path

import maya.cmds
import maya.mel

from .cameras import get_renderable_camera_names
from .render_layers import saved_current_render_layer, set_current_render_layer


def _make_home_paths_relative(scene_path: Path) -> None:
    """Rewrite workstation-home paths relative to the exported scene."""
    home = Path.home().as_posix().rstrip("/")
    scene_directory = scene_path.parent
    try:
        contents = scene_path.read_text(
            encoding="utf-8", errors="surrogateescape"
        )
    except OSError:
        return

    def replace_path(match: re.Match[str]) -> str:
        path = Path(match.group(1))
        try:
            path.relative_to(home)
            resolved = Path(os.path.relpath(path, scene_directory))
        except ValueError:
            return match.group(0)
        return f'"{resolved.as_posix()}"'

    contents = re.sub(r'"(' + re.escape(home) + r'/[^"]+)"', replace_path, contents)
    scene_path.write_text(contents, encoding="utf-8", errors="surrogateescape")


def vrscene_path(output_path: str, layer_name: str) -> Path:
    """Return the deterministic local path used for a layer's vrscene."""
    safe_layer_name = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in layer_name
    )
    # Keep exported scenes outside the render output root so the publish
    # collector only discovers rendered images.
    return (
        Path(output_path).parent
        / ".ayon"
        / "deadline_cloud"
        / f"{safe_layer_name}.vrscene"
    ).resolve()


def vrscene_asset_paths(scene_path: Path) -> set[str]:
    """Return the exported scene and any V-Ray sidecar files beside it."""
    if not scene_path.parent.is_dir():
        return set()
    return {
        path.as_posix()
        for path in scene_path.parent.rglob("*")
        if path.is_file()
    }


def vrscene_referenced_asset_paths(scene_path: Path) -> set[str]:
    """Return existing file paths referenced by an exported V-Ray scene."""
    asset_paths: set[str] = set()
    files_to_scan: list[Path] = []
    if scene_path.is_file():
        files_to_scan.append(scene_path)
    if scene_path.parent.is_dir():
        for path in scene_path.parent.glob("*"):
            if (
                path.is_file()
                and path.suffix.lower() in {".vrfiles", ".vrscene", ".vrdata"}
                and path not in files_to_scan
            ):
                files_to_scan.append(path)
    for file_path in files_to_scan:
        try:
            contents = file_path.read_text(
                encoding="utf-8", errors="surrogateescape"
            )
        except OSError:
            continue

        for match in re.finditer(r'"([^"\r\n]+)"', contents):
            raw = match.group(1).strip()
            # Fast filter: skip empty strings, null bytes, excessively long paths,
            # or V-Ray inline binary/compressed data (e.g. ZIPC, HEX chunks)
            if (
                not raw
                or "\0" in raw
                or len(raw) > 1024
                or raw.startswith(("ZIPC", "HEX", "data:"))
            ):
                continue

            # Path components cannot exceed 255 characters on standard filesystems
            parts = re.split(r"[\\/]", raw)
            if any(len(part) > 255 for part in parts):
                continue

            # Skip single-token strings without extensions (e.g. shader/attribute/node names)
            if len(parts) == 1 and "." not in raw:
                continue

            candidate_str = raw.replace("\\\\", "/").replace("\\", "/")

            # Support UDIM tile patterns
            if "<udim>" in candidate_str.lower():
                try:
                    pattern_str = re.sub(r'<[^>]+>', '*', candidate_str)
                    pattern_path = Path(pattern_str)
                    search_dir = pattern_path.parent
                    if not search_dir.is_absolute():
                        search_dir = (file_path.parent / search_dir).resolve()
                    if search_dir.is_dir():
                        for matched in search_dir.glob(pattern_path.name):
                            if matched.is_file():
                                asset_paths.add(matched.resolve().as_posix())
                except (OSError, ValueError):
                    pass
                continue

            try:
                candidate = Path(candidate_str)
                # Check absolute paths directly; resolve relative paths against the scene directory
                if candidate.is_absolute():
                    if candidate.is_file():
                        asset_paths.add(candidate.resolve().as_posix())
                else:
                    scene_relative = (file_path.parent / candidate).resolve()
                    if scene_relative.is_file():
                        asset_paths.add(scene_relative.as_posix())
                    elif candidate.is_file():
                        asset_paths.add(candidate.resolve().as_posix())
            except (OSError, ValueError):
                continue

    return asset_paths


def export_vrscene(
    output_path: Path,
    layer_name: str,
    camera_name: str | None = None,
) -> Path:
    """Export one V-Ray scene locally for V-Ray Standalone rendering."""
    if not maya.cmds.pluginInfo("vrayformaya", query=True, loaded=True):
        raise RuntimeError("V-Ray for Maya must be loaded before exporting a vrscene.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path = output_path.resolve()

    if camera_name is None:
        cameras = get_renderable_camera_names()
        if not cameras:
            raise RuntimeError(f"No renderable camera found for layer {layer_name}.")
        camera_name = cameras[0]

    if not maya.cmds.objExists("vraySettings"):
        maya.mel.eval("vrayCreateVRaySettingsNode()")

    settings = {
        "vrscene_render_on": 0,
        "vrscene_on": 1,
        "vrscene_filename": output_path.as_posix(),
        "misc_eachFrameInFile": 0,
        "autoLayerTokenInAbsPath": 0,
        "misc_sidecarFilesOn": 1,
    }
    original_settings = {
        name: maya.cmds.getAttr(f"vraySettings.{name}")
        for name in settings
    }

    with saved_current_render_layer():
        set_current_render_layer(layer_name)
        for name, value in settings.items():
            if isinstance(value, str):
                maya.cmds.setAttr(
                    f"vraySettings.{name}", value, type="string"
                )
            else:
                maya.cmds.setAttr(f"vraySettings.{name}", value)
        try:
            result = maya.mel.eval(
                f'vrend -camera "{camera_name}"'
            )
        finally:
            for name, value in original_settings.items():
                if isinstance(value, str):
                    maya.cmds.setAttr(
                        f"vraySettings.{name}", value, type="string"
                    )
                else:
                    maya.cmds.setAttr(f"vraySettings.{name}", value)

    if result != 0 or not output_path.is_file():
        raise RuntimeError(f"V-Ray scene export failed: {output_path}")

    _make_home_paths_relative(output_path)

    return output_path
