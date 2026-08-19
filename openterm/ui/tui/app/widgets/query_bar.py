from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from openterm.ui.tui.tui_style import WHITE


class QueryBar(Static):
    def show(self, text: str) -> None:
        self.update(Text(f"> {text}", style=f"bold {WHITE}"))
        self.display = True

    def hide(self) -> None:
        self.update("")
        self.display = False
