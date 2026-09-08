"""Shared implementation for platform-specific runtime builders."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
PBS_BASE_URL = "https://github.com/astral-sh/python-build-standalone/releases/download"


@dataclass(frozen=True)
class BuildConfig:
    name: str
    build_dir: Path
    runtime_dir: Path
    target_map: dict[str, str]
    checksums: dict[str, str]
    release: str


def required_python_version() -> str:
    with PYPROJECT.open("rb") as stream:
        requirement = tomllib.load(stream)["project"].get("requires-python", "")
    match = re.fullmatch(r"\s*==\s*(\d+\.\d+\.\d+)\s*", requirement)
    if not match:
        raise RuntimeError(f"pyproject.toml must pin requires-python exactly; found {requirement!r}")
    return match.group(1)


def standalone_target(target_map: dict[str, str]) -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system not in {"Linux", "Darwin"}:
        raise RuntimeError(f"Unsupported host platform: {system} {machine}")
    try:
        return target_map[machine]
    except KeyError as error:
        raise RuntimeError(f"Unsupported host architecture: {machine}") from error


def artifact_name(version: str, target: str, release: str) -> str:
    return f"cpython-{version}+{release}-{target}-install_only_stripped.tar.gz"


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "golim-build"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        total = int(response.headers.get("Content-Length", "0"))
        received = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            received += len(chunk)
            if total:
                print(f"\rDownloading: {received / total:.0%}", end="", flush=True)
    if total:
        print()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def extract_runtime(archive: Path, destination: Path, build_dir: Path) -> None:
    staging = Path(tempfile.mkdtemp(prefix="cpython-", dir=build_dir))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        extracted = staging / "python"
        if not (extracted / "bin").is_dir():
            raise RuntimeError("Standalone archive does not contain a python/bin directory")
        remove_path(destination)
        shutil.move(str(extracted), str(destination))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def runtime_python(runtime_dir: Path, version: str) -> Path:
    major_minor = ".".join(version.split(".")[:2])
    for candidate in (
        runtime_dir / "bin" / f"python{major_minor}",
        runtime_dir / "bin" / "python3",
        runtime_dir / "python.exe",
    ):
        if candidate.is_file():
            return candidate
    raise RuntimeError("Could not find the extracted runtime interpreter")


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def clean_build_dir(build_dir: Path) -> None:
    remove_path(build_dir)
    build_dir.mkdir(parents=True)


def clean_build_artifacts(build_dir: Path, runtime_dir: Path) -> None:
    for path in build_dir.iterdir():
        if path.name != runtime_dir.name:
            remove_path(path)


def build(config: BuildConfig) -> None:
    clean_build_dir(config.build_dir)
    version = required_python_version()
    target = standalone_target(config.target_map)
    name = artifact_name(version, target, config.release)
    expected_hash = config.checksums.get(name)
    if expected_hash is None:
        raise RuntimeError(f"No pinned SHA-256 is available for {name}; add it before building")
    archive = config.build_dir / f".{name}"
    url = f"{PBS_BASE_URL}/{config.release}/{urllib.parse.quote(name, safe='-._')}"
    try:
        print(f"Python requirement: {version}")
        print(f"Standalone target: {target}")
        print(f"Downloading: {url}")
        download(url, archive)
        actual_hash = sha256(archive)
        if actual_hash != expected_hash:
            raise RuntimeError(f"SHA-256 mismatch for {name}: expected {expected_hash}, got {actual_hash}")
        print(f"SHA-256 verified: {actual_hash}")
        extract_runtime(archive, config.runtime_dir, config.build_dir)
    finally:
        archive.unlink(missing_ok=True)
    python = runtime_python(config.runtime_dir, version)
    try:
        run([str(python), "-m", "pip", "install", "--no-cache-dir", "."])
    finally:
        clean_build_artifacts(config.build_dir, config.runtime_dir)
    run([str(python), "-m", "pip", "check"])
    run([str(config.runtime_dir / "bin" / "golim"), "--version"])
    print(f"Runtime ready at {config.runtime_dir}")


def cli(config: BuildConfig) -> None:
    try:
        build(config)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"{config.name}: {error}", file=sys.stderr)
        raise SystemExit(1)
