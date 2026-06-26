"""Backend components for the terminal emulator (PTY and OSC parser)."""

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
from typing import Callable, Optional

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


def _strip_ansi(data: bytes) -> bytes:
    return _ANSI_ESCAPE_RE.sub(b"", data)


@dataclass(slots=True)
class CommandEndEvent:
    exit_code: int
    user: str
    cwd: str
    output: str


class OscParser:
    """Parses standard output to extract shell integration sequences (OSC 9998/9999)."""

    def __init__(self, on_command_start: Callable[[], None]):
        self._on_command_start = on_command_start
        self._raw_buf = bytearray()
        self._output_buf = bytearray()
        self._capturing = False
        self._command_started = False

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    @property
    def is_command_started(self) -> bool:
        return self._command_started

    def feed(self, data: bytes) -> tuple[bytes, list[CommandEndEvent]]:
        self._raw_buf.extend(data)
        buf = bytes(self._raw_buf)
        pyte_data = bytearray()
        pos = 0
        events = []

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
                self._on_command_start()
            elif osc_body.startswith(b"9999;"):
                self._capturing = False
                if self._command_started:
                    event = self._handle_command_end(osc_body[5:])
                    if event is not None:
                        events.append(event)
                    self._command_started = False
                self._output_buf = bytearray()
            else:
                seq = b"\x1b]" + osc_body + raw_term
                pyte_data.extend(seq)
                if self._capturing:
                    self._output_buf.extend(seq)
        else:
            self._raw_buf = bytearray()

        return bytes(pyte_data), events

    def _handle_command_end(self, metadata: bytes) -> Optional[CommandEndEvent]:
        text = metadata.decode("utf-8", errors="replace")
        parts = text.split("\x1f", 2)
        if len(parts) < 3:
            return None
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
        return CommandEndEvent(
            exit_code=exit_code,
            user=user,
            cwd=cwd,
            output=clean_out
        )


class PtyManager:
    """Manages the lifecycle and I/O of the background shell process."""

    def __init__(self, shell: str, cols: int, rows: int, on_data: Callable[[bytes], None], on_exit: Callable[[], None]):
        self._shell = shell
        self._on_data = on_data
        self._on_exit = on_exit
        self._fd: Optional[int] = None
        self._pid: Optional[int] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._start(cols, rows)

    def _start(self, cols: int, rows: int) -> None:
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

        self.resize(cols, rows)
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def resize(self, cols: int, rows: int) -> None:
        if self._fd is not None:
            fcntl.ioctl(
                self._fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )

    def write(self, data: bytes) -> None:
        if self._fd is not None and self._running:
            try:
                os.write(self._fd, data)
            except OSError:
                pass

    def write_byte(self, byte: bytes) -> None:
        self.write(byte)

    def close(self) -> None:
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

    def _read_loop(self) -> None:
        while self._running:
            try:
                if self._fd is None:
                    break
                r, _, _ = select.select([self._fd], [], [], 0.05)
                if r:
                    data = os.read(self._fd, 8192)
                    if not data:
                        break
                    self._on_data(data)
            except OSError:
                break
        self._running = False
        self._on_exit()

    @property
    def is_running(self) -> bool:
        return self._running
