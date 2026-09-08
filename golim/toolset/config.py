"""Singleton configuration and privileged-command state for the MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path

from golim import config
from golim.config.app_home import get_app_home


class ServerConfig:
    """Own MCP configuration reads and privileged whitelist updates."""

    def __init__(self):
        self.app_home = get_app_home()

    @property
    def config_path(self) -> Path:
        return self.app_home / "config" / "config.json"

    @property
    def whitelist_path(self) -> Path:
        override = os.environ.get("GOLIM_PRIVILEGED_WHITELIST")
        if override:
            return Path(override)
        return self.app_home / "privileged_whitelist"

    def read(self) -> dict:
        path = self.config_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = {}
        if (
            not isinstance(data, dict)
            or not isinstance(data.get(config.Config.PROVIDERS), dict)
            or not isinstance(data.get(config.Config.ATTRIBUTES), dict)
        ):
            data = {}
        return data

    def attribute(self, key: str, default=None):
        return self.read().get(config.Config.ATTRIBUTES, {}).get(key, default)

    @property
    def unrestricted_mode(self) -> bool:
        return bool(self.attribute(config.Config.UNRESTRICTED_MODE))

    @property
    def websearch_provider(self) -> str:
        return self.attribute(config.Config.WEBSEARCH_PROVIDER, config.Config.EXA)

    @property
    def exa_api_key(self) -> str | None:
        return self.attribute(config.Config.EXA_API_KEY) or os.environ.get("EXA_API_KEY") or None

    @property
    def parallel_api_key(self) -> str | None:
        return self.attribute(config.Config.PARALLEL_API_KEY) or os.environ.get("PARALLEL_API_KEY") or None

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


_server_config: ServerConfig | None = None


def init_config() -> ServerConfig:
    """Create and install the MCP config singleton for the current app home."""
    global _server_config
    _server_config = ServerConfig()
    return _server_config


def get_config() -> ServerConfig:
    global _server_config
    app_home = get_app_home()
    if _server_config is None or getattr(_server_config, "app_home", None) != app_home:
        _server_config = ServerConfig()
    return _server_config
