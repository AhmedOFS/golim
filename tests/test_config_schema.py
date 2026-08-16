import json
import os
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import MagicMock, patch

from cterm.config import Config, ConfigSchemaError
from cterm.config.utils import (
    get_configured_model_choices,
    get_openai_compatible_models,
    normalize_openai_compatible_url,
    resolve_provider_settings,
)


class ConfigSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name

    def tearDown(self):
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
        path = Path(self.tmp.name) / ".cterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "providers": {"ollama": {"ollama_host": "http://localhost:11434"}},
            "attributes": {"api_provider": "ollama", "current_model": "model"},
        }))

        self.assertTrue(Config().is_complete())

    def test_legacy_flat_config_is_rejected(self):
        path = Path(self.tmp.name) / ".cterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "api_provider": "openrouter",
            "openrouter_api_key": "key",
            "openrouter_model": "provider/model",
        }))

        with self.assertRaises(ConfigSchemaError):
            Config()

    def test_schema_is_loaded_without_filling_missing_values(self):
        path = Path(self.tmp.name) / ".cterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
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

    def test_model_picker_keeps_configured_remote_model_when_catalogue_is_unavailable(self):
        config = Config()
        config.set_provider_value(Config.OPEN_ROUTER, Config.PROVIDER_API_KEY, "key")
        config.set(Config.API_PROVIDER, Config.OPEN_ROUTER)
        config.set(Config.SELECTED_MODEL, "provider/selected")

        with patch("cterm.config.utils.get_models", return_value=[]), \
             patch("cterm.config.utils.get_openrouter_models", return_value=[]), \
             patch("cterm.config.utils.get_openai_compatible_models", return_value=[]):
            labels, choices = get_configured_model_choices(config, "ollama")

        self.assertEqual(labels, ["open_router: provider/selected"])
        self.assertEqual(choices, {"open_router: provider/selected": ("open_router", "provider/selected")})

    def test_provider_settings_reject_noncanonical_provider_values(self):
        config = MagicMock(
            api_provider="openrouter",
            selected_model="provider/model",
            small_model=None,
        )

        error, model, small_model, label = resolve_provider_settings(config)

        self.assertIn("Unsupported API provider", error)
        self.assertIsNone(model)
        self.assertIsNone(small_model)
        self.assertEqual(label, "provider/model")

    def test_normalize_openai_compatible_url_strips_v1_scheme_and_slashes(self):
        cases = {
            "http://host:8000/v1": "http://host:8000",
            "http://host:8000/v1/": "http://host:8000",
            "host:8000/v1": "http://host:8000",
            "host:8000": "http://host:8000",
            "https://host:8443/v1": "https://host:8443",
            "http://host:8000/": "http://host:8000",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_openai_compatible_url(raw), expected)

        self.assertIsNone(normalize_openai_compatible_url(None))
        self.assertEqual(normalize_openai_compatible_url(""), "")
        self.assertEqual(normalize_openai_compatible_url("v1"), "http://v1")

    def test_get_openai_compatible_models_appends_v1_once(self):
        response = MagicMock()
        response.json.return_value = {"data": [{"id": "local-model"}]}

        with patch("cterm.config.utils.requests.get", return_value=response) as get:
            models = get_openai_compatible_models("host:8000/v1", "key")

        self.assertEqual(models, ["local-model"])
        get.assert_called_once_with(
            "http://host:8000/v1/models",
            headers={"Authorization": "Bearer key"},
            timeout=10.0,
        )


@unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
class OpenAiCompatibleUrlWizardTests(unittest.TestCase):
    def test_url_state_normalizes_scheme_and_v1(self):
        from cterm.ui.tui.config.config_tui import _state_openai_compatible_url

        config = _FakeUrlConfig(current="http://host:8000/v1")
        ui = _FakeUrlUI(["http://host:8000/v1", ""])

        next_state = _state_openai_compatible_url(config, ui, "ollama")

        self.assertEqual(next_state, "OPENAI_COMPATIBLE_CONNECT")
        self.assertEqual(config.values[Config.OPENAI_COMPATIBLE_SERVER_URL], "http://host:8000")

    def test_url_state_requires_a_url(self):
        from cterm.ui.tui.config.config_tui import _state_openai_compatible_url

        config = _FakeUrlConfig(current="")
        ui = _FakeUrlUI([""])

        next_state = _state_openai_compatible_url(config, ui, "ollama")

        self.assertEqual(next_state, "OPENAI_COMPATIBLE_URL")
        self.assertNotIn(Config.OPENAI_COMPATIBLE_SERVER_URL, config.values)

    def test_connect_state_validates_v1_models(self):
        from cterm.ui.tui.config.config_tui import _state_openai_compatible_connect

        config = _FakeUrlConfig(current="http://host:8000/v1")

        with patch("cterm.ui.tui.config.config_tui.get_openai_compatible_models", return_value=["m"]):
            self.assertEqual(_state_openai_compatible_connect(config, _FakeUrlUI([]), "ollama"), "OPENAI_COMPATIBLE_MODEL")

        with patch("cterm.ui.tui.config.config_tui.get_openai_compatible_models", return_value=[]):
            self.assertEqual(_state_openai_compatible_connect(config, _FakeUrlUI([]), "ollama"), "OPENAI_COMPATIBLE_URL")


class _FakeUrlConfig:
    def __init__(self, current):
        self.openai_compatible_server_url = current
        self.openai_compatible_api_key = "key"
        self.values = {}

    def set(self, key, value):
        self.values[key] = value

    def set_provider_value(self, provider, key, value):
        pass


class _FakeUrlUI:
    def __init__(self, answers):
        self.answers = list(answers)

    def input(self, title, default="", placeholder="", hint=""):
        return self.answers.pop(0)

    def log(self, text, style=""):
        pass


if __name__ == "__main__":
    unittest.main()
