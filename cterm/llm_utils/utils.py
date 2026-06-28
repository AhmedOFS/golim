
import asyncio
import os
from pathlib import Path
import sys
import threading
import time


class Spinner:
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, message="Thinking", reserve_above=False):
        self.message = message
        self.reserve_above = reserve_above
        self.running = False
        self.thread = None
        self._idx = 0
        self._lock = threading.Lock()
        self._is_tty = sys.stderr.isatty()

    def _clear_line(self):
        sys.stderr.write("\r\033[K" if self._is_tty else "\r")

    def _draw_locked(self):
        self._clear_line()
        sys.stderr.write(f"{self.FRAMES[self._idx % len(self.FRAMES)]} {self.message}...")
        sys.stderr.flush()

    def _spin(self):
        while self.running:
            with self._lock:
                self._draw_locked()
            self._idx += 1
            time.sleep(0.1)
        with self._lock:
            self._clear_line()
            sys.stderr.flush()

    def start(self):
        if self.reserve_above and self._is_tty:
            sys.stderr.write("\n")
            sys.stderr.flush()
        self.running = True
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)

    def update_message(self, message):
        with self._lock:
            self.message = message

    def write_above(self, text, end="\n"):
        if not self.reserve_above or not self._is_tty:
            sys.stderr.write(text + end)
            sys.stderr.flush()
            return

        with self._lock:
            self._clear_line()
            sys.stderr.write("\033[1A\r\033[K")
            if end == "\r":
                sys.stderr.write(text)
                sys.stderr.write("\033[1B\r")
            else:
                sys.stderr.write(text + "\n")
            self._draw_locked()
            sys.stderr.flush()

import concurrent.futures

def _run_async(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


def _indent(text, prefix="      "):
    return "\n".join(prefix + line for line in text.splitlines())


def _clip_label(text, max_chars=80):
    line = text.splitlines()[0] if text else (text or "")
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + "..."


def get_socket_path() -> Path:
    return Path(f"/tmp/cterm_mcp_{os.getlogin()}.sock")
