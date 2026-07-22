import json
import os


def _read_cterm_config() -> dict:
    """Read the nested cterm configuration schema, returning {} on I/O errors."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    config_path = os.path.join(config_home, "cterm", "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("providers"), dict) or not isinstance(data.get("attributes"), dict):
            return {}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
