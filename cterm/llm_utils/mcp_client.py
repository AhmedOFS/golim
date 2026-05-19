

import json
from pathlib import Path
import socket
import sys

import select as _select

class FastMCPClient:
    def __init__(self, socket_path):
        self.socket_path = socket_path

    def _open_socket(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.socket_path))
        return sock

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
                    if "stream" not in frame:
                        return frame
            if not buf.strip():
                raise ConnectionError("No response from server")
            return json.loads(buf.decode().strip())
        finally:
            sock.close()

    def _stream_request(self, method, params=None, on_stream=None):
        sock = self._open_socket()
        try:
            sock.sendall((json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params or {}}) + "\n").encode())
            buf, final_frame, TIMEOUT = b"", None, 120
            while True:
                readable, _, exceptional = _select.select([sock], [], [sock], TIMEOUT)
                if exceptional:
                    raise ConnectionError("Socket error while waiting for stream frames")
                if not readable:
                    raise TimeoutError(f"No data received from server after {TIMEOUT}s")
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
                    if "stream" in frame:
                        if on_stream:
                            stream = frame["stream"]
                            on_stream(stream["fd"], stream["line"], stream.get("end", "\n"))
                    else:
                        final_frame = frame
            if buf.strip():
                frame = json.loads(buf.decode().strip())
                if "stream" in frame:
                    if on_stream:
                        stream = frame["stream"]
                        on_stream(stream["fd"], stream["line"], stream.get("end", "\n"))
                else:
                    final_frame = frame
            if final_frame is None:
                raise ConnectionError("No result frame received from server")
            return final_frame
        finally:
            sock.close()

    _PARAMS_MAP = {
        'list_files': {'type':'object','properties':{'path':{'type':'string','description':'Directory path to list'}},'required':['path']},
        'read_file':  {'type':'object','properties':{'path':{'type':'string','description':'File path to read'}},'required':['path']},
        'bash':  {'type':'object','properties':{'command':{'type':'string','description':'Shell command to execute'}},'required':['command']},
        'calculate':  {'type':'object','properties':{'a':{'type':'number','description':'First number'},'b':{'type':'number','description':'Second number'},'op':{'type':'string','description':'Operation: add, sub, mul, div','enum':['add','sub','mul','div']}},'required':['a','b','op']},
        'fetch_json': {'type':'object','properties':{'url':{'type':'string','description':'URL to fetch JSON from'}},'required':['url']},
    }

    def _make_tool(self, name, description='', parameters=None):
        return type('Tool', (), {'name': name, 'description': description,
                                 'inputSchema': parameters or {},
                                 'parameters': self._PARAMS_MAP.get(name, parameters or {})})()

    async def list_tools(self):
        try:
            response = self._send_request("tools/list")
            result = response.get("result", {})
            tools_data = result.get("tools") if isinstance(result, dict) else (result if isinstance(result, list) else None) or response.get("tools")
            if not tools_data:
                raise ValueError("No tools in response")
            return [self._make_tool(t.get('name'), t.get('description',''), t.get('inputSchema',{})) for t in tools_data]
        except Exception:
            return [self._make_tool(name, f'Tool: {name}', params)
                    for name, params in self._PARAMS_MAP.items()]

    async def call_tool(self, tool_name, args, stream_output=False, on_stream=None):
        try:
            if not Path(self.socket_path).exists():
                raise ConnectionRefusedError(f"UDS socket not found at {self.socket_path}")
            call_args = dict(args)
            if stream_output and tool_name == "bash":
                call_args["stream"] = True
                def _on_stream(fd, line, end="\n"):
                    if on_stream:
                        on_stream(fd, line, end)
                        return
                    output = f"\033[33m{line}\033[0m" if fd == "stderr" else line
                    sys.stdout.write(output + end)
                    sys.stdout.flush()
                response = self._stream_request("tools/call", {"name": tool_name, "arguments": call_args}, on_stream=_on_stream)
            else:
                response = self._send_request("tools/call", {"name": tool_name, "arguments": call_args})
            if "result" in response:
                return response["result"]
            if "error" in response:
                return {"ok": False, "error": response["error"].get("message", "Unknown error")}
            return {"ok": True, "result": response}
        except Exception as e:
            return {"ok": False, "error": f"Tool execution error: {str(e)}"}

    def close(self):
        pass
