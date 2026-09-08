
import asyncio
import os
import pwd
from pathlib import Path
import shlex
import sys
import threading
import time


import concurrent.futures

_PYTHON_BINARIES = {"python", "python3"}
_COMMAND_SEPARATORS = ("&&", "||", "|", ";", "&")
_THINKING_KEYS = (
    "thinking",
    "reasoning",
    "reasoning_content",
    "reasoning_text",
    "reasoning_details",
)
_DIRECT_THINKING_KEYS = ("thinking", "reasoning", "reasoning_content", "reasoning_text")
_REASONING_DETAIL_KEYS = ("delta", "text", "content", "reasoning")
_CONTENT_MARKER = "[assistant content]\n"
_TRACE_MARKER = "\n\n[assistant thinking trace]\n"
_TRACE_ONLY_MARKER = "[assistant thinking trace]\n"
_PYTHON_DENIED_RESULT = {
    "ok": False,
    "error": "Python code execution not approved by user",
}
_FINAL_SUMMARY_PROMPT = "Provide a concise final summary of what was accomplished."



def _run_async(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


def _indent(text, prefix="      "):
    return "\n".join(prefix + line for line in text.splitlines())


def _clip_label(text, max_chars=80):
    line = text.splitlines()[0] if text else (text or "")
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + "..."


def _clip_text(text, limit=1200):
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...<truncated>..."


def get_socket_path() -> Path:
    """Return the same per-user socket path used by the MCP server.

    ``os.getlogin`` depends on a controlling terminal and is frequently wrong
    (or unavailable) in services, SSH sessions, and containers.
    """
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        username = os.environ.get("USER", "default")
    return Path(f"/tmp/golim_tools_{username}.sock")
def _is_python_binary(tok: str) -> bool:
    name = os.path.basename(tok)
    return name in _PYTHON_BINARIES or name.startswith("python3.")


def _extract_heredoc_python(command: str) -> str | None:
    """Extract Python source from a ``python3 << DELIM`` heredoc."""
    import re

    m = re.search(
        r'\bpython(?:3(?:\.\d+)?)?\s+<<\s+([\'"]?)(\w+)\1\s*\n(.+?)\n\s*\2',
        command,
        re.DOTALL,
    )
    if m:
        return m.group(3).strip()
    return None


def _detect_python_in_bash(command: str) -> str | None:
    """Check if a bash command runs Python code and return the code to approve.

    Returns the Python source for ``-c`` invocations, heredoc content for
    ``<<`` invocations, the full command for script/module invocations,
    or ``None`` if this is not a Python execution.
    """
    # Check for heredoc pattern first (preserves formatting and newlines)
    py_code = _extract_heredoc_python(command)
    if py_code is not None:
        return py_code

    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _COMMAND_SEPARATORS:
            i += 1
            continue
        if _is_python_binary(tok):
            if i + 2 < len(tokens) and tokens[i + 1] == "-c":
                return tokens[i + 2]
            return command
        # Skip past this command segment to the next separator
        while i < len(tokens) and tokens[i] not in _COMMAND_SEPARATORS:
            i += 1

    return None
