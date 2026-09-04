import unittest
import asyncio
from importlib.util import find_spec
from unittest.mock import Mock, patch

import requests

from openterm.config import Config
from openterm.config.utils import get_openrouter_models, validate_openrouter_key


class OpenRouterModelApiTests(unittest.TestCase):
    def test_get_openrouter_models_uses_api_and_keeps_provider_order(self):
        response = Mock()
        response.json.return_value = {
            "data": [
                {"id": "openai/gpt-4o"},
                {"id": "anthropic/claude-sonnet"},
                {"id": "openai/gpt-4o"},
                {"name": "missing-id"},
            ]
        }

        with patch("openterm.config.utils.requests.get", return_value=response) as get:
            models = get_openrouter_models("test-key")

        self.assertEqual(models, ["openai/gpt-4o", "anthropic/claude-sonnet"])
        get.assert_called_once_with(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": "Bearer test-key"},
            timeout=10.0,
        )
        response.raise_for_status.assert_called_once_with()

    def test_get_openrouter_models_returns_empty_list_on_request_failure(self):
        with patch("openterm.config.utils.requests.get", side_effect=requests.ConnectionError("offline")):
            self.assertEqual(get_openrouter_models("test-key"), [])

    def test_validate_openrouter_key_checks_current_key_endpoint(self):
        response = Mock()
        response.json.return_value = {"data": {"label": "My Key", "usage": 12.34, "limit": 50}}

        with patch("openterm.config.utils.requests.get", return_value=response) as get:
            self.assertTrue(validate_openrouter_key("test-key"))

        get.assert_called_once_with(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": "Bearer test-key"},
            timeout=10.0,
        )
        response.raise_for_status.assert_called_once_with()

    def test_validate_openrouter_key_rejects_missing_or_invalid_keys(self):
        self.assertFalse(validate_openrouter_key(None))
        self.assertFalse(validate_openrouter_key(""))

        with patch("openterm.config.utils.requests.get", side_effect=requests.HTTPError("401 Unauthorized")):
            self.assertFalse(validate_openrouter_key("bad-key"))

    def test_validate_openrouter_key_rejects_malformed_response(self):
        response = Mock()
        response.json.side_effect = ValueError("not json")

        with patch("openterm.config.utils.requests.get", return_value=response):
            self.assertFalse(validate_openrouter_key("test-key"))

@unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
class OpenRouterModelConfigTests(unittest.TestCase):
    def test_openrouter_model_uses_search_picker(self):
        from openterm.ui.tui.config import config_tui
        config = _FakeConfig(selected_model="saved/model")
        ui = _FakeUI("provider/selected")

        with patch.object(config_tui, "get_openrouter_models", return_value=["provider/first", "saved/model"]):
            next_state = config_tui._state_openrouter_model(config, ui, "ollama")

        self.assertEqual(next_state, "COMMON_BASH")
        self.assertEqual(ui.search_calls, [("Select model", ["provider/first", "saved/model"], "saved/model")])
        self.assertEqual(config.chosen, [("provider/selected", Config.OPEN_ROUTER)])

    def test_openrouter_key_input_saves_key_then_validates(self):
        from openterm.ui.tui.config import config_tui
        config = _FakeConfig()
        ui = _FakeUI("sk-or-fresh-key")

        next_state = config_tui._state_openrouter_key_input(config, ui, "ollama")

        self.assertEqual(next_state, "OPENROUTER_CONNECT")
        self.assertEqual(config.values[(Config.OPEN_ROUTER, Config.PROVIDER_API_KEY)], "sk-or-fresh-key")

    def test_openrouter_key_choice_validates_kept_key(self):
        from openterm.ui.tui.config import config_tui
        config = _FakeConfig()

        ui = _FakeUI("", select_answer=0)
        next_state = config_tui._state_openrouter_key_choice(config, ui, "ollama")
        self.assertEqual(next_state, "OPENROUTER_CONNECT")

        ui = _FakeUI("", select_answer=1)
        next_state = config_tui._state_openrouter_key_choice(config, ui, "ollama")
        self.assertEqual(next_state, "OPENROUTER_KEY_INPUT")

    def test_openrouter_connect_accepts_valid_key(self):
        from openterm.ui.tui.config import config_tui
        config = _FakeConfig()
        ui = _FakeUI("")

        with patch.object(config_tui, "validate_openrouter_key", return_value=True):
            next_state = config_tui._state_openrouter_connect(config, ui, "ollama")

        self.assertEqual(next_state, "OPENROUTER_MODEL")

    def test_openrouter_connect_rejects_invalid_key(self):
        from openterm.ui.tui.config import config_tui
        config = _FakeConfig()
        ui = _FakeUI("")

        with patch.object(config_tui, "validate_openrouter_key", return_value=False):
            next_state = config_tui._state_openrouter_connect(config, ui, "ollama")

        self.assertEqual(next_state, "OPENROUTER_KEY_INPUT")

    def test_search_picker_starts_in_input_and_down_selects_first_model(self):
        from openterm.ui.tui.config.config_tui import ConfigApp
        from openterm.ui.tui.config.mixin import ModelSearchRequest

        async def run_case():
            with patch.object(ConfigApp, "run_wizard"):
                app = ConfigApp()
                async with app.run_test() as pilot:
                    request = ModelSearchRequest("Select model", ["first/model", "second/model"])
                    app._show_model_search(request)
                    await pilot.pause()
                    self.assertEqual(app.focused.id, "text_input")

                    await pilot.press("down")
                    self.assertEqual(app.focused.id, "option_list")
                    await pilot.press("enter")
                    self.assertEqual(request.answer, "first/model")

        asyncio.run(run_case())


class OpenRouterBasicConfigValidationTests(unittest.TestCase):
    def test_init_openrouter_rejects_bad_key_then_accepts_valid_key(self):
        from openterm.ui.basic import basic_config

        class FakeConfig:
            def __init__(self):
                self.openrouter_api_key = None
                self.selected_model = None
                self.values = {}
                self.chosen = []

            def set_provider_value(self, provider, key, value):
                self.values[(provider, key)] = value

            def set(self, key, value):
                self.values[key] = value

            def unset(self, key):
                self.values[key] = None

            def choose_model(self, model, provider):
                self.chosen.append((model, provider))

        config = FakeConfig()
        answers = iter(["bad-key", "sk-or-good-key", "openai/gpt-4o", ""])
        with patch.object(basic_config, "validate_openrouter_key", side_effect=[False, True]) as fetch, \
             patch("builtins.input", side_effect=lambda *a: next(answers)):
            code = basic_config.init_openrouter(config)

        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(config.values[(Config.OPEN_ROUTER, Config.PROVIDER_API_KEY)], "sk-or-good-key")
        self.assertEqual(config.chosen, [("openai/gpt-4o", Config.OPEN_ROUTER)])


class _FakeConfig:
    def __init__(self, selected_model=None):
        self.selected_model = selected_model
        self.openrouter_api_key = "test-key"
        self.values = {}
        self.chosen = []

    def set(self, key, value):
        self.values[key] = value

    def set_provider_value(self, provider, key, value):
        self.values[(provider, key)] = value

    def choose_model(self, model, provider):
        self.chosen.append((model, provider))
        self.selected_model = model


class _FakeUI:
    def __init__(self, answer, select_answer=0):
        self.answer = answer
        self.select_answer = select_answer
        self.search_calls = []
        self.select_calls = []
        self.input_calls = []

    def search_models(self, title, models, default_model):
        self.search_calls.append((title, models, default_model))
        return self.answer

    def select(self, title, options, default_index, hint=""):
        self.select_calls.append((title, options, default_index))
        return self.select_answer

    def input(self, title, default="", placeholder="", hint=""):
        self.input_calls.append(title)
        return self.answer

    def log(self, *_args):
        pass


if __name__ == "__main__":
    unittest.main()
