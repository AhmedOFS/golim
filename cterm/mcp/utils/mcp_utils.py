import json
import os


def _read_cterm_config() -> dict:
    """Read ~/.config/cterm/config.json, returning {} on any error.

    Merges the ``attributes`` sub-dict into the top level so callers
    that are unaware of the nested schema (MCP bash/web helpers) can
    look up keys like ``bash_unrestricted`` at the root regardless of
    whether the file uses the flat or the ``{providers, attributes}``
    schema layout.
    """
    config_path = os.path.expanduser("~/.config/cterm/config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        if isinstance(data.get("attributes"), dict):
            merged = dict(data)
            merged.update(data["attributes"])
            return merged
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
