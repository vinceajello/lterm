from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import termios
import threading
from dataclasses import dataclass
from typing import Optional

import pyte
from collections import deque
from rich.style import Style
from rich.text import Text
from src.ui.bottompanel import TextArea
from textual import events
from textual.message import Message
from textual.widget import Widget

_SPECIAL_KEYS: dict[str, bytes] = {
    "enter": b"\r",
    "backspace": b"\x7f",
    "tab": b"\t",
    "ctrl+u": b"\x15",
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
}

_ANSI_ESCAPE_RE = re.compile(
    rb"\x1b(?:"
    rb"\[[0-?]*[ -/]*[@-~]"
    rb"|\][^\x07]*\x07"
    rb"|[PX^_][^\x1b]*\x1b\\"
    rb"|[@-_]"
    rb"|[^\x1b]"
    rb")",
    re.DOTALL,
)

_LTERM_PROMPT_CMD = (
    r"__lterm_e=$?;"
    r" printf '\033]9999;%d\037%s\037%s\007'"
    " \"$__lterm_e\" \"$USER\" \"$PWD\""
)
_LTERM_PS0 = "\033]9998;\007"


@dataclass(slots=True)
class CommandHandle:
    handle_id: int
    command: str
    output: str = ""
    exit_code: Optional[int] = None
    user: str = ""
    cwd: str = ""
    started: bool = False
    completed: bool = False

def _strip_ansi(data: bytes) -> bytes:
    return _ANSI_ESCAPE_RE.sub(b"", data)

class ScrollbackScreen(pyte.Screen):
    """pyte.Screen with a scrollback history buffer."""

    def __init__(self, cols: int, rows: int, history_size: int = 5000) -> None:
        super().__init__(cols, rows)
        self._history: deque[dict] = deque(maxlen=history_size)

    def index(self) -> None:
        margins = self.margins
        top, bottom = margins if margins is not None else (0, self.lines - 1)
        if top == 0 and self.cursor.y == bottom:
            self._history.append(dict(self.buffer[top]))
        super().index()

def _resolve_color(color: str) -> Optional[str]:
    _named = {
        "black": "#000000", "red": "#cc0000", "green": "#4e9a06",
        "brown": "#c4a000", "blue": "#3465a4", "magenta": "#75507b",
        "cyan": "#06989a", "white": "#d3d7cf", "brightblack": "#555753",
        "brightred": "#ef2929", "brightgreen": "#8ae234", "brightbrown": "#fce94f",
        "brightblue": "#729fcf", "brightmagenta": "#ad7fa8",
        "brightcyan": "#34e2e2", "brightwhite": "#eeeeec",
    }
    if color == "default":
        return None
    if len(color) == 6 and all(c in "0123456789abcdefABCDEF" for c in color):
        return f"#{color}"
    if color.isdigit():
        return f"color({color})"
    return _named.get(color)

