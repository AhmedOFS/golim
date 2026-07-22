#!/usr/bin/env python3
"""cterm - Main entry point"""
import argparse
import datetime
import re
import shutil
import sys
from pathlib import Path
from . import __version__
from .config import Config
from .logger import setup_root_logger
from .core.agent_ui import active_agent_ui
from .core.runtime import Runtime
from .ui.basic.basic import TerminalUI

def _setup_session_log():
    log_dir = Path.home() / "cterm" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"cterm_{timestamp}.log"
    log_file = open(log_path, "w", encoding="utf-8")
    _ansi_strip = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    real_stderr = sys.stderr

    class _Tee:
        def write(self, text):
            real_stderr.write(text)
            real_stderr.flush()
            if "\n" in text:
                log_file.write(_ansi_strip.sub("", text))
                log_file.flush()
        def flush(self):
            real_stderr.flush()
            log_file.flush()
        def isatty(self):
            return real_stderr.isatty()

    sys.stderr = _Tee()
    return log_file, log_path, real_stderr

def chat_command(message: str, binary: str = "ollama", debug: bool = False) -> int:
    """Send a message to the configured model."""
    error, model, small_model = _resolve_chat_settings(binary)
    if error:
        print(error)
        return 1

    log_file, log_path, real_stderr = _setup_session_log()
    try:
        ui = TerminalUI(model=model, binary=binary, small_model=small_model, debug=debug)
        token = active_agent_ui.set(ui)
        try:
            response = ui.run(message)
        finally:
            active_agent_ui.reset(token)
        log_file.write(f"\nresponse: {response}\n")
        log_file.flush()
        print(response)
        return 0
    except KeyboardInterrupt:
        print("\n\nInterrupted")
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        sys.stderr = real_stderr
        log_file.close()
        print(f"\n\033[2m(log: {log_path})\033[0m", file=sys.stderr)


def _resolve_chat_settings(binary: str = "ollama") -> tuple[str | None, str | None, str | None]:
    config = Config()
    model = config.selected_model
    small_model = config.small_model
    provider = config.api_provider

    if provider == Config.OLLAMA:
        if not shutil.which(binary):
            return f"Error: {binary} is not installed", None, None
        model = config.selected_model
        small_model = config.small_model
    elif provider in {Config.OPEN_ROUTER, "openrouter"}:
        if not config.openrouter_api_key:
            return "Error: OpenRouter API key not configured\nRun 'cterm -i' to set it up", None, None
        model = config.selected_model
        small_model = config.small_model
    elif provider == Config.OPENAI_COMPATIBLE:
        if not config.openai_compatible_server_url:
            return "Error: OpenAI-compatible server URL not configured\nRun 'cterm -i' to set it up", None, None
        model = config.selected_model
        small_model = config.small_model

    if not model:
        return "Error: No model configured\nRun 'cterm -i' to initialize", None, None

    return None, model, small_model


def _create_tui_log():
    log_dir = Path.home() / "cterm" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"cterm_{timestamp}.log"
    return open(log_path, "w", encoding="utf-8"), log_path


def tui_command(binary: str = "ollama", debug: bool = False) -> int:
    """Open the default Textual interface."""
    from .ui.tui.app.tui import CtermApp

    config = Config()
    provider = config.api_provider
    if provider in {Config.OPEN_ROUTER, "openrouter"}:
        model_label = config.selected_model or "OpenRouter"
    elif provider == Config.OPENAI_COMPATIBLE:
        model_label = config.selected_model or "OpenAI-compatible"
    else:
        model_label = config.selected_model or "Ollama"

    error, model, small_model = _resolve_chat_settings(binary)

    try:
        result = CtermApp(
            model_label,
            config=config,
            model=model,
            binary=binary,
            small_model=small_model,
            debug=debug,
            runtime_error=error,
            log_factory=_create_tui_log,
        ).run()
        return int(result or 0)
    except ImportError as exc:
        print(f"Error: Textual is required for the default UI: {exc}")
        return 1


def run_command(message: str, binary: str = "ollama", debug: bool = False) -> int:
    return chat_command(message, binary, debug=debug)


def run_tui_command(binary: str = "ollama", debug: bool = False) -> int:
    return tui_command(binary, debug=debug)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for cterm CLI."""
    parser = argparse.ArgumentParser(
        prog="cterm",
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
        help="initialize cterm (opens the configuration TUI)"
    )
    parser.add_argument(
        "-init",
        action="store_true",
        dest="init",
        help="initialize cterm (alias for --init, opens the configuration TUI)"
    )
    parser.add_argument(
        "--binary",
        default="ollama",
        help="ollama binary name or path (default: ollama)"
    )
    parser.add_argument(
        "-d", "--debug",
        action="store_true",
        help="print each tool call and whether it succeeded"
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="message to send to the LLM"
    )
    
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    setup_root_logger(debug=args.debug)
    
    if args.version:
        print(f"cterm {__version__}")
        return 0
    
    if args.init:
        from .ui.tui.config.config_tui import init_command_tui

        return init_command_tui(args.binary)
    
    # Chat mode
    if not args.message:
        return tui_command(args.binary, debug=args.debug)
    
    message = " ".join(args.message)
    return chat_command(message, args.binary, debug=args.debug)


if __name__ == "__main__":
    # Ensure that running this file directly raises the intended error, 
    # as it's meant to be imported as a module in a package structure.
    raise SystemExit(main())
