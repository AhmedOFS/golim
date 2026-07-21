import unittest
import asyncio
from importlib.util import find_spec
from unittest.mock import patch

from cterm import __main__ as main_module


class MainTuiTests(unittest.TestCase):
    def test_no_arguments_opens_tui(self):
        with patch.object(main_module, "setup_root_logger"), \
             patch.object(main_module, "tui_command", return_value=0) as tui:
            result = main_module.main([])

        self.assertEqual(result, 0)
        tui.assert_called_once_with("ollama", debug=False)

    def test_message_arguments_keep_plain_chat_mode(self):
        with patch.object(main_module, "setup_root_logger"), \
             patch.object(main_module, "chat_command", return_value=0) as chat, \
             patch.object(main_module, "tui_command") as tui:
            result = main_module.main(["hello", "there"])

        self.assertEqual(result, 0)
        chat.assert_called_once_with("hello there", "ollama", debug=False)
        tui.assert_not_called()

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_reused_tui_runtime_refreshes_ui(self):
        from cterm.ui.tui.tui import CtermApp

        first_ui = object()
        second_ui = object()
        app = CtermApp("model", model="main")

        runtime = app._get_runtime(first_ui)
        same_runtime = app._get_runtime(second_ui)

        self.assertIs(same_runtime, runtime)
        self.assertIs(runtime.ui, second_ui)

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_prompt_placeholder_changes_with_state(self):
        from cterm.ui.tui.tui import (
            CtermApp,
            DONE_PROMPT_PLACEHOLDER,
            INITIAL_PROMPT_PLACEHOLDER,
            RUNNING_PROMPT_PLACEHOLDER,
        )

        class FakePrompt:
            def __init__(self):
                self.disabled = True
                self.placeholder = ""
                self.focused = False

            def focus(self):
                self.focused = True

        prompt = FakePrompt()
        app = CtermApp("model", model="main")

        def fake_query_one(selector, *_args, **_kwargs):
            if selector == "#prompt":
                return prompt
            raise AssertionError(selector)

        app.query_one = fake_query_one

        app.update_prompt_placeholder()
        self.assertEqual(prompt.placeholder, INITIAL_PROMPT_PLACEHOLDER)

        app.set_busy(True)
        self.assertEqual(prompt.placeholder, RUNNING_PROMPT_PLACEHOLDER)

        app._has_completed_query = True
        app.set_busy(False)
        self.assertEqual(prompt.placeholder, DONE_PROMPT_PLACEHOLDER)
        self.assertFalse(prompt.disabled)
        self.assertTrue(prompt.focused)

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_config_reload_updates_model_and_discards_runtime(self):
        from cterm.ui.tui.tui import CtermApp

        class FakeStatic:
            def __init__(self):
                self.value = None

            def update(self, value):
                self.value = value

        class FakeRuntime:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        app = CtermApp("old", model="old-model", small_model="old-small")
        model_widget = FakeStatic()
        runtime = FakeRuntime()
        app._runtime = runtime

        def fake_query_one(selector, *_args, **_kwargs):
            if selector == "#model":
                return model_widget
            raise AssertionError(selector)

        app.query_one = fake_query_one

        with patch("cterm.ui.tui.tui.Config") as config_cls, \
             patch("cterm.ui.tui.tui.shutil.which", return_value="/usr/bin/ollama"):
            config = config_cls.return_value
            config.api_provider = "ollama"
            config.selected_model = "new-model"
            config.small_model = "new-small"

            app._reload_config_settings()

        self.assertEqual(app.model_label, "new-model")
        self.assertEqual(app._model, "new-model")
        self.assertEqual(app._small_model, "new-small")
        self.assertIsNone(app._runtime_error)
        self.assertEqual(model_widget.value, "new-model")
        self.assertTrue(runtime.terminated)
        self.assertIsNone(app._runtime)

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tab_opens_config_while_prompt_is_focused(self):
        from cterm.ui.tui.tui import CtermApp

        async def run_case():
            app = CtermApp("model", model="main")
            async with app.run_test() as pilot:
                self.assertEqual(app.focused.id, "prompt")
                await pilot.press("tab")
                await pilot.pause(0.2)
                self.assertTrue(app._config_active)
                self.assertFalse(app.query_one("#config_panel").has_class("hidden"))
                self.assertTrue(app.query_one("#frame").has_class("hidden"))
                self.assertEqual(type(app._request).__name__, "SelectRequest")
                self.assertEqual(app.focused.id, "config_option_list")
                app._cancel_config()
                await pilot.pause(0.2)

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
