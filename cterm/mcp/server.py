#!/usr/bin/env python3
"""
cterm_server - Persistent, self-terminating MCP tool server.
This server runs in the background, bound to a Unix Domain Socket (UDS),
and shuts down after a period of inactivity.
"""
import logging
import os
import time
import threading
import asyncio
import socket
import json
from pathlib import Path
import pwd
import queue
import types

from cterm.mcp.vars import INACTIVITY_TIMEOUT_SECONDS
from cterm.mcp.utils.cancellation import bind_tool_cancellation

logger = logging.getLogger(__name__)

def get_socket_path() -> Path:
    """Returns the UDS path based on the current user (using UID for robustness)."""
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        username = os.environ.get('USER', 'default')
    return Path(f"/tmp/cterm_mcp_{username}.sock")

class MCPServer:
    """Wrapper to handle MCP protocol over UDS"""
    def __init__(self, mcp_instance):
        self.mcp = mcp_instance
        self.last_activity = time.time()
        
    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = time.time()
    
    async def _wait_for_disconnect(self, reader: asyncio.StreamReader):
        """Wait until the client half-closes or closes its connection."""
        while True:
            data = await reader.read(4096)
            if not data:
                return

    @staticmethod
    def _start_tool_worker(tool_func, arguments, events, cancel_event):
        def run():
            result = None
            try:
                with bind_tool_cancellation(cancel_event):
                    result = tool_func(**arguments)
                    if isinstance(result, types.GeneratorType):
                        try:
                            for chunk in result:
                                events.put(("chunk", chunk))
                        finally:
                            result.close()
                    else:
                        events.put(("result", result))
            except BaseException as exc:
                events.put(("error", exc))

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker

    async def _cancel_tool_worker(self, worker, cancel_event):
        cancel_event.set()
        # Do not block the event loop while a non-cooperative Python tool
        # finishes. Subprocess-backed tools normally exit within this window.
        deadline = time.monotonic() + 2.0
        while worker.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    async def _run_tool_call(
        self, tool_func, arguments, request_id, writer, reader=None,
    ):
        """Run one tool off-loop and cancel it when its client disconnects."""
        events = queue.Queue()
        cancel_event = threading.Event()
        worker = self._start_tool_worker(tool_func, arguments, events, cancel_event)
        worker_finished = False
        disconnect_task = (
            asyncio.create_task(self._wait_for_disconnect(reader))
            if reader is not None else None
        )

        def send(obj: dict):
            writer.write((json.dumps(obj) + "\n").encode("utf-8"))

        try:
            while True:
                if disconnect_task is not None and disconnect_task.done():
                    await self._cancel_tool_worker(worker, cancel_event)
                    worker_finished = True
                    return False

                try:
                    event_type, value = events.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue

                if event_type == "error":
                    if isinstance(value, (BrokenPipeError, ConnectionResetError)):
                        raise value
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": f"Tool execution error: {value}"},
                    })
                    worker_finished = True
                    return True

                if event_type == "chunk":
                    chunk = value
                    if chunk.get("type") == "stream":
                        send({
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "stream": {
                                "fd": chunk["fd"],
                                "line": chunk["line"],
                                "end": chunk.get("end", "\n"),
                            },
                        })
                        await writer.drain()
                    elif chunk.get("type") == "result":
                        payload = {k: v for k, v in chunk.items() if k != "type"}
                        send({"jsonrpc": "2.0", "id": request_id, "result": payload})
                        worker_finished = True
                        return True
                    continue

                send({"jsonrpc": "2.0", "id": request_id, "result": value})
                worker_finished = True
                return True
        except (BrokenPipeError, ConnectionResetError):
            await self._cancel_tool_worker(worker, cancel_event)
            worker_finished = True
            raise
        finally:
            if not worker_finished and worker.is_alive():
                await self._cancel_tool_worker(worker, cancel_event)
            if disconnect_task is not None:
                disconnect_task.cancel()
                try:
                    await disconnect_task
                except asyncio.CancelledError:
                    pass

    async def handle_request(
        self, request_data: str, writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader | None = None,
    ):
        """
        Handle an MCP request and write response(s) to writer.

        For streaming tool results the server sends multiple newline-delimited
        JSON frames before the final result frame:
          {"jsonrpc":"2.0","id":N,"stream":{"fd":"stdout"|"stderr","line":"..."}}
          {"jsonrpc":"2.0","id":N,"result": <final result dict>}

        Non-streaming tools send a single result frame as before.
        """
        self.update_activity()

        def send(obj: dict):
            """Serialise obj and write it as a newline-terminated frame."""
            writer.write((json.dumps(obj) + "\n").encode("utf-8"))

        try:
            request = json.loads(request_data)
            method = request.get("method", "")
            params = request.get("params", {})
            request_id = request.get("id", 1)

            if method == "tools/list":
                import inspect
                tools = []
                for tool_name in dir(self.mcp):
                    if not tool_name.startswith('_'):
                        tool_func = getattr(self.mcp, tool_name, None)
                        if callable(tool_func) and hasattr(tool_func, '__mcp_tool__'):
                            sig = inspect.signature(tool_func)
                            properties = {}
                            required = []
                            for pname, param in sig.parameters.items():
                                if pname in ('kwargs', 'args'):
                                    continue
                                prop = {"type": "string"}
                                if param.annotation is not inspect.Parameter.empty:
                                    if param.annotation is int:
                                        prop["type"] = "integer"
                                    elif param.annotation is bool:
                                        prop["type"] = "boolean"
                                    elif param.annotation is list or (
                                        hasattr(param.annotation, '__origin__')
                                        and param.annotation.__origin__ is list
                                    ):
                                        prop["type"] = "array"
                                        inner = getattr(param.annotation, '__args__', None)
                                        if inner and len(inner) == 1 and inner[0] is str:
                                            prop["items"] = {"type": "string"}
                                if param.default is inspect.Parameter.empty:
                                    required.append(pname)
                                properties[pname] = prop
                            tools.append({
                                "name": tool_name,
                                "description": tool_func.__doc__ or f"Tool: {tool_name}",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": properties,
                                    "required": required,
                                }
                            })
                send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": tools},
                })

            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})

                if not hasattr(self.mcp, tool_name):
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
                    })
                    return

                tool_func = getattr(self.mcp, tool_name)
                await self._run_tool_call(
                    tool_func, arguments, request_id, writer, reader,
                )

            else:
                send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })

        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "Parse error"}})
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32603, "message": f"Internal error: {str(e)}"}})


