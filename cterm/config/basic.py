"""Basic terminal configuration UI for cterm (interactive init flow)."""
import os
import subprocess
import time

from .config import Config
from .utils import (
    detect_ollama,
    get_models,
    _ollama_server_running,
    select_model,
    select_optional_model,
)


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
