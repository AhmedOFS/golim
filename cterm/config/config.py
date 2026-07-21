"""Configuration and recent-model persistence for cterm."""
from pathlib import Path
import json
import os


class Config:
    """Schema-backed config manager with compatibility accessors.

    The on-disk schema intentionally separates credentials/endpoints from the
    user-facing runtime attributes.  ``_load`` migrates the pre-schema flat
    configuration so existing installations continue to work.
    """

    PROVIDERS = "providers"
    ATTRIBUTES = "attributes"
    API_PROVIDER = "api_provider"
    SELECTED_MODEL = "current_model"
    SMALL_MODEL = "small_model"  # retained for the skill-selection runtime
    BASH_UNRESTRICTED = "bash_unrestricted"
    STREAM_THINKING_TRACES = "thinking_traces"
    MAX_ITERATION_LIMIT = "max_iteration_limit"
    DARK_MODE = "dark_mode"

    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    OPEN_ROUTER = "open_router"
    OLLAMA_SERVER_URL = "ollama_host"
    OPENROUTER_API_KEY = "api_key"
    LLAMACPP_SERVER_URL = "url"
    # Historical names retained for extensions and the basic fallback UI.
    OPENROUTER_MODEL = SELECTED_MODEL
    OPENROUTER_SMALL_MODEL = SMALL_MODEL
    LLAMACPP_MODEL = SELECTED_MODEL
    LLAMACPP_SMALL_MODEL = SMALL_MODEL

    _ATTRIBUTE_DEFAULTS = {
        API_PROVIDER: OLLAMA,
        SELECTED_MODEL: None,
        STREAM_THINKING_TRACES: False,
        BASH_UNRESTRICTED: False,
        MAX_ITERATION_LIMIT: 50,
        DARK_MODE: False,
    }

    def __init__(self):
        cfg_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        cfg_dir = Path(cfg_home) / "cterm"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        self.path = cfg_dir / "config.json"
        self.data = self._load()

    @property
    def models_path(self) -> Path:
        """The model recency list lives alongside the TUI prompt history."""
        data_dir = Path.home() / "cterm" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "models.json"

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text()) if self.path.exists() else {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            return self._empty_schema()
        return self._migrate(raw)

    def _empty_schema(self) -> dict:
        return {
            self.PROVIDERS: {
                self.OLLAMA: {},
                self.OPENAI_COMPATIBLE: {},
                self.OPEN_ROUTER: {},
            },
            self.ATTRIBUTES: dict(self._ATTRIBUTE_DEFAULTS),
        }

    def _migrate(self, raw: dict) -> dict:
        if self.PROVIDERS in raw or self.ATTRIBUTES in raw:
            data = self._empty_schema()
            data[self.PROVIDERS].update(raw.get(self.PROVIDERS, {}))
            # Do not silently fill missing attributes here: startup must be
            # able to send an incomplete schema through configuration.
            data[self.ATTRIBUTES] = dict(raw.get(self.ATTRIBUTES, {}))
            # Preserve unrelated existing configuration such as web search.
            data.update({k: v for k, v in raw.items() if k not in data})
            return data

        data = self._empty_schema()
        legacy_provider = raw.get("api_provider", self.OLLAMA)
        provider_map = {"openrouter": self.OPEN_ROUTER, "llamacpp": self.OPENAI_COMPATIBLE}
        data[self.ATTRIBUTES].update({
            self.API_PROVIDER: provider_map.get(legacy_provider, legacy_provider),
            self.SELECTED_MODEL: raw.get("selected_model") or raw.get("openrouter_model") or raw.get("llamacpp_model"),
            self.STREAM_THINKING_TRACES: bool(raw.get("stream_thinking_traces", False)),
            self.BASH_UNRESTRICTED: bool(raw.get("bash_unrestricted", False)),
            self.MAX_ITERATION_LIMIT: raw.get("max_iteration_limit", 50),
            self.DARK_MODE: bool(raw.get("dark_mode", False)),
        })
        data[self.PROVIDERS][self.OLLAMA][self.OLLAMA_SERVER_URL] = raw.get("ollama_server_url", "http://localhost:11434")
        data[self.PROVIDERS][self.OPEN_ROUTER][self.OPENROUTER_API_KEY] = raw.get("openrouter_api_key", "")
        data[self.PROVIDERS][self.OPENAI_COMPATIBLE].update({
            self.LLAMACPP_SERVER_URL: raw.get("llamacpp_server_url", ""),
            self.OPENROUTER_API_KEY: raw.get("llamacpp_api_key", ""),
        })
        if raw.get("small_model"):
            data[self.SMALL_MODEL] = raw["small_model"]
        data.update({k: v for k, v in raw.items() if k in {"websearch_provider", "exa_api_key", "parallel_api_key"}})
        return data

    def ensure_attribute_defaults(self) -> None:
        """Persist defaults after the configuration flow has been entered."""
        attributes = self.data[self.ATTRIBUTES]
        changed = False
        for key, value in self._ATTRIBUTE_DEFAULTS.items():
            if key not in attributes:
                attributes[key] = value
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def get(self, key: str, default=None):
        if key in self._ATTRIBUTE_DEFAULTS or key == self.SMALL_MODEL:
            return self.data[self.ATTRIBUTES].get(key, self.data.get(key, default))
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        if key == self.OLLAMA_SERVER_URL:
            self.set_provider_value(self.OLLAMA, key, value)
            return
        if key == self.LLAMACPP_SERVER_URL:
            self.set_provider_value(self.OPENAI_COMPATIBLE, key, value)
            return
        if key in self._ATTRIBUTE_DEFAULTS or key == self.SMALL_MODEL:
            self.data[self.ATTRIBUTES][key] = value
        else:
            self.data[key] = value
        self.save()

    def unset(self, key: str) -> None:
        if key == self.OLLAMA_SERVER_URL:
            self.provider(self.OLLAMA).pop(key, None)
            self.save()
            return
        if key == self.LLAMACPP_SERVER_URL:
            self.provider(self.OPENAI_COMPATIBLE).pop(key, None)
            self.save()
            return
        if key in self._ATTRIBUTE_DEFAULTS or key == self.SMALL_MODEL:
            self.data[self.ATTRIBUTES].pop(key, None)
        else:
            self.data.pop(key, None)
        self.save()

    def provider(self, name: str) -> dict:
        return self.data[self.PROVIDERS].setdefault(name, {})

    def set_provider_value(self, provider: str, key: str, value) -> None:
        self.provider(provider)[key] = value
        self.save()

    def is_complete(self) -> bool:
        attributes = self.data.get(self.ATTRIBUTES, {})
        if any(key not in attributes for key in self._ATTRIBUTE_DEFAULTS):
            return False
        provider = self.api_provider
        if provider not in {self.OLLAMA, self.OPENAI_COMPATIBLE, self.OPEN_ROUTER} or not self.selected_model:
            return False
        values = self.provider(provider)
        if provider == self.OPEN_ROUTER:
            return bool(values.get(self.OPENROUTER_API_KEY))
        if provider == self.OPENAI_COMPATIBLE:
            # Local OpenAI-compatible servers (including llama.cpp) commonly
            # do not require authentication; the URL is the required field.
            return bool(values.get(self.LLAMACPP_SERVER_URL))
        return True

    def recent_models(self) -> list[dict[str, str]]:
        try:
            models = json.loads(self.models_path.read_text()) if self.models_path.exists() else []
        except Exception:
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
    def unrestricted_bash(self): return bool(self.get(self.BASH_UNRESTRICTED, False))
    @property
    def stream_thinking_traces(self): return bool(self.get(self.STREAM_THINKING_TRACES, False))
    @property
    def max_iteration_limit(self):
        try: return max(1, int(self.get(self.MAX_ITERATION_LIMIT, 50)))
        except (TypeError, ValueError): return 50
    @property
    def dark_mode(self): return bool(self.get(self.DARK_MODE, False))
    @property
    def api_provider(self): return self.get(self.API_PROVIDER, self.OLLAMA)
    @property
    def ollama_server_url(self): return self.provider(self.OLLAMA).get(self.OLLAMA_SERVER_URL, "http://localhost:11434")
    @property
    def openrouter_api_key(self): return self.provider(self.OPEN_ROUTER).get(self.OPENROUTER_API_KEY) or None
    @property
    def llamacpp_server_url(self): return self.provider(self.OPENAI_COMPATIBLE).get(self.LLAMACPP_SERVER_URL, "http://127.0.0.1:8083")
    @property
    def llamacpp_api_key(self): return self.provider(self.OPENAI_COMPATIBLE).get(self.OPENROUTER_API_KEY) or None
    # Legacy model accessors keep callers working while all providers use current_model.
    @property
    def openrouter_model(self): return self.selected_model
    @property
    def openrouter_small_model(self): return self.small_model
    @property
    def llamacpp_model(self): return self.selected_model
    @property
    def llamacpp_small_model(self): return self.small_model
    @property
    def websearch_provider(self): return self.get("websearch_provider")
    @property
    def exa_api_key(self): return self.get("exa_api_key")
    @property
    def parallel_api_key(self): return self.get("parallel_api_key")
