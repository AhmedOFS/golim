
"""MCP tools definitions for cterm"""
import json
import os
import subprocess
import sys
import time


try:
    from utils import (
        _parse_command_part,
        _run_pipeline,
        _split_chained_commands,
        _BLOCKED_BINARIES,
    )
except ImportError:
    from .utils import (
        _parse_command_part,
        _run_pipeline,
        _split_chained_commands,
        _BLOCKED_BINARIES,
    )
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

# ---------------------------------------------------------------------------
# Config helper
# ---------------------------------------------------------------------------

def _read_cterm_config() -> dict:
    """Read ~/.config/cterm/config.json, returning {} on any error."""
    config_path = os.path.expanduser("~/.config/cterm/config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _is_bash_unrestricted() -> bool:
    """Return True when bash_unrestricted is set to true in cterm config."""
    return bool(_read_cterm_config().get("bash_unrestricted", False))


# ---------------------------------------------------------------------------
# Unrestricted bash helpers
# ---------------------------------------------------------------------------

def _run_unrestricted(command: str, timeout: int = 60) -> dict:
    """
    Run *command* via a real bash shell.

    The sudo whitelist is still enforced: any `sudo <binary>` invocation in
    the command is pre-scanned and rejected when the resolved binary is not
    on cterm's privileged whitelist.  Everything else runs unfiltered under
    the current user.
    """
    try:
        from privilege import is_privileged_binary_allowed
    except ImportError:
        from .privilege import is_privileged_binary_allowed

    import shutil
    import re

    # Best-effort whitelist check: find `sudo <word>` tokens and verify each.
    # This is intentionally conservative — it catches the common cases without
    # trying to fully parse arbitrary shell syntax.
    for m in re.finditer(r'(?<![^\s])sudo\s+(\S+)', command):
        binary_token = m.group(1)
        # Strip leading flags (e.g. -n / --non-interactive)
        if binary_token.startswith("-"):
            continue
        resolved = shutil.which(binary_token)
        if resolved and not is_privileged_binary_allowed(resolved):
            return {
                "ok": False,
                "error": (
                    f"Privileged command requires approval: {resolved}"
                ),
                "approval_required": True,
                "approval_kind": "privileged_whitelist",
                "binary": resolved,
                "results": [],
            }

    # Block binaries that have a cterm tool equivalent.
    for m in re.finditer(r'(?:^|[|&;(]\s*)(\S+)', command):
        token = m.group(1).lstrip("(").strip()
        resolved = shutil.which(token)
        if resolved:
            name = os.path.basename(resolved)
            if name in _BLOCKED_BINARIES:
                tool_name, hint = _BLOCKED_BINARIES[name]
                return {
                    "ok": False,
                    "error": f"`{name}` is not available. {hint}",
                    "results": [],
                }

    try:
        result = subprocess.run(
            ["/bin/bash", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        entry = {
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
        ok = result.returncode == 0 or bool(result.stdout.strip())
        return {"ok": ok, "command": command, "results": [entry]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Command timed out: {command}", "results": []}
    except Exception as e:
        return {"ok": False, "error": str(e), "results": []}


def _stream_unrestricted(command: str, timeout: int = 60):
    """
    Streaming variant of _run_unrestricted.  Yields the same dict protocol
    as the streaming path in bash().
    """
    try:
        from privilege import is_privileged_binary_allowed
    except ImportError:
        from .privilege import is_privileged_binary_allowed

    import shutil
    import re
    import select

    for m in re.finditer(r'(?<![^\s])sudo\s+(\S+)', command):
        binary_token = m.group(1)
        if binary_token.startswith("-"):
            continue
        resolved = shutil.which(binary_token)
        if resolved and not is_privileged_binary_allowed(resolved):
            yield {
                "type": "result",
                "ok": False,
                "error": f"Privileged command requires approval: {resolved}",
                "approval_required": True,
                "approval_kind": "privileged_whitelist",
                "binary": resolved,
                "results": [],
            }
            return

    # Block binaries that have a cterm tool equivalent.
    for m in re.finditer(r'(?:^|[|&;(]\s*)(\S+)', command):
        token = m.group(1).lstrip("(").strip()
        resolved = shutil.which(token)
        if resolved:
            name = os.path.basename(resolved)
            if name in _BLOCKED_BINARIES:
                tool_name, hint = _BLOCKED_BINARIES[name]
                yield {
                    "type": "result",
                    "ok": False,
                    "error": f"`{name}` is not available. {hint}",
                    "results": [],
                }
                return

    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
        )
    except Exception as e:
        yield {"type": "result", "ok": False, "error": str(e), "results": []}
        return

    output_lines = {"stdout": [], "stderr": []}
    pending = {"stdout": "", "stderr": ""}
    deadline = time.time() + timeout
    fd_map = {proc.stdout.fileno(): "stdout", proc.stderr.fileno(): "stderr"}

    while fd_map:
        remaining = deadline - time.time()
        if remaining <= 0:
            proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
            yield {"type": "result", "ok": False,
                   "error": f"Command timed out: {command}", "results": []}
            return

        readable, _, exceptional = select.select(
            list(fd_map), [], list(fd_map), min(remaining, 1.0)
        )
        for fd in exceptional:
            fd_map.pop(fd, None)

        for fd in readable:
            stream_name = fd_map.get(fd)
            if stream_name is None:
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                fd_map.pop(fd, None)
                continue
            if not data:
                fd_map.pop(fd, None)
                continue
            text = data.decode(errors="replace")
            buf = pending[stream_name] + text
            while True:
                nl = buf.find("\n")
                cr = buf.find("\r")
                positions = [p for p in (nl, cr) if p != -1]
                if not positions:
                    break
                split_at = min(positions)
                end_ch = buf[split_at]
                chunk = buf[:split_at]
                if end_ch == "\r" and buf[split_at + 1:split_at + 2] == "\n":
                    end_ch = "\n"
                    buf = buf[split_at + 2:]
                else:
                    buf = buf[split_at + 1:]
                if chunk:
                    output_lines[stream_name].append(chunk)
                    yield {"type": "stream", "fd": stream_name, "line": chunk, "end": end_ch}
            pending[stream_name] = buf

    for stream_name, chunk in pending.items():
        if chunk:
            output_lines[stream_name].append(chunk)
            yield {"type": "stream", "fd": stream_name, "line": chunk, "end": "\n"}

    proc.wait()
    proc.stdout.close()
    proc.stderr.close()

    entry = {
        "command": command,
        "stdout": "\n".join(output_lines["stdout"]),
        "stderr": "\n".join(output_lines["stderr"]),
        "returncode": proc.returncode,
    }
    ok = proc.returncode == 0 or bool(entry["stdout"].strip())
    yield {"type": "result", "ok": ok, "command": command, "results": [entry]}


# --- Tool Definitions ---

@tool
def finder(
    path: str,
    pattern: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_depth: int | None = None,
    type_filter: str | None = None,
    max_results: int = 1000,
) -> dict:
    """
    Recursively find files/dirs under path with glob filtering, depth control,
    and type filtering. Returns relative paths from root.

    Args:
        path:        Root directory (~-expanded).
        pattern:     Required filename glob, e.g. "*.py".
        include:     Extra globs; entry matches if it fits pattern OR any of these.
        exclude:     Path globs to skip. Layered on top of built-in defaults
                     (.git, node_modules, __pycache__, .venv, dist, build, etc.).
        max_depth:   Max recursion depth (1 = immediate children). None = unlimited.
        type_filter: "file", "dir", or None for both.
        max_results: Result cap (default 1000).

    Returns:
        {"ok": True, "path": str, "matches": [str, ...], "total": int, "truncated": bool}
    """
    import fnmatch

    def coerce_glob_list(value):
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item)]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return None
                if not isinstance(parsed, list):
                    return None
                return [str(item) for item in parsed if str(item)]
            return [item.strip() for item in text.split(",") if item.strip()]
        return None

    if pattern is None or not str(pattern).strip():
        return {"ok": False, "error": "finder requires an explicit pattern argument"}
    pattern = str(pattern)
    include = coerce_glob_list(include)
    if include is None:
        return {"ok": False, "error": "include must be a list or a JSON list string"}
    exclude = coerce_glob_list(exclude)
    if exclude is None:
        return {"ok": False, "error": "exclude must be a list or a JSON list string"}

    expanded_root = os.path.expanduser(path)
    if not os.path.isdir(expanded_root):
        return {"ok": False, "error": f"Not a directory: {path}"}

    DEFAULT_EXCLUDE = [
        "**/.git/**", "**/node_modules/**", "**/__pycache__/**",
        "**/.venv/**", "**/venv/**", "**/.mypy_cache/**", "**/.pytest_cache/**",
        "**/*.pyc", "**/.DS_Store", "**/dist/**", "**/build/**",
        "**/.next/**", "**/.nuxt/**", "**/.cache/**",
    ]
    if max_depth is not None:
        max_depth = int(max_depth)
    if max_results is not None:
        max_results = int(max_results)
    effective_exclude = DEFAULT_EXCLUDE + exclude

    def match_any(s, pats):
        return any(fnmatch.fnmatch(s, p) or fnmatch.fnmatch(os.path.basename(s), p) for p in pats)

    matches, truncated = [], False

    for dirpath, dirnames, filenames in os.walk(expanded_root):
        rel_dir = os.path.relpath(dirpath, expanded_root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1

        if depth > 0 and match_any(rel_dir.replace(os.sep, "/"), effective_exclude):
            dirnames.clear()
            continue

        if max_depth is not None and depth >= max_depth:
            dirnames.clear()

        dirnames[:] = [
            d for d in dirnames
            if not match_any(os.path.relpath(os.path.join(dirpath, d), expanded_root).replace(os.sep, "/"), effective_exclude)
        ]

        entries = ([(d, "dir") for d in dirnames] if type_filter != "file" else []) + \
                  ([(f, "file") for f in filenames] if type_filter != "dir" else [])

        for name, _ in sorted(entries):
            rel = os.path.relpath(os.path.join(dirpath, name), expanded_root).replace(os.sep, "/")
            if match_any(rel, effective_exclude):
                continue
            if not fnmatch.fnmatch(name, pattern) and not (include and match_any(name, include)):
                continue
            matches.append(rel)
            if len(matches) >= max_results:
                truncated = True
                break

        if truncated:
            break

    return {"ok": True, "path": path, "matches": matches, "total": len(matches), "truncated": truncated}

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
def bash(command: str, stream: bool = False, allow_privileged: bool = False) -> dict:
    """
    Executes command lines.

    When `bash_unrestricted` is set to true in ~/.configcterm/config.json the
    command is passed directly to /bin/bash -c, giving full shell access
    (pipes, redirections, subshells, here-docs, etc.).  The only remaining
    restriction is cterm's sudo whitelist: any `sudo <binary>` call whose
    resolved path is not on the whitelist is rejected and an
    approval_required result is returned, exactly as in the restricted path.

    When `bash_unrestricted` is false (the default) the original safe argv
    parser is used.  It supports unquoted `&&` chaining, unquoted `|`
    pipelines, quoted arguments, environment-variable and `~` expansion,
    glob expansion, and `2>/dev/null` stderr suppression.  Other redirection
    and shell-only syntax are rejected.

    In both modes, `stream=True` yields incremental output chunks followed by
    a final result dict.
    """
    # ------------------------------------------------------------------ #
    #  Unrestricted path                                                   #
    # ------------------------------------------------------------------ #
    if _is_bash_unrestricted():
        if stream:
            return _stream_unrestricted(command, timeout=60)
        return _run_unrestricted(command, timeout=60)

    # ------------------------------------------------------------------ #
    #  Original restricted path (unchanged)                               #
    # ------------------------------------------------------------------ #
    parts = _split_chained_commands(command)
    results = []

    if not stream:
        for cmd_str in parts:
            parsed, err = _parse_command_part(
                cmd_str,
                results,
                allow_privileged=allow_privileged,
            )
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
                    stderr_detail = result_entry.get("stderr", "").strip()
                    error_msg = f"Command failed: {cmd_str}"
                    if stderr_detail:
                        error_msg += f"\n{stderr_detail}"
                    return {"ok": False, "error": error_msg, "results": results}

            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"Command timed out: {cmd_str}", "results": results}
            except Exception as e:
                return {"ok": False, "error": str(e), "results": results}

        return {"ok": True, "command": command, "results": results}

    # ------------------------------------------------------------------ #
    #  Streaming path (restricted)                                         #
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
            parsed, err = _parse_command_part(
                cmd_str,
                results,
                allow_privileged=allow_privileged,
            )
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
                    stderr_detail = result_entry.get("stderr", "").strip()
                    error_msg = f"Command failed: {cmd_str}"
                    if stderr_detail:
                        error_msg += f"\n{stderr_detail}"
                    yield {"type": "result", "ok": False,
                           "error": error_msg, "results": results}
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

            deadline = time.time() + 60
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
                stderr_detail = result_entry.get("stderr", "").strip()
                error_msg = f"Command failed: {cmd_str}"
                if stderr_detail:
                    error_msg += f"\n{stderr_detail}"
                yield {"type": "result", "ok": False,
                       "error": error_msg, "results": results}
                return

        yield {"type": "result", "ok": True, "command": command, "results": results}

    return _stream_generator()


@tool
def exec_python(
    code: str | None = None,
    timeout: int = 30,
    cwd: str | None = None,
    stdin: str | None = None,
    **kwargs,
) -> dict:
    """
    Runs Python code directly with the current Python interpreter.

    Args:
        code:    Python source to run. Friendly aliases are also accepted:
                 "script", "source", or "python".
        timeout: Maximum run time in seconds. Capped at 120 seconds.
        cwd:     Optional working directory.
        stdin:   Optional text passed to the Python process on stdin.
    """
    if code is None:
        for alias in ("script", "source", "python"):
            if alias in kwargs:
                code = kwargs[alias]
                break

    if code is None or not str(code).strip():
        return {"ok": False, "error": "exec requires Python code in the 'code' argument"}

    try:
        timeout = max(1, min(int(timeout), 120))
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout must be an integer number of seconds"}

    run_cwd = None
    if cwd:
        run_cwd = os.path.expanduser(str(cwd))
        if not os.path.isdir(run_cwd):
            return {"ok": False, "error": f"Working directory not found: {cwd}"}

    try:
        result = subprocess.run(
            [sys.executable, "-c", str(code)],
            input="" if stdin is None else str(stdin),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            cwd=run_cwd,
        )
        response = {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
        if result.returncode != 0:
            response["error"] = "Python code failed"
        return response
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "error": f"Python code timed out after {timeout}s",
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
            "returncode": None,
        }
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
mcp.finder = finder
mcp.read_file = read_file
mcp.bash = bash
mcp.exec = exec_python
mcp.system_info = system_info
mcp.write_file = write_file