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


## Command Helpers

def run_cmd(binary: str, *args, timeout: float = 3.0) -> str | None:
    """Run a command and return stdout."""
    try:
        result = subprocess.run(
            [binary] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.stdout.strip() or None
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
        try:
            result = subprocess.run(
                [binary] + cmd,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            if result.returncode != 0:
                continue
            output = result.stdout.strip()
            if not output:
                continue
        except Exception:
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


def _ollama_server_running(binary: str) -> bool:
    """Check whether the Ollama server is responding."""
    try:
        result = subprocess.run(
            [binary, "list"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return result.returncode == 0
    except Exception:
        return False


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

def choose_provider(config: Config) -> str | None:
    """Let the user choose between Ollama, OpenRouter, and llama.cpp.
    Returns None on Ctrl+C."""
    print("\nAPI Provider selection:")
    print(f"  1. Ollama (local, default)")
    print(f"  2. OpenRouter (cloud, requires API key)")
    print(f"  3. llama.cpp (local, llama.cpp server)")
    default = "1" if config.api_provider == "ollama" else "2" if config.api_provider == "openrouter" else "3"
    try:
        choice = input(f"Select provider [1-3, default {default}]: ").strip() or default
    except (KeyboardInterrupt, EOFError):
        print()
        return None
    if choice == "2":
        return "openrouter"
    if choice == "3":
        return "llamacpp"
    return "ollama"


def init_openrouter(config: Config) -> int:
    """Configure cterm to use OpenRouter."""
    api_key = config.openrouter_api_key
    if api_key:
        masked = api_key[:8] + "..." if len(api_key) > 8 else "***"
        print(f"✓ OpenRouter API key: {masked}")
        try:
            change = input("Change API key? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            change = ""
        if change in ("y", "yes"):
            api_key = None

    if not api_key:
        try:
            api_key = input("Enter your OpenRouter API key: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return 1
        if not api_key:
            print("Error: API key is required")
            return 1
        config.set(Config.OPENROUTER_API_KEY, api_key)
        print("✓ API key saved")

    saved_model = config.openrouter_model
    print("\nOpenRouter model (e.g. anthropic/claude-3.5-sonnet,")
    print("  openai/gpt-4o, google/gemini-2.0-flash-001)")
    print("  See https://openrouter.ai/models for the full list.")
    prompt = f"Enter model name [{saved_model or 'anthropic/claude-3.5-sonnet'}]: "
    try:
        model = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return 1
    if not model:
        model = saved_model or "anthropic/claude-3.5-sonnet"
    config.set(Config.OPENROUTER_MODEL, model)
    #config.set(Config.SELECTED_MODEL, model)
    print(f"✓ Model: {model}")

    small = config.openrouter_small_model
    print(f"\nOptional small model for lightweight tasks (skills selection,")
    print(f"  verification). Enter to skip or 'none' to clear.")
    prompt = f"Small model [{small or 'none'}]: "
    try:
        small_choice = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        small_choice = ""
    if small_choice and small_choice.lower() not in ("none", "clear", "skip"):
        config.set(Config.OPENROUTER_SMALL_MODEL, small_choice)
        config.set(Config.SMALL_MODEL, small_choice)
        print(f"✓ Small model: {small_choice}")
    elif small_choice and small_choice.lower() in ("none", "clear"):
        config.unset(Config.OPENROUTER_SMALL_MODEL)
        config.unset(Config.SMALL_MODEL)
        print("✓ Small model cleared")
    elif small:
        print(f"✓ Small model: {small}")

    config.set(Config.API_PROVIDER, "openrouter")
    print(f"\n✓ OpenRouter configured with provider: openrouter")
    return 0


def init_ollama(config: Config, binary: str) -> int:
    """Configure cterm to use Ollama (local)."""
    installed, version = detect_ollama(binary)
    if not installed:
        print(f"Error: {binary} is not installed")
        print("Please install Ollama from https://ollama.ai")
        return 1

    print(f"✓ {binary} is installed: {version or 'version unknown'}")
    if version:
        config.set("ollama_version", version)

    ollama_host = os.environ.get("OLLAMA_HOST", "")
    if ollama_host:
        host = ollama_host.rstrip("/")
        if not host.startswith("http://") and not host.startswith("https://"):
            host = f"http://{host}"
        config.set(Config.OLLAMA_SERVER_URL, host)
        print(f"✓ Using OLLAMA_HOST: {host}")
    else:
        config.set(Config.OLLAMA_SERVER_URL, "http://localhost:11434")

    def _try_start_server():
        """Attempt to start the ollama service. Returns True on success."""
        print("Starting Ollama server...")
        try:
            subprocess.run(
                ["systemctl", "start", "ollama.service"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as e:
            print(
                f"  systemctl said: {e.stderr.strip()}"
            )
            return False
        except FileNotFoundError:
            print(
                "  systemctl not found — cannot manage the ollama service."
            )
            return False

        for _ in range(15):
            time.sleep(1)
            if _ollama_server_running(binary):
                break
        else:
            return False

        print("✓ Ollama server started")
        return True

    if not _ollama_server_running(binary):
        if _try_start_server():
            pass  # server started, continue
        else:
            while True:
                print("\nCould not connect to the Ollama server.")
                print("  1. Enter a different Ollama URL (e.g. http://192.168.1.100:11434)")
                print("  2. Go back to provider selection")
                try:
                    choice = input("Choose [1/2]: ").strip()
                except (KeyboardInterrupt, EOFError):
                    print()
                    return 1
                if choice == "2":
                    return 2
                if choice == "1":
                    try:
                        custom_url = input("Ollama URL [http://localhost:11434]: ").strip()
                    except (KeyboardInterrupt, EOFError):
                        print()
                        return 1
                    if not custom_url:
                        custom_url = "http://localhost:11434"
                    if not custom_url.startswith("http://") and not custom_url.startswith("https://"):
                        custom_url = f"http://{custom_url}"
                    config.set(Config.OLLAMA_SERVER_URL, custom_url.rstrip("/"))
                    if _ollama_server_running(binary):
                        print("✓ Connected")
                        break
                    print("Still unreachable. Try a different URL or go back.\n")

    models = get_models(binary)
    if not models:
        print("\nNo models found. Please install a model first:")
        print(f"  {binary} pull llama2")
        return 1

    selected = select_model(models, config.selected_model)
    if not selected:
        print("\nNo model selected")
        return 1

    config.set(Config.SELECTED_MODEL, selected)
    print(f"✓ Selected model: {selected}")

    small_model = select_optional_model(models, config.small_model, "small model")
    if small_model:
        config.set(Config.SMALL_MODEL, small_model)
        print(f"✓ Selected small model: {small_model}")
    else:
        config.unset(Config.SMALL_MODEL)
        print("✓ No small model configured")

    return 0


def init_llamacpp(config: Config) -> int:
    """Configure cterm to use llama.cpp server."""
    saved_url = config.llamacpp_server_url
    prompt = f"llama.cpp server URL [{saved_url}]: "
    try:
        url = input(prompt).strip() or saved_url
    except (KeyboardInterrupt, EOFError):
        print()
        return 1
    config.set(Config.LLAMACPP_SERVER_URL, url)

    saved_model = config.llamacpp_model
    prompt = f"Enter model name [{saved_model or 'default'}]: "
    try:
        model = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return 1
    if not model:
        model = saved_model or "default"
    config.set(Config.LLAMACPP_MODEL, model)
    #config.set(Config.SELECTED_MODEL, model)
    print(f"✓ Model: {model}")

    small = config.llamacpp_small_model
    print(f"\nOptional small model for lightweight tasks (skills selection,")
    print(f"  verification). Enter to skip or 'none' to clear.")
    prompt = f"Small model [{small or 'none'}]: "
    try:
        small_choice = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        small_choice = ""
    if small_choice and small_choice.lower() not in ("none", "clear", "skip"):
        config.set(Config.LLAMACPP_SMALL_MODEL, small_choice)
        config.set(Config.SMALL_MODEL, small_choice)
        print(f"✓ Small model: {small_choice}")
    elif small_choice and small_choice.lower() in ("none", "clear"):
        config.unset(Config.LLAMACPP_SMALL_MODEL)
        config.unset(Config.SMALL_MODEL)
        print("✓ Small model cleared")
    elif small:
        print(f"✓ Small model: {small}")

    config.set(Config.API_PROVIDER, "llamacpp")
    print(f"\n✓ llama.cpp configured with provider: llamacpp")
    return 0


def init_command(binary: str = "ollama") -> int:
    """Initialize cterm by detecting Ollama and selecting a model."""
    config = Config()
    config.set(Config.API_PROVIDER, "ollama")

    while True:
        provider = choose_provider(config)
        if provider is None:
            return 1  # Ctrl+C during provider selection

        if provider == "openrouter":
            result = init_openrouter(config)
            if result != 0:
                return result
        elif provider == "llamacpp":
            result = init_llamacpp(config)
            if result != 0:
                return result
        else:
            result = init_ollama(config, binary)
            if result == 2:
                continue  # go back to provider selection
            if result != 0:
                return result

        break  # ollama succeeded

    # Unrestricted bash (common to both providers)
    current_unrestricted = config.unrestricted_bash
    prompt = (
        f"Enable unrestricted bash mode? [y/N]"
        f"{' (currently enabled)' if current_unrestricted else ''}: "
    )
    try:
        choice = input(prompt).strip().lower()
    except (KeyboardInterrupt, EOFError):
        choice = ""
    if choice in ("y", "yes"):
        if not current_unrestricted:
            config.set(Config.BASH_UNRESTRICTED, True)
            print("✓ Unrestricted bash mode enabled")
    else:
        if current_unrestricted:
            config.set(Config.BASH_UNRESTRICTED, False)
            print("✓ Unrestricted bash mode disabled")

    current_thinking = config.stream_thinking_traces
    prompt = (
        f"Stream model thinking traces in the UI? [y/N]"
        f"{' (currently enabled)' if current_thinking else ''}: "
    )
    try:
        choice = input(prompt).strip().lower()
    except (KeyboardInterrupt, EOFError):
        choice = ""
    if choice in ("y", "yes"):
        if not current_thinking:
            config.set(Config.STREAM_THINKING_TRACES, True)
            print("✓ Thinking trace streaming enabled")
    else:
        if current_thinking:
            config.set(Config.STREAM_THINKING_TRACES, False)
            print("✓ Thinking trace streaming disabled")

    print("\nYou can now use cterm:")
    print('  cterm "Hello, how are you?"')
    return 0


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
    
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    setup_root_logger(debug=args.debug)
    
    if args.version:
        print(f"cterm {__version__}")
        return 0
    
    if args.init:
        return init_command(args.binary)
    
    # Chat mode
    if not args.message:
        return tui_command(args.binary, debug=args.debug)
    
    message = " ".join(args.message)
    return chat_command(message, args.binary, debug=args.debug)


if __name__ == "__main__":
    # Ensure that running this file directly raises the intended error, 
    # as it's meant to be imported as a module in a package structure.
    raise SystemExit(main())
