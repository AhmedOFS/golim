"""Textual interface for the default golim command."""

from __future__ import annotations

import os
import threading
from rich.console import RenderableType
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, OptionList

from golim.core.agent_events import AgentEvents
from golim.config import Config, get_config
from golim.core.runtime import Runtime
from golim.ui.tui.config.config_tui import (
    ConfigPromptHandle,
    _ConfigCancelled,
    run_config,
)
from golim.ui.tui.config.mixin import (
    ConfigUIMixin,
    InputRequest,
    MessageRequest,
    ModelSearchRequest,
    SelectRequest,
)
from golim.ui.tui.tui_style import (
    BG_DARK,
    BG_PITCH_BLACK,
    BORDER,
    CODE_BG,
    DIM,
    ERROR,
    FOOTER,
    PROMPT_MARKER,
    STYLE_DIM,
    STYLE_ERROR,
    STYLE_SUCCESS,
    STYLE_TEXT,
    STYLE_TOOL,
    STYLE_WARNING,
    SUCCESS,
    WHITE,
)
from golim.ui.tui.app.widgets.transcript import Transcript
from golim.ui.tui.app.widgets.home import HomeScreen
from golim.ui.tui.app.widgets.spinner import Spinner
from golim.ui.tui.app.widgets.query_bar import QueryBar
from golim.ui.tui.app.widgets.footer import Footer
from golim.ui.tui.app.widgets.prompt_line import PromptLine
from golim.ui.tui.config.widgets.config_panel import ConfigPanel
from golim.ui.tui.app.widgets.menu import (
    MAIN_MENU,
    MODEL_MENU,
    SETTINGS_MENU,
    MenuPanel,
)
from golim.config.utils import get_configured_model_choices, resolve_provider_settings
from golim.logger import start_run_logging

from golim.ui.tui.app.transcript_writer import TranscriptWriter, strip_emojis
from golim.ui.tui.app.agent_events_handler import (
    _ANSI_RE,
    ApprovalRequest,
    ChatResult,
    TUIAgentEventsHandler,
)


