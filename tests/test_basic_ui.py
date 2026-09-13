import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from golim.ui.basic.basic import TerminalUI
from golim.ui.basic.spinner import Spinner
from golim.logger import log_diagnostic_section, start_run_logging
from golim.ui.tui.app.transcript_writer import TranscriptWriter


class _RecordingSpinner:
    def __init__(self):
        self.calls = []
        self.stopped = False

    def write_above(self, text, end="\n"):
        self.calls.append((text, end))

    def stop(self):
        self.stopped = True


class BasicUiSudoAuthTests(unittest.TestCase):
    def test_request_sudo_password_uses_hidden_input(self):
        ui = TerminalUI(config=object())

        with patch("golim.ui.basic.basic.getpass.getpass", return_value="sekret") as getpass_mock:
            password = ui.request_sudo_password()

        self.assertEqual(password, "sekret")
        getpass_mock.assert_called_once()

    def test_request_sudo_password_returns_none_on_interrupt(self):
        ui = TerminalUI(config=object())

        def interrupt(prompt):
            raise KeyboardInterrupt

        with patch("golim.ui.basic.basic.getpass.getpass", side_effect=interrupt):
            self.assertIsNone(ui.request_sudo_password())

    def test_request_sudo_password_treats_empty_as_decline(self):
        ui = TerminalUI(config=object())

        with patch("golim.ui.basic.basic.getpass.getpass", return_value=""):
            self.assertIsNone(ui.request_sudo_password())


class _TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class BasicUiOutputTests(unittest.TestCase):
    def test_escape_requests_soft_then_hard_interrupt(self):
        ui = TerminalUI(config=object())
        runtime = MagicMock()
        runtime.should_interrupt.side_effect = [False, True]
        ui._runtime = runtime
        ui.status = MagicMock()

        ui._handle_escape()
        ui._handle_escape()

        runtime.interrupt.assert_called_once_with()
        runtime.hard_cancel.assert_called_once_with()
        self.assertEqual(
            ui.status.call_args_list,
            [
                call("Interrupting. Press Esc again to force."),
                call("Interrupting. Press Esc again to force."),
            ],
        )

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

    def test_thinking_streams_above_active_spinner_without_stopping_it(self):
        ui = TerminalUI(config=object())
        spinner = _RecordingSpinner()
        ui._spinner = spinner

        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            ui.thinking_delta("The user wants to uninstall Spotify. ")
            ui.thinking_delta("Let me check the current OS first.")
            ui.thinking_complete(
                "The user wants to uninstall Spotify. Let me check the current OS first."
            )

        self.assertFalse(spinner.stopped)
        self.assertIs(ui._spinner, spinner)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            spinner.calls,
            [
                ("\033[38;5;248mTHINKING: The user wants to uninstall Spotify. \033[0m", ""),
                ("\033[38;5;248mLet me check the current OS first.\033[0m", ""),
                ("", "\n"),
            ],
        )

    def test_bash_carriage_return_stream_stays_on_one_line(self):
        ui = TerminalUI(config=object())

        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            ui.status("bash")
            ui.tool_output(fd="stdout", line="step 1", end="\r")
            ui.tool_output(fd="stdout", line="step 2", end="\r")
            ui.tool_output(fd="stdout", line="done", end="\n")
            ui.clear_status()

        self.assertEqual(stderr.getvalue(), "step 1\rstep 2\rdone\n")

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

    def test_spinner_keeps_multiple_lines_above_spinner(self):
        stderr = _TtyBuffer()
        with patch("sys.stderr", stderr):
            spinner = Spinner("Thinking", reserve_above=True)
            spinner.running = True
            spinner.write_above("first\nsecond", end="")
            spinner.write_above("third", end="\n")

        output = stderr.getvalue()
        self.assertIn("first", output)
        self.assertIn("second", output)
        self.assertIn("third", output)
        self.assertGreaterEqual(output.count("\033[1L"), 2)

    def test_spinner_does_not_animate_when_stderr_is_not_a_tty(self):
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            spinner = Spinner("Thinking", reserve_above=True)
            spinner.start()
            spinner.write_above("thinking", end="")
            spinner.stop()

        self.assertIsNone(spinner.thread)
        self.assertEqual(stderr.getvalue(), "thinking")


if __name__ == "__main__":
    unittest.main()
