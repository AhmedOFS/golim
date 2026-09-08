from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static


class Footer(Horizontal):
    DEFAULT_HINT = "esc Interrupt • ↑/↓ History • Tab Menu"

    def __init__(self, model_label: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._model_label = model_label

    def compose(self) -> ComposeResult:
        yield Static(self._model_label, id="model")
        yield Static(self.DEFAULT_HINT, id="keys")

    def set_model(self, text: str) -> None:
        self.query_one("#model", Static).update(text)

    def set_hint(self, text: str) -> None:
        self.query_one("#keys", Static).update(text)
