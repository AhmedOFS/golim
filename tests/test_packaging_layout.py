import platform
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.build.linux import build_runtime as linux_build
from scripts.build.macos import build_runtime as macos_build
from scripts.release.release_builder import (
    compile_linux_launcher,
    remove_interpreter_tools,
)
from scripts.release.linux.release import LINUX_RUNTIME
from scripts.release.macos.release import MACOS_RUNTIME


class PackagingLayoutTests(unittest.TestCase):
    def test_platform_builders_use_separate_output_trees(self):
        self.assertEqual(linux_build.BUILD_DIR, Path("build/linux").resolve())
        self.assertEqual(macos_build.BUILD_DIR, Path("build/macos").resolve())
        self.assertEqual(linux_build.RUNTIME_DIR, linux_build.BUILD_DIR / "cpython")
        self.assertEqual(macos_build.RUNTIME_DIR, macos_build.BUILD_DIR / "cpython")
        self.assertEqual(LINUX_RUNTIME, Path("dist/linux").resolve())
        self.assertEqual(MACOS_RUNTIME, Path("dist/macos").resolve())

    def test_macos_targets_cover_intel_and_apple_silicon(self):
        with patch.object(platform, "system", return_value="Darwin"):
            with patch.object(platform, "machine", return_value="x86_64"):
                self.assertEqual(
                    macos_build.standalone_target(), "x86_64-apple-darwin"
                )
            with patch.object(platform, "machine", return_value="arm64"):
                self.assertEqual(
                    macos_build.standalone_target(), "aarch64-apple-darwin"
                )

    def test_linux_launcher_uses_rpath_not_runpath_over_library_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "include" / "python3.14").mkdir(parents=True)
            (runtime / "lib").mkdir()
            (runtime / "lib" / "libpython3.14.so").touch()
            (runtime / "cterm").touch()
            calls = []

            def record_called_process(command, **kwargs):
                calls.append(list(command))
                return None

            with patch("scripts.release.release_builder._compiler", return_value="cc"):
                with patch(
                    "scripts.release.release_builder.run", side_effect=record_called_process
                ):
                    compile_linux_launcher(runtime)

            command = calls[0]
            self.assertIn("-Wl,-rpath,$ORIGIN/lib", command)
            self.assertIn("-Wl,--disable-new-dtags", command)
            self.assertLess(
                command.index("-Wl,-rpath,$ORIGIN/lib"),
                command.index("-Wl,--disable-new-dtags"),
            )

    def test_macos_release_keeps_only_python_interpreter_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary) / "bin"
            bin_dir.mkdir()
            for name in ("python3.14", "python3.14-config", "cterm", "pip"):
                (bin_dir / name).write_text("tool")

            remove_interpreter_tools(Path(temporary), keep_interpreter=True)

            self.assertTrue((bin_dir / "python3.14").exists())
            self.assertFalse((bin_dir / "python3.14-config").exists())
            self.assertFalse((bin_dir / "cterm").exists())
            self.assertFalse((bin_dir / "pip").exists())


if __name__ == "__main__":
    unittest.main()
