import json
import logging

import requests

from golim.api.utils import (
    normalize_messages_for_openai,
    normalize_openai_response,
    normalize_openai_stream_response,
    parse_utf8_json_response,
)
from golim.api.retry import with_retries

logger = logging.getLogger(__name__)

_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_HEADERS = {
    "Content-Type": "application/json",
}

_MAX_ERROR_BODY_PREVIEW = 500


def _raise_for_status(response):
    """Raise for HTTP errors, attaching OpenRouter's 402 error body.

    A 402 Payment Required body names the affordable token budget, which
    plain raise_for_status() discards and leaves only the status line.
    Every other status keeps the standard requests behavior.
    """
    if response.status_code != 402:
        response.raise_for_status()
        return
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or json.dumps(error)
        elif error is not None:
            detail = error
        else:
            detail = json.dumps(payload)
    else:
        detail = response.text
    detail = str(detail or "").strip()[:_MAX_ERROR_BODY_PREVIEW]
    message = f"{response.status_code} {response.reason or ''}".strip()
    if detail:
        message = f"{message}: {detail}"
    raise requests.HTTPError(message, response=response)


def chat(model, messages, tools=None, response_format=None, config=None, on_thinking_delta=None):
    api_key = config.openrouter_api_key if config else None
    if not api_key:
        raise RuntimeError(
            "OpenRouter API key is not configured. Run 'Golim -i' to set it up."
        )

    headers = dict(_OPENROUTER_HEADERS)
    headers["Authorization"] = f"Bearer {api_key}"

    normalized_messages = normalize_messages_for_openai(messages)

    payload = {"model": model, "messages": normalized_messages, "stream": bool(on_thinking_delta)}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = {"type": "json_object"}

    n_msg = len(normalized_messages)
    n_tools = len(tools) if tools else 0

    def _request():
        response = requests.post(
            _CHAT_COMPLETIONS_URL, headers=headers, json=payload, timeout=(10, 120), stream=bool(on_thinking_delta)
        )
        _raise_for_status(response)
        if on_thinking_delta:
            return normalize_openai_stream_response(response, on_thinking_delta)
        try:
            return normalize_openai_response(parse_utf8_json_response(response))
        except json.JSONDecodeError as exc:
            body_preview = response.text[:500] if response.text else "(empty)"
            raise ValueError(
                f"OpenRouter returned invalid JSON (status {response.status_code}): "
                f"{exc}. Body preview: {body_preview}"
            ) from exc

    return with_retries(_request, provider="OpenRouter")
