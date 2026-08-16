import unittest
import asyncio
from importlib.util import find_spec
from unittest.mock import MagicMock, patch

from cterm import __main__ as main_module


class MainTuiTests(unittest.TestCase):
    def test_no_arguments_opens_tui(self):
        with patch.object(main_module, "setup_root_logger"), \
             patch.object(main_module, "tui_command", return_value=0) as tui:
            result = main_module.main([])

        self.assertEqual(result, 0)
        tui.assert_called_once_with("ollama")

    def test_message_arguments_keep_plain_chat_mode(self):
        with patch.object(main_module, "setup_root_logger"), \
             patch.object(main_module, "chat_command", return_value=0) as chat, \
             patch.object(main_module, "tui_command") as tui:
            result = main_module.main(["hello", "there"])

        self.assertEqual(result, 0)
        chat.assert_called_once_with("hello there", "ollama")
        tui.assert_not_called()

    def test_broken_config_prints_diagnostic_instead_of_crashing(self):
        from cterm.config import ConfigSchemaError
        from cterm.config.config import Config

        error = ConfigSchemaError("Invalid cterm configuration at /bad/path")
        with patch.object(main_module, "setup_root_logger"), \
             patch.object(Config, "_load", side_effect=error), \
             patch("sys.stderr") as stderr:
            for argv in ([], ["hello"], ["-i"]):
                result = main_module.main(argv)
                self.assertEqual(result, 1)

        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("Invalid cterm configuration", output)
        self.assertIn("cterm -i", output)

    def test_config_schema_error_reports_json_line_and_column(self):
        import json as _json

        from cterm.config import ConfigSchemaError
        from cterm.config.config import Config

        try:
            _json.loads('{\n    "providers": ')
        except _json.JSONDecodeError as exc:
            json_error = exc
            schema_error = ConfigSchemaError("Invalid cterm configuration at /bad/path")
            schema_error.__cause__ = json_error

        with patch.object(main_module, "setup_root_logger"), \
             patch.object(Config, "_load", side_effect=schema_error), \
             patch("sys.stderr") as stderr:
            result = main_module.main(["hello"])

        self.assertEqual(result, 1)
        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn(f"line {json_error.lineno}, column {json_error.colno}", output)

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_reused_tui_runtime_does_not_store_ui(self):
        from cterm.ui.tui.app.app_tui import CtermApp

        app = CtermApp("model", model="main")

        runtime = app._get_runtime()
        same_runtime = app._get_runtime()

        self.assertIs(same_runtime, runtime)
        self.assertFalse(hasattr(runtime, "ui"))

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_prompt_placeholder_changes_with_state(self):
        from cterm.ui.tui.app.app_tui import CtermApp
        from cterm.ui.tui.app.widgets.prompt_line import (
            DONE_PROMPT_PLACEHOLDER,
            INITIAL_PROMPT_PLACEHOLDER,
            RUNNING_PROMPT_PLACEHOLDER,
            PromptLine,
        )

        class FakePrompt:
            def __init__(self):
                self.disabled = True
                self.placeholder = ""
                self.focused = False

            def focus(self):
                self.focused = True

        class FakePromptLine:
            def __init__(self):
                self.prompt = FakePrompt()

            def update_placeholder(self, busy, has_completed):
                if busy:
                    self.prompt.placeholder = RUNNING_PROMPT_PLACEHOLDER
                elif has_completed:
                    self.prompt.placeholder = DONE_PROMPT_PLACEHOLDER
                else:
                    self.prompt.placeholder = INITIAL_PROMPT_PLACEHOLDER

            def set_busy(self, busy):
                if not busy:
                    self.prompt.disabled = False
                    self.prompt.focused = True

            def focus_input(self):
                self.prompt.focus()

        fake_prompt_line = FakePromptLine()
        app = CtermApp("model", model="main")

        def fake_query_one(selector, *_args, **_kwargs):
            if selector == PromptLine:
                return fake_prompt_line
            if selector == "#prompt":
                return fake_prompt_line.prompt
            raise AssertionError(str(selector))

        app.query_one = fake_query_one

        app.update_prompt_placeholder()
        self.assertEqual(fake_prompt_line.prompt.placeholder, INITIAL_PROMPT_PLACEHOLDER)

        app.set_busy(True)
        self.assertEqual(fake_prompt_line.prompt.placeholder, RUNNING_PROMPT_PLACEHOLDER)

        app._has_completed_query = True
        app.set_busy(False)
        self.assertEqual(fake_prompt_line.prompt.placeholder, DONE_PROMPT_PLACEHOLDER)
        self.assertFalse(fake_prompt_line.prompt.disabled)
        self.assertTrue(fake_prompt_line.prompt.focused)

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_config_reload_updates_model_and_discards_runtime(self):
        from cterm.ui.tui.app.app_tui import CtermApp
        from cterm.ui.tui.app.widgets.footer import Footer

        class FakeFooter:
            def __init__(self):
                self.model_label = ""

            def set_model(self, text):
                self.model_label = text

        class FakeRuntime:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        config = MagicMock()
        config.api_provider = "ollama"
        config.selected_model = "new-model"
        config.small_model = "new-small"
        app = CtermApp("old", config=config, model="old-model", small_model="old-small")
        fake_footer = FakeFooter()
        runtime = FakeRuntime()
        app._runtime = runtime

        def fake_query_one(selector, *_args, **_kwargs):
            if selector == "#footer" or selector == Footer:
                return fake_footer
            raise AssertionError(str(selector))

        app.query_one = fake_query_one

        with patch("cterm.config.utils.is_ollama_installed", return_value=True):
            app._reload_config_settings()

        self.assertEqual(app.model_label, "new-model")
        self.assertEqual(app._model, "new-model")
        self.assertEqual(app._small_model, "new-small")
        self.assertIsNone(app._runtime_error)
        self.assertEqual(fake_footer.model_label, "new-model")
        self.assertTrue(runtime.terminated)
        self.assertIsNone(app._runtime)
        config.reload.assert_called_once_with()

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tab_opens_main_menu_while_prompt_is_focused(self):
        from cterm.ui.tui.app.app_tui import CtermApp

        async def run_case():
            config = MagicMock()
            config.is_complete.return_value = True
            app = CtermApp("model", model="main", config=config)
            async with app.run_test() as pilot:
                self.assertEqual(app.focused.id, "prompt")
                await pilot.press("tab")
                await pilot.pause(0.2)
                self.assertTrue(app._menu_active)
                self.assertFalse(app.query_one("#menu_panel").has_class("hidden"))
                self.assertTrue(app.query_one("#frame").has_class("hidden"))
                self.assertEqual(app.focused.id, "menu_options")
                app._hide_menu()
                await pilot.pause(0.2)

        asyncio.run(run_case())

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_model_menu_arrow_keys_move_between_search_and_results(self):
        from cterm.ui.tui.app.app_tui import CtermApp

        async def run_case():
            config = MagicMock()
            config.is_complete.return_value = True
            app = CtermApp("model", model="main", config=config)
            async with app.run_test() as pilot:
                with patch("cterm.ui.tui.app.app_tui.get_configured_model_choices", return_value=(
                    ["provider/one", "provider/two"],
                    {"provider/one": ("provider", "one"), "provider/two": ("provider", "two")},
                )):
                    app._show_menu()
                    app._show_model_menu()
                    await pilot.pause()
                    self.assertEqual(app.focused.id, "menu_input")
                    await pilot.press("down")
                    self.assertEqual(app.focused.id, "menu_options")
                    await pilot.press("up")
                    self.assertEqual(app.focused.id, "menu_input")

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
