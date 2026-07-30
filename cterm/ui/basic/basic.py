"""Thin terminal UI layer for cterm client output."""

import sys
import logging
from typing import Any, Protocol

from cterm.core.agent_ui import AgentUI
from cterm.config import Config, get_config
from cterm.core.runtime import Runtime
from cterm.core.utils import _clip_label
from cterm.logger import log_diagnostic_section
from cterm.ui.basic.spinner import Spinner


class _SpinnerLogHandler(logging.Handler):
    def __init__(self, ui):
        super().__init__()
        self._ui = ui

    def emit(self, record):
        try:
            self._ui._write_output(self.format(record))
        except Exception:
            self.handleError(record)


class TerminalUI(AgentUI):
    def __init__(
        self,
        *,
        config: Config | None = None,
        model: str | None = None,
        binary: str = "ollama",
        small_model: str | None = None,
        debug: bool = False,
    ):
        self._config = config or get_config()
        self._model = model
        self._binary = binary
        self._small_model = small_model
        self._debug = debug
        self._spinner = None
        self._thinking_live = False
        self._debug_handler = None
        self._debug_previous_handlers = None
        self._debug_previous_level = None

    def run(self, message: str) -> str:
        with Runtime(
            config=self._config,
            model=self._model,
            binary=self._binary,
            small_model=self._small_model,
            debug=self._debug,
        ) as runtime:
            return runtime.run(message)

    def update_spinner(self, message):
        if self._spinner is None:
            self._spinner = Spinner(message, reserve_above=True)
            self._spinner.start()
        else:
            self._spinner.update_message(message)

    def stop_spinner(self):
        if self._spinner:
            self._spinner.stop()
            self._spinner = None

    def enable_debug_logging(self):
        if self._debug_handler is not None:
            return

        root = logging.getLogger()
        handler = _SpinnerLogHandler(self)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        self._debug_previous_handlers = [
            existing for existing in root.handlers
            if not getattr(existing, "_cterm_run_log", False)
        ]
        self._debug_previous_level = root.level
        for existing in self._debug_previous_handlers:
            root.removeHandler(existing)
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self._debug_handler = handler

    def disable_debug_logging(self):
        if self._debug_handler is None:
            return

        root = logging.getLogger()
        root.removeHandler(self._debug_handler)
        for previous in self._debug_previous_handlers or []:
            root.addHandler(previous)
        if self._debug_previous_level is not None:
            root.setLevel(self._debug_previous_level)
        self._debug_handler = None
        self._debug_previous_handlers = None
        self._debug_previous_level = None

    def log_tool_call(self, tool_name, args):
        log_diagnostic_section(f"tool_call {tool_name}", args or {})

    def log_tool_result(self, tool_name, result):
        log_diagnostic_section(f"tool_result {tool_name}", result)

    def log_tool_output(self, tool_name, fd, line, end="\n"):
        log_diagnostic_section(
            f"tool_output {tool_name} {fd}",
            f"{line}{end}",
        )

    def log_thinking_trace(self, text):
        log_diagnostic_section("thinking_trace", str(text or ""))

    def log_summary(self, text):
        log_diagnostic_section("summary", str(text or ""))

    def _write_output(self, text, end="\n"):
        if self._spinner:
            self._spinner.write_above(str(text), end=end)
            return
        sys.stderr.write(str(text) + end)
        sys.stderr.flush()

    def message(self, text):
        self._write_output(text)

    def thinking_trace_delta(self, text):
        if not text:
            return
        if not self._thinking_live and self._spinner:
            self.stop_spinner()
        prefix = "THINKING: " if not self._thinking_live else ""
        self._thinking_live = True
        self._write_output(f"\033[38;5;248m{prefix}{text}\033[0m", end="")

    def thinking_trace_complete(self, text):
        if not text:
            return
        if self._thinking_live:
            self._write_output("")
        self._thinking_live = False


    def tool_call(self, tool_name, args):
        if tool_name == "bash":
            label = _clip_label(args.get("command", ""), 120)
            self._write_output(f"$ {label}")
        elif tool_name in ("exec_python", "exec"):
            code = args.get("code") or args.get("script") or args.get("source") or ""
            first_line = code.strip().split("\n")[0] if code else ""
            self._write_output(f"exec: {_clip_label(first_line, 80)}")
        else:
            formatted = self._format_tool_call(tool_name, args)
            self._write_output(formatted)

    def handle_tool_output(self, fd=None, line="", end="\n", result=None):
        if result is not None:
            formatted = self._format_tool_result(result)
            if formatted:
                self._write_output(formatted)
            self._write_output("")
        elif fd is not None and self._spinner:
            output = f"\033[33m{line}\033[0m" if fd == "stderr" else line
            self._spinner.write_above(output, end=end)

    def handle_shell_result_output(self, result):
        if not isinstance(result, dict):
            return

        for entry in result.get("results", []):
            if not isinstance(entry, dict):
                continue
            stdout = entry.get("stdout")
            if stdout:
                stdout = str(stdout)
                self._write_output(stdout, end="" if stdout.endswith("\n") else "\n")
            stderr = entry.get("stderr")
            if stderr:
                text = str(stderr)
                self._write_output(
                    f"\033[33m{text}\033[0m",
                    end="" if text.endswith("\n") else "\n",
                )

    def approve_privileged_binary(self, binary):
    
        prompt = f"Allow sudo access for {binary}? [Y/N] "
        try:
            with open("/dev/tty", "r+", encoding="utf-8") as tty:
                tty.write(prompt)
                tty.flush()
                answer = tty.readline()
        except OSError:
            try:
                answer = input(prompt)
            except (EOFError, KeyboardInterrupt):
                return False
        return answer.strip().lower() in {"y", "yes"}
    
    def _show_python(self, code):
        sys.stderr.write("\033[38;5;248m" + "-" * 40 + "\033[0m\n")
        for line in code.split("\n"):
            sys.stderr.write(f"\033[33m{line}\033[0m\n")
        sys.stderr.write("\033[38;5;248m" + "-" * 40 + "\033[0m\n")

    def show_python_code(self, code):
        if self._spinner:
            self._spinner.stop()
            self._spinner = None
        sys.stderr.write("\n\033[1mPython code in bash command:\033[0m\n")
        self._show_python(code)
        sys.stderr.flush()

    def approve_python_code(self, code):
        if self._spinner:
            self._spinner.stop()
            self._spinner = None

        sys.stderr.write("\n\033[1mPython code requires approval:\033[0m\n")
        self._show_python(code)

        prompt = "Execute this Python code? [Y/N] "
        try:
            with open("/dev/tty", "r+", encoding="utf-8") as tty:
                tty.write(prompt)
                tty.flush()
                answer = tty.readline()
        except OSError:
            try:
                answer = input(prompt)
            except (EOFError, KeyboardInterrupt):
                return False
        return answer.strip().lower() in {"y", "yes"}

    def _format_tool_call(self, tool_name, args):
        if tool_name == "finder":
            return f"finder: {args.get('pattern', '')} in {args.get('path', '')}"
        if tool_name == "read_file":
            path = args.get("path", "")
            page = args.get("page", 1)
            s = f"read_file: {path}"
            if page > 1:
                s += f" (page {page})"
            return s
        if tool_name == "write_file":
            path = args.get("path", "")
            mode = args.get("mode", "overwrite")
            s = f"write_file: {path}"
            if mode != "overwrite":
                s += f" [{mode}]"
            return s
        if tool_name == "websearch":
            return f"websearch: {_clip_label(args.get('query', ''), 100)}"
        if tool_name == "system_info":
            return "system_info"
        return f"{tool_name}: {str(list(args.keys()))}" if args else tool_name

    def _format_tool_result(self, result):
        ok = result.get("ok")
        if ok is True:
            if "matches" in result:
                n = result.get("total", 0)
                truncated = result.get("truncated", False)
                s = f"\033[32m✓\033[0m {n} matches"
                if truncated:
                    s += " (truncated)"
                return s
            if "content" in result:
                path = result.get("path", "")
                page = result.get("page", 1)
                total = result.get("total_pages", 1)
                return f"\033[32m✓\033[0m {path} (pg {page}/{total})"
            if "bytes_written" in result:
                return f"\033[32m✓\033[0m {result.get('path', '')} ({result['bytes_written']} bytes)"
            if "stdout" in result:
                preview = _clip_label(result.get("stdout", "").strip(), 80)
                return f"\033[32m✓\033[0m {preview}" if preview else "\033[32m✓\033[0m done"
            if "text" in result:
                preview = _clip_label(result.get("text", "").strip(), 80)
                return f"\033[32m✓\033[0m {preview}" if preview else "\033[32m✓\033[0m done"
            return "\033[32m✓\033[0m ok"
        if ok is False:
            error = result.get("error", "unknown error")
            return f"\033[31m✗\033[0m {error}"
        return None
