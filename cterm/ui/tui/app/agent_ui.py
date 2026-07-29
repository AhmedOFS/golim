from __future__ import annotations

import ctypes
import json
import re
import threading
from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text

from cterm.core.agent_ui import AgentUI

from cterm.ui.tui.tui_style import (
    STYLE_DIM,
    STYLE_ERROR,
    STYLE_SUCCESS,
    STYLE_TEXT,
    STYLE_TOOL,
    STYLE_TOOL_OUTPUT,
    STYLE_WARNING,
)


_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_OSC_RE = re.compile(r"\x1B\].*?(?:\x07|\x1B\\)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _resolve_carriage_returns(text: str) -> str:
    resolved_lines = []
    for raw_line in text.split("\n"):
        resolved_lines.append(raw_line.split("\r")[-1])
    return "\n".join(resolved_lines)


def _sanitize_stream_text(text: str) -> str:
    text = _OSC_RE.sub("", str(text))
    text = _ANSI_RE.sub("", text)
    text = _resolve_carriage_returns(text)
    return _CONTROL_RE.sub("", text)


def _format_nested(value, indent: int = 2) -> list[str]:
    lines: list[str] = []
    pad = " " * indent
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, (dict, list)) and val:
                lines.append(f"{pad}{key}:")
                lines.extend(_format_nested(val, indent + 2))
            else:
                lines.append(f"{pad}{key}: {val}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)) and item:
                lines.extend(_format_nested(item, indent))
            else:
                lines.append(f"{pad}- {item}")
    else:
        lines.append(f"{pad}{value}")
    return lines


@dataclass
class ChatResult:
    ok: bool
    text: str
    log_path: str | None = None


@dataclass
class ApprovalRequest:
    run_id: int
    binary: str
    event: threading.Event
    answer: bool | None = None
    code: str | None = None


class RunCancelled(Exception):
    """Raised inside the background chat worker when the active run is cancelled."""


def _raise_in_thread(thread_id: int | None, exc_type: type[BaseException]) -> bool:
    if thread_id is None:
        return False
    result = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id),
        ctypes.py_object(exc_type),
    )
    if result > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), None)
        return False
    return result == 1


MAX_TOOL_OUTPUT_LINES = 2
MAX_EXPANDED_OUTPUT_LINES = 20


