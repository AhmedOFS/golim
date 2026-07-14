#!/usr/bin/env python3
"""cterm - Main entry point"""
import argparse
import datetime
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
import os
from . import __version__
from .config import Config
from .ui.config_tui import init_command_tui
from .logger import setup_root_logger
from .llm import  chat_with_tools 

# NOTE: The actual location must be correct for your project structure (e.g., .cterm_server)
# Assuming a file named cterm_server.py in the same package:
def get_socket_path() -> Path:
    """Returns the UDS path based on the current user (Mock implementation)."""
    # This must match the implementation in cterm_server.py
    import os
    return Path(f"/tmp/cterm_mcp_{os.getlogin()}.sock")

## Server Management Helper
# This function encapsulates the logic to ensure the background server is running.

def ensure_server_running(service_name: str = "cterm-mcp.service", timeout: float = 10.0) -> bool:
    """
    Checks if the systemd user service is active. If not, starts it and
    waits for the UDS file to appear before returning.

    :param service_name: The name of the systemd user service.
    :param timeout: Maximum time to wait for the server to start (UDS to appear).
    :return: True if the server is running or successfully started, False otherwise.
    """
    socket_path = get_socket_path()
    start_time = time.time()
    
    # Check 1: Is the UDS socket already present? (Server is likely already running)
    if socket_path.exists():
        logging.info("Server UDS found. Assuming server is active.")
        return True

    # Check 2: Service is not running, so start it.
    logging.info(f"Server UDS not found at {socket_path}. Attempting to start user service '{service_name}'...")
    
    try:
        # Use systemctl --user to manage the user service
        subprocess.run(
            ["systemctl", "--user", "start", service_name],
            check=True,
            capture_output=True,
            text=True,
            timeout=5
        )
        logging.info(f"Attempting to start {service_name}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to start service '{service_name}' (systemctl error):\n{e.stderr.strip()}")
        return False
    except FileNotFoundError:
        logging.error("The 'systemctl' command was not found. Cannot manage the user service.")
        return False
    except subprocess.TimeoutExpired:
        logging.warning("Timeout while trying to execute 'systemctl --user start'.")
        
    # Check 3: Wait for the UDS file to appear
    logging.info(f"Waiting for UDS file to appear at {socket_path}...")
    while time.time() - start_time < timeout:
        if socket_path.exists():
            logging.info("Server is up (UDS file found).")
            # Give the server a small moment to fully initialize its listener socket
            time.sleep(0.1) 
            return True
        time.sleep(0.2) # Polling interval

    logging.error(f"Timeout: Server failed to start and create UDS at {socket_path} within {timeout}s.")
    return False


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
    config = Config()
    model = config.selected_model
    small_model = config.small_model
    provider = config.api_provider



    if provider == "ollama":
        if not shutil.which(binary):
            print(f"Error: {binary} is not installed")
            return 1
        else:
              model = config.selected_model
              small_model = config.small_model
    elif provider == "openrouter":
        if not config.openrouter_api_key:
            print("Error: OpenRouter API key not configured")
            print("Run 'cterm -i' to set it up")
            return 1
        else:
              model = config.openrouter_model
              small_model = config.openrouter_small_model
    elif provider == "llamacpp":
        if not config.llamacpp_server_url:
            print("Error: llama.cpp server URL not configured")
            print("Run 'cterm -i' to set it up")
            return 1
        else:
              model = config.llamacpp_model
              small_model = config.llamacpp_small_model
    if not model:
        print("Error: No model configured")
        print("Run 'cterm -i' to initialize")
        return 1
    # Ensure the UDS server daemon is running for tool execution
    if not ensure_server_running():
        return 1

    log_file, log_path, real_stderr = _setup_session_log()
    try:
        response = chat_with_tools(model, message, binary, small_model=small_model, debug=debug)
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

    if provider == "ollama":
        if not shutil.which(binary):
            return f"Error: {binary} is not installed", None, None
        model = config.selected_model
        small_model = config.small_model
    elif provider == "openrouter":
        if not config.openrouter_api_key:
            return "Error: OpenRouter API key not configured\nRun 'cterm -i' to set it up", None, None
        model = config.openrouter_model
        small_model = config.openrouter_small_model
    elif provider == "llamacpp":
        if not config.llamacpp_server_url:
            return "Error: llama.cpp server URL not configured\nRun 'cterm -i' to set it up", None, None
        model = config.llamacpp_model
        small_model = config.llamacpp_small_model

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
    from .ui.tui import ChatResult, CtermApp

    config = Config()
    provider = config.api_provider
    if provider == "openrouter":
        model_label = config.openrouter_model or "OpenRouter"
    elif provider == "llamacpp":
        model_label = config.llamacpp_model or "llama.cpp"
    else:
        model_label = config.selected_model or "Ollama"

    def _runner(message, ui):
        error, model, small_model = _resolve_chat_settings(binary)
        log_file, log_path = _create_tui_log()
        if hasattr(ui, "set_log_file"):
            ui.set_log_file(log_file)
        try:
            log_file.write(f"prompt: {message}\n")
            if error:
                log_file.write(f"error: {error}\n")
                return ChatResult(False, error, str(log_path))
            if not ensure_server_running():
                error_text = "Error: cterm tool server could not be started"
                log_file.write(f"error: {error_text}\n")
                return ChatResult(False, error_text, str(log_path))

            response = chat_with_tools(
                model,
                message,
                binary,
                small_model=small_model,
                debug=debug,
                ui=ui,
            )
            log_file.write(f"\nresponse: {response}\n")
            return ChatResult(True, response, str(log_path))
        finally:
            log_file.flush()
            log_file.close()

    try:
        result = CtermApp(_runner, model_label, debug=debug).run()
        return int(result or 0)
    except ImportError as exc:
        print(f"Error: Textual is required for the default UI: {exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    """Main entry point for cterm CLI."""
    parser = argparse.ArgumentParser(
        prog="cterm",
        description="Terminal interface for LLMs (Ollama, OpenRouter, llama.cpp)"
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
