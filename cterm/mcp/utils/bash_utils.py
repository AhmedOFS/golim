"""Bash command execution for cterm — restricted and unrestricted modes."""

import errno
import glob
import os
import pty
import re
import select
import shlex
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass

from ..config import get_config
from .cancellation import is_tool_cancelled

from ..vars import (
    _BLOCKED_BINARIES,
    _COMMAND_SEPARATORS,
    _PTY_BINARIES,
    FORBIDDEN_CHARS,
    OUTPUT_LINE_LIMIT,
    PRIVILEGED_WRAPPER,
    READ_FILE_PAGE_SIZE,
)


SUBPROCESS_WAIT_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class ResolvedCommand:
    argv: list
    suppress_stderr: bool = False


@dataclass(frozen=True)
class ParsedCommandPart:
    argv_list: list
    suppress_stderr: bool = False


# ---------------------------------------------------------------------------
#  Token-level helpers
# ---------------------------------------------------------------------------

def _is_safe_arg(arg: str) -> bool:
    return not any(c in FORBIDDEN_CHARS for c in arg)


def _expand_supported_vars(token: str) -> str:
    home = os.path.expanduser("~")
    token = token.replace("${HOME}", home).replace("$HOME", home)
    return os.path.expanduser(token)


def _expand_globs(args: list[str]) -> list[str]:
    expanded = []
    for arg in args:
        if any(c in arg for c in ("*", "?", "[")):
            matches = sorted(glob.glob(arg))
            expanded.extend(matches if matches else [arg])
        else:
            expanded.append(arg)
    return expanded


def _strip_supported_redirection(tokens: list[str]) -> tuple[list[str], bool]:
    """Remove the only supported redirection form (`2>/dev/null`)."""
    cleaned, suppress, i = [], False, 0
    while i < len(tokens):
        if tokens[i] == "2>/dev/null":
            suppress = True
        elif (
            tokens[i] == "2>"
            and i + 1 < len(tokens)
            and tokens[i + 1] == "/dev/null"
        ):
            suppress = True
            i += 1
        else:
            cleaned.append(tokens[i])
        i += 1
    return cleaned, suppress


def _split_on_separator(command: str, sep: str) -> list[str]:
    """Split command on sep, respecting double-quoted strings."""
    parts, cur, in_dq, i = [], [], False, 0
    while i < len(command):
        ch = command[i]
        if ch == '"':
            in_dq = not in_dq
            cur.append(ch)
        elif not in_dq and command[i:i + len(sep)] == sep:
            parts.append("".join(cur).strip())
            cur = []
            i += len(sep) - 1
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur).strip())
    return parts


def _split_pipes(command: str) -> list[str]:
    return _split_on_separator(command, "|")


def _split_chained_commands(command: str) -> list[str]:
    return _split_on_separator(command, "&&")


# ---------------------------------------------------------------------------
#  Command segmentation & binary detection (heuristic, shared)
# ---------------------------------------------------------------------------

def _iter_segments(command: str):
    """Coarsely split a command string on shell separators for binary detection.

    Quoted separators inside a segment are not specially handled — this is a
    heuristic used only to identify the leading binary of each segment, mirroring
    the original implementation's behavior.
    """
    text = command
    for sep in _COMMAND_SEPARATORS:
        text = text.replace(sep, "\n")
    for segment in text.splitlines():
        segment = segment.strip()
        if segment:
            yield segment


