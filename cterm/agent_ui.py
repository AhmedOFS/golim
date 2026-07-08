
from asyncio import Protocol

from typing import Any, Protocol

class AgentUI(Protocol):
    """Common UI contract consumed by ToolAgent."""

    def update_spinner(self, message: str) -> None:
        ...

    def stop_spinner(self) -> None:
        ...

    def message(self, text: str) -> None:
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
