import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from cterm.llm import ToolAgent


class FakeMCPClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def call_tool(self, tool_name, args, stream_output=False, on_stream=None):
        self.calls.append((tool_name, dict(args), stream_output))
        return self.results.pop(0)


class FakeUI:
    def __init__(self, approved=True):
        self.approved = approved
        self.approval_prompts = []

    def update_spinner(self, message):
        pass

    def stop_spinner(self):
        pass

    def message(self, text):
        pass

    def tool_call(self, tool_name, args):
        pass

    def handle_tool_output(self, fd=None, line="", end="\n", result=None):
        pass

    def handle_shell_result_output(self, result):
        pass

    def approve_privileged_binary(self, binary):
        self.approval_prompts.append(binary)
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

        with patch.object(agent.ui, "approve_privileged_binary", return_value=True), \
             redirect_stderr(stderr):
            result = agent._execute_tool("bash", {"command": "sudo snap install spotify"})

        self.assertTrue(result["ok"], result)
        self.assertIn("spotify 1.2.92 installed", stderr.getvalue())

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


if __name__ == "__main__":
    unittest.main()
