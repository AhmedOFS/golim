"""Basic terminal configuration UI for openterm (interactive init flow)."""
import os
import subprocess
import time

from ...config import Config, get_config
from ...config.utils import (
    is_ollama_installed,
    get_models,
    validate_openrouter_key,
    normalize_openai_compatible_url,
    _ollama_server_running,
    select_model,
    select_optional_model,
)


def choose_provider(config: Config) -> str | None:
    """Let the user choose between Ollama, OpenRouter, and OpenAI-compatible.
    Returns None on Ctrl+C."""
    print("\nSet up API Provider:")
    print(f"  1. Ollama (local, default)")
    print(f"  2. OpenRouter (cloud, requires API key)")
    print(f"  3. OpenAI-compatible (URL and optional API key)")
    if config.api_provider == Config.OLLAMA:
        default = "1"
    elif config.api_provider == Config.OPEN_ROUTER:
        default = "2"
    elif config.api_provider == Config.OPENAI_COMPATIBLE:
        default = "3"
    elif config.api_provider is None:
        default = "1"
    else:
        print(f"Error: Unsupported API provider: {config.api_provider!r}")
        return None
    try:
        choice = input(f"Select provider [1-3, default {default}]: ").strip() or default
    except (KeyboardInterrupt, EOFError):
        print()
        return None
    if choice == "1":
        return Config.OLLAMA
    if choice == "2":
        return Config.OPEN_ROUTER
    if choice == "3":
        return Config.OPENAI_COMPATIBLE
    print("Error: provider selection must be 1, 2, or 3")
    return None


def init_openrouter(config: Config) -> int:
    """Configure openterm to use OpenRouter."""
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

    while True:
        if not api_key:
            try:
                api_key = input("Enter your OpenRouter API key: ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                return 1
            if not api_key:
                print("Error: API key is required")
                return 1
        print("Checking OpenRouter API key...")
        if validate_openrouter_key(api_key):
            break
        print("Could not validate the OpenRouter API key. Check your key and connection.\n")
        api_key = ""

    config.set_provider_value(Config.OPEN_ROUTER, Config.PROVIDER_API_KEY, api_key)
    print("✓ API key saved")

    saved_model = config.selected_model
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
    config.set(Config.SELECTED_MODEL, model)
    print(f"✓ Model: {model}")

    small = config.small_model
    print(f"\nOptional small model for lightweight tasks (skills selection,")
    print(f"  verification). Enter to skip or 'none' to clear.")
    prompt = f"Small model [{small or 'none'}]: "
    try:
        small_choice = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        small_choice = ""
    if small_choice and small_choice.lower() not in ("none", "clear", "skip"):
        config.set(Config.SMALL_MODEL, small_choice)
        print(f"✓ Small model: {small_choice}")
    elif small_choice and small_choice.lower() in ("none", "clear"):
        config.unset(Config.SMALL_MODEL)
        print("✓ Small model cleared")
    elif small:
        print(f"✓ Small model: {small}")

    config.set(Config.API_PROVIDER, Config.OPEN_ROUTER)
    config.remember_model(model, Config.OPEN_ROUTER)
    print("\n✓ OpenRouter configured")
    return 0


def init_ollama(config: Config, binary: str) -> int:
    """Configure openterm to use Ollama (local)."""
    if not is_ollama_installed(binary):
        print(f"Error: {binary} is not installed")
        print("Please install Ollama from https://ollama.ai")
        return 1

    print(f"✓ {binary} is installed")

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
    config.set(Config.API_PROVIDER, Config.OLLAMA)
    config.remember_model(selected, Config.OLLAMA)
    print(f"✓ Selected model: {selected}")

    small_model = select_optional_model(models, config.small_model, "small model")
    if small_model:
        config.set(Config.SMALL_MODEL, small_model)
        print(f"✓ Selected small model: {small_model}")
    else:
        config.unset(Config.SMALL_MODEL)
        print("✓ No small model configured")

    return 0


def init_openai_compatible(config: Config) -> int:
    """Configure openterm to use an OpenAI-compatible server."""
    prompt = "OpenAI-compatible server URL: "
    try:
        url = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return 1
    if not url:
        print("Error: server URL is required")
        return 1
    config.set(Config.OPENAI_COMPATIBLE_SERVER_URL, normalize_openai_compatible_url(url))

    api_key = config.openai_compatible_api_key
    prompt = f"OpenAI-compatible API key [{api_key or 'optional'}]: "
    try:
        api_key = input(prompt).strip() or api_key
    except (KeyboardInterrupt, EOFError):
        print()
        return 1
    config.set_provider_value(Config.OPENAI_COMPATIBLE, Config.PROVIDER_API_KEY, api_key)

    saved_model = config.selected_model
    prompt = f"Enter model name [{saved_model or 'default'}]: "
    try:
        model = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return 1
    if not model:
        model = saved_model or "default"
    config.set(Config.SELECTED_MODEL, model)
    config.remember_model(model, Config.OPENAI_COMPATIBLE)
    print(f"✓ Model: {model}")

    small = config.small_model
    print(f"\nOptional small model for lightweight tasks (skills selection,")
    print(f"  verification). Enter to skip or 'none' to clear.")
    prompt = f"Small model [{small or 'none'}]: "
    try:
        small_choice = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        small_choice = ""
    if small_choice and small_choice.lower() not in ("none", "clear", "skip"):
        config.set(Config.SMALL_MODEL, small_choice)
        print(f"✓ Small model: {small_choice}")
    elif small_choice and small_choice.lower() in ("none", "clear"):
        config.unset(Config.SMALL_MODEL)
        print("✓ Small model cleared")
    elif small:
        print(f"✓ Small model: {small}")

    config.set(Config.API_PROVIDER, Config.OPENAI_COMPATIBLE)
    print("\n✓ OpenAI-compatible provider configured")
    return 0


def init_command(binary: str = "ollama") -> int:
    """Initialize openterm by detecting Ollama and selecting a model."""
    config = get_config()
    config.set(Config.API_PROVIDER, Config.OLLAMA)

    while True:
        provider = choose_provider(config)
        if provider is None:
            return 1  # Ctrl+C during provider selection

        if provider == Config.OPEN_ROUTER:
            result = init_openrouter(config)
            if result != 0:
                return result
        elif provider == Config.OPENAI_COMPATIBLE:
            result = init_openai_compatible(config)
            if result != 0:
                return result
        elif provider == Config.OLLAMA:
            result = init_ollama(config, binary)
            if result == 2:
                continue  # go back to provider selection
            if result != 0:
                return result
        else:
            print(f"Error: Unsupported API provider: {provider!r}")
            return 1

        break  # ollama succeeded

    # Unrestricted mode (common to both providers)
    current_unrestricted = config.unrestricted_mode
    prompt = (
        f"Enable unrestricted mode? [y/N]"
        f"{' (currently enabled)' if current_unrestricted else ''}: "
    )
    try:
        choice = input(prompt).strip().lower()
    except (KeyboardInterrupt, EOFError):
        choice = ""
    if choice in ("y", "yes"):
        if not current_unrestricted:
            config.set(Config.UNRESTRICTED_MODE, True)
            print("✓ Unrestricted mode enabled")
    else:
        if current_unrestricted:
            config.set(Config.UNRESTRICTED_MODE, False)
            print("✓ Unrestricted mode disabled")

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

    print("\nYou can now use openterm:")
    print('  openterm "Hello, how are you?"')
    return 0
