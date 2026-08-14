from __future__ import annotations

from io import StringIO
import os
import re
import threading
from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text

from cterm.core.agent_events import AgentEvents
from cterm.ui.tui.app.transcript_writer import TranscriptWriter
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


def _clean_output_lines(text: str) -> list[str]:
    """Return captured shell output without terminal control sequences."""
    clean = _sanitize_stream_text(str(text).rstrip("\n"))
    return clean.splitlines() if clean else []


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


def _language_for_path(path: str) -> str:
    ext = os.path.splitext(str(path))[1].lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".json": "json",
        ".sh": "bash",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
        ".html": "html",
        ".css": "css",
        ".sql": "sql",
        ".c": "c",
        ".cpp": "cpp",
        ".rs": "rust",
        ".go": "go",
        ".rb": "ruby",
        ".conf": "ini",
        ".ini": "ini",
    }.get(ext, "python")


MAX_TOOL_OUTPUT_LINES = 2
MAX_EXPANDED_OUTPUT_LINES = 20


class TUIAgentEventsHandler(AgentEvents):
    """Adapter used by ToolAgent to render progress inside the Textual app."""

    def __init__(self, app, run_id: int, cancel_event: threading.Event, transcript: TranscriptWriter | None = None):
        self.app = app
        self.run_id = run_id
        self.cancel_event = cancel_event
        # The app supplies the file-owning writer.  Keep direct handler use
        # (including tests) side-effect free when no transcript is supplied.
        self.transcript = transcript or TranscriptWriter(StringIO())
        self._spinner_message = ""
        self._thinking_buffer = ""
        self._last_stream: dict[str, tuple[Text, str]] = {}
        self._stream_buffers: dict[str, list[str]] = {}
        self._current_tool: str | None = None
        self._code_shown_for_approval = False

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set() or not self.app.is_run_active(self.run_id)

    def _ensure_active(self) -> bool:
        """Return True while the run may keep rendering.

        A cancelled run simply stops emitting UI output.  It must not raise
        into the agent or MCP client stacks, so in-flight tool calls finish
        and are stopped only by the runtime's own cooperative signals.
        """
        return not self.is_cancelled()

    def _emit(self, text: str, style: str = STYLE_TEXT) -> None:
        if not self._ensure_active():
            return
        clean = _ANSI_RE.sub("", str(text))
        self.transcript.write(clean)
        self.app.call_from_thread(self.app.append_line, clean, style)

    def _reset_stream_state(self) -> None:
        self._last_stream.clear()
        self._stream_buffers.clear()

    def _show_stream(self, fd: str, text: str, style: str, end: str = "\n") -> None:
        if not self._ensure_active():
            return
        text = _sanitize_stream_text(text)
        if not text:
            return
        suffix = end if end in ("\n", "") else "\n"
        self.transcript.write(f"[{self._current_tool} {fd}] {text}{suffix}", end="")
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
        if not self._ensure_active():
            return
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

    def thinking_delta(self, text):
        if not text:
            return
        if not self._ensure_active():
            return
        clean = _ANSI_RE.sub("", str(text))
        self._thinking_buffer += clean
        self.app.call_from_thread(self.app.append_thinking_delta, self._thinking_buffer)
        self.app.call_from_thread(self.app.set_status, f"Thinking: {' '.join(self._thinking_buffer.split())[:80]}")

    def thinking_complete(self, text):
        if not self._ensure_active():
            return
        full_text = str(text or self._thinking_buffer).strip()
        self._thinking_buffer = ""
        if full_text:
            self.transcript.write(f"▶ THINKING: {full_text}")
            self.app.call_from_thread(self.app.append_thinking_trace, full_text)

    def status(self, message):
        if not self._ensure_active():
            return
        self._spinner_message = str(message)
        self.app.call_from_thread(self.app.set_status, f"{self._spinner_message}...")

    def clear_status(self):
        if self.is_cancelled():
            return
        if self.app._runtime is not None and self.app._runtime.should_interrupt():
            return
        self.app.call_from_thread(self.app.set_status, "")

    def tool_call(self, tool_name, args):
        self._reset_stream_state()
        self._current_tool = tool_name
        self._code_shown_for_approval = False
        if tool_name == "bash":
            self._emit(f"$ {args.get('command', '')}", STYLE_TOOL)
        elif tool_name in ("exec_python", "exec"):
            code = args.get("code") or args.get("script") or args.get("source") or ""
            if not self._ensure_active():
                return
            self.transcript.write("» running script")
            self.transcript.write(code, end="" if str(code).endswith("\n") else "\n")
            self.app.call_from_thread(self.app.append_code, "» running script", code)
            self._code_shown_for_approval = True
        elif tool_name == "finder":
            self._emit(
                f"⦾ finding: {args.get('pattern', '')} in {args.get('path', '')}",
                STYLE_TOOL,
            )
        elif tool_name == "read_file":
            path = args.get("path", "")
            page = args.get("page", 1)
            page_label = f" (page {page})" if page and page != 1 else ""
            self._emit(f"▤ reading: {path}{page_label}", STYLE_TOOL)
        elif tool_name == "write_file":
            path = args.get("path", "")
            mode = args.get("mode", "overwrite")
            mode_label = f" [{mode}]" if mode != "overwrite" else ""
            content = args.get("content") or ""
            if not self._ensure_active():
                return
            title = f"✎ writing: {path}{mode_label}"
            self.transcript.write(title)
            self.transcript.write(content, end="" if str(content).endswith("\n") else "\n")
            self.app.call_from_thread(
                self.app.append_code,
                title,
                content,
                _language_for_path(path),
            )
            self._code_shown_for_approval = True
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

    def tool_output(self, fd=None, line="", end="\n", result=None):
        if result is not None:
            if self._current_tool == "bash":
                self._last_stream.clear()
                if not self._ensure_active():
                    return
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

        self._show_stream(fd, str(line), STYLE_TOOL_OUTPUT, end=end)

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
                        combined.extend(_clean_output_lines(stdout))
                    stderr = entry.get("stderr")
                    if stderr:
                        combined.extend(_clean_output_lines(stderr))
            else:
                stdout = result.get("stdout")
                if stdout:
                    combined.extend(_clean_output_lines(stdout))
                stderr = result.get("stderr")
                if stderr:
                    combined.extend(_clean_output_lines(stderr))

        if not combined:
            for fd_name in ("stdout", "stderr"):
                combined.extend(self._stream_buffers.get(fd_name, []))

        if not combined:
            return
        self._emit_capped_lines(combined)

    def _emit_tool_result(self, result) -> None:
        if not self._ensure_active():
            return
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
        self.transcript.write("✓ Done")
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
        self.transcript.write(f"✓ {count} matches")
        matches = result.get("matches", [])
        for match in matches[:200]:
            detail_lines.append(Text(f"  {match}", style=STYLE_SUCCESS))
        if len(matches) > 200:
            detail_lines.append(
                Text(
                    f"  ... {len(matches) - 200} more matches omitted ...",
                    style=STYLE_SUCCESS,
                )
            )
        self.app.call_from_thread(
            self.app.append_expandable_result, summary, Group(*detail_lines)
        )

    def shell_output(self, result):
        if not isinstance(result, dict):
            return
        for fd_name in ("stdout", "stderr"):
            self._commit_stream(fd_name)

        for entry in result.get("results", []):
            if not isinstance(entry, dict):
                continue
            stdout = entry.get("stdout")
            if stdout:
                self._emit_capped(_sanitize_stream_text(stdout))
            stderr = entry.get("stderr")
            if stderr:
                self._emit_capped(_sanitize_stream_text(stderr))

    def _emit_capped(self, text: str) -> None:
        lines = text.splitlines() or [text]
        self._emit_capped_lines(lines)

    def _emit_capped_lines(self, lines: list[str]) -> None:
        lines = [clean for line in lines if (clean := _sanitize_stream_text(line))]
        if len(lines) <= MAX_TOOL_OUTPUT_LINES:
            for line in lines:
                self._emit(line, STYLE_TOOL_OUTPUT)
            return

        if not self._ensure_active():
            return
        self.transcript.write("…")
        for line in lines[-MAX_TOOL_OUTPUT_LINES:]:
            self.transcript.write(line)
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

    def request_binary_approval(self, binary):
        if not self._ensure_active():
            return False
        event = threading.Event()
        request = ApprovalRequest(self.run_id, binary, event)
        self.app.call_from_thread(self.app.start_approval_prompt, request)
        event.wait()
        return bool(request.answer)

    def python_code(self, code):
        if not self._ensure_active():
            return
        self.app.call_from_thread(self.app.append_code, "» python in bash", code)

    def request_python_approval(self, code):
        if not self._ensure_active():
            return False
        if not self._code_shown_for_approval:
            self.app.call_from_thread(self.app.append_code, "» python in bash", code)
        event = threading.Event()
        request = ApprovalRequest(self.run_id, "", event, code=code)
        self.app.call_from_thread(self.app.start_python_approval_prompt, request)
        event.wait()
        return bool(request.answer)

    def request_write_approval(self, path, content, mode):
        if not self._ensure_active():
            return False
        if not self._code_shown_for_approval:
            self.app.call_from_thread(
                self.app.append_code,
                f"✎ writing: {path}",
                content,
                _language_for_path(path),
            )
        event = threading.Event()
        request = ApprovalRequest(self.run_id, "", event, code=content)
        self.app.call_from_thread(self.app.start_write_approval_prompt, request)
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
