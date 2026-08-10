#!/usr/bin/env python3
"""Create a Debian package from the assembled Linux Cterm distribution."""

from __future__ import annotations

import argparse
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[3]
APP_DIST = ROOT / "dist" / "linux"
RELEASE_DIR = ROOT / "release"
DEB_DIR = RELEASE_DIR / "linux"
SERVICE_SOURCE = ROOT / "packaging" / "cterm-mcp.service"
POSTINSTALL_SOURCE = ROOT / "packaging" / "postinstall.sh"
POSTRM_SOURCE = ROOT / "packaging" / "postrm.sh"


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def debian_architecture() -> str:
    dpkg = shutil.which("dpkg")
    if dpkg is not None:
        result = subprocess.run(
            [dpkg, "--print-architecture"],
            check=True,
            capture_output=True,
            text=True,
        )
        architecture = result.stdout.strip()
        if architecture:
            return architecture

    architectures = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    try:
        return architectures[platform.machine().lower()]
    except KeyError as error:
        raise RuntimeError(
            f"Could not determine Debian architecture for {platform.machine()}"
        ) from error


def write_control(debian_dir: Path, version: str, architecture: str) -> None:
    (debian_dir / "control").write_text(
        """Package: cterm
"""
        f"Version: {version}\n"
        f"Architecture: {architecture}\n"
        "Section: utils\n"
        "Priority: optional\n"
        "Maintainer: Cterm Developers\n"
        "Description: Terminal interface for LLMs\n"
        " Relocatable Cterm CLI, TUI, and local MCP tool server.\n",
        encoding="utf-8",
    )


def write_service(destination: Path) -> None:
    shutil.copy2(SERVICE_SOURCE, destination)


def populate_package_tree(staging: Path, app_dist: Path) -> Path:
    debian_dir = staging / "DEBIAN"
    app_target = staging / "usr" / "lib" / "cterm"
    bin_dir = staging / "usr" / "bin"
    service_dir = staging / "usr" / "lib" / "systemd" / "user"

    debian_dir.mkdir(parents=True)
    app_target.parent.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    service_dir.mkdir(parents=True)

    shutil.copytree(app_dist, app_target, symlinks=True)
    (bin_dir / "cterm").symlink_to("../lib/cterm/cterm")
    write_service(service_dir / "cterm-mcp.service")
    shutil.copy2(POSTINSTALL_SOURCE, debian_dir / "postinst")
    (debian_dir / "postinst").chmod(0o755)
    shutil.copy2(POSTRM_SOURCE, debian_dir / "postrm")
    (debian_dir / "postrm").chmod(0o755)

    write_control(debian_dir, project_version(), debian_architecture())
    return app_target


def build_deb(output: Path, app_dist: Path = APP_DIST) -> Path:
    if not app_dist.is_dir():
        raise RuntimeError(f"missing assembled Linux distribution: {app_dist}")
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        raise RuntimeError("dpkg-deb is required to create a .deb package")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cterm-deb-") as temporary:
        staging = Path(temporary) / "package"
        populate_package_tree(staging, app_dist)
        if output.exists():
            output.unlink()
        subprocess.run(
            [dpkg_deb, "--build", "--root-owner-group", str(staging), str(output)],
            check=True,
            cwd=ROOT,
        )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-dist",
        type=Path,
        default=APP_DIST,
        help="assembled Linux distribution to package",
    )
    args = parser.parse_args(argv)
    app_dist = args.app_dist if args.app_dist.is_absolute() else ROOT / args.app_dist
    output = DEB_DIR / f"cterm_{project_version()}_{debian_architecture()}.deb"
    package = build_deb(output, app_dist)
    print(f"Debian package created at {package}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"create_deb.py: {error}", file=sys.stderr)
        raise SystemExit(1)