def run_server():
    """Starts the MCP server on UDS with inactivity timeout."""
    from cterm.app_home import resolve_app_home
    from cterm.mcp.config import init_config

    resolve_app_home()
    init_config()
    from cterm.mcp.tools import mcp

    print("Starting cterm MCP tool server...")
    
    mcp_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(mcp_loop)
    
    socket_path = get_socket_path()
    mcp_server = MCPServer(mcp)
    
    # 1. Cleanup old socket file if present
    if socket_path.exists():
        try:
            os.unlink(socket_path)
        except OSError as e:
            if e.errno != 13:  # errno 13 is Permission denied
                logger.error("Error unlinking old socket %s: %s", socket_path, e)
    
    print(f"Server starting on UDS: {socket_path}")
    
    # 2. Inactivity Check Thread
    def check_for_timeout():
        while True:
            time.sleep(30)  # Check every 30 seconds
            if time.time() - mcp_server.last_activity > INACTIVITY_TIMEOUT_SECONDS:
                logger.info("Inactivity timeout reached. Shutting down server.")
                mcp_loop.call_soon_threadsafe(mcp_loop.stop)
                break
    
    threading.Thread(target=check_for_timeout, daemon=True).start()
    
    # 3. Setup UDS Server
    server = None
    
    try:
        # Create UDS socket
        server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_socket.bind(str(socket_path))
        server_socket.listen(5)
        
        # Make socket world-readable (but not world-writable)
        os.chmod(socket_path, 0o666)
        
        logger.debug("Socket created at %s", socket_path)

        # Handle client connections
        async def handle_client(reader, writer):
            try:
                # Read the request (up to 64KB)
                data = await reader.read(65536)
                if not data:
                    writer.close()
                    await writer.wait_closed()
                    return
                
                request_text = data.decode('utf-8').strip()
                logger.debug("Received request: %s...", request_text[:100])

                # Process the request, streaming frames directly to writer
                await mcp_server.handle_request(request_text, writer, reader)

                # Final drain to flush any buffered bytes
                await writer.drain()
                logger.debug("Response(s) sent.")

            except (BrokenPipeError, ConnectionResetError) as exc:
                logger.debug("MCP client disconnected while sending response: %s", exc)
            except Exception as e:
                logger.exception("Error handling client: %s", e)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        
        # Start the asyncio server
        logger.debug("Starting asyncio server...")
        server = mcp_loop.run_until_complete(
            asyncio.start_server(handle_client, sock=server_socket)
        )
        
        print(f"✓ MCP server listening on {socket_path}")
        
        # List registered tools
        tool_count = 0
        for name in dir(mcp):
            if not name.startswith('_') and callable(getattr(mcp, name, None)):
                tool_count += 1
        print(f"✓ Registered {tool_count} tools")
        
        # Run event loop
        mcp_loop.run_forever()
        
        logger.debug("Event loop stopped gracefully.")
        
    except SystemExit:
        logger.debug("Caught SystemExit.")
    except Exception as e:
        logger.exception("Server loop failed: %s", e)
    finally:
        # Cleanup
        if server:
            server.close()
            mcp_loop.run_until_complete(server.wait_closed())
        
        if socket_path.exists():
            logger.debug("Unlinking socket file: %s", socket_path)
            os.unlink(socket_path)
        
        mcp_loop.close()
        logger.info("Server process exiting.")

if __name__ == "__main__":
    run_server()
