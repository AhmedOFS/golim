from __future__ import annotations

import io
from collections.abc import Callable

from rich.console import Console, RenderableType
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text
from rich.theme import Theme
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip

from golim.ui.tui.tui_style import CODE_BG, STYLE_DIM, WHITE


class Transcript(ScrollView, can_focus=False):
    """A scrollable, markdown-capable log that supports in-place line updates.

    This exists instead of Textual's built-in RichLog because RichLog is
    strictly append-only: every ``write()`` call creates a brand new row.
    That makes it impossible to correctly render carriage-return-driven
    terminal output (progress bars, spinners, ``\\r``-based redraws) — each
    update just piles up as its own line instead of overwriting the current
    one. Transcript keeps its own buffer of pre-rendered lines and exposes a
    ``replace_last``/``commit`` write mode so callers can update an
    in-progress line in place and only "lock it in" once it's finished.

    It also supports generic *expandable* entries: a line (or block of
    lines) that was written via ``write_expandable`` can be clicked to swap
    between a collapsed "summary" renderable and an expanded "detail"
    renderable. Thinking traces use this same click/toggle machinery, but
    are visually distinguished with a ▶/▼ disclosure arrow; other
    expandable entries (tool results, truncated output, etc.) are clickable
    but render without that arrow and without any style/color change
    between their collapsed and expanded states.
    """

    DEFAULT_CSS = """
    Transcript {
        overflow-x: hidden;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lines: list[Strip] = []
        self._pending_strips: list[Strip] = []
        self._renderable_log: list[tuple[RenderableType, int, int | None, int, str | None]] = []
        self._pending_renderable: RenderableType | None = None
        self._pending_indent: int | None = None
        self._pending_query_text: str | None = None
        self._expandable_entries: dict[int, dict[str, object]] = {}
        self._next_expandable_id = 1
        self._live_thinking_id: int | None = None
        self._query_header_callback: Callable[[str], None] | None = None
        self._initial_query_text = ""

        theme = Theme(
            {
                "markdown.code": f"{WHITE} on {CODE_BG}",
                "markdown.code_block": f"#525252 on {CODE_BG}",
                "markdown.h1": f"bold {WHITE}",
                "markdown.h2": f"bold {WHITE}",
                "markdown.h3": f"bold {WHITE}",
                "markdown.h4": f"bold {WHITE}",
                "markdown.h5": f"bold {WHITE}",
                "markdown.h6": f"bold {WHITE}",
            }
        )

        self._render_console = Console(
            file=io.StringIO(),
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            markup=False,
            safe_box=False,
            legacy_windows=False,
            soft_wrap=False,
            theme=theme,
        )

    _SCROLLBAR_WIDTH = 1
    _RIGHT_GUTTER = 2
    _CONTENT_INDENT = 2
    _QUERY_TRIGGER_OFFSET = 3

    @property
    def _thinking_entries(self) -> dict[int, dict[str, object]]:
        return self._expandable_entries

    def _content_width(self) -> int:
        width = (self.size.width or 80) - self._SCROLLBAR_WIDTH - self._RIGHT_GUTTER
        return max(width, 1)

    def set_query_header_callback(self, callback: Callable[[str], None] | None) -> None:
        self._query_header_callback = callback
        self._notify_query_header()

    def _query_at_scroll(self) -> str:
        cursor = 0
        current = self._initial_query_text
        scroll_y = int(self.scroll_offset.y)
        rendered_query_count = 0
        for entry in self._renderable_log:
            query_text = entry[4] if len(entry) > 4 else None
            if query_text is not None:
                trigger_offset = (
                    0
                    if not self._initial_query_text and rendered_query_count == 0
                    else self._QUERY_TRIGGER_OFFSET
                )
                trigger_scroll = min(cursor + trigger_offset, int(self.max_scroll_y))
                if trigger_scroll <= scroll_y:
                    current = query_text
                rendered_query_count += 1
            cursor += entry[1]
        return current

    def _notify_query_header(self) -> None:
        if self._query_header_callback is not None:
            self._query_header_callback(self._query_at_scroll())

    def _render_to_strips(
        self,
        renderable: RenderableType,
        width: int,
        indent: int = _CONTENT_INDENT,
    ) -> list[Strip]:
        indent = min(max(indent, 0), max(width - 1, 0))
        render_width = max(width - indent, 1)
        options = self._render_console.options.update(width=render_width, height=None)
        rendered = self._render_console.render_lines(renderable, options, pad=False)
        if not rendered:
            return [Strip([])]
        if not indent:
            return [Strip(list(segments)) for segments in rendered]
        prefix = " " * indent
        return [Strip([Segment(prefix), *segments]) for segments in rendered]

    def _thinking_renderable(self, text: str, expanded: bool, width: int) -> Text:
        arrow = "▼" if expanded else "▶"
        prefix = f"{arrow} THINKING:"
        if expanded:
            return Text(f"{prefix} {text}", style=STYLE_DIM)
        one_line = " ".join(text.split())
        max_summary = max(width - len(prefix) - 1, 1)
        if len(one_line) > max_summary:
            one_line = one_line[: max(max_summary - 1, 1)] + "…"
        return Text(f"{prefix} {one_line}", style=STYLE_DIM)

    def _rebuild_committed_lines(self) -> None:
        width = self._content_width()
        new_lines = []
        rebuilt_log = []
        for entry in self._renderable_log:
            entry_id = entry[2] if len(entry) > 2 else None
            renderable = entry[0]
            if entry_id is not None:
                state = self._expandable_entries.get(entry_id)
                if state is None:
                    continue
                kind = state.get("kind", "thinking")
                if kind == "thinking":
                    if state.get("live"):
                        renderable = Text(f"THINKING: {state['text']}", style=STYLE_DIM)
                    else:
                        renderable = self._thinking_renderable(
                            str(state["text"]),
                            bool(state["expanded"]),
                            width,
                        )
                else:
                    renderable = state["detail"] if state.get("expanded") else state["summary"]
            indent = entry[3] if len(entry) > 3 else self._CONTENT_INDENT
            strips = self._render_to_strips(renderable, width, indent)
            new_lines.extend(strips)
            query_text = entry[4] if len(entry) > 4 else None
            rebuilt_log.append((renderable, len(strips), entry_id, indent, query_text))
        self._lines = new_lines
        self._renderable_log = rebuilt_log
        self.virtual_size = Size(width, len(self._lines) + len(self._pending_strips))
        self.show_horizontal_scrollbar = False
        self._notify_query_header()
        self.refresh()

    def write(
        self,
        renderable: RenderableType,
        *,
        replace_last: bool = False,
        commit: bool = True,
        scroll_end: bool = True,
        indent: int | None = None,
        query_text: str | None = None,
    ) -> None:
        width = self._content_width()
        indent = self._CONTENT_INDENT if indent is None else indent
        new_strips = self._render_to_strips(renderable, width, indent)

        was_at_bottom = self.scroll_y >= max(self.max_scroll_y - 1, 0)

        if replace_last:
            self._pending_strips = new_strips
            self._pending_renderable = renderable
            self._pending_indent = indent
            self._pending_query_text = query_text
            if commit:
                self._lines.extend(self._pending_strips)
                self._renderable_log.append(
                    (
                        self._pending_renderable,
                        len(self._pending_strips),
                        None,
                        self._pending_indent,
                        self._pending_query_text,
                    )
                )
                self._pending_strips = []
                self._pending_renderable = None
                self._pending_indent = None
                self._pending_query_text = None
        else:
            if self._pending_strips:
                self._lines.extend(self._pending_strips)
                if self._pending_renderable is not None:
                    self._renderable_log.append(
                        (
                            self._pending_renderable,
                            len(self._pending_strips),
                            None,
                            self._pending_indent,
                            self._pending_query_text,
                        )
                    )
                self._pending_strips = []
                self._pending_renderable = None
                self._pending_indent = None
                self._pending_query_text = None
            self._lines.extend(new_strips)
            self._renderable_log.append((renderable, len(new_strips), None, indent, query_text))

        total_lines = len(self._lines) + len(self._pending_strips)
        self.virtual_size = Size(width, total_lines)
        self.show_horizontal_scrollbar = False

        if scroll_end and was_at_bottom:
            self.scroll_end(animate=False)
        self._notify_query_header()
        self.refresh()

    def write_query(self, text: str) -> None:
        """Append a query bar entry inside this transcript's scroll surface."""
        is_followup = bool(self._initial_query_text) or any(
            entry[4] is not None for entry in self._renderable_log
        )
        if is_followup:
            self.write(Text(""), indent=0)
        self.write(Text(f"> {text}", style=f"bold {WHITE}"), indent=0, query_text=text)
        # Match QueryBar's margin-bottom without creating another widget.
        self.write(Text(""), indent=0)
        self._notify_query_header()

    def set_initial_query(self, text: str) -> None:
        """Set the sticky initial query without duplicating it in the transcript."""
        self._initial_query_text = text
        self._notify_query_header()

    def write_expandable(self, summary: RenderableType, detail: RenderableType) -> int:
        self._commit_pending()
        was_at_bottom = self._was_at_bottom()
        entry_id = self._next_expandable_id
        self._next_expandable_id += 1
        self._expandable_entries[entry_id] = {
            "kind": "generic",
            "summary": summary,
            "detail": detail,
            "expanded": False,
        }
        indent = self._CONTENT_INDENT
        strips = self._render_to_strips(summary, self._content_width(), indent)
        self._lines.extend(strips)
        self._renderable_log.append((summary, len(strips), entry_id, indent, None))
        self.virtual_size = Size(self._content_width(), len(self._lines) + len(self._pending_strips))
        self.show_horizontal_scrollbar = False
        if was_at_bottom:
            self.scroll_end(animate=False)
        self.refresh()
        return entry_id

    def discard_pending(self) -> None:
        if not self._pending_strips and self._pending_renderable is None:
            return
        self._pending_strips = []
        self._pending_renderable = None
        self._pending_indent = None
        self._pending_query_text = None
        self.virtual_size = Size(self._content_width(), len(self._lines))
        self.show_horizontal_scrollbar = False
        self.refresh()

    def clear(self) -> None:
        self._lines = []
        self._pending_strips = []
        self._renderable_log = []
        self._pending_renderable = None
        self._pending_indent = None
        self._pending_query_text = None
        self._initial_query_text = ""
        self._expandable_entries = {}
        self._next_expandable_id = 1
        self._live_thinking_id = None
        self.virtual_size = Size(self._content_width(), 0)
        self.show_horizontal_scrollbar = False
        self.scroll_home(animate=False)
        self._notify_query_header()
        self.refresh()

    def _get_bg(self) -> RichStyle:
        bg = self.styles.background
        if bg and bg.a > 0:
            return RichStyle(bgcolor=f"rgb({bg.r},{bg.g},{bg.b})")
        return RichStyle()

    def render_line(self, y: int) -> Strip:
        scroll_y = int(self.scroll_offset.y)
        index = scroll_y + y
        committed = len(self._lines)
        bg = self._get_bg()
        if index < committed:
            strip = self._lines[index]
        elif index < committed + len(self._pending_strips):
            strip = self._pending_strips[index - committed]
        else:
            return Strip.blank(self._content_width(), bg)
        strip = strip.adjust_cell_length(self._content_width())
        if bg:
            strip = strip.apply_style(bg)
        return strip

    def on_resize(self) -> None:
        self._pending_strips = []
        self._pending_renderable = None
        self._pending_indent = None
        self._pending_query_text = None
        self._rebuild_committed_lines()

    def _commit_pending(self) -> None:
        if not self._pending_strips:
            return
        self._lines.extend(self._pending_strips)
        if self._pending_renderable is not None:
            self._renderable_log.append(
                (
                    self._pending_renderable,
                    len(self._pending_strips),
                    None,
                    self._pending_indent,
                    self._pending_query_text,
                )
            )
        self._pending_strips = []
        self._pending_renderable = None
        self._pending_indent = None
        self._pending_query_text = None

    def _was_at_bottom(self) -> bool:
        return self.scroll_y >= max(self.max_scroll_y - 1, 0)

    def append_thinking_delta(self, text: str) -> None:
        was_at_bottom = self._was_at_bottom()
        self._commit_pending()
        if self._live_thinking_id is None:
            thinking_id = self._next_expandable_id
            self._next_expandable_id += 1
            self._live_thinking_id = thinking_id
            self._expandable_entries[thinking_id] = {
                "kind": "thinking",
                "text": text,
                "expanded": False,
                "live": True,
            }
            renderable = Text(f"THINKING: {text}", style=STYLE_DIM)
            strips = self._render_to_strips(renderable, self._content_width(), self._CONTENT_INDENT)
            self._lines.extend(strips)
            self._renderable_log.append(
                (renderable, len(strips), thinking_id, self._CONTENT_INDENT, None)
            )
            self.virtual_size = Size(self._content_width(), len(self._lines))
            self.show_horizontal_scrollbar = False
            if was_at_bottom:
                self.scroll_end(animate=False)
            self.refresh()
            return

        state = self._expandable_entries.get(self._live_thinking_id)
        if state is not None:
            state["text"] = text
            self._rebuild_committed_lines()
            if was_at_bottom:
                self.scroll_end(animate=False)

    def append_thinking_trace(self, text: str) -> None:
        was_at_bottom = self._was_at_bottom()
        if self._live_thinking_id is not None:
            state = self._expandable_entries.get(self._live_thinking_id)
            if state is not None:
                state["text"] = text
                state["expanded"] = False
                state["live"] = False
                self._live_thinking_id = None
                self._rebuild_committed_lines()
                if was_at_bottom:
                    self.scroll_end(animate=False)
                return
            self._live_thinking_id = None

        thinking_id = self._next_expandable_id
        self._next_expandable_id += 1
        self._expandable_entries[thinking_id] = {
            "kind": "thinking",
            "text": text,
            "expanded": False,
            "live": False,
        }
        renderable = self._thinking_renderable(text, False, self._content_width())
        strips = self._render_to_strips(renderable, self._content_width(), self._CONTENT_INDENT)
        self._commit_pending()
        self._lines.extend(strips)
        self._renderable_log.append(
            (renderable, len(strips), thinking_id, self._CONTENT_INDENT, None)
        )
        self.virtual_size = Size(self._content_width(), len(self._lines))
        self.show_horizontal_scrollbar = False
        if was_at_bottom:
            self.scroll_end(animate=False)
        self.refresh()

    def on_click(self, event) -> None:
        line_index = int(self.scroll_offset.y) + int(event.y)
        cursor = 0
        for _, strip_count, entry_id, _indent, _query_text in self._renderable_log:
            if cursor <= line_index < cursor + strip_count:
                if entry_id is not None:
                    state = self._expandable_entries.get(entry_id)
                    if state is not None:
                        state["expanded"] = not bool(state["expanded"])
                        self._rebuild_committed_lines()
                        self._notify_query_header()
                        event.stop()
                return
            cursor += strip_count

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if round(old_value) != round(new_value):
            self._notify_query_header()
