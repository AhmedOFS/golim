"""Textual configuration wizard for ``openterm -init``.

This mirrors the original interactive flow in ``openterm.config.basic``, but
presents each step as a single-prompt "page" in a simple linear (with
branches) sequence: one page shows one list or one text input, Enter
advances to the next page, and every page — including error/dead-end pages
— offers a Back option (the Esc key, or the "← Back" entry appended to every
list) that returns to the previous page with your prior answers preserved.

Visually the wizard stays deliberately plain: choice pages render as an
unboxed list (just a dim highlight on the current row), while the
occasional free-text step (API keys, URLs, model names) is set apart as a
small bordered input form. There's no color-coded status dashboard — just
plain text, a dim tone for secondary/trail lines, and red reserved for
actual errors.

The branching/stateful logic (provider selection, Ollama server start/retry
loop, model listing, optional small-model selection, unrestricted-bash,
proactive-auth, and thinking-trace toggles) is expressed as a small state machine (see
``STATE_HANDLERS`` / ``run_config``) that runs in a background worker thread
and talks to the UI through event-based prompt requests — the same
handshake pattern already used for privileged-binary approval prompts in
``openterm/ui/tui.py``.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Callable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, OptionList, RichLog, Static

from openterm.config import Config, get_config
from openterm.config.utils import (
    is_ollama_installed,
    get_models,
    get_openai_compatible_models,
    get_openrouter_models,
    validate_openrouter_key,
    normalize_openai_compatible_url,
    _ollama_server_running,
)
from openterm.ui.tui.config.mixin import (
    BACK_LABEL,
    ConfigUIMixin,
    InputRequest,
    MessageRequest,
    ModelSearchRequest,
    SelectRequest,
)
from openterm.ui.tui.tui_style import (
    BG_DARK,
    BORDER,
    DIM,
    ERROR,
    STYLE_ACCENT,
    STYLE_DIM,
    STYLE_ERROR,
    STYLE_SUCCESS,
    STYLE_TEXT,
    STYLE_WARNING,
    SUCCESS,
    WHITE,
)


class ConfigPromptHandle:
    """Thread-safe handle the worker uses to drive the UI.

    Every method blocks the calling (worker) thread until the UI has
    collected the user's response, mirroring the blocking semantics of the
    original ``input()``-based flow. ``select`` and ``input`` raise
    ``_GoBack`` when the user asks to return to the previous page (via the
    "← Back" list entry or the Esc key), which the wizard loop in
    ``run_config`` catches to rewind to the prior state."""

    def __init__(self, app):
        self.app = app

    def _check_alive(self) -> None:
        if self.app._cancelled:
            raise _ConfigCancelled()

    def select(self, title: str, options: list[str], default_index: int = 0, hint: str = "") -> int:
        self._check_alive()
        default_index = max(0, min(default_index, len(options) - 1)) if options else 0
        display_options = list(options) + [BACK_LABEL]
        req = SelectRequest(
            title,
            display_options,
            default_index,
            hint=hint or "↑/↓ move • Enter select • Esc back",
        )
        self.app.call_from_thread(self.app._show_select, req)
        req.event.wait()
        self._check_alive()
        if req.went_back:
            raise _GoBack()
        return int(req.answer) if req.answer is not None else 0

    def input(self, title: str, default: str = "", placeholder: str = "", hint: str = "") -> str:
        self._check_alive()
        req = InputRequest(
            title, default, placeholder, hint=hint or "Enter to continue • Esc back"
        )
        self.app.call_from_thread(self.app._show_input, req)
        req.event.wait()
        self._check_alive()
        if req.went_back:
            raise _GoBack()
        return req.answer if req.answer is not None else ""

    def search_models(self, title: str, models: list[str], default_model: str | None = None) -> str:
        self._check_alive()
        default_index = models.index(default_model) if default_model in models else 0
        req = ModelSearchRequest(title, list(models), default_index)
        self.app.call_from_thread(self.app._show_model_search, req)
        req.event.wait()
        self._check_alive()
        if req.went_back:
            raise _GoBack()
        return req.answer or ""

    def confirm(self, title: str, default_yes: bool = False, hint: str = "") -> bool:
        idx = self.select(title, ["No", "Yes"], 1 if default_yes else 0, hint=hint or "↑/↓ move • Enter select • Esc back")
        return idx == 1

    def message(self, title: str, hint: str = "Enter to continue") -> None:
        self._check_alive()
        req = MessageRequest(title, hint=hint)
        self.app.call_from_thread(self.app._show_message, req)
        req.event.wait()
        self._check_alive()
        if req.went_back:
            raise _GoBack()

    def log(self, text: str, style: str = STYLE_TEXT) -> None:
        self._check_alive()
        self.app.call_from_thread(self.app._append_log, text, style)


class _ConfigCancelled(Exception):
    """Raised in the worker when the user fully quits the wizard (Ctrl+C)."""


class _GoBack(Exception):
    """Raised in the worker when the user asks to return to the previous page."""


# ---------------------------------------------------------------------------
# Driver: each state below renders exactly one page (one select or one
# input) and returns the name of the next state. ``run_config`` drives the
# sequence and, whenever a state raises ``_GoBack``, rewinds to whichever
# state was actually shown immediately before it — so Back always lands on
# the page you actually came from, branches and all.
# ---------------------------------------------------------------------------


def _try_start_server(ui: ConfigPromptHandle, binary: str) -> bool:
    ui.log("Starting Ollama server...", STYLE_WARNING)
    try:
        subprocess.run(
            ["systemctl", "start", "ollama.service"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        ui.log(f"  systemctl said: {e.stderr.strip()}", STYLE_ERROR)
        return False
    except FileNotFoundError:
        ui.log("  systemctl not found — cannot manage the ollama service.", STYLE_ERROR)
        return False

    for _ in range(15):
        time.sleep(1)
        if _ollama_server_running(binary):
            break
    else:
        return False

    ui.log("✓ Ollama server started", STYLE_SUCCESS)
    return True


def _state_provider(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    options = [
        "Ollama (local, default)",
        "OpenAI-compatible (URL and optional API key)",
        "OpenRouter (cloud, requires API key)",
    ]
    provider_defaults = {
        Config.OLLAMA: 0,
        Config.OPENAI_COMPATIBLE: 1,
        Config.OPEN_ROUTER: 2,
    }
    provider = config.api_provider
    if provider is None:
        default = 0
    elif provider in provider_defaults:
        default = provider_defaults[provider]
    else:
        ui.log(f"Error: Unsupported API provider: {provider!r}", STYLE_ERROR)
        return "EXIT"
    ui.log("API provider setup", STYLE_ACCENT)
    idx = ui.select("Select API provider", options, default)
    if idx == 2:
        return "OPENROUTER_KEY_CHOICE" if config.openrouter_api_key else "OPENROUTER_KEY_INPUT"
    if idx == 1:
        return "OPENAI_COMPATIBLE_URL"
    if idx == 0:
        return "OLLAMA_INSTALL_CHECK"
    ui.log(f"Error: Unsupported provider selection: {idx!r}", STYLE_ERROR)
    return "EXIT"


# -- OpenRouter branch --------------------------------------------------


def _state_openrouter_key_choice(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    api_key = config.openrouter_api_key
    masked = api_key[:8] + "..." if len(api_key) > 8 else "***"
    ui.log(f"✓ OpenRouter API key: {masked}", STYLE_SUCCESS)
    idx = ui.select("Change API key?", ["Keep existing key", "Enter a new key"], 0)
    if idx == 1:
        return "OPENROUTER_KEY_INPUT"
    return "OPENROUTER_CONNECT"


def _state_openrouter_key_input(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    while True:
        api_key = ui.input("Enter your OpenRouter API key", placeholder="sk-or-...")
        if not api_key:
            ui.log("Error: API key is required", STYLE_ERROR)
            continue
        config.set_provider_value(Config.OPEN_ROUTER, Config.PROVIDER_API_KEY, api_key)
        ui.log("✓ API key saved", STYLE_SUCCESS)
        return "OPENROUTER_CONNECT"


def _state_openrouter_connect(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    if validate_openrouter_key(config.openrouter_api_key):
        return "OPENROUTER_MODEL"
    ui.log("Could not validate the OpenRouter API key. Check your key and connection.", STYLE_ERROR)
    return "OPENROUTER_KEY_INPUT"


def _state_openrouter_model(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    saved_model = config.selected_model
    models = get_openrouter_models(config.openrouter_api_key)
    if not models:
        ui.log("Could not fetch OpenRouter models. Check your API key and connection.", STYLE_ERROR)
        ui.message("Unable to load OpenRouter models. Press Enter to retry.")
        return "OPENROUTER_MODEL"
    ui.log("OpenRouter models:", STYLE_ACCENT)
    model = ui.search_models("Select model", models, saved_model)
    config.choose_model(model, Config.OPEN_ROUTER)
    ui.log(f"✓ Model: {model}", STYLE_SUCCESS)
    ui.log("✓ OpenRouter configured", STYLE_SUCCESS)
    return _provider_complete_state(ui)


# -- Ollama branch --------------------------------------------------------


def _state_ollama_install_check(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    if not is_ollama_installed(binary):
        ui.log(f"Error: {binary} is not installed", STYLE_ERROR)
        ui.log("Please install Ollama from https://ollama.ai", STYLE_ERROR)
        ui.select("Ollama not found", ["Exit"], 0)
        return "EXIT"

    ui.log(f"✓ {binary} is installed", STYLE_SUCCESS)

    ollama_host = os.environ.get("OLLAMA_HOST", "")
    if ollama_host:
        host = ollama_host.rstrip("/")
        if not host.startswith("http://") and not host.startswith("https://"):
            host = f"http://{host}"
        config.set(Config.OLLAMA_SERVER_URL, host)
        ui.log(f"✓ Using OLLAMA_HOST: {host}", STYLE_SUCCESS)
    else:
        config.set(Config.OLLAMA_SERVER_URL, "http://localhost:11434")

    return "OLLAMA_CONNECT"


def _state_ollama_connect(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    if _ollama_server_running(binary):
        return "OLLAMA_MODEL"
    if _try_start_server(ui, binary):
        return "OLLAMA_MODEL"
    ui.log("Could not connect to the Ollama server.", STYLE_WARNING)
    return "OLLAMA_URL_INPUT"


def _state_ollama_url_input(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    current = getattr(config, "ollama_server_url", "") or ""
    custom_url = ui.input("Ollama URL", default=current, placeholder="http://localhost:11434")
    if not custom_url:
        custom_url = "http://localhost:11434"
    if not custom_url.startswith("http://") and not custom_url.startswith("https://"):
        custom_url = f"http://{custom_url}"
    config.set(Config.OLLAMA_SERVER_URL, custom_url.rstrip("/"))
    # Loop back through the connect check with the new URL.
    return "OLLAMA_CONNECT"


def _state_ollama_model(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    models = get_models(binary)
    if not models:
        ui.log("No models found. Please install a model first:", STYLE_ERROR)
        ui.log(f"  {binary} pull llama2", STYLE_ERROR)
        ui.select("No models available", ["Exit"], 0)
        return "EXIT"

    saved = config.selected_model
    default = models.index(saved) if saved and saved in models else 0
    ui.log("Available models:", STYLE_ACCENT)
    idx = ui.select("Select model", models, default)
    selected = models[idx] if 0 <= idx < len(models) else None
    if not selected:
        ui.log("No model selected", STYLE_ERROR)
        return "OLLAMA_MODEL"

    config.choose_model(selected, Config.OLLAMA)
    ui.log(f"✓ Selected model: {selected}", STYLE_SUCCESS)
    return "COMMON_BASH"


# -- OpenAI-compatible branch ------------------------------------------------------


def _state_openai_compatible_url(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    current = normalize_openai_compatible_url(config.openai_compatible_server_url or "")
    url = ui.input(
        "OpenAI-compatible server URL (required)",
        default=current,
        placeholder="http://your-server:port",
    )
    if not url:
        ui.log("Error: server URL is required", STYLE_ERROR)
        return "OPENAI_COMPATIBLE_URL"
    config.set(Config.OPENAI_COMPATIBLE_SERVER_URL, normalize_openai_compatible_url(url))
    api_key = ui.input(
        "OpenAI-compatible API key (optional)",
        default=config.openai_compatible_api_key or "",
        placeholder="Leave blank when not required",
    )
    config.set_provider_value(Config.OPENAI_COMPATIBLE, Config.PROVIDER_API_KEY, api_key)
    return "OPENAI_COMPATIBLE_CONNECT"


def _state_openai_compatible_connect(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    if get_openai_compatible_models(config.openai_compatible_server_url, config.openai_compatible_api_key):
        return "OPENAI_COMPATIBLE_MODEL"
    ui.log("Could not connect to the OpenAI-compatible server or list its models.", STYLE_WARNING)
    return "OPENAI_COMPATIBLE_URL"


def _state_openai_compatible_model(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    saved_model = config.selected_model
    model = ui.input("Enter model name", default=saved_model or "", placeholder=saved_model or "default")
    if not model:
        model = saved_model or "default"
    config.choose_model(model, Config.OPENAI_COMPATIBLE)
    ui.log(f"✓ Model: {model}", STYLE_SUCCESS)
    ui.log("✓ OpenAI-compatible provider configured", STYLE_SUCCESS)
    return _provider_complete_state(ui)


def _provider_complete_state(ui: ConfigPromptHandle) -> str:
    """Provider setup does not ask global settings when opened from Tab."""
    return "DONE" if getattr(ui, "config_mode", "initial") == "provider" else "COMMON_BASH"


# -- Common tail ------------------------------------------------------------


def _state_common_bash(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    current_unrestricted = config.unrestricted_mode
    enable = ui.confirm(
        f"Enable unrestricted mode?{' (currently enabled)' if current_unrestricted else ''}",
        default_yes=current_unrestricted,
    )
    if enable and not current_unrestricted:
        config.set(Config.UNRESTRICTED_MODE, True)
        ui.log("✓ Unrestricted mode enabled", STYLE_SUCCESS)
    elif not enable and current_unrestricted:
        config.set(Config.UNRESTRICTED_MODE, False)
        ui.log("✓ Unrestricted mode disabled", STYLE_SUCCESS)
    return "COMMON_THINKING"


def _state_common_thinking(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    current_thinking = config.stream_thinking_traces
    enable = ui.confirm(
        f"Stream model thinking traces in the UI?{' (currently enabled)' if current_thinking else ''}",
        default_yes=current_thinking,
    )
    if enable and not current_thinking:
        config.set(Config.STREAM_THINKING_TRACES, True)
        ui.log("✓ Thinking trace streaming enabled", STYLE_SUCCESS)
    elif not enable and current_thinking:
        config.set(Config.STREAM_THINKING_TRACES, False)
        ui.log("✓ Thinking trace streaming disabled", STYLE_SUCCESS)
    return "COMMON_PROACTIVE_AUTH"


def _state_common_proactive_auth(config: Config, ui: ConfigPromptHandle, binary: str) -> str:
    current_proactive_auth = config.proactive_auth
    enable = ui.confirm(
        f"Proactively authenticate sudo at startup?"
        f"{' (currently enabled)' if current_proactive_auth else ''}",
        default_yes=current_proactive_auth,
    )
    if enable and not current_proactive_auth:
        config.set(Config.PROACTIVE_AUTH, True)
        ui.log("✓ Proactive sudo authentication enabled", STYLE_SUCCESS)
    elif not enable and current_proactive_auth:
        config.set(Config.PROACTIVE_AUTH, False)
        ui.log("✓ Proactive sudo authentication disabled", STYLE_SUCCESS)
    return "DONE"


STATE_HANDLERS: dict[str, Callable[[Config, ConfigPromptHandle, str], str]] = {
    "PROVIDER": _state_provider,
    "OPENROUTER_KEY_CHOICE": _state_openrouter_key_choice,
    "OPENROUTER_KEY_INPUT": _state_openrouter_key_input,
    "OPENROUTER_CONNECT": _state_openrouter_connect,
    "OPENROUTER_MODEL": _state_openrouter_model,
    "OLLAMA_INSTALL_CHECK": _state_ollama_install_check,
    "OLLAMA_CONNECT": _state_ollama_connect,
    "OLLAMA_URL_INPUT": _state_ollama_url_input,
    "OLLAMA_MODEL": _state_ollama_model,
    "OPENAI_COMPATIBLE_URL": _state_openai_compatible_url,
    "OPENAI_COMPATIBLE_CONNECT": _state_openai_compatible_connect,
    "OPENAI_COMPATIBLE_MODEL": _state_openai_compatible_model,
    "COMMON_BASH": _state_common_bash,
    "COMMON_THINKING": _state_common_thinking,
    "COMMON_PROACTIVE_AUTH": _state_common_proactive_auth,
}

_NON_INTERACTIVE_STATES = frozenset({
    "OLLAMA_CONNECT",
    "OLLAMA_INSTALL_CHECK",
    "OPENAI_COMPATIBLE_CONNECT",
    "OPENROUTER_CONNECT",
})


def run_config(config: Config, binary: str, ui: ConfigPromptHandle, mode: str = "initial") -> int:
    """Drive the wizard as a sequence of single-prompt pages.

    Every page can raise ``_GoBack`` (via the shared prompt handle), in
    which case we rewind to whichever page was actually shown right before
    it. Going back from the very first page cancels the wizard, matching
    what happens when the user cancels the very first prompt today."""
    history: list[str] = []
    ui.config_mode = mode
    state = "PROVIDER"

    while state not in ("DONE", "EXIT"):
        handler = STATE_HANDLERS.get(state)
        if handler is None:
            return 1
        try:
            next_state = handler(config, ui, binary)
        except _GoBack:
            if history:
                state = history.pop()
                continue
            return 1
        if state not in _NON_INTERACTIVE_STATES:
            history.append(state)
        state = next_state

    if state == "EXIT":
        return 1

    ui.log("", STYLE_TEXT)
    ui.log("You can now use openterm:", STYLE_TEXT)
    ui.log('  openterm "Hello, how are you?"', STYLE_TEXT)
    return 0


# ---------------------------------------------------------------------------
# Textual app
# ---------------------------------------------------------------------------


class ConfigApp(ConfigUIMixin, App[int]):
    """Single-prompt-per-page configuration wizard with Back navigation."""

    _config_prefix = ""

    CSS = f"""
    Screen {{
        background: {BG_DARK};
        color: {WHITE};
    }}

    Screen.-pitch-black {{
        background: #000000;
    }}

    #outer {{
        height: 100%;
        width: 100%;
        padding: 2 4;
    }}

    #title {{
        height: 1;
        color: {WHITE};
        text-style: bold;
        margin-bottom: 1;
    }}

    #transcript {{
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

    #prompt_label {{
        height: auto;
        min-height: 1;
        color: {WHITE};
        text-style: bold;
        margin-bottom: 1;
    }}

    #option_list {{
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

    #option_list > .option-list--option-highlighted {{
        background: {WHITE} 12%;
        text-style: bold;
    }}

    #option_list.model_search {{
        max-height: 6;
    }}

    #text_input {{
        height: 3;
        width: 56;
        background: transparent;
        border: round {BORDER};
        padding: 0 1;
        margin-top: 0;
    }}

    #text_input:focus {{
        border: round {WHITE};
    }}

    #hint {{
        height: 1;
        color: {DIM};
        margin-top: 1;
    }}

    .hidden {{
        display: none;
    }}
    """

    BINDINGS = [
        ("ctrl+c", "cancel", "Quit"),
        ("escape", "back", "Back"),
    ]

    def __init__(self, binary: str = "ollama"):
        super().__init__()
        self.binary = binary
        self._request: SelectRequest | InputRequest | ModelSearchRequest | MessageRequest | None = None
        self._model_search_results: list[str] = []
        self._cancelled = False
        self._result = 1

    def compose(self) -> ComposeResult:
        with Vertical(id="outer"):
            yield Static("openterm configuration", id="title")
            yield RichLog(id="transcript", markup=False, auto_scroll=True, wrap=True)
            yield Static("", id="prompt_label")
            yield Input(id="text_input", classes="hidden")
            yield OptionList(id="option_list", classes="hidden")
            yield Static("↑/↓ move • Enter select • Esc back", id="hint")

    def on_mount(self) -> None:
        config = get_config()
        self.dark = config.dark_mode
        self.screen.set_class(config.dark_mode, "-pitch-black")
        # self._append_log("openterm configuration", STYLE_DIM)
        self._append_log("", STYLE_TEXT)
        self.run_wizard()

    @work(exclusive=True, thread=True)
    def run_wizard(self) -> None:
        config = get_config()
        ui = ConfigPromptHandle(self)
        try:
            self._result = run_config(config, self.binary, ui)
        except _ConfigCancelled:
            self._result = 1
        except Exception as exc:
            try:
                self.call_from_thread(self._append_log, f"Error: {exc}", STYLE_ERROR)
            except Exception:
                pass
            self._result = 1
        finally:
            self.call_from_thread(self._finish)

    # -- event handlers (delegate to mixin) ---------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        self._handle_config_input_changed(event)

    # -- widget events ------------------------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._handle_config_option_selected(event)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._handle_config_input_submitted(event)

    def on_key(self, event) -> None:
        self._handle_config_key(event)

    # -- log / teardown -----------------------------------------------------

    def _append_log(self, text: str, style: str = STYLE_TEXT) -> None:
        transcript = self.query_one("#transcript", RichLog)
        transcript.write(Text(text or "", style=style))

    def _finish(self) -> None:
        self._request = None
        self.query_one("#option_list", OptionList).classes = "hidden"
        self.query_one("#text_input", Input).classes = "hidden"
        self.query_one("#prompt_label", Static).update("")
        self.query_one("#hint", Static).update("Done.")
        self.exit(self._result)

    def action_back(self) -> None:
        """Esc: ask the currently pending prompt to rewind to the previous page."""
        self._config_back()

    def action_cancel(self) -> None:
        """Ctrl+C: quit the wizard entirely, regardless of history."""
        if self._cancelled:
            return
        self._cancelled = True
        req = self._request
        if req is not None:
            self._request = None
            req.event.set()
        self._result = 1
        self.exit(1)


def init_command_tui(binary: str = "ollama") -> int:
    """Launch the Textual configuration wizard.

    Falls back to the plain ``openterm.config.basic.init_command`` flow when
    Textual is unavailable, so ``openterm --init`` keeps working in minimal
    environments.
    """
    try:
        result = ConfigApp(binary=binary).run()
        return int(result or 0)
    except ImportError:
        from openterm.ui.basic.basic_config import init_command
        return init_command(binary)
