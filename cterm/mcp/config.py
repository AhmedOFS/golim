"""Singleton configuration and privileged-command state for the MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path


class Config:
    """Own MCP configuration reads and privileged whitelist updates."""

    @property
    def config_path(self) -> Path:
        config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(config_home) / "cterm" / "config.json"

    @property
    def whitelist_path(self) -> Path:
        override = os.environ.get("CTERM_PRIVILEGED_WHITELIST")
        if override:
            return Path(override)
        config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(config_home) / "cterm" / "privileged_whitelist"

    def read(self) -> dict:
        path = self.config_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = {}
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("providers"), dict)
            or not isinstance(data.get("attributes"), dict)
        ):
            data = {}
        return data

    def attribute(self, key: str, default=None):
        return self.read().get("attributes", {}).get(key, default)

    @property
    def unrestricted_bash(self) -> bool:
        return bool(self.attribute("bash_unrestricted"))

    @property
    def websearch_provider(self) -> str:
        provider = self.attribute("websearch_provider", "exa")
        return provider if provider in ("exa", "parallel") else "exa"

    @property
    def exa_api_key(self) -> str | None:
        return self.attribute("exa_api_key") or os.environ.get("EXA_API_KEY") or None

    @property
    def parallel_api_key(self) -> str | None:
        return self.attribute("parallel_api_key") or os.environ.get("PARALLEL_API_KEY") or None

    @staticmethod
    def _normalise_binary(binary: str) -> str:
        return str(Path(binary).resolve())

    def read_whitelist(self) -> set[str]:
        whitelist_path = self.whitelist_path
        try:
            lines = whitelist_path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            allowed = set()
        else:
            allowed = {
                self._normalise_binary(line.split("#", 1)[0].strip())
                for line in lines
                if line.split("#", 1)[0].strip()
            }
        return set(allowed)

    def is_privileged_binary_allowed(self, binary: str) -> bool:
        return self._normalise_binary(binary) in self.read_whitelist()

    def add_privileged_binary(self, binary: str) -> None:
        whitelist_path = self.whitelist_path
        whitelist_path.parent.mkdir(parents=True, exist_ok=True)
        allowed = self.read_whitelist()
        allowed.add(self._normalise_binary(binary))
        tmp = whitelist_path.with_suffix(".tmp")
        tmp.write_text(
            "".join(f"{entry}\n" for entry in sorted(allowed)),
            encoding="utf-8",
        )
        tmp.replace(whitelist_path)


config = Config()


def get_config() -> Config:
    return config
