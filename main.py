"""lterm-textual — A TUI terminal emulator built with Textual."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import tempfile
import termios
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import pyte
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.containers import Horizontal, Vertical
from textual.widgets import Label, RichLog, Static

from agent import ask as agent_ask, ask_error as agent_ask_error, generate_suggestions


# Strips ANSI/VT escape sequences from raw bytes
_ANSI_ESCAPE_RE = re.compile(
    rb"\x1b(?:"
    rb"\[[0-?]*[ -/]*[@-~]"   # CSI sequences
    rb"|\][^\x07]*\x07"       # OSC sequences
    rb"|[PX^_][^\x1b]*\x1b\\" # DCS / SOS / PM / APC
    rb"|[@-_]"                 # 2-char Fe sequences
    rb"|[^\x1b]"               # any other ESC+char
    rb")",
    re.DOTALL,
)

# Shell integration: bash rcfile injected at shell startup
_RCFILE_TEMPLATE = r"""
[[ -f ~/.bashrc ]] && source ~/.bashrc

# --- lterm shell integration ---

__lterm_prompt() {
    local __exit=$?
    local __cmd
    __cmd=$(HISTTIMEFORMAT='' history 1 | sed 's/^[[:space:]]*[0-9]*[[:space:]]*//')
    printf '\033]9999;%d\037%s\037%s\037%s\007' \
        "$__exit" "$USER" "$PWD" "$__cmd"
}

export -f __lterm_prompt

if [[ -z "$PROMPT_COMMAND" ]]; then
    PROMPT_COMMAND='__lterm_prompt'
elif [[ "$PROMPT_COMMAND" != *__lterm_prompt* ]]; then
    PROMPT_COMMAND="__lterm_prompt; $PROMPT_COMMAND"
fi
export PROMPT_COMMAND

PS0=$'\033]9998;\007'
export PS0

