from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

import pyte
from rich.style import Style
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget

from src.ui.bottompanel import TextArea
from src.ui.terminal_backend import CommandEndEvent, OscParser, PtyManager

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
    intercepted: bool = False


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
        self._screen: Optional[pyte.Screen] = None
        self._stream: Optional[pyte.ByteStream] = None
        self._scroll_offset: int = 0
        self._render_lock = threading.Lock()
        self._rendered: Text = Text()
        self._refresh_pending = False
        
        self._pty_manager: Optional[PtyManager] = None
        self._osc_parser = OscParser(on_command_start=self._bind_active_handle)

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

    def history_snapshot(self, limit: int | None = None) -> list[dict[str, object]]:
        handles = self._handles if limit is None else self._handles[-max(limit, 0) :]
        items: list[dict[str, object]] = []
        for handle in handles:
            kind = "shell_command"
            if handle.intercepted and handle.command.startswith("--"):
                kind = "agent_question"
            elif handle.intercepted and handle.command.startswith("??"):
                kind = "agent_command_explanation"
            elif handle.intercepted:
                kind = "agent_query"

            items.append(
                {
                    "handle_id": handle.handle_id,
                    "kind": kind,
                    "command": handle.command,
                    "output": handle.output,
                    "exit_code": handle.exit_code,
                    "user": handle.user,
                    "cwd": handle.cwd,
                    "started": handle.started,
                    "completed": handle.completed,
                    "intercepted": handle.intercepted,
                }
            )
        return items

    def _replace_prompt_input(self, text: str) -> None:
        if not self._pty_manager or not self._pty_manager.is_running:
            return
        self._pty_manager.write_byte(b"\x15")
        if text:
            self._pty_manager.write_byte(text.encode("utf-8"))
        self._line_buf = text
        self._line_cursor = len(text)

    def set_prompt_input(self, text: str) -> bool:
        if not self._pty_manager or not self._pty_manager.is_running or not self._prompt_is_idle():
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
            intercepted=True,
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
        return not self._osc_parser.is_capturing and not self._osc_parser.is_command_started

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        cols = max(self.size.width or 80, 1)
        rows = max(self.size.height or 24, 1)
        
        self._screen = ScrollbackScreen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        
        self._pty_manager = PtyManager(
            shell=self._shell,
            cols=cols,
            rows=rows,
            on_data=self._on_pty_data,
            on_exit=self._on_pty_exit
        )
        
        self.focus()

    def on_unmount(self) -> None:
        if self._pty_manager is not None:
            self._pty_manager.close()

    def _on_pty_data(self, data: bytes) -> None:
        pyte_data, events = self._osc_parser.feed(data)
        with self._render_lock:
            if self._stream is not None:
                self._stream.feed(pyte_data)
            self._scroll_offset = 0
            self._rendered = self._build_text()
        
        if not self._refresh_pending:
            self._refresh_pending = True
            self.app.call_from_thread(self._do_refresh)

        for event in events:
            self._handle_command_end(event)

    def _on_pty_exit(self) -> None:
        self.app.call_from_thread(self._on_shell_exit)

    def _do_refresh(self) -> None:
        self._refresh_pending = False
        self.refresh()

    def _handle_command_end(self, event: CommandEndEvent) -> None:
        self._shell_user = event.user
        self._shell_cwd = event.cwd
        
        handle = self._active_handle
        command = handle.command if handle is not None else ""
        if not command.strip():
            return
            
        if handle is not None:
            handle.output = event.output
            handle.exit_code = event.exit_code
            handle.user = event.user
            handle.cwd = event.cwd
            handle.completed = True
            self._active_handle = None
            
        self._last_context = self._build_context(command, event.output)
        self.app.call_from_thread(
            self.post_message,
            TerminalView.CommandExecuted(event.exit_code, event.user, event.cwd, command, event.output),
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def on_paste(self, event: events.Paste) -> None:
        if not self._pty_manager or not self._pty_manager.is_running:
            return
        text = event.text
        if not text:
            return
            
        if self._prompt_is_idle():
            single_line_text = text.replace("\r", "").replace("\n", " ")
            self._insert_input(single_line_text)
            
        self._pty_manager.write(text.encode("utf-8"))

    def on_resize(self, event: events.Resize) -> None:
        cols, rows = event.size.width, event.size.height
        with self._render_lock:
            if self._screen is not None:
                self._screen.resize(rows, cols)
            if self._pty_manager is not None:
                self._pty_manager.resize(cols, rows)
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
        if not self._pty_manager or not self._pty_manager.is_running:
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
                self._pty_manager.close()
                self._pty_manager = None
                self._on_shell_exit()
                event.stop()
                event.prevent_default()
                return
            if submitted.startswith("--") or submitted.startswith("??"):
                prefix = submitted[:2]
                body = submitted[2:].strip()
                if body:
                    self._pty_manager.write_byte(b"\x15")
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
            self._pty_manager.write(data)
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
