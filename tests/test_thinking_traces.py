import tempfile
import unittest
from importlib.util import find_spec
from unittest.mock import patch

from openterm.config import Config, init_config
from openterm.core.agent import ToolAgent


class FakeUI:
    def __init__(self):
        self.deltas = []
        self.completed = []

    def thinking_delta(self, text):
        self.deltas.append(text)

    def thinking_complete(self, text):
        self.completed.append(text)


class ThinkingTraceTests(unittest.TestCase):
    def test_agent_streams_thinking_when_config_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"HOME": tmp}):
            init_config().set(Config.STREAM_THINKING_TRACES, True)
            ui = FakeUI()
            agent = ToolAgent("model", ui=ui)

            def fake_chat(*args, on_thinking_delta=None, **kwargs):
                on_thinking_delta("one ")
                on_thinking_delta("two")
                return {"message": {"role": "assistant", "content": "ok"}}

            with patch("openterm.core.agent.chat_with_model_api", side_effect=fake_chat):
                result = agent._chat_with_optional_thinking("model", [])

        self.assertEqual(result["message"]["content"], "ok")
        self.assertEqual(ui.deltas, ["one ", "two"])
        self.assertEqual(ui.completed, ["one two"])

    def test_agent_does_not_stream_thinking_when_config_disabled(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"HOME": tmp}):
            init_config().set(Config.STREAM_THINKING_TRACES, False)
            ui = FakeUI()
            agent = ToolAgent("model", ui=ui)

            def fake_chat(*args, **kwargs):
                self.assertNotIn("on_thinking_delta", kwargs)
                return {"message": {"role": "assistant", "content": "ok"}}

            with patch("openterm.core.agent.chat_with_model_api", side_effect=fake_chat):
                result = agent._chat_with_optional_thinking("model", [])

        self.assertEqual(result["message"]["content"], "ok")
        self.assertEqual(ui.deltas, [])
        self.assertEqual(ui.completed, [])

    def test_agent_surfaces_thinking_returned_in_non_streaming_response(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"HOME": tmp}):
            init_config().set(Config.STREAM_THINKING_TRACES, True)
            ui = FakeUI()
            agent = ToolAgent("model", ui=ui)

            def fake_chat(*args, on_thinking_delta=None, **kwargs):
                return {
                    "message": {
                        "role": "assistant",
                        "thinking": "returned by the provider",
                        "content": "ok",
                    }
                }

            with patch("openterm.core.agent.chat_with_model_api", side_effect=fake_chat):
                result = agent._chat_with_optional_thinking("model", [])

        self.assertEqual(result["message"]["content"], "ok")
        self.assertEqual(ui.deltas, [])
        self.assertEqual(ui.completed, ["returned by the provider"])

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_thinking_renderable_starts_with_icon_and_label(self):
        from openterm.ui.tui.app.widgets.transcript import Transcript

        transcript = Transcript()

        collapsed = transcript._thinking_renderable("first second", False, 80).plain
        expanded = transcript._thinking_renderable("first\nsecond", True, 80).plain

        self.assertEqual(collapsed, "▶ THINKING: first second")
        self.assertEqual(expanded, "▼ THINKING: first\nsecond")

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_live_thinking_updates_then_collapses_same_entry(self):
        from openterm.ui.tui.app.widgets.transcript import Transcript

        transcript = Transcript()
        transcript.append_thinking_delta("first")
        self.assertEqual(len(transcript._renderable_log), 1)
        self.assertEqual(transcript._renderable_log[0][0].plain, "THINKING: first")

        transcript.append_thinking_delta("first second")
        self.assertEqual(len(transcript._renderable_log), 1)
        live_id = transcript._renderable_log[0][2]
        self.assertTrue(transcript._thinking_entries[live_id]["live"])

        transcript.append_thinking_trace("first second")
        self.assertEqual(len(transcript._renderable_log), 1)
        self.assertEqual(transcript._renderable_log[0][0].plain, "▶ THINKING: first second")
        self.assertFalse(transcript._thinking_entries[live_id]["live"])


if __name__ == "__main__":
    unittest.main()
