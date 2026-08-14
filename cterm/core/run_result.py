"""Structured results returned by an agent run."""

from __future__ import annotations

from typing import TypedDict


class RunResult(TypedDict):
    """JSON-compatible result returned by ``ToolAgent`` and ``Runtime``."""

    ok: bool
    LLM_response: str


def make_run_result(ok: bool, response: str = "") -> RunResult:
    """Build the stable run-result payload shared by the agent and UIs."""
    return {
        "ok": bool(ok),
        "LLM_response": str(response or ""),
    }
