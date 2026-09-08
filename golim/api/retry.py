"""Small, provider-neutral retry helpers for model requests."""
from __future__ import annotations

import logging
import time

import requests


logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
RETRY_DELAYS = (0.25, 0.5)


def with_retries(operation, *, provider: str):
    """Run ``operation`` up to three times for transport/malformed responses.

    Requests' ``RequestException`` covers connection, HTTP, timeout, redirect,
    and URL errors.  Value/Key/Type errors are included because providers can
    return invalid JSON or a response that does not match the chat schema.
    """
    retryable = (requests.exceptions.RequestException, ValueError, KeyError, TypeError, AttributeError, IndexError)
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return operation()
        except retryable as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS - 1:
                break
            delay = RETRY_DELAYS[attempt]
            logger.debug(
                "%s request failed (%s/%s): %s; retrying in %.2fs",
                provider, attempt + 1, MAX_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
    raise RuntimeError(
        f"{provider} request failed after {MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error
