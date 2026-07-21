"""Model-selection heading for the shared searchable picker."""

from textual.widgets import Static


class ModelSelection(Static):
    def __init__(self, **kwargs):
        super().__init__("Select model", **kwargs)
