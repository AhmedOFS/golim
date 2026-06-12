"""Utility helpers shared by cterm modules."""

import glob
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass

try:
    from privilege import (
        add_privileged_binary,
        is_privileged_binary_allowed,
    )
except ImportError:
    from .privilege import (
        add_privileged_binary,
        is_privileged_binary_allowed,
    )

# The privileged wrapper installed by dev_setup.sh / the .deb package.
PRIVILEGED_WRAPPER = "/usr/lib/cterm/cterm-privileged"

FORBIDDEN_CHARS = set("><`\\'()")

# Binaries that are blocked in favour of a cterm tool equivalent.
# Maps resolved binary name -> (tool_name, usage_hint)
_BLOCKED_BINARIES = {
    # "find": (
    #     "finder",
    #     "Use the `finder` tool instead of the `find` command. "
    #     "Example: finder(path=\".\", pattern=\"*.py\")",
    # ),
}


@dataclass(frozen=True)
class ParsedCommandPart:
    argv_list: list[list[str]]
    suppress_stderr: bool = False


@dataclass(frozen=True)
class ResolvedCommand:
    argv: list[str]
    suppress_stderr: bool = False


def _is_safe_arg(arg: str) -> bool:
    """Reject arguments containing shell metacharacters."""
    return not any(c in FORBIDDEN_CHARS for c in arg)


def _expand_supported_vars(token: str) -> str:
    home = os.path.expanduser("~")
    if token == "$HOME":
        return home
    if token.startswith("$HOME/"):
        return home + token[len("$HOME"):]
    if token == "${HOME}":
        return home
    if token.startswith("${HOME}/"):
        return home + token[len("${HOME}"):]
    if token == "~" or token.startswith("~/"):
        return os.path.expanduser(token)
    return os.path.expandvars(token)


def _expand_globs(args: list[str]) -> list[str]:
    expanded: list[str] = []
    for arg in args:
        if any(c in arg for c in ("*", "?", "[")):
            matches = sorted(glob.glob(arg))
            expanded.extend(matches if matches else [arg])
        else:
            expanded.append(arg)
    return expanded


def _strip_supported_redirection(tokens: list[str]) -> tuple[list[str], bool, dict | None]:
    cleaned = []
    suppress_stderr = False
    idx = 0

    while idx < len(tokens):
        token = tokens[idx]
        if token == "2>/dev/null":
            suppress_stderr = True
            idx += 1
            continue
        if token == "2>" and idx + 1 < len(tokens) and tokens[idx + 1] == "/dev/null":
            suppress_stderr = True
            idx += 2
            continue
        cleaned.append(token)
        idx += 1

    return cleaned, suppress_stderr, None


def _check_blocked_binary(resolved: str, results_ref: list) -> dict | None:
    """
    Return an error dict if *resolved* is a blocked binary, otherwise None.

    The binary name is matched against _BLOCKED_BINARIES by basename so that
    absolute paths like /usr/bin/find are caught as well as bare `find`.
    """
    name = os.path.basename(resolved)
    if name in _BLOCKED_BINARIES:
        tool_name, hint = _BLOCKED_BINARIES[name]
        return {
            "ok": False,
            "error": (
                f"`{name}` is not available. {hint}"
            ),
            "blocked_binary": name,
            "suggested_tool": tool_name,
            "results": results_ref,
        }
    return None


def _build_cmd(
    tokens: list[str],
    results_ref: list,
    allow_privileged: bool = False,
) -> tuple[list[str] | None, dict | None]:
    """Resolve a token list to argv, including cterm's privileged routing."""
    privileged = False
    if tokens[0] == "sudo":
        privileged = True
        tokens = tokens[1:]
    if not tokens:
        return None, {"ok": False, "error": "Empty command after stripping sudo", "results": results_ref}

    tokens = [_expand_supported_vars(token) for token in tokens]
    binary = tokens[0]
    args = _expand_globs(tokens[1:])

    for arg in args:
        if not _is_safe_arg(arg):
            return None, {
                "ok": False,
                "error": f"Forbidden character in argument: {arg!r}",
                "results": results_ref,
            }

    resolved = shutil.which(binary)
    if not resolved:
        return None, {"ok": False, "error": f"Command not found: {binary}", "results": results_ref}

    # Block binaries that have a cterm tool equivalent.
    blocked_err = _check_blocked_binary(resolved, results_ref)
    if blocked_err:
        return None, blocked_err

    if privileged:
        if not os.path.isfile(PRIVILEGED_WRAPPER):
            return None, {
                "ok": False,
                "error": (
                    f"Privileged wrapper not found: {PRIVILEGED_WRAPPER}. "
                    "Run sudocterm.sh install."
                ),
                "results": results_ref,
            }
        if not is_privileged_binary_allowed(resolved):
            if not allow_privileged:
                return None, {
                    "ok": False,
                    "error": f"Privileged command requires approval: {resolved}",
                    "approval_required": True,
                    "approval_kind": "privileged_whitelist",
                    "binary": resolved,
                    "results": results_ref,
                }
            add_privileged_binary(resolved)
        return ["sudo", "--non-interactive", PRIVILEGED_WRAPPER, resolved] + args, None

    return [resolved] + args, None


