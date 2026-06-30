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


class ToolApprovalTests(unittest.TestCase):
    def _agent_with_client(self, client):
        agent = ToolAgent("model")
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
        agent = self._agent_with_client(client)

        with patch("cterm.llm.prompt_to_add_privileged_binary", return_value=True) as prompt:
            result = agent._execute_tool("bash", {"command": "sudo chmod 666 new-logs.txt"})

        self.assertTrue(result["ok"], result)
        prompt.assert_called_once_with("/usr/bin/chmod")
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

        with patch("cterm.llm.prompt_to_add_privileged_binary", return_value=True), \
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
        agent = self._agent_with_client(client)

        with patch("cterm.llm.prompt_to_add_privileged_binary", return_value=False):
            result = agent._execute_tool("bash", {"command": "sudo chmod 666 new-logs.txt"})

        self.assertFalse(result["ok"], result)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("not approved", result["error"])


if __name__ == "__main__":
    unittest.main()
