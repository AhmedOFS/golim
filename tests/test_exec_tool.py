import unittest
from unittest.mock import patch

from openterm.mcp.tools import mcp


class ExecToolTests(unittest.TestCase):
    def test_exec_tool_runs_multiline_python_code(self):
        result = getattr(mcp, "exec")(
            code="total = 2 + 3\nprint(total)",
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], "5\n")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["returncode"], 0)

    def test_exec_tool_accepts_friendly_code_alias(self):
        result = getattr(mcp, "exec")(script="print('alias works')")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], "alias works\n")

    def test_exec_tool_reports_python_failure(self):
        result = getattr(mcp, "exec")(code="raise RuntimeError('boom')")

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["returncode"], 1)
        self.assertIn("RuntimeError: boom", result["stderr"])

    def test_exec_tool_does_not_cap_timeout_at_120_seconds(self):
        class Completed:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                self.input = input
                self.timeout = timeout
                return "", ""

            def poll(self):
                return self.returncode

        with patch("openterm.mcp.tools.subprocess.Popen", return_value=Completed()) as popen:
            result = getattr(mcp, "exec")(code="pass", timeout=121)

        self.assertTrue(result["ok"], result)
        self.assertEqual(popen.call_args.kwargs["start_new_session"], True)


if __name__ == "__main__":
    unittest.main()
