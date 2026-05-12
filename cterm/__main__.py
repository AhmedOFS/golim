#!/usr/bin/env python3
"""cterm - Main entry point"""
import argparse
import shutil
import subprocess
import sys
import time
import logging
from pathlib import Path

# --- ASSUMED IMPORTS ---
from . import __version__
from .config import Config
from .llm import  chat_with_tools 

# We must import get_socket_path from the server module to know where to check
# NOTE: The actual location must be correct for your project structure (e.g., .cterm_server)
# Assuming a file named cterm_server.py in the same package:
def get_socket_path() -> Path:
    """Returns the UDS path based on the current user (Mock implementation)."""
    # This must match the implementation in cterm_server.py
    import os
    return Path(f"/tmp/cterm_mcp_{os.getlogin()}.sock")
# -----------------------

# Set up logging for clarity in the process
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

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
        logging.info(f"Service '{service_name}' successfully requested to start.")
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


## Command Helpers

def run_cmd(binary: str, *args, timeout: float = 3.0) -> str | None:
    """Run a command and return stdout/stderr."""
    try:
        result = subprocess.run(
            [binary] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return (result.stdout or result.stderr).strip() or None
    except Exception:
        return None

def detect_ollama(binary: str = "ollama") -> tuple[bool, str | None]:
    """Returns (installed, version)."""
    if not shutil.which(binary):
        return False, None
    
    for args in (["--version"], ["version"], ["--help"]):
        output = run_cmd(binary, *args)
        if output:
            return True, output.splitlines()[0]
    
    return True, None


def get_models(binary: str) -> list[str]:
    """Get list of installed models."""
    for cmd in (["list"], ["models"]):
        output = run_cmd(binary, *cmd)
        if not output:
            continue
        
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        # Remove separator and header lines
        lines = [l for l in lines if not all(c in "-= " for c in l)]
        header_words = {"name", "model", "id", "size", "modified", "version"}
        if lines and any(w in lines[0].lower() for w in header_words):
            lines = lines[1:]
        
        # Extract first column
        models = []
        for line in lines:
            parts = line.split()
            if parts and parts[0].lower() not in header_words:
                models.append(parts[0])
        
        return models
    
    return []


def select_model(models: list[str], saved: str | None) -> str | None:
    """Display models and get user selection."""
    if not models:
        print("No models detected")
        return None
    
    print("Available models:")
    for i, name in enumerate(models, 1):
        print(f"  {i}. {name}")
    
    if saved and saved in models:
        print(f"Current saved model: {saved}")
    
    try:
        choice = input("Select model by number or name [1]: ").strip() or "1"
    except (KeyboardInterrupt, EOFError):
        print()
        return None
    
    # By number
    if choice.isdigit():
        idx = int(choice) - 1
        return models[idx] if 0 <= idx < len(models) else None
    
    # By name (exact or substring)
    exact = [m for m in models if m == choice]
    if exact:
        return exact[0]
    
    matches = [m for m in models if choice in m]
    return matches[0] if matches else None


def select_optional_model(models: list[str], saved: str | None, label: str) -> str | None:
    """Select an optional model, keeping or clearing an existing value."""
    if not models:
        print("No models detected")
        return None

    if saved and saved in models:
        print(f"Current saved {label}: {saved}")
        prompt = f"Select {label} by number or name [Enter to keep, 'none' to clear]: "
    else:
        prompt = f"Select optional {label} by number or name [Enter to skip]: "

    try:
        choice = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return saved

    if not choice:
        return saved if saved and saved in models else None

    if choice.lower() in {"none", "clear", "skip"}:
        return None

    if choice.isdigit():
        idx = int(choice) - 1
        return models[idx] if 0 <= idx < len(models) else None

    exact = [m for m in models if m == choice]
    if exact:
        return exact[0]

    matches = [m for m in models if choice in m]
    return matches[0] if matches else None


## Main Commands

def init_command(binary: str = "ollama") -> int:
    """Initialize cterm by detecting Ollama and selecting a model."""
    # Check installation
    installed, version = detect_ollama(binary)
    
    if not installed:
        print(f"Error: {binary} is not installed")
        print("Please install Ollama from https://ollama.ai")
        return 1
    
    print(f"✓ {binary} is installed: {version or 'version unknown'}")
    
    # Load config
    config = Config()
    
    # Update version if needed
    if version:
        config.set("ollama_version", version)
    
    # Model selection
    models = get_models(binary)
    if not models:
        print("\nNo models found. Please install a model first:")
        print(f"  {binary} pull llama2")
        return 1
    
    print()
    selected = select_model(models, config.selected_model)
    if selected:
        config.set(Config.SELECTED_MODEL, selected)
        print(f"\n✓ Selected model: {selected}")
        small_model = select_optional_model(models, config.small_model, "small model")
        if small_model:
            config.set(Config.SMALL_MODEL, small_model)
            print(f"✓ Selected small model: {small_model}")
        else:
            config.unset(Config.SMALL_MODEL)
            print("✓ No small model configured")
        print("\nYou can now use cterm:")
        print('  cterm "Hello, how are you?"')
        return 0
    else:
        print("\nNo model selected")
        return 1


def chat_command(message: str, binary: str = "ollama", debug: bool = False) -> int:
    """Send a message to the configured model."""
    config = Config()
    model = config.selected_model
    small_model = config.small_model
    
    if not model:
        print("Error: No model configured")
        print("Run 'cterm -i' to initialize")
        return 1
    
    # Check if ollama is still installed
    if not shutil.which(binary):
        print(f"Error: {binary} is not installed")
        return 1
    
    # --- SERVER START/CHECK LOGIC ---
    # Ensure the UDS server daemon is running before attempting a tool-enabled chat
    if not ensure_server_running():
        # The helper function already printed an error message
        return 1
    # ------------------------------

    try:
        # Assuming chat_with_tools connects to the UDS defined by get_socket_path()
        response = chat_with_tools(model, message, binary, small_model=small_model, debug=debug)
        print(response)
        return 0
    except KeyboardInterrupt:
        print("\n\nInterrupted")
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Main entry point for cterm CLI."""
    parser = argparse.ArgumentParser(
        prog="cterm",
        description="Terminal interface for Ollama LLMs"
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show version and exit"
    )
    parser.add_argument(
        "-i", "--init",
        action="store_true",
        help="initialize cterm (detect Ollama and select model)"
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
    
    args = parser.parse_args(argv or sys.argv[1:])
    
    if args.version:
        print(f"cterm {__version__}")
        return 0
    
    if args.init:
        return init_command(args.binary)
    
    # Chat mode
    if not args.message:
        parser.print_help()
        return 1
    
    message = " ".join(args.message)
    return chat_command(message, args.binary, debug=args.debug)


if __name__ == "__main__":
    # Ensure that running this file directly raises the intended error, 
    # as it's meant to be imported as a module in a package structure.
    raise SystemExit(main())
