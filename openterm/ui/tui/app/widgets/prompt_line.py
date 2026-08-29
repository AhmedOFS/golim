from __future__ import annotations

from textual.containers import Horizontal
from textual.widgets import Input, Static

from openterm.ui.tui.app.history import History

INITIAL_PROMPT_PLACEHOLDER = "Type your Request..."
RUNNING_PROMPT_PLACEHOLDER = "Use // to add a clarification..."
DONE_PROMPT_PLACEHOLDER = "Type a new request, or use // to add a follow up..."


class PromptLine(Horizontal):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history = History()

    def compose(self):
        yield Static(">", id="prompt_marker")
        yield Input(id="prompt", placeholder=INITIAL_PROMPT_PLACEHOLDER)

    def update_placeholder(self, busy: bool, has_completed: bool) -> None:
        prompt = self.query_one("#prompt", Input)
        if busy:
            prompt.placeholder = RUNNING_PROMPT_PLACEHOLDER
        elif has_completed:
            prompt.placeholder = DONE_PROMPT_PLACEHOLDER
        else:
            prompt.placeholder = INITIAL_PROMPT_PLACEHOLDER

    def set_busy(self, busy: bool) -> None:
        prompt = self.query_one("#prompt", Input)
        if not busy:
            prompt.disabled = False
            prompt.focus()

    def get_value(self) -> str:
        return self.query_one("#prompt", Input).value

    def set_value(self, text: str) -> None:
        self.query_one("#prompt", Input).value = text

    def clear(self) -> None:
        self.query_one("#prompt", Input).value = ""

    def focus_input(self) -> None:
        self.query_one("#prompt", Input).focus()

    def previous_history(self) -> None:
        value = self._history.previous()
        self.query_one("#prompt", Input).value = value

    def next_history(self) -> None:
        value = self._history.next()
        self.query_one("#prompt", Input).value = value

    def add_history(self, text: str) -> None:
        self._history.add(text)

    def start_approval(self) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.placeholder = "y or n"
        prompt.value = ""
        prompt.focus()

    def start_password(self) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.password = True
        prompt.placeholder = "sudo password (input hidden)"
        prompt.value = ""
        prompt.focus()

    def finish_approval(self, busy: bool, has_completed: bool) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.password = False
        prompt.placeholder = RUNNING_PROMPT_PLACEHOLDER if busy else (
            DONE_PROMPT_PLACEHOLDER if has_completed else INITIAL_PROMPT_PLACEHOLDER
        )
        prompt.disabled = False
