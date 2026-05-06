
import os
from pathlib import Path
import sys
import threading
import time


class Spinner:
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, message="Thinking"):
        self.message = message
        self.running = False
        self.thread = None
        self._idx = 0

    def _spin(self):
        while self.running:
            sys.stderr.write(f"\r{self.FRAMES[self._idx % len(self.FRAMES)]} {self.message}...")
            sys.stderr.flush()
            self._idx += 1
            time.sleep(0.1)
        sys.stderr.write("\r" + " " * (len(self.message) + 10) + "\r")
        sys.stderr.flush()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)




def _indent(text, prefix="      "):
    return "\n".join(prefix + line for line in text.splitlines())


def get_socket_path() -> Path:
    return Path(f"/tmp/cterm_mcp_{os.getlogin()}.sock")

