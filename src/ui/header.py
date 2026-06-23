from textual.widget import Widget
from textual.reactive import reactive
from textual.widgets import Label
from textual.app import ComposeResult

from datetime import datetime

class StatusHeader(Widget):

    DEFAULT_CSS = """
    StatusHeader {
        dock: top;
        height: 1;
        background: #1c1c2e;
        color: #cdd6f4;
        layout: horizontal;
        padding: 0 1;
    }
    StatusHeader #title {
        width: 1fr;
        color: #89b4fa;
    }
    StatusHeader #clock {
        width: auto;
        color: #a6e3a1;
    }
    """

    _time: reactive[str] = reactive("", layout=False)

    def compose(self) -> ComposeResult:
        yield Label("LTerm v0.1.0", id="title")
        yield Label("", id="clock")

    def on_mount(self) -> None:
        self._tick()
        self.set_interval(1, self._tick)

    def _tick(self) -> None:
        self.query_one("#clock", Label).update(datetime.now().strftime("%H:%M:%S"))
