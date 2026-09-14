import asyncio
import tempfile
import unittest
from importlib.util import find_spec
from unittest.mock import patch

from golim.config import Config, init_config
from golim.core.agent import ToolAgent


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

            with patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
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

            with patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
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

            with patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
                result = agent._chat_with_optional_thinking("model", [])

        self.assertEqual(result["message"]["content"], "ok")
        self.assertEqual(ui.deltas, [])
        self.assertEqual(ui.completed, ["returned by the provider"])

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_thinking_renderable_starts_with_icon_and_label(self):
        from golim.ui.tui.app.widgets.transcript import Transcript

        transcript = Transcript()

        collapsed = transcript._thinking_renderable("first second", False, 80).plain
        expanded = transcript._thinking_renderable("first\nsecond", True, 80).plain

        self.assertEqual(collapsed, "▶ THINKING: first second")
        self.assertEqual(expanded, "▼ THINKING: first\nsecond")

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_live_thinking_updates_then_collapses_same_entry(self):
        from golim.ui.tui.app.widgets.transcript import Transcript

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

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_queries_share_transcript_and_keep_content_gutter(self):
        from rich.text import Text

        from golim.ui.tui.app.widgets.transcript import Transcript

        transcript = Transcript()
        transcript.write_query("first task")
        transcript.write(Text("response"))
        transcript.write_query("followup task")

        self.assertEqual(
            [strip.text for strip in transcript._lines],
            ["> first task", "", "  response", "", "> followup task", ""],
        )

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_initial_query_is_sticky_without_transcript_duplicate(self):
        from rich.text import Text

        from golim.ui.tui.app.widgets.transcript import Transcript

        headers = []
        transcript = Transcript()
        transcript.set_query_header_callback(headers.append)
        transcript.set_initial_query("first task")
        transcript.write(Text("response"))
        transcript.write_query("followup task")

        self.assertEqual(headers[-1], "first task")
        self.assertEqual(
            [strip.text for strip in transcript._lines],
            ["  response", "", "> followup task", ""],
        )

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_stream_replacement_keeps_single_indented_pending_line(self):
        from rich.text import Text

        from golim.ui.tui.app.widgets.transcript import Transcript

        transcript = Transcript()
        transcript.write(Text("progress 1"), replace_last=True, commit=False)
        transcript.write(Text("progress 2"), replace_last=True, commit=False)

        self.assertEqual(transcript._lines, [])
        self.assertEqual([strip.text for strip in transcript._pending_strips], ["  progress 2"])

        transcript.write(Text("finished"))
        self.assertEqual(
            [strip.text for strip in transcript._lines],
            ["  progress 2", "  finished"],
        )

    @unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
    def test_tui_sticky_query_tracks_last_query_at_scroll_top(self):
        from rich.text import Text
        from textual.app import App, ComposeResult

        from golim.ui.tui.app.widgets.transcript import Transcript

        headers = []

        class TestApp(App[None]):
            def compose(self) -> ComposeResult:
                yield Transcript(id="transcript")

        async def run_case():
            app = TestApp()
            async with app.run_test(size=(40, 8)) as pilot:
                transcript = app.query_one(Transcript)
                transcript.set_query_header_callback(headers.append)
                transcript.write_query("first task")
                for index in range(8):
                    transcript.write(Text(f"first output {index}"))
                transcript.write_query("followup task")
                for index in range(20):
                    transcript.write(Text(f"followup output {index}"))
                await pilot.pause()

                transcript.scroll_to(y=13, animate=False)
                await pilot.pause()
                self.assertEqual(headers[-1], "first task")

                transcript.scroll_to(y=14, animate=False)
                await pilot.pause()
                self.assertEqual(headers[-1], "followup task")

                transcript.scroll_end(animate=False)
                await pilot.pause()
                self.assertEqual(headers[-1], "followup task")

                transcript.scroll_home(animate=False)
                await pilot.pause()
                self.assertEqual(headers[-1], "first task")

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
