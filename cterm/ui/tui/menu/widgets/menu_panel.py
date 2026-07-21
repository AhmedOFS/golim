"""Standalone Tab-menu view; this is intentionally not a config wizard."""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .main_menu import MainMenu
from .model_selection import ModelSelection
from .settings import SettingsMenu


class MenuPanel(Vertical):
    """Application menu with independent model-picker and settings pages."""

    def compose(self) -> ComposeResult:
        yield MainMenu(id="menu_title")
        yield Input(id="menu_input", classes="hidden")
        yield OptionList(id="menu_options")
        yield Static("↑/↓ move • Enter select • Esc close", id="menu_hint")

    def show_options(self, title: str, options: list[str], selected: int = 0) -> None:
        title_widget = self.query_one("#menu_title", Static)
        title_widget.update(title)
        options_widget = self.query_one("#menu_options", OptionList)
        options_widget.clear_options()
        options_widget.add_options(Option(option) for option in options)
        options_widget.highlighted = max(0, min(selected, len(options) - 1)) if options else 0
        self.query_one("#menu_input", Input).classes = "hidden"
        options_widget.classes = ""
        options_widget.focus()

    def show_model_search(self, labels: list[str]) -> None:
        title_widget = self.query_one("#menu_title", Static)
        title_widget.update("Select model")
        input_widget = self.query_one("#menu_input", Input)
        input_widget.classes = ""
        input_widget.value = ""
        input_widget.placeholder = "Search models"
        self.set_model_results(labels)
        input_widget.focus()

    def set_model_results(self, labels: list[str]) -> None:
        options_widget = self.query_one("#menu_options", OptionList)
        options_widget.clear_options()
        options_widget.add_options(Option(label) for label in labels)
        options_widget.highlighted = 0 if labels else None

    def title_widget(self, kind: str) -> type[Static]:
        return {"main": MainMenu, "models": ModelSelection, "settings": SettingsMenu}[kind]
