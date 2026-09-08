import asyncio
import unittest
from importlib.util import find_spec


@unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
class PromptLineTests(unittest.TestCase):
    def test_paste_keeps_all_lines_and_replaces_selection(self):
        from textual import events
        from textual.app import App, ComposeResult

        from golim.ui.tui.app.widgets.prompt_line import PromptInput, PromptLine

        class TestApp(App[None]):
            def compose(self) -> ComposeResult:
                yield PromptLine()

        async def run_case():
            app = TestApp()
            async with app.run_test() as pilot:
                prompt = app.query_one(PromptInput)
                prompt.value = "ask this"
                prompt.selection = (4, 8)

                prompt._on_paste(events.Paste("that\nwith two lines"))

                self.assertEqual(prompt.value, "ask that\nwith two lines")

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