def _split_pipes(command: str) -> list[str]:
    segments = []
    current = []
    in_dquote = False

    for ch in command:
        if ch == '"':
            in_dquote = not in_dquote
            current.append(ch)
        elif ch == '|' and not in_dquote:
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)

    segments.append("".join(current))
    return segments


def _split_chained_commands(command: str) -> list[str]:
    segments = []
    current = []
    in_dquote = False
    idx = 0

    while idx < len(command):
        ch = command[idx]
        if ch == '"':
            in_dquote = not in_dquote
            current.append(ch)
            idx += 1
            continue
        if ch == "&" and not in_dquote and command[idx:idx + 2] == "&&":
            segments.append("".join(current).strip())
            current = []
            idx += 2
            continue
        current.append(ch)
        idx += 1

    segments.append("".join(current).strip())
    return segments


def _build_pipe_procs(
    pipe_segments: list[str],
    results_ref: list,
    allow_privileged: bool = False,
) -> tuple[list[ResolvedCommand], dict | None]:
    commands = []

    for seg in pipe_segments:
        seg = seg.strip()
        if not seg:
            return [], {"ok": False, "error": "Empty pipe segment", "results": results_ref}

        try:
            tokens = shlex.split(seg)
        except ValueError as e:
            return [], {
                "ok": False,
                "error": f"Command parse error in pipe segment {seg!r}: {e}",
                "results": results_ref,
            }

        if not tokens:
            return [], {"ok": False, "error": "Empty pipe segment after parsing", "results": results_ref}

        tokens, suppress_stderr, _ = _strip_supported_redirection(tokens)
        cmd, err = _build_cmd(tokens, results_ref, allow_privileged=allow_privileged)
        if err:
            return [], err
        commands.append(ResolvedCommand(cmd, suppress_stderr=suppress_stderr))

    return commands, None


def _parse_command_part(
    cmd_str: str,
    results_ref: list,
    allow_privileged: bool = False,
) -> tuple[ParsedCommandPart | None, dict | None]:
    pipe_segments = _split_pipes(cmd_str)
    if len(pipe_segments) > 1:
        commands, err = _build_pipe_procs(
            pipe_segments,
            results_ref,
            allow_privileged=allow_privileged,
        )
        if err:
            return None, err
        return ParsedCommandPart(argv_list=commands), None

    try:
        tokens = shlex.split(cmd_str)
    except ValueError as e:
        return None, {"ok": False, "error": f"Command parse error: {e}", "results": results_ref}

    if not tokens:
        return ParsedCommandPart(argv_list=[]), None

    tokens, suppress_stderr, _ = _strip_supported_redirection(tokens)
    if not tokens:
        return ParsedCommandPart(argv_list=[], suppress_stderr=suppress_stderr), None

    cmd, err = _build_cmd(tokens, results_ref, allow_privileged=allow_privileged)
    if err:
        return None, err
    return ParsedCommandPart(argv_list=[ResolvedCommand(cmd)], suppress_stderr=suppress_stderr), None


def _run_pipeline(argv_list: list[ResolvedCommand], cmd_str: str, timeout: int | None = None) -> dict:
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
            )
            if procs:
                procs[-1].stdout.close()
            procs.append(proc)

        last = procs[-1]
        try:
            stdout_data, stderr_data = last.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            for proc in procs:
                proc.kill()
            return {"ok": False, "error": f"Pipeline timed out: {cmd_str}"}

        for proc in procs[:-1]:
            proc.wait()
            if proc.stderr:
                proc.stderr.close()
        if last.stdout:
            last.stdout.close()
        if last.stderr:
            last.stderr.close()

        return {
            "command": cmd_str,
            "stdout": stdout_data,
            "stderr": stderr_data,
            "returncode": last.returncode,
        }
    except Exception as e:
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                pass
        return {"ok": False, "error": str(e)}