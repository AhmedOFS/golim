"""Textual interface for the default cterm command."""

from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass
from typing import Callable
from rich.theme import Theme
from rich.console import Console, RenderableType
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


_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

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
            if commit:
                self._lines.extend(self._pending_strips)
                self._pending_strips = []
        else:
            # A non-replacing write always finalizes whatever was pending
            # first, then appends fresh, permanent content after it.
            if self._pending_strips:
                self._lines.extend(self._pending_strips)
                self._pending_strips = []
            self._lines.extend(new_strips)

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

    def clear(self) -> None:
        self._lines = []
        self._pending_strips = []
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
        # Note: existing lines were wrapped at their original write-time
        # width and are not re-wrapped on resize (cropped/padded only).
        # This mirrors the same known limitation stock RichLog has.
        total_lines = len(self._lines) + len(self._pending_strips)
        self.virtual_size = Size(self._content_width(), total_lines)
        self.show_horizontal_scrollbar = False
        self.refresh()


class TextualAgentUI(AgentUI):
    """Adapter used by ToolAgent to render progress inside the Textual app."""

    def __init__(self, app: "CtermApp", run_id: int):
        self.app = app
        self.run_id = run_id
        self._spinner_message = ""
        # Per-fd "current terminal line" state. \n commits the line
        # permanently; \r rewinds to the start of the still-open line so
        # subsequent text overwrites it in place — this is what makes
        # progress bars / spinners render correctly instead of scrolling.
        self._last_stream: dict[str, tuple[Text, str]] = {}

    def _emit(self, text: str, style: str = STYLE_TEXT) -> None:
        if not self.app.is_run_active(self.run_id):
            return
        self.app.call_from_thread(self.app.append_line, text, style)

    # -- streaming tool output (stdout/stderr) ------------------------------
    def _reset_stream_state(self) -> None:
        self._last_stream.clear()

    def _show_stream(self, fd: str, text: str, style: str) -> None:
        if not self.app.is_run_active(self.run_id):
            return
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
        if entry is None or not self.app.is_run_active(self.run_id):
            return
        renderable, _ = entry
        self.app.call_from_thread(
            self.app.append_stream,
            renderable,
            True,
            True,
        )

    def message(self, text):
        self._emit(_ANSI_RE.sub("", str(text)))

    def update_spinner(self, message):
        if not self.app.is_run_active(self.run_id):
            return
        self._spinner_message = str(message)
        self.app.call_from_thread(self.app.set_status, f"{self._spinner_message}...")

    def stop_spinner(self):
        if not self.app.is_run_active(self.run_id):
            return
        self.app.call_from_thread(self.app.set_status, "")

    def tool_call(self, tool_name, args):
        # A new tool invocation starts a fresh output stream.
        self._reset_stream_state()
        if tool_name == "bash":
            self._emit(f"$ {args.get('command', '')}", STYLE_TOOL)
        elif tool_name in ("exec_python", "exec"):
            code = args.get("code") or args.get("script") or args.get("source") or ""
            first_line = code.strip().split("\n")[0] if code else ""
            self._emit(f"executing: {first_line}", STYLE_TOOL)
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
            # Command finished — flush any trailing partial line(s) first.
            for fd_name in ("stdout", "stderr"):
                self._commit_stream(fd_name)
            formatted, style = self._format_tool_result(result)
            if formatted:
                self._emit(formatted, style)
            return

        if fd is None:
            return

        self._show_stream(fd, str(line), STYLE_TOOL_OUTPUT)

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
        """Used only for the static, post-completion output summary — capped
        to MAX_TOOL_OUTPUT_LINES since it's a snapshot, not a live tail."""
        lines = text.splitlines() or [text]
        for line in lines[:MAX_TOOL_OUTPUT_LINES]:
            self._emit(line, STYLE_TOOL_OUTPUT)
        if len(lines) > MAX_TOOL_OUTPUT_LINES:
            self._emit("…", STYLE_TOOL_OUTPUT)

    def approve_privileged_binary(self, binary):
        if not self.app.is_run_active(self.run_id):
            return False
        event = threading.Event()
        request = ApprovalRequest(self.run_id, binary, event)
        self.app.call_from_thread(self.app.start_approval_prompt, request)
        event.wait()
        return bool(request.answer)

    def approve_python_code(self, code):
        if not self.app.is_run_active(self.run_id):
            return False
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
        scrollbar-color: #2e8cff;
        scrollbar-color-hover: #65a8ef;
        scrollbar-color-active: #65a8ef;
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
        color: transparent;
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
        self._spinner_message = ""
        self._spinner_frame_index = 0
        self._spinner_timer: Timer | None = None

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
            # A run is already in flight — don't start a second concurrent
            # run_chat. Leave the typed text in place so the user can just
            # press Enter again once the current run finishes.
            self.bell()
            return
        event.input.value = ""
        # The active query is pinned above the transcript instead of being
        # written into the scrolling log, so it stays visible while the
        # transcript below it scrolls.
        query_bar = self.query_one("#query_bar", Static)
        query_bar.update(Text(f"> {text}", style="bold #f3f3f3"))
        query_bar.display = True
        self.query_one("#transcript", Transcript).clear()
        self._active_run_id += 1
        self._busy = True
        self.run_chat(text, self._active_run_id)

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

    def append_markdown(self, text: str, ok: bool = True) -> None:
        """Render the final answer as Markdown so tables/emphasis show correctly."""
        transcript = self.query_one("#transcript", Transcript)
        if ok:
            transcript.write(Markdown(text, style=STYLE_TEXT))
        else:
            for line in text.splitlines() or [""]:
                transcript.write(Text(line, style=STYLE_ERROR))

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
        syntax = Syntax(request.code or "", "python", theme="monokai", line_numbers=True)
        self.append_line("Python code requires approval:", STYLE_WARNING)
        transcript = self.query_one("#transcript", Transcript)
        transcript.write(syntax)
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

    def is_run_active(self, run_id: int) -> bool:
        return self._busy and self._active_run_id == run_id

    def action_interrupt(self) -> None:
        if self._busy:
            if self._approval_request is not None:
                self._approval_request.answer = False
                self._approval_request.event.set()
                self._approval_request = None
            self._active_run_id += 1
            self.set_status("")
            self.set_busy(False)
            self.append_line("Interrupted.", STYLE_WARNING)
        else:
            self.exit(0)

    @work(exclusive=True, thread=True)
    def run_chat(self, message: str, run_id: int) -> None:
        ui = TextualAgentUI(self, run_id)
        try:
            result = self.chat_runner(message, ui)
            if not self.is_run_active(run_id):
                return
            self.call_from_thread(self.append_line, "")
            self.call_from_thread(self.append_markdown, result.text, result.ok)
            if result.log_path:
                self.call_from_thread(self.append_line, f"(log: {result.log_path})", STYLE_DIM)
        except Exception as exc:
            if self.is_run_active(run_id):
                self.call_from_thread(self.append_line, f"Error: {exc}", STYLE_ERROR)
        finally:
            if self.is_run_active(run_id):
                self.call_from_thread(self.set_status, "")
                self.call_from_thread(self.set_busy, False)