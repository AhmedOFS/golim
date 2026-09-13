from __future__ import annotations

from textual import events
from textual.containers import Horizontal
from textual.widgets import Input, Static

from golim.ui.tui.app.history import History

INITIAL_PROMPT_PLACEHOLDER = "What do you want to do..."
RUNNING_PROMPT_PLACEHOLDER = "Add a clarification..."
DONE_PROMPT_PLACEHOLDER = "Followup with something, or /new to make a new Golim..."


class PromptLine(Horizontal):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history = History()

    def compose(self):
        yield Static(">", id="prompt_marker")
        yield PromptInput(id="prompt", placeholder=INITIAL_PROMPT_PLACEHOLDER)

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


class PromptInput(Input):
    """Single-line prompt input that keeps all text from bracketed pastes.

    Textual's standard ``Input`` intentionally takes only the first line from
    a paste. The prompt still submits with Enter, but pasted newlines are kept
    in the value so multi-line prompts reach the agent intact.
    """

    def _paste_text(self, text: str) -> None:
        if not text:
            return
        start, end = self.selection
        if start == end:
            self.insert_text_at_cursor(text)
        else:
            self.replace(text, start, end)

    def _on_paste(self, event: events.Paste) -> None:
        self._paste_text(event.text)
        event.stop()
        event.prevent_default()

    def action_paste(self) -> None:
        """Paste the complete clipboard contents, including newlines."""
        self._paste_text(self.app.clipboard)
