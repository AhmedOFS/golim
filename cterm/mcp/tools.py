
"""MCP tools definitions for cterm"""
import os
import subprocess
import sys
import time

from .vars import (
    EXA_MCP_URL,
    MAX_NUM_RESULTS,
    NO_RESULTS,
    OUTPUT_LINE_LIMIT,
    PARALLEL_MCP_URL,
    READ_FILE_PAGE_SIZE,
)
from .utils.bash_utils import (
    _is_bash_unrestricted,
    _requires_pty_streaming,
    _run_restricted,
    _run_unrestricted,
    _stream_restricted,
    _stream_unrestricted,
    _terminate_process_group,
)
from .utils.cancellation import is_tool_cancelled
from .utils.web_utils import (
    _exa_api_key,
    _parallel_api_key,
    _websearch_mcp_call,
    _websearch_provider,
)

_should_stream_with_pty = _requires_pty_streaming


def _coerce_bash_timeout(timeout):
    if timeout is None:
        return None, None
    if isinstance(timeout, bool):
        return None, {"ok": False, "error": "timeout must be a number of seconds or null, not a boolean"}
    try:
        coerced = float(timeout)
    except (TypeError, ValueError):
        return None, {"ok": False, "error": "timeout must be a number of seconds or null"}
    if coerced <= 0:
        return None, {"ok": False, "error": "timeout must be greater than zero"}
    return coerced, None




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
def finder(
    path: str,
    pattern: str | None = "*",
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

                full = entry.path.replace(os.sep, "/")

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
                        matches.append(full)

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

                    matches.append(full)

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
    Reads one page from a file.

    Args:
        path: File path to read.
        page: 1-based page number. Each page returns up to READ_FILE_PAGE_SIZE (200) lines.
    """
    if not os.path.isfile(path):
        return {"ok": False, "error": f"File not found: {path}"}
    try:
        page = int(page)
        if page < 1:
            return {"ok": False, "error": "page must be greater than or equal to 1"}

        path = os.path.abspath(os.path.expanduser(path))

        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

        page_size = READ_FILE_PAGE_SIZE
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
def bash(command: str, stream: bool = False, allow_privileged: bool = False, timeout: int | None = None) -> dict:
    """
    Executes command lines.

    When `bash_unrestricted` is true in ~/.cterm/config/config.json the command
    is passed directly to /bin/bash -c, giving full shell access (pipes,
    redirections, subshells, here-docs, etc.). The only remaining restriction
    is cterm's sudo whitelist: any `sudo <binary>` call whose resolved path is
    not on the whitelist is rejected and an approval_required result is
    returned, exactly as in the restricted path.

    When `bash_unrestricted` is false (the default) the original safe argv
    parser is used. It supports unquoted `&&` chaining, unquoted `|` pipelines,
    quoted arguments, environment-variable and `~` expansion, glob expansion,
    and `2>/dev/null` stderr suppression. Other redirection and shell-only
    syntax are rejected.

    In both modes, `stream=True` yields incremental output chunks followed by
    a final result dict.
    """
    timeout, timeout_error = _coerce_bash_timeout(timeout)
    if timeout_error:
        return timeout_error
    if _is_bash_unrestricted():
        runner, streamer = _run_unrestricted, _stream_unrestricted
    else:
        runner, streamer = _run_restricted, _stream_restricted
    if stream:
        return streamer(command, allow_privileged=allow_privileged, timeout=timeout)
    return runner(command, allow_privileged=allow_privileged, timeout=timeout)


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
        timeout: Maximum run time in seconds. ``None`` disables the limit.
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
        timeout = None if timeout is None else max(1, int(timeout))
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout must be an integer number of seconds"}

    run_cwd = None
    if cwd:
        run_cwd = os.path.expanduser(str(cwd))
        if not os.path.isdir(run_cwd):
            return {"ok": False, "error": f"Working directory not found: {cwd}"}

    try:
        process = subprocess.Popen(
            [sys.executable, "-c", str(code)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=run_cwd,
            start_new_session=True,
        )
        started = time.monotonic()
        input_data = "" if stdin is None else str(stdin)
        sent_input = False
        while True:
            if is_tool_cancelled():
                _terminate_process_group(process)
                process.stdout.close()
                process.stderr.close()
                return {"ok": False, "error": "Tool execution cancelled"}
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                _terminate_process_group(process)
                process.stdout.close()
                process.stderr.close()
                return {"ok": False, "error": f"Python code timed out after {timeout}s"}
            try:
                stdout, stderr = process.communicate(
                    input=input_data if not sent_input else None,
                    timeout=0.1 if remaining is None else min(0.1, remaining),
                )
                sent_input = True
                break
            except subprocess.TimeoutExpired:
                sent_input = True
                continue
        response = {
            "ok": process.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": process.returncode,
        }
        if process.returncode != 0:
            response["error"] = "Python code failed"
        return response
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