def _safe_shlex(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _segment_binary(tokens: list[str]):
    """Return (resolved_path, basename) for the leading binary of a token list.

    Skips `sudo` (and its flags), `env` (and `VAR=val` assignments), and the
    `command` builtin. Returns None when no binary can be resolved.
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "sudo":
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
            continue
        if tok == "env" or ("=" in tok and not tok.startswith("=")):
            i += 1
            continue
        if tok == "command":
            i += 1
            continue
        if "/" in tok:
            return tok, os.path.basename(tok)
        resolved = shutil.which(tok)
        if resolved:
            return resolved, os.path.basename(resolved)
        return None, None
    return None, None


def _check_blocked_binary(resolved: str, results_ref: list) -> dict | None:
    name = os.path.basename(resolved)
    if name in _BLOCKED_BINARIES:
        tool_name, hint = _BLOCKED_BINARIES[name]
        return {
            "ok": False,
            "error": f"`{name}` is not available. {hint}",
            "blocked_binary": name,
            "suggested_tool": tool_name,
            "results": results_ref,
        }
    return None


# ---------------------------------------------------------------------------
#  Restricted command parsing
# ---------------------------------------------------------------------------

def _build_cmd(tokens: list[str], results_ref: list, allow_privileged: bool = False):
    privileged = tokens[0] == "sudo"
    if privileged:
        tokens = tokens[1:]
    if not tokens:
        return None, {"ok": False, "error": "Empty command after stripping sudo", "results": results_ref}

    tokens = [_expand_supported_vars(t) for t in tokens]
    binary, args = tokens[0], _expand_globs(tokens[1:])

    for arg in args:
        if not _is_safe_arg(arg):
            return None, {"ok": False, "error": f"Forbidden character in argument: {arg!r}", "results": results_ref}

    if not (resolved := shutil.which(binary)):
        return None, {"ok": False, "error": f"Command not found: {binary}", "results": results_ref}

    if err := _check_blocked_binary(resolved, results_ref):
        return None, err

    if privileged:
        if not os.path.isfile(PRIVILEGED_WRAPPER):
            return None, {
                "ok": False,
                "error": (
                    f"Privileged wrapper not found: {PRIVILEGED_WRAPPER}. "
                    "Install the package's privileged integration first."
                ),
                "results": results_ref,
            }
        if not get_config().is_privileged_binary_allowed(resolved):
            if not allow_privileged:
                return None, {
                    "ok": False,
                    "error": f"Privileged command requires approval: {resolved}",
                    "approval_required": True,
                    "approval_kind": "privileged_whitelist",
                    "binary": resolved,
                    "results": results_ref,
                }
            get_config().add_privileged_binary(resolved)
        return ["sudo", "--non-interactive", PRIVILEGED_WRAPPER, resolved] + args, None

    return [resolved] + args, None


def _build_pipe_procs(pipe_segments: list[str], results_ref: list, allow_privileged: bool = False):
    commands = []
    for seg in pipe_segments:
        seg = seg.strip()
        if not seg:
            return [], {"ok": False, "error": "Empty pipe segment", "results": results_ref}
        try:
            tokens = shlex.split(seg)
        except ValueError as e:
            return [], {"ok": False, "error": f"Command parse error in pipe segment {seg!r}: {e}", "results": results_ref}
        if not tokens:
            return [], {"ok": False, "error": "Empty pipe segment after parsing", "results": results_ref}
        tokens, suppress_stderr = _strip_supported_redirection(tokens)
        cmd, err = _build_cmd(tokens, results_ref, allow_privileged=allow_privileged)
        if err:
            return [], err
        commands.append(ResolvedCommand(cmd, suppress_stderr=suppress_stderr))
    return commands, None


def _parse_command_part(cmd_str: str, results_ref: list, allow_privileged: bool = False):
    pipe_segments = _split_pipes(cmd_str)
    if len(pipe_segments) > 1:
        commands, err = _build_pipe_procs(pipe_segments, results_ref, allow_privileged=allow_privileged)
        if err:
            return None, err
        return ParsedCommandPart(argv_list=commands), None

    try:
        tokens = shlex.split(cmd_str)
    except ValueError as e:
        return None, {"ok": False, "error": f"Command parse error: {e}", "results": results_ref}

    if not tokens:
        return ParsedCommandPart(argv_list=[]), None

    tokens, suppress_stderr = _strip_supported_redirection(tokens)
    if not tokens:
        return ParsedCommandPart(argv_list=[], suppress_stderr=suppress_stderr), None

    cmd, err = _build_cmd(tokens, results_ref, allow_privileged=allow_privileged)
    if err:
        return None, err
    return ParsedCommandPart(argv_list=[ResolvedCommand(cmd)], suppress_stderr=suppress_stderr), None


# ---------------------------------------------------------------------------
#  Pipeline execution (synchronous, non-streaming)
# ---------------------------------------------------------------------------

def _run_pipeline(argv_list: list, cmd_str: str, timeout=None) -> dict:
    procs = []
    try:
        for command in argv_list:
            stdin = procs[-1].stdout if procs else None
            proc = subprocess.Popen(
                command.argv,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL if command.suppress_stderr else subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            if procs:
                procs[-1].stdout.close()
            procs.append(proc)

        last = procs[-1]
        started = time.monotonic()
        while True:
            if is_tool_cancelled():
                for p in procs:
                    _terminate_process_group(p)
                return {"ok": False, "error": "Tool execution cancelled"}
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                for p in procs:
                    _terminate_process_group(p)
                return {"ok": False, "error": f"Pipeline timed out: {cmd_str}"}
            try:
                stdout_data, stderr_data = last.communicate(
                    timeout=0.1 if remaining is None else min(0.1, remaining),
                )
                break
            except subprocess.TimeoutExpired:
                continue

        for p in procs[:-1]:
            while p.poll() is None:
                if is_tool_cancelled():
                    for proc in procs:
                        _terminate_process_group(proc)
                    return {"ok": False, "error": "Tool execution cancelled"}
                try:
                    p.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
            if p.stderr:
                p.stderr.close()
        for stream in (last.stdout, last.stderr):
            if stream:
                stream.close()

        return {"command": cmd_str, "stdout": stdout_data, "stderr": stderr_data, "returncode": last.returncode}
    except Exception as e:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait()
            except Exception:
                pass
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
#  Config & output finalization
# ---------------------------------------------------------------------------

def _is_bash_unrestricted() -> bool:
    return get_config().unrestricted_bash


def _count_output_lines(results: list[dict]) -> int:
    return sum(
        len(str(e.get("stdout", "")).splitlines()) + len(str(e.get("stderr", "")).splitlines())
        for e in results
    )


def _truncate_text(text: str, limit: int) -> str:
    """Keep the first limit/2 and last limit/2 lines, with a marker between."""
    lines = str(text).splitlines()
    if len(lines) <= limit:
        return str(text)
    half = limit // 2
    omitted = len(lines) - limit
    return "\n".join(
        lines[:half]
        + [f"... [{omitted} lines truncated, {half} head + {half} tail kept] ..."]
        + lines[-half:]
    )


def _finalize_bash_payload(payload: dict) -> dict:
    """Truncate the final result's stdout/stderr to OUTPUT_LINE_LIMIT lines
    (first half + last half) when it exceeds the limit. Only applied to the
    final result frame, never to intermediate stream frames."""
    results = payload.get("results")
    if not isinstance(results, list):
        return payload

    line_count = _count_output_lines(results)
    if line_count <= OUTPUT_LINE_LIMIT:
        return {**payload, "output_line_count": line_count, "output_limit": OUTPUT_LINE_LIMIT, "output_truncated": False}

    truncated = []
    for entry in results:
        copied = dict(entry)
        copied["stdout"] = _truncate_text(copied.get("stdout", ""), OUTPUT_LINE_LIMIT)
        copied["stderr"] = _truncate_text(copied.get("stderr", ""), OUTPUT_LINE_LIMIT)
        truncated.append(copied)

    return {
        **payload,
        "results": truncated,
        "output_line_count": line_count,
        "output_limit": OUTPUT_LINE_LIMIT,
        "output_truncated": True,
    }


# ---------------------------------------------------------------------------
#  PTY detection
# ---------------------------------------------------------------------------

def _uses_privileged_wrapper(argv: list[str]) -> bool:
    return len(argv) >= 4 and argv[0] == "sudo" and argv[2] == PRIVILEGED_WRAPPER


def _requires_pty_streaming(argv: list[str]) -> bool:
    return _uses_privileged_wrapper(argv) and os.path.basename(argv[3]) in _PTY_BINARIES


def _command_requires_pty_streaming(command: str) -> bool:
    for segment in _iter_segments(command):
        tokens = _safe_shlex(segment)
        if _requires_pty_streaming(tokens):
            return True
    return False


# ---------------------------------------------------------------------------
#  Streaming subprocess helper (unified for pty and pipe modes)
#  Yields stream frames; returns (entry_dict, error_dict) via StopIteration.
# ---------------------------------------------------------------------------

def _append_stream_text(stream_name, text, pending, output_lines):
    """Split decoded text into complete lines, emit stream frames, keep the
    incomplete trailing piece in `pending`. Returns the new pending value."""
    pending += text.replace("\r\n", "\n").replace("\r", "\n")
    lines = pending.split("\n")
    pending = lines.pop()  # possibly-incomplete trailing piece
    for line in lines:
        output_lines.append(line)
        yield {"type": "stream", "fd": stream_name, "line": line, "end": "\n"}
    return pending


def _terminate_process_group(proc):
    """Terminate a streamed command and its inherited child processes."""
    if proc.poll() is not None:
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (AttributeError, OSError):
        try:
            proc.terminate()
        except OSError:
            return

    try:
        proc.wait(timeout=SUBPROCESS_WAIT_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, OSError):
        try:
            proc.kill()
        except OSError:
            return

    try:
        proc.wait(timeout=SUBPROCESS_WAIT_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _stream_subprocess(argv, cmd_str, results, timeout, suppress_stderr, use_pty):
    output_lines = {"stdout": [], "stderr": []}
    pending = {"stdout": "", "stderr": ""}
    fd_to_stream = {}  # fd -> stream_name
    master_fd = None
    proc = None
    slave_fd = None
    completed = False

    try:
        if use_pty:
            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd if not suppress_stderr else subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = None
            fd_to_stream = {master_fd: "stdout"}
        else:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL if suppress_stderr else subprocess.PIPE,
                bufsize=0,
                close_fds=True,
                start_new_session=True,
            )
            fd_to_stream = {proc.stdout.fileno(): "stdout"}
            if not suppress_stderr:
                fd_to_stream[proc.stderr.fileno()] = "stderr"
    except Exception as e:
        for fd in (slave_fd, master_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return None, {"ok": False, "error": str(e), "results": results}

    deadline = time.monotonic() + timeout if timeout is not None else float("inf")

    try:
        while fd_to_stream:
            if is_tool_cancelled():
                return None, {
                    "ok": False,
                    "error": "Tool execution cancelled",
                    "results": results,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                return None, {"ok": False, "error": f"Command timed out: {cmd_str}", "results": results}

            readable, _, exceptional = select.select(
                list(fd_to_stream), [], list(fd_to_stream), min(1.0, remaining)
            )
            for fd in exceptional:
                fd_to_stream.pop(fd, None)

            for fd in readable:
                stream_name = fd_to_stream.get(fd)
                if not stream_name:
                    continue
                try:
                    data = os.read(fd, 4096)
                except OSError as e:
                    if use_pty and e.errno == errno.EIO:
                        fd_to_stream.pop(fd, None)
                        continue
                    fd_to_stream.pop(fd, None)
                    return None, {"ok": False, "error": str(e), "results": results}
                if not data:
                    fd_to_stream.pop(fd, None)
                    continue
                pending[stream_name] = yield from _append_stream_text(
                    stream_name, data.decode(errors="replace"),
                    pending[stream_name], output_lines[stream_name],
                )

        # Flush any trailing partial line.
        for stream_name, chunk in pending.items():
            if chunk:
                output_lines[stream_name].append(chunk)
                yield {"type": "stream", "fd": stream_name, "line": chunk, "end": "\n"}
        completed = True

    finally:
        if proc is not None:
            try:
                if not completed and proc.poll() is None:
                    _terminate_process_group(proc)
                elif proc.poll() is None:
                    proc.wait(timeout=SUBPROCESS_WAIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                _terminate_process_group(proc)
            except OSError:
                pass
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass

    return {
        "command": cmd_str,
        "stdout": "\n".join(output_lines["stdout"]),
        "stderr": "" if suppress_stderr or use_pty else "\n".join(output_lines["stderr"]),
        "returncode": proc.returncode,
    }, None


def _stream_command_with_pty(argv, cmd_str, results, timeout=None, suppress_stderr=False):
    """Stream a single command through a pseudo-terminal (for apt/snap/etc.
    that detect a tty before prompting)."""
    return _stream_subprocess(
        argv, cmd_str, results, timeout, suppress_stderr, use_pty=True,
    )


def _stream_command(argv, cmd_str, results, timeout, suppress_stderr):
    """Stream a single command through regular pipes."""
    return _stream_subprocess(
        argv, cmd_str, results, timeout, suppress_stderr, use_pty=False,
    )


def _restricted_failure_payload(cmd_str, result_entry, results):
    if result_entry.get("ok") is False:
        return {"ok": False, "error": result_entry["error"], "results": results}
    if result_entry["returncode"] != 0 and not result_entry["stdout"].strip():
        error_msg = f"Command failed: {cmd_str}"
        if stderr := result_entry.get("stderr", "").strip():
            error_msg += f"\n{stderr}"
        return {"ok": False, "error": error_msg, "results": results}
    return None


# ---------------------------------------------------------------------------
#  Restricted execution (single generator drives both streaming and run modes)
# ---------------------------------------------------------------------------

def _exec_restricted(command, timeout=120, allow_privileged=False):
    """Generator: yields stream frames then a final result frame."""
    results = []

    commands = _split_chained_commands(command)
    for command_index, cmd_str in enumerate(commands):
        parsed, err = _parse_command_part(cmd_str, results, allow_privileged=allow_privileged)
        if err:
            if err.get("approval_required"):
                # Earlier links have already run.  The client must retry only
                # the unexecuted suffix after approval, never the whole chain.
                err["retry_command"] = " && ".join(commands[command_index:])
            yield {"type": "result", **err}
            return

        if not parsed.argv_list:
            continue

        if len(parsed.argv_list) == 1:
            rc = parsed.argv_list[0]
            suppress = parsed.suppress_stderr or rc.suppress_stderr
            streamer = _stream_command_with_pty if _requires_pty_streaming(rc.argv) else _stream_command
            entry, err = yield from streamer(
                rc.argv, cmd_str, results, timeout=timeout,
                suppress_stderr=suppress,
            )
            if err:
                yield {"type": "result", **err}
                return
            if entry is None:
                continue
        else:
            entry = _run_pipeline(parsed.argv_list, cmd_str, timeout=timeout)
            if entry.get("ok") is False:
                yield {"type": "result", **entry, "results": results}
                return
            for fd in ("stdout", "stderr"):
                if not entry.get(fd):
                    continue
                for line in entry[fd].splitlines():
                    yield {"type": "stream", "fd": fd, "line": line, "end": "\n"}

        results.append(entry)
        if failure := _restricted_failure_payload(cmd_str, entry, results):
            yield {"type": "result", **_finalize_bash_payload(failure)}
            return

    yield {"type": "result", **_finalize_bash_payload({"ok": True, "command": command, "results": results})}


# ---------------------------------------------------------------------------
#  Unrestricted execution
# ---------------------------------------------------------------------------

def _prepare_unrestricted(command: str, allow_privileged: bool) -> tuple[str | None, dict | None]:
    sudo_replacements = []
    for m in re.finditer(r'(?<![^\s])sudo\s+(\S+)', command):
        binary_token = m.group(1)
        if binary_token.startswith("-"):
            continue
        if not (resolved := shutil.which(binary_token)):
            continue
        if not get_config().is_privileged_binary_allowed(resolved):
            if not allow_privileged:
                return None, {
                    "ok": False,
                    "error": f"Privileged command requires approval: {resolved}",
                    "approval_required": True,
                    "approval_kind": "privileged_whitelist",
                    "binary": resolved,
                    "results": [],
                }
            get_config().add_privileged_binary(resolved)
        sudo_replacements.append((m.start(), m.end(), resolved))

    if sudo_replacements:
        parts, last_end = [], 0
        for start, end, resolved in sudo_replacements:
            parts.append(command[last_end:start])
            parts.append(f"sudo --non-interactive {PRIVILEGED_WRAPPER} {resolved}")
            last_end = end
        parts.append(command[last_end:])
        command = "".join(parts)

    for segment in _iter_segments(command):
        tokens = _safe_shlex(segment)
        if not tokens:
            continue
        _, name = _segment_binary(tokens)
        if name in _BLOCKED_BINARIES:
            _, hint = _BLOCKED_BINARIES[name]
            return None, {"ok": False, "error": f"`{name}` is not available. {hint}", "results": []}

    return command, None


def _exec_unrestricted(command, timeout=None, allow_privileged=False):
    """Generator: yields stream frames then a final result frame."""
    command, err = _prepare_unrestricted(command, allow_privileged)
    if err:
        yield {"type": "result", **err}
        return

    argv = ["/bin/bash", "-c", command]
    results = []

    streamer = _stream_command_with_pty if _command_requires_pty_streaming(command) else _stream_command
    entry, err = yield from streamer(
        argv, command, results, timeout=timeout, suppress_stderr=False,
    )
    if err:
        yield {"type": "result", **err}
        return

    ok = entry["returncode"] == 0 or bool(entry["stdout"].strip())
    yield {"type": "result", **_finalize_bash_payload({"ok": ok, "command": command, "results": [entry]})}


# ---------------------------------------------------------------------------
#  Public API — thin wrappers preserving original function signatures
# ---------------------------------------------------------------------------

def _run_restricted(command, timeout=120, allow_privileged=False):
    for frame in _exec_restricted(command, timeout=timeout, allow_privileged=allow_privileged):
        if frame.get("type") == "result":
            return {k: v for k, v in frame.items() if k != "type"}
    return {"ok": False, "error": "No result from bash execution"}


def _run_unrestricted(command, timeout=None, allow_privileged=False):
    for frame in _exec_unrestricted(command, timeout=timeout, allow_privileged=allow_privileged):
        if frame.get("type") == "result":
            return {k: v for k, v in frame.items() if k != "type"}
    return {"ok": False, "error": "No result from bash execution"}


def _stream_restricted(command, timeout=120, allow_privileged=False):
    return _exec_restricted(command, timeout=timeout, allow_privileged=allow_privileged)


def _stream_unrestricted(command, timeout=None, allow_privileged=False):
    return _exec_unrestricted(command, timeout=timeout, allow_privileged=allow_privileged)
