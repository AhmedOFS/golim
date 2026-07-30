from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Protocol


active_agent_events_handler: ContextVar["AgentEvents | None"] = ContextVar(
    "active_agent_events_handler",
    default=None,
)


class AgentEvents(Protocol):
    """Interaction callbacks consumed by ToolAgent."""

    def status(self, message: str) -> None:
        ...

    def clear_status(self) -> None:
        ...

    def message(self, text: str) -> None:
        ...

    def thinking_delta(self, text: str) -> None:
        ...

    def thinking_complete(self, text: str) -> None:
        ...

    def tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> None:
        ...

    def tool_output(
        self,
        fd: str | None = None,
        line: str = "",
        end: str = "\n",
        result: dict[str, Any] | None = None,
    ) -> None:
        ...

    def shell_output(
        self,
        result: dict[str, Any],
    ) -> None:
        ...

    def python_code(self, code: str) -> None:
        ...

    def request_binary_approval(self, binary: str) -> bool:
        ...

    def request_python_approval(self, code: str) -> bool:
        ...
