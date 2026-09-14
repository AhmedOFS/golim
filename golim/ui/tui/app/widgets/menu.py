from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option


@dataclass(frozen=True)
class MenuOption:
    key: str
    label: str | Callable[[object], str]

    def render(self, context: object = None) -> str:
        return self.label(context) if callable(self.label) else self.label


@dataclass(frozen=True)
class MenuPage:
    title: str
    options: tuple[MenuOption, ...] = ()

    def labels(self, context: object = None) -> list[str]:
        return [option.render(context) for option in self.options]

    def option_at(self, index: int) -> MenuOption | None:
        if 0 <= index < len(self.options):
            return self.options[index]
        return None


MAIN_MENU = MenuPage(
    "Configuration Menu",
    (
        MenuOption("select_model", "Model Selection"),
        MenuOption("set_up_provider", "Provider Setup"),
        MenuOption("settings", "Settings"),
        MenuOption("close", "Close"),
    ),
)

MODEL_MENU = MenuPage("Select a model")

SETTINGS_MENU = MenuPage(
    "Settings",
    (
        MenuOption("thinking_traces", lambda config: f"Thinking traces: {'on' if config.stream_thinking_traces else 'off'}"),
        MenuOption("unrestricted_mode", lambda config: f"Unrestricted mode: {'on' if config.unrestricted_mode else 'off'}"),
        MenuOption("proactive_auth", lambda config: f"Authenticate Sudo on App Start: {'on' if config.proactive_auth else 'off'}"),
        MenuOption("max_iterations", lambda config: f"Max iteration limit: {config.max_iteration_limit}"),
        MenuOption("dark_mode", lambda config: f"Dark mode: {'on' if config.dark_mode else 'off'}"),
        MenuOption("back", "Back"),
    ),
)


class MenuPanel(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(id="menu_title")
        yield Input(id="menu_input", classes="hidden")
        yield OptionList(id="menu_options")

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

    def show_page(self, page: MenuPage, context: object = None, selected: int = 0) -> None:
        self.show_options(page.title, page.labels(context), selected)

    def show_model_search(self, labels: list[str], page: MenuPage = MODEL_MENU) -> None:
        title_widget = self.query_one("#menu_title", Static)
        title_widget.update(page.title)
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
