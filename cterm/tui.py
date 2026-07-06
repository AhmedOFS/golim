"""Textual interface for the default cterm command."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Input, RichLog, Static


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


@dataclass
class ChatResult:
    ok: bool
    text: str
    log_path: str | None = None


class TextualAgentUI:
    """Adapter used by ToolAgent to render progress inside the Textual app."""

    def __init__(self, app: "CtermApp", run_id: int):
        self.app = app
        self.run_id = run_id
        self._spinner_message = ""

    def _emit(self, text: str, style: str = STYLE_TEXT, end: str = "\n") -> None:
        if not self.app.is_run_active(self.run_id):
            return
        self.app.call_from_thread(self.app.append_line, text, style, end)

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
        if tool_name == "bash":
            self._emit(f"$ {args.get('command', '')}", STYLE_TOOL)
        elif tool_name in ("exec_python", "exec"):
            code = args.get("code") or args.get("script") or args.get("source") or ""
            first_line = code.strip().split("\n")[0] if code else ""
            self._emit(f"exec: {first_line}", STYLE_TOOL)
        elif tool_name == "finder":
            self._emit(
                f"◎ finding: {args.get('pattern', '')} in {args.get('path', '')}",
                STYLE_TOOL,
            )
        else:
            self._emit(f"{tool_name}: {', '.join(args.keys()) if args else ''}", STYLE_TOOL)

    def handle_tool_output(self, fd=None, line="", end="\n", result=None):
        if result is not None:
            formatted, style = self._format_tool_result(result)
            if formatted:
                self._emit(formatted, style)
            return

        if fd is not None:
            style = STYLE_WARNING if fd == "stderr" else STYLE_TEXT
            self._emit(str(line), style, end=end)

    def handle_shell_result_output(self, result):
        if not isinstance(result, dict):
            return

        for entry in result.get("results", []):
            if not isinstance(entry, dict):
                continue
            stdout = entry.get("stdout")
            if stdout:
                self._emit(str(stdout).rstrip("\n"), STYLE_TEXT)
            stderr = entry.get("stderr")
            if stderr:
                self._emit(str(stderr).rstrip("\n"), STYLE_WARNING)

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
        background: #1f1f1f;
        color: #f3f3f3;
    }

    #outer {
        height: 100%;
        width: 100%;
        padding: 2 4 1 4;
        background: #1f1f1f;
    }

    #frame {
        height: 1fr;
        width: 100%;
        border-left: thick #2e8cff;
        background: #000000;
        padding: 1 2 0 2;
    }

    #query_bar {
        height: auto;
        width: 100%;
        color: #f3f3f3;
        text-style: bold;
        background: #000000;
        margin-bottom: 1;
    }

    #transcript {
        height: 1fr;
        width: 100%;
        background: #000000;
        color: #f3f3f3;
        scrollbar-background: #000000;
        scrollbar-color: #2e8cff;
        scrollbar-color-hover: #65a8ef;
        scrollbar-color-active: #65a8ef;
    }

    #status {
        height: 1;
        color: #7d8a99;
        background: #000000;
        margin-top: 1;
    }

    #prompt_line {
        height: 3;
        width: 100%;
        background: #171313;
        margin-top: 1;
        padding: 0 1;
    }

    #prompt_marker {
        width: 3;
        height: 3;
        content-align: left middle;
        color: #9a4f4f;
        background: #171313;
    }

    #prompt {
        height: 3;
        width: 1fr;
        border: none;
        background: #171313;
        color: #f3f3f3;
        padding: 0;
    }

    #prompt:focus {
        border: none;
    }

    #footer {
        height: 1;
        width: 100%;
        color: #8a858b;
        background: #000000;
    }

    #model {
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
        self._spinner_message = ""
        self._spinner_frame_index = 0
        self._spinner_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="outer"):
            with Vertical(id="frame"):
                yield Static("", id="query_bar")
                yield RichLog(id="transcript", markup=False, highlight=False, wrap=True)
                yield Static("", id="status")
            with Horizontal(id="prompt_line"):
                yield Static(">", id="prompt_marker")
                yield Input(id="prompt", placeholder="")
            with Horizontal(id="footer"):
                yield Static(self.model_label, id="model")
                yield Static("esc Interrupt • ↑/↓ History • Tab Inspect", id="keys")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self._busy:
            return
        event.input.value = ""
        # The active query is pinned above the transcript instead of being
        # written into the scrolling log, so it stays visible while the
        # transcript below it scrolls.
        self.query_one("#query_bar", Static).update(Text(f"> {text}", style="bold #f3f3f3"))
        self.query_one("#transcript", RichLog).clear()
        self._active_run_id += 1
        self.run_chat(text, self._active_run_id)

    def append_line(self, text: str, style: str = STYLE_TEXT, end: str = "\n") -> None:
        log = self.query_one("#transcript", RichLog)
        clean = _ANSI_RE.sub("", text)
        if end == "" and clean:
            log.write(Text(clean, style=style), scroll_end=True)
            return
        for line in clean.splitlines() or [""]:
            log.write(Text(line, style=style), scroll_end=True)

    def append_markdown(self, text: str, ok: bool = True) -> None:
        """Render the final answer as Markdown so tables/emphasis show correctly."""
        log = self.query_one("#transcript", RichLog)
        if ok:
            log.write(Markdown(text, style=STYLE_TEXT), scroll_end=True)
        else:
            for line in text.splitlines() or [""]:
                log.write(Text(line, style=STYLE_ERROR), scroll_end=True)

    # -- Spinner -----------------------------------------------------------
    def set_status(self, text: str) -> None:
        self._spinner_message = text
        if text:
            self._start_spinner()
        else:
            self._stop_spinner()

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
        prompt.disabled = busy
        if not busy:
            prompt.focus()

    def is_run_active(self, run_id: int) -> bool:
        return self._busy and self._active_run_id == run_id

    def action_interrupt(self) -> None:
        if self._busy:
            self._active_run_id += 1
            self.set_status("")
            self.set_busy(False)
            self.append_line("Interrupted.", STYLE_WARNING)
        else:
            self.exit(0)

    @work(exclusive=True, thread=True)
    def run_chat(self, message: str, run_id: int) -> None:
        self.call_from_thread(self.set_busy, True)
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