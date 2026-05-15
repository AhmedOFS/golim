import unittest

from cterm.tools_mcp import run_shell


class RunShellParsingTests(unittest.TestCase):
    def test_simple_command_still_returns_stdout(self):
        result = run_shell("printf hello")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"][0]["stdout"], "hello")

    def test_command_not_found_still_fails(self):
        result = run_shell("definitely_missing_cterm_command")

        self.assertFalse(result["ok"], result)
        self.assertIn("Command not found", result["error"])

    def test_nonzero_without_stdout_still_fails(self):
        result = run_shell("false")

        self.assertFalse(result["ok"], result)
        self.assertIn("Command failed", result["error"])

    def test_nonzero_with_stdout_still_succeeds(self):
        result = run_shell("du -x -h -d 1 /")

        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result["results"][0]["returncode"], 0)
        self.assertTrue(result["results"][0]["stdout"].strip())

    def test_pipeline_still_works(self):
        result = run_shell("printf hello | wc -c")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"][0]["stdout"].strip(), "5")

    def test_quoted_pipe_is_not_pipeline_separator(self):
        result = run_shell('printf "a|b"')

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"][0]["stdout"], "a|b")

    def test_sudo_prefix_is_still_stripped_for_unprivileged_commands(self):
        result = run_shell("sudo test -d /")

        self.assertTrue(result["ok"], result)

    def test_streaming_stdout_still_works(self):
        frames = list(run_shell("printf hello", stream=True))

        self.assertTrue(frames[-1]["ok"], frames)
        self.assertEqual(frames[-1]["results"][0]["stdout"], "hello")

    def test_expands_home_variable_without_shell(self):
        result = run_shell("test -d $HOME")

        self.assertTrue(result["ok"], result)

    def test_expands_tilde_without_shell(self):
        result = run_shell("test -d ~")

        self.assertTrue(result["ok"], result)

    def test_rejects_other_shell_variables(self):
        result = run_shell("echo $PATH")

        self.assertFalse(result["ok"], result)
        self.assertIn("Forbidden character", result["error"])

    def test_supports_stderr_suppression_to_dev_null(self):
        result = run_shell("ls /definitely_missing_cterm_path 2>/dev/null")

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["results"][0]["stderr"], "")

    def test_chained_commands_still_work(self):
        result = run_shell("test -d $HOME && test -d ~")

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["results"]), 2)

    def test_rejects_other_redirection(self):
        result = run_shell("echo hello >/tmp/cterm-test")

        self.assertFalse(result["ok"], result)
        self.assertIn("Forbidden character", result["error"])

    def test_supports_stderr_suppression_in_pipeline(self):
        result = run_shell("ls /definitely_missing_cterm_path 2>/dev/null | wc -l")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"][0]["stdout"].strip(), "0")
        self.assertEqual(result["results"][0]["stderr"], "")

    def test_streaming_supports_stderr_suppression(self):
        frames = list(run_shell("ls /definitely_missing_cterm_path 2>/dev/null", stream=True))

        self.assertFalse(frames[-1]["ok"], frames)
        self.assertEqual(frames[-1]["results"][0]["stderr"], "")
        self.assertFalse(
            any(frame.get("type") == "stream" and frame.get("fd") == "stderr" for frame in frames),
            frames,
        )


if __name__ == "__main__":
    unittest.main()
