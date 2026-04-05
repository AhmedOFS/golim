"""LLM interaction module for cterm using Ollama's native tool calling"""
import subprocess
import sys
import threading
import time
import json
import asyncio
import os
import socket
import select as _select
from pathlib import Path


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
                            on_stream(frame["stream"]["fd"], frame["stream"]["line"])
                    else:
                        final_frame = frame
            if buf.strip():
                frame = json.loads(buf.decode().strip())
                if "stream" in frame:
                    if on_stream:
                        on_stream(frame["stream"]["fd"], frame["stream"]["line"])
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
        'run_shell':  {'type':'object','properties':{'command':{'type':'string','description':'Shell command to execute'}},'required':['command']},
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

    async def call_tool(self, tool_name, args, stream_output=False):
        try:
            if not Path(self.socket_path).exists():
                raise ConnectionRefusedError(f"UDS socket not found at {self.socket_path}")
            call_args = dict(args)
            if stream_output and tool_name == "run_shell":
                call_args["stream"] = True
                def _on_stream(fd, line):
                    print(f"\033[33m{line}\033[0m" if fd == "stderr" else line, flush=True)
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


def get_socket_path() -> Path:
    return Path(f"/tmp/cterm_mcp_{os.getlogin()}.sock")


class Spinner:
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, message="Thinking"):
        self.message = message
        self.running = False
        self.thread = None
        self._idx = 0

    def _spin(self):
        while self.running:
            sys.stderr.write(f"\r{self.FRAMES[self._idx % len(self.FRAMES)]} {self.message}...")
            sys.stderr.flush()
            self._idx += 1
            time.sleep(0.1)
        sys.stderr.write("\r" + " " * (len(self.message) + 10) + "\r")
        sys.stderr.flush()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)


def chat_with_model(model, message, binary="ollama"):
    spinner = Spinner("Thinking")
    try:
        spinner.start()
        result = subprocess.run([binary, "run", model, message], capture_output=True, text=True, check=True)
        spinner.stop()
        response = result.stdout.strip() or result.stderr.strip()
        if not response:
            raise RuntimeError("Empty response from model")
        return response
    except Exception:
        spinner.stop()
        raise


def chat_with_model_api(model, messages, tools=None, binary="ollama"):
    import requests
    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    response = requests.post("http://localhost:11434/api/chat", json=payload, timeout=60)
    response.raise_for_status()
    return response.json()


def _indent(text, prefix="      "):
    return "\n".join(prefix + line for line in text.splitlines())


