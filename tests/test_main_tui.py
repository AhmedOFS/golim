import unittest
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


if __name__ == "__main__":
    unittest.main()
