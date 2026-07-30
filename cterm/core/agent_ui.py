from contextvars import ContextVar
from typing import Any, Protocol


active_agent_ui: ContextVar["AgentUI | None"] = ContextVar(
    "active_agent_ui",
    default=None,
)

class AgentUI(Protocol):
    """Common UI contract consumed by ToolAgent."""

    def update_spinner(self, message: str) -> None:
        ...

    def stop_spinner(self) -> None:
        ...

    def message(self, text: str) -> None:
        ...

    def log_tool_call(self, tool_name: str, args: dict[str, Any]) -> None:
        ...

    def log_tool_result(self, tool_name: str, result: dict[str, Any]) -> None:
        ...

    def log_tool_output(self, tool_name: str, fd: str, line: str, end: str = "\n") -> None:
        ...

    def log_thinking_trace(self, text: str) -> None:
        ...

    def log_summary(self, text: str) -> None:
        ...

    def thinking_trace_delta(self, text: str) -> None:
        ...

    def thinking_trace_complete(self, text: str) -> None:
        ...

    def tool_call(self, tool_name: str, args: dict[str, Any]) -> None:
        ...

    def handle_tool_output(
        self,
        fd: str | None = None,
        line: str = "",
        end: str = "\n",
        result: dict[str, Any] | None = None,
    ) -> None:
        ...

    def handle_shell_result_output(self, result: dict[str, Any]) -> None:
        ...

    def approve_privileged_binary(self, binary: str) -> bool:
        ...

    def approve_python_code(self, code: str) -> bool:
        ...

    def show_python_code(self, code: str) -> None:
        ...
