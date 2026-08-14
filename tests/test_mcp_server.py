import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cterm.mcp.utils.cancellation import is_tool_cancelled
from cterm.mcp.server import MCPServer
from cterm.mcp.tools import bash, exec_python


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


class _CaptureWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        return None


class MCPServerTests(unittest.TestCase):
    @staticmethod
    async def _wait_thread_event(event, timeout=2):
        deadline = time.monotonic() + timeout
        while not event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return event.is_set()

    @staticmethod
    async def _start_tool_call(server, tools, name, arguments, request_id=1):
        reader = _DisconnectReader()
        writer = _CaptureWriter()
        task = asyncio.create_task(server._run_tool_call(
            getattr(tools, name), arguments, request_id, writer, reader,
        ))
        return task, reader, writer

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


if __name__ == "__main__":
    unittest.main()
