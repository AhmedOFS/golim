import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from golim.toolset.tools import mcp


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

    def test_exec_tool_round_trips_non_ascii_output(self):
        result = getattr(mcp, "exec")(
            code="print('héllo • wörld ✓')",
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], "héllo • wörld ✓\n")

    def test_exec_tool_round_trips_non_ascii_stdin(self):
        result = getattr(mcp, "exec")(
            code="import sys\nprint(sys.stdin.read())",
            stdin="héllo •",
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], "héllo •\n")

    def test_exec_tool_accepts_non_ascii_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "ünïcode •"
            cwd.mkdir()
            result = getattr(mcp, "exec")(
                code="import os\nprint(os.getcwd())",
                cwd=str(cwd),
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], str(cwd) + "\n")

    def test_exec_tool_decodes_undecodable_output_with_replacement(self):
        result = getattr(mcp, "exec")(
            code="import sys\nsys.stdout.buffer.write(b'bad:\\xff\\n')",
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], "bad:\ufffd\n")

    def test_exec_tool_encodes_code_as_utf8_bytes_for_popen(self):
        class Completed:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                self.input = input
                return b"", b""

        with patch("golim.toolset.tools.subprocess.Popen", return_value=Completed()) as popen:
            result = getattr(mcp, "exec")(code="print('héllo •')")

        self.assertTrue(result["ok"], result)
        argv = popen.call_args.args[0]
        self.assertIsInstance(argv[0], bytes)
        self.assertEqual(argv[1], b"-c")
        self.assertEqual(argv[2], "print('héllo •')".encode("utf-8"))
        self.assertIsNone(popen.call_args.kwargs["cwd"])
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUTF8"], "1")

    def test_exec_tool_does_not_cap_timeout_at_120_seconds(self):
        class Completed:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                self.input = input
                self.timeout = timeout
                return b"", b""

            def poll(self):
                return self.returncode

        with patch("golim.toolset.tools.subprocess.Popen", return_value=Completed()) as popen:
            result = getattr(mcp, "exec")(code="pass", timeout=121)

        self.assertTrue(result["ok"], result)
        self.assertEqual(popen.call_args.kwargs["start_new_session"], True)
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")


if __name__ == "__main__":
    unittest.main()
