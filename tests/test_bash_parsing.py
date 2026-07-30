import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from cterm.mcp.tools import bash, read_file, _should_stream_with_pty
from cterm.mcp.utils import bash_utils


class BashParsingTests(unittest.TestCase):
    def setUp(self):
        self.unrestricted_patch = patch("cterm.mcp.tools._is_bash_unrestricted", return_value=False)
        self.unrestricted_patch.start()

    def tearDown(self):
        self.unrestricted_patch.stop()

    def test_simple_command_still_returns_stdout(self):
        result = bash("printf hello")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"][0]["stdout"], "hello")

    def test_command_not_found_still_fails(self):
        result = bash("definitely_missing_cterm_command")

        self.assertFalse(result["ok"], result)
        self.assertIn("Command not found", result["error"])

    def test_nonzero_without_stdout_still_fails(self):
        result = bash("false")

        self.assertFalse(result["ok"], result)
        self.assertIn("Command failed", result["error"])

    def test_nonzero_with_stdout_still_succeeds(self):
        result = bash("du -x -h -d 1 /")

        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result["results"][0]["returncode"], 0)
        self.assertTrue(result["results"][0]["stdout"].strip())

    def test_pipeline_still_works(self):
        result = bash("printf hello | wc -c")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"][0]["stdout"].strip(), "5")

    def test_quoted_pipe_is_not_pipeline_separator(self):
        result = bash('printf "a|b"')

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"][0]["stdout"], "a|b")

    def test_sudo_returns_approval_required_when_binary_is_not_whitelisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "cterm-privileged"
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            whitelist = Path(tmp) / "privileged_whitelist"

            with patch.dict(os.environ, {"CTERM_PRIVILEGED_WHITELIST": str(whitelist)}), \
                 patch.object(bash_utils, "PRIVILEGED_WRAPPER", str(wrapper)):
                parsed, err = bash_utils._parse_command_part("sudo test -d /", [])

        self.assertIsNone(parsed)
        self.assertFalse(err["ok"], err)
        self.assertTrue(err["approval_required"], err)
        self.assertEqual(err["approval_kind"], "privileged_whitelist")
        self.assertIn("Privileged command requires approval", err["error"])

    def test_sudo_allow_privileged_updates_whitelist_and_routes_to_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "cterm-privileged"
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            whitelist = Path(tmp) / "privileged_whitelist"

            with patch.dict(os.environ, {"CTERM_PRIVILEGED_WHITELIST": str(whitelist)}), \
                 patch.object(bash_utils, "PRIVILEGED_WRAPPER", str(wrapper)):
                parsed, err = bash_utils._parse_command_part(
                    "sudo test -d /",
                    [],
                    allow_privileged=True,
                )

            self.assertIsNone(err)
            resolved_test = bash_utils.shutil.which("test")
            self.assertEqual(
                parsed.argv_list[0].argv,
                ["sudo", "--non-interactive", str(wrapper), resolved_test, "-d", "/"],
            )
            self.assertIn(resolved_test, whitelist.read_text(encoding="utf-8").splitlines())

    def test_privileged_snap_and_apt_stream_with_pty(self):
        wrapper = bash_utils.PRIVILEGED_WRAPPER

        self.assertTrue(_should_stream_with_pty(["sudo", "--non-interactive", wrapper, "/usr/bin/snap", "install", "spotify"]))
        self.assertTrue(_should_stream_with_pty(["sudo", "--non-interactive", wrapper, "/usr/bin/apt", "install", "spotify"]))
        self.assertTrue(_should_stream_with_pty(["sudo", "--non-interactive", wrapper, "/usr/bin/apt-get", "install", "spotify"]))
        self.assertFalse(_should_stream_with_pty(["/usr/bin/snap", "run", "spotify"]))
        self.assertFalse(_should_stream_with_pty(["sudo", "--non-interactive", wrapper, "/usr/bin/chmod", "666", "file"]))

    def test_unrestricted_privileged_apt_command_requires_pty(self):
        wrapper = bash_utils.PRIVILEGED_WRAPPER

        self.assertTrue(
            bash_utils._command_requires_pty_streaming(
                f"sudo --non-interactive {wrapper} /usr/bin/apt install spotify"
            )
        )
        self.assertFalse(
            bash_utils._command_requires_pty_streaming(
                f"sudo --non-interactive {wrapper} /usr/bin/chmod 666 file"
            )
        )

    def test_unrestricted_streaming_privileged_apt_uses_pty_helper(self):
        calls = []

        def fake_stream_command_with_pty(argv, command, results_ref, **kwargs):
            calls.append((argv, command, kwargs))
            yield {"type": "stream", "fd": "stdout", "line": "pty output", "end": "\n"}
            return {
                "command": command,
                "stdout": "pty output",
                "stderr": "",
                "returncode": 0,
            }, None

        with patch("cterm.mcp.tools._is_bash_unrestricted", return_value=True), \
             patch.object(bash_utils, "is_privileged_binary_allowed", return_value=True), \
             patch.object(bash_utils, "_stream_command_with_pty", fake_stream_command_with_pty):
            frames = list(bash("sudo apt install spotify", stream=True))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["/bin/bash", "-c", calls[0][1]])
        self.assertIn(f"{bash_utils.PRIVILEGED_WRAPPER} /usr/bin/apt", calls[0][1])
        self.assertEqual(frames[0]["line"], "pty output")
        self.assertTrue(frames[-1]["ok"], frames)

    def test_streaming_stdout_still_works(self):
        frames = list(bash("printf hello", stream=True))

        self.assertTrue(frames[-1]["ok"], frames)
        self.assertEqual(frames[-1]["results"][0]["stdout"], "hello")

    def test_accepts_string_timeout_for_non_streaming_command(self):
        result = bash("printf hello", timeout="120")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"][0]["stdout"], "hello")

    def test_accepts_string_timeout_for_streaming_command(self):
        frames = list(bash("printf hello", stream=True, timeout="120"))

        self.assertTrue(frames[-1]["ok"], frames)
        self.assertEqual(frames[-1]["results"][0]["stdout"], "hello")

    def test_invalid_timeout_returns_clear_error(self):
        result = bash("printf hello", timeout="soon")

        self.assertFalse(result["ok"], result)
        self.assertIn("timeout must be a number", result["error"])

    def test_boolean_timeout_is_rejected(self):
        result = bash("printf hello", timeout=True)

        self.assertFalse(result["ok"], result)
        self.assertIn("not a boolean", result["error"])

    def test_approval_retry_exposes_only_unexecuted_chain_suffix(self):
        with patch.object(bash_utils, "is_privileged_binary_allowed", return_value=False), \
             patch.object(bash_utils.os.path, "isfile", return_value=True):
            result = bash("printf first && sudo echo second")

        self.assertTrue(result["approval_required"], result)
        self.assertEqual(result["retry_command"], "sudo echo second")
        self.assertEqual(result["results"][0]["stdout"], "first")

    def test_expands_home_variable_without_shell(self):
        result = bash("test -d $HOME")

        self.assertTrue(result["ok"], result)

    def test_expands_tilde_without_shell(self):
        result = bash("test -d ~")

        self.assertTrue(result["ok"], result)

    def test_expands_general_environment_variables(self):
        result = bash("echo $PATH")

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["results"][0]["stdout"].strip())

    def test_supports_stderr_suppression_to_dev_null(self):
        result = bash("ls /definitely_missing_cterm_path 2>/dev/null")

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["results"][0]["stderr"], "")

    def test_chained_commands_still_work(self):
        result = bash("test -d $HOME && test -d ~")

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["results"]), 2)

    def test_rejects_other_redirection(self):
        result = bash("echo hello >/tmp/cterm-test")

        self.assertFalse(result["ok"], result)
        self.assertIn("Forbidden character", result["error"])

    def test_supports_stderr_suppression_in_pipeline(self):
        result = bash("ls /definitely_missing_cterm_path 2>/dev/null | wc -l")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"][0]["stdout"].strip(), "0")
        self.assertEqual(result["results"][0]["stderr"], "")

    def test_streaming_supports_stderr_suppression(self):
        frames = list(bash("ls /definitely_missing_cterm_path 2>/dev/null", stream=True))

        self.assertFalse(frames[-1]["ok"], frames)
        self.assertEqual(frames[-1]["results"][0]["stderr"], "")
        self.assertFalse(
            any(frame.get("type") == "stream" and frame.get("fd") == "stderr" for frame in frames),
            frames,
        )

    def test_long_output_is_truncated_to_first_and_last_half(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            result = bash("seq 1 60")

            self.assertTrue(result["ok"], result)
            self.assertTrue(result["output_truncated"], result)
            self.assertEqual(result["output_line_count"], 60)
            self.assertEqual(result["output_limit"], 50)
            self.assertNotIn("output_file", result)

            lines = result["results"][0]["stdout"].splitlines()
            # First 25 lines (1..25), a marker, then last 25 lines (36..60).
            self.assertEqual(lines[:25], [str(i) for i in range(1, 26)])
            self.assertIn("truncated", lines[25])
            self.assertEqual(lines[26:], [str(i) for i in range(36, 61)])

    def test_short_output_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            result = bash("seq 1 30")

            self.assertTrue(result["ok"], result)
            self.assertFalse(result["output_truncated"], result)
            self.assertEqual(result["output_line_count"], 30)
            self.assertNotIn("output_file", result)
            self.assertEqual(len(result["results"][0]["stdout"].splitlines()), 30)

    def test_long_find_output_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            root = Path(tmp) / "files"
            root.mkdir()
            for idx in range(60):
                (root / f"file_{idx}.txt").write_text("x", encoding="utf-8")

            result = bash(f"find {root} -type f")

            self.assertTrue(result["ok"], result)
            self.assertTrue(result["output_truncated"], result)
            self.assertGreaterEqual(result["output_line_count"], 60)
            self.assertNotIn("output_file", result)

            lines = result["results"][0]["stdout"].splitlines()
            self.assertEqual(len(lines[:25]), 25)
            self.assertIn("truncated", lines[25])
            self.assertEqual(len(lines[26:]), 25)

    def test_long_du_output_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            root = Path(tmp) / "sizes"
            root.mkdir()
            for idx in range(60):
                (root / f"file_{idx}.txt").write_text("x", encoding="utf-8")

            result = bash(f"du -a {root}")

            self.assertTrue(result["ok"], result)
            self.assertTrue(result["output_truncated"], result)
            self.assertNotIn("output_file", result)

    def test_streaming_emits_all_lines_and_truncates_final_result(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            root = Path(tmp) / "files"
            root.mkdir()
            for idx in range(60):
                (root / f"file_{idx}.txt").write_text("x", encoding="utf-8")

            frames = list(bash(f"find {root} -type f", stream=True))

            stream_frames = [frame for frame in frames if frame.get("type") == "stream"]
            # Streaming is not capped; all 60 lines are emitted live.
            self.assertEqual(len(stream_frames), 60)
            # The final result frame is truncated (first 25 + marker + last 25).
            self.assertTrue(frames[-1]["output_truncated"], frames[-1])
            self.assertNotIn("output_file", frames[-1])
            result_lines = frames[-1]["results"][0]["stdout"].splitlines()
            self.assertEqual(len(result_lines[:25]), 25)
            self.assertIn("truncated", result_lines[25])
            self.assertEqual(len(result_lines[26:]), 25)

    def test_streaming_short_output_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            frames = list(bash("seq 1 30", stream=True))

            stream_frames = [frame for frame in frames if frame.get("type") == "stream"]
            self.assertEqual(len(stream_frames), 30)
            self.assertEqual(stream_frames[-1]["line"], "30")
            self.assertFalse(frames[-1]["output_truncated"], frames[-1])
            self.assertNotIn("output_file", frames[-1])

    def test_read_file_returns_200_line_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.txt"
            path.write_text("\n".join(str(i) for i in range(1, 421)), encoding="utf-8")

            result = read_file(str(path), page=2)

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["page"], 2)
            self.assertEqual(result["page_size"], 200)
            self.assertEqual(result["total_lines"], 420)
            self.assertEqual(result["total_pages"], 3)
            self.assertTrue(result["has_next_page"])
            self.assertEqual(result["next_page"], 3)
            self.assertEqual(result["content"].splitlines()[0], "201")
            self.assertEqual(result["content"].splitlines()[-1], "400")


if __name__ == "__main__":
    unittest.main()
