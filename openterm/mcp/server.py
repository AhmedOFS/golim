#!/usr/bin/env python3
"""
openterm_server - Persistent, self-terminating MCP tool server.
This server runs in the background, bound to a Unix Domain Socket (UDS),
and shuts down after a period of inactivity.
"""
import logging
import os
import re
import time
import threading
import asyncio
import socket
import json
import itertools
from pathlib import Path
import pwd
import queue
import types

from openterm.mcp.vars import INACTIVITY_TIMEOUT_SECONDS
from openterm.mcp.config import get_config
from openterm.mcp import sudo_auth
from openterm.mcp.utils.cancellation import bind_tool_cancellation

logger = logging.getLogger(__name__)

_SECRET_REDACTION_RE = re.compile(
    r'("(?:password|token)"\s*:\s*)"(?:\\.|[^"\\])*"'
)


def _redact_secrets(text: str) -> str:
    """Scrub secret fields before raw protocol text reaches any log."""
    return _SECRET_REDACTION_RE.sub(r'\1"***"', text)


class _PendingApproval:
    """Approval state held while a tool call waits for the client's answer."""

    def __init__(self, info):
        self.info = info
        self.event = threading.Event()
        self.approved = False


class _PendingAuth:
    """Sudo-authentication state held while a tool call waits for a password."""

    def __init__(self):
        self.event = threading.Event()
        self.password: str | None = None

def get_socket_path() -> Path:
    """Returns the UDS path based on the current user (using UID for robustness)."""
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        username = os.environ.get('USER', 'default')
    return Path(f"/tmp/openterm_mcp_{username}.sock")

