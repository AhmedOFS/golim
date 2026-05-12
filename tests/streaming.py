#!/usr/bin/env python3
"""Smoke-test live MCP run_shell streaming with apt output."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cterm.cterm_server import get_socket_path
from cterm.llm_utils.mcp_client import FastMCPClient


COMMAND = "sudo snap install spotify"
SERVICE_NAME = "cterm-mcp.service"


def socket_responds(socket_path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(str(socket_path))
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            }
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            return bool(sock.recv(4096))
    except OSError:
        return False


def wait_for_service(socket_path: Path, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if socket_path.exists() and socket_responds(socket_path):
            return True
        time.sleep(0.2)
    return False


def ensure_server_running(socket_path: Path) -> None:
    if socket_path.exists() and socket_responds(socket_path):
        return

    subprocess.run(
        ["systemctl", "--user", "start", SERVICE_NAME],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    if wait_for_service(socket_path):
        return

    raise RuntimeError(f"{SERVICE_NAME} did not respond on {socket_path}")


async def main() -> int:
    socket_path = get_socket_path()
    ensure_server_running(socket_path)
    client = FastMCPClient(socket_path)

    try:
        print(f"$ {COMMAND}", flush=True)
        result = await client.call_tool(
            "run_shell",
            {"command": COMMAND},
            stream_output=True,
        )
        print(f"\nFinal result: {result}", flush=True)
        return 0 if result.get("ok") else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
