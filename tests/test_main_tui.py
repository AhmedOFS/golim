import unittest
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


if __name__ == "__main__":
    unittest.main()
