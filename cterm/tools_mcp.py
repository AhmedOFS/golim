#!/usr/bin/env python3
"""MCP tools definitions for cterm"""
import os
import shutil
import subprocess
import shlex
import requests

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

# --- Security Config ---

# The privileged wrapper installed by dev_setup.sh / the .deb package.
# Sudoers grants NOPASSWD only for this wrapper, not for apt/snap directly.
# This means the user still needs a password for sudo snap in their own terminal.
PRIVILEGED_WRAPPER = "/usr/lib/cterm/cterm-privileged"

# Binaries the wrapper is allowed to execute (must match the wrapper's ALLOWED list)
PRIVILEGED_WHITELIST = {
    "apt", "apt-get", "tee", "snap",
}

BINARY_PATHS = {
    "apt":     "/usr/bin/apt",
    "apt-get": "/usr/bin/apt-get",
    "tee":     "/usr/bin/tee",
    "snap":    "/usr/bin/snap",
}

# Characters never allowed in any argument
FORBIDDEN_CHARS = set('|><`$\\\'\"()')

def _is_safe_arg(arg: str) -> bool:
    """Reject arguments containing shell metacharacters."""
    return not any(c in FORBIDDEN_CHARS for c in arg)

# --- Tool Definitions ---

@tool
def list_files(path: str) -> dict:
    """Lists files in a directory"""
    if not os.path.isdir(path):
        return {"ok": False, "error": f"Not a directory: {path}"}
    try:
        return {"ok": True, "path": path, "contents": os.listdir(path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

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
def run_shell(command: str) -> dict:
    """
    Executes shell commands. Supports && chaining. Privileged commands
    (apt, apt-get, tee, snap) are routed through the cterm privileged
    wrapper and run as root without a password prompt. Everything else
    runs as the current user with no restrictions.
    """
    # Split on && and run each part in sequence
    parts = [c.strip() for c in command.split("&&")]
    results = []

    for cmd_str in parts:
        try:
            tokens = shlex.split(cmd_str)
        except ValueError as e:
            return {"ok": False, "error": f"Command parse error: {e}", "results": results}

        if not tokens:
            continue

        # Strip leading sudo if the LLM added it — we handle privilege ourselves
        if tokens[0] == "sudo":
            tokens = tokens[1:]
        if not tokens:
            continue

        binary = tokens[0]
        args = tokens[1:]

        for arg in args:
            if not _is_safe_arg(arg):
                return {"ok": False, "error": f"Forbidden character in argument: {arg!r}", "results": results}

        if binary in PRIVILEGED_WHITELIST:
            resolved = BINARY_PATHS.get(binary)
            if not resolved or not os.path.isfile(resolved):
                return {"ok": False, "error": f"Binary not found: {binary}", "results": results}
            if not os.path.isfile(PRIVILEGED_WRAPPER):
                return {"ok": False, "error": f"Privileged wrapper not found: {PRIVILEGED_WRAPPER}. Run dev_setup.sh install.", "results": results}
            cmd = ["sudo", "--non-interactive", PRIVILEGED_WRAPPER, resolved] + args
        else:
            resolved = shutil.which(binary)
            if not resolved:
                return {"ok": False, "error": f"Command not found: {binary}", "results": results}
            cmd = [resolved] + args

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            results.append({
                "command": cmd_str,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            })
            # Stop the chain on failure, just like real &&
            if result.returncode != 0 and not result.stdout.strip():
                return {"ok": False, "error": f"Command failed: {cmd_str}", "results": results}

        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Command timed out: {cmd_str}", "results": results}
        except Exception as e:
            return {"ok": False, "error": str(e), "results": results}

    return {"ok": True, "command": command, "results": results}

@tool
def calculate(a: float, b: float, op: str) -> dict:
    """Performs basic math operations (add, sub, mul, div)"""
    ops = {
        "add": lambda x, y: x + y,
        "sub": lambda x, y: x - y,
        "mul": lambda x, y: x * y,
        "div": lambda x, y: x / y if y != 0 else None
    }
    if op not in ops:
        return {"ok": False, "error": f"Invalid op: {op}. Use: add, sub, mul, div"}
    result = ops[op](a, b)
    if result is None:
        return {"ok": False, "error": "Division by zero"}
    return {"ok": True, "result": result}

@tool
def fetch_json(url: str) -> dict:
    """Fetches and parses JSON from a URL"""
    try:
        resp = requests.get(url, timeout=5)
        return {"ok": True, "url": url, "data": resp.json()}
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

# Attach tools to mcp object
mcp.list_files = list_files
mcp.read_file = read_file
mcp.run_shell = run_shell
mcp.calculate = calculate
mcp.fetch_json = fetch_json
mcp.system_info = system_info