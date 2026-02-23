#!/usr/bin/env python3
"""MCP tools definitions for cterm"""
import os
import subprocess
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
def run_shell(command: str, sudo: bool = False) -> dict:
    """Executes a shell command with optional sudo support."""
    try:
        if sudo:
            command = f"sudo {command}"
        output = subprocess.check_output(
            command,
            shell=True,
            text=True,
            stderr=subprocess.STDOUT
        )
        return {"ok": True, "command": command, "output": output}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "command": command, "error": e.output}

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

# Attach tools to mcp object
mcp.list_files = list_files
mcp.read_file = read_file
mcp.run_shell = run_shell
mcp.calculate = calculate
mcp.fetch_json = fetch_json