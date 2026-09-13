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
        self._above_line = ""
        self._replace_above_line = False

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
        self.running = True
        # A non-TTY has no cursor to animate. More importantly, emitting
        # carriage-return spinner frames there would race with streamed
        # thinking and tool output, which intentionally use raw line endings.
        if not self._is_tty:
            return
        if self.reserve_above:
            sys.stderr.write("\n")
            sys.stderr.flush()
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
        text = str(text)
        if not self.reserve_above or not self._is_tty:
            sys.stderr.write(text + end)
            sys.stderr.flush()
            return

        with self._lock:
            # A provider may deliver several thinking lines in one delta.
            # Commit each embedded newline separately so the spinner remains
            # immediately below the output area.
            parts = text.split("\n")
            for index, part in enumerate(parts):
                is_last = index == len(parts) - 1
                if not is_last:
                    self._write_above_locked(part, "\n")
                elif part or end:
                    self._write_above_locked(part, end)

    def _write_above_locked(self, text, end):
        self._clear_line()
        sys.stderr.write("\033[1A\r\033[K")

        if self._replace_above_line:
            self._above_line = ""

        combined = self._above_line + str(text)
        if end == "\r":
            rendered = combined
            self._above_line = ""
            self._replace_above_line = True
        else:
            rendered = combined + end
            last_newline = rendered.rfind("\n")
            self._above_line = rendered[last_newline + 1:] if last_newline >= 0 else rendered
            self._replace_above_line = False

        sys.stderr.write(rendered)
        if rendered.endswith("\n"):
            # Keep a fresh output line above the spinner so the next
            # write does not erase the line just completed.
            sys.stderr.write("\033[1L")
        sys.stderr.write("\033[1B\r")
        self._draw_locked()
        sys.stderr.flush()
