"""Bash command execution for openterm — restricted and unrestricted modes."""

import errno
import os
import pty
import re
import select
import shlex
import signal
import shutil
import subprocess

from ..config import get_config
from .cancellation import is_tool_cancelled

from ..vars import (
    _BLOCKED_BINARIES,
    _COMMAND_SEPARATORS,
    _PTY_BINARIES,
    OUTPUT_LINE_LIMIT,
    PRIVILEGED_WRAPPER,
    TOKEN_ENV_VAR,
)


SUBPROCESS_WAIT_TIMEOUT_SECONDS = 1.0


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


# sudo options that cannot affect execution through the privileged wrapper and
# are dropped instead of being treated as the target binary.
_SUDO_BOOLEAN_OPTIONS = frozenset({
    "-n", "--non-interactive",  # the wrapper call is never interactive
    "-k", "-K",                 # dropped; the token is supplied per call
    "-E", "--preserve-env",     # environment preservation is not forwarded
})
# sudo options that change the execution target; dropping them would silently
# change what the command does, so they are rejected with a clear error.
_SUDO_TARGET_OPTIONS = frozenset({"-u", "--user", "-g", "--group"})
# Short letters that are harmless alongside a privilege-listing invocation.
_SUDO_LIST_SHORT_LETTERS = frozenset("nkKEl")
_SUDO_LIST_INVOCATION_RE = re.compile(
    r"(?:^\s*|(?<=[;&|()\n])\s*)sudo"
    r"(?P<options>(?:\s+(?:--|-{1,2}[A-Za-z][A-Za-z=-]*))*)(?=\s|$)"
)
_SUDO_INVOCATION_RE = re.compile(
    r"(?P<prefix>^\s*|(?<=[;&|()\n])\s*)sudo"
    r"(?P<options>(?:\s+(?:--|-{1,2}[A-Za-z][A-Za-z=-]*))*)"
    r"\s+(?P<binary>\S+)"
)


def _sudo_list_invocation(command: str) -> bool:
    """Detect `sudo` calls that only list privileges (-l/-ll/--list), possibly
    combined with harmless boolean flags such as -n."""
    for match in _SUDO_LIST_INVOCATION_RE.finditer(command):
        saw_list_flag = False
        list_only = True
        for token in match.group("options").split():
            base = token.split("=", 1)[0]
            if base == "--":
                break
            if base in _SUDO_BOOLEAN_OPTIONS:
                continue
            if base in ("-l", "-ll", "--list"):
                saw_list_flag = True
                continue
            letters = base[1:]
            if (
                base.startswith("-")
                and not base.startswith("--")
                and len(letters) > 1
                and set(letters) <= _SUDO_LIST_SHORT_LETTERS
            ):
                if "l" in letters:
                    saw_list_flag = True
                continue
            list_only = False
            break
        if list_only and saw_list_flag:
            return True
    return False


def _privileged_list_notice_payload(command: str) -> dict:
    message = (
        "Privileged commands are routed through the openterm wrapper; "
        "the user authenticates sudo once per session and approves each "
        "command. There is no need to probe with sudo -l."
    )
    return {
        "ok": True,
        "command": command,
        "results": [
            {
                "command": command.strip(),
                "stdout": message,
                "stderr": "",
                "returncode": 0,
            }
        ],
    }


# ---------------------------------------------------------------------------
#  Config & output finalization
# ---------------------------------------------------------------------------

def _is_unrestricted_mode() -> bool:
    return get_config().unrestricted_mode


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
    return len(argv) >= 3 and argv[0] == "sudo" and PRIVILEGED_WRAPPER in argv


def _requires_pty_streaming(argv: list[str]) -> bool:
    if not _uses_privileged_wrapper(argv):
        return False
    wrapper_index = argv.index(PRIVILEGED_WRAPPER)
    return (
        wrapper_index + 1 < len(argv)
        and os.path.basename(argv[wrapper_index + 1]) in _PTY_BINARIES
    )


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


