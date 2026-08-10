#!/usr/bin/env python3
"""Assemble the relocatable macOS Cterm application runtime."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release.release_builder import ReleaseConfig, cli, run_cli  # noqa: E402

BUILD_RUNTIME = ROOT / "scripts" / "build" / "macos" / "build_runtime.py"
BUILD_DIR = ROOT / "build" / "macos" / "cpython"
DIST_DIR = ROOT / "dist"
MACOS_RUNTIME = DIST_DIR / "macos"
RELEASE_DIR = DIST_DIR
CONFIG = ReleaseConfig("macos/release.py", BUILD_RUNTIME, BUILD_DIR, MACOS_RUNTIME, keep_interpreter=True)


def main(argv: list[str] | None = None) -> int:
    return cli(CONFIG, "macos", argv)


if __name__ == "__main__":
    run_cli(CONFIG, "macos")
