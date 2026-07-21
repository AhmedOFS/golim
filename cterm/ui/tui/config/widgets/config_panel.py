from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, OptionList, RichLog, Static

from cterm.ui.tui.tui_style import STYLE_DIM, STYLE_TEXT, WHITE


class ConfigPanel(Vertical):
    def __init__(self, prefix: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._prefix = prefix

    def compose(self) -> ComposeResult:
        yield Static("cterm configuration", id=f"{self._prefix}title")
        yield RichLog(id=f"{self._prefix}transcript", markup=False, auto_scroll=True, wrap=True)
        yield Static("", id=f"{self._prefix}prompt_label")
        yield Input(id=f"{self._prefix}text_input", classes="hidden")
        yield OptionList(id=f"{self._prefix}option_list", classes="hidden")
        yield Static(f"↑/↓ move • Enter select • Esc back", id=f"{self._prefix}hint")

    def append_log(self, text: str, style: str = STYLE_TEXT) -> None:
        transcript = self.query_one(f"#{self._prefix}transcript", RichLog)
        transcript.write(Text(text or "", style=style))

    def show(self) -> None:
        self.display = True

    def hide(self) -> None:
        self.display = False

    def reset(self) -> None:
        self.query_one(f"#{self._prefix}option_list", OptionList).classes = "hidden"
        self.query_one(f"#{self._prefix}text_input", Input).classes = "hidden"
        self.query_one(f"#{self._prefix}prompt_label", Static).update("")
        self.query_one(f"#{self._prefix}hint", Static).update(
            "↑/↓ move • Enter select • Esc back"
        )