# Silent history reload triggered by lterm via SIGUSR1
trap 'history -r' SIGUSR1
"""


def _strip_ansi(data: bytes) -> bytes:
    return _ANSI_ESCAPE_RE.sub(b"", data)


# Map named pyte colors → hex strings (standard VGA palette)
_NAMED_COLORS: dict[str, str] = {
    "black": "#000000",
    "red": "#cc0000",
    "green": "#4e9a06",
    "brown": "#c4a000",
    "blue": "#3465a4",
    "magenta": "#75507b",
    "cyan": "#06989a",
    "white": "#d3d7cf",
    "brightblack": "#555753",
    "brightred": "#ef2929",
    "brightgreen": "#8ae234",
    "brightbrown": "#fce94f",
    "brightblue": "#729fcf",
    "brightmagenta": "#ad7fa8",
    "brightcyan": "#34e2e2",
    "brightwhite": "#eeeeec",
}

_SPECIAL_KEYS: dict[str, bytes] = {
    "enter": b"\r",
    "backspace": b"\x7f",
    "tab": b"\t",
    "escape": b"\x1b",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "delete": b"\x1b[3~",
    "insert": b"\x1b[2~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
    # Common ctrl combinations
    "ctrl+a": b"\x01",
    "ctrl+b": b"\x02",
    "ctrl+c": b"\x03",
    "ctrl+d": b"\x04",
    "ctrl+e": b"\x05",
    "ctrl+f": b"\x06",
    "ctrl+k": b"\x0b",
    "ctrl+l": b"\x0c",
    "ctrl+n": b"\x0e",
    "ctrl+p": b"\x10",
    "ctrl+r": b"\x12",
    "ctrl+s": b"\x13",
    "ctrl+u": b"\x15",
    "ctrl+w": b"\x17",
    "ctrl+z": b"\x1a",
}


def _resolve_color(color: str) -> Optional[str]:
    """Convert a pyte color value to a Rich-compatible color string."""
    if color == "default":
        return None
    # True color: pyte returns 6-char hex without '#'
    if len(color) == 6 and all(c in "0123456789abcdefABCDEF" for c in color):
        return f"#{color}"
    # 256-color index stored as a decimal string
    if color.isdigit():
        return f"color({color})"
    return _NAMED_COLORS.get(color, None)


class StatusHeader(Widget):
    """A slim header bar showing the app title and current time."""

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


class ScrollbackScreen(pyte.Screen):
    """pyte.Screen extended with a scrollback history buffer."""

    def __init__(self, cols: int, rows: int, history_size: int = 5000) -> None:
        super().__init__(cols, rows)
        self._history: deque[dict] = deque(maxlen=history_size)
        self._in_alt_screen: bool = False

    def set_mode(self, *modes: int, **kwargs) -> None:  # type: ignore[override]
        super().set_mode(*modes, **kwargs)
        if kwargs.get("private") and 1049 in modes:
            self._in_alt_screen = True

    def reset_mode(self, *modes: int, **kwargs) -> None:  # type: ignore[override]
        super().reset_mode(*modes, **kwargs)
        if kwargs.get("private") and 1049 in modes:
            self._in_alt_screen = False

    def index(self) -> None:
        # self.margins is None when no explicit scroll region is set.
        margins = self.margins
        top, bottom = margins if margins is not None else (0, self.lines - 1)
        # Only save when the screen is actually about to scroll (cursor at bottom)
        # and the scroll region starts at row 0 (main screen, not a sub-region).
        if not self._in_alt_screen and top == 0 and self.cursor.y == bottom:
            self._history.append(dict(self.buffer[top]))
        super().index()


class TerminalView(Widget):
    """An embedded VT100 terminal emulator widget powered by pyte."""

    class CommandExecuted(Message):
        """Posted when a shell command finishes."""
        def __init__(
            self,
            exit_code: int,
            user: str,
            cwd: str,
            command: str,
            output: str,
        ) -> None:
            super().__init__()
            self.exit_code = exit_code
            self.user = user
            self.cwd = cwd
            self.command = command
            self.output = output

    class AgentQueried(Message):
        """Posted when the user submits a -- query for the agent."""
        def __init__(self, prompt: str, context: str) -> None:
            super().__init__()
            self.prompt = prompt
            self.context = context

    DEFAULT_CSS = """
    TerminalView {
        width: 1fr;
        height: 1fr;
        min-height: 5;
        background: #000000;
        color: #ffffff;
    }
    """

    can_focus = True

    def __init__(self, shell: str = "/bin/bash", **kwargs) -> None:
        super().__init__(**kwargs)
        self._shell = shell
        self._fd: Optional[int] = None
        self._pid: Optional[int] = None
        self._screen: Optional[pyte.Screen] = None
        self._stream: Optional[pyte.ByteStream] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._rcfile: Optional[str] = None
        # OSC-based shell integration state
        self._raw_buf = bytearray()
        self._output_buf = bytearray()
        self._capturing = False
        self._command_started = False  # True once PS0 (OSC 9998) has fired
        # Line buffer for -- agent interception
        self._line_buf: str = ""
        self._last_context: str = ""  # filled by CommandExecuted
        # Track PTY size to avoid unnecessary pyte screen truncation
        self._pty_cols: int = 0
        self._pty_rows: int = 0
        self._scroll_offset: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        cols = max(self.size.width or 80, 1)
        rows = max(self.size.height or 24, 1)
        self._start_shell(cols, rows)
        self.focus()

    def on_unmount(self) -> None:
        self._running = False
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        if self._rcfile is not None:
            try:
                os.unlink(self._rcfile)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Shell process
    # ------------------------------------------------------------------

    def _start_shell(self, cols: int, rows: int) -> None:
        self._screen = ScrollbackScreen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)

        # Write shell integration rcfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", delete=False, prefix="lterm_rc_"
        ) as f:
            f.write(_RCFILE_TEMPLATE)
            self._rcfile = f.name

        self._pid, self._fd = pty.fork()

        if self._pid == 0:  # child
            env = {
                **os.environ,
                "TERM": "xterm-256color",
                "COLUMNS": str(cols),
                "LINES": str(rows),
            }
            try:
                os.execvpe(
                    self._shell,
                    [self._shell, "--rcfile", self._rcfile],
                    env,
                )
            except Exception:
                pass
            os._exit(1)

        # parent
        self._resize_pty(cols, rows)
        self._pty_cols, self._pty_rows = cols, rows
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _resize_pty(self, cols: int, rows: int) -> None:
        if self._fd is not None:
            fcntl.ioctl(
                self._fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )

    def _read_loop(self) -> None:
        while self._running:
            try:
                r, _, _ = select.select([self._fd], [], [], 0.05)
                if r:
                    data = os.read(self._fd, 8192)
                    if not data:
                        break
                    pyte_data = self._process_raw(data)
                    self._stream.feed(pyte_data)
                    self._scroll_offset = 0  # new output → snap to live
                    self.app.call_from_thread(self.refresh)
            except OSError:
                break
        self._running = False
        self.app.call_from_thread(self._on_shell_exit)

    def _on_shell_exit(self) -> None:
        panel = self.app.query_one(BottomPanel)
        panel.clear()
        panel.write("Goodbye! Closing in 2 seconds…")
        self.app.set_timer(2, self.app.exit)

    # ------------------------------------------------------------------
    # OSC shell-integration parser
    # ------------------------------------------------------------------

    def _process_raw(self, data: bytes) -> bytes:
        """Strip OSC 9998/9999 markers; return cleaned bytes for pyte."""
        self._raw_buf.extend(data)
        buf = bytes(self._raw_buf)
        pyte_data = bytearray()
        pos = 0

        while pos < len(buf):
            osc_start = buf.find(b"\x1b]", pos)

            if osc_start == -1:
                # No OSC ahead — keep last byte if it could be partial ESC
                if buf.endswith(b"\x1b"):
                    chunk = buf[pos:-1]
                    self._raw_buf = bytearray(b"\x1b")
                else:
                    chunk = buf[pos:]
                    self._raw_buf = bytearray()
                pyte_data.extend(chunk)
                if self._capturing:
                    self._output_buf.extend(chunk)
                break

            # Flush bytes before the OSC
            chunk = buf[pos:osc_start]
            pyte_data.extend(chunk)
            if self._capturing:
                self._output_buf.extend(chunk)
            pos = osc_start

            # Find terminator: BEL (\x07) or ST (\x1b\x5c)
            bel_pos = buf.find(b"\x07", pos + 2)
            st_pos = buf.find(b"\x1b\\", pos + 2)

            if bel_pos == -1 and st_pos == -1:
                # Incomplete OSC — hold in buffer unless it's suspiciously long
                # (cap at 2 KB to avoid blocking on malformed sequences)
                if len(buf) - pos > 2048:
                    pyte_data.extend(b"\x1b]")
                    pos += 2
                    continue
                self._raw_buf = bytearray(buf[pos:])
                break

            # Pick whichever terminator comes first
            if bel_pos != -1 and (st_pos == -1 or bel_pos < st_pos):
                osc_body = buf[pos + 2 : bel_pos]
                pos = bel_pos + 1
                raw_term = b"\x07"
            else:
                osc_body = buf[pos + 2 : st_pos]
                pos = st_pos + 2
                raw_term = b"\x1b\\"

            if osc_body == b"9998;":
                # Command start: begin capturing output
                self._output_buf = bytearray()
                self._capturing = True
                self._command_started = True
            elif osc_body.startswith(b"9999;"):
                # Command end: parse metadata and emit message
                self._capturing = False
                if self._command_started:
                    self._handle_command_end(osc_body[5:])
                    self._command_started = False
                self._output_buf = bytearray()
            else:
                # Unknown OSC — pass through to pyte unchanged
                seq = b"\x1b]" + osc_body + raw_term
                pyte_data.extend(seq)
                if self._capturing:
                    self._output_buf.extend(seq)
        else:
            self._raw_buf = bytearray()

        return bytes(pyte_data)

    def _handle_command_end(self, metadata: bytes) -> None:
        text = metadata.decode("utf-8", errors="replace")
        parts = text.split("\x1f", 3)
        if len(parts) < 4:
            return
        try:
            exit_code = int(parts[0])
        except ValueError:
            exit_code = -1
        user, cwd, command = parts[1], parts[2], parts[3]
        # Skip empty commands
        if not command.strip():
            return
        raw_out = bytes(self._output_buf)
        clean_out = (
            _strip_ansi(raw_out)
            .decode("utf-8", errors="replace")
            .replace("\r", "")
            .strip()
        )
        # Keep context for the next agent query
        self._last_context = (
            f"User: {user}\nCwd: {cwd}\nLast command: {command}\n"
            + (f"Output:\n{clean_out}" if clean_out else "")
        )
        self.app.call_from_thread(
            self.post_message,
            TerminalView.CommandExecuted(exit_code, user, cwd, command, clean_out),
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        cols, rows = event.size.width, event.size.height
        self._pty_cols, self._pty_rows = cols, rows
        if self._screen is not None:
            self._screen.resize(rows, cols)
        self._resize_pty(cols, rows)

    def on_key(self, event: events.Key) -> None:
        if self._fd is None or not self._running:
            return

        # Scrollback navigation — Shift+Up/Down scroll one line,
        # Shift+PageUp/Down scroll half a screen.  Any other key snaps to live.
        if event.key in ("shift+up", "shift+down", "shift+pageup", "shift+pagedown"):
            screen = self._screen
            if isinstance(screen, ScrollbackScreen):
                history_len = len(screen._history)
                rows = screen.lines
                half = max(rows // 2, 1)
                if event.key == "shift+up":
                    self._scroll_offset = min(self._scroll_offset + 1, history_len)
                elif event.key == "shift+down":
                    self._scroll_offset = max(self._scroll_offset - 1, 0)
                elif event.key == "shift+pageup":
                    self._scroll_offset = min(self._scroll_offset + half, history_len)
                elif event.key == "shift+pagedown":
                    self._scroll_offset = max(self._scroll_offset - half, 0)
            self.refresh()
            event.stop()
            event.prevent_default()
            return

        # Any other key → snap back to the live view
        self._scroll_offset = 0

        # --- Agent interception: buffer printable chars to detect -- prefix ---
        if event.character and event.key not in _SPECIAL_KEYS:
            self._line_buf += event.character
        elif event.key == "backspace":
            self._line_buf = self._line_buf[:-1]
        elif event.key == "enter":
            line = self._line_buf.strip()
            self._line_buf = ""
            if line.startswith("--"):
                prompt = line[2:].strip()
                if prompt:
                    # Erase what the user typed on the PTY line, then newline
                    # so the shell returns to a clean prompt
                    try:
                        os.write(self._fd, b"\x15\n")
                    except OSError:
                        pass
                    # Write directly to ~/.bash_history
                    history_file = os.path.expanduser("~/.bash_history")
                    try:
                        with open(history_file, "a") as hf:
                            hf.write(f"-- {prompt}\n")
                        # Signal bash to silently reload history (trap 'history -r' SIGUSR1)
                        if self._pid is not None:
                            os.kill(self._pid, signal.SIGUSR1)
                    except OSError:
                        pass
                    self.post_message(
                        TerminalView.AgentQueried(prompt, self._last_context)
                    )
                    event.stop()
                    event.prevent_default()
                    return
        # ---------------------------------------------------------------

        data: Optional[bytes] = _SPECIAL_KEYS.get(event.key)
        if data is None and event.character:
            data = event.character.encode("utf-8")

        if data is not None:
            try:
                os.write(self._fd, data)
            except OSError:
                pass
            event.stop()
            event.prevent_default()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _char_style(self, char: pyte.screens.Char) -> Style:
        fg = _resolve_color(char.fg)
        bg = _resolve_color(char.bg)
        return Style(
            color=fg,
            bgcolor=bg,
            bold=char.bold,
            italic=char.italics,
            underline=char.underscore,
            blink=char.blink,
            reverse=char.reverse,
            strike=char.strikethrough,
        )

    def render(self) -> Text:
        if self._screen is None:
            return Text()

        screen = self._screen
        result = Text(end="", no_wrap=True, overflow="fold")
        offset = self._scroll_offset

        if offset > 0 and isinstance(screen, ScrollbackScreen):
            # Build a virtual viewport: last `offset` history rows + top screen rows
            history = screen._history
            num_hist = min(offset, len(history), screen.lines)
            hist_start = max(len(history) - offset, 0)
            hist_rows = list(history)[hist_start : hist_start + num_hist]
            num_screen = screen.lines - num_hist

            all_rows: list[tuple[bool, object]] = (
                [(True, h) for h in hist_rows]
                + [(False, screen.buffer[r]) for r in range(num_screen)]
            )
            default = screen.default_char
            for i, (is_hist, row_data) in enumerate(all_rows):
                for col in range(screen.columns):
                    if is_hist:
                        char = row_data.get(col, default)  # type: ignore[union-attr]
                    else:
                        char = row_data[col]  # type: ignore[index]
                    result.append(char.data, self._char_style(char))
                if i < len(all_rows) - 1:
                    result.append("\n")
        else:
            for row in range(screen.lines):
                row_buf = screen.buffer[row]
                for col in range(screen.columns):
                    char = row_buf[col]
                    style = self._char_style(char)
                    if row == screen.cursor.y and col == screen.cursor.x:
                        style = style + Style(reverse=True)
                    result.append(char.data, style=style)
                if row < screen.lines - 1:
                    result.append("\n")

        return result


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
        margin: 0 0 1 0;
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

    def on_child_removed(self, event: events.ChildRemoved) -> None:
        if not list(self.children):
            self.display = False

    def _check_visibility(self) -> None:
        # Remove rows that became empty after a button click
        for row in list(self.query(Horizontal)):
            if not list(row.children):
                row.remove()
        if not list(self.query(SuggestionButton)):
            self.display = False


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
        self.app.query_one(BottomPanel).styles.height = new_height
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
        event.stop()


class BottomPanel(RichLog):
    """A scrollable log panel at the bottom of the screen.

    Public API
    ----------
    write(text)  – append a line of text (inherited from RichLog).
    clear()      – remove all content (inherited from RichLog).
    """

    DEFAULT_CSS = """
    BottomPanel {
        height: 8;
        background: #11111b;
        color: #cdd6f4;
        border-top: solid #313244;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(highlight=False, markup=False, wrap=True, **kwargs)


class TerminalApp(App):
    """lterm-textual — minimal terminal emulator TUI."""

    TITLE = "LTerm"
    CSS = """
    Screen {
        background: #000000;
        padding: 0;
        layout: vertical;
    }
    #bottom-dock {
        height: auto;
        width: 1fr;
    }
    """
    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        yield TerminalView()
        with Vertical(id="bottom-dock"):
            yield SuggestionBar()
            yield ResizeHandle()
            yield BottomPanel()

    def on_mount(self) -> None:
        self.panel.write("Welcome to LTerm! I'm your terminal learning assistant.")
        # self.add_suggestion("echo hello suggestions")

    @property
    def suggestions(self) -> SuggestionBar:
        """Convenience accessor for the suggestion bar."""
        return self.query_one(SuggestionBar)

    def add_suggestion(self, text: str) -> None:
        """Append a suggestion button to the bar."""
        self.suggestions.add_suggestion(text)

    def clear_suggestions(self) -> None:
        """Remove all suggestion buttons."""
        self.suggestions.clear_suggestions()

    def on_suggestion_bar_pressed(self, event: SuggestionBar.Pressed) -> None:
        terminal = self.query_one(TerminalView)
        if terminal._fd is not None and terminal._running:
            try:
                os.write(terminal._fd, event.text.encode())
            except OSError:
                pass

    @property
    def panel(self) -> BottomPanel:
        """Convenience accessor for the bottom panel."""
        return self.query_one(BottomPanel)

    def on_terminal_view_agent_queried(
        self, event: TerminalView.AgentQueried
    ) -> None:
        """Run the agent query in a background thread and stream into the panel."""
        panel = self.panel
        panel.clear()
        panel.write(f"⟩ {event.prompt}")
        panel.write("")

        def _run() -> None:
            buf = ""
            last_update = 0.0
            prompt_line = f"⟩ {event.prompt}"

            def _flush() -> None:
                panel.clear()
                panel.write(prompt_line)
                panel.write(buf)

            try:
                for chunk in agent_ask(event.prompt, context=event.context or None):
                    buf += chunk
                    now = time.monotonic()
                    if now - last_update >= 0.1:
                        self.call_from_thread(_flush)
                        last_update = now
                # Final update to show the complete response
                self.call_from_thread(_flush)
                # After streaming completes, generate suggestions from the answer
                if buf:
                    try:
                        suggestions = generate_suggestions(buf)
                        self.call_from_thread(self.clear_suggestions)
                        for s in suggestions:
                            self.call_from_thread(self.add_suggestion, s)
                    except Exception:
                        pass
            except Exception as exc:
                self.call_from_thread(panel.write, f"[agent error] {exc}")

        threading.Thread(target=_run, daemon=True).start()

    def on_terminal_view_command_executed(
        self, event: TerminalView.CommandExecuted
    ) -> None:
        ok = event.exit_code == 0
        if ok:
            icon = "✓ OK"
            lines = [
                icon,
                f"  Current User: {event.user}",
                f"  Current Dir:  {event.cwd}",
                f"  Last Command: {event.command}",
            ]
            if event.output:
                lines.append(f"  Output: {event.output}")
            self.panel.clear()
            self.panel.write("\n".join(lines))

        if not ok:
            error_info = (
                f"✗ ERROR\n"
                f"  Current User: {event.user}\n"
                f"  Current Dir:  {event.cwd}\n"
                f"  Last Command: {event.command}\n"
                + (f"  Output: {event.output}" if event.output else "")
            )
            panel = self.panel

            def _run_error() -> None:
                buf = ""
                last_update = 0.0

                def _flush() -> None:
                    panel.clear()
                    panel.write(buf)

                try:
                    for chunk in agent_ask_error(error_info):
                        buf += chunk
                        now = time.monotonic()
                        if now - last_update >= 0.1:
                            self.call_from_thread(_flush)
                            last_update = now
                    # Final update to show the complete response
                    self.call_from_thread(_flush)
                    if buf:
                        try:
                            suggestions = generate_suggestions(buf)
                            self.call_from_thread(self.clear_suggestions)
                            for s in suggestions:
                                self.call_from_thread(self.add_suggestion, s)
                        except Exception:
                            pass
                except Exception as exc:
                    self.call_from_thread(panel.clear)
                    self.call_from_thread(
                        panel.write, f"[agent unavailable] {exc}"
                    )

            threading.Thread(target=_run_error, daemon=True).start()

    def action_quit(self) -> None:
        self.exit()


def main() -> None:
    app = TerminalApp()
    app.run()


if __name__ == "__main__":
    main()
