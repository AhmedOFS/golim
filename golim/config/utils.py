"""Helper utilities for golim configuration (binary detection, model listing, selection)."""
import shutil
import subprocess
import time
from urllib.parse import urlparse, urlunparse

import requests


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"


def normalize_openai_compatible_url(url: str | None) -> str:
    """Normalize an OpenAI-compatible server URL to its base form.

    OpenAI-compatible servers expose ``/v1/models`` and
    ``/v1/chat/completions``, so a configured base URL may or may not already
    end in ``/v1``.  This injects an ``http://`` scheme when the user omits
    one and strips a trailing ``/v1`` so downstream callers can append
    ``/v1/...`` without producing ``/v1/v1``.  Empty input is returned as-is.
    """
    if not url:
        return url
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


def is_ollama_installed(binary: str = "ollama") -> bool:
    """Return whether the configured Ollama executable is available."""
    return bool(shutil.which(binary))


def resolve_provider_settings(config, binary: str = "ollama") -> tuple[str | None, str | None, str]:
    """Validate the configured provider and return its runtime settings.

    The returned tuple is ``(error, model, label)``.  Provider
    identifiers are deliberately matched only against the canonical Config
    constants; unknown values are errors rather than implicit fallbacks.
    """
    from golim.config import Config

    provider = config.api_provider
    model = config.selected_model

    if provider == Config.OPEN_ROUTER:
        label = model or "OpenRouter"
        if not config.openrouter_api_key:
            return "Error: OpenRouter API key not configured\nRun 'Golim -i' to set it up", None, label
    elif provider == Config.OPENAI_COMPATIBLE:
        label = model or "OpenAI-compatible"
        if not config.openai_compatible_server_url:
            return "Error: OpenAI-compatible server URL not configured\nRun 'Golim -i' to set it up", None, label
    elif provider == Config.OLLAMA:
        label = model or "Ollama"
        if not is_ollama_installed(binary):
            return f"Error: {binary} is not installed", None, label
    else:
        label = model or f"Unknown provider: {provider}"
        return f"Error: Unsupported API provider: {provider!r}\nRun 'Golim -i' to set it up", None, label

    if not model:
        return "Error: No model configured\nRun 'Golim -i' to initialize", None, label

    return None, model, label


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


def validate_openrouter_key(api_key: str | None) -> bool:
    """Return whether an OpenRouter API key is valid.

    The current-key endpoint returns 401 for an invalid or missing token and
    200 with the key details for a valid token.  The credits endpoint is not
    used here because it requires a management key, not a regular API key.
    """
    if not api_key:
        return False
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(OPENROUTER_KEY_URL, headers=headers, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return False
    return isinstance(payload, dict) and "data" in payload


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
        response = requests.get(normalize_openai_compatible_url(url) + "/v1/models", headers=headers, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return []
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    return [entry["id"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)]


def get_configured_model_choices(config, binary: str) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Build the Tab-menu model list with recent entries before provider lists.

    A configured provider's saved model remains selectable when its catalogue
    cannot be fetched. This is especially important immediately after the
    first-run wizard, where that saved model may be the only known model for a
    remote provider.
    """
    from golim.config import Config

    providers: list[tuple[str, list[str]]] = []
    if config.provider(Config.OLLAMA).get(Config.OLLAMA_SERVER_URL) is not None:
        providers.append((Config.OLLAMA, get_models(binary)))
    if config.openrouter_api_key:
        providers.append((Config.OPEN_ROUTER, get_openrouter_models(config.openrouter_api_key)))
    if config.provider(Config.OPENAI_COMPATIBLE).get(Config.OPENAI_COMPATIBLE_SERVER_URL):
        providers.append((Config.OPENAI_COMPATIBLE, get_openai_compatible_models(config.openai_compatible_server_url, config.openai_compatible_api_key)))
    pairs = [(provider, model) for provider, models in providers for model in models]
    configured_providers = {provider for provider, _ in providers}
    recent = [
        (item["provider"], item["model"])
        for item in config.recent_models()
        if item["provider"] in configured_providers
    ]
    current = (config.api_provider, config.selected_model)
    saved = [current] if current[0] in configured_providers and current[1] else []
    ordered: list[tuple[str, str]] = []
    for pair in recent + saved + pairs:
        if pair not in ordered:
            ordered.append(pair)
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
