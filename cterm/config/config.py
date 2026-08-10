"""Configuration and recent-model persistence for cterm."""
from __future__ import annotations

import contextvars
import json
from pathlib import Path

from cterm.app_home import get_app_home


_config_context: contextvars.ContextVar[Config | None] = contextvars.ContextVar("_config_context", default=None)


def get_config() -> Config:
    config = _config_context.get()
    if config is None:
        config = Config()
    return config


def init_config() -> Config:
    config = Config()
    _config_context.set(config)
    return config


class ConfigSchemaError(ValueError):
    """Raised when an existing config file does not use cterm's schema."""


class Config:
    """Read and write cterm's single, nested configuration schema.

    Provider credentials and endpoints live under ``providers``.  All runtime
    settings live under ``attributes``.  This class deliberately does not
    migrate, flatten, or support previous config layouts.
    """

    PROVIDERS = "providers"
    ATTRIBUTES = "attributes"
    API_PROVIDER = "api_provider"
    SELECTED_MODEL = "current_model"
    SMALL_MODEL = "small_model"
    BASH_UNRESTRICTED = "bash_unrestricted"
    STREAM_THINKING_TRACES = "thinking_traces"
    MAX_ITERATION_LIMIT = "max_iteration_limit"
    DARK_MODE = "dark_mode"
    WEBSEARCH_PROVIDER = "websearch_provider"
    EXA_API_KEY = "exa_api_key"
    PARALLEL_API_KEY = "parallel_api_key"

    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    OPEN_ROUTER = "open_router"
    OLLAMA_SERVER_URL = "ollama_host"
    PROVIDER_API_KEY = "api_key"
    OPENAI_COMPATIBLE_SERVER_URL = "url"

    _ATTRIBUTE_DEFAULTS = {
        API_PROVIDER: OLLAMA,
        SELECTED_MODEL: None,
        SMALL_MODEL: None,
        STREAM_THINKING_TRACES: False,
        BASH_UNRESTRICTED: False,
        MAX_ITERATION_LIMIT: 50,
        DARK_MODE: False,
        WEBSEARCH_PROVIDER: "exa",
        EXA_API_KEY: None,
        PARALLEL_API_KEY: None,
    }
    _PROVIDER_DEFAULTS = {
        OLLAMA: {OLLAMA_SERVER_URL: "http://localhost:11434"},
        OPENAI_COMPATIBLE: {PROVIDER_API_KEY: None, OPENAI_COMPATIBLE_SERVER_URL: None},
        OPEN_ROUTER: {PROVIDER_API_KEY: None},
    }

    def __init__(self):
        cfg_dir = get_app_home() / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        self.path = cfg_dir / "config.json"
        self.data = self._load()

    @property
    def models_path(self) -> Path:
        data_dir = get_app_home() / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "models.json"

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty_schema()
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigSchemaError(f"Invalid cterm configuration at {self.path}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get(self.PROVIDERS), dict) or not isinstance(raw.get(self.ATTRIBUTES), dict):
            raise ConfigSchemaError(
                f"Invalid cterm configuration at {self.path}: expected providers and attributes objects"
            )
        return raw

    def _empty_schema(self) -> dict:
        return {
            self.PROVIDERS: {name: dict(values) for name, values in self._PROVIDER_DEFAULTS.items()},
            self.ATTRIBUTES: dict(self._ATTRIBUTE_DEFAULTS),
        }

    def reload(self) -> None:
        self.data = self._load()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def get(self, key: str, default=None):
        return self.data[self.ATTRIBUTES].get(key, default)

    def set(self, key: str, value) -> None:
        if key == self.OLLAMA_SERVER_URL:
            self.set_provider_value(self.OLLAMA, key, value)
            return
        if key == self.OPENAI_COMPATIBLE_SERVER_URL:
            self.set_provider_value(self.OPENAI_COMPATIBLE, key, value)
            return
        self.data[self.ATTRIBUTES][key] = value
        self.save()

    def unset(self, key: str) -> None:
        if key == self.OLLAMA_SERVER_URL:
            self.provider(self.OLLAMA).pop(key, None)
        elif key == self.OPENAI_COMPATIBLE_SERVER_URL:
            self.provider(self.OPENAI_COMPATIBLE).pop(key, None)
        else:
            self.data[self.ATTRIBUTES][key] = None
        self.save()

    def provider(self, name: str) -> dict:
        return self.data[self.PROVIDERS].get(name, {})

    def set_provider_value(self, provider: str, key: str, value) -> None:
        self.data[self.PROVIDERS].setdefault(provider, {})[key] = value
        self.save()

    def is_complete(self) -> bool:
        attributes = self.data[self.ATTRIBUTES]
        required = [self.API_PROVIDER, self.SELECTED_MODEL]
        if any(key not in attributes for key in required):
            return False
        provider = self.api_provider
        if provider not in self._PROVIDER_DEFAULTS or not self.selected_model:
            return False
        values = self.provider(provider)
        if provider == self.OPEN_ROUTER:
            return bool(values.get(self.PROVIDER_API_KEY))
        if provider == self.OPENAI_COMPATIBLE:
            return bool(values.get(self.OPENAI_COMPATIBLE_SERVER_URL))
        return bool(values.get(self.OLLAMA_SERVER_URL))

    def recent_models(self) -> list[dict[str, str]]:
        try:
            models = json.loads(self.models_path.read_text()) if self.models_path.exists() else []
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in models if isinstance(item, dict) and isinstance(item.get("model"), str) and isinstance(item.get("provider"), str)]

    def remember_model(self, model: str, provider: str) -> None:
        entries = [item for item in self.recent_models() if item != {"model": model, "provider": provider}]
        entries.insert(0, {"model": model, "provider": provider})
        tmp = self.models_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2))
        tmp.replace(self.models_path)

    @property
    def selected_model(self): return self.get(self.SELECTED_MODEL)
    @property
    def small_model(self): return self.get(self.SMALL_MODEL)
    @property
    def unrestricted_bash(self): return bool(self.get(self.BASH_UNRESTRICTED))
    @property
    def stream_thinking_traces(self): return bool(self.get(self.STREAM_THINKING_TRACES))
    @property
    def max_iteration_limit(self):
        try: return max(1, int(self.get(self.MAX_ITERATION_LIMIT)))
        except (TypeError, ValueError): return 50
    @property
    def dark_mode(self): return bool(self.get(self.DARK_MODE))
    @property
    def api_provider(self): return self.get(self.API_PROVIDER)
    @property
    def ollama_server_url(self): return self.provider(self.OLLAMA).get(self.OLLAMA_SERVER_URL)
    @property
    def openrouter_api_key(self): return self.provider(self.OPEN_ROUTER).get(self.PROVIDER_API_KEY)
    @property
    def openai_compatible_server_url(self): return self.provider(self.OPENAI_COMPATIBLE).get(self.OPENAI_COMPATIBLE_SERVER_URL)
    @property
    def openai_compatible_api_key(self): return self.provider(self.OPENAI_COMPATIBLE).get(self.PROVIDER_API_KEY)
    @property
    def websearch_provider(self): return self.get(self.WEBSEARCH_PROVIDER)
    @property
    def exa_api_key(self): return self.get(self.EXA_API_KEY)
    @property
    def parallel_api_key(self): return self.get(self.PARALLEL_API_KEY)
