#!/usr/bin/env python3
"""Generate openterm/version.py from the current git release tag."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "openterm" / "version.py"

_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def git_version() -> str:
    """Return the release version from the most recent git tag."""
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not determine the release version: git describe failed "
            f"({result.stderr.strip()})"
        )
    tag = result.stdout.strip()
    match = _TAG_RE.match(tag)
    if not match:
        raise RuntimeError(
            f"git tag {tag!r} is not a release tag; expected a vX.Y.Z tag"
        )
    return match.group(1)


def write_version_file(version: str) -> None:
    VERSION_FILE.write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def ensure_version_file() -> str:
    """Regenerate openterm/version.py from git and return the version."""
    version = git_version()
    write_version_file(version)
    return version


def current_version() -> str:
    """Return the generated openterm/version.py value, regenerating when missing."""
    if VERSION_FILE.is_file():
        match = re.search(
            r'^__version__\s*=\s*"([^"]+)"\s*$',
            VERSION_FILE.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    return ensure_version_file()


def main(argv: list[str] | None = None) -> int:
    version = ensure_version_file()
    print(f"Generated {VERSION_FILE} with version {version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"scripts/var_setup.py: {error}", file=sys.stderr)
        raise SystemExit(1)
