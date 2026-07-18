"""Shared privileged-command whitelist helpers."""

from __future__ import annotations

import os
from pathlib import Path

def get_config_dir() -> Path:
    cfg_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(cfg_home) / "cterm"


def get_privileged_whitelist_path() -> Path:
    override = os.environ.get("CTERM_PRIVILEGED_WHITELIST")
    if override:
        return Path(override)
    return get_config_dir() / "privileged_whitelist"


def _normalise_binary(binary: str) -> str:
    return str(Path(binary).resolve())


def read_privileged_whitelist(path: Path | None = None) -> set[str]:
    whitelist_path = path or get_privileged_whitelist_path()
    try:
        lines = whitelist_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    allowed = set()
    for line in lines:
        entry = line.split("#", 1)[0].strip()
        if entry:
            allowed.add(_normalise_binary(entry))
    return allowed


def is_privileged_binary_allowed(binary: str, path: Path | None = None) -> bool:
    return _normalise_binary(binary) in read_privileged_whitelist(path)


def add_privileged_binary(binary: str, path: Path | None = None) -> None:
    whitelist_path = path or get_privileged_whitelist_path()
    whitelist_path.parent.mkdir(parents=True, exist_ok=True)
    allowed = read_privileged_whitelist(whitelist_path)
    allowed.add(_normalise_binary(binary))
    tmp = whitelist_path.with_suffix(".tmp")
    tmp.write_text(
        "".join(f"{entry}\n" for entry in sorted(allowed)),
        encoding="utf-8",
    )
    tmp.replace(whitelist_path)