class ToolAgent:
    MAX_SHELL_RETRIES = 3
    MAX_TOOL_ITERATIONS = 10

    def __init__(self, model, binary="ollama"):
        self.model = model
        self.binary = binary
        self.mcp_client = None
        self.tools = []
        self.use_native_tools = self._check_native_tool_support()

    def _check_native_tool_support(self):
        try:
            import requests
            return requests.get("http://localhost:11434/api/tags", timeout=5).status_code == 200
        except Exception:
            return False

    def __enter__(self):
        socket_path = get_socket_path()
        def _try_connect():
            self.mcp_client = FastMCPClient(socket_path)
            self.tools = asyncio.run(self.mcp_client.list_tools())

        try:
            _try_connect()
        except (ConnectionRefusedError, FileNotFoundError):
            try:
                subprocess.run(["systemctl", "--user", "start", "cterm-mcp.service"],
                               check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError:
                raise RuntimeError("Failed to activate tool server.")
            for _ in range(10):
                time.sleep(0.2)
                try:
                    _try_connect()
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    continue
            else:
                raise TimeoutError("Tool server started but socket never became available.")
        return self

    def __exit__(self, *_):
        if self.mcp_client:
            self.mcp_client.close()

    def run(self, user_message):
        return self._run_with_native_tools(user_message) if self.use_native_tools else self._run_with_fallback(user_message)

    def _execute_tool_with_retry(self, tool_name, args, messages, ollama_tools):
        attempt = 0
        while True:
            is_shell = tool_name == "run_shell"
            print(f"\n🔧 [{attempt + 1}] {tool_name}({json.dumps(args)})", file=sys.stderr)
            if is_shell:
                print("─" * 60, file=sys.stderr)

            tool_result = asyncio.run(self.mcp_client.call_tool(tool_name, args, stream_output=is_shell))

            if is_shell:
                exit_codes = [r.get("returncode") for r in tool_result.get("results", [])]
                print("─" * 60, file=sys.stderr)
                print(f"  ↳ ok={tool_result.get('ok', True)}  returncodes={exit_codes}", file=sys.stderr)
            else:
                print(f"  ↳ result: {tool_result}", file=sys.stderr)

            if not is_shell or tool_result.get("ok", True):
                return tool_result, messages

            attempt += 1
            failed_cmd = args.get("command", "<unknown>")
            error_msg  = tool_result.get("error", "")
            stdout_out = "".join(r.get("stdout","") + "\n" for r in tool_result.get("results",[]))
            stderr_out = "".join(r.get("stderr","") + "\n" for r in tool_result.get("results",[]))

            print(f"\n❌ Attempt {attempt}/{self.MAX_SHELL_RETRIES} failed", file=sys.stderr)
            print(f"   command : {failed_cmd}", file=sys.stderr)
            print(f"   error   : {error_msg or '(none)'}", file=sys.stderr)
            if stdout_out.strip():
                print(f"   stdout  :\n{_indent(stdout_out.strip())}", file=sys.stderr)
            if stderr_out.strip():
                print(f"   stderr  :\n{_indent(stderr_out.strip())}", file=sys.stderr)

            if attempt >= self.MAX_SHELL_RETRIES:
                print(f"\n⛔ Giving up after {attempt} attempt(s).", file=sys.stderr)
                return tool_result, messages

            failure_summary = (
                f"The command `{failed_cmd}` failed.\nError: {error_msg}\n"
                + (f"Stdout output:\n{stdout_out.strip()}\n" if stdout_out.strip() else "")
                + (f"Stderr output:\n{stderr_out.strip()}\n" if stderr_out.strip() else "")
                + "Please analyse the error and call run_shell again with a corrected command that addresses the problem."
            )
            messages = list(messages)
            messages += [{"role":"tool","content":json.dumps(tool_result)},
                         {"role":"user","content":failure_summary}]

            print(f"\n↩️  Asking model to retry (attempt {attempt + 1} of {self.MAX_SHELL_RETRIES})…", file=sys.stderr)
            spinner = Spinner(f"Retry {attempt + 1}/{self.MAX_SHELL_RETRIES}")
            spinner.start()
            retry_response = chat_with_model_api(self.model, messages, ollama_tools, self.binary)
            spinner.stop()

            retry_message = retry_response.get("message", {})
            if not retry_message.get("tool_calls"):
                print(f"\n🤖 Model declined to retry:\n{_indent(retry_message.get('content','(no reply)'))}", file=sys.stderr)
                return tool_result, messages

            retry_call = retry_message["tool_calls"][0]
            tool_name  = retry_call["function"]["name"]
            args       = retry_call["function"].get("arguments", {})
            print(f"   model chose: {tool_name}({json.dumps(args)})", file=sys.stderr)
            messages.append(retry_message)

    def _run_with_native_tools(self, user_message):
        ollama_tools = [
            {"type":"function","function":{"name":t.name,"description":getattr(t,'description',''),"parameters":getattr(t,'parameters',{})}}
            for t in self.tools
        ]
        messages = [
            {"role":"system","content":(
                "Use the tools available to you to perform the tasks or answer the questions asked of you on the user's system. "
                "Use the run_shell tool to execute commands, and use snap or apt for app installations when relevant."
            )},
            {"role":"user","content":user_message}
        ]
        try:
            for iteration in range(1, self.MAX_TOOL_ITERATIONS + 1):
                print(f"\n🔁 Iteration {iteration}/{self.MAX_TOOL_ITERATIONS}", file=sys.stderr)

                spinner = Spinner("Thinking")
                spinner.start()
                response = chat_with_model_api(self.model, messages, ollama_tools, self.binary)
                spinner.stop()

                message = response.get("message", {})

                if message.get("tool_calls"):
                    function  = message["tool_calls"][0].get("function", {})
                    tool_name = function.get("name")
                    args      = function.get("arguments", {})
                    messages.append(message)

                    tool_result, messages = self._execute_tool_with_retry(tool_name, args, messages, ollama_tools)

                    if messages[-1].get("role") != "tool":
                        messages.append({"role":"tool","content":json.dumps(tool_result)})

                    messages.append({"role":"user","content":(
                        f"The original task was: {user_message}\n\n"
                        "Has the task been fully completed based on the tool results so far?\n"
                        "- If YES: reply with your final summary for the user and do NOT call any more tools.\n"
                        "- If NO: call the next required tool to continue."
                    )})
                    print("🔍 Checking task completion…", file=sys.stderr)
                    continue

                print(f"\n✅ Task complete (iteration {iteration})", file=sys.stderr)
                return message.get("content", "No response")

            print(f"\n⚠️  Reached maximum iterations ({self.MAX_TOOL_ITERATIONS}), returning last model reply.", file=sys.stderr)
            messages.append({"role":"user","content":(
                "You have reached the maximum number of tool calls. "
                "Please summarise what was accomplished and what (if anything) still needs to be done."
            )})
            spinner = Spinner("Summarising")
            spinner.start()
            final = chat_with_model_api(self.model, messages, ollama_tools, self.binary)
            spinner.stop()
            return final.get("message", {}).get("content", "No response")

        except Exception as e:
            import traceback
            print(f"\n💥 Exception in _run_with_native_tools: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return self._run_with_fallback(user_message)

    def _run_with_fallback(self, user_message):
        return chat_with_model(self.model, user_message, self.binary)


def chat_with_tools(model, message, binary="ollama"):
    with ToolAgent(model, binary) as agent:
        return agent.run(message)