class TextualAgentUI(AgentUI):
    """Adapter used by ToolAgent to render progress inside the Textual app."""

    def __init__(self, app, run_id: int, cancel_event: threading.Event):
        self.app = app
        self.run_id = run_id
        self.cancel_event = cancel_event
        self._spinner_message = ""
        self._thinking_buffer = ""
        self._last_stream: dict[str, tuple[Text, str]] = {}
        self._stream_buffers: dict[str, list[str]] = {}
        self._current_tool: str | None = None
        self._code_shown_for_approval = False
        self._log_file = None

    def set_log_file(self, log_file) -> None:
        self._log_file = log_file

    def _write_log(self, text: str) -> None:
        if self._log_file is None:
            return
        try:
            self._log_file.write(text)
            if not text.endswith("\n"):
                self._log_file.write("\n")
            self._log_file.flush()
        except Exception:
            pass

    def _log_json_section(self, title: str, payload) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        except TypeError:
            body = repr(payload)
        self._write_log(f"\n## {title}\n{body}\n")

    def _log_text_section(self, title: str, text: str) -> None:
        self._write_log(f"\n## {title}\n{text}\n")

    def _log_stream_line(self, tool_name: str, fd: str, line: str, end: str) -> None:
        clean = _sanitize_stream_text(str(line))
        suffix = end if end in ("\n", "") else "\n"
        self._write_log(f"[{tool_name} {fd}] {clean}{suffix}")

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set() or not self.app.is_run_active(self.run_id)

    def _ensure_active(self) -> None:
        if self.is_cancelled():
            raise RunCancelled()

    def _emit(self, text: str, style: str = STYLE_TEXT) -> None:
        self._ensure_active()
        self.app.call_from_thread(self.app.append_line, text, style)

    def _reset_stream_state(self) -> None:
        self._last_stream.clear()
        self._stream_buffers.clear()

    def _show_stream(self, fd: str, text: str, style: str) -> None:
        self._ensure_active()
        text = _sanitize_stream_text(text)
        if not text:
            return
        self._stream_buffers.setdefault(fd, []).append(text)
        renderable = Text(text, style=style)
        self._last_stream[fd] = (renderable, style)
        self.app.call_from_thread(
            self.app.append_stream,
            renderable,
            True,
            False,
        )

    def _commit_stream(self, fd: str) -> None:
        entry = self._last_stream.get(fd)
        if entry is None:
            return
        self._ensure_active()
        renderable, _ = entry
        self.app.call_from_thread(
            self.app.append_stream,
            renderable,
            True,
            True,
        )
        self._last_stream.pop(fd, None)

    def message(self, text):
        self._emit(_ANSI_RE.sub("", str(text)))

    def thinking_trace_delta(self, text):
        if not text:
            return
        self._ensure_active()
        clean = _ANSI_RE.sub("", str(text))
        self._thinking_buffer += clean
        self.app.call_from_thread(self.app.append_thinking_delta, self._thinking_buffer)
        self.app.call_from_thread(self.app.set_status, f"Thinking: {' '.join(self._thinking_buffer.split())[:80]}")

    def thinking_trace_complete(self, text):
        self._ensure_active()
        full_text = str(text or self._thinking_buffer).strip()
        self._thinking_buffer = ""
        if full_text:
            self._log_text_section("thinking_trace", full_text)
            self.app.call_from_thread(self.app.append_thinking_trace, full_text)

    def update_spinner(self, message):
        self._ensure_active()
        self._spinner_message = str(message)
        self.app.call_from_thread(self.app.set_status, f"{self._spinner_message}...")

    def stop_spinner(self):
        if self.is_cancelled():
            return
        if self.app._runtime is not None and self.app._runtime.should_interrupt():
            return
        self.app.call_from_thread(self.app.set_status, "")

    def tool_call(self, tool_name, args):
        self._reset_stream_state()
        self._current_tool = tool_name
        self._code_shown_for_approval = False
        self._log_json_section(f"tool_call {tool_name}", args or {})
        if tool_name == "bash":
            self._emit(f"$ {args.get('command', '')}", STYLE_TOOL)
        elif tool_name in ("exec_python", "exec"):
            code = args.get("code") or args.get("script") or args.get("source") or ""
            self._ensure_active()
            self.app.call_from_thread(self.app.append_code, "» running script", code)
            self._code_shown_for_approval = True
        elif tool_name == "finder":
            self._emit(
                f"⦾ finding: {args.get('pattern', '')} in {args.get('path', '')}",
                STYLE_TOOL,
            )
        elif tool_name == "websearch":
            self._emit(
                f"🌐 searching the web for : {args.get('query', '')}",
                STYLE_TOOL,
            )
        elif tool_name == "system_info":
            self._emit(
                f"⛭ checking system info",
                STYLE_TOOL,
            )
        else:
            self._emit(f"{tool_name}: {', '.join(args.keys()) if args else ''}", STYLE_TOOL)

    def handle_tool_output(self, fd=None, line="", end="\n", result=None):
        if result is not None:
            if self._current_tool:
                self._log_json_section(f"tool_result {self._current_tool}", result)
            if self._current_tool == "bash":
                self._last_stream.clear()
                self._ensure_active()
                self.app.call_from_thread(self.app.discard_pending_stream)
                self._emit_bash_result_output(result)
                formatted, style = self._format_tool_result(result)
                if formatted and not (isinstance(result, dict) and result.get("ok") is True):
                    self._emit(formatted, style)
                self.app.call_from_thread(self.app.append_line, "", STYLE_TEXT)
                return

            for fd_name in ("stdout", "stderr"):
                self._commit_stream(fd_name)
            self._emit_tool_result(result)
            self.app.call_from_thread(self.app.append_line, "", STYLE_TEXT)
            return

        if fd is None:
            return

        if self._current_tool:
            self._log_stream_line(self._current_tool, fd, str(line), end)
        self._show_stream(fd, str(line), STYLE_TOOL_OUTPUT)

    def _emit_bash_result_output(self, result) -> None:
        combined: list[str] = []
        if isinstance(result, dict):
            entries = result.get("results")
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    stdout = entry.get("stdout")
                    if stdout:
                        combined.extend(_resolve_carriage_returns(str(stdout).rstrip("\n")).splitlines())
                    stderr = entry.get("stderr")
                    if stderr:
                        combined.extend(_resolve_carriage_returns(str(stderr).rstrip("\n")).splitlines())
            else:
                stdout = result.get("stdout")
                if stdout:
                    combined.extend(_resolve_carriage_returns(str(stdout).rstrip("\n")).splitlines())
                stderr = result.get("stderr")
                if stderr:
                    combined.extend(_resolve_carriage_returns(str(stderr).rstrip("\n")).splitlines())

        if not combined:
            for fd_name in ("stdout", "stderr"):
                combined.extend(self._stream_buffers.get(fd_name, []))

        if not combined:
            return
        self._emit_capped_lines(combined)

    def _emit_tool_result(self, result) -> None:
        self._ensure_active()
        if (
            self._current_tool == "system_info"
            and isinstance(result, dict)
            and result.get("ok") is True
        ):
            self._emit_system_info_result(result)
            return
        if (
            self._current_tool == "finder"
            and isinstance(result, dict)
            and result.get("ok") is True
            and "matches" in result
        ):
            self._emit_finder_result(result)
            return
        formatted, style = self._format_tool_result(result)
        if formatted:
            self._emit(formatted, style)

    def _emit_system_info_result(self, result: dict) -> None:
        data = {k: v for k, v in result.items() if k != "ok"}
        summary = Text("✓ Done", style=STYLE_SUCCESS)
        detail_lines = [Text("✓ Done", style=STYLE_SUCCESS)]
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                detail_lines.append(Text(f"  {key}:", style=STYLE_SUCCESS))
                for line in _format_nested(value, indent=4):
                    detail_lines.append(Text(line, style=STYLE_SUCCESS))
            else:
                detail_lines.append(Text(f"  {key}: {value}", style=STYLE_SUCCESS))
        self.app.call_from_thread(
            self.app.append_expandable_result, summary, Group(*detail_lines)
        )

    def _emit_finder_result(self, result: dict) -> None:
        count = result.get("total", 0)
        summary = Text(f"✓ {count} matches", style=STYLE_SUCCESS)
        detail_lines = [Text(f"✓ {count} matches", style=STYLE_SUCCESS)]
        for match in result.get("matches", []):
            detail_lines.append(Text(f"  {match}", style=STYLE_SUCCESS))
        self.app.call_from_thread(
            self.app.append_expandable_result, summary, Group(*detail_lines)
        )

    def handle_shell_result_output(self, result):
        if not isinstance(result, dict):
            return
        if self._current_tool:
            self._log_json_section(f"tool_result {self._current_tool}", result)

        for fd_name in ("stdout", "stderr"):
            self._commit_stream(fd_name)

        for entry in result.get("results", []):
            if not isinstance(entry, dict):
                continue
            stdout = entry.get("stdout")
            if stdout:
                self._emit_capped(_resolve_carriage_returns(str(stdout).rstrip("\n")))
            stderr = entry.get("stderr")
            if stderr:
                self._emit_capped(_resolve_carriage_returns(str(stderr).rstrip("\n")))

    def _emit_capped(self, text: str) -> None:
        lines = text.splitlines() or [text]
        self._emit_capped_lines(lines)

    def _emit_capped_lines(self, lines: list[str]) -> None:
        if len(lines) <= MAX_TOOL_OUTPUT_LINES:
            for line in lines:
                self._emit(line, STYLE_TOOL_OUTPUT)
            return

        self._ensure_active()
        summary_lines = [Text("…", style=STYLE_TOOL_OUTPUT)]
        summary_lines.extend(
            Text(line, style=STYLE_TOOL_OUTPUT) for line in lines[-MAX_TOOL_OUTPUT_LINES:]
        )

        expanded = self._summarize_long_output(lines, MAX_EXPANDED_OUTPUT_LINES)
        detail_lines = [Text(line, style=STYLE_TOOL_OUTPUT) for line in expanded]

        self.app.call_from_thread(
            self.app.append_expandable_result, Group(*summary_lines), Group(*detail_lines)
        )

    def _summarize_long_output(self, lines: list[str], limit: int) -> list[str]:
        if len(lines) <= limit:
            return list(lines)
        if limit <= 1:
            return ["…"]

        tail_count = max(1, limit // 2)
        head_count = max(0, limit - tail_count - 1)
        return lines[:head_count] + ["…"] + lines[-tail_count:]

    def approve_privileged_binary(self, binary):
        self._ensure_active()
        event = threading.Event()
        request = ApprovalRequest(self.run_id, binary, event)
        self.app.call_from_thread(self.app.start_approval_prompt, request)
        event.wait()
        return bool(request.answer)

    def show_python_code(self, code):
        self._ensure_active()
        self.app.call_from_thread(self.app.append_code, "» python in bash", code)

    def approve_python_code(self, code):
        self._ensure_active()
        if not self._code_shown_for_approval:
            self.app.call_from_thread(self.app.append_code, "» python in bash", code)
        event = threading.Event()
        request = ApprovalRequest(self.run_id, "", event, code=code)
        self.app.call_from_thread(self.app.start_python_approval_prompt, request)
        event.wait()
        return bool(request.answer)

    def _format_tool_result(self, result):
        ok = result.get("ok") if isinstance(result, dict) else None
        if ok is True:
            if "matches" in result:
                count = result.get("total", 0)
                return f"✓ {count} matches", STYLE_SUCCESS
            if "content" in result:
                return (
                    f"✓ {result.get('path', '')} (pg {result.get('page', 1)}/{result.get('total_pages', 1)})",
                    STYLE_SUCCESS,
                )
            if "bytes_written" in result:
                return f"✓ {result.get('path', '')} ({result['bytes_written']} bytes)", STYLE_SUCCESS
            return "✓ Done", STYLE_SUCCESS
        if ok is False:
            return f"✗ {result.get('error', 'unknown error')}", STYLE_ERROR
        return None, STYLE_TEXT
