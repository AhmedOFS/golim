"""Request-scoped cancellation state for MCP tool execution."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event


_tool_cancellation: ContextVar[Event | None] = ContextVar(
    "mcp_tool_cancellation",
    default=None,
)


@contextmanager
def bind_tool_cancellation(event: Event):
    token = _tool_cancellation.set(event)
    try:
        yield
    finally:
        _tool_cancellation.reset(token)


def is_tool_cancelled() -> bool:
    event = _tool_cancellation.get()
    return event is not None and event.is_set()
