
"""MCP tools definitions for cterm"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import requests

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

def _run_unrestricted(command: str, timeout: int | None = None, allow_privileged: bool = False) -> dict:
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


def _stream_unrestricted(command: str, timeout: int | None = None, allow_privileged: bool = False):
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

    has_deadline = timeout is not None
    deadline = (time.time() + timeout) if has_deadline else float("inf")
    fd_map = {proc.stdout.fileno(): "stdout", proc.stderr.fileno(): "stderr"}

    while fd_map:
        remaining = deadline - time.time()
        if has_deadline and remaining <= 0:
            proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
            yield {"type": "result", "ok": False,
                   "error": f"Command timed out: {command}", "results": []}
            return

        readable, _, exceptional = select.select(
            list(fd_map), [], list(fd_map), min(remaining, 1.0) if has_deadline else 1.0
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
    system_inclusive: bool = False,
    ) -> dict:
    """
    Fast recursive filename finder.

    ```
    Matching is performed against basenames only.

    system_inclusive=False:
        - excludes hidden files/directories
        - excludes common system locations
        - if path == "/", searches only user-content roots

    system_inclusive=True:
        - searches exactly the requested tree
        - includes hidden files/directories

    max_results is a lexical-result cap:
        all matches are collected, sorted, then truncated.
    """

    import fnmatch
    import json
    import os

    def coerce_glob_list(value):
        if value is None:
            return []

        if isinstance(value, list):
            return [str(x) for x in value if str(x)]

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

                return [str(x) for x in parsed if str(x)]

            return [x.strip() for x in text.split(",") if x.strip()]

        return None

    if not pattern or not str(pattern).strip():
        return {
            "ok": False,
            "error": "finder requires an explicit pattern argument",
        }

    include = coerce_glob_list(include)
    if include is None:
        return {
            "ok": False,
            "error": "include must be a list or JSON list string",
        }

    exclude = coerce_glob_list(exclude)
    if exclude is None:
        return {
            "ok": False,
            "error": "exclude must be a list or JSON list string",
        }

    if type_filter not in (None, "file", "dir"):
        return {
            "ok": False,
            "error": 'type_filter must be "file", "dir", or None',
        }

    try:
        if max_depth is not None:
            max_depth = int(max_depth)

            if max_depth < 0:
                raise ValueError

        max_results = int(max_results)

        if max_results <= 0:
            raise ValueError

    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": "invalid numeric argument",
        }

    root = os.path.abspath(os.path.expanduser(path))

    if not os.path.isdir(root):
        return {
            "ok": False,
            "error": f"Not a directory: {path}",
        }

    DEFAULT_EXCLUDE = {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        "node_modules",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".cache",
    }

    matches = []

    def name_matches(name):
        if fnmatch.fnmatch(name, pattern):
            return True

        for p in include:
            if fnmatch.fnmatch(name, p):
                return True

        return False

    def excluded(rel_path, name):
        if name in DEFAULT_EXCLUDE:
            return True

        for pat in exclude:
            if (
                fnmatch.fnmatch(rel_path, pat)
                or fnmatch.fnmatch(name, pat)
            ):
                return True

        return False

    if not system_inclusive and root == "/":
        search_roots = [
            p
            for p in ("/home", "/Users")
            if os.path.isdir(p)
        ]
    else:
        search_roots = [root]

    def walk(base_root, current_dir, depth):
        if (
            max_depth is not None
            and depth > max_depth
        ):
            return

        try:
            entries = os.scandir(current_dir)
        except (PermissionError, FileNotFoundError, OSError):
            return

        with entries:
            for entry in entries:
                name = entry.name

                if not system_inclusive and name.startswith("."):
                    continue

                rel = os.path.relpath(
                    entry.path,
                    base_root,
                ).replace(os.sep, "/")

                if excluded(rel, name):
                    continue

                try:
                    is_dir = entry.is_dir(
                        follow_symlinks=False
                    )
                except OSError:
                    continue

                if is_dir:
                    if (
                        type_filter != "file"
                        and name_matches(name)
                    ):
                        matches.append(rel)

                    walk(
                        base_root,
                        entry.path,
                        depth + 1,
                    )

                else:
                    if type_filter == "dir":
                        continue

                    if not name_matches(name):
                        continue

                    matches.append(rel)

    for search_root in search_roots:
        walk(
            search_root,
            search_root,
            0,
        )

    matches.sort()

    total = len(matches)

    if total > max_results:
        matches = matches[:max_results]
        truncated = True
    else:
        truncated = False

    return {
        "ok": True,
        "path": path,
        "matches": matches,
        "total": total,
        "truncated": truncated,
    }


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
            return _stream_unrestricted(command, allow_privileged=allow_privileged)
        return _run_unrestricted(command, allow_privileged=allow_privileged)

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
                    result_entry = _run_pipeline(argv_list, cmd_str)
                else:
                    result = subprocess.run(
                        argv_list[0].argv,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL if parsed.suppress_stderr else subprocess.PIPE,
                        text=True,
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
                result_entry = _run_pipeline(argv_list, cmd_str)
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

            fd_to_stream = {
                proc.stdout.fileno(): "stdout",
            }
            if not parsed.suppress_stderr:
                fd_to_stream[proc.stderr.fileno()] = "stderr"

            while fd_to_stream:
                readable, _, exceptional = select.select(
                    list(fd_to_stream),
                    [],
                    list(fd_to_stream),
                    1.0,
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

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
MAX_NUM_RESULTS = 20
MAX_RESPONSE_BYTES = 256 * 1024
NO_RESULTS = "No search results found. Please try a different query."


def _websearch_mcp_call(url: str, tool: str, args: dict, headers: dict | None = None) -> tuple[str | None, str | None]:
    """
    Call an MCP tool over HTTP.

    Returns (text, None) on success, or (None, error_message) on failure.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json, text/event-stream")
    req_headers.setdefault("Content-Type", "application/json")
    try:
        resp = requests.post(
            url,
            json=payload,
            headers=req_headers,
            timeout=25,
        )
        resp.raise_for_status()
        body = resp.text
    except requests.exceptions.HTTPError as exc:
        status = resp.status_code if isinstance(exc, requests.exceptions.HTTPError) and hasattr(exc, 'response') and exc.response is not None else 0
        detail = resp.text[:200] if hasattr(exc, 'response') and exc.response is not None else str(exc)
        return None, f"HTTP {status}: {detail}"
    except requests.exceptions.RequestException as exc:
        return None, str(exc)

    if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return None, f"Response exceeded {MAX_RESPONSE_BYTES} bytes"

    text = _parse_mcp_response(body)
    if text is None:
        return None, "Could not parse search results from MCP response"
    return text, None


def _parse_mcp_response(body: str) -> str | None:
    """
    Parse an MCP JSON-RPC response body.

    Accepts both direct JSON and SSE/NDJSON frames (lines starting with 'data: ').
    Returns the first text content block found, or None.
    """
    import json

    def _try_extract(payload: str) -> str | None:
        trimmed = payload.strip()
        if not trimmed.startswith("{"):
            return None
        try:
            parsed = json.loads(trimmed)
        except json.JSONDecodeError:
            return None
        content = parsed.get("result", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("text"):
                    return item["text"]
        return None

    body = body.strip()
    direct = _try_extract(body)
    if direct:
        return direct

    for line in body.splitlines():
        if line.startswith("data: "):
            found = _try_extract(line[6:])
            if found:
                return found

    return None


def _websearch_provider() -> str:
    """Return the configured web search provider, defaulting to 'exa'."""
    config = _read_cterm_config()
    provider = config.get("websearch_provider", "exa")
    if provider not in ("exa", "parallel"):
        return "exa"
    return provider


def _exa_api_key() -> str | None:
    """Read Exa API key from config, then env var."""
    config = _read_cterm_config()
    key = config.get("exa_api_key") or os.environ.get("EXA_API_KEY")
    return key if key else None


def _parallel_api_key() -> str | None:
    """Read Parallel API key from config, then env var."""
    config = _read_cterm_config()
    key = config.get("parallel_api_key") or os.environ.get("PARALLEL_API_KEY")
    return key if key else None


@tool
def websearch(
    query: str,
    num_results: int = 8,
    livecrawl: str = "fallback",
    type: str = "auto",
    context_max_characters: int | None = None,
) -> dict:
    """
    Search the web using the session's local web search provider.

    Supports Exa and Parallel as backends. Provider is selected via the
    'websearch_provider' config key ('exa' or 'parallel'), defaulting to Exa.

    Args:
        query:                  Web search query.
        num_results:            Number of search results to return (1-20, default 8).
        livecrawl:              Live crawl mode: 'fallback' (default) or 'preferred'.
        type:                   Search type: 'auto' (default), 'fast', or 'deep'.
        context_max_characters: Maximum characters for context string (default 10000).

    Returns:
        {"ok": True, "provider": str, "text": str} or {"ok": False, "error": str}
    """
    try:
        num_results = max(1, min(int(num_results), MAX_NUM_RESULTS))
    except (TypeError, ValueError):
        num_results = 8

    if livecrawl not in ("fallback", "preferred"):
        livecrawl = "fallback"

    if type not in ("auto", "fast", "deep"):
        type = "auto"

    if context_max_characters is not None:
        try:
            context_max_characters = max(1, int(context_max_characters))
        except (TypeError, ValueError):
            context_max_characters = None

    provider = _websearch_provider()

    if provider == "exa":
        exa_key = _exa_api_key()
        url = EXA_MCP_URL
        if exa_key:
            url = f"{EXA_MCP_URL}?exaApiKey={exa_key}"

        exa_args = {
            "query": query,
            "type": type,
            "numResults": num_results,
            "livecrawl": livecrawl,
        }
        if context_max_characters is not None:
            exa_args["contextMaxCharacters"] = context_max_characters

        text, _ = _websearch_mcp_call(url, "web_search_exa", exa_args)
        return {
            "ok": True,
            "provider": "exa",
            "text": text or NO_RESULTS,
        }

    else:
        par_key = _parallel_api_key()
        headers = {"User-Agent": "cterm/1.0"}
        if par_key:
            headers["Authorization"] = f"Bearer {par_key}"

        par_args = {
            "objective": query,
            "search_queries": [query],
            "session_id": f"cterm_{os.getpid()}",
        }

        text, _ = _websearch_mcp_call(PARALLEL_MCP_URL, "web_search", par_args, headers)
        return {
            "ok": True,
            "provider": "parallel",
            "text": text or NO_RESULTS,
        }


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
mcp.websearch = websearch
mcp.write_file = write_file
