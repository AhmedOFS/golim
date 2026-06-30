"""Thin terminal UI layer for cterm client output."""

import sys

from cterm.llm_utils.utils import Spinner, _clip_label


class TerminalUI:
    def __init__(self):
        self._spinner = None

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

    def tool_call(self, tool_name, args):
        if tool_name == "bash":
            label = _clip_label(args.get("command", ""), 120)
            sys.stderr.write(f"$ {label}\n")
        elif tool_name in ("exec_python", "exec"):
            code = args.get("code") or args.get("script") or args.get("source") or ""
            first_line = code.strip().split("\n")[0] if code else ""
            sys.stderr.write(f"exec: {_clip_label(first_line, 80)}\n")
        else:
            formatted = self._format_tool_call(tool_name, args)
            sys.stderr.write(f"{formatted}\n")
        sys.stderr.flush()

    def handle_tool_output(self, fd=None, line="", end="\n", result=None):
        if result is not None:
            formatted = self._format_tool_result(result)
            if formatted:
                sys.stderr.write(f"{formatted}\n")
                sys.stderr.flush()
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
                sys.stderr.write(str(stdout))
                if not str(stdout).endswith("\n"):
                    sys.stderr.write("\n")
            stderr = entry.get("stderr")
            if stderr:
                text = str(stderr)
                sys.stderr.write(f"\033[33m{text}\033[0m")
                if not text.endswith("\n"):
                    sys.stderr.write("\n")
        sys.stderr.flush()

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
