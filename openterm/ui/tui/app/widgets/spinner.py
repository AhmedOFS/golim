from __future__ import annotations

from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static

from openterm.ui.tui.tui_style import STYLE_SUCCESS

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SPINNER_INTERVAL = 0.09


class Spinner(Static):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._spinner_message = ""
        self._spinner_frame_index = 0
        self._spinner_timer: Timer | None = None

    def set_message(self, text: str) -> None:
        self._spinner_message = text
        if text:
            self.start()
        else:
            self.stop()

    def start(self) -> None:
        if self._spinner_timer is None:
            self._spinner_frame_index = 0
            self._spinner_timer = self.set_interval(_SPINNER_INTERVAL, self._advance)
        self._advance()

    def _advance(self) -> None:
        frame = _SPINNER_FRAMES[self._spinner_frame_index % len(_SPINNER_FRAMES)]
        self._spinner_frame_index += 1
        self.update(Text(f"{frame} {self._spinner_message}", style=STYLE_SUCCESS))

    def stop(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self.update("")
