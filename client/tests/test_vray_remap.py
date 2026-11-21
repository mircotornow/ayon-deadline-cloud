"""Unit tests for the embedded V-Ray Standalone worker path remapping script."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

SUBMITTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "ayon_deadline_cloud"
    / "maya"
    / "src"
    / "deadline"
    / "maya_submitter"
    / "maya_render_submitter.py"
)


def _extract_remap_script() -> str:
    """Extract the embedded `remap_python_script` string via AST."""
    tree = ast.parse(SUBMITTER_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "remap_python_script"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    return node.value.value
    raise AssertionError("Could not find remap_python_script in maya_render_submitter.py")


class TestVRayRemapScript(unittest.TestCase):
    """Verify remap_vray.py handles case, Unicode, sidecars, UDIMs, and boundaries."""

    def setUp(self) -> None:
        self.remap_script = _extract_remap_script()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script_file = self.root / "remap_vray.py"
        self.script_file.write_text(self.remap_script, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_remap(
        self,
        rules: list[dict[str, str]],
        vrscene_path: Path,
        remap_xml: Path,
    ) -> subprocess.CompletedProcess[str]:
        mapping_file = self.root / "path_mapping_rules.json"
        mapping_file.write_text(
            json.dumps({"path_mapping_rules": rules}),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["MAPPING_FILE"] = str(mapping_file)
        env["VRSCENE_PATH"] = str(vrscene_path)
        env["REMAP_OUTPUT"] = str(remap_xml)
        proc = subprocess.run(
            [sys.executable, str(self.script_file)],
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            self.fail(
                f"remap_vray.py exited with {proc.returncode}\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        return proc

    def test_case_unicode_sidecar_double_backslash_udim_and_boundary(self) -> None:
        asset_root = self.root / "assetroot-17f132e0e4ff31bbaabf"

        # Create staged files with real mixed-case names on disk
        hdri = (
            asset_root
            / "SeaDrive"
            / "paskuh"
            / unicodedata.normalize("NFC", "Für meine Gruppen")
            / "Projects"
            / "EPILOGUE"
            / "Library"
            / "Resources"
            / "Setlights"
            / "publish"
            / "image"
            / "imageHDRI_ringlights"
            / "v001"
            / "EPI_setlights_imageHDRI_ringlights_v001.exr"
        )
        hdri.parent.mkdir(parents=True, exist_ok=True)
        hdri.write_bytes(b"EXR")

        tex = (
            asset_root
            / "SeaDrive"
            / "paskuh"
            / unicodedata.normalize("NFC", "Für meine Gruppen")
            / "Projects"
            / "EPILOGUE"
            / "Assets"
            / "OA_1"
            / "publish"
            / "look"
            / "lookMain"
            / "v010"
            / "resources"
            / "ScuffsHeavy001_OVERLAY_6K.jpg"
        )
        tex.parent.mkdir(parents=True, exist_ok=True)
        tex.write_bytes(b"JPG")

        udim_tile = tex.parent / "HeroMask_1001.exr"
        udim_tile.write_bytes(b"UDIM")

        render_dir = asset_root / "Project" / "EPILOGUE" / "renders"
        render_dir.mkdir(parents=True, exist_ok=True)

        scene_dir = asset_root / "Project" / "EPILOGUE" / ".ayon" / "deadline_cloud"
        scene_dir.mkdir(parents=True, exist_ok=True)

        # NFD version of "für meine gruppen" to simulate cross-platform Unicode mismatch
        nfd_folder = unicodedata.normalize("NFD", "für meine gruppen")

        main_vrscene = scene_dir / "OA.vrscene"
        main_vrscene.write_text(
            "\n".join(
                [
                    '#include "OA.vrfiles"',
                    # Escaped double backslashes + all-lowercase + NFD umlaut
                    f'file="D:\\\\Bibliothek\\\\3D\\\\_Freelance_Projects\\\\Epilogue\\\\seadrive\\\\paskuh\\\\{nfd_folder}\\\\projects\\\\epilogue\\\\library\\\\resources\\\\setlights\\\\publish\\\\image\\\\imagehdri_ringlights\\\\v001\\\\epi_setlights_imagehdri_ringlights_v001.exr";',
                    # Non-existent output path with lowercase parent dir
                    'img_file="D:/Bibliothek/3D/_Freelance_Projects/Epilogue/project/epilogue/renders/OA.1081.exr";',
                    # Boundary test: should NOT match D:\Bibliothek\3D\_Freelance_Projects\Epilogue
                    'other="D:/Bibliothek/3D/_Freelance_Projects/EpilogueExtra/keep_me.exr";',
                ]
            ),
            encoding="utf-8",
        )

        # Companion sidecar .vrfiles containing lowercase texture & UDIM paths
        sidecar_vrfiles = scene_dir / "OA.vrfiles"
        sidecar_vrfiles.write_text(
            "\n".join(
                [
                    'file="D:/Bibliothek/3D/_Freelance_Projects/Epilogue/seadrive/paskuh/für meine gruppen/projects/epilogue/assets/oa_1/publish/look/lookmain/v010/resources/scuffsheavy001_overlay_6k.jpg";',
                    'file="D:/Bibliothek/3D/_Freelance_Projects/Epilogue/seadrive/paskuh/für meine gruppen/projects/epilogue/assets/oa_1/publish/look/lookmain/v010/resources/heromask_<UDIM>.exr";',
                ]
            ),
            encoding="utf-8",
        )

        # Binary .vrdata should be untouched
        binary_vrdata = scene_dir / "OA.vrdata"
        binary_vrdata.write_bytes(b"\x00\x01BIN\"D:/Bibliothek/3D/_Freelance_Projects/Epilogue/x\"")

        remap_xml = self.root / "vray_remap.xml"
        result = self._run_remap(
            rules=[
                {
                    "source_path": r"D:\Bibliothek\3D\_Freelance_Projects\Epilogue",
                    "destination_path": asset_root.as_posix(),
                }
            ],
            vrscene_path=main_vrscene,
            remap_xml=remap_xml,
        )

        self.assertTrue(remap_xml.is_file())
        self.assertIn("Patched: OA.vrscene", result.stdout)
        self.assertIn("Patched: OA.vrfiles", result.stdout)

        patched_main = main_vrscene.read_text(encoding="utf-8")
        self.assertIn(f'file="{hdri.as_posix()}"', patched_main)
        self.assertIn(
            f'img_file="{render_dir.as_posix()}/OA.1081.exr"',
            patched_main,
        )
        # Boundary protection: EpilogueExtra must remain untouched
        self.assertIn(
            'other="D:/Bibliothek/3D/_Freelance_Projects/EpilogueExtra/keep_me.exr"',
            patched_main,
        )

        patched_sidecar = sidecar_vrfiles.read_text(encoding="utf-8")
        self.assertIn(f'file="{tex.as_posix()}"', patched_sidecar)
        self.assertIn(
            f'file="{tex.parent.as_posix()}/HeroMask_<UDIM>.exr"',
            patched_sidecar,
        )

        # Binary .vrdata untouched
        self.assertEqual(
            binary_vrdata.read_bytes(),
            b"\x00\x01BIN\"D:/Bibliothek/3D/_Freelance_Projects/Epilogue/x\"",
        )

    def test_vray_case_insensitive_env_not_enabled(self) -> None:
        source = SUBMITTER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("VRAY_PATH_REMAPPING_CASE_SENSITIVE=0", source)
        self.assertIn("VRAY_PATH_REMAPPING_CASE_SENSITIVE=1", source)


if __name__ == "__main__":
    unittest.main()
