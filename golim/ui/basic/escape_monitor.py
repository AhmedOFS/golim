"""Escape-key monitoring for the non-Textual terminal UI."""

from __future__ import annotations

import contextlib
import os
import select
import threading

try:
    import termios
except ImportError:  # pragma: no cover - the supported Unix platforms have it
    termios = None


class EscapeMonitor:
    """Read bare Escape presses from the controlling terminal.

    The monitor owns a separate ``/dev/tty`` descriptor so model/tool work can
    continue while the main thread is blocked in a runtime call. Terminal
    settings are restored on every exit path. Arrow-key escape sequences are
    consumed without being mistaken for an interrupt.
    """

    _ESCAPE_LOOKAHEAD_SECONDS = 0.03

    def __init__(self, on_escape):
        self._on_escape = on_escape
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._read_lock = threading.Lock()
        self._fd = None
        self._saved_attributes = None
        self._raw_attributes = None
        self._thread = None

    def start(self) -> None:
        if termios is None:
            return
        fd = None
        try:
            fd = os.open("/dev/tty", os.O_RDONLY | os.O_NONBLOCK)
            saved = termios.tcgetattr(fd)
            raw = saved.copy()
            raw[3] &= ~(termios.ICANON | termios.ECHO)
            raw[6][termios.VMIN] = 0
            raw[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, raw)
        except (OSError, termios.error):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return

        self._fd = fd
        self._saved_attributes = saved
        self._raw_attributes = raw
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        with self._read_lock:
            self._restore_terminal()
            self._thread = None

    @contextlib.contextmanager
    def paused(self):
        """Temporarily return the terminal to canonical mode for prompts."""
        if self._fd is None or self._saved_attributes is None:
            yield
            return

        self._paused.set()
        with self._read_lock:
            try:
                termios.tcsetattr(self._fd, termios.TCSANOW, self._saved_attributes)
                yield
            finally:
                if self._fd is not None and self._raw_attributes is not None:
                    try:
                        termios.tcsetattr(self._fd, termios.TCSANOW, self._raw_attributes)
                    except (OSError, termios.error):
                        pass
                self._paused.clear()

    def _restore_terminal(self) -> None:
        if self._fd is None:
            return
        if self._saved_attributes is not None and termios is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSANOW, self._saved_attributes)
            except (OSError, termios.error):
                pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.05)
                continue
            with self._read_lock:
                fd = self._fd
                if fd is None:
                    return
                try:
                    ready, _, _ = select.select([fd], [], [], 0.1)
                    if not ready:
                        continue
                    byte = os.read(fd, 1)
                except (OSError, ValueError):
                    return
                if byte == b"\x1b":
                    self._handle_escape()

    def _handle_escape(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            ready, _, _ = select.select([fd], [], [], self._ESCAPE_LOOKAHEAD_SECONDS)
            if not ready:
                self._on_escape()
                return
            following = os.read(fd, 1)
        except (OSError, ValueError):
            return

        if following in (b"[", b"O"):
            # Consume the rest of a CSI/SS3 key sequence, e.g. arrows, so
            # only a bare Escape is treated as an interrupt.
            while not self._stop.is_set():
                try:
                    ready, _, _ = select.select([fd], [], [], 0.1)
                    if not ready:
                        return
                    byte = os.read(fd, 1)
                except (OSError, ValueError):
                    return
                if byte and 0x40 <= byte[0] <= 0x7E:
                    return
            return

        if following == b"\x1b":
            # Two quick bare Escape presses are the soft-then-hard pair.
            self._on_escape()
            self._on_escape()
            return

        # A bare Escape followed by another ordinary byte is still an
        # Escape press; the extra byte is consumed while no prompt is active.
        self._on_escape()
