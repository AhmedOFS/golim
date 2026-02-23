"""Configuration management for cterm"""
from pathlib import Path
import json
import os


class Config:
    """Simple config manager with auto-save."""
    
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