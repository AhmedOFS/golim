#!/usr/bin/env python3
"""Download and install the pinned Linux Openterm runtime."""

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
BUILD_DIR = PROJECT_ROOT / "build" / "linux"
RUNTIME_DIR = BUILD_DIR / "cpython"
TARGET_MAP = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "amd64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
    "arm64": "aarch64-unknown-linux-gnu",
}
PBS_SHA256 = {
    "cpython-3.14.0+20251120-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz":
        "66f385305ae0eefd6b65e2cf942bc91d943a61e26fb7581e029f760a2b04f393",
}


def standalone_target() -> str:
    return _standalone_target(TARGET_MAP)


def artifact_name(version: str, target: str) -> str:
    return _artifact_name(version, target, PBS_RELEASE)


def main() -> None:
    cli(BuildConfig("linux/build_runtime.py", BUILD_DIR, RUNTIME_DIR, TARGET_MAP, PBS_SHA256, PBS_RELEASE))


if __name__ == "__main__":
    main()
