"""Labels used by the Tab configuration menu."""

from textual.widgets import Static


class MainMenu(Static):
    """A named menu heading, kept separate from the config flow controller."""

    def __init__(self, **kwargs):
        super().__init__("Configuration", **kwargs)
