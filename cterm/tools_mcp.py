#!/usr/bin/env python3
"""MCP tools definitions for cterm"""
import os
import subprocess
import requests
import time


try:
    from utils import _parse_command_part, _run_pipeline, _split_chained_commands
except ImportError:
    from .utils import _parse_command_part, _run_pipeline, _split_chained_commands
# Simple wrapper class to hold tools (no FastMCP dependency needed for server)
class MCPTools:
    """Container for MCP tool functions"""
    pass

mcp = MCPTools()

# Decorator to mark functions as MCP tools
def tool(func):
    """Decorator to mark a function as an MCP tool"""
    func.__mcp_tool__ = True
    return func

# --- Tool Definitions ---

@tool
def list_files(path: str) -> dict:
    """Lists files in a directory"""
    # FIX: Expand ~ to the user's home directory
    expanded_path = os.path.expanduser(path)
    if not os.path.isdir(expanded_path):
        return {"ok": False, "error": f"Not a directory: {path}"}
    try:
        return {"ok": True, "path": path, "contents": os.listdir(expanded_path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@tool
def read_file(path: str) -> dict:
    """Reads the content of a file"""
    if not os.path.isfile(path):
        return {"ok": False, "error": f"File not found: {path}"}
    try:
        with open(path, "r") as f:
            content = f.read()
        return {"ok": True, "path": path, "content": content}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@tool
def run_shell(command: str, stream: bool = False) -> dict:
    """
    Executes shell commands. Supports `&&` chaining and `|` pipelines.
    Privileged commands
    (apt, apt-get, tee, snap) are routed through the cterm privileged
    wrapper and run as root without a password prompt. Everything else
    runs as the current user with no restrictions.

    When stream=True, stdout/stderr lines are yielded incrementally as
    {"type": "stream", "fd": "stdout"|"stderr", "line": "..."} dicts,
    followed by a final {"type": "result", ...} summary dict.
    When stream=False (default), behaviour is identical to before.
    """
    parts = _split_chained_commands(command)
    results = []

    # ------------------------------------------------------------------ #
    #  Non-streaming path (original behaviour)                            #
    # ------------------------------------------------------------------ #
    if not stream:
        for cmd_str in parts:
            parsed, err = _parse_command_part(cmd_str, results)
            if err:
                return err
            argv_list = parsed.argv_list
            if not argv_list:
                continue

            try:
                if len(argv_list) > 1:
                    result_entry = _run_pipeline(argv_list, cmd_str, timeout=60)
                else:
                    result = subprocess.run(
                        argv_list[0].argv,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL if parsed.suppress_stderr else subprocess.PIPE,
                        text=True,
                        timeout=60,
                    )
                    result_entry = {
                        "command": cmd_str,
                        "stdout": result.stdout,
                        "stderr": "" if parsed.suppress_stderr else result.stderr,
                        "returncode": result.returncode,
                    }

                results.append(result_entry)
                if result_entry.get("ok") is False:
                    return {"ok": False, "error": result_entry["error"], "results": results}
                if result_entry["returncode"] != 0 and not result_entry["stdout"].strip():
                    return {"ok": False, "error": f"Command failed: {cmd_str}", "results": results}
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"Command timed out: {cmd_str}", "results": results}
            except Exception as e:
                return {"ok": False, "error": str(e), "results": results}

        return {"ok": True, "command": command, "results": results}

    # ------------------------------------------------------------------ #
    #  Streaming path                                                      #
    #  Returns a generator; each yield is a dict to be serialised         #
    #  by the caller. Protocol:                                            #
    #    {"type": "stream", "fd": "stdout"|"stderr", "line": str}         #
    #    {"type": "result", "ok": bool, ...}   ← final item               #
    # ------------------------------------------------------------------ #
    def _stream_generator():
        import select

        def emit_completed_chunks(fd, text, pending, output_chunks):
            pending += text
            while True:
                newline_pos = pending.find("\n")
                carriage_pos = pending.find("\r")
                positions = [pos for pos in (newline_pos, carriage_pos) if pos != -1]
                if not positions:
                    break

                split_at = min(positions)
                end = pending[split_at]
                chunk = pending[:split_at]
                if end == "\r" and pending[split_at + 1:split_at + 2] == "\n":
                    end = "\n"
                    pending = pending[split_at + 2:]
                else:
                    pending = pending[split_at + 1:]
                if chunk:
                    output_chunks.append(chunk)
                    yield {
                        "type": "stream",
                        "fd": fd,
                        "line": chunk,
                        "end": end,
                    }

            return pending

        for cmd_str in parts:
            parsed, err = _parse_command_part(cmd_str, results)
            if err:
                yield {"type": "result", **err}
                return
            argv_list = parsed.argv_list
            if not argv_list:
                continue

            if len(argv_list) > 1:
                result_entry = _run_pipeline(argv_list, cmd_str, timeout=60)
                if result_entry.get("stdout"):
                    for line in result_entry["stdout"].splitlines():
                        yield {"type": "stream", "fd": "stdout", "line": line}
                if result_entry.get("stderr"):
                    for line in result_entry["stderr"].splitlines():
                        yield {"type": "stream", "fd": "stderr", "line": line}
                results.append(result_entry)
                if result_entry.get("ok") is False:
                    yield {"type": "result", "ok": False,
                           "error": result_entry["error"], "results": results}
                    return
                if result_entry["returncode"] != 0 and not result_entry["stdout"].strip():
                    yield {"type": "result", "ok": False,
                           "error": f"Command failed: {cmd_str}", "results": results}
                    return
                continue

            output_lines = {
                "stdout": [],
                "stderr": [],
            }
            pending = {
                "stdout": "",
                "stderr": "",
            }

            try:
                proc = subprocess.Popen(
                    argv_list[0].argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL if parsed.suppress_stderr else subprocess.PIPE,
                    bufsize=0,
                    close_fds=True,
                )
            except Exception as e:
                yield {"type": "result", "ok": False,
                       "error": str(e), "results": results}
                return

            deadline = time.time() + 60  # 60-second timeout
            fd_to_stream = {
                proc.stdout.fileno(): "stdout",
            }
            if not parsed.suppress_stderr:
                fd_to_stream[proc.stderr.fileno()] = "stderr"

            while fd_to_stream:
                remaining = deadline - time.time()
                if remaining <= 0:
                    proc.kill()
                    proc.wait()
                    proc.stdout.close()
                    if proc.stderr:
                        proc.stderr.close()
                    yield {"type": "result", "ok": False,
                           "error": f"Command timed out: {cmd_str}",
                           "results": results}
                    return

                readable, _, exceptional = select.select(
                    list(fd_to_stream),
                    [],
                    list(fd_to_stream),
                    min(remaining, 1.0),
                )
                for fd in exceptional:
                    fd_to_stream.pop(fd, None)

                if not readable:
                    continue

                for fd in readable:
                    stream_name = fd_to_stream.get(fd)
                    if stream_name is None:
                        continue

                    try:
                        data = os.read(fd, 4096)
                    except OSError as e:
                        fd_to_stream.pop(fd, None)
                        yield {"type": "result", "ok": False,
                               "error": str(e), "results": results}
                        return

                    if not data:
                        fd_to_stream.pop(fd, None)
                        continue

                    text = data.decode(errors="replace")
                    chunks = emit_completed_chunks(
                        stream_name,
                        text,
                        pending[stream_name],
                        output_lines[stream_name],
                    )
                    pending[stream_name] = yield from chunks

            for stream_name, chunk in pending.items():
                if chunk:
                    output_lines[stream_name].append(chunk)
                    yield {
                        "type": "stream",
                        "fd": stream_name,
                        "line": chunk,
                        "end": "\n",
                    }

            proc.wait()
            proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

            result_entry = {
                "command": cmd_str,
                "stdout": "\n".join(output_lines["stdout"]),
                "stderr": "" if parsed.suppress_stderr else "\n".join(output_lines["stderr"]),
                "returncode": proc.returncode,
            }
            results.append(result_entry)

            if proc.returncode != 0 and not result_entry["stdout"].strip():
                yield {"type": "result", "ok": False,
                       "error": f"Command failed: {cmd_str}", "results": results}
                return

        yield {"type": "result", "ok": True, "command": command, "results": results}

    return _stream_generator()


@tool
def calculate(a: float, b: float, op: str) -> dict:
    """Performs basic math operations (add, sub, mul, div)"""
    ops = {
        "add": lambda x, y: x + y,
        "sub": lambda x, y: x - y,
        "mul": lambda x, y: x * y,
        "div": lambda x, y: x / y if y != 0 else None
    }
    if op not in ops:
        return {"ok": False, "error": f"Invalid op: {op}. Use: add, sub, mul, div"}
    result = ops[op](a, b)
    if result is None:
        return {"ok": False, "error": "Division by zero"}
    return {"ok": True, "result": result}

@tool
def fetch_json(url: str) -> dict:
    """Fetches and parses JSON from a URL"""
    try:
        resp = requests.get(url, timeout=5)
        return {"ok": True, "url": url, "data": resp.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@tool
def system_info() -> dict:
    """Returns OS and environment information"""
    try:
        import platform
        return {
            "ok": True,
            "os": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "python_version": platform.python_version(),
            },
            "env": dict(os.environ),
            "cwd": os.getcwd(),
            "hostname": platform.node(),
            "user": os.environ.get("USER") or os.environ.get("USERNAME", "unknown"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
@tool
def write_file(path: str, content: str, mode: str = "overwrite") -> dict:
    """
    Writes content to a new or existing file.

    Args:
        path:    Destination file path. Parent directories are created
                 automatically if they do not exist.
        content: Text to write.
        mode:    "overwrite" (default) — replace the file entirely.
                 "append"    — add content to the end of an existing file
                               (or create it if absent).
    """
    expanded_path = os.path.expanduser(path)

    if mode not in ("overwrite", "append"):
        return {"ok": False, "error": f"Invalid mode: {mode!r}. Use 'overwrite' or 'append'."}

    try:
        parent = os.path.dirname(expanded_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        open_mode = "w" if mode == "overwrite" else "a"
        with open(expanded_path, open_mode, encoding="utf-8") as f:
            f.write(content)

        return {
            "ok": True,
            "path": path,
            "mode": mode,
            "bytes_written": len(content.encode("utf-8")),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
# Attach tools to mcp object
mcp.list_files = list_files
mcp.read_file = read_file
mcp.run_shell = run_shell
mcp.calculate = calculate
mcp.fetch_json = fetch_json
mcp.system_info = system_info
mcp.write_file = write_file
