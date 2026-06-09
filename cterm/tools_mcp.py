
"""MCP tools definitions for cterm"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

OUTPUT_LINE_LIMIT = 50


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


def _data_dir() -> str:
    path = os.path.expanduser("~/cterm/data")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        fallback = "/tmp/cterm/data"
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _count_output_lines(results: list[dict]) -> int:
    total = 0
    for entry in results:
        total += len(str(entry.get("stdout", "")).splitlines())
        total += len(str(entry.get("stderr", "")).splitlines())
    return total


def _command_uses_output_file_binary(command: str) -> bool:
    for separator in ("&&", "||", "|", ";", "&", "(", ")"):
        command = command.replace(separator, "\n")

    for segment in command.splitlines():
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()

        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if token == "sudo":
                idx += 1
                while idx < len(tokens) and tokens[idx].startswith("-"):
                    idx += 1
                continue
            if token == "env" or "=" in token and not token.startswith("="):
                idx += 1
                continue
            if token == "command":
                idx += 1
                continue

            name = os.path.basename(token)
            if "/" not in token:
                resolved = shutil.which(token)
                if resolved:
                    name = os.path.basename(resolved)
            if name in {"find", "du"}:
                return True
            break
    return False


def _payload_allows_output_file(payload: dict) -> bool:
    if _command_uses_output_file_binary(str(payload.get("command") or "")):
        return True
    for entry in payload.get("results") or []:
        if isinstance(entry, dict) and _command_uses_output_file_binary(
            str(entry.get("command") or "")
        ):
            return True
    return False


def _save_bash_output(payload: dict) -> str:
    base_name = f"bash_output_{int(time.time() * 1000)}_{os.getpid()}"

    for directory in (_data_dir(), "/tmp/cterm/data"):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{base_name}.json")
        counter = 2
        while os.path.exists(path):
            path = os.path.join(directory, f"{base_name}_{counter}.json")
            counter += 1
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            return path
        except OSError:
            continue

    raise OSError("Could not write bash output file")


def _truncate_text_lines(text: str, remaining: int) -> tuple[str, int]:
    lines = str(text).splitlines()
    if remaining <= 0:
        return "", 0
    kept = lines[:remaining]
    return "\n".join(kept), max(0, remaining - len(kept))


def _truncate_result_entries(results: list[dict], limit: int = OUTPUT_LINE_LIMIT) -> list[dict]:
    remaining = limit
    truncated = []
    for entry in results:
        copied = dict(entry)
        copied["stdout"], remaining = _truncate_text_lines(copied.get("stdout", ""), remaining)
        copied["stderr"], remaining = _truncate_text_lines(copied.get("stderr", ""), remaining)
        truncated.append(copied)
    return truncated


def _finalize_bash_payload(payload: dict) -> dict:
    results = payload.get("results")
    if not isinstance(results, list):
        return payload

    line_count = _count_output_lines(results)
    payload["output_line_count"] = line_count
    payload["output_limit"] = OUTPUT_LINE_LIMIT
    payload["output_truncated"] = False
    if line_count <= OUTPUT_LINE_LIMIT:
        return payload
    if not _payload_allows_output_file(payload):
        return payload

    saved_path = _save_bash_output(payload)
    truncated_payload = dict(payload)
    truncated_payload["results"] = _truncate_result_entries(results)
    truncated_payload["output_truncated"] = True
    truncated_payload["output_file"] = saved_path
    truncated_payload["message"] = (
        f"Output exceeded {OUTPUT_LINE_LIMIT} lines. "
        f"Full output saved to {saved_path}."
    )
    return truncated_payload


# ---------------------------------------------------------------------------
# Unrestricted bash helpers
# ---------------------------------------------------------------------------

def _run_unrestricted(command: str, timeout: int = 60, allow_privileged: bool = False) -> dict:
    """
    Run *command* via a real bash shell.

    The sudo whitelist is still enforced: any `sudo <binary>` invocation in
    the command is pre-scanned and rejected when the resolved binary is not
    on cterm's privileged whitelist.  When `allow_privileged` is True, missing
    binaries are automatically added to the whitelist and the command is
    re-routed through cterm's privileged wrapper.  Everything else runs
    unfiltered under the current user.
    """
    try:
        from privilege import is_privileged_binary_allowed, add_privileged_binary
    except ImportError:
        from .privilege import is_privileged_binary_allowed, add_privileged_binary
    from .utils import PRIVILEGED_WRAPPER

    import shutil
    import re

    # Best-effort whitelist check: find `sudo <word>` tokens and verify each.
    # This is intentionally conservative — it catches the common cases without
    # trying to fully parse arbitrary shell syntax.
    sudo_replacements = []
    for m in re.finditer(r'(?<![^\s])sudo\s+(\S+)', command):
        binary_token = m.group(1)
        # Strip leading flags (e.g. -n / --non-interactive)
        if binary_token.startswith("-"):
            continue
        resolved = shutil.which(binary_token)
        if not resolved:
            continue
        if not is_privileged_binary_allowed(resolved):
            if not allow_privileged:
                return {
                    "ok": False,
                    "error": f"Privileged command requires approval: {resolved}",
                    "approval_required": True,
                    "approval_kind": "privileged_whitelist",
                    "binary": resolved,
                    "results": [],
                }
            add_privileged_binary(resolved)
        sudo_replacements.append((m.start(), m.end(), resolved))

    # Build the transformed command: replace `sudo <binary>` with the wrapper
    # invocation so that whitelisted sudo commands go through
    # /usr/lib/cterm/cterm-privileged instead of real sudo.
    if sudo_replacements:
        parts = []
        last_end = 0
        for start, end, resolved in sudo_replacements:
            parts.append(command[last_end:start])
            parts.append(f"sudo --non-interactive {PRIVILEGED_WRAPPER} {resolved}")
            last_end = end
        parts.append(command[last_end:])
        command = "".join(parts)

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
        return _finalize_bash_payload({"ok": ok, "command": command, "results": [entry]})
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Command timed out: {command}", "results": []}
    except Exception as e:
        return {"ok": False, "error": str(e), "results": []}


def _stream_unrestricted(command: str, timeout: int = 60, allow_privileged: bool = False):
    """
    Streaming variant of _run_unrestricted.  Yields the same dict protocol
    as the streaming path in bash().
    """
    try:
        from privilege import is_privileged_binary_allowed, add_privileged_binary
    except ImportError:
        from .privilege import is_privileged_binary_allowed, add_privileged_binary
    from .utils import PRIVILEGED_WRAPPER

    import shutil
    import re
    import select

    sudo_replacements = []
    for m in re.finditer(r'(?<![^\s])sudo\s+(\S+)', command):
        binary_token = m.group(1)
        if binary_token.startswith("-"):
            continue
        resolved = shutil.which(binary_token)
        if not resolved:
            continue
        if not is_privileged_binary_allowed(resolved):
            if not allow_privileged:
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
            add_privileged_binary(resolved)
        sudo_replacements.append((m.start(), m.end(), resolved))

    if sudo_replacements:
        parts = []
        last_end = 0
        for start, end, resolved in sudo_replacements:
            parts.append(command[last_end:start])
            parts.append(f"sudo --non-interactive {PRIVILEGED_WRAPPER} {resolved}")
            last_end = end
        parts.append(command[last_end:])
        command = "".join(parts)

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
    cap_stream_output = _command_uses_output_file_binary(command)
    streamed_lines = 0

    def should_emit_stream():
        nonlocal streamed_lines
        if not cap_stream_output:
            return True
        if streamed_lines >= OUTPUT_LINE_LIMIT:
            return False
        streamed_lines += 1
        return True

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
                    if should_emit_stream():
                        yield {"type": "stream", "fd": stream_name, "line": chunk, "end": end_ch}
            pending[stream_name] = buf

    for stream_name, chunk in pending.items():
        if chunk:
            output_lines[stream_name].append(chunk)
            if should_emit_stream():
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
    final_payload = _finalize_bash_payload({"ok": ok, "command": command, "results": [entry]})
    yield {"type": "result", **final_payload}


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
def read_file(path: str, page: int = 1) -> dict:
    """
    Reads one 50-line page from a file.

    Args:
        path: File path to read.
        page: 1-based page number. Each page returns up to 50 lines.
    """
    if not os.path.isfile(path):
        return {"ok": False, "error": f"File not found: {path}"}
    try:
        page = int(page)
        if page < 1:
            return {"ok": False, "error": "page must be greater than or equal to 1"}

        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

        page_size = OUTPUT_LINE_LIMIT
        total_lines = len(lines)
        total_pages = max(1, (total_lines + page_size - 1) // page_size)
        start = (page - 1) * page_size
        end = start + page_size
        page_lines = lines[start:end] if start < total_lines else []

        return {
            "ok": True,
            "path": path,
            "content": "\n".join(page_lines),
            "page": page,
            "page_size": page_size,
            "total_lines": total_lines,
            "total_pages": total_pages,
            "has_next_page": page < total_pages,
            "next_page": page + 1 if page < total_pages else None,
        }
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
            return _stream_unrestricted(command, timeout=60, allow_privileged=allow_privileged)
        return _run_unrestricted(command, timeout=60, allow_privileged=allow_privileged)

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
                    return _finalize_bash_payload({"ok": False, "error": result_entry["error"], "results": results})

                if result_entry["returncode"] != 0 and not result_entry["stdout"].strip():
                    stderr_detail = result_entry.get("stderr", "").strip()
                    error_msg = f"Command failed: {cmd_str}"
                    if stderr_detail:
                        error_msg += f"\n{stderr_detail}"
                    return _finalize_bash_payload({"ok": False, "error": error_msg, "results": results})

            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"Command timed out: {cmd_str}", "results": results}
            except Exception as e:
                return {"ok": False, "error": str(e), "results": results}

        return _finalize_bash_payload({"ok": True, "command": command, "results": results})

    # ------------------------------------------------------------------ #
    #  Streaming path (restricted)                                         #
    # ------------------------------------------------------------------ #
    def _stream_generator():
        import select
        cap_stream_output = _command_uses_output_file_binary(command)
        streamed_lines = 0

        def should_emit_stream():
            nonlocal streamed_lines
            if not cap_stream_output:
                return True
            if streamed_lines >= OUTPUT_LINE_LIMIT:
                return False
            streamed_lines += 1
            return True

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
                    if should_emit_stream():
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
                        if should_emit_stream():
                            yield {"type": "stream", "fd": "stdout", "line": line}
                if result_entry.get("stderr"):
                    for line in result_entry["stderr"].splitlines():
                        if should_emit_stream():
                            yield {"type": "stream", "fd": "stderr", "line": line}
                results.append(result_entry)
                if result_entry.get("ok") is False:
                    final_payload = _finalize_bash_payload({"ok": False, "error": result_entry["error"], "results": results})
                    yield {"type": "result", **final_payload}
                    return
                if result_entry["returncode"] != 0 and not result_entry["stdout"].strip():
                    stderr_detail = result_entry.get("stderr", "").strip()
                    error_msg = f"Command failed: {cmd_str}"
                    if stderr_detail:
                        error_msg += f"\n{stderr_detail}"
                    final_payload = _finalize_bash_payload({"ok": False, "error": error_msg, "results": results})
                    yield {"type": "result", **final_payload}
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
                    if should_emit_stream():
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
                final_payload = _finalize_bash_payload({"ok": False, "error": error_msg, "results": results})
                yield {"type": "result", **final_payload}
                return

        final_payload = _finalize_bash_payload({"ok": True, "command": command, "results": results})
        yield {"type": "result", **final_payload}

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