class TerminalView(Widget):
    """A bare-minimum VT100 terminal emulator widget powered by pyte."""

    class CommandExecuted(Message):
        """Posted when a shell command finishes."""
        def __init__(self, exit_code: int, user: str, cwd: str, command: str, output: str, intercepted: bool = False) -> None:
            super().__init__()
            self.exit_code = exit_code
            self.user = user
            self.cwd = cwd
            self.command = command
            self.output = output
            self.intercepted = intercepted

    class AgentQueried(Message):
        """Posted when the user submits a -- or ?? query for the agent."""
        def __init__(self, query: str, context: str, mode: str = "ask") -> None:
            super().__init__()
            self.query = query
            self.context = context
            self.mode = mode

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
        self._scroll_offset: int = 0
        self._render_lock = threading.Lock()
        self._rendered: Text = Text()
        self._refresh_pending = False
        self._raw_buf = bytearray()
        self._output_buf = bytearray()
        self._capturing = False
        self._command_started = False
        self._line_buf = ""
        self._line_cursor = 0
        self._history_nav_index: Optional[int] = None
        self._history_nav_draft = ""
        self._handles: list[CommandHandle] = []
        self._pending_handles: deque[CommandHandle] = deque()
        self._active_handle: Optional[CommandHandle] = None
        self._next_handle_id = 1
        self._shell_user = os.environ.get("USER", "")
        self._shell_cwd = os.getcwd()
        self._last_context = ""

    def _reset_line_state(self) -> None:
        self._line_buf = ""
        self._line_cursor = 0
        self._reset_history_navigation()

    def _reset_history_navigation(self) -> None:
        self._history_nav_index = None
        self._history_nav_draft = ""

    def _insert_input(self, text: str) -> None:
        self._line_buf = (
            self._line_buf[: self._line_cursor]
            + text
            + self._line_buf[self._line_cursor :]
        )
        self._line_cursor += len(text)
        self._reset_history_navigation()

    def _backspace_input(self) -> None:
        if self._line_cursor == 0:
            return
        self._line_buf = (
            self._line_buf[: self._line_cursor - 1]
            + self._line_buf[self._line_cursor :]
        )
        self._line_cursor -= 1
        self._reset_history_navigation()

    def _delete_input(self) -> None:
        if self._line_cursor >= len(self._line_buf):
            return
        self._line_buf = (
            self._line_buf[: self._line_cursor]
            + self._line_buf[self._line_cursor + 1 :]
        )
        self._reset_history_navigation()

    def _clear_input(self) -> None:
        self._line_buf = ""
        self._line_cursor = 0
        self._reset_history_navigation()

    def _move_cursor_left(self) -> None:
        self._line_cursor = max(self._line_cursor - 1, 0)

    def _move_cursor_right(self) -> None:
        self._line_cursor = min(self._line_cursor + 1, len(self._line_buf))

    def _move_cursor_home(self) -> None:
        self._line_cursor = 0

    def _move_cursor_end(self) -> None:
        self._line_cursor = len(self._line_buf)

    def _history_commands(self) -> list[str]:
        return [handle.command for handle in self._handles]

    def _replace_prompt_input(self, text: str) -> None:
        if self._fd is None:
            return
        try:
            os.write(self._fd, b"\x15")
            if text:
                os.write(self._fd, text.encode("utf-8"))
        except OSError:
            return
        self._line_buf = text
        self._line_cursor = len(text)

    def set_prompt_input(self, text: str) -> bool:
        if self._fd is None or not self._running or not self._prompt_is_idle():
            return False
        self._replace_prompt_input(text)
        self.focus()
        return True

    def agent_context(self) -> str:
        return self._last_context or self._build_context(self._line_buf)

    def _navigate_history(self, step: int) -> bool:
        commands = self._history_commands()
        if not commands:
            return False
        if self._history_nav_index is None:
            self._history_nav_draft = self._line_buf
            self._history_nav_index = len(commands)
        next_index = min(max(self._history_nav_index + step, 0), len(commands))
        self._history_nav_index = next_index
        if next_index == len(commands):
            self._replace_prompt_input(self._history_nav_draft)
        else:
            self._replace_prompt_input(commands[next_index])
        return True

    def _track_submitted_command(self, command: str) -> CommandHandle:
        handle = CommandHandle(handle_id=self._next_handle_id, command=command)
        self._next_handle_id += 1
        self._handles.append(handle)
        self._pending_handles.append(handle)
        return handle

    def _build_context(self, command: str, output: str = "") -> str:
        context = (
            f"User: {self._shell_user}\n"
            f"Cwd: {self._shell_cwd}\n"
            f"Last command: {command}"
        )
        if output:
            context += f"\nOutput:\n{output}"
        return context

    def _emit_command_executed(self, handle: CommandHandle, intercepted: bool = False) -> None:
        self.post_message(
            TerminalView.CommandExecuted(
                handle.exit_code if handle.exit_code is not None else -1,
                handle.user,
                handle.cwd,
                handle.command,
                handle.output,
                intercepted=intercepted,
            )
        )

    def _complete_intercepted_command(self, command: str, query: str, mode: str) -> None:
        handle = CommandHandle(
            handle_id=self._next_handle_id,
            command=command,
            exit_code=0,
            user=self._shell_user,
            cwd=self._shell_cwd,
            started=True,
            completed=True,
        )
        self._next_handle_id += 1
        self._handles.append(handle)
        self._last_context = self._build_context(command)
        self._emit_command_executed(handle, intercepted=True)
        self.post_message(
            TerminalView.AgentQueried(query, self._last_context, mode=mode)
        )

    def _bind_active_handle(self) -> None:
        if self._active_handle is None and self._pending_handles:
            self._active_handle = self._pending_handles.popleft()
            self._active_handle.started = True

    def _prompt_is_idle(self) -> bool:
        return not self._capturing and not self._command_started

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

    # ------------------------------------------------------------------
    # Shell process
    # ------------------------------------------------------------------

    def _start_shell(self, cols: int, rows: int) -> None:
        self._screen = ScrollbackScreen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._pid, self._fd = pty.fork()

        if self._pid == 0:  # child
            env = {
                **os.environ,
                "TERM": "xterm-256color",
                "COLUMNS": str(cols),
                "LINES": str(rows),
                "HISTFILE": "/dev/null",
                "HISTSIZE": "0",
                "HISTFILESIZE": "0",
                "PROMPT_COMMAND": _LTERM_PROMPT_CMD,
                "PS0": _LTERM_PS0,
            }
            try:
                os.execvpe(self._shell, [self._shell], env)
            except Exception:
                pass
            os._exit(1)

        self._resize_pty(cols, rows)
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
                    with self._render_lock:
                        self._stream.feed(pyte_data)
                        self._scroll_offset = 0
                        self._rendered = self._build_text()
                    if not self._refresh_pending:
                        self._refresh_pending = True
                        self.app.call_from_thread(self._do_refresh)
            except OSError:
                break
        self._running = False
        self.app.call_from_thread(self._on_shell_exit)

    def _do_refresh(self) -> None:
        self._refresh_pending = False
        self.refresh()

    def _process_raw(self, data: bytes) -> bytes:
        """Strip OSC 9998/9999 shell-integration markers; return bytes for pyte."""
        self._raw_buf.extend(data)
        buf = bytes(self._raw_buf)
        pyte_data = bytearray()
        pos = 0

        while pos < len(buf):
            osc_start = buf.find(b"\x1b]", pos)
            if osc_start == -1:
                chunk = buf[pos:-1] if buf.endswith(b"\x1b") else buf[pos:]
                self._raw_buf = bytearray(b"\x1b") if buf.endswith(b"\x1b") else bytearray()
                pyte_data.extend(chunk)
                if self._capturing:
                    self._output_buf.extend(chunk)
                break

            chunk = buf[pos:osc_start]
            pyte_data.extend(chunk)
            if self._capturing:
                self._output_buf.extend(chunk)
            pos = osc_start

            bel_pos = buf.find(b"\x07", pos + 2)
            st_pos = buf.find(b"\x1b\\", pos + 2)
            if bel_pos == -1 and st_pos == -1:
                self._raw_buf = bytearray(buf[pos:]) if len(buf) - pos <= 2048 else bytearray()
                if len(buf) - pos > 2048:
                    pyte_data.extend(b"\x1b]")
                    pos += 2
                    continue
                break

            if bel_pos != -1 and (st_pos == -1 or bel_pos < st_pos):
                osc_body = buf[pos + 2 : bel_pos]
                pos = bel_pos + 1
                raw_term = b"\x07"
            else:
                osc_body = buf[pos + 2 : st_pos]
                pos = st_pos + 2
                raw_term = b"\x1b\\"

            if osc_body == b"9998;":
                self._output_buf = bytearray()
                self._capturing = True
                self._command_started = True
                self._bind_active_handle()
            elif osc_body.startswith(b"9999;"):
                self._capturing = False
                if self._command_started:
                    self._handle_command_end(osc_body[5:])
                    self._command_started = False
                self._output_buf = bytearray()
            else:
                seq = b"\x1b]" + osc_body + raw_term
                pyte_data.extend(seq)
                if self._capturing:
                    self._output_buf.extend(seq)
        else:
            self._raw_buf = bytearray()

        return bytes(pyte_data)

    def _handle_command_end(self, metadata: bytes) -> None:
        text = metadata.decode("utf-8", errors="replace")
        parts = text.split("\x1f", 2)
        if len(parts) < 3:
            return
        try:
            exit_code = int(parts[0])
        except ValueError:
            exit_code = -1
        user, cwd = parts[1], parts[2]
        clean_out = (
            _strip_ansi(bytes(self._output_buf))
            .decode("utf-8", errors="replace")
            .replace("\r", "")
            .strip()
        )
        self._shell_user = user
        self._shell_cwd = cwd
        handle = self._active_handle
        command = handle.command if handle is not None else ""
        if not command.strip():
            return
        if handle is not None:
            handle.output = clean_out
            handle.exit_code = exit_code
            handle.user = user
            handle.cwd = cwd
            handle.completed = True
            self._active_handle = None
        self._last_context = self._build_context(command, clean_out)
        self.app.call_from_thread(
            self.post_message,
            TerminalView.CommandExecuted(exit_code, user, cwd, command, clean_out),
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        cols, rows = event.size.width, event.size.height
        with self._render_lock:
            if self._screen is not None:
                self._screen.resize(rows, cols)
            self._resize_pty(cols, rows)
            self._rendered = self._build_text()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if isinstance(self._screen, ScrollbackScreen):
            with self._render_lock:
                self._scroll_offset = min(
                    self._scroll_offset + 3, len(self._screen._history)
                )
                self._rendered = self._build_text()
            self.refresh()
            event.stop()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if isinstance(self._screen, ScrollbackScreen):
            with self._render_lock:
                self._scroll_offset = max(self._scroll_offset - 3, 0)
                self._rendered = self._build_text()
            self.refresh()
            event.stop()

    def on_key(self, event: events.Key) -> None:
        if self._fd is None or not self._running:
            return
        if isinstance(self._screen, ScrollbackScreen):
            history_len = len(self._screen._history)
            half = max((self._screen.lines) // 2, 1)
            if event.key == "shift+up":
                with self._render_lock:
                    self._scroll_offset = min(self._scroll_offset + 1, history_len)
                    self._rendered = self._build_text()
                self.refresh()
                event.stop()
                return
            elif event.key == "shift+down":
                with self._render_lock:
                    self._scroll_offset = max(self._scroll_offset - 1, 0)
                    self._rendered = self._build_text()
                self.refresh()
                event.stop()
                return
            elif event.key == "shift+pageup":
                with self._render_lock:
                    self._scroll_offset = min(self._scroll_offset + half, history_len)
                    self._rendered = self._build_text()
                self.refresh()
                event.stop()
                return
            elif event.key == "shift+pagedown":
                with self._render_lock:
                    self._scroll_offset = max(self._scroll_offset - half, 0)
                    self._rendered = self._build_text()
                self.refresh()
                event.stop()
                return
        if self._prompt_is_idle() and event.key == "up":
            self._navigate_history(-1)
            event.stop()
            event.prevent_default()
            return
        if self._prompt_is_idle() and event.key == "down":
            self._navigate_history(1)
            event.stop()
            event.prevent_default()
            return

        if event.character and event.key not in _SPECIAL_KEYS:
            self._insert_input(event.character)
        elif event.key == "backspace":
            self._backspace_input()
        elif event.key == "delete":
            self._delete_input()
        elif event.key == "left":
            self._move_cursor_left()
        elif event.key == "right":
            self._move_cursor_right()
        elif event.key == "home":
            self._move_cursor_home()
        elif event.key == "end":
            self._move_cursor_end()
        elif event.key == "ctrl+u":
            self._clear_input()

        self._scroll_offset = 0
        data: Optional[bytes] = _SPECIAL_KEYS.get(event.key)
        if data is None and event.character:
            data = event.character.encode("utf-8")
        if event.key == "enter":
            submitted_line = self._line_buf
            submitted = submitted_line.strip()
            if submitted == "bye":
                self._reset_line_state()
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
                    self._fd = None
                self._on_shell_exit()
                event.stop()
                event.prevent_default()
                return
            if submitted.startswith("--") or submitted.startswith("??"):
                prefix = submitted[:2]
                body = submitted[2:].strip()
                if body:
                    try:
                        os.write(self._fd, b"\x15")
                    except OSError:
                        pass
                    self._reset_line_state()
                    self._complete_intercepted_command(
                        submitted,
                        body,
                        mode="command" if prefix == "??" else "ask",
                    )
                    event.stop()
                    event.prevent_default()
                    return
            self._reset_line_state()
            if submitted:
                self._track_submitted_command(submitted)
        if data is not None:
            try:
                os.write(self._fd, data)
            except OSError:
                pass
            event.stop()
            event.prevent_default()

    def _on_shell_exit(self) -> None:
        textarea = self.app.query_one(TextArea)
        textarea.clear()
        textarea.write("Goodbye! Closing in 2 seconds…")
        self.app.set_timer(2, self.app.exit)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _build_text(self) -> Text:
        """Build a Rich Text from the current screen state. Call under _render_lock."""
        screen = self._screen
        if screen is None:
            return Text()
        result = Text(end="", no_wrap=True, overflow="fold")
        offset = self._scroll_offset
        cx, cy = screen.cursor.x, screen.cursor.y

        if offset > 0 and isinstance(screen, ScrollbackScreen):
            history = screen._history
            num_hist = min(offset, len(history), screen.lines)
            hist_start = max(len(history) - offset, 0)
            hist_rows = list(history)[hist_start : hist_start + num_hist]
            num_screen = screen.lines - num_hist
            default = screen.default_char
            all_rows: list[tuple[bool, object]] = (
                [(True, h) for h in hist_rows]
                + [(False, screen.buffer[r]) for r in range(num_screen)]
            )
            for i, (is_hist, row_data) in enumerate(all_rows):
                for col in range(screen.columns):
                    char = row_data.get(col, default) if is_hist else row_data[col]  # type: ignore[union-attr,index]
                    result.append(char.data, Style(
                        color=_resolve_color(char.fg),
                        bgcolor=_resolve_color(char.bg),
                        bold=char.bold,
                        italic=char.italics,
                        underline=char.underscore,
                        blink=char.blink,
                        reverse=char.reverse,
                        strike=char.strikethrough,
                    ))
                if i < len(all_rows) - 1:
                    result.append("\n")
        else:
            for row in range(screen.lines):
                row_buf = screen.buffer[row]
                for col in range(screen.columns):
                    char = row_buf[col]
                    style = Style(
                        color=_resolve_color(char.fg),
                        bgcolor=_resolve_color(char.bg),
                        bold=char.bold,
                        italic=char.italics,
                        underline=char.underscore,
                        blink=char.blink,
                        reverse=char.reverse,
                        strike=char.strikethrough,
                    )
                    if row == cy and col == cx:
                        style = style + Style(reverse=True)
                    result.append(char.data, style=style)
                if row < screen.lines - 1:
                    result.append("\n")
        return result

    def render(self) -> Text:
        with self._render_lock:
            return self._rendered
