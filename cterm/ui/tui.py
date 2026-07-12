"""Textual interface for the default cterm command."""

from __future__ import annotations

import io
import ctypes
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable
from rich.theme import Theme
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.style import Style as RichStyle
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import Input, Static

from cterm.agent_ui import AgentUI
from cterm.ui.history import History


_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_OSC_RE = re.compile(r"\x1B\].*?(?:\x07|\x1B\\)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Spinner animation frames (braille dots) used for the "Thinking..." indicator.
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SPINNER_INTERVAL = 0.09  # seconds between frames

# Color palette pulled from the target UI.
STYLE_TEXT = "#f3f3f3"          # regular output / prose
STYLE_TOOL = "bold #f3f3f3"     # "$ command" / "◎ finding" lines
STYLE_SUCCESS = "#7d8a99"       # dim "✓ Done" / "✓ N matches" lines
STYLE_ERROR = "#e06c75"         # "✗ ..." failures
STYLE_WARNING = "#c2b280"       # stderr / shell warnings (e.g. cp overwrite notice)
STYLE_DIM = "#6f6f6f"           # secondary/path info, log path footer
STYLE_TOOL_OUTPUT = "#C7A4A4"   # raw stdout/stderr streamed from running tools

MAX_TOOL_OUTPUT_LINES = 2       # cap on the *static* post-completion output summary
MAX_EXPANDED_OUTPUT_LINES = 20  # cap on the expanded/clicked-open output view


def _resolve_carriage_returns(text: str) -> str:
    """Collapse \\r-driven in-place rewrites down to their final visual state.

    Within a single terminal line, \\r moves the cursor back to column 0;
    whatever is written after the last \\r on that line is what a real
    terminal would end up showing. This is only used for the *static*
    end-of-command summary (already-captured text) — the live stream path
    handles \\r directly via the Transcript widget's replace-last-line
    support instead.
    """
    resolved_lines = []
    for raw_line in text.split("\n"):
        resolved_lines.append(raw_line.split("\r")[-1])
    return "\n".join(resolved_lines)


def _sanitize_stream_text(text: str) -> str:
    """Return the final visible text for a streamed terminal line."""
    text = _OSC_RE.sub("", str(text))
    text = _ANSI_RE.sub("", text)
    text = _resolve_carriage_returns(text)
    return _CONTROL_RE.sub("", text)


def _format_nested(value, indent: int = 2) -> list[str]:
    """Recursively pretty-print a (possibly nested) result value as plain
    indented lines instead of a Python/JSON-style repr blob.

    e.g. {"cpu": {"cores": 8, "model": "..."}, "disks": ["sda", "sdb"]}
    becomes:
        cpu:
          cores: 8
          model: ...
        disks:
          - sda
          - sdb
    instead of a single line containing the dict's repr().
    """
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


class Transcript(ScrollView, can_focus=False):
    """A scrollable, markdown-capable log that supports in-place line updates.

    This exists instead of Textual's built-in RichLog because RichLog is
    strictly append-only: every ``write()`` call creates a brand new row.
    That makes it impossible to correctly render carriage-return-driven
    terminal output (progress bars, spinners, ``\\r``-based redraws) — each
    update just piles up as its own line instead of overwriting the current
    one. Transcript keeps its own buffer of pre-rendered lines and exposes a
    ``replace_last``/``commit`` write mode so callers can update an
    in-progress line in place and only "lock it in" once it's finished.

    It also supports generic *expandable* entries: a line (or block of
    lines) that was written via ``write_expandable`` can be clicked to swap
    between a collapsed "summary" renderable and an expanded "detail"
    renderable. Thinking traces use this same click/toggle machinery, but
    are visually distinguished with a ▶/▼ disclosure arrow; other
    expandable entries (tool results, truncated output, etc.) are clickable
    but render without that arrow and without any style/color change
    between their collapsed and expanded states.
    """

    DEFAULT_CSS = """
    Transcript {
        overflow-x: hidden;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Committed, permanent lines (already finalized, never rewritten).
        self._lines: list[Strip] = []
        # The current in-progress line(s), which a subsequent replace_last
        # write will discard and redraw. Empty when nothing is pending.
        self._pending_strips: list[Strip] = []
        # Log of committed (renderable, strip_count, expandable_id) for re-rendering on resize.
        self._renderable_log: list[tuple[RenderableType, int, int | None]] = []
        self._pending_renderable: RenderableType | None = None
        # Shared id->state map for all clickable/expandable entries
        # (thinking traces as well as generic tool-result entries).
        self._expandable_entries: dict[int, dict[str, object]] = {}
        self._next_expandable_id = 1
        self._live_thinking_id: int | None = None
        # We only ever want vertical scrolling — content wraps to width by
        # design, so a horizontal scrollbar should never appear. This is
        # enforced via `overflow-x: hidden` in DEFAULT_CSS above (the
        # `show_horizontal_scrollbar` instance flag is NOT sufficient on its
        # own: Textual recomputes it during scrollbar arrangement based on
        # virtual_size vs. the content region, and virtual_size.width can
        # transiently be stale-wide right when a vertical scrollbar first
        # appears — see _content_width()/write() — which used to make
        # Textual think there was horizontal overflow and draw the h-bar
        # anyway. Setting overflow-x at the style level makes that
        # transient mismatch harmless.)
        # Dedicated off-screen console purely used to render Rich renderables
        # (Text, Markdown, ...) into Strips at an arbitrary width. Kept
        # separate from the app's real console so rendering never touches
        # the actual terminal.

        theme = Theme(
            {
                # Inline `code`
                "markdown.code": "#f3f3f3 on #2a2a2a",

                # Fenced code blocks
                "markdown.code_block": "#f3f3f3 on #2a2a2a",
            }
        )

        self._render_console = Console(
            file=io.StringIO(),
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            markup=False,
            safe_box=False,
            legacy_windows=False,
            soft_wrap=False,
            theme=theme,
        )

    # Must match `scrollbar-size-vertical` in DEFAULT_CSS.
    _SCROLLBAR_WIDTH = 1

    def _content_width(self) -> int:
        """The width to wrap/pad content to.

        Deliberately NOT derived from `scrollable_content_region`: that
        value only updates to reflect the vertical scrollbar *after*
        Textual's layout pass reacts to a virtual_size change, so on the
        exact write() that first pushes virtual_size past the visible
        height, scrollable_content_region.width is still the old, wider
        (no-scrollbar) value for that one frame. A line rendered at that
        stale width gets permanently wrapped too wide and is only ever
        cropped (never re-wrapped) afterwards by render_line, leaving a
        stray sliver of that line's last column poking out next to the
        scrollbar.

        Reserving the scrollbar's column unconditionally — regardless of
        whether it happens to be visible on any given frame — avoids the
        race outright, since a growing transcript will almost always need
        it eventually anyway.
        """
        width = (self.size.width or 80) - self._SCROLLBAR_WIDTH
        return max(width, 1)

    # -- rendering -----------------------------------------------------------
    def _render_to_strips(self, renderable: RenderableType, width: int) -> list[Strip]:
        options = self._render_console.options.update(width=max(width, 1), height=None)
        rendered = self._render_console.render_lines(renderable, options, pad=False)
        if not rendered:
            return [Strip([])]
        return [Strip(list(segments)) for segments in rendered]

    def _thinking_renderable(self, text: str, expanded: bool, width: int) -> Text:
        arrow = "▼" if expanded else "▶"
        prefix = f"{arrow} THINKING:"
        if expanded:
            return Text(f"{prefix} {text}", style=STYLE_DIM)
        one_line = " ".join(text.split())
        max_summary = max(width - len(prefix) - 1, 1)
        if len(one_line) > max_summary:
            one_line = one_line[: max(max_summary - 1, 1)] + "…"
        return Text(f"{prefix} {one_line}", style=STYLE_DIM)

    def _rebuild_committed_lines(self) -> None:
        width = self._content_width()
        new_lines = []
        rebuilt_log = []
        for entry in self._renderable_log:
            entry_id = entry[2] if len(entry) > 2 else None
            renderable = entry[0]
            if entry_id is not None:
                state = self._expandable_entries.get(entry_id)
                if state is None:
                    continue
                kind = state.get("kind", "thinking")
                if kind == "thinking":
                    # Thinking traces get the ▶/▼ disclosure arrow treatment.
                    if state.get("live"):
                        renderable = Text(f"THINKING: {state['text']}", style=STYLE_DIM)
                    else:
                        renderable = self._thinking_renderable(
                            str(state["text"]),
                            bool(state["expanded"]),
                            width,
                        )
                else:
                    # Generic expandable entries: no arrow, no color change —
                    # just swap between the pre-built summary/detail
                    # renderables the caller supplied.
                    renderable = state["detail"] if state.get("expanded") else state["summary"]
            strips = self._render_to_strips(renderable, width)
            new_lines.extend(strips)
            rebuilt_log.append((renderable, len(strips), entry_id))
        self._lines = new_lines
        self._renderable_log = rebuilt_log
        self.virtual_size = Size(width, len(self._lines) + len(self._pending_strips))
        self.show_horizontal_scrollbar = False
        self.refresh()

    def write(
        self,
        renderable: RenderableType,
        *,
        replace_last: bool = False,
        commit: bool = True,
        scroll_end: bool = True,
    ) -> None:
        """Render ``renderable`` and add it to the transcript.

        - ``replace_last=True`` discards whatever the previous *uncommitted*
          write showed and shows this instead (this is what makes \\r-style
          in-place updates work).
        - ``commit=False`` marks the newly written content as still
          "in progress" — the next replace_last write will overwrite it
          rather than stacking below it. ``commit=True`` (the default)
          finalizes it permanently.
        """
        width = self._content_width()
        new_strips = self._render_to_strips(renderable, width)

        was_at_bottom = self.scroll_y >= max(self.max_scroll_y - 1, 0)

        if replace_last:
            self._pending_strips = new_strips
            self._pending_renderable = renderable
            if commit:
                self._lines.extend(self._pending_strips)
                self._renderable_log.append((self._pending_renderable, len(self._pending_strips), None))
                self._pending_strips = []
                self._pending_renderable = None
        else:
            if self._pending_strips:
                self._lines.extend(self._pending_strips)
                if self._pending_renderable is not None:
                    self._renderable_log.append((self._pending_renderable, len(self._pending_strips), None))
                self._pending_strips = []
                self._pending_renderable = None
            self._lines.extend(new_strips)
            self._renderable_log.append((renderable, len(new_strips), None))

        total_lines = len(self._lines) + len(self._pending_strips)
        # NOTE: virtual_size is a reactive, but its watcher only fires when
        # the new Size differs from the old one. During streaming, repeated
        # replace_last(commit=False) writes redraw the *same* in-progress
        # line at the *same* width/line-count, so virtual_size is often
        # assigned an unchanged value and the reactive never fires. We must
        # call refresh() explicitly or those updates silently never repaint.
        self.virtual_size = Size(width, total_lines)
        # Defensive: some Textual versions recompute show_horizontal_scrollbar
        # as a side effect of the virtual_size watcher, which can re-enable
        # it even with overflow-x: hidden in CSS. Force it off again here,
        # synchronously, after that recompute has had a chance to run.
        self.show_horizontal_scrollbar = False

        if scroll_end and was_at_bottom:
            self.scroll_end(animate=False)
        self.refresh()

    def write_expandable(self, summary: RenderableType, detail: RenderableType) -> int:
        """Write a collapsed ``summary`` renderable; clicking it swaps in
        ``detail`` (and clicking again swaps back). Used for tool-result
        entries (system info, finder matches, truncated bash output, etc.)
        — unlike thinking traces, no disclosure arrow is added and no style
        is applied or changed by this method; callers are responsible for
        any styling baked into the renderables they pass in, and that
        styling should stay the same across the collapsed/expanded swap.

        Commits any still-pending (uncommitted, in-place-update) content
        first, so a live spinner-style line doesn't get silently dropped.
        """
        self._commit_pending()
        entry_id = self._next_expandable_id
        self._next_expandable_id += 1
        self._expandable_entries[entry_id] = {
            "kind": "generic",
            "summary": summary,
            "detail": detail,
            "expanded": False,
        }
        strips = self._render_to_strips(summary, self._content_width())
        self._lines.extend(strips)
        self._renderable_log.append((summary, len(strips), entry_id))
        self.virtual_size = Size(self._content_width(), len(self._lines) + len(self._pending_strips))
        self.show_horizontal_scrollbar = False
        self.refresh()
        return entry_id

    def discard_pending(self) -> None:
        """Drop any uncommitted in-place-update line without committing it
        to the permanent transcript. Used when a live streamed "current
        line" (e.g. the last line of running bash output) is about to be
        replaced by a proper summary/expandable block instead, so the raw
        last line doesn't linger as a leftover duplicate."""
        if not self._pending_strips and self._pending_renderable is None:
            return
        self._pending_strips = []
        self._pending_renderable = None
        self.virtual_size = Size(self._content_width(), len(self._lines))
        self.show_horizontal_scrollbar = False
        self.refresh()

    def clear(self) -> None:
        self._lines = []
        self._pending_strips = []
        self._renderable_log = []
        self._pending_renderable = None
        self._expandable_entries = {}
        self._next_expandable_id = 1
        self._live_thinking_id = None
        self.virtual_size = Size(self._content_width(), 0)
        self.show_horizontal_scrollbar = False
        self.scroll_home(animate=False)
        self.refresh()

    def _get_bg(self) -> RichStyle:
        bg = self.styles.background
        if bg and bg.a > 0:
            return RichStyle(bgcolor=f"rgb({bg.r},{bg.g},{bg.b})")
        return RichStyle()

    def render_line(self, y: int) -> Strip:
        scroll_y = int(self.scroll_offset.y)
        index = scroll_y + y
        committed = len(self._lines)
        bg = self._get_bg()
        if index < committed:
            strip = self._lines[index]
        elif index < committed + len(self._pending_strips):
            strip = self._pending_strips[index - committed]
        else:
            return Strip.blank(self._content_width(), bg)
        strip = strip.adjust_cell_length(self._content_width())
        if bg:
            strip = strip.apply_style(bg)
        return strip

    def on_resize(self) -> None:
        self._pending_strips = []
        self._pending_renderable = None
        self._rebuild_committed_lines()

    def _commit_pending(self) -> None:
        if not self._pending_strips:
            return
        self._lines.extend(self._pending_strips)
        if self._pending_renderable is not None:
            self._renderable_log.append((self._pending_renderable, len(self._pending_strips), None))
        self._pending_strips = []
        self._pending_renderable = None

    def append_thinking_delta(self, text: str) -> None:
        self._commit_pending()
        if self._live_thinking_id is None:
            thinking_id = self._next_expandable_id
            self._next_expandable_id += 1
            self._live_thinking_id = thinking_id
            self._expandable_entries[thinking_id] = {
                "kind": "thinking",
                "text": text,
                "expanded": False,
                "live": True,
            }
            renderable = Text(f"THINKING: {text}", style=STYLE_DIM)
            strips = self._render_to_strips(renderable, self._content_width())
            self._lines.extend(strips)
            self._renderable_log.append((renderable, len(strips), thinking_id))
            self.virtual_size = Size(self._content_width(), len(self._lines))
            self.show_horizontal_scrollbar = False
            self.scroll_end(animate=False)
            self.refresh()
            return

        state = self._expandable_entries.get(self._live_thinking_id)
        if state is not None:
            state["text"] = text
            self._rebuild_committed_lines()
            self.scroll_end(animate=False)

    def append_thinking_trace(self, text: str) -> None:
        if self._live_thinking_id is not None:
            state = self._expandable_entries.get(self._live_thinking_id)
            if state is not None:
                state["text"] = text
                state["expanded"] = False
                state["live"] = False
                self._live_thinking_id = None
                self._rebuild_committed_lines()
                self.scroll_end(animate=False)
                return
            self._live_thinking_id = None

        thinking_id = self._next_expandable_id
        self._next_expandable_id += 1
        self._expandable_entries[thinking_id] = {
            "kind": "thinking",
            "text": text,
            "expanded": False,
            "live": False,
        }
        renderable = self._thinking_renderable(text, False, self._content_width())
        strips = self._render_to_strips(renderable, self._content_width())
        self._commit_pending()
        self._lines.extend(strips)
        self._renderable_log.append((renderable, len(strips), thinking_id))
        self.virtual_size = Size(self._content_width(), len(self._lines))
        self.show_horizontal_scrollbar = False
        self.scroll_end(animate=False)
        self.refresh()

    def on_click(self, event) -> None:
        line_index = int(self.scroll_offset.y) + int(event.y)
        cursor = 0
        for _, strip_count, entry_id in self._renderable_log:
            if cursor <= line_index < cursor + strip_count:
                if entry_id is not None:
                    state = self._expandable_entries.get(entry_id)
                    if state is not None:
                        state["expanded"] = not bool(state["expanded"])
                        self._rebuild_committed_lines()
                        event.stop()
                return
            cursor += strip_count


class TextualAgentUI(AgentUI):
    """Adapter used by ToolAgent to render progress inside the Textual app."""

    def __init__(self, app: "CtermApp", run_id: int, cancel_event: threading.Event):
        self.app = app
        self.run_id = run_id
        self.cancel_event = cancel_event
        self._spinner_message = ""
        self._thinking_buffer = ""
        # Per-fd "current terminal line" state. \n commits the line
        # permanently; \r rewinds to the start of the still-open line so
        # subsequent text overwrites it in place — this is what makes
        # progress bars / spinners render correctly instead of scrolling.
        self._last_stream: dict[str, tuple[Text, str]] = {}
        # Full history of every streamed line per fd for the *currently
        # running* tool call. This is only a fallback source for bash
        # output now — the primary source is the result dict itself (see
        # _emit_bash_result_output) since live per-line fd streaming isn't
        # guaranteed to fire for every tool.
        self._stream_buffers: dict[str, list[str]] = {}
        # Name of the tool currently in flight, so handle_tool_output knows
        # which result-formatting branch to use.
        self._current_tool: str | None = None

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set() or not self.app.is_run_active(self.run_id)

    def _ensure_active(self) -> None:
        if self.is_cancelled():
            raise RunCancelled()

    def _emit(self, text: str, style: str = STYLE_TEXT) -> None:
        self._ensure_active()
        self.app.call_from_thread(self.app.append_line, text, style)

    # -- streaming tool output (stdout/stderr) ------------------------------
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
            self.app.call_from_thread(self.app.append_thinking_trace, full_text)

    def update_spinner(self, message):
        self._ensure_active()
        self._spinner_message = str(message)
        self.app.call_from_thread(self.app.set_status, f"{self._spinner_message}...")

    def stop_spinner(self):
        if self.is_cancelled():
            return
        self.app.call_from_thread(self.app.set_status, "")

    def tool_call(self, tool_name, args):
        # A new tool invocation starts a fresh output stream.
        self._reset_stream_state()
        self._current_tool = tool_name
        if tool_name == "bash":
            self._emit(f"$ {args.get('command', '')}", STYLE_TOOL)
        elif tool_name in ("exec_python", "exec"):
            # Show the code up front (the same view later reused, unmodified,
            # for the approval prompt — it is never re-rendered a second
            # time) rather than a one-line summary.
            code = args.get("code") or args.get("script") or args.get("source") or ""
            self._ensure_active()
            self.app.call_from_thread(self.app.append_code, "» running script", code)
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
            if self._current_tool == "bash":
                # Discard the live in-place "current line" — it's
                # superseded by a capped/expandable block built from the
                # full captured output in `result` itself, which is
                # authoritative (unlike live fd streaming, which isn't
                # guaranteed to have fired for every line).
                self._last_stream.clear()
                self._ensure_active()
                self.app.call_from_thread(self.app.discard_pending_stream)
                self._emit_bash_result_output(result)
                formatted, style = self._format_tool_result(result)
                if formatted:
                    self._emit(formatted, style)
                return

            # Command finished — flush any trailing partial line(s) first.
            for fd_name in ("stdout", "stderr"):
                self._commit_stream(fd_name)
            self._emit_tool_result(result)
            return

        if fd is None:
            return

        self._show_stream(fd, str(line), STYLE_TOOL_OUTPUT)

    def _emit_bash_result_output(self, result) -> None:
        """Build the capped/expandable output block for a finished bash
        command. Reads directly from the result payload's own
        stdout/stderr fields (the same shape used per-entry in
        handle_shell_result_output) since that's the authoritative full
        capture; falls back to whatever was live-streamed only if the
        result itself doesn't carry that content."""
        combined: list[str] = []
        if isinstance(result, dict):
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
        """Route a finished tool's result to the right formatter.

        system_info and finder results become clickable (collapsed summary
        that expands to the full data) on success; everything else falls
        back to the original plain-line ✓/✗ summary.
        """
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
        # Recursively pretty-print nested values instead of dumping their
        # Python/JSON repr on one line.
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

        # Flush any not-yet-committed live partial lines first.
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
        """Static, post-completion output summary — capped to
        MAX_TOOL_OUTPUT_LINES. If the output was actually truncated, the
        summary becomes clickable and expands to show up to
        MAX_EXPANDED_OUTPUT_LINES lines, with no arrow and no style change
        between the collapsed and expanded views."""
        if len(lines) <= MAX_TOOL_OUTPUT_LINES:
            for line in lines:
                self._emit(line, STYLE_TOOL_OUTPUT)
            return

        self._ensure_active()
        summary_lines = [Text(line, style=STYLE_TOOL_OUTPUT) for line in lines[:MAX_TOOL_OUTPUT_LINES]]
        summary_lines.append(Text("…", style=STYLE_TOOL_OUTPUT))

        expanded = lines[:MAX_EXPANDED_OUTPUT_LINES]
        detail_lines = [Text(line, style=STYLE_TOOL_OUTPUT) for line in expanded]
        if len(lines) > MAX_EXPANDED_OUTPUT_LINES:
            detail_lines.append(Text("…", style=STYLE_TOOL_OUTPUT))

        self.app.call_from_thread(
            self.app.append_expandable_result, Group(*summary_lines), Group(*detail_lines)
        )

    def approve_privileged_binary(self, binary):
        self._ensure_active()
        event = threading.Event()
        request = ApprovalRequest(self.run_id, binary, event)
        self.app.call_from_thread(self.app.start_approval_prompt, request)
        event.wait()
        return bool(request.answer)

    def approve_python_code(self, code):
        self._ensure_active()
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


class CtermApp(App[int]):
    """Default interactive cterm screen."""

    CSS = """
    Screen {
        background: #1e1e1e;
        color: #f3f3f3;
        border: none;
        outline: none;
    }

    #outer {
        height: 100%;
        width: 100%;
        padding: 0 2 1 0;
        border: none;
        outline: none;
    }

    #frame {
        height: 1fr;
        width: 100%;
        padding: 1 2 0 2;
    }

    #query_bar {
        height: auto;
        width: 100%;
        color: #f3f3f3;
        text-style: bold;
        margin-bottom: 1;
    }

    #body {
        height: 1fr;
        width: 100%;
        border-left: solid #f3f3f3;
        padding-left: 1;
        margin-left: 2;
    }

    #transcript {
        height: 1fr;
        width: 100%;
        background: #1e1e1e;
        color: #f3f3f3;
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
        scrollbar-background: #1e1e1e;
        scrollbar-color: #f3f3f3;
        scrollbar-color-hover: #f3f3f3;
        scrollbar-color-active: #f3f3f3;
    }

    #status {
        height: 1;
        color: #7d8a99;
        margin-top: 1;
    }

    #prompt_line {
        height: 3;
        width: 100%;
        margin-top: 1;
        margin-bottom: 1;
        background: rgba(66, 56, 56, 0.4);
        padding: 0 1;
        align: left middle;
    }

    #prompt_marker {
        width: 3;
        height: 1;
        content-align: left middle;
        color: #9a4f4f;
    }

    #prompt {
        height: 1;
        width: 1fr;
        border: none;
        background: transparent;
        color: #f3f3f3;
        padding: 0;
    }

    #prompt:focus {
        border: none;
        background: transparent;
    }

    #footer {
        height: 1;
        width: 100%;
        color: #8a858b;
    }

    #model {
        margin-left: 1;
        width: 1fr;
    }

    #keys {
        width: 2fr;
        text-align: right;
    }
    """

    BINDINGS = [
        ("escape", "interrupt", "Interrupt"),
        ("ctrl+c", "interrupt", "Interrupt"),
        ("ctrl+q", "quit", "Quit"),
        ("up", "previous_history", "History Up"),
        ("down", "next_history", "History Down"),
    ]

    def __init__(
        self,
        chat_runner: Callable[[str, TextualAgentUI], ChatResult],
        model_label: str,
        *,
        debug: bool = False,
    ):
        super().__init__()
        self.chat_runner = chat_runner
        self.model_label = model_label
        self.cterm_debug = debug
        self._busy = False
        self._active_run_id = 0
        self._approval_request: ApprovalRequest | None = None
        self._active_cancel_event: threading.Event | None = None
        self._chat_worker = None
        self._chat_thread_id: int | None = None
        self._spinner_message = ""
        self._spinner_frame_index = 0
        self._spinner_timer: Timer | None = None
        self._history = History()

    def compose(self) -> ComposeResult:
        with Vertical(id="outer"):
            with Vertical(id="frame"):
                yield Static("", id="query_bar")
                with Vertical(id="body"):
                    yield Transcript(id="transcript")
                    yield Static("", id="status")
            with Horizontal(id="prompt_line"):
                yield Static(">", id="prompt_marker")
                yield Input(id="prompt", placeholder="Type your Request...")
            with Horizontal(id="footer"):
                yield Static(self.model_label, id="model")
                yield Static("esc Interrupt • ↑/↓ History • Tab Inspect", id="keys")

    def on_mount(self) -> None:
        self.query_one("#query_bar", Static).display = False
        self.query_one("#prompt", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if self._approval_request is not None:
            self.finish_approval_prompt(text)
            event.input.value = ""
            return
        if not text:
            return
        if self._busy:
            self.bell()
            return
        self._history.add(text)
        event.input.value = ""
        # The active query is pinned above the transcript instead of being
        # written into the scrolling log, so it stays visible while the
        # transcript below it scrolls.
        query_bar = self.query_one("#query_bar", Static)
        query_bar.update(Text(f"> {text}", style="bold #f3f3f3"))
        query_bar.display = True
        self.query_one("#transcript", Transcript).clear()
        self._active_run_id += 1
        self._active_cancel_event = threading.Event()
        self._chat_thread_id = None
        self._busy = True
        self._chat_worker = self.run_chat(text, self._active_run_id)

    def append_line(self, text: str, style: str = STYLE_TEXT, end: str = "\n") -> None:
        transcript = self.query_one("#transcript", Transcript)
        clean = _ANSI_RE.sub("", text)
        if end == "" and clean:
            transcript.write(Text(clean, style=style))
            return
        for line in clean.splitlines() or [""]:
            transcript.write(Text(line, style=style))

    def append_stream(self, renderable: RenderableType, replace_last: bool, commit: bool) -> None:
        """Low-level hook used by TextualAgentUI for live \\r/\\n-aware output."""
        transcript = self.query_one("#transcript", Transcript)
        transcript.write(renderable, replace_last=replace_last, commit=commit)

    def discard_pending_stream(self) -> None:
        """Drop the live in-progress streamed line without committing it —
        used right before showing a proper capped/expandable output block
        for a finished bash command, so the raw last line doesn't linger
        alongside the summary."""
        transcript = self.query_one("#transcript", Transcript)
        transcript.discard_pending()

    def append_expandable_result(self, summary: RenderableType, detail: RenderableType) -> None:
        """Write a clickable tool-result line: collapsed by default, swaps
        to the full detail view on click (and back on a second click)."""
        transcript = self.query_one("#transcript", Transcript)
        transcript.write_expandable(summary, detail)

    def append_code(self, title: str, code: str, language: str = "python") -> None:
        """Show a tool-invocation title followed by the code itself,
        rendered with the same syntax view used for approval prompts. This
        is the only place the code is rendered — the approval prompt (if
        one is needed) does not re-display it."""
        transcript = self.query_one("#transcript", Transcript)
        transcript.write(Text(title, style=STYLE_TOOL))
        transcript.write(Syntax(code or "", language, theme="monokai", line_numbers=True))

    def append_markdown(self, text: str, ok: bool = True) -> None:
        """Render the final answer as Markdown so tables/emphasis show correctly."""
        transcript = self.query_one("#transcript", Transcript)
        if ok:
            transcript.write(Markdown(text, style=STYLE_TEXT))
        else:
            for line in text.splitlines() or [""]:
                transcript.write(Text(line, style=STYLE_ERROR))

    def append_thinking_trace(self, text: str) -> None:
        transcript = self.query_one("#transcript", Transcript)
        transcript.append_thinking_trace(text)

    def append_thinking_delta(self, text: str) -> None:
        transcript = self.query_one("#transcript", Transcript)
        transcript.append_thinking_delta(text)

    # -- Spinner -----------------------------------------------------------
    def set_status(self, text: str) -> None:
        self._spinner_message = text
        if text:
            self._start_spinner()
        else:
            self._stop_spinner()

    def start_python_approval_prompt(self, request: ApprovalRequest) -> None:
        if not self.is_run_active(request.run_id):
            request.answer = False
            request.event.set()
            return
        self._approval_request = request
        self.set_status("")
        # NOTE: the code itself is intentionally NOT re-rendered here — it
        # was already shown in full by append_code() when the tool call
        # started (title "» running script"). Only the approval question is
        # shown, referring back to that code above.
        self.append_line("The script above requires approval to run.", STYLE_WARNING)
        self.append_line("Execute this Python code? [Y/N]", STYLE_WARNING)
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.placeholder = "y or n"
        prompt.value = ""
        prompt.focus()

    # -- Privileged command approval ---------------------------------------
    def start_approval_prompt(self, request: ApprovalRequest) -> None:
        if not self.is_run_active(request.run_id):
            request.answer = False
            request.event.set()
            return
        self._approval_request = request
        self.set_status("")
        self.append_line(f"Allow sudo access for {request.binary}? [Y/N]", STYLE_WARNING)
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.placeholder = "y or n"
        prompt.value = ""
        prompt.focus()

    def finish_approval_prompt(self, text: str) -> None:
        request = self._approval_request
        if request is None:
            return
        request.answer = text.strip().lower() in {"y", "yes"}
        self._approval_request = None
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = ""
        prompt.disabled = False
        request.event.set()

    def _start_spinner(self) -> None:
        if self._spinner_timer is None:
            self._spinner_frame_index = 0
            self._spinner_timer = self.set_interval(_SPINNER_INTERVAL, self._advance_spinner)
        self._advance_spinner()

    def _advance_spinner(self) -> None:
        frame = _SPINNER_FRAMES[self._spinner_frame_index % len(_SPINNER_FRAMES)]
        self._spinner_frame_index += 1
        status = self.query_one("#status", Static)
        status.update(Text(f"{frame} {self._spinner_message}", style=STYLE_SUCCESS))

    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self.query_one("#status", Static).update("")

    # -- Busy state ----------------------------------------------------------
    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        prompt = self.query_one("#prompt", Input)
        if not busy:
            prompt.disabled = False
            prompt.placeholder = ""
            prompt.focus()

    def action_previous_history(self) -> None:
        prompt = self.query_one("#prompt", Input)
        value = self._history.previous()
        prompt.value = value

    def action_next_history(self) -> None:
        prompt = self.query_one("#prompt", Input)
        value = self._history.next()
        prompt.value = value

    def is_run_active(self, run_id: int) -> bool:
        return self._busy and self._active_run_id == run_id

    def _cancel_active_run(self, *, force_thread: bool = True) -> None:
        if self._active_cancel_event is not None:
            self._active_cancel_event.set()
        worker = self._chat_worker
        if worker is not None and hasattr(worker, "cancel"):
            try:
                worker.cancel()
            except Exception:
                pass
        if force_thread:
            _raise_in_thread(self._chat_thread_id, RunCancelled)

    def action_interrupt(self) -> None:
        if self._busy:
            self._cancel_active_run()
            if self._approval_request is not None:
                self._approval_request.answer = False
                self._approval_request.event.set()
                self._approval_request = None
            self._active_run_id += 1
            self.set_status("")
            self.set_busy(False)
            self.append_line("Interrupted.", STYLE_WARNING)
        else:
            self._cancel_active_run()
            if self._chat_thread_id is not None:
                timer = threading.Timer(0.5, lambda: os._exit(0))
                timer.daemon = True
                timer.start()
            self.exit(0)

    @work(exclusive=True, thread=True)
    def run_chat(self, message: str, run_id: int) -> None:
        self._chat_thread_id = threading.get_ident()
        cancel_event = self._active_cancel_event or threading.Event()
        ui = TextualAgentUI(self, run_id, cancel_event)
        try:
            result = self.chat_runner(message, ui)
            if not self.is_run_active(run_id):
                return
            self.call_from_thread(self.append_line, "")
            self.call_from_thread(self.append_markdown, result.text, result.ok)
            if result.log_path:
                self.call_from_thread(self.append_line, f"(log: {result.log_path})", STYLE_DIM)
        except RunCancelled:
            return
        except Exception as exc:
            if self.is_run_active(run_id):
                self.call_from_thread(self.append_line, f"Error: {exc}", STYLE_ERROR)
        finally:
            if self._active_run_id == run_id:
                self._chat_thread_id = None
                self._chat_worker = None
            if self.is_run_active(run_id):
                self.call_from_thread(self.set_status, "")
                self.call_from_thread(self.set_busy, False)