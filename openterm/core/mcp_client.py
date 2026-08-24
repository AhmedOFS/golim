

import json
from pathlib import Path
import socket
import sys
import select as _select
import threading

class FastMCPClient:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self._active_sockets = set()
        self._socket_lock = threading.Lock()
        self.on_approval_request = None

    def _handle_approval_request(self, approval):
        """Ask the wired-in user UI for a binary approval decision.

        The approval exchange stays in the transport layer: the model never
        receives the approval request or an approval-required tool result.
        """
        callback = self.on_approval_request
        if callback is None:
            return False
        return bool(callback(approval))

    def _send_approval_response(self, approval_id, approved):
        self._send_request("approval/respond", {
            "approval_id": approval_id,
            "approved": bool(approved),
        })

    def _open_socket(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.socket_path))
        with self._socket_lock:
            self._active_sockets.add(sock)
        return sock

    def _close_socket(self, sock):
        with self._socket_lock:
            self._active_sockets.discard(sock)
        try:
            # ``close()`` from a different thread does not reliably wake a
            # blocking ``select``/``recv`` on Linux.  Shutdown first so the
            # server observes EOF and the client-side request thread wakes
            # immediately during cancellation.
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _dispatch_interim_frame(self, frame, on_stream=None):
        """Handle interim traffic received before the response frame.

        Approval requests are answered out-of-band; ``tools/progress``
        notifications are forwarded to the stream callback. Returns True
        when the frame was interim and the caller should keep waiting for
        the JSON-RPC response (``result``/``error``).
        """
        if "approval_request" in frame:
            approval = frame["approval_request"]
            approved = self._handle_approval_request(approval)
            self._send_approval_response(approval.get("approval_id"), approved)
            return True
        if "result" in frame or "error" in frame:
            return False
        if frame.get("method") == "tools/progress":
            params = frame.get("params") or {}
            if on_stream:
                on_stream(params.get("fd"), params.get("line"), params.get("end", "\n"))
        return True

    def _send_request(self, method, params=None):
        sock = self._open_socket()
        try:
            sock.sendall((json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params or {}}) + "\n").encode())
            buf = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    frame = json.loads(line.decode())
                    if not self._dispatch_interim_frame(frame):
                        return frame
            if not buf.strip():
                raise ConnectionError("No response from server")
            return json.loads(buf.decode().strip())
        finally:
            self._close_socket(sock)

    def _stream_request(self, method, params=None, on_stream=None):
        sock = self._open_socket()
        try:
            sock.sendall((json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params or {}}) + "\n").encode())
            buf, final_frame = b"", None
            while True:
                readable, _, exceptional = _select.select([sock], [], [sock])
                if exceptional:
                    raise ConnectionError("Socket error while waiting for stream frames")
                if not readable:
                    continue
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    frame = json.loads(line.decode())
                    if not self._dispatch_interim_frame(frame, on_stream):
                        final_frame = frame
            if buf.strip():
                frame = json.loads(buf.decode().strip())
                if not self._dispatch_interim_frame(frame, on_stream):
                    final_frame = frame
            if final_frame is None:
                raise ConnectionError("No result frame received from server")
            return final_frame
        finally:
            self._close_socket(sock)

    def _make_tool(self, name, description='', parameters=None):
        return type('Tool', (), {'name': name, 'description': description,
                                 'inputSchema': parameters or {},
                                 'parameters': parameters or {}})()

    async def list_tools(self):
        response = self._send_request("tools/list")
        if "error" in response:
            error = response["error"]
            message = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"tools/list failed: {message}")

        result = response.get("result", {})
        if isinstance(result, dict):
            tools_data = result.get("tools")
        elif isinstance(result, list):
            tools_data = result
        else:
            tools_data = response.get("tools")

        if not tools_data:
            raise ValueError("No tools in response")

        return [
            self._make_tool(
                t.get('name'),
                t.get('description', ''),
                t.get('inputSchema', {}),
            )
            for t in tools_data
        ]

    async def call_tool(self, tool_name, args, stream_output=False, on_stream=None):
        try:
            if not Path(self.socket_path).exists():
                raise ConnectionRefusedError(f"UDS socket not found at {self.socket_path}")
            call_args = dict(args)
            response = self._call_tool_once(tool_name, call_args, stream_output, on_stream)
            return self._response_to_result(response)
        except Exception as e:
            return {"ok": False, "error": f"Tool execution error: {str(e)}"}

    def _call_tool_once(self, tool_name, call_args, stream_output=False, on_stream=None):
        if stream_output and tool_name == "bash":
            call_args = dict(call_args)
            call_args["stream"] = True
            def _on_stream(fd, line, end="\n"):
                if on_stream:
                    on_stream(fd, line, end)
                    return
                output = f"\033[33m{line}\033[0m" if fd == "stderr" else line
                sys.stdout.write(output + end)
                sys.stdout.flush()
            return self._stream_request("tools/call", {"name": tool_name, "arguments": call_args}, on_stream=_on_stream)
        return self._send_request("tools/call", {"name": tool_name, "arguments": call_args})

    def _response_to_result(self, response):
        if "result" in response:
            return response["result"]
        if "error" in response:
            return {"ok": False, "error": response["error"].get("message", "Unknown error")}
        return {"ok": True, "result": response}

    def close(self):
        """Abort outstanding requests by closing their Unix sockets."""
        with self._socket_lock:
            sockets = list(self._active_sockets)
        for sock in sockets:
            self._close_socket(sock)