def _stream_subprocess(argv, cmd_str, results, suppress_stderr, use_pty, session_token=None):
    output_lines = {"stdout": [], "stderr": []}
    pending = {"stdout": "", "stderr": ""}
    fd_to_stream = {}  # fd -> stream_name
    master_fd = None
    proc = None
    slave_fd = None
    completed = False

    # The session token is forwarded to privileged wrapper invocations via
    # the environment (sudoers env_keep); nothing travels through stdin,
    # so pipelines keep working unmodified.
    env = (
        {**os.environ, TOKEN_ENV_VAR: session_token}
        if session_token is not None
        else None
    )

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
                env=env,
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
                env=env,
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

    try:
        while fd_to_stream:
            if is_tool_cancelled():
                return None, {
                    "ok": False,
                    "error": "Tool execution cancelled",
                    "results": results,
                }

            readable, _, exceptional = select.select(
                list(fd_to_stream), [], list(fd_to_stream), 1.0
            )
            for fd in exceptional:
                fd_to_stream.pop(fd, None)

            if not readable and not exceptional and proc.poll() is not None:
                # The child exited and its output is fully drained; close
                # the master so the loop can finish without waiting for an
                # EIO that may never arrive.
                fd_to_stream.clear()
                break

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


def _stream_command_with_pty(argv, cmd_str, results, suppress_stderr=False, session_token=None):
    """Stream a single command through a pseudo-terminal (for apt/snap/etc.
    that detect a tty before prompting)."""
    return _stream_subprocess(
        argv, cmd_str, results, suppress_stderr, use_pty=True,
        session_token=session_token,
    )


def _stream_command(argv, cmd_str, results, suppress_stderr, session_token=None):
    """Stream a single command through regular pipes."""
    return _stream_subprocess(
        argv, cmd_str, results, suppress_stderr, use_pty=False,
        session_token=session_token,
    )


def _request_privileged_approval(approve_privileged, err, cmd_str):
    """Ask the client to approve a privileged binary and return the decision.

    The server updates the whitelist before reporting a positive decision, so
    callers simply re-prepare the command afterwards. Without an approval
    channel the request is denied.
    """
    if approve_privileged is None:
        return False
    return bool(approve_privileged({
        "approval_kind": err.get("approval_kind", "privileged_whitelist"),
        "binary": err.get("binary", ""),
        "binaries": err.get("binaries", []),
        "command": cmd_str,
    }))


def _privileged_denied_payload(err, results):
    return {
        "ok": False,
        "error": f"Privileged command not approved: {err.get('binary', '')}",
        "results": results,
    }


def _resolve_session_token(session_token):
    """Return the session token from the injected provider.

    The MCP server injects a zero-arg callable (which may block on the
    out-of-band auth exchange); tests and direct callers may pass a plain
    string. Anything resolving falsy means no credentials are available.
    """
    if callable(session_token):
        return session_token()
    return session_token


def _prepare_shell_command(
    command: str,
    always_approve: bool = False,
    privileged_approved: bool = False,
) -> tuple[str | None, dict | None]:
    """Rewrite sudo invocations while leaving all other shell syntax intact.

    The shell is deliberately not parsed here. The small amount of inspection
    is only to keep sudo invocations on the wrapper path and to validate the
    options that the wrapper cannot represent.
    """
    sudo_replacements = []
    privileged_binaries = []
    invocations = list(_SUDO_INVOCATION_RE.finditer(command))
    for m in invocations:
        for token in m.group("options").split():
            base = token.split("=", 1)[0]
            if base in _SUDO_TARGET_OPTIONS:
                return None, {
                    "ok": False,
                    "error": f"sudo {base} is not supported through the privileged wrapper",
                    "results": [],
                }
            if token != "--" and base not in _SUDO_BOOLEAN_OPTIONS:
                return None, {
                    "ok": False,
                    "error": f"Unsupported sudo option: {token}",
                    "results": [],
                }
        binary_token = m.group("binary")
        if not (resolved := shutil.which(binary_token)):
            return None, {
                "ok": False,
                "error": f"Command not found: {binary_token}",
                "results": [],
            }
        if err := _check_blocked_binary(resolved, []):
            return None, err
        if not os.path.isfile(PRIVILEGED_WRAPPER):
            return None, {
                "ok": False,
                "error": (
                    "Privileged execution is unavailable because the "
                    "privileged integration is not installed on this system."
                ),
                "results": [],
            }
        privileged_binaries.append(resolved)

    if always_approve and privileged_binaries and not privileged_approved:
        return None, {
            "ok": False,
            "error": f"Privileged command requires approval: {privileged_binaries[0]}",
            "approval_required": True,
            "approval_kind": "privileged_whitelist",
            "binary": privileged_binaries[0],
            "binaries": privileged_binaries,
            "results": [],
        }

    for resolved in privileged_binaries:
        if not always_approve and not get_config().is_privileged_binary_allowed(resolved):
            return None, {
                "ok": False,
                "error": f"Privileged command requires approval: {resolved}",
                "approval_required": True,
                "approval_kind": "privileged_whitelist",
                "binary": resolved,
                "results": [],
            }

    for m, resolved in zip(invocations, privileged_binaries):
        sudo_replacements.append((m.start(), m.end("binary"), resolved, m.group("prefix")))

    if sudo_replacements:
        parts, last_end = [], 0
        for start, end, resolved, prefix in sudo_replacements:
            parts.append(command[last_end:start])
            parts.append(f"{prefix}sudo -n {PRIVILEGED_WRAPPER} {resolved}")
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


