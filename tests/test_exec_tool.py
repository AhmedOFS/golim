import unittest
from unittest.mock import patch

from cterm.mcp.tools import mcp


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
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("cterm.mcp.tools.subprocess.run", return_value=completed) as run:
            result = getattr(mcp, "exec")(code="pass", timeout=121)

        self.assertTrue(result["ok"], result)
        self.assertEqual(run.call_args.kwargs["timeout"], 121)


if __name__ == "__main__":
    unittest.main()
