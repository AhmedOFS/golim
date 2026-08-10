#!/usr/bin/env python3
"""Download and install the pinned native macOS Cterm runtime."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build.runtime_builder import (  # noqa: E402
    BuildConfig,
    ROOT as PROJECT_ROOT,
    artifact_name as _artifact_name,
    cli,
    standalone_target as _standalone_target,
)

PBS_RELEASE = "20251120"
BUILD_DIR = PROJECT_ROOT / "build" / "macos"
RUNTIME_DIR = BUILD_DIR / "cpython"
TARGET_MAP = {
    "x86_64": "x86_64-apple-darwin",
    "amd64": "x86_64-apple-darwin",
    "arm64": "aarch64-apple-darwin",
    "aarch64": "aarch64-apple-darwin",
}
PBS_SHA256 = {
    "cpython-3.14.0+20251120-aarch64-apple-darwin-install_only_stripped.tar.gz":
        "5995f024e9c95ccbc87b9bb97152ad1ec9efcf9d76e0574cd37dd946afbea3c6",
    "cpython-3.14.0+20251120-x86_64-apple-darwin-install_only_stripped.tar.gz":
        "eb0a5915f58c3dcb64d587e951f8942e6c9c6af63fab8bbb5596d1ce5738c2a4",
}


def standalone_target() -> str:
    return _standalone_target(TARGET_MAP)


def artifact_name(version: str, target: str) -> str:
    return _artifact_name(version, target, PBS_RELEASE)


def main() -> None:
    cli(BuildConfig("macos/build_runtime.py", BUILD_DIR, RUNTIME_DIR, TARGET_MAP, PBS_SHA256, PBS_RELEASE))


if __name__ == "__main__":
    main()