def _exec_shell(
    command,
    approve_privileged=None,
    session_token=None,
    always_approve=False,
):
    """Generator: yields stream frames then a final result frame."""
    if _sudo_list_invocation(command):
        yield {"type": "result", **_finalize_bash_payload(_privileged_list_notice_payload(command))}
        return

    prepared, err = _prepare_shell_command(command, always_approve=always_approve)
    seen = set()
    approval_granted = False
    while err and err.get("approval_required"):
        binary = err.get("binary", "")
        if always_approve:
            if approval_granted:
                yield {
                    "type": "result",
                    "ok": False,
                    "error": f"Privileged command failed after approval: {binary}",
                    "results": [],
                }
                return
        elif binary in seen:
            yield {
                "type": "result",
                "ok": False,
                "error": f"Privileged command failed after approval: {binary}",
                "results": [],
            }
            return

        seen.add(binary)
        if not _request_privileged_approval(approve_privileged, err, command):
            yield {"type": "result", **_privileged_denied_payload(err, [])}
            return
        approval_granted = True
        prepared, err = _prepare_shell_command(
            command,
            always_approve=always_approve,
            privileged_approved=always_approve,
        )
    if err:
        yield {"type": "result", **err}
        return

    token = None
    if PRIVILEGED_WRAPPER in prepared:
        token = _resolve_session_token(session_token)
        if not token:
            # Fail closed: no session token available, so the broker would
            # refuse the wrapper invocation.
            yield {
                "type": "result",
                "ok": False,
                "error": "sudo authentication required; the user must authenticate this session",
                "results": [],
            }
            return

    argv = ["/bin/bash", "-c", prepared]
    results = []

    streamer = _stream_command_with_pty if _command_requires_pty_streaming(prepared) else _stream_command
    entry, err = yield from streamer(
        argv, command, results, suppress_stderr=False,
        session_token=token,
    )
    if err:
        yield {"type": "result", **err}
        return

    ok = entry["returncode"] == 0 or bool(entry["stdout"].strip())
    payload = {"ok": ok, "command": command, "results": [entry]}
    if not ok:
        error = f"Command failed: {command}"
        if stderr := entry.get("stderr", "").strip():
            error += f"\n{stderr}"
        payload["error"] = error
    yield {"type": "result", **_finalize_bash_payload(payload)}


# ---------------------------------------------------------------------------
#  Public API — thin wrappers
# ---------------------------------------------------------------------------

def _run_shell(command, approve_privileged=None, session_token=None, always_approve=False):
    for frame in _exec_shell(
        command,
        approve_privileged=approve_privileged,
        session_token=session_token,
        always_approve=always_approve,
    ):
        if frame.get("type") == "result":
            return {k: v for k, v in frame.items() if k != "type"}
    return {"ok": False, "error": "No result from bash execution"}


def _stream_shell(command, approve_privileged=None, session_token=None, always_approve=False):
    return _exec_shell(
        command,
        approve_privileged=approve_privileged,
        session_token=session_token,
        always_approve=always_approve,
    )
