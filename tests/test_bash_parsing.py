import unittest
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from cterm.tools_mcp import bash, read_file
from cterm import utils


class BashParsingTests(unittest.TestCase):
    def setUp(self):
        self.unrestricted_patch = patch("cterm.tools_mcp._is_bash_unrestricted", return_value=False)
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
                 patch.object(utils, "PRIVILEGED_WRAPPER", str(wrapper)):
                parsed, err = utils._parse_command_part("sudo test -d /", [])

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
                 patch.object(utils, "PRIVILEGED_WRAPPER", str(wrapper)):
                parsed, err = utils._parse_command_part(
                    "sudo test -d /",
                    [],
                    allow_privileged=True,
                )

            self.assertIsNone(err)
            resolved_test = utils.shutil.which("test")
            self.assertEqual(
                parsed.argv_list[0].argv,
                ["sudo", "--non-interactive", str(wrapper), resolved_test, "-d", "/"],
            )
            self.assertIn(resolved_test, whitelist.read_text(encoding="utf-8").splitlines())

    def test_streaming_stdout_still_works(self):
        frames = list(bash("printf hello", stream=True))

        self.assertTrue(frames[-1]["ok"], frames)
        self.assertEqual(frames[-1]["results"][0]["stdout"], "hello")

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

    def test_long_non_find_du_bash_output_is_not_saved_to_file(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            result = bash("seq 1 60")

            self.assertTrue(result["ok"], result)
            self.assertFalse(result["output_truncated"], result)
            self.assertEqual(result["output_line_count"], 60)
            self.assertNotIn("output_file", result)
            self.assertEqual(len(result["results"][0]["stdout"].splitlines()), 60)

    def test_long_find_output_is_saved_to_file(self):
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
            self.assertEqual(len(result["results"][0]["stdout"].splitlines()), 50)

            output_file = Path(result["output_file"])
            self.assertEqual(output_file.parent, Path(tmp) / "cterm" / "data")
            saved = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(saved["results"][0]["stdout"].splitlines()), 60)

    def test_long_du_output_is_saved_to_file(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            root = Path(tmp) / "sizes"
            root.mkdir()
            for idx in range(60):
                (root / f"file_{idx}.txt").write_text("x", encoding="utf-8")

            result = bash(f"du -a {root}")

            self.assertTrue(result["ok"], result)
            self.assertTrue(result["output_truncated"], result)
            self.assertIn("output_file", result)

    def test_streaming_long_find_output_emits_only_first_50_lines(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            root = Path(tmp) / "files"
            root.mkdir()
            for idx in range(60):
                (root / f"file_{idx}.txt").write_text("x", encoding="utf-8")

            frames = list(bash(f"find {root} -type f", stream=True))

            stream_frames = [frame for frame in frames if frame.get("type") == "stream"]
            self.assertEqual(len(stream_frames), 50)
            self.assertTrue(frames[-1]["output_truncated"], frames[-1])
            self.assertTrue(Path(frames[-1]["output_file"]).exists())

    def test_streaming_long_non_find_du_output_emits_all_lines(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            frames = list(bash("seq 1 60", stream=True))

            stream_frames = [frame for frame in frames if frame.get("type") == "stream"]
            self.assertEqual(len(stream_frames), 60)
            self.assertEqual(stream_frames[-1]["line"], "60")
            self.assertFalse(frames[-1]["output_truncated"], frames[-1])
            self.assertNotIn("output_file", frames[-1])

    def test_read_file_returns_50_line_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.txt"
            path.write_text("\n".join(str(i) for i in range(1, 121)), encoding="utf-8")

            result = read_file(str(path), page=2)

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["page"], 2)
            self.assertEqual(result["page_size"], 50)
            self.assertEqual(result["total_lines"], 120)
            self.assertEqual(result["total_pages"], 3)
            self.assertTrue(result["has_next_page"])
            self.assertEqual(result["next_page"], 3)
            self.assertEqual(result["content"].splitlines()[0], "51")
            self.assertEqual(result["content"].splitlines()[-1], "100")


if __name__ == "__main__":
    unittest.main()
