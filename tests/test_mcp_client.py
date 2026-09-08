import asyncio
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from golim.core.mcp_client import FastMCPClient


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
    @staticmethod
    def _run_fake_server(socket_path, behaviors, received):
        """Serve a fixed per-connection script over a UDS socket.

        Each behavior is a list of actions: ("recv",) records one JSON
        request, ("send", frame) writes one JSON frame, ("close",) ends the
        connection.
        """
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(8)

        def serve():
            try:
                for behavior in behaviors:
                    conn, _ = server.accept()
                    try:
                        for action in behavior:
                            if action[0] == "recv":
                                data = conn.recv(65536)
                                if data:
                                    received.append(json.loads(data.decode().strip()))
                            elif action[0] == "send":
                                conn.sendall((json.dumps(action[1]) + "\n").encode())
                            elif action[0] == "close":
                                break
                    finally:
                        try:
                            conn.close()
                        except OSError:
                            pass
            finally:
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _approval_server_behaviors():
        progress_notification = {
            "jsonrpc": "2.0",
            "method": "tools/progress",
            "params": {"token": 1, "fd": "stdout", "line": "live", "end": "\n"},
        }
        approval_frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "approval_request": {
                "approval_id": "1:0",
                "approval_kind": "privileged_whitelist",
                "binary": "/usr/bin/apt",
                "command": "sudo apt update",
            },
        }
        final_frame = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True, "results": []}}
        return [
            [
                ("recv",),
                ("send", progress_notification),
                ("send", approval_frame),
                ("send", final_frame),
                ("close",),
            ],
            [
                ("recv",),
                ("send", {"jsonrpc": "2.0", "id": 2, "result": {"resolved": True}}),
                ("close",),
            ],
        ]

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

    def test_approval_decision_defaults_to_deny_without_callback(self):
        client = FastMCPClient("/tmp")

        self.assertFalse(client._handle_approval_request({
            "binary": "/usr/bin/apt",
        }))

    def test_approval_decision_uses_callback_when_set(self):
        client = FastMCPClient("/tmp")
        client.on_approval_request = lambda approval: approval["binary"] == "/usr/bin/apt"

        self.assertTrue(client._handle_approval_request({"binary": "/usr/bin/apt"}))
        self.assertFalse(client._handle_approval_request({"binary": "/usr/bin/chmod"}))

    def test_progress_notification_dispatches_to_stream_callback(self):
        client = FastMCPClient("/tmp")
        streams = []

        consumed = client._dispatch_interim_frame(
            {
                "jsonrpc": "2.0",
                "method": "tools/progress",
                "params": {"token": 1, "fd": "stderr", "line": "warn", "end": "\n"},
            },
            lambda fd, line, end: streams.append((fd, line, end)),
        )

        self.assertTrue(consumed)
        self.assertEqual(streams, [("stderr", "warn", "\n")])

    def test_auth_request_defaults_to_no_password_without_callback(self):
        client = FastMCPClient("/tmp")

        self.assertIsNone(client._handle_auth_request({"auth_id": "1:auth:0"}))

    def test_auth_request_uses_callback_when_set(self):
        client = FastMCPClient("/tmp")
        client.on_auth_request = lambda auth: "sekret"

        self.assertEqual(
            client._handle_auth_request({"auth_id": "1:auth:0", "kind": "sudo_password"}),
            "sekret",
        )

    @staticmethod
    def _auth_server_behaviors():
        auth_frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "auth_request": {"auth_id": "1:auth:0", "kind": "sudo_password"},
        }
        final_frame = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True, "results": []}}
        return [
            [
                ("recv",),
                ("send", auth_frame),
                ("send", final_frame),
                ("close",),
            ],
            [
                ("recv",),
                ("send", {"jsonrpc": "2.0", "id": 2, "result": {"resolved": True}}),
                ("close",),
            ],
        ]

    def test_stream_request_handles_auth_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "mcp.sock"
            received = []
            passwords = []
            thread = self._run_fake_server(
                socket_path, self._auth_server_behaviors(), received,
            )
            client = FastMCPClient(socket_path)
            client.on_auth_request = lambda auth: passwords.append(auth) or "sekret"

            frame = client._stream_request(
                "tools/call",
                {"name": "bash", "arguments": {"command": "sudo apt update"}},
            )
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(frame["result"], {"ok": True, "results": []})
        self.assertEqual(passwords[0]["kind"], "sudo_password")
        self.assertEqual(
            received[1],
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "auth/respond",
                "params": {"auth_id": "1:auth:0", "password": "sekret"},
            },
        )

    def test_stream_request_reports_no_password_without_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "mcp.sock"
            received = []
            thread = self._run_fake_server(
                socket_path, self._auth_server_behaviors(), received,
            )
            client = FastMCPClient(socket_path)

            frame = client._stream_request(
                "tools/call",
                {"name": "bash", "arguments": {"command": "sudo apt update"}},
            )
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            received[1]["params"],
            {"auth_id": "1:auth:0", "password": None},
        )

    def test_auth_status_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "mcp.sock"
            received = []
            thread = self._run_fake_server(
                socket_path,
                [[
                    ("recv",),
                    ("send", {"jsonrpc": "2.0", "id": 1, "result": {
                        "ok": True, "wrapper_installed": True, "authenticated": False,
                    }}),
                    ("close",),
                ]],
                received,
            )
            client = FastMCPClient(socket_path)
            status = client.auth_status()
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(status, {"ok": True, "wrapper_installed": True, "authenticated": False})
        self.assertEqual(received[0]["method"], "auth/status")

    def test_authenticate_sends_password_via_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "mcp.sock"
            received = []
            thread = self._run_fake_server(
                socket_path,
                [[
                    ("recv",),
                    ("send", {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}),
                    ("close",),
                ]],
                received,
            )
            client = FastMCPClient(socket_path)
            result = client.authenticate("sekret")
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, {"ok": True})
        self.assertEqual(received[0]["method"], "auth/sudo_password")
        self.assertEqual(received[0]["params"], {"password": "sekret"})

    def test_unknown_notification_is_ignored_without_callback(self):
        client = FastMCPClient("/tmp")

        self.assertTrue(client._dispatch_interim_frame({
            "jsonrpc": "2.0",
            "method": "tools/other",
            "params": {},
        }))

    def test_response_frames_are_not_interim(self):
        client = FastMCPClient("/tmp")

        self.assertFalse(client._dispatch_interim_frame(
            {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}, None,
        ))
        self.assertFalse(client._dispatch_interim_frame(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}, None,
        ))

    def test_stream_request_handles_approval_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "mcp.sock"
            received = []
            approvals = []
            thread = self._run_fake_server(
                socket_path, self._approval_server_behaviors(), received,
            )
            client = FastMCPClient(socket_path)
            client.on_approval_request = lambda approval: approvals.append(approval) or True
            streams = []

            frame = client._stream_request(
                "tools/call",
                {"name": "bash", "arguments": {"command": "sudo apt update"}},
                on_stream=lambda fd, line, end: streams.append((fd, line, end)),
            )
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(frame["result"], {"ok": True, "results": []})
        self.assertEqual(streams, [("stdout", "live", "\n")])
        self.assertEqual(approvals[0]["binary"], "/usr/bin/apt")
        self.assertEqual(
            received[1],
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "approval/respond",
                "params": {"approval_id": "1:0", "approved": True},
            },
        )

    def test_stream_request_denies_approval_without_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "mcp.sock"
            received = []
            thread = self._run_fake_server(
                socket_path, self._approval_server_behaviors(), received,
            )
            client = FastMCPClient(socket_path)

            frame = client._stream_request(
                "tools/call",
                {"name": "bash", "arguments": {"command": "sudo apt update"}},
            )
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(frame["result"], {"ok": True, "results": []})
        self.assertEqual(received[1]["method"], "approval/respond")
        self.assertFalse(received[1]["params"]["approved"])

    def test_send_request_handles_approval_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "mcp.sock"
            received = []
            thread = self._run_fake_server(
                socket_path, self._approval_server_behaviors(), received,
            )
            client = FastMCPClient(socket_path)
            client.on_approval_request = lambda approval: True

            frame = client._send_request(
                "tools/call",
                {"name": "bash", "arguments": {"command": "sudo apt update"}},
            )
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(frame["result"], {"ok": True, "results": []})
        self.assertEqual(received[1]["method"], "approval/respond")
        self.assertTrue(received[1]["params"]["approved"])

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
