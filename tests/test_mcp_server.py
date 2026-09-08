import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from golim.toolset.utils.cancellation import is_tool_cancelled
from golim.toolset import auth_session
from golim.toolset.server import MCPServer
from golim.toolset.tools import bash, exec_python


class _Writer:
    def write(self, _data):
        pass

    async def drain(self):
        raise BrokenPipeError("client disconnected")


class _Tools:
    def __init__(self):
        self.closed = False

    def streamed(self):
        try:
            yield {"type": "stream", "fd": "stdout", "line": "output"}
            yield {"type": "result", "ok": True}
        finally:
            self.closed = True


class _CancellationTools:
    def __init__(self):
        self.cooperative_started = threading.Event()
        self.cooperative_cancelled = threading.Event()
        self.noncooperative_started = threading.Event()
        self.noncooperative_finished = threading.Event()
        self.release_noncooperative = threading.Event()

    def cooperative(self):
        self.cooperative_started.set()
        while not is_tool_cancelled():
            time.sleep(0.01)
        self.cooperative_cancelled.set()
        return {"ok": False, "cancelled": True}

    def noncooperative(self):
        self.noncooperative_started.set()
        self.release_noncooperative.wait(10)
        self.noncooperative_finished.set()
        return {"ok": True}

    def fast(self, value="ok"):
        return {"ok": True, "value": value}


class _DisconnectReader:
    def __init__(self):
        self.closed = asyncio.Event()

    async def read(self, _size):
        await self.closed.wait()
        return b""

    def close(self):
        self.closed.set()


class _ApprovalTools:
    def __init__(self):
        self.decisions = []

    def gated(self, _approve_privileged=None, **_kwargs):
        decision = _approve_privileged({
            "approval_kind": "privileged_whitelist",
            "binary": "/usr/bin/apt",
            "command": "sudo apt update",
        })
        self.decisions.append(decision)
        return {"ok": decision}


class _AuthTools:
    def __init__(self):
        self.results = []

    def secured(self, _session_token=None, **_kwargs):
        token = _session_token() if callable(_session_token) else _session_token
        self.results.append(token)
        return {"ok": bool(token)}


class _GatedBashTools:
    def __init__(self):
        self.decisions = []

    def bash(self, _approve_privileged=None, **_kwargs):
        decision = _approve_privileged({
            "approval_kind": "privileged_whitelist",
            "binary": "/usr/bin/apt",
            "command": "sudo apt update",
        })
        self.decisions.append(decision)
        return {"ok": decision}


