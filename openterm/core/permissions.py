"""Restricted-mode approval policy routed through the active UI."""

from __future__ import annotations

from openterm.config import get_config
from openterm.core.agent_events import AgentEvents
from openterm.core.utils import _PYTHON_DENIED_RESULT, _detect_python_in_bash


EXEC_TOOL_NAMES = ("exec_python", "exec")


class Permissions:
    """Checks whether a tool call needs user approval and asks the UI.

    The class owns the restricted-mode gating decisions made before a tool
    call is dispatched (embedded Python in bash, the Python execution tool,
    and file writes) plus the out-of-band privileged-binary callback that
    the MCP client invokes mid-transport. Denials are composed here and
    surfaced through ``ui.tool_output`` so both UIs render them uniformly.
    """

    def __init__(self, ui: AgentEvents | None = None):
        self.ui = ui

    def is_approved(self, tool_name: str, args: dict) -> dict | None:
        """Gate a pending tool call in restricted mode.

        Returns ``None`` when execution may proceed, otherwise the denial
        tool result, already reported through ``ui.tool_output``.
        """
        if tool_name == "bash":
            return self._gate_embedded_python(args)
        if get_config().unrestricted_mode:
            return None
        if tool_name in EXEC_TOOL_NAMES:
            return self._gate_python_execution(args)
        if tool_name == "write_file":
            return self._gate_file_write(args)
        return None

    def approve_binary(self, approval: dict) -> bool:
        """Answer a privileged-command approval request from the MCP client.

        Wired as the client's ``on_approval_request`` callback; the approval
        exchange never reaches the model as a tool result.
        """
        ui = self._require_ui()
        return bool(ui.request_binary_approval(approval.get("binary", "")))

    def _gate_embedded_python(self, args: dict) -> dict | None:
        command = str(args.get("command", ""))
        py_code = _detect_python_in_bash(command)
        # A command whose detected Python spans the whole input is treated
        # as plain bash by the shell path, so it needs no extra approval.
        if py_code is None or py_code == command:
            return None
        if get_config().unrestricted_mode:
            self.ui.python_code(py_code)
            return None
        if self.ui.request_python_approval(py_code):
            return None
        return self._deny_python()

    def _gate_python_execution(self, args: dict) -> dict | None:
        code = str(
            args.get("code")
            or args.get("script")
            or args.get("source")
            or ""
        )
        if self.ui.request_python_approval(code):
            return None
        return self._deny_python()

    def _gate_file_write(self, args: dict) -> dict | None:
        path = str(args.get("path", ""))
        content = str(args.get("content", ""))
        mode = str(args.get("mode", "overwrite"))
        if self.ui.request_write_approval(path, content, mode):
            return None
        denial = {
            "ok": False,
            "error": f"File write not approved by user: {path}",
        }
        self.ui.tool_output(result=denial)
        return denial

    def _deny_python(self) -> dict:
        denial = dict(_PYTHON_DENIED_RESULT)
        self.ui.tool_output(result=denial)
        return denial

    def _require_ui(self) -> AgentEvents:
        if self.ui is None:
            raise RuntimeError(
                "Permissions requires a bound AgentEvents handler; "
                "call Runtime.bind_ui() first."
            )
        return self.ui
