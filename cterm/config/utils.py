"""Helper utilities for cterm configuration (binary detection, model listing, selection)."""
import shutil
import subprocess
import time

import requests


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


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


def get_openrouter_models(api_key: str | None) -> list[str]:
    """Return the model identifiers advertised by OpenRouter.

    OpenRouter orders this endpoint for its own catalogue.  Preserve that
    ordering so the picker presents the provider's current top models first.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = requests.get(OPENROUTER_MODELS_URL, headers=headers, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return []

    entries = payload.get("data", []) if isinstance(payload, dict) else []
    models: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        model_id = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(model_id, str) and model_id and model_id not in seen:
            models.append(model_id)
            seen.add(model_id)
    return models


def get_openai_compatible_models(url: str, api_key: str | None) -> list[str]:
    """Return models exposed by an OpenAI-compatible ``/v1/models`` endpoint."""
    if not url:
        return []
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = requests.get(url.rstrip("/") + "/v1/models", headers=headers, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return []
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    return [entry["id"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)]


def get_configured_model_choices(config, binary: str) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Build the Tab-menu model list with recent entries before provider lists."""
    from cterm.config import Config

    providers: list[tuple[str, list[str]]] = []
    if config.provider(Config.OLLAMA).get(Config.OLLAMA_SERVER_URL) is not None:
        providers.append((Config.OLLAMA, get_models(binary)))
    if config.openrouter_api_key:
        providers.append((Config.OPEN_ROUTER, get_openrouter_models(config.openrouter_api_key)))
    if config.provider(Config.OPENAI_COMPATIBLE).get(Config.LLAMACPP_SERVER_URL):
        providers.append((Config.OPENAI_COMPATIBLE, get_openai_compatible_models(config.llamacpp_server_url, config.llamacpp_api_key)))
    pairs = [(provider, model) for provider, models in providers for model in models]
    recent = [(item["provider"], item["model"]) for item in config.recent_models()]
    ordered = [pair for pair in recent if pair in pairs] + [pair for pair in pairs if pair not in recent]
    labels = [f"{provider}: {model}" for provider, model in ordered]
    return labels, dict(zip(labels, ordered))


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
