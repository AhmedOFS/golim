"""Utility helpers shared by cterm modules."""

import glob
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass

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
# `$` is excluded because env-var tokens are expanded safely via
# _expand_supported_vars / os.path.expandvars before the safety check.
FORBIDDEN_CHARS = set("><`\\'()")


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
    """Expand shell variables and the narrow conveniences cterm intentionally supports.

    Processing order:
    1. Expand ~ / $HOME / ${HOME} (as before, for clarity and cross-platform safety).
    2. Expand any remaining $VAR / ${VAR} references via os.path.expandvars so that
       legitimate environment variables (e.g. $XDG_CONFIG_HOME, $GOPATH) are resolved
       before the argument reaches the safety checker.
    """
    home = os.path.expanduser("~")
    # Explicit ~ / $HOME shortcuts (kept from original for clarity)
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
    # General environment-variable expansion for everything else
    return os.path.expandvars(token)


def _expand_globs(args: list[str]) -> list[str]:
    """Expand glob patterns (*, ?, [...]) in argument tokens.

    Each token is passed to glob.glob.  If the pattern produces matches the
    token is replaced by the sorted match list (POSIX sh behaviour).  If
    there are no matches the token is kept verbatim (also POSIX sh behaviour
    for non-matching globs, i.e. no 'nullglob').
    """
    expanded: list[str] = []
    for arg in args:
        # Only bother calling glob when the token contains a wildcard.
        if any(c in arg for c in ("*", "?", "[")):
            matches = sorted(glob.glob(arg))
            expanded.extend(matches if matches else [arg])
        else:
            expanded.append(arg)
    return expanded


def _strip_supported_redirection(tokens: list[str]) -> tuple[list[str], bool, dict | None]:
    """
    Support only stderr suppression to /dev/null.

    This keeps bash on shell=False while allowing common diagnostic
    commands such as `du / 2>/dev/null`.
    """
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


def _build_cmd(tokens: list[str], results_ref: list) -> tuple[list[str] | None, dict | None]:
    """Resolve a token list to argv, including cterm's privileged routing."""
    if tokens[0] == "sudo":
        tokens = tokens[1:]
    if not tokens:
        return None, {"ok": False, "error": "Empty command after stripping sudo", "results": results_ref}

    # Expand environment variables in every token first, then apply glob
    # expansion to the arguments (not the binary name itself).
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

    if binary in PRIVILEGED_WHITELIST:
        resolved = BINARY_PATHS.get(binary)
        if not resolved or not os.path.isfile(resolved):
            return None, {"ok": False, "error": f"Binary not found: {binary}", "results": results_ref}
        if not os.path.isfile(PRIVILEGED_WRAPPER):
            return None, {
                "ok": False,
                "error": (
                    f"Privileged wrapper not found: {PRIVILEGED_WRAPPER}. "
                    "Run dev_setup.sh install."
                ),
                "results": results_ref,
            }
        return ["sudo", "--non-interactive", PRIVILEGED_WRAPPER, resolved] + args, None

    resolved = shutil.which(binary)
    if not resolved:
        return None, {"ok": False, "error": f"Command not found: {binary}", "results": results_ref}
    return [resolved] + args, None


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


def _split_chained_commands(command: str) -> list[str]:
    """Split a command string on unquoted `&&` operators."""
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


def _build_pipe_procs(pipe_segments: list[str], results_ref: list) -> tuple[list[ResolvedCommand], dict | None]:
    """
    Resolve a list of pipe-segment strings into a list of argv lists.

    Returns (argv_list, error_dict). error_dict is None on success.
    argv_list entries are either:
      - A plain list of strings (unprivileged command)
      - A list starting with "sudo" (privileged command, routed via wrapper)
    """
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
        cmd, err = _build_cmd(tokens, results_ref)
        if err:
            return [], err
        commands.append(ResolvedCommand(cmd, suppress_stderr=suppress_stderr))

    return commands, None


def _parse_command_part(cmd_str: str, results_ref: list) -> tuple[ParsedCommandPart | None, dict | None]:
    """Resolve one `&&` segment into argv and supported execution flags."""
    pipe_segments = _split_pipes(cmd_str)
    if len(pipe_segments) > 1:
        commands, err = _build_pipe_procs(pipe_segments, results_ref)
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

    cmd, err = _build_cmd(tokens, results_ref)
    if err:
        return None, err
    return ParsedCommandPart(argv_list=[ResolvedCommand(cmd)], suppress_stderr=suppress_stderr), None


def _run_pipeline(argv_list: list[ResolvedCommand], cmd_str: str, timeout: int = 60) -> dict:
    """
    Execute a resolved pipeline (list of argv lists) and return a result dict.
    Pipes stdout of process N into stdin of process N+1.
    """
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