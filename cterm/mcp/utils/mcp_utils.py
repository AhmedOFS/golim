import json
import os


def _read_cterm_config() -> dict:
    """Read ~/.config/cterm/config.json, returning {} on any error."""
    config_path = os.path.expanduser("~/.config/cterm/config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
