#!/usr/bin/env python3
"""Assemble the relocatable Linux Cterm application runtime."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release.release_builder import (  # noqa: E402
    LAUNCHER_SOURCE,
    ReleaseConfig,
    assemble_runtime as _assemble_runtime,
    cli,
    compile_application,
    compile_linux_launcher,
    copy_runtime,
    flatten_site_packages,
    remove_development_files,
    remove_generated_caches,
    remove_interpreter_tools,
    run,
    run_cli,
    verify_runtime,
)

BUILD_RUNTIME = ROOT / "scripts" / "build" / "linux" / "build_runtime.py"
BUILD_DIR = ROOT / "build" / "linux" / "cpython"
DIST_DIR = ROOT / "dist"
LINUX_RUNTIME = DIST_DIR / "linux"
RELEASE_DIR = DIST_DIR
CONFIG = ReleaseConfig("linux/release.py", BUILD_RUNTIME, BUILD_DIR, LINUX_RUNTIME)
compile_launcher = compile_linux_launcher


def assemble_runtime() -> None:
    _assemble_runtime(CONFIG, "linux")


def main(argv: list[str] | None = None) -> int:
    return cli(CONFIG, "linux", argv)


if __name__ == "__main__":
    run_cli(CONFIG, "linux")
