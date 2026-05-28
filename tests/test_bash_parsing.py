import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from cterm.tools_mcp import bash
from cterm import utils


class BashParsingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
