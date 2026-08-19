import unittest
from importlib.util import find_spec


@unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
class TuiErrorRenderingTests(unittest.TestCase):
    def test_error_result_uses_error_text_not_markdown(self):
        from openterm.ui.tui.app.app_tui import OpentermApp
        from openterm.ui.tui.tui_style import STYLE_ERROR

        app = OpentermApp("model", model="main")
        written = []

        class Transcript:
            def write(self, renderable):
                written.append(renderable)

        app.query_one = lambda *_args: Transcript()
        app.append_markdown("Error: provider unavailable", ok=False)

        self.assertEqual(written[0].plain, "Error: provider unavailable")
        self.assertEqual(written[0].style, STYLE_ERROR)


if __name__ == "__main__":
    unittest.main()