class _CaptureWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        return None


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        # The sudo password cache is process-global; keep tests independent.
        auth_session.clear_session_token()

    @staticmethod
    async def _wait_thread_event(event, timeout=2):
        deadline = time.monotonic() + timeout
        while not event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return event.is_set()

    @staticmethod
    async def _start_tool_call(server, tools, name, arguments, request_id=1, inject_approval=False):
        reader = _DisconnectReader()
        writer = _CaptureWriter()
        task = asyncio.create_task(server._run_tool_call(
            getattr(tools, name), arguments, request_id, writer, reader,
            inject_approval=inject_approval,
        ))
        return task, reader, writer

    @staticmethod
    async def _wait_for_frames(writer, count=1, timeout=2):
        deadline = time.monotonic() + timeout
        while len(writer.frames) < count and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return len(writer.frames) >= count

    @staticmethod
    def _approval_respond_request(approval_id, approved):
        return json.dumps({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "approval/respond",
            "params": {"approval_id": approval_id, "approved": approved},
        })

    @staticmethod
    def _auth_respond_request(auth_id, password):
        return json.dumps({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "auth/respond",
            "params": {"auth_id": auth_id, "password": password},
        })

    def test_broken_client_closes_streaming_generator(self):
        tools = _Tools()
        server = MCPServer(tools)
        writer = _Writer()

        with self.assertRaises(BrokenPipeError):
            asyncio.run(server.handle_request(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "streamed", "arguments": {}},
                }),
                writer,
            ))

        self.assertTrue(tools.closed)

    def test_stream_chunks_are_sent_as_progress_notifications(self):
        tools = _Tools()
        server = MCPServer(tools)

        async def run():
            task, reader, writer = await self._start_tool_call(
                server, tools, "streamed", {},
            )
            self.assertTrue(await asyncio.wait_for(task, 2))
            reader.close()
            return writer

        writer = asyncio.run(run())
        frames = [json.loads(data.decode()) for data in writer.frames]
        progress = [f for f in frames if f.get("method") == "tools/progress"]
        self.assertEqual(progress, [{
            "jsonrpc": "2.0",
            "method": "tools/progress",
            "params": {"token": 1, "fd": "stdout", "line": "output", "end": "\n"},
        }])
        self.assertEqual(frames[-1]["id"], 1)
        self.assertEqual(frames[-1]["result"], {"ok": True})

    def test_connection_close_cancels_cooperative_python_tool(self):
        async def run():
            tools = _CancellationTools()
            server = MCPServer(tools)
            task, reader, _ = await self._start_tool_call(server, tools, "cooperative", {})
            self.assertTrue(await self._wait_thread_event(tools.cooperative_started, 1))
            reader.close()
            self.assertFalse(await asyncio.wait_for(task, 2))
            self.assertTrue(tools.cooperative_cancelled.is_set())

        asyncio.run(run())

    def test_noncooperative_tool_is_not_killed_but_other_client_continues(self):
        async def run():
            tools = _CancellationTools()
            server = MCPServer(tools)
            try:
                first_task, first_reader, _ = await self._start_tool_call(
                    server, tools, "noncooperative", {}, 1,
                )
                self.assertTrue(await self._wait_thread_event(tools.noncooperative_started, 1))

                second_task, second_reader, second_writer = await self._start_tool_call(
                    server, tools, "fast", {"value": "other-client"}, 2,
                )
                self.assertTrue(await second_task)
                response = json.loads(second_writer.frames[0].decode())
                self.assertEqual(response["result"]["value"], "other-client")
                second_reader.close()

                first_reader.close()
                self.assertFalse(await asyncio.wait_for(first_task, 3))
                self.assertFalse(tools.noncooperative_finished.is_set())

                tools.release_noncooperative.set()
                self.assertTrue(await self._wait_thread_event(tools.noncooperative_finished, 2))
                self.assertTrue(tools.noncooperative_finished.is_set())
            finally:
                tools.release_noncooperative.set()

        asyncio.run(run())

    def test_clients_have_independent_cancellation_events(self):
        async def run():
            first_tools = _CancellationTools()
            second_tools = _CancellationTools()
            server = MCPServer(first_tools)
            first_task, first_reader, _ = await self._start_tool_call(
                server, first_tools, "cooperative", {}, 1,
            )
            self.assertTrue(await self._wait_thread_event(first_tools.cooperative_started, 1))

            second_task, second_reader, _ = await self._start_tool_call(
                server, second_tools, "cooperative", {}, 2,
            )
            self.assertTrue(await self._wait_thread_event(second_tools.cooperative_started, 1))

            first_reader.close()
            self.assertFalse(await asyncio.wait_for(first_task, 2))
            self.assertTrue(first_tools.cooperative_cancelled.is_set())
            self.assertFalse(second_tools.cooperative_cancelled.is_set())

            second_reader.close()
            self.assertFalse(await asyncio.wait_for(second_task, 2))
            self.assertTrue(second_tools.cooperative_cancelled.is_set())

        asyncio.run(run())

    def test_connection_close_terminates_exec_subprocess(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                started = Path(tmp) / "started"
                finished = Path(tmp) / "finished"
                code = (
                    "from pathlib import Path; import time; "
                    f"Path({str(started)!r}).touch(); time.sleep(10); "
                    f"Path({str(finished)!r}).touch()"
                )
                tools = type("Tools", (), {"exec": staticmethod(exec_python)})()
                server = MCPServer(tools)
                task, reader, _ = await self._start_tool_call(
                    server, tools, "exec", {"code": code, "timeout": 30},
                )
                for _ in range(100):
                    if started.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.exists())
                reader.close()
                self.assertFalse(await asyncio.wait_for(task, 2))
                await asyncio.sleep(0.1)
                self.assertFalse(finished.exists())

        asyncio.run(run())

    def test_connection_close_terminates_streamed_bash_subprocess(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                started = Path(tmp) / "started"
                finished = Path(tmp) / "finished"
                command = f"touch {started} && sleep 10 && touch {finished}"
                tools = type("Tools", (), {"bash": staticmethod(bash)})()
                server = MCPServer(tools)
                task, reader, _ = await self._start_tool_call(
                    server, tools, "bash", {"command": command, "stream": True},
                )
                for _ in range(100):
                    if started.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.exists())
                reader.close()
                self.assertFalse(await asyncio.wait_for(task, 2))
                await asyncio.sleep(0.1)
                self.assertFalse(finished.exists())

        asyncio.run(run())

    def test_approval_request_frame_is_sent_and_respond_resolves_it(self):
        async def run():
            tools = _ApprovalTools()
            server = MCPServer(tools)
            task, reader, writer = await self._start_tool_call(
                server, tools, "gated", {}, 1, inject_approval=True,
            )
            self.assertTrue(await self._wait_for_frames(writer))
            frame = json.loads(writer.frames[0].decode())
            self.assertEqual(frame["id"], 1)
            self.assertEqual(frame["approval_request"]["binary"], "/usr/bin/apt")
            self.assertEqual(frame["approval_request"]["command"], "sudo apt update")
            approval_id = frame["approval_request"]["approval_id"]

            respond_writer = _CaptureWriter()
            await server.handle_request(
                self._approval_respond_request(approval_id, True),
                respond_writer,
            )
            self.assertTrue(await asyncio.wait_for(task, 2))
            self.assertTrue(tools.decisions[0])
            result_frame = json.loads(writer.frames[-1].decode())
            self.assertEqual(result_frame["result"], {"ok": True})
            self.assertEqual(server._pending_approvals, {})
            reader.close()

        asyncio.run(run())

    def test_approval_respond_whitelists_binary(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                whitelist = Path(tmp) / "privileged_whitelist"
                with patch.dict(os.environ, {"GOLIM_PRIVILEGED_WHITELIST": str(whitelist)}):
                    tools = _ApprovalTools()
                    server = MCPServer(tools)
                    task, reader, writer = await self._start_tool_call(
                        server, tools, "gated", {}, 1, inject_approval=True,
                    )
                    self.assertTrue(await self._wait_for_frames(writer))
                    approval_id = json.loads(writer.frames[0].decode())["approval_request"]["approval_id"]

                    await server.handle_request(
                        self._approval_respond_request(approval_id, True),
                        _CaptureWriter(),
                    )
                    self.assertTrue(await asyncio.wait_for(task, 2))
                    reader.close()

                lines = whitelist.read_text(encoding="utf-8").splitlines()
                self.assertIn("/usr/bin/apt", lines)

        asyncio.run(run())

    def test_approval_respond_denial_does_not_whitelist_binary(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                whitelist = Path(tmp) / "privileged_whitelist"
                with patch.dict(os.environ, {"GOLIM_PRIVILEGED_WHITELIST": str(whitelist)}):
                    tools = _ApprovalTools()
                    server = MCPServer(tools)
                    task, reader, writer = await self._start_tool_call(
                        server, tools, "gated", {}, 1, inject_approval=True,
                    )
                    self.assertTrue(await self._wait_for_frames(writer))
                    approval_id = json.loads(writer.frames[0].decode())["approval_request"]["approval_id"]

                    await server.handle_request(
                        self._approval_respond_request(approval_id, False),
                        _CaptureWriter(),
                    )
                    self.assertTrue(await asyncio.wait_for(task, 2))
                    self.assertFalse(tools.decisions[0])
                    reader.close()

                self.assertFalse(whitelist.exists())

        asyncio.run(run())

    def test_disconnect_denies_pending_approval(self):
        async def run():
            tools = _ApprovalTools()
            server = MCPServer(tools)
            task, reader, writer = await self._start_tool_call(
                server, tools, "gated", {}, 1, inject_approval=True,
            )
            self.assertTrue(await self._wait_for_frames(writer))
            reader.close()
            self.assertFalse(await asyncio.wait_for(task, 2))
            self.assertFalse(tools.decisions[0])
            self.assertEqual(server._pending_approvals, {})

        asyncio.run(run())

    def test_auth_request_frame_is_sent_and_respond_resolves_it(self):
        async def run():
            tools = _AuthTools()
            server = MCPServer(tools)
            task, reader, writer = await self._start_tool_call(
                server, tools, "secured", {}, 1, inject_approval=True,
            )
            self.assertTrue(await self._wait_for_frames(writer))
            frame = json.loads(writer.frames[0].decode())
            self.assertEqual(frame["id"], 1)
            self.assertEqual(frame["auth_request"]["kind"], "sudo_password")
            auth_id = frame["auth_request"]["auth_id"]

            respond_writer = _CaptureWriter()
            with patch("golim.toolset.server.auth_session.register_session", return_value="tok") as register:
                await server.handle_request(
                    self._auth_respond_request(auth_id, "sekret"),
                    respond_writer,
                )
                self.assertTrue(await asyncio.wait_for(task, 2))
            register.assert_called_once_with("sekret")
            self.assertEqual(tools.results[0], "tok")
            result_frame = json.loads(writer.frames[-1].decode())
            self.assertEqual(result_frame["result"], {"ok": True})
            self.assertEqual(server._pending_auths, {})
            reader.close()

        asyncio.run(run())

    def test_auth_respond_password_never_appears_in_written_frames(self):
        async def run():
            tools = _AuthTools()
            server = MCPServer(tools)
            task, reader, writer = await self._start_tool_call(
                server, tools, "secured", {}, 1, inject_approval=True,
            )
            self.assertTrue(await self._wait_for_frames(writer))
            auth_id = json.loads(writer.frames[0].decode())["auth_request"]["auth_id"]

            with patch("golim.toolset.server.auth_session.register_session", return_value="tok"):
                await server.handle_request(
                    self._auth_respond_request(auth_id, "sekret"),
                    _CaptureWriter(),
                )
                self.assertTrue(await asyncio.wait_for(task, 2))
            reader.close()
            raw = b"".join(writer.frames).decode()
            self.assertNotIn("sekret", raw)

        asyncio.run(run())

    def test_auth_respond_invalid_password_fails_the_tool(self):
        async def run():
            tools = _AuthTools()
            server = MCPServer(tools)
            task, reader, writer = await self._start_tool_call(
                server, tools, "secured", {}, 1, inject_approval=True,
            )
            self.assertTrue(await self._wait_for_frames(writer))
            auth_id = json.loads(writer.frames[0].decode())["auth_request"]["auth_id"]

            with patch("golim.toolset.server.auth_session.register_session", return_value=None):
                await server.handle_request(
                    self._auth_respond_request(auth_id, "wrong"),
                    _CaptureWriter(),
                )
                self.assertTrue(await asyncio.wait_for(task, 2))
            self.assertIsNone(tools.results[0])
            result_frame = json.loads(writer.frames[-1].decode())
            self.assertFalse(result_frame["result"]["ok"])
            reader.close()

        asyncio.run(run())

    def test_disconnect_releases_pending_auth_without_password(self):
        async def run():
            tools = _AuthTools()
            server = MCPServer(tools)
            task, reader, writer = await self._start_tool_call(
                server, tools, "secured", {}, 1, inject_approval=True,
            )
            self.assertTrue(await self._wait_for_frames(writer))
            reader.close()
            self.assertFalse(await asyncio.wait_for(task, 2))
            self.assertIsNone(tools.results[0])
            self.assertEqual(server._pending_auths, {})

        asyncio.run(run())

    def test_notification_tool_call_auto_denies_auth_request(self):
        async def run():
            tools = _AuthTools()
            server = MCPServer(tools)
            writer = _CaptureWriter()
            task = asyncio.create_task(server._run_tool_call(
                getattr(tools, "secured"), {}, None, writer, reader=None,
                inject_approval=True, emit=False,
            ))
            self.assertTrue(await asyncio.wait_for(task, 2))
            self.assertIsNone(tools.results[0])
            self.assertEqual(writer.frames, [])
            self.assertEqual(server._pending_auths, {})

        asyncio.run(run())

    def test_auth_status_reports_wrapper_and_ticket_state(self):
        async def run():
            server = MCPServer(_Tools())
            writer = _CaptureWriter()
            with patch("golim.toolset.server.auth_session.wrapper_installed", return_value=True), \
                 patch("golim.toolset.server.auth_session.authd_available", return_value=True), \
                 patch("golim.toolset.server.auth_session.has_valid_session_token", return_value=False):
                await server.handle_request(
                    json.dumps({"jsonrpc": "2.0", "id": 3, "method": "auth/status"}),
                    writer,
                )
            frame = json.loads(writer.frames[0].decode())
            self.assertEqual(frame["result"], {
                "ok": True,
                "wrapper_installed": True,
                "authd_available": True,
                "authenticated": False,
            })

        asyncio.run(run())

    def test_auth_sudo_password_method_validates_once(self):
        async def run():
            server = MCPServer(_Tools())
            writer = _CaptureWriter()
            with patch("golim.toolset.server.auth_session.register_session", return_value="tok"):
                await server.handle_request(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "auth/sudo_password",
                        "params": {"password": "sekret"},
                    }),
                    writer,
                )
            frame = json.loads(writer.frames[0].decode())
            self.assertEqual(frame["result"], {"ok": True})
            self.assertTrue(auth_session.has_session_token())
            auth_session.clear_session_token()

            denied_writer = _CaptureWriter()
            with patch("golim.toolset.server.auth_session.register_session", return_value=None):
                await server.handle_request(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "auth/sudo_password",
                        "params": {"password": "bad"},
                    }),
                    denied_writer,
                )
            denied = json.loads(denied_writer.frames[0].decode())
            self.assertFalse(denied["result"]["ok"])

        asyncio.run(run())

    def test_redaction_scrubs_password_fields_from_raw_frames(self):
        from golim.toolset.server import _redact_secrets

        raw = '{"jsonrpc":"2.0","id":1,"method":"auth/respond","params":{"auth_id":"1:auth:0","password":"sekret"}}'
        redacted = _redact_secrets(raw)
        self.assertNotIn("sekret", redacted)
        self.assertIn('"password":"***"', redacted)
        self.assertIn('"auth_id":"1:auth:0"', redacted)

    def test_redaction_handles_escaped_and_long_passwords(self):
        from golim.toolset.server import _redact_secrets

        password = 'a"' + ("secret" * 30)
        raw = json.dumps({"password": password})
        redacted = _redact_secrets(raw)
        self.assertNotIn(password, redacted)
        self.assertNotIn("secret", redacted)
        self.assertIn('"password": "***"', redacted)

    def test_unknown_approval_respond_reports_unresolved(self):
        from golim.toolset.tools import mcp
        server = MCPServer(mcp)
        writer = _CaptureWriter()

        asyncio.run(server.handle_request(
            self._approval_respond_request("missing:0", True),
            writer,
        ))

        frame = json.loads(writer.frames[0].decode())
        self.assertEqual(frame["result"], {"resolved": False})

    def test_notification_tool_call_completes_without_any_frames(self):
        tools = _Tools()
        server = MCPServer(tools)
        reader = _DisconnectReader()
        writer = _CaptureWriter()

        async def run():
            await server.handle_request(
                json.dumps({
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": "streamed", "arguments": {}},
                }),
                writer,
                reader,
            )

        asyncio.run(run())
        reader.close()

        self.assertTrue(tools.closed)
        self.assertEqual(writer.frames, [])

    def test_notifications_are_processed_but_never_answered(self):
        from golim.toolset.tools import mcp
        server = MCPServer(mcp)
        writer = _CaptureWriter()

        asyncio.run(server.handle_request(
            json.dumps({"jsonrpc": "2.0", "method": "tools/list"}),
            writer,
        ))
        asyncio.run(server.handle_request(
            json.dumps({"jsonrpc": "2.0", "method": "ping"}),
            writer,
        ))

        self.assertEqual(writer.frames, [])

    def test_requests_still_receive_their_response(self):
        from golim.toolset.tools import mcp
        server = MCPServer(mcp)
        writer = _CaptureWriter()

        asyncio.run(server.handle_request(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            writer,
        ))

        frame = json.loads(writer.frames[0].decode())
        self.assertEqual(frame["id"], 1)
        self.assertEqual(frame["error"]["code"], -32601)

    def test_notification_bash_call_auto_denies_privileged_approval(self):
        tools = _GatedBashTools()
        server = MCPServer(tools)
        writer = _CaptureWriter()

        asyncio.run(server.handle_request(
            json.dumps({
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "bash", "arguments": {"command": "sudo apt update"}},
            }),
            writer,
        ))

        self.assertEqual(tools.decisions, [False])
        self.assertEqual(writer.frames, [])
        self.assertEqual(server._pending_approvals, {})

    def test_tools_list_hides_approval_parameters_from_model_schema(self):
        from golim.toolset.tools import mcp
        server = MCPServer(mcp)
        writer = _CaptureWriter()

        asyncio.run(server.handle_request(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            writer,
        ))

        frame = json.loads(writer.frames[0].decode())
        bash_schema = next(
            tool["inputSchema"] for tool in frame["result"]["tools"]
            if tool["name"] == "bash"
        )
        self.assertNotIn("allow_privileged", bash_schema["properties"])
        self.assertNotIn("_approve_privileged", bash_schema["properties"])
        self.assertNotIn("allow_privileged", bash_schema["required"])
        self.assertNotIn("_approve_privileged", bash_schema["required"])


if __name__ == "__main__":
    unittest.main()
