import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openterm.ui.basic.basic import TerminalUI
from openterm.ui.basic.spinner import Spinner
from openterm.logger import log_diagnostic_section, start_run_logging
from openterm.ui.tui.app.transcript_writer import TranscriptWriter


class _RecordingSpinner:
    def __init__(self):
        self.calls = []
        self.stopped = False

    def write_above(self, text, end="\n"):
        self.calls.append((text, end))

    def stop(self):
        self.stopped = True


class _TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class BasicUiOutputTests(unittest.TestCase):
    def test_output_uses_spinner_write_above_while_spinner_is_active(self):
        ui = TerminalUI(config=object())
        spinner = _RecordingSpinner()
        ui._spinner = spinner

        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            ui.message("status")
            ui.tool_call("finder", {"pattern": "*.py", "path": "."})
            ui.tool_output(fd="stdout", line="streamed")
            ui.tool_output(result={"ok": True, "text": "done"})
            ui.shell_output(
                {"results": [{"stdout": "stdout\n", "stderr": "stderr"}]}
            )

        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            spinner.calls,
            [
                ("status", "\n"),
                ("finder: *.py in .", "\n"),
                ("streamed", "\n"),
                ("\033[32m✓\033[0m done", "\n"),
                ("", "\n"),
                ("stdout\n", ""),
                ("\033[33mstderr\033[0m", "\n"),
            ],
        )

    def test_thinking_is_streamed_without_collapsed_or_truncated_duplicate(self):
        ui = TerminalUI(config=object())
        spinner = _RecordingSpinner()
        ui._spinner = spinner

        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            ui.thinking_delta("The user wants to uninstall Spotify. ")
            ui.thinking_delta("Let me check the current OS first.")
            ui.thinking_complete(
                "The user wants to uninstall Spotify. Let me check the current OS first."
            )

        self.assertTrue(spinner.stopped)
        self.assertEqual(ui._spinner, None)
        self.assertIn("THINKING: The user wants to uninstall Spotify.", stderr.getvalue())
        self.assertIn("Let me check the current OS first.", stderr.getvalue())
        self.assertNotIn("▶ THINKING:", stderr.getvalue())

    def test_transcript_records_thinking_without_spinner_output(self):
        transcript_file = io.StringIO()
        ui = TerminalUI(config=object(), transcript=TranscriptWriter(transcript_file))

        ui.status("Thinking")
        ui.thinking_complete("The model checked the current system.")

        text = transcript_file.getvalue()
        self.assertIn("▶ THINKING: The model checked the current system.", text)
        self.assertNotIn("Thinking...", text)
        self.assertNotIn("⠋", text)

    def test_run_logging_keeps_full_diagnostic_sections_out_of_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / "transcripts" / "session.log"
            transcript_path.parent.mkdir()
            run_logging = start_run_logging(transcript_path)
            try:
                log_diagnostic_section(
                    "tool_result bash",
                    {"results": [{"stdout": "full output that is not truncated"}]},
                )
            finally:
                run_logging.close()

            log_path = Path(tmp) / "logs" / "session.log"
            text = log_path.read_text(encoding="utf-8")

        self.assertIn("## tool_result bash", text)
        self.assertIn("full output that is not truncated", text)

    def test_spinner_preserves_partial_and_completed_lines_above_spinner(self):
        stderr = _TtyBuffer()
        with patch("sys.stderr", stderr):
            spinner = Spinner("Thinking", reserve_above=True)
            spinner.running = True
            spinner.write_above("one", end="")
            spinner.write_above("two", end="\n")
            spinner.write_above("next", end="\n")

        output = stderr.getvalue()
        self.assertIn("onetwo", output)
        self.assertIn("next", output)
        self.assertIn("\033[1L", output)


if __name__ == "__main__":
    unittest.main()
