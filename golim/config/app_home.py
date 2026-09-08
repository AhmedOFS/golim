"""Shared application-home resolution for golim."""

from __future__ import annotations

from pathlib import Path


_app_home: Path | None = None


def resolve_app_home() -> Path:
    """Resolve, create, and store golim's application home at startup."""
    global _app_home
    app_home = Path.home() / ".golim"
    app_home.mkdir(parents=True, exist_ok=True)
    _app_home = app_home
    return _app_home


def get_app_home() -> Path:
    """Return the startup app home, with a fallback for library callers."""
    return _app_home or (Path.home() / ".golim")
