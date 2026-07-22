import unittest
import asyncio
from importlib.util import find_spec
from unittest.mock import Mock, patch

import requests

from cterm.config import Config
from cterm.config.utils import get_openrouter_models


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

        with patch("cterm.config.utils.requests.get", return_value=response) as get:
            models = get_openrouter_models("test-key")

        self.assertEqual(models, ["openai/gpt-4o", "anthropic/claude-sonnet"])
        get.assert_called_once_with(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": "Bearer test-key"},
            timeout=10.0,
        )
        response.raise_for_status.assert_called_once_with()

    def test_get_openrouter_models_returns_empty_list_on_request_failure(self):
        with patch("cterm.config.utils.requests.get", side_effect=requests.ConnectionError("offline")):
            self.assertEqual(get_openrouter_models("test-key"), [])

@unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
class OpenRouterModelConfigTests(unittest.TestCase):
    def test_openrouter_model_uses_search_picker(self):
        from cterm.ui.tui.config import config_tui
        config = _FakeConfig(selected_model="saved/model")
        ui = _FakeUI("provider/selected")

        with patch.object(config_tui, "get_openrouter_models", return_value=["provider/first", "saved/model"]):
            next_state = config_tui._state_openrouter_model(config, ui, "ollama")

        self.assertEqual(next_state, "OPENROUTER_SMALL_MODEL")
        self.assertEqual(ui.search_calls, [("Select model", ["provider/first", "saved/model"], "saved/model")])
        self.assertEqual(config.values[Config.SELECTED_MODEL], "provider/selected")

    def test_openrouter_small_model_defaults_to_normal_model(self):
        from cterm.ui.tui.config import config_tui
        config = _FakeConfig(selected_model="provider/normal")
        ui = _FakeUI("provider/small")

        with patch.object(config_tui, "get_openrouter_models", return_value=["provider/first", "provider/normal"]):
            next_state = config_tui._state_openrouter_small_model(config, ui, "ollama")

        self.assertEqual(next_state, "COMMON_BASH")
        self.assertEqual(ui.search_calls, [("Select small model", ["provider/first", "provider/normal"], "provider/normal")])
        self.assertEqual(config.values[Config.SMALL_MODEL], "provider/small")

    def test_search_picker_starts_in_input_and_down_selects_first_model(self):
        from cterm.ui.tui.config.config_tui import ConfigApp
        from cterm.ui.tui.config.mixin import ModelSearchRequest

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


class _FakeConfig:
    def __init__(self, selected_model=None):
        self.selected_model = selected_model
        self.openrouter_api_key = "test-key"
        self.values = {}

    def set(self, key, value):
        self.values[key] = value


class _FakeUI:
    def __init__(self, answer):
        self.answer = answer
        self.search_calls = []

    def search_models(self, title, models, default_model):
        self.search_calls.append((title, models, default_model))
        return self.answer

    def log(self, *_args):
        pass


if __name__ == "__main__":
    unittest.main()
