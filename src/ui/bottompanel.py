from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import RichLog, Static
from textual.message import Message
from textual.widget import Widget
from rich.text import Text
from rich.style import Style
from textual import events

class ResizeHandle(Widget):
    """Draggable horizontal bar that resizes the BottomPanel."""

    DEFAULT_CSS = """
    ResizeHandle {
        height: 1;
        background: #313244;
        color: #585b70;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._dragging = False

    def render(self) -> Text:
        width = self.size.width or 80
        label = " ↕ "
        bar = "─" * ((width - len(label)) // 2)
        line = (bar + label + bar).ljust(width)[:width]
        return Text(line, style=Style(color="#585b70", bgcolor="#313244"))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        screen_height = self.app.size.height
        new_height = screen_height - event.screen_y - 1
        new_height = max(3, min(new_height, screen_height - 6))
        self.app.query_one("#bottom-dock").styles.height = new_height
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
        event.stop()


class SuggestionButton(Static):
    """A static clickable chip — no animations, no transitions."""

    DEFAULT_CSS = """
    SuggestionButton {
        height: 1;
        width: auto;
        margin: 0 1 0 0;
        padding: 0 1;
        background: #313244;
        color: #cdd6f4;
        border: none;
    }
    SuggestionButton:hover {
        text-style: underline;
    }
    """

    can_focus = False

    def __init__(self, text: str, **kwargs) -> None:
        super().__init__(text, **kwargs)
        self._text = text

    def on_click(self, event: events.Click) -> None:
        self.post_message(SuggestionBar.Pressed(self._text))
        row = self.parent
        bar = row.parent if row is not None else None
        self.remove()
        if bar is not None:
            bar.call_after_refresh(bar._check_visibility)
        event.stop()


class SuggestionBar(Widget):
    """A wrapping bar of clickable suggestion chips.

    Public API
    ----------
    add_suggestion(text)   – append a chip, wrapping to a new row if needed.
    clear_suggestions()    – remove all chips.
    """

    DEFAULT_CSS = """
    SuggestionBar {
        height: auto;
        max-height: 8;
        background: #1e1e2e;
        border-bottom: solid #313244;
        layout: vertical;
        padding: 1 1 0 1;
        overflow: hidden;
    }
    SuggestionBar Horizontal {
        height: 1;
        width: 1fr;
        background: #1e1e2e;
        margin: 0 0 0 0;
    }
    """

    class Pressed(Message):
        """Posted when a suggestion chip is clicked."""
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    # ------------------------------------------------------------------
    # Row-packing helpers
    # ------------------------------------------------------------------

    def _last_row(self) -> Horizontal | None:
        rows = [c for c in self.children if isinstance(c, Horizontal)]
        return rows[-1] if rows else None

    def _row_used_width(self, row: Horizontal) -> int:
        total = 0
        for btn in row.children:
            if isinstance(btn, SuggestionButton):
                total += len(btn._text) + 4  # 2 padding + 1 margin-right + 1 gap
        return total

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_suggestion(self, text: str) -> None:
        """Append a clickable chip, wrapping to a new row when needed."""
        self.display = True
        btn = SuggestionButton(text)
        btn_width = len(text) + 4
        available = max((self.size.width or 80) - 4, 20)

        row = self._last_row()
        if row is None or self._row_used_width(row) + btn_width > available:
            row = Horizontal()
            self.mount(row)
        row.mount(btn)

    def clear_suggestions(self) -> None:
        """Remove all chips and hide the bar."""
        for child in list(self.children):
            child.remove()
        self.display = False

    def on_mount(self) -> None:
        self.display = False

    def _check_visibility(self) -> None:
        # Remove rows that became empty after a button click
        for row in list(self.query(Horizontal)):
            if not list(row.children):
                row.remove()
        if not list(self.query(SuggestionButton)):
            self.display = False


class TextArea(RichLog):
    """A scrollable log panel at the bottom of the screen.

    Public API
    ----------
    write(text)  – append a line of text (inherited from RichLog).
    clear()      – remove all content (inherited from RichLog).
    """

    DEFAULT_CSS = """
    TextArea {
        height: 1fr;
        background: #11111b;
        color: #cdd6f4;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(highlight=False, markup=False, wrap=True, **kwargs)


class BottomPanel(Vertical):
    """Bottom dock containing the resize handle and text area."""

    def compose(self) -> ComposeResult:
        yield ResizeHandle()
        yield SuggestionBar()
        yield TextArea()
