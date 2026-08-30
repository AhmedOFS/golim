import json
import os
import tempfile
import threading
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import MagicMock, patch

from openterm.config import Config, ConfigSchemaError, get_config, init_config
from openterm.config.app_home import resolve_app_home
from openterm.config.utils import (
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
        resolve_app_home()

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def test_schema_separates_provider_values_and_attributes(self):
        config = Config()
        config.set_provider_value(Config.OPEN_ROUTER, Config.PROVIDER_API_KEY, "key")
        config.choose_model("provider/model", Config.OPEN_ROUTER)
        config.set(Config.SMALL_MODEL, "provider/small")
        config.set(Config.EXA_API_KEY, "exa-key")

        saved = json.loads(config.path.read_text())
        self.assertEqual(saved["providers"]["open_router"]["api_key"], "key")
        self.assertNotIn("current_model", saved["attributes"])
        self.assertNotIn("api_provider", saved["attributes"])
        self.assertEqual(saved["attributes"]["small_model"], "provider/small")
        self.assertEqual(saved["attributes"]["exa_api_key"], "exa-key")
        self.assertTrue(config.is_complete())

    def test_models_json_entry_drives_configuration_status(self):
        path = Path(self.tmp.name) / ".openterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "providers": {"ollama": {"ollama_host": "http://localhost:11434"}},
            "attributes": {},
        }))

        self.assertFalse(Config().is_complete())

        models_path = Path(self.tmp.name) / ".openterm" / "data" / "models.json"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(json.dumps([{"model": "model", "provider": "ollama"}]))

        self.assertTrue(Config().is_complete())

    def test_malformed_model_history_is_ignored(self):
        models_path = Path(self.tmp.name) / ".openterm" / "data" / "models.json"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text("null")

        config = Config()

        self.assertEqual(config.recent_models(), [])
        self.assertIsNone(config.selected_model)

    def test_legacy_flat_config_is_rejected(self):
        path = Path(self.tmp.name) / ".openterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "api_provider": "openrouter",
            "openrouter_api_key": "key",
            "openrouter_model": "provider/model",
        }))

        with self.assertRaises(ConfigSchemaError):
            Config()

    def test_schema_is_loaded_without_filling_missing_values(self):
        path = Path(self.tmp.name) / ".openterm" / "config" / "config.json"
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

    def test_proactive_auth_defaults_to_enabled_and_can_be_disabled(self):
        config = Config()
        self.assertTrue(config.proactive_auth)

        config.set(Config.PROACTIVE_AUTH, False)

        self.assertFalse(config.proactive_auth)
        saved = json.loads(config.path.read_text())
        self.assertFalse(saved[Config.ATTRIBUTES][Config.PROACTIVE_AUTH])

    def test_missing_proactive_auth_in_existing_config_defaults_to_enabled(self):
        path = Path(self.tmp.name) / ".openterm" / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"providers": {}, "attributes": {}}))

        self.assertTrue(Config().proactive_auth)

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

    def test_choose_model_sets_session_pair_and_latest_entry(self):
        config = Config()
        config.set_provider_value(Config.OPEN_ROUTER, Config.PROVIDER_API_KEY, "key")

        config.choose_model("first/model", Config.OLLAMA)
        config.choose_model("second/model", Config.OPEN_ROUTER)

        self.assertEqual(config.selected_model, "second/model")
        self.assertEqual(config.api_provider, Config.OPEN_ROUTER)
        self.assertEqual(config.latest_model(), {"model": "second/model", "provider": Config.OPEN_ROUTER})
        stored = json.loads(config.models_path.read_text())
        self.assertEqual(stored[0], {"model": "second/model", "provider": Config.OPEN_ROUTER})
        self.assertTrue(config.is_complete())

    def test_fresh_instance_resumes_most_recent_model(self):
        config = Config()
        config.set(Config.OLLAMA_SERVER_URL, "http://localhost:11434")
        config.choose_model("one/model", Config.OLLAMA)
        config.choose_model("two/model", Config.OPEN_ROUTER)

        resumed = Config()

        self.assertEqual(resumed.selected_model, "two/model")
        self.assertEqual(resumed.api_provider, Config.OPEN_ROUTER)

    def test_session_pair_survives_config_reload(self):
        config = Config()
        config.choose_model("session/model", Config.OLLAMA)

        config.reload()

        self.assertEqual(config.selected_model, "session/model")
        self.assertEqual(config.api_provider, Config.OLLAMA)

    def test_running_instances_keep_their_own_model_choice(self):
        first = Config()
        second = Config()
        first.choose_model("shared/first", Config.OLLAMA)
        # Both instances seed from the same models.json initially.
        second_reloaded = Config()
        self.assertEqual(second_reloaded.selected_model, "shared/first")

        # The second instance chooses a new model: models.json and its own
        # session update, while the first instance keeps its session pair.
        second.choose_model("shared/second", Config.OPEN_ROUTER)

        self.assertEqual(first.selected_model, "shared/first")
        self.assertEqual(first.api_provider, Config.OLLAMA)
        self.assertEqual(second.selected_model, "shared/second")
        self.assertEqual(second.api_provider, Config.OPEN_ROUTER)

    def test_worker_threads_share_the_session_bound_config(self):
        # Regression: per-request LLM threads must not construct a fresh
        # Config; it would re-seed the provider/model from models.json on
        # disk and dispatch another instance's latest choice mid-run.
        config = init_config()
        config.set(Config.OLLAMA_SERVER_URL, "http://localhost:11434")
        config.choose_model("mine/model", Config.OLLAMA)

        # Another running instance writes a newer entry to models.json.
        other = Config()
        other.choose_model("other/model", Config.OPEN_ROUTER)

        seen = {}

        def worker():
            cfg = get_config()
            seen["obj"] = cfg
            seen["model"] = cfg.selected_model
            seen["provider"] = cfg.api_provider

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        self.assertIs(seen["obj"], config)
        self.assertEqual(seen["model"], "mine/model")
        self.assertEqual(seen["provider"], Config.OLLAMA)

    def test_model_picker_keeps_configured_remote_model_when_catalogue_is_unavailable(self):
        config = Config()
        config.set_provider_value(Config.OPEN_ROUTER, Config.PROVIDER_API_KEY, "key")
        config.choose_model("provider/selected", Config.OPEN_ROUTER)

        with patch("openterm.config.utils.get_models", return_value=[]), \
             patch("openterm.config.utils.get_openrouter_models", return_value=[]), \
             patch("openterm.config.utils.get_openai_compatible_models", return_value=[]):
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

        with patch("openterm.config.utils.requests.get", return_value=response) as get:
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
        from openterm.ui.tui.config.config_tui import _state_openai_compatible_url

        config = _FakeUrlConfig(current="http://host:8000/v1")
        ui = _FakeUrlUI(["http://host:8000/v1", ""])

        next_state = _state_openai_compatible_url(config, ui, "ollama")

        self.assertEqual(next_state, "OPENAI_COMPATIBLE_CONNECT")
        self.assertEqual(config.values[Config.OPENAI_COMPATIBLE_SERVER_URL], "http://host:8000")

    def test_url_state_requires_a_url(self):
        from openterm.ui.tui.config.config_tui import _state_openai_compatible_url

        config = _FakeUrlConfig(current="")
        ui = _FakeUrlUI([""])

        next_state = _state_openai_compatible_url(config, ui, "ollama")

        self.assertEqual(next_state, "OPENAI_COMPATIBLE_URL")
        self.assertNotIn(Config.OPENAI_COMPATIBLE_SERVER_URL, config.values)

    def test_connect_state_validates_v1_models(self):
        from openterm.ui.tui.config.config_tui import _state_openai_compatible_connect

        config = _FakeUrlConfig(current="http://host:8000/v1")

        with patch("openterm.ui.tui.config.config_tui.get_openai_compatible_models", return_value=["m"]):
            self.assertEqual(_state_openai_compatible_connect(config, _FakeUrlUI([]), "ollama"), "OPENAI_COMPATIBLE_MODEL")

        with patch("openterm.ui.tui.config.config_tui.get_openai_compatible_models", return_value=[]):
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
