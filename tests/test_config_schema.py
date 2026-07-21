import json
import os
from pathlib import Path
import tempfile
import unittest

from cterm.config import Config


class ConfigSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_xdg = os.environ.get("XDG_CONFIG_HOME")
        self.old_home = os.environ.get("HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        os.environ["HOME"] = self.tmp.name

    def tearDown(self):
        if self.old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.old_xdg
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def test_schema_separates_provider_values_and_attributes(self):
        config = Config()
        config.ensure_attribute_defaults()
        config.set_provider_value(Config.OPEN_ROUTER, Config.OPENROUTER_API_KEY, "key")
        config.set(Config.API_PROVIDER, Config.OPEN_ROUTER)
        config.set(Config.SELECTED_MODEL, "provider/model")

        saved = json.loads(config.path.read_text())
        self.assertEqual(saved["providers"]["open_router"]["api_key"], "key")
        self.assertEqual(saved["attributes"]["current_model"], "provider/model")
        self.assertTrue(config.is_complete())

    def test_missing_schema_attribute_requires_configuration(self):
        path = Path(self.tmp.name) / "cterm" / "config.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "providers": {"ollama": {"ollama_host": "http://localhost:11434"}},
            "attributes": {"api_provider": "ollama", "current_model": "model"},
        }))

        self.assertFalse(Config().is_complete())

    def test_legacy_flat_config_is_migrated_on_load(self):
        path = Path(self.tmp.name) / "cterm" / "config.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "api_provider": "openrouter",
            "openrouter_api_key": "key",
            "openrouter_model": "provider/model",
        }))

        config = Config()
        self.assertEqual(config.api_provider, Config.OPEN_ROUTER)
        self.assertEqual(config.openrouter_api_key, "key")
        self.assertEqual(config.selected_model, "provider/model")

    def test_remember_model_persists_unique_most_recent_entries(self):
        config = Config()
        config.remember_model("one", Config.OLLAMA)
        config.remember_model("two", Config.OPEN_ROUTER)
        config.remember_model("one", Config.OLLAMA)

        self.assertEqual(config.recent_models(), [
            {"model": "one", "provider": "ollama"},
            {"model": "two", "provider": "open_router"},
        ])


if __name__ == "__main__":
    unittest.main()
