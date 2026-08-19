#!/usr/bin/env python3
"""openterm - Main entry point"""
import argparse
import json
import re
import sys
from .version import __version__
from .config.app_home import resolve_app_home
from .config import ConfigSchemaError, get_config, init_config
from .config.utils import resolve_provider_settings
from .logger import setup_root_logger, start_run_logging
from .core.agent_events import active_agent_events_handler
from .core.runtime import Runtime
from .ui.basic.basic import TerminalUI
from .ui.tui.app.transcript_writer import TranscriptWriter

def _setup_session_log():
    transcript = TranscriptWriter()
    _ansi_strip = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    real_stderr = sys.stderr

    class _Tee:
        def write(self, text):
            real_stderr.write(text)
            real_stderr.flush()
            if "\n" in text:
                transcript.write(_ansi_strip.sub("", text), end="")
        def flush(self):
            real_stderr.flush()
            transcript.flush()
        def isatty(self):
            return real_stderr.isatty()

    sys.stderr = _Tee()
    return transcript, transcript.path, real_stderr

def chat_command(message: str, binary: str = "ollama") -> int:
    """Send a message to the configured model."""
    init_config()
    error, model, small_model = _resolve_chat_settings(binary)
    if error:
        print(error)
        return 1

    transcript, log_path, real_stderr = _setup_session_log()
    run_logging = start_run_logging(log_path)
    try:
        ui = TerminalUI(model=model, binary=binary, small_model=small_model)
        token = active_agent_events_handler.set(ui)
        try:
            response = ui.run(message)
        finally:
            active_agent_events_handler.reset(token)
        response_text = str(response.get("LLM_response", ""))
        transcript.write(f"\nresponse: {response_text}")
        print(response_text)
        return 0 if response.get("ok") else 1
    except KeyboardInterrupt:
        print("\n\nInterrupted")
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        run_logging.close()
        sys.stderr = real_stderr
        transcript.close()
        print(f"\n\033[2m(log: {log_path})\033[0m", file=sys.stderr)


def _resolve_chat_settings(binary: str = "ollama") -> tuple[str | None, str | None, str | None]:
    error, model, small_model, _ = resolve_provider_settings(get_config(), binary)
    return error, model, small_model


def tui_command(binary: str = "ollama") -> int:
    """Open the default Textual interface."""
    init_config()
    from .ui.tui.app.app_tui import OpentermApp

    config = get_config()
    error, model, small_model, model_label = resolve_provider_settings(config, binary)

    try:
        result = OpentermApp(
            model_label,
            config=config,
            model=model,
            binary=binary,
            small_model=small_model,
            runtime_error=error,
        ).run()
        return int(result or 0)
    except ImportError as exc:
        print(f"Error: Textual is required for the default UI: {exc}")
        return 1


def run_command(message: str, binary: str = "ollama") -> int:
    return chat_command(message, binary)


def run_tui_command(binary: str = "ollama") -> int:
    return tui_command(binary)


def _print_config_schema_error(exc: ConfigSchemaError) -> int:
    """Report a broken config file and point at the offending location."""
    cause = exc.__cause__
    detail = ""
    if isinstance(cause, json.JSONDecodeError):
        detail = f" (line {cause.lineno}, column {cause.colno})"
    print(f"Error: {exc}{detail}", file=sys.stderr)
    print("Fix or remove the config file, then run 'openterm -i' to reconfigure it.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Main entry point for openterm CLI."""
    resolve_app_home()
    parser = argparse.ArgumentParser(
        prog="openterm",
        description="Terminal interface for LLMs (Ollama, OpenRouter, OpenAI-compatible)"
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show version and exit"
    )
    parser.add_argument(
        "-i", "--init",
        action="store_true",
        help="initialize openterm (opens the configuration TUI)"
    )
    parser.add_argument(
        "-init",
        action="store_true",
        dest="init",
        help="initialize openterm (alias for --init, opens the configuration TUI)"
    )
    parser.add_argument(
        "--binary",
        default="ollama",
        help="ollama binary name or path (default: ollama)"
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="message to send to the LLM"
    )
    
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    setup_root_logger()

    if args.version:
        print(f"openterm {__version__}")
        return 0

    try:
        if args.init:
            init_config()
            from .ui.tui.config.config_tui import init_command_tui

            return init_command_tui(args.binary)

        # Chat mode
        if not args.message:
            return tui_command(args.binary)

        message = " ".join(args.message)
        return chat_command(message, args.binary)
    except ConfigSchemaError as exc:
        return _print_config_schema_error(exc)


if __name__ == "__main__":
    # Ensure that running this file directly raises the intended error, 
    # as it's meant to be imported as a module in a package structure.
    raise SystemExit(main())
