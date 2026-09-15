from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from golim.ui.tui.tui_style import PROMPT_MARKER, TOOL_OUTPUT, WHITE


_LOGO = r"""       ___________________
     _/        /          \_
    /  _______/____________ \
   /__/       \            \_\
  |   |        \__         |  |
  |   |           \        |  |
  |   /________    ________\  |
  |  /                     \ |
  |  |  | ## |      | ## |  | |
  |  |  '----'  __  '----'  | |
  |  |         /  \        | |
  |  /__      |____|     __\ |
  | /   \              /   \ |
  | |    _________________ | |
  | |   /_________________\| |
  |  \     |          |   /  |
   \  \____|__________|__/  /
    \_____________________/


 ██████╗  ██████╗ ██╗     ██╗███╗   ███╗
██╔════╝ ██╔═══██╗██║     ██║████╗ ████║
██║  ███╗██║   ██║██║     ██║██╔████╔██║
██║   ██║██║   ██║██║     ██║██║╚██╔╝██║
╚██████╔╝╚██████╔╝███████╗██║██║ ╚═╝ ██║
 ╚═════╝  ╚═════╝ ╚══════╝╚═╝╚═╝     ╚═╝
"""
_FACE_LINE_COUNT = 18
_WORDMARK_START = 20


def load_logo() -> str:
    return _LOGO


def render_logo() -> Text:
    lines = load_logo().rstrip("\n").splitlines()
    if not lines:
        return Text()

    face_lines = lines[:_FACE_LINE_COUNT]
    wordmark_lines = lines[_WORDMARK_START:]
    face_width = max((len(line) for line in face_lines), default=0)
    wordmark_width = max((len(line) for line in wordmark_lines), default=face_width)
    block_width = max(face_width, wordmark_width)
    face_offset = max((wordmark_width - face_width) // 2, 0)

    rendered = Text()
    for index, line in enumerate(lines):
        if index:
            rendered.append("\n")
        if index < _FACE_LINE_COUNT:
            normalized = (" " * face_offset + line.rstrip()).ljust(block_width)
            rendered.append(normalized, style=f"bold {TOOL_OUTPUT}")
        else:
            rendered.append(line.rstrip().ljust(block_width), style=WHITE)
    return rendered


class HomeScreen(Static):
    def __init__(self, **kwargs) -> None:
        super().__init__(render_logo(), markup=False, **kwargs)
