from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static


class Footer(Horizontal):
    def __init__(self, model_label: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._model_label = model_label

    def compose(self) -> ComposeResult:
        yield Static(self._model_label, id="model")
        yield Static("esc Interrupt • ↑/↓ History • Tab Menu", id="keys")

    def set_model(self, text: str) -> None:
        self.query_one("#model", Static).update(text)
