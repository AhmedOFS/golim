"""Configuration management for cterm"""
from pathlib import Path
import json
import os


class Config:
    """Simple config manager with auto-save."""
    SELECTED_MODEL = "selected_model"
    SMALL_MODEL = "small_model"
    BASH_UNRESTRICTED = "bash_unrestricted"
    API_PROVIDER = "api_provider"
    OPENROUTER_API_KEY = "openrouter_api_key"
    OPENROUTER_MODEL = "openrouter_model"
    OPENROUTER_SMALL_MODEL = "openrouter_small_model"
    LLAMACPP_SERVER_URL = "llamacpp_server_url"
    LLAMACPP_MODEL = "llamacpp_model"
    LLAMACPP_SMALL_MODEL = "llamacpp_small_model"

    def __init__(self):
        cfg_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        cfg_dir = Path(cfg_home) / "cterm"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        self.path = cfg_dir / "config.json"
        self.data = self._load()

    def _load(self) -> dict:
        """Load configuration from file."""
        try:
            return json.loads(self.path.read_text()) if self.path.exists() else {}
        except Exception:
            return {}

    def save(self) -> None:
        """Save configuration to file atomically."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def get(self, key: str, default=None):
        """Get a configuration value."""
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        """Set a configuration value and save."""
        self.data[key] = value
        self.save()

    def unset(self, key: str) -> None:
        """Remove a configuration value and save."""
        if key in self.data:
            del self.data[key]
            self.save()

    @property
    def selected_model(self) -> str | None:
        """Primary model used for regular chats and tool calls."""
        return self.get(self.SELECTED_MODEL)

    @property
    def small_model(self) -> str | None:
        """Optional smaller model for lightweight future tasks."""
        return self.get(self.SMALL_MODEL)

    @property
    def unrestricted_bash(self) -> bool:
        """Run bash commands through /bin/bash -c with full shell syntax."""
        return bool(self.get(self.BASH_UNRESTRICTED, False))

    @property
    def api_provider(self) -> str:
        """API provider: 'ollama', 'openrouter', or 'llamacpp'."""
        return self.get(self.API_PROVIDER, "ollama")

    @property
    def openrouter_api_key(self) -> str | None:
        """OpenRouter API key."""
        return self.get(self.OPENROUTER_API_KEY)

    @property
    def openrouter_model(self) -> str | None:
        """OpenRouter model identifier (e.g. anthropic/claude-3.5-sonnet)."""
        return self.get(self.OPENROUTER_MODEL)

    @property
    def openrouter_small_model(self) -> str | None:
        """Optional smaller OpenRouter model for lightweight tasks."""
        return self.get(self.OPENROUTER_SMALL_MODEL)

    @property
    def llamacpp_server_url(self) -> str:
        """llama.cpp server URL."""
        return self.get(self.LLAMACPP_SERVER_URL, "http://127.0.0.1:8083")

    @property
    def llamacpp_model(self) -> str | None:
        """llama.cpp model identifier."""
        return self.get(self.LLAMACPP_MODEL)

    @property
    def llamacpp_small_model(self) -> str | None:
        """Optional smaller llama.cpp model for lightweight tasks."""
        return self.get(self.LLAMACPP_SMALL_MODEL)
