"""Utility helpers shared by cterm modules."""

import os
import shlex
import shutil
import subprocess

# The privileged wrapper installed by dev_setup.sh / the .deb package.
# Sudoers grants NOPASSWD only for this wrapper, not for apt/snap directly.
# This means the user still needs a password for sudo snap in their own terminal.
PRIVILEGED_WRAPPER = "/usr/lib/cterm/cterm-privileged"

# Binaries the wrapper is allowed to execute (must match the wrapper's ALLOWED list)
PRIVILEGED_WHITELIST = {
    "apt", "apt-get", "tee", "snap",
}

BINARY_PATHS = {
    "apt": "/usr/bin/apt",
    "apt-get": "/usr/bin/apt-get",
    "tee": "/usr/bin/tee",
    "snap": "/usr/bin/snap",
}

# Characters never allowed in any argument.
# Note: `"` is excluded because shlex.split already handles quoting safely -
# by the time we see individual tokens, quotes have been consumed/resolved.
# `|` is excluded because we handle pipe segments ourselves via _split_pipes().
FORBIDDEN_CHARS = set("><`$\\'()")


def _is_safe_arg(arg: str) -> bool:
    """Reject arguments containing shell metacharacters."""
    return not any(c in FORBIDDEN_CHARS for c in arg)


def _split_pipes(command: str) -> list[str]:
    """
    Split a command string on unquoted `|` characters into pipe segments.

    Uses a simple state machine so that pipes inside double-quoted strings
    (e.g.  echo "hello | world" | cat) are NOT treated as pipe operators.
    Returns a list of raw segment strings, e.g.:
        'ls -la | grep foo | wc -l'  ->  ['ls -la ', ' grep foo ', ' wc -l']
    """
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


def _build_pipe_procs(pipe_segments: list[str], results_ref: list) -> tuple[list, dict | None]:
    """
    Resolve a list of pipe-segment strings into a list of argv lists.

    Returns (argv_list, error_dict). error_dict is None on success.
    argv_list entries are either:
      - A plain list of strings (unprivileged command)
      - A list starting with "sudo" (privileged command, routed via wrapper)
    """
    argv_list = []

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

        if tokens[0] == "sudo":
            tokens = tokens[1:]
        if not tokens:
            return [], {"ok": False, "error": "Empty command after stripping sudo", "results": results_ref}

        binary = tokens[0]
        args = tokens[1:]

        for arg in args:
            if not _is_safe_arg(arg):
                return [], {
                    "ok": False,
                    "error": f"Forbidden character in argument: {arg!r}",
                    "results": results_ref,
                }

        if binary in PRIVILEGED_WHITELIST:
            resolved = BINARY_PATHS.get(binary)
            if not resolved or not os.path.isfile(resolved):
                return [], {"ok": False, "error": f"Binary not found: {binary}", "results": results_ref}
            if not os.path.isfile(PRIVILEGED_WRAPPER):
                return [], {
                    "ok": False,
                    "error": (
                        f"Privileged wrapper not found: {PRIVILEGED_WRAPPER}. "
                        "Run dev_setup.sh install."
                    ),
                    "results": results_ref,
                }
            argv_list.append(["sudo", "--non-interactive", PRIVILEGED_WRAPPER, resolved] + args)
        else:
            resolved = shutil.which(binary)
            if not resolved:
                return [], {"ok": False, "error": f"Command not found: {binary}", "results": results_ref}
            argv_list.append([resolved] + args)

    return argv_list, None


def _run_pipeline(argv_list: list[list[str]], cmd_str: str, timeout: int = 60) -> dict:
    """
    Execute a resolved pipeline (list of argv lists) and return a result dict.
    Pipes stdout of process N into stdin of process N+1.
    """
    procs = []
    try:
        for argv in argv_list:
            stdin = procs[-1].stdout if procs else None
            proc = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