class GolimApp(ConfigUIMixin, App[int]):

    _config_prefix = "config_"
    _menu_hint = "↑/↓ move • Enter select • Esc close"
    _config_hint = "↑/↓ move • Enter select • Esc back"
    """Default interactive golim screen."""

    CSS = f"""
    Screen {{
        background: {BG_DARK};
        color: {WHITE};
        border: none;
        outline: none;
    }}

    Screen.-pitch-black {{
        background: {BG_PITCH_BLACK};
    }}

    Screen.-pitch-black #transcript {{
        background: {BG_PITCH_BLACK};
        scrollbar-background: {BG_PITCH_BLACK};
    }}

    #outer {{
        height: 100%;
        width: 100%;
        padding: 0 0 1 0;
        border: none;
        outline: none;
    }}

    #frame {{
        height: 1fr;
        width: 100%;
        padding: 1 0 0 2;
        margin-right: 2;
    }}

    #home_screen {{
        height: 1fr;
        width: 100%;
        content-align: center middle;
        color: {WHITE};
    }}

    #query_bar {{
        height: auto;
        width: 100%;
        margin-left: 1;
        color: {WHITE};
        text-style: bold;
        margin-bottom: 1;
    }}

    #body {{
        height: 1fr;
        width: 100%;
        margin-left: 1;
    }}

    #transcript {{
        height: 1fr;
        width: 1fr;
        margin-left: 0;
        background: {BG_DARK};
        color: {WHITE};
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
        scrollbar-background: {BG_DARK};
        scrollbar-color: {WHITE};
        scrollbar-color-hover: {WHITE};
        scrollbar-color-active: {WHITE};
    }}

    #status {{
        height: 1;
        color: {SUCCESS};
        margin-left: 1;
        margin-top: 1;
    }}

    #prompt_line {{
        height: 3;
        width: 100%;
        margin-top: 1;
        margin-bottom: 1;
        background: transparent;
        border-top: hkey {DIM};
        border-bottom: hkey {DIM};
        padding: 0 1;
        align: left middle;
    }}

    #prompt_marker {{
        width: 3;
        height: 1;
        content-align: left middle;
        color: {PROMPT_MARKER};
    }}

    #prompt {{
        height: 1;
        width: 1fr;
        border: none;
        background: transparent;
        color: {WHITE};
        padding: 0;
    }}

    #prompt:focus {{
        border: none;
        background: transparent;
    }}

    #footer {{
        height: 1;
        width: 100%;
        color: {FOOTER};
        margin-right: 2;
    }}

    #config_panel {{
        height: 1fr;
        width: 100%;
        padding: 1 2 0 2;
        margin-right: 2;
    }}

    #menu_panel {{
        height: 1fr;
        width: 100%;
        padding: 1 2 0 2;
        margin-right: 2;
    }}

    #menu_title {{
        height: 1;
        color: {WHITE};
        text-style: bold;
        margin-bottom: 1;
    }}

    #menu_options {{
        height: auto;
        max-height: 14;
        width: 100%;
        background: transparent;
        border: none;
        padding: 0;
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
        scrollbar-background: transparent;
        scrollbar-color: {WHITE};
        scrollbar-color-hover: {WHITE};
        scrollbar-color-active: {WHITE};
    }}

    #menu_options > .option-list--option-highlighted {{
        background: {WHITE} 12%;
        text-style: bold;
    }}

    #menu_input {{
        height: 3;
        width: 56;
        background: transparent;
        border: round {BORDER};
        padding: 0 1;
        margin-bottom: 1;
    }}

    #config_title {{
        height: 1;
        color: {WHITE};
        text-style: bold;
        margin-bottom: 1;
    }}

    #config_transcript {{
        height: 1fr;
        max-height: 8;
        width: 100%;
        background: transparent;
        color: {DIM};
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
        scrollbar-background: transparent;
        scrollbar-color: {WHITE};
        margin-bottom: 1;
    }}

    #config_prompt_label {{
        height: auto;
        min-height: 1;
        color: {WHITE};
        text-style: bold;
        margin-bottom: 1;
    }}

    #config_option_list {{
        height: auto;
        max-height: 12;
        width: 100%;
        background: transparent;
        border: none;
        padding: 0;
        margin-top: 0;
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
        scrollbar-background: transparent;
        scrollbar-color: {WHITE};
        scrollbar-color-hover: {WHITE};
        scrollbar-color-active: {WHITE};
    }}

    #config_option_list > .option-list--option-highlighted {{
        background: {WHITE} 12%;
        text-style: bold;
    }}

    #config_option_list.model_search {{
        max-height: 6;
    }}

    #config_text_input {{
        height: 3;
        width: 56;
        background: transparent;
        border: round {BORDER};
        padding: 0 1;
        margin-top: 0;
    }}

    #config_text_input:focus {{
        border: round {WHITE};
    }}

    .hidden {{
        display: none;
    }}

    #model {{
        margin-left: 1;
        width: 1fr;
    }}

    #keys {{
        width: 2fr;
        text-align: right;
    }}
    """

    BINDINGS = [
        ("escape", "interrupt", "Interrupt"),
        ("ctrl+q", "quit", "Quit"),
        ("up", "previous_history", "History Up"),
        ("down", "next_history", "History Down"),
        ("tab", "open_config", "Menu"),
    ]

    def __init__(
        self,
        model_label: str,
        *,
        config: Config | None = None,
        model: str | None = None,
        binary: str = "ollama",
        runtime_error: str | None = None,
    ):
        super().__init__()
        self._config = config or get_config()
        self._model = model
        self._binary = binary
        self._runtime_error = runtime_error
        self._runtime: Runtime | None = None
        self.model_label = model_label
        self._busy = False
        self._active_run_id = 0
        self._approval_request: ApprovalRequest | None = None
        self._auth_request = None
        self._active_cancel_event: threading.Event | None = None
        self._chat_worker = None
        self._chat_thread_id: int | None = None
        self._pending_followup: tuple[str, bool] | None = None
        self._pending_followup_lock = threading.Lock()
        self._has_completed_query = False
        self._config_active = False
        self._request: SelectRequest | InputRequest | ModelSearchRequest | MessageRequest | None = None
        self._model_search_results: list[str] = []
        self._cancelled = False
        self._config_result = 1
        self._config_thread: threading.Thread | None = None
        self._config_error: str | None = None
        self._menu_active = False
        self._menu_page = "main"
        self._menu_labels: list[str] = []
        self._menu_choices: dict[str, tuple[str, str]] = {}
    def _get_runtime(self) -> Runtime | None:
        if self._model is None:
            return None
        if self._runtime is None:
            self._runtime = Runtime(
                config=self._config,
                model=self._model,
                binary=self._binary,
            )
        return self._runtime

    def _resolve_current_settings(self) -> tuple[str | None, str | None, str]:
        return resolve_provider_settings(self._config, self._binary)

    def _reload_config_settings(self) -> None:
        self._config.reload()
        self._runtime_error, self._model, self.model_label = self._resolve_current_settings()
        self.query_one("#footer", Footer).set_model(self.model_label)
        if self._runtime is not None:
            self._runtime.terminate()
            self._runtime = None

    def compose(self) -> ComposeResult:
        with Vertical(id="outer"):
            with Vertical(id="frame"):
                yield HomeScreen(id="home_screen")
                yield QueryBar(id="query_bar")
                with Vertical(id="body", classes="hidden"):
                    yield Transcript(id="transcript")
                    yield Spinner(id="status")
            yield ConfigPanel(id="config_panel", prefix="config_", classes="hidden")
            yield MenuPanel(id="menu_panel", classes="hidden")
            yield PromptLine(id="prompt_line")
            yield Footer(self.model_label, id="footer")

    def _apply_dark_mode(self, dark: bool) -> None:
        self.dark = dark
        self.screen.set_class(dark, "-pitch-black")

    def on_mount(self) -> None:
        self.query_one("#transcript", Transcript).set_query_header_callback(
            self._update_query_header
        )
        self._show_home_screen()
        self.update_prompt_placeholder()
        self.query_one(PromptLine).focus_input()
        if not self._config.is_complete():
            self._open_provider_config(mode="initial")
        self._apply_dark_mode(self._config.dark_mode)

    def on_unmount(self) -> None:
        if self._runtime is not None:
            self._runtime.terminate()

    def on_key(self, event) -> None:
        if event.key in {"tab", "ctrl+i"} and not self._config_active and not self._menu_active:
            event.stop()
            event.prevent_default()
            self.action_open_config()
            return
        if self._config_active:
            self._handle_config_key(event)
        elif self._menu_active:
            self._handle_menu_key(event)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._config_active:
            if event.input.id == f"{self._config_prefix}text_input":
                self._handle_config_input_submitted(event)
            else:
                self.bell()
            return
        if self._menu_active:
            self._handle_menu_input_submitted(event)
            return
        text = event.value.strip()
        if self._auth_request is not None:
            # The password is consumed as an answer and must never be
            # echoed into the transcript or added to prompt history.
            self.finish_sudo_password_prompt(event.value)
            event.input.value = ""
            return
        if self._approval_request is not None:
            self.finish_approval_prompt(text)
            event.input.value = ""
            return
        if not text:
            return
        if self.detect_slash_command(text):
            event.input.value = ""
            return
        self.query_one(PromptLine).add_history(text)
        event.input.value = ""
        if self._busy:
            # Continuing is the default: input during a run is queued as a
            # clarification that interrupts the active run.
            self.queue_followup(text, clarification=True)
            self.set_status("Clarifying")
            return
        run_as_followup = bool(self._runtime is not None and self._runtime.messages)
        if run_as_followup:
            self.append_followup_query(text)
        else:
            self._show_conversation()
            transcript = self.query_one("#transcript", Transcript)
            transcript.clear()
            transcript.set_initial_query(text)
        self._active_run_id += 1
        self._active_cancel_event = threading.Event()
        self._chat_thread_id = None
        self._busy = True
        self.update_prompt_placeholder()
        self._chat_worker = self.run_chat(
            text,
            self._active_run_id,
            followup=run_as_followup,
            clarification=False,
        )

    # -- Slash commands ------------------------------------------------------
    def detect_slash_command(self, text: str) -> bool:
        """Intercept slash commands before normal message handling.

        Returns True when the input was consumed as a command. Anything
        else (including other '/'-prefixed text) falls through as an
        ordinary message.
        """
        if text == "/new":
            self._start_new_chat()
            return True
        return False

    def _start_new_chat(self) -> None:
        """Empty the transcript and drop conversation context without
        starting a run; the next message is handled as a new command."""
        if self._busy:
            self.bell()
            return
        with self._pending_followup_lock:
            self._pending_followup = None
        self._has_completed_query = False
        self._show_home_screen()
        self.query_one("#transcript", Transcript).clear()
        if self._runtime is not None:
            self._runtime.reset_conversation()
        self.update_prompt_placeholder()

    def _show_home_screen(self) -> None:
        self.query_one("#home_screen", HomeScreen).display = True
        self.query_one("#query_bar", QueryBar).hide()
        self.query_one("#body", Vertical).classes = "hidden"

    def _show_conversation(self) -> None:
        self.query_one("#home_screen", HomeScreen).display = False
        self.query_one("#body", Vertical).classes = ""

    def _update_query_header(self, text: str) -> None:
        query_bar = self.query_one("#query_bar", QueryBar)
        if text:
            query_bar.show(text)
        else:
            query_bar.hide()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self._config_active and event.option_list.id == f"{self._config_prefix}option_list":
            self._handle_config_option_selected(event)
        elif self._menu_active and event.option_list.id == "menu_options":
            self._handle_menu_selected(event.option_index)

    # -- Tab menu and provider configuration -------------------------------
    def action_open_config(self) -> None:
        if self._config_active or self._menu_active:
            return
        if self._busy or self._approval_request is not None:
            self.bell()
            return
        self._show_menu()

    def _show_menu(self) -> None:
        self._menu_active = True
        self._menu_page = "main"
        self.set_status("")
        self.query_one("#footer", Footer).set_hint(self._menu_hint)
        self.query_one("#frame", Vertical).classes = "hidden"
        self.query_one("#prompt_line", PromptLine).classes = "hidden"
        panel = self.query_one("#menu_panel", MenuPanel)
        panel.classes = ""
        panel.show_page(MAIN_MENU)

    def _hide_menu(self) -> None:
        self._menu_active = False
        self.query_one("#footer", Footer).set_hint(Footer.DEFAULT_HINT)
        self.query_one("#menu_panel", MenuPanel).classes = "hidden"
        self.query_one("#frame", Vertical).classes = ""
        self.query_one("#prompt_line", PromptLine).classes = ""
        self.update_prompt_placeholder()
        self.query_one(PromptLine).focus_input()

    def _open_provider_config(self, mode: str = "provider") -> None:
        if self._menu_active:
            self._hide_menu()
        if self._config_active:
            return
        self._show_config_panel()
        self._config_thread = threading.Thread(target=self._run_config_wizard_thread, args=(mode,), daemon=True)
        self._config_thread.start()

    def _show_config_panel(self) -> None:
        self._config_active = True
        self._cancelled = False
        self._config_result = 1
        self._config_error = None
        self._request = None
        self.set_status("")
        self.query_one("#footer", Footer).set_hint(self._config_hint)
        panel = self.query_one("#config_panel", ConfigPanel)
        self.query_one("#frame", Vertical).classes = "hidden"
        self.query_one("#prompt_line", PromptLine).classes = "hidden"
        panel.classes = ""
        panel.reset()
        panel.append_log("", STYLE_TEXT)

    def _hide_config_panel(self) -> None:
        self._config_active = False
        self._request = None
        self.query_one("#footer", Footer).set_hint(Footer.DEFAULT_HINT)
        panel = self.query_one("#config_panel", ConfigPanel)
        panel.reset()
        panel.classes = "hidden"
        self.query_one("#frame", Vertical).classes = ""
        self.query_one("#prompt_line", PromptLine).classes = ""
        self.update_prompt_placeholder()
        self.query_one(PromptLine).focus_input()

    def _set_config_hint(self, text: str) -> None:
        self.query_one("#footer", Footer).set_hint(text)

    def _run_config_wizard_thread(self, mode: str) -> None:
        config = get_config()
        ui = ConfigPromptHandle(self)
        try:
            self._config_result = run_config(config, self._binary, ui, mode=mode)
        except _ConfigCancelled:
            self._config_result = 1
        except Exception as exc:
            self._config_error = str(exc)
            try:
                self.call_from_thread(self._append_config_log, f"Error: {exc}", STYLE_ERROR)
            except Exception:
                pass
            self._config_result = 1
        finally:
            self.call_from_thread(self._finish_config)

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._config_active:
            self._handle_config_input_changed(event)
        elif self._menu_active and event.input.id == "menu_input" and self._menu_page == "models":
            panel = self.query_one("#menu_panel", MenuPanel)
            panel.set_model_results([label for label in self._menu_labels if event.value.casefold() in label.casefold()])

    def _append_config_log(self, text: str, style: str = STYLE_TEXT) -> None:
        self.query_one("#config_panel", ConfigPanel).append_log(text, style)

    def _append_log(self, text: str, style: str = STYLE_TEXT) -> None:
        self._append_config_log(text, style)

    def _finish_config(self) -> None:
        result = self._config_result
        self._hide_config_panel()
        if result == 0:
            self._reload_config_settings()
            self.append_line("Configuration updated.", STYLE_SUCCESS)
        elif self._config_error:
            self.append_line(f"Configuration error: {self._config_error}", STYLE_ERROR)
        elif self._cancelled:
            self.append_line("Configuration cancelled.", STYLE_DIM)

    def action_back(self) -> None:
        if not self._config_active:
            return
        self._config_back()

    def _cancel_config(self) -> None:
        if not self._config_active or self._cancelled:
            return
        self._cancelled = True
        req = self._request
        if req is not None:
            self._request = None
            req.event.set()
        self._config_result = 1

    def _show_model_menu(self) -> None:
        labels, choices = get_configured_model_choices(self._config, self._binary)
        if not labels:
            self.append_line("No models are available. Set up a provider or add a model first.", STYLE_WARNING)
            self._hide_menu()
            return
        self._menu_page = "models"
        self._menu_labels = labels
        self._menu_choices = choices
        self.query_one("#menu_panel", MenuPanel).show_model_search(labels, MODEL_MENU)

    def _show_settings_menu(self) -> None:
        config = get_config()
        self._menu_page = "settings"
        self.query_one("#menu_panel", MenuPanel).show_page(SETTINGS_MENU, config)

    def _handle_menu_selected(self, index: int) -> None:
        if self._menu_page == "main":
            option = MAIN_MENU.option_at(index)
            if option is None:
                return
            if option.key == "select_model":
                self._show_model_menu()
            elif option.key == "set_up_provider":
                self._open_provider_config()
            elif option.key == "settings":
                self._show_settings_menu()
            else:
                self._hide_menu()
            return
        if self._menu_page == "models":
            panel = self.query_one("#menu_panel", MenuPanel)
            shown = panel.query_one("#menu_options", OptionList).get_option_at_index(index).prompt
            label = str(shown)
            provider, model = self._menu_choices[label]
            config = get_config()
            config.choose_model(model, provider)
            self._hide_menu()
            self._reload_config_settings()
            self.append_line(f"Selected {model} ({provider}).", STYLE_SUCCESS)
            return
        if self._menu_page == "settings":
            config = get_config()
            option = SETTINGS_MENU.option_at(index)
            if option is None:
                return
            if option.key == "thinking_traces":
                config.set(Config.STREAM_THINKING_TRACES, not config.stream_thinking_traces)
            elif option.key == "unrestricted_mode":
                config.set(Config.UNRESTRICTED_MODE, not config.unrestricted_mode)
            elif option.key == "proactive_auth":
                config.set(Config.PROACTIVE_AUTH, not config.proactive_auth)
            elif option.key == "max_iterations":
                self._menu_page = "max_iterations"
                panel = self.query_one("#menu_panel", MenuPanel)
                panel.show_options(SETTINGS_MENU.title, [])
                input_widget = panel.query_one("#menu_input", Input)
                input_widget.classes = ""
                input_widget.value = str(config.max_iteration_limit)
                input_widget.placeholder = "Positive integer"
                input_widget.focus()
                return
            elif option.key == "dark_mode":
                config.set(Config.DARK_MODE, not config.dark_mode)
                self._apply_dark_mode(config.dark_mode)
            else:
                self._menu_page = "main"
                self.query_one("#menu_panel", MenuPanel).show_page(MAIN_MENU)
                return
            self._show_settings_menu()

    def _handle_menu_input_submitted(self, event: Input.Submitted) -> None:
        if self._menu_page == "models":
            panel = self.query_one("#menu_panel", MenuPanel)
            options = panel.query_one("#menu_options", OptionList)
            if options.option_count:
                self._handle_menu_selected(options.highlighted or 0)
            else:
                self.bell()
            return
        if self._menu_page == "max_iterations":
            try:
                value = int(event.value.strip())
                if value < 1:
                    raise ValueError
            except ValueError:
                self.bell()
                return
            get_config().set(Config.MAX_ITERATION_LIMIT, value)
            self._show_settings_menu()

    def _handle_menu_key(self, event) -> None:
        if self._menu_page == "models":
            panel = self.query_one("#menu_panel", MenuPanel)
            input_widget = panel.query_one("#menu_input", Input)
            option_list = panel.query_one("#menu_options", OptionList)
            if (
                event.key == "down"
                and self.focused is input_widget
                and option_list.option_count
            ):
                option_list.highlighted = 0
                option_list.focus()
                event.stop()
                event.prevent_default()
                return
            if (
                event.key == "up"
                and self.focused is option_list
                and option_list.highlighted == 0
            ):
                input_widget.focus()
                event.stop()
                event.prevent_default()
                return
        if event.key == "escape":
            if self._menu_page == "main":
                self._hide_menu()
            else:
                self._menu_page = "main"
                self.query_one("#menu_panel", MenuPanel).show_page(MAIN_MENU)
            event.stop()
            event.prevent_default()

    def append_followup_query(self, text: str) -> None:
        self.query_one("#transcript", Transcript).write_query(text)

    def queue_followup(self, text: str, *, clarification: bool) -> None:
        with self._pending_followup_lock:
            self._pending_followup = (text, clarification)
        if self._runtime is not None:
            self._runtime.interrupt()

    def pop_pending_followup(self) -> tuple[str, bool] | None:
        with self._pending_followup_lock:
            pending = self._pending_followup
            self._pending_followup = None
        return pending

    def append_line(self, text: str, style: str = STYLE_TEXT, end: str = "\n") -> None:
        transcript = self.query_one("#transcript", Transcript)
        clean = _ANSI_RE.sub("", text)
        if end == "" and clean:
            transcript.write(Text(clean, style=style))
            return
        for line in clean.splitlines() or [""]:
            transcript.write(Text(line, style=style))

    def append_stream(self, renderable: RenderableType, replace_last: bool, commit: bool) -> None:
        """Low-level hook used by TUIAgentEventsHandler for live \\r/\\n-aware output."""
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
        transcript.write(Syntax(code or "", language, theme="monokai", line_numbers=True, background_color=CODE_BG))

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
        if text and self._busy and self._runtime is not None and self._runtime.should_interrupt():
            with self._pending_followup_lock:
                is_clarification = (
                    self._pending_followup is not None and self._pending_followup[1]
                )
            text = "Clarifying" if is_clarification else "Interrupting. Press Esc again to force."
        self.query_one("#status", Spinner).set_message(text)

    def start_python_approval_prompt(self, request: ApprovalRequest) -> None:
        if not self.is_run_active(request.run_id):
            request.answer = False
            request.event.set()
            return
        self._approval_request = request
        self.set_status("")
        self.append_line("The script above requires approval to run.", STYLE_WARNING)
        self.append_line("Execute this Python code? [Y/N]", STYLE_WARNING)
        self.query_one(PromptLine).start_approval()

    # -- Privileged command approval ---------------------------------------
    def start_approval_prompt(self, request: ApprovalRequest) -> None:
        if not self.is_run_active(request.run_id):
            request.answer = False
            request.event.set()
            return
        self._approval_request = request
        self.set_status("")
        self.append_line(f"Allow sudo access for {request.binary}? [Y/N]", STYLE_WARNING)
        self.query_one(PromptLine).start_approval()

    # -- Sudo session authentication ---------------------------------------
    def start_sudo_password_prompt(self, request) -> None:
        if not self.is_run_active(request.run_id):
            request.password = None
            request.event.set()
            return
        self._auth_request = request
        self.set_status("")
        self.append_line("Privileged commands require sudo authentication.", STYLE_WARNING)
        self.append_line("Enter your sudo password once for this session:", STYLE_WARNING)
        self.query_one(PromptLine).start_password()

    def finish_sudo_password_prompt(self, text: str) -> None:
        request = self._auth_request
        if request is None:
            return
        request.password = text or None
        self._auth_request = None
        self.query_one(PromptLine).finish_approval(self._busy, self._has_completed_query)
        request.event.set()

    # -- File write approval -------------------------------------------------
    def start_write_approval_prompt(self, request: ApprovalRequest) -> None:
        if not self.is_run_active(request.run_id):
            request.answer = False
            request.event.set()
            return
        self._approval_request = request
        self.set_status("")
        self.append_line("The file above requires approval to write.", STYLE_WARNING)
        self.append_line("Write this file? [Y/N]", STYLE_WARNING)
        self.query_one(PromptLine).start_approval()

    def finish_approval_prompt(self, text: str) -> None:
        request = self._approval_request
        if request is None:
            return
        request.answer = text.strip().lower() in {"y", "yes"}
        self._approval_request = None
        self.query_one(PromptLine).finish_approval(self._busy, self._has_completed_query)
        request.event.set()

    # -- Busy state ----------------------------------------------------------
    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.update_prompt_placeholder()
        self.query_one(PromptLine).set_busy(busy)

    def update_prompt_placeholder(self) -> None:
        self.query_one(PromptLine).update_placeholder(self._busy, self._has_completed_query)

    def action_previous_history(self) -> None:
        if self._config_active:
            return
        self.query_one(PromptLine).previous_history()

    def action_next_history(self) -> None:
        if self._config_active:
            return
        self.query_one(PromptLine).next_history()

    def is_run_active(self, run_id: int) -> bool:
        return self._busy and self._active_run_id == run_id

    def _cancel_active_run(self, *, force: bool = True) -> None:
        """Request cancellation without aborting the worker cleanup path.

        The chat worker owns the call to ``Runtime.run``. Cancelling the
        Textual worker directly can prevent ``Runtime.run`` from returning
        and therefore skip the agent-history handoff into ``Runtime``.
        Runtime cancellation signals are sufficient to make the agent unwind;
        keep the worker alive until ``run_chat`` reaches its ``finally`` block.
        """
        if self._runtime is not None:
            self._runtime.interrupt()
        if force:
            if self._active_cancel_event is not None:
                self._active_cancel_event.set()

    def action_interrupt(self) -> None:
        if self._menu_active:
            self._hide_menu()
            return
        if self._config_active:
            if self._request is not None:
                self.action_back()
            else:
                self._cancel_config()
            return
        if self._busy:
            # First Escape requests a cooperative interrupt so completed tool
            # output can be recorded. A second Escape requests hard
            # cancellation through Runtime; the worker remains alive long
            # enough for Runtime.run() to return and save agent history.
            force = self._runtime is not None and self._runtime.should_interrupt()
            if force and self._runtime is not None:
                self._runtime.hard_cancel()
            self._cancel_active_run(force=force)
            if self._approval_request is not None:
                self._approval_request.answer = False
                self._approval_request.event.set()
                self._approval_request = None
            if self._auth_request is not None:
                self._auth_request.password = None
                self._auth_request.event.set()
                self._auth_request = None
            self.set_status("Interrupting. Press Esc again to force.")
        else:
            self._cancel_active_run()
            if self._chat_thread_id is not None:
                timer = threading.Timer(0.5, lambda: os._exit(0))
                timer.daemon = True
                timer.start()
            self.exit(0)

    @work(exclusive=True, thread=True)
    def run_chat(
        self,
        message: str,
        run_id: int,
        *,
        followup: bool = False,
        clarification: bool = False,
    ) -> None:
        self._chat_thread_id = threading.get_ident()
        cancel_event = self._active_cancel_event or threading.Event()
        transcript = None
        log_path = None
        run_logging = None
        try:
            transcript = TranscriptWriter()
            log_path = transcript.path
            run_logging = start_run_logging(log_path) if log_path is not None else None
            transcript.write(f"prompt: {message}")
            ui = TUIAgentEventsHandler(self, run_id, cancel_event, transcript=transcript)

            if self._runtime_error:
                transcript.write(f"error: {self._runtime_error}")
                result = ChatResult(False, self._runtime_error, str(log_path) if log_path else None)
            else:
                runtime = self._get_runtime()
                if runtime is None:
                    error = "Error: runtime is not available"
                    transcript.write(f"error: {error}")
                    result = ChatResult(False, error, str(log_path) if log_path else None)
                else:
                    runtime.bind_ui(ui)
                    current_message = message
                    current_followup = followup
                    current_clarification = clarification
                    while True:
                        if current_followup:
                            # Each followup is a fresh run: clear the UI
                            # cancellation signal so a cancelled or
                            # interrupted run cannot silence its followup.
                            # This mirrors Runtime.run_followup clearing
                            # the runtime interrupt flags.
                            if self._active_cancel_event is not None:
                                self._active_cancel_event.clear()
                            response = runtime.run_followup(
                                current_message,
                                clarification=current_clarification,
                            )
                        else:
                            response = runtime.run(current_message)
                        pending = self.pop_pending_followup() if self.is_run_active(run_id) else None
                        if pending is None:
                            break
                        current_message, current_clarification = pending
                        self.call_from_thread(self.append_followup_query, current_message)
                        current_followup = True
                    response_text = strip_emojis(str(response.get("LLM_response", "")))
                    transcript.write(f"\nresponse: {response_text}")
                    result = ChatResult(
                        bool(response.get("ok")),
                        response_text,
                        str(log_path) if log_path else None,
                    )
            if not self.is_run_active(run_id):
                return
            self.call_from_thread(self.append_line, "")
            if result.ok:
                self.call_from_thread(self.append_markdown, result.text, True)
                self._has_completed_query = True
            else:
                self.call_from_thread(
                    self.append_line,
                    result.text or "Request did not complete.",
                    STYLE_WARNING,
                )
            if result.log_path:
                self.call_from_thread(self.append_line, f"(transcript: {result.log_path})", STYLE_DIM)
        except Exception as exc:
            if self.is_run_active(run_id):
                self.call_from_thread(self.append_line, f"Error: {exc}", STYLE_ERROR)
        finally:
            if run_logging is not None:
                run_logging.close()
            if transcript is not None:
                transcript.close()
            if self._active_run_id == run_id:
                self._chat_thread_id = None
                self._chat_worker = None
            if self._busy and self._active_run_id == run_id:
                self._busy = False
                self.call_from_thread(self.set_status, "")
                self.call_from_thread(self.set_busy, False)
