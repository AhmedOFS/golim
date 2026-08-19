import threading
import logging
import unittest
from io import StringIO
from importlib.util import find_spec

from openterm.ui.tui.app.transcript_writer import TranscriptWriter


def _plain(renderable):
    if hasattr(renderable, "plain"):
        return renderable.plain
    if hasattr(renderable, "renderables"):
        return "\n".join(_plain(item) for item in renderable.renderables)
    return str(renderable)


@unittest.skipIf(find_spec("textual") is None, "Textual is not installed")
class TextualToolOutputTests(unittest.TestCase):
    def make_ui(self, transcript=None):
        from openterm.ui.tui.app.agent_events_handler import TUIAgentEventsHandler

        class FakeApp:
            def __init__(self):
                self.lines = []
                self.expandables = []
                self.codes = []
                self.streams = []
                self.thinking_traces = []
                self.discarded_pending = False

            def is_run_active(self, run_id):
                return True

            def call_from_thread(self, func, *args):
                return func(*args)

            def append_line(self, text, style):
                self.lines.append((text, style))

            def append_expandable_result(self, summary, detail):
                self.expandables.append((summary, detail))

            def append_code(self, title, code, language="python"):
                self.codes.append((title, code, language))

            def append_stream(self, renderable, replace_last, commit):
                self.streams.append((renderable, replace_last, commit))

            def append_thinking_trace(self, text):
                self.thinking_traces.append(text)

            def discard_pending_stream(self):
                self.discarded_pending = True

            def set_status(self, text):
                pass

        app = FakeApp()
        ui = TUIAgentEventsHandler(app, 1, threading.Event(), transcript=transcript)
        return ui, app

    def test_system_info_done_expands_to_formatted_data(self):
        ui, app = self.make_ui()
        ui.tool_call("system_info", {})
        ui.tool_output(result={
            "ok": True,
            "os": {"system": "Linux", "release": "6.1"},
            "cwd": "/home/ahmed/Openterm",
        })

        summary, detail = app.expandables[-1]
        self.assertEqual(_plain(summary), "✓ Done")
        self.assertIn("os:", _plain(detail))
        self.assertIn("system: Linux", _plain(detail))
        self.assertIn("cwd: /home/ahmed/Openterm", _plain(detail))

    def test_finder_result_expands_to_match_list(self):
        ui, app = self.make_ui()
        ui.tool_call("finder", {"path": "/tmp", "pattern": "*.py"})
        ui.tool_output(result={
            "ok": True,
            "total": 2,
            "matches": ["/tmp/a.py", "/tmp/b.py"],
        })

        summary, detail = app.expandables[-1]
        self.assertEqual(_plain(summary), "✓ 2 matches")
        self.assertIn("/tmp/a.py", _plain(detail))
        self.assertIn("/tmp/b.py", _plain(detail))

    def test_bash_truncated_nested_results_expand_to_twenty_lines(self):
        ui, app = self.make_ui()
        ui.tool_call("bash", {"command": "seq 1 60"})
        ui.tool_output(result={
            "ok": True,
            "output_truncated": True,
            "results": [{"stdout": "\n".join(str(i) for i in range(1, 51)) + "\n"}],
        })

        summary, detail = app.expandables[-1]
        self.assertEqual(_plain(summary).splitlines(), ["…", "49", "50"])
        detail_lines = _plain(detail).splitlines()
        self.assertEqual(detail_lines[:9], [str(i) for i in range(1, 10)])
        self.assertEqual(detail_lines[9], "…")
        self.assertEqual(detail_lines[10:], [str(i) for i in range(41, 51)])
        self.assertEqual(len(detail_lines), 20)
        self.assertTrue(app.discarded_pending)

    def test_streamed_bash_completion_lines_remain_visible_after_done(self):
        ui, app = self.make_ui()
        ui.tool_call("bash", {"command": "sudo snap install example"})
        ui.tool_output(fd="stdout", line="progress 10%", end="\n")
        ui.tool_output(result={
            "ok": True,
            "results": [{
                "stdout": "\n".join([
                    "progress 10%",
                    "progress 50%",
                    "progress 90%",
                    "example 1.0 installed",
                ]),
                "stderr": "",
                "returncode": 0,
            }],
        })

        summary, _ = app.expandables[-1]
        self.assertEqual(_plain(summary).splitlines(), ["…", "progress 90%", "example 1.0 installed"])
        self.assertEqual(app.lines, [("$ sudo snap install example", "bold #f3f3f3"), ("", "#f3f3f3")])
        self.assertTrue(app.discarded_pending)

    def test_final_streamed_bash_result_does_not_reemit_terminal_controls(self):
        ui, app = self.make_ui()
        ui.tool_call("bash", {"command": "sudo snap remove example"})
        ui.tool_output(fd="stdout", line="progress", end="\n")
        ui.tool_output(result={
            "ok": True,
            "results": [{
                "stdout": (
                    "first line\nsecond line\n"
                    "\033[0m\033[?25h\033[Kexample removed\n"
                ),
                "stderr": "",
                "returncode": 0,
            }],
        })

        summary, detail = app.expandables[-1]
        self.assertNotIn("\033", _plain(summary))
        self.assertNotIn("\033", _plain(detail))
        self.assertIn("example removed", _plain(summary))

    def test_exec_tool_displays_code_with_running_script_title(self):
        ui, app = self.make_ui()
        ui.tool_call("exec", {"code": "print('hello')\nprint('world')"})

        self.assertEqual(app.codes, [("» running script", "print('hello')\nprint('world')", "python")])

    def test_read_file_call_shows_target_path(self):
        ui, app = self.make_ui()
        ui.tool_call("read_file", {"path": "/home/ahmed/notes.md"})
        ui.tool_output(result={
            "ok": True,
            "path": "/home/ahmed/notes.md",
            "page": 1,
            "total_pages": 2,
            "content": "hello",
        })

        self.assertTrue(any(
            "/home/ahmed/notes.md" in text for text, _ in app.lines
        ))

    def test_write_file_call_displays_path_and_code(self):
        ui, app = self.make_ui()
        ui.tool_call("write_file", {
            "path": "/home/ahmed/app.py",
            "content": "print('hi')\n",
        })
        ui.tool_output(result={
            "ok": True,
            "path": "/home/ahmed/app.py",
            "bytes_written": 12,
        })

        self.assertEqual(app.codes, [
            ("✎ writing: /home/ahmed/app.py", "print('hi')\n", "python")
        ])
        self.assertTrue(any(
            "/home/ahmed/app.py" in text for text, _ in app.lines
        ))

    def test_tui_transcript_records_visible_progress(self):
        transcript_file = StringIO()
        tw = TranscriptWriter(transcript_file)
        ui, _ = self.make_ui(transcript=tw)

        ui.message("Available Skills: Filesystem_Operations")
        ui.message("\033[32m✓\033[0m Filesystem_Operations")
        ui.tool_call("bash", {"command": "seq 1 4"})
        ui.tool_output(fd="stdout", line="live output", end="\n")
        ui.tool_output(result={
            "ok": True,
            "results": [{"stdout": "1\n2\n3\n4", "stderr": ""}],
        })
        ui.thinking_complete("inspect the current system")

        text = transcript_file.getvalue()
        self.assertIn("Available Skills: Filesystem_Operations", text)
        self.assertIn("✓ Filesystem_Operations", text)
        self.assertIn("$ seq 1 4", text)
        self.assertIn("[bash stdout] live output", text)
        self.assertIn("…\n3\n4", text)
        self.assertIn("▶ THINKING: inspect the current system", text)

    def test_tui_log_records_full_tool_output_and_thinking_trace(self):
        ui, _ = self.make_ui()
        log = StringIO()
        from openterm.logger import DIAGNOSTIC_LOGGER

        handler = logging.StreamHandler(log)
        handler.setFormatter(logging.Formatter("%(message)s"))
        DIAGNOSTIC_LOGGER.addHandler(handler)
        DIAGNOSTIC_LOGGER.setLevel(logging.DEBUG)

        try:
            from openterm.logger import log_diagnostic_section

            log_diagnostic_section("tool_call bash", {"command": "seq 1 3"})
            log_diagnostic_section("tool_result bash", {
                "ok": True,
                "results": [{
                    "command": "seq 1 3",
                    "stdout": "1\n2\n3",
                    "stderr": "",
                    "returncode": 0,
                }],
            })
            log_diagnostic_section("tool_output bash stdout", "full streamed line\n")
            log_diagnostic_section("thinking_trace", "first line\nsecond line")
        finally:
            DIAGNOSTIC_LOGGER.removeHandler(handler)

        text = log.getvalue()
        self.assertIn("## tool_call bash", text)
        self.assertIn('"command": "seq 1 3"', text)
        self.assertIn("## tool_result bash", text)
        self.assertIn('"stdout": "1\\n2\\n3"', text)
        self.assertIn("full streamed line", text)
        self.assertIn("## thinking_trace\nfirst line\nsecond line", text)


if __name__ == "__main__":
    unittest.main()
