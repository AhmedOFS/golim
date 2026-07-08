from textual.app import App, ComposeResult
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import Static


class TestScrollView(ScrollView):
    DEFAULT_CSS = """
    TestScrollView {
        background: transparent;
        overflow-x: hidden;
    }
    """

    def on_mount(self):
        self.virtual_size = Size(80, 100)

    def render_line(self, y: int) -> Strip:
        row = y + int(self.scroll_offset.y)

        if row == 10:
            return Strip.blank(2)

        from rich.segment import Segment
        from rich.style import Style

        return Strip([
            Segment(" " * self.size.width, Style(bgcolor="blue"))
        ])


class TestApp(App):
    CSS = """
    Screen {
        background: rgb(40, 60, 100);
    }

    #container {
        width: 100%;
        height: 100%;
        padding: 2;
        background: transparent;
    }

    #scroll {
        width: 100%;
        height: 1fr;
        background: transparent;
        border: solid white;
    }

    #footer {
        height: 1;
        background: transparent;
    }
    """

    def compose(self) -> ComposeResult:
        yield TestScrollView(id="scroll")
        yield Static("Footer", id="footer")


if __name__ == "__main__":
    TestApp().run()