import json
import logging

import requests

from cterm.api.utils import (
    normalize_messages_for_openai,
    normalize_openai_response,
    normalize_openai_stream_response,
    parse_utf8_json_response,
)
from cterm.api.retry import with_retries
from cterm.config.utils import normalize_openai_compatible_url

logger = logging.getLogger(__name__)


def chat(model, messages, tools=None, response_format=None, config=None, on_thinking_delta=None):
    server_url = config.openai_compatible_server_url if config else None
    url = normalize_openai_compatible_url(server_url or "") + "/v1/chat/completions"

    normalized_messages = normalize_messages_for_openai(messages)

    payload = {"model": model, "messages": normalized_messages, "stream": bool(on_thinking_delta)}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = {"type": "json_object"}

    n_msg = len(normalized_messages)
    n_tools = len(tools) if tools else 0

    headers = {"Authorization": f"Bearer {config.openai_compatible_api_key}"} if config and config.openai_compatible_api_key else None
    def _request():
        response = requests.post(url, headers=headers, json=payload, timeout=(10, 120), stream=bool(on_thinking_delta))
        response.raise_for_status()
        if on_thinking_delta:
            return normalize_openai_stream_response(response, on_thinking_delta)
        try:
            return normalize_openai_response(parse_utf8_json_response(response))
        except json.JSONDecodeError as exc:
            body_preview = response.text[:500] if response.text else "(empty)"
            raise ValueError(
                f"OpenAI-compatible server returned invalid JSON (status {response.status_code}): "
                f"{exc}. Body preview: {body_preview}"
            ) from exc

    return with_retries(_request, provider="OpenAI-compatible API")
