import json
import os
from pathlib import Path
import tempfile
import unittest

from cterm.config import Config, ConfigSchemaError


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
        config.set_provider_value(Config.OPEN_ROUTER, Config.PROVIDER_API_KEY, "key")
        config.set(Config.API_PROVIDER, Config.OPEN_ROUTER)
        config.set(Config.SELECTED_MODEL, "provider/model")
        config.set(Config.SMALL_MODEL, "provider/small")
        config.set(Config.EXA_API_KEY, "exa-key")

        saved = json.loads(config.path.read_text())
        self.assertEqual(saved["providers"]["open_router"]["api_key"], "key")
        self.assertEqual(saved["attributes"]["current_model"], "provider/model")
        self.assertEqual(saved["attributes"]["small_model"], "provider/small")
        self.assertEqual(saved["attributes"]["exa_api_key"], "exa-key")
        self.assertTrue(config.is_complete())

    def test_missing_schema_attribute_requires_configuration(self):
        path = Path(self.tmp.name) / "cterm" / "config.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "providers": {"ollama": {"ollama_host": "http://localhost:11434"}},
            "attributes": {"api_provider": "ollama", "current_model": "model"},
        }))

        self.assertTrue(Config().is_complete())

    def test_legacy_flat_config_is_rejected(self):
        path = Path(self.tmp.name) / "cterm" / "config.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "api_provider": "openrouter",
            "openrouter_api_key": "key",
            "openrouter_model": "provider/model",
        }))

        with self.assertRaises(ConfigSchemaError):
            Config()

    def test_schema_is_loaded_without_filling_missing_values(self):
        path = Path(self.tmp.name) / "cterm" / "config.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"providers": {}, "attributes": {}}))

        config = Config()
        self.assertEqual(config.data, {"providers": {}, "attributes": {}})
        self.assertFalse(config.is_complete())

    def test_unset_optional_attribute_preserves_the_schema(self):
        config = Config()
        config.set(Config.SMALL_MODEL, "small/model")
        config.unset(Config.SMALL_MODEL)

        saved = json.loads(config.path.read_text())
        self.assertIn(Config.SMALL_MODEL, saved[Config.ATTRIBUTES])
        self.assertIsNone(saved[Config.ATTRIBUTES][Config.SMALL_MODEL])

    def test_provider_values_are_never_written_to_attributes(self):
        config = Config()
        config.set(Config.OLLAMA_SERVER_URL, "http://ollama.example:11434")

        saved = json.loads(config.path.read_text())
        self.assertEqual(
            saved[Config.PROVIDERS][Config.OLLAMA][Config.OLLAMA_SERVER_URL],
            "http://ollama.example:11434",
        )
        self.assertNotIn(Config.OLLAMA_SERVER_URL, saved[Config.ATTRIBUTES])

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
