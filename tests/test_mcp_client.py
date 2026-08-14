import asyncio
import socket
import unittest
from unittest.mock import patch

from cterm.core.mcp_client import FastMCPClient


class FakeClient(FastMCPClient):
    def __init__(self, responses):
        super().__init__("/tmp")
        self.responses = list(responses)
        self.calls = []

    def _call_tool_once(self, tool_name, call_args, stream_output=False, on_stream=None):
        self.calls.append((tool_name, dict(call_args), stream_output))
        return self.responses.pop(0)


class FakeListClient(FastMCPClient):
    def __init__(self, response):
        super().__init__("/tmp")
        self.response = response

    def _send_request(self, method, params=None):
        self.method = method
        return self.response


class MCPClientTests(unittest.TestCase):
    def test_close_shutdowns_inflight_socket_before_closing(self):
        client = FastMCPClient("/tmp")
        actions = []

        class Socket:
            def shutdown(self, how):
                actions.append(("shutdown", how))

            def close(self):
                actions.append(("close",))

        client._active_sockets.add(Socket())

        client.close()

        self.assertEqual(
            actions,
            [("shutdown", socket.SHUT_RDWR), ("close",)],
        )

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

    def test_make_tool_uses_supplied_schema(self):
        client = FastMCPClient("/tmp")
        schema = {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        }

        tool = client._make_tool("finder", "Tool: finder", schema)

        self.assertEqual(tool.parameters, schema)
        self.assertEqual(tool.inputSchema, schema)
        self.assertIn("pattern", tool.parameters["properties"])

    def test_make_tool_does_not_invent_schema_for_empty_input_schema(self):
        client = FastMCPClient("/tmp")
        tool = client._make_tool("exec", "Tool: exec", {})

        self.assertEqual(tool.parameters, {})
        self.assertEqual(tool.inputSchema, {})

    def test_list_tools_uses_server_input_schema(self):
        server_schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string"},
                "system_inclusive": {"type": "boolean"},
            },
            "required": ["path"],
        }
        client = FakeListClient({
            "result": {
                "tools": [{
                    "name": "finder",
                    "description": "live finder",
                    "inputSchema": server_schema,
                }],
            },
        })

        tools = asyncio.run(client.list_tools())

        self.assertEqual(client.method, "tools/list")
        self.assertEqual(tools[0].parameters, server_schema)
        self.assertEqual(tools[0].inputSchema, server_schema)

    def test_list_tools_keeps_empty_server_schema_empty(self):
        client = FakeListClient({
            "result": {
                "tools": [{
                    "name": "finder",
                    "description": "empty finder",
                    "inputSchema": {},
                }],
            },
        })

        tools = asyncio.run(client.list_tools())

        self.assertEqual(tools[0].parameters, {})

    def test_list_tools_raises_when_response_has_no_tools(self):
        client = FakeListClient({"result": {"tools": []}})

        with self.assertRaises(ValueError):
            asyncio.run(client.list_tools())


if __name__ == "__main__":
    unittest.main()