class MCPServer:
    """Wrapper to handle MCP protocol over UDS"""
    def __init__(self, mcp_instance):
        self.mcp = mcp_instance
        self.last_activity = time.time()
        self._approvals_lock = threading.Lock()
        self._pending_approvals = {}
        self._pending_auths = {}
        self._approval_seq = itertools.count()

    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = time.time()

    def _resolve_approval(self, approval_id, approved):
        """Resolve a pending approval; whitelist its command binaries."""
        with self._approvals_lock:
            pending = self._pending_approvals.pop(approval_id, None)
        if pending is None:
            return False
        binaries = pending.info.get("binaries") or [pending.info.get("binary")]
        if approved:
            for binary in binaries:
                if binary:
                    get_config().add_privileged_binary(binary)
        pending.approved = bool(approved)
        pending.event.set()
        return True

    def _resolve_auth(self, auth_id, password):
        """Resolve a pending sudo-auth request with a client password."""
        with self._approvals_lock:
            pending = self._pending_auths.pop(auth_id, None)
        if pending is None:
            return False
        pending.password = password
        pending.event.set()
        return True
    
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
        inject_approval=False, emit=True,
    ):
        """Run one tool off-loop and cancel it when its client disconnects.

        ``emit=False`` marks a JSON-RPC notification call: the tool still
        runs to completion, but no frames are written and privileged
        approvals and sudo-auth requests auto-deny because no client is
        awaiting an answer.
        """
        events = queue.Queue()
        cancel_event = threading.Event()
        local_approvals = []
        local_auths = []

        def request_approval(info):
            """Block the tool worker until the client answers the approval."""
            if not emit:
                return False
            with self._approvals_lock:
                seq = next(self._approval_seq)
                approval_id = f"{request_id}:{seq}"
                pending = _PendingApproval(info)
                self._pending_approvals[approval_id] = pending
            local_approvals.append(approval_id)
            events.put(("approval", approval_id, info))
            pending.event.wait()
            return pending.approved

        def request_sudo_password():
            """Block the tool worker until the client returns a password.

            The password is validated once against sudo, held in memory for
            the session, and never returned to the model.
            """
            if not emit:
                return None
            with self._approvals_lock:
                seq = next(self._approval_seq)
                auth_id = f"{request_id}:auth:{seq}"
                pending = _PendingAuth()
                self._pending_auths[auth_id] = pending
            local_auths.append(auth_id)
            events.put(("auth", auth_id))
            pending.event.wait()
            return pending.password

        def obtain_session_token():
            """Return the broker session token, registering if needed.

            A cached token is reused for every wrapper invocation;
            otherwise one password is collected out-of-band, registered
            with the broker (which verifies it via sudo/PAM), and the
            returned token is held in memory for the session.
            """
            if sudo_auth.has_session_token() and sudo_auth.has_valid_session_token():
                return sudo_auth.session_token()
            sudo_auth.clear_session_token()
            password = request_sudo_password()
            if not password:
                return None
            token = sudo_auth.register_session(password)
            if not token:
                return None
            sudo_auth.set_session_token(token)
            return token

        call_arguments = dict(arguments)
        if inject_approval:
            call_arguments["_approve_privileged"] = request_approval
            call_arguments["_session_token"] = obtain_session_token
        worker = self._start_tool_worker(tool_func, call_arguments, events, cancel_event)
        worker_finished = False
        disconnect_task = (
            asyncio.create_task(self._wait_for_disconnect(reader))
            if reader is not None else None
        )

        def release_pending_approvals():
            for approval_id in local_approvals:
                self._resolve_approval(approval_id, False)
            for auth_id in local_auths:
                self._resolve_auth(auth_id, None)

        def send(obj: dict):
            writer.write((json.dumps(obj) + "\n").encode("utf-8"))

        try:
            while True:
                if disconnect_task is not None and disconnect_task.done():
                    release_pending_approvals()
                    await self._cancel_tool_worker(worker, cancel_event)
                    worker_finished = True
                    return False

                self.update_activity()

                try:
                    event = events.get_nowait()
                    event_type = event[0]
                    value = event[1] if len(event) == 2 else event[1:]
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue

                if event_type == "approval":
                    approval_id, info = value
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "approval_request": {
                            "approval_id": approval_id,
                            "approval_kind": info.get("approval_kind", "privileged_whitelist"),
                            "binary": info.get("binary", ""),
                            "binaries": info.get("binaries", []),
                            "command": info.get("command", ""),
                        },
                    })
                    await writer.drain()
                    continue

                if event_type == "auth":
                    auth_id = value
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "auth_request": {
                            "auth_id": auth_id,
                            "kind": "sudo_password",
                        },
                    })
                    await writer.drain()
                    continue

                if event_type == "error":
                    if isinstance(value, (BrokenPipeError, ConnectionResetError)):
                        raise value
                    if emit:
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
                        if emit:
                            send({
                                "jsonrpc": "2.0",
                                "method": "tools/progress",
                                "params": {
                                    "token": request_id,
                                    "fd": chunk["fd"],
                                    "line": chunk["line"],
                                    "end": chunk.get("end", "\n"),
                                },
                            })
                            await writer.drain()
                    elif chunk.get("type") == "result":
                        payload = {k: v for k, v in chunk.items() if k != "type"}
                        if emit:
                            send({"jsonrpc": "2.0", "id": request_id, "result": payload})
                        worker_finished = True
                        return True
                    continue

                if emit:
                    send({"jsonrpc": "2.0", "id": request_id, "result": value})
                worker_finished = True
                return True
        except (BrokenPipeError, ConnectionResetError):
            release_pending_approvals()
            await self._cancel_tool_worker(worker, cancel_event)
            worker_finished = True
            raise
        finally:
            if not worker_finished and worker.is_alive():
                release_pending_approvals()
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
        JSON frames before the final result frame. Progress frames are valid
        JSON-RPC 2.0 notifications correlated by params.token; only the final
        frame carries the request id:
          {"jsonrpc":"2.0","method":"tools/progress","params":{"token":N,"fd":"stdout"|"stderr","line":"..."}}
          {"jsonrpc":"2.0","id":N,"result": <final result dict>}

        A held bash tool call can also emit an approval frame:
          {"jsonrpc":"2.0","id":N,"approval_request":{"approval_id":...,"binary":...}}
        resolved by a separate `approval/respond` request, or a sudo-auth
        frame:
          {"jsonrpc":"2.0","id":N,"auth_request":{"auth_id":...,"kind":"sudo_password"}}
        resolved by a separate `auth/respond` request carrying the password.
        Approval and auth exchanges are never returned to the model as tool
        results, and passwords never appear in logs.
        Non-streaming tools send a single result frame as before.

        Frames without an "id" are JSON-RPC 2.0 notifications: they are
        processed but never answered, not even with an error reply.
        """
        self.update_activity()

        def send(obj: dict):
            """Serialise obj and write it as a newline-terminated frame."""
            writer.write((json.dumps(obj) + "\n").encode("utf-8"))

        try:
            request = json.loads(request_data)
            method = request.get("method", "")
            params = request.get("params", {})
            has_response_id = "id" in request
            request_id = request.get("id")

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
                                if pname in ('kwargs', 'args') or pname.startswith('_'):
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
                if has_response_id:
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"tools": tools},
                    })

            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = dict(params.get("arguments") or {})
                arguments.pop("allow_privileged", None)
                arguments.pop("timeout", None)

                if not hasattr(self.mcp, tool_name):
                    if has_response_id:
                        send({
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
                        })
                    return

                tool_func = getattr(self.mcp, tool_name)
                await self._run_tool_call(
                    tool_func, arguments, request_id, writer, reader,
                    inject_approval=(tool_name == "bash"),
                    emit=has_response_id,
                )

            elif method == "approval/respond":
                approval_id = params.get("approval_id")
                approved = bool(params.get("approved"))
                resolved = self._resolve_approval(approval_id, approved)
                if has_response_id:
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"resolved": resolved},
                    })

            elif method == "auth/status":
                if has_response_id:
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "ok": True,
                            "wrapper_installed": sudo_auth.wrapper_installed(),
                            "broker_available": sudo_auth.broker_available(),
                            "authenticated": sudo_auth.has_valid_session_token(),
                        },
                    })

            elif method == "auth/sudo_password":
                # The password is registered with the root-side broker,
                # which verifies it via sudo/PAM and issues the session
                # token held here. Never logged or echoed in any frame.
                password = str(params.get("password") or "")
                token = (
                    sudo_auth.register_session(password) if password else None
                )
                ok = bool(token)
                if ok:
                    sudo_auth.set_session_token(token)
                if has_response_id:
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": (
                            {"ok": True}
                            if ok
                            else {"ok": False, "error": "sudo authentication failed"}
                        ),
                    })

            elif method == "auth/respond":
                auth_id = params.get("auth_id")
                password = params.get("password")
                resolved = self._resolve_auth(auth_id, password if password else None)
                if has_response_id:
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"resolved": resolved},
                    })

            else:
                if has_response_id:
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
    from openterm.config.app_home import resolve_app_home
    from openterm.mcp.config import init_config

    resolve_app_home()
    init_config()
    from openterm.mcp.tools import mcp

    print("Starting openterm MCP tool server...")
    
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
        
        # Restrict the socket to the owning user: sudo passwords traverse
        # this socket, so no other local account may connect to it.
        os.chmod(socket_path, 0o600)
        
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
                logger.debug(
                    "Received request: %s...", _redact_secrets(request_text)[:100]
                )

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
        sudo_auth.clear_session_token()
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
