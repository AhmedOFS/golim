import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from cterm.core.agent import ToolAgent


class FakeMCPClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def call_tool(self, tool_name, args, stream_output=False, on_stream=None):
        self.calls.append((tool_name, dict(args), stream_output))
        return self.results.pop(0)


class StreamingFakeMCPClient(FakeMCPClient):
    async def call_tool(self, tool_name, args, stream_output=False, on_stream=None):
        self.calls.append((tool_name, dict(args), stream_output))
        if stream_output and on_stream:
            on_stream("stdout", "live line", "\n")
        return self.results.pop(0)


class FakeUI:
    def __init__(self, approved=True):
        self.approved = approved
        self.approval_prompts = []
        self.tool_results = []
        self.shell_results = []
        self.streams = []

    def status(self, message):
        pass

    def clear_status(self):
        pass

    def message(self, text):
        pass

    def tool_call(self, tool_name, args):
        pass

    def tool_output(self, fd=None, line="", end="\n", result=None):
        if result is not None:
            self.tool_results.append(result)
        elif fd is not None:
            self.streams.append((fd, line, end))

    def shell_output(self, result):
        self.shell_results.append(result)

    def request_binary_approval(self, binary):
        self.approval_prompts.append(binary)
        return self.approved

    def request_python_approval(self, code):
        self.approval_prompts.append(("python", code))
        return self.approved

    def request_write_approval(self, path, content, mode):
        self.approval_prompts.append(("write", path, content, mode))
        return self.approved


class ToolApprovalTests(unittest.TestCase):
    def _agent_with_client(self, client, ui=None):
        agent = ToolAgent("model", ui=ui)
        agent.mcp_client = client
        return agent

    def test_tool_agent_prompts_then_retries_with_service_side_approval(self):
        client = FakeMCPClient([
            {
                "ok": False,
                "approval_required": True,
                "approval_kind": "privileged_whitelist",
                "binary": "/usr/bin/chmod",
            },
            {"ok": True, "results": []},
        ])
        ui = FakeUI(approved=True)
        agent = self._agent_with_client(client, ui=ui)

        result = agent._execute_tool("bash", {"command": "sudo chmod 666 new-logs.txt"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(ui.approval_prompts, ["/usr/bin/chmod"])
        self.assertEqual(client.calls[0][1], {"command": "sudo chmod 666 new-logs.txt"})
        self.assertEqual(
            client.calls[1][1],
            {"command": "sudo chmod 666 new-logs.txt", "allow_privileged": True},
        )
        self.assertTrue(client.calls[0][2])
        self.assertTrue(client.calls[1][2])

    def test_shell_retry_prints_captured_output_when_no_stream_frame_arrives(self):
        client = FakeMCPClient([
            {
                "ok": False,
                "approval_required": True,
                "approval_kind": "privileged_whitelist",
                "binary": "/usr/bin/snap",
            },
            {
                "ok": True,
                "results": [{
                    "command": "sudo snap install spotify",
                    "stdout": "spotify 1.2.92 installed",
                    "stderr": "",
                    "returncode": 0,
                }],
            },
        ])
        agent = self._agent_with_client(client)
        stderr = StringIO()

        with patch.object(agent.ui, "request_binary_approval", return_value=True), \
             redirect_stderr(stderr):
            result = agent._execute_tool("bash", {"command": "sudo snap install spotify"})

        self.assertTrue(result["ok"], result)
        self.assertIn("spotify 1.2.92 installed", stderr.getvalue())

    def test_streaming_bash_forwards_final_result_to_ui(self):
        final_result = {
            "ok": True,
            "output_truncated": True,
            "results": [{
                "command": "seq 1 60",
                "stdout": "\n".join(str(i) for i in range(1, 51)) + "\n",
                "stderr": "",
                "returncode": 0,
            }],
        }
        client = StreamingFakeMCPClient([final_result])
        ui = FakeUI()
        agent = self._agent_with_client(client, ui=ui)

        result = agent._execute_tool("bash", {"command": "seq 1 60"})

        self.assertIs(result, final_result)
        self.assertEqual(ui.streams, [("stdout", "live line", "\n")])
        self.assertEqual(ui.tool_results, [final_result])
        self.assertEqual(ui.shell_results, [])

    def test_tool_agent_denial_does_not_retry(self):
        client = FakeMCPClient([
            {
                "ok": False,
                "approval_required": True,
                "approval_kind": "privileged_whitelist",
                "binary": "/usr/bin/chmod",
            },
        ])
        ui = FakeUI(approved=False)
        agent = self._agent_with_client(client, ui=ui)

        result = agent._execute_tool("bash", {"command": "sudo chmod 666 new-logs.txt"})

        self.assertFalse(result["ok"], result)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(ui.approval_prompts, ["/usr/bin/chmod"])
        self.assertIn("not approved", result["error"])

    def test_write_file_prompts_before_writing_in_restricted_mode(self):
        client = FakeMCPClient([
            {"ok": True, "path": "/home/ahmed/out.txt", "bytes_written": 5},
        ])
        ui = FakeUI(approved=True)
        agent = self._agent_with_client(client, ui=ui)

        with patch("cterm.core.agent.get_config") as get_config:
            get_config.return_value.unrestricted_bash = False
            result = agent._execute_tool("write_file", {
                "path": "/home/ahmed/out.txt",
                "content": "hello",
                "mode": "overwrite",
            })

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            ui.approval_prompts,
            [("write", "/home/ahmed/out.txt", "hello", "overwrite")],
        )
        self.assertEqual(client.calls[0][0], "write_file")

    def test_write_file_denial_skips_execution(self):
        client = FakeMCPClient([])
        ui = FakeUI(approved=False)
        agent = self._agent_with_client(client, ui=ui)

        with patch("cterm.core.agent.get_config") as get_config:
            get_config.return_value.unrestricted_bash = False
            result = agent._execute_tool("write_file", {
                "path": "/home/ahmed/out.txt",
                "content": "hello",
                "mode": "overwrite",
            })

        self.assertFalse(result["ok"], result)
        self.assertEqual(len(client.calls), 0)
        self.assertIn("not approved", result["error"])
        self.assertFalse(ui.tool_results[0]["ok"])

    def test_write_file_unrestricted_mode_skips_approval(self):
        client = FakeMCPClient([
            {"ok": True, "path": "/home/ahmed/out.txt", "bytes_written": 5},
        ])
        ui = FakeUI(approved=False)
        agent = self._agent_with_client(client, ui=ui)

        with patch("cterm.core.agent.get_config") as get_config:
            get_config.return_value.unrestricted_bash = True
            result = agent._execute_tool("write_file", {
                "path": "/home/ahmed/out.txt",
                "content": "hello",
                "mode": "overwrite",
            })

        self.assertTrue(result["ok"], result)
        self.assertEqual(ui.approval_prompts, [])
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
