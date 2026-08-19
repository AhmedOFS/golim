"""Shared application-home resolution for openterm."""

from __future__ import annotations

import contextvars
from pathlib import Path


_app_home_context: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_app_home_context",
    default=None,
)


def resolve_app_home() -> Path:
    """Resolve, create, and bind openterm's application home for this context."""
    app_home = Path.home() / ".openterm"
    app_home.mkdir(parents=True, exist_ok=True)
    _app_home_context.set(app_home)
    return app_home


def get_app_home() -> Path:
    """Return the bound app home, or the current user's default app home."""
    default_home = Path.home() / ".openterm"
    bound_home = _app_home_context.get()
    if bound_home is not None and bound_home == default_home:
        return bound_home
    return default_home
