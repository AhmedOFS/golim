import asyncio
import unittest
from unittest.mock import patch

from cterm.llm_utils.mcp_client import FastMCPClient


class FakeClient(FastMCPClient):
    def __init__(self, responses):
        super().__init__("/tmp")
        self.responses = list(responses)
        self.calls = []

    def _call_tool_once(self, tool_name, call_args, stream_output=False, on_stream=None):
        self.calls.append((tool_name, dict(call_args), stream_output))
        return self.responses.pop(0)


class MCPClientTests(unittest.TestCase):
    def test_client_returns_privileged_approval_required_without_prompting(self):
        client = FakeClient([
            {
                "result": {
                    "ok": False,
                    "approval_required": True,
                    "approval_kind": "privileged_whitelist",
                    "binary": "/usr/bin/systemctl",
                },
            },
        ])

        result = asyncio.run(client.call_tool("bash", {"command": "sudo systemctl status"}))

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["approval_required"], result)
        self.assertEqual(client.calls[0][1], {"command": "sudo systemctl status"})

    def test_client_passes_explicit_privileged_approval_to_service(self):
        client = FakeClient([
            {"result": {"ok": True, "results": []}},
        ])

        result = asyncio.run(client.call_tool(
            "bash",
            {"command": "sudo systemctl status", "allow_privileged": True},
        ))

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            client.calls[0][1],
            {"command": "sudo systemctl status", "allow_privileged": True},
        )

    def test_finder_schema_requires_pattern(self):
        client = FastMCPClient("/tmp")
        tool = client._make_tool("finder", "Tool: finder", {})

        self.assertEqual(tool.parameters["required"], ["path", "pattern"])
        self.assertIn("pattern", tool.parameters["properties"])


if __name__ == "__main__":
    unittest.main()
