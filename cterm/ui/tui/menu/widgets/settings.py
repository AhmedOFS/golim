"""Settings heading for the shared option-list menu."""

from textual.widgets import Static


class SettingsMenu(Static):
    def __init__(self, **kwargs):
        super().__init__("Settings", **kwargs)
