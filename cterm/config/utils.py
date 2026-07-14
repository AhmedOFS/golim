"""Helper utilities for cterm configuration (binary detection, model listing, selection)."""
import shutil
import subprocess
import time


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
