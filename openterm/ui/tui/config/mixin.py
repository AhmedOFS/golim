"""Shared config-wizard prompt mixin for ConfigApp and OpentermApp.

Extracted from ``config_tui.py`` to avoid code duplication between the
standalone config wizard and the embedded config panel."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from openterm.ui.tui.tui_style import STYLE_DIM


BACK_LABEL = "← Back"


@dataclass
class SelectRequest:
    """A single-choice prompt rendered as an OptionList."""
    title: str
    options: list[str]
    default_index: int = 0
    hint: str = "↑/↓ to move • Enter to select"
    event: threading.Event = field(default_factory=threading.Event)
    answer: int | None = None
    went_back: bool = False


@dataclass
class InputRequest:
    """A free-text prompt rendered as an Input."""
    title: str
    default: str = ""
    placeholder: str = ""
    hint: str = "Type, then Enter"
    event: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None
    went_back: bool = False


@dataclass
class ModelSearchRequest:
    """A searchable model picker with an input kept above its result list."""
    title: str
    models: list[str]
    default_index: int = 0
    hint: str = "Type to search • ↓ results • Enter select"
    event: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None
    went_back: bool = False


@dataclass
class MessageRequest:
    """A non-interactive status line acknowledged by Enter."""
    title: str
    hint: str = "Enter to continue"
    event: threading.Event = field(default_factory=threading.Event)
    went_back: bool = False


class ConfigUIMixin:
    """Mixin that renders config-wizard prompts and handles their events.

    Subclasses must set ``_config_prefix`` (``""`` or ``"config_"``) and
    provide ``_request`` / ``_model_search_results`` attributes, a working
    ``_append_log(text, style)`` method, and the full Textual App interface
    (``query_one``, ``focused``, ``bell()``, etc.).
    """

    _config_prefix = ""

    # -- widget-ID helper ---------------------------------------------------

    def _cfg(self, name: str) -> str:
        return f"#{self._config_prefix}{name}"

    # -- prompt rendering ---------------------------------------------------

    def _show_select(self, req: SelectRequest) -> None:
        self._request = req
        self.query_one(self._cfg("prompt_label"), Static).update(req.title)
        ol = self.query_one(self._cfg("option_list"), OptionList)
        ol.clear_options()
        ol.add_options(Option(opt) for opt in req.options)
        ol.classes = ""
        self.query_one(self._cfg("text_input"), Input).classes = "hidden"
        self.query_one(self._cfg("hint"), Static).update(req.hint)
        idx = max(0, min(req.default_index, len(req.options) - 1))
        ol.highlighted = idx
        ol.focus()

    def _show_input(self, req: InputRequest) -> None:
        self._request = req
        self.query_one(self._cfg("prompt_label"), Static).update(req.title)
        ti = self.query_one(self._cfg("text_input"), Input)
        ti.classes = ""
        ti.value = req.default
        ti.placeholder = req.placeholder
        self.query_one(self._cfg("option_list"), OptionList).classes = "hidden"
        self.query_one(self._cfg("hint"), Static).update(req.hint)
        ti.focus()

    def _show_model_search(self, req: ModelSearchRequest) -> None:
        self._request = req
        self.query_one(self._cfg("prompt_label"), Static).update(req.title)
        ti = self.query_one(self._cfg("text_input"), Input)
        ti.classes = ""
        ti.value = ""
        ti.placeholder = "Search models"
        self.query_one(self._cfg("option_list"), OptionList).classes = "model_search"
        self.query_one(self._cfg("hint"), Static).update(req.hint)
        self._set_model_search_results(req.models, req.default_index)
        ti.focus()

    def _show_message(self, req: MessageRequest) -> None:
        self._request = req
        self.query_one(self._cfg("prompt_label"), Static).update(req.title)
        self.query_one(self._cfg("text_input"), Input).classes = "hidden"
        ol = self.query_one(self._cfg("option_list"), OptionList)
        ol.classes = ""
        ol.clear_options()
        ol.add_options(Option("Continue"))
        ol.highlighted = 0
        self.query_one(self._cfg("hint"), Static).update(req.hint)
        ol.focus()

    def _set_model_search_results(self, models: list[str], highlighted: int = 0) -> None:
        self._model_search_results = models
        ol = self.query_one(self._cfg("option_list"), OptionList)
        ol.clear_options()
        ol.add_options(Option(model) for model in models)
        if models:
            ol.highlighted = max(0, min(highlighted, len(models) - 1))

    # -- event handling -----------------------------------------------------

    def _handle_config_option_selected(self, event: OptionList.OptionSelected) -> bool:
        req = self._request
        if req is None:
            return False
        if isinstance(req, ModelSearchRequest):
            if 0 <= event.option_index < len(self._model_search_results):
                req.answer = self._model_search_results[event.option_index]
                self._append_log(f"{req.title}: {req.answer}", STYLE_DIM)
            else:
                return True
        elif isinstance(req, SelectRequest):
            idx = event.option_index
            if idx == len(req.options) - 1:
                req.went_back = True
                self._append_log(f"{req.title}: {BACK_LABEL}", STYLE_DIM)
            else:
                req.answer = idx
                label = req.options[idx] if 0 <= idx < len(req.options) else ""
                self._append_log(f"{req.title}: {label}", STYLE_DIM)
        elif isinstance(req, MessageRequest):
            self._append_log(req.title, STYLE_DIM)
        else:
            return False
        self._request = None
        req.event.set()
        return True

    def _handle_config_input_submitted(self, event: Input.Submitted) -> bool:
        req = self._request
        if isinstance(req, ModelSearchRequest):
            if not self._model_search_results:
                self.bell()
                return True
            req.answer = self._model_search_results[0]
            self._append_log(f"{req.title}: {req.answer}", STYLE_DIM)
            self._request = None
            event.input.value = ""
            req.event.set()
            return True
        if req is None or not isinstance(req, InputRequest):
            return False
        value = event.value.strip()
        req.answer = value
        shown = value if value else "(default)"
        self._append_log(f"{req.title}: {shown}", STYLE_DIM)
        self._request = None
        event.input.value = ""
        req.event.set()
        return True

    def _handle_config_input_changed(self, event: Input.Changed) -> bool:
        if event.input.id != f"{self._config_prefix}text_input":
            return False
        req = self._request
        if not isinstance(req, ModelSearchRequest):
            return False
        query = event.value.casefold()
        self._set_model_search_results(
            [model for model in req.models if query in model.casefold()]
        )
        return True

    def _handle_config_key(self, event) -> bool:
        req = self._request
        if not isinstance(req, ModelSearchRequest):
            return False
        input_widget = self.query_one(self._cfg("text_input"), Input)
        option_list = self.query_one(self._cfg("option_list"), OptionList)
        if event.key == "down" and self.focused is input_widget and self._model_search_results:
            option_list.highlighted = 0
            option_list.focus()
            event.prevent_default()
            event.stop()
            return True
        elif event.key == "up" and self.focused is option_list and option_list.highlighted == 0:
            input_widget.focus()
            event.prevent_default()
            event.stop()
            return True
        return False

    def _config_back(self) -> None:
        req = self._request
        if req is None:
            return
        req.went_back = True
        self._request = None
        req.event.set()
