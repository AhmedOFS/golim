import logging

import requests

from cterm.api.utils import normalize_messages_for_openai, normalize_openai_response, normalize_openai_stream_response

logger = logging.getLogger(__name__)


def chat(model, messages, tools=None, response_format=None, config=None, on_thinking_delta=None):
    server_url = config.llamacpp_server_url if config else "http://127.0.0.1:8083"
    url = server_url.rstrip("/") + "/v1/chat/completions"

    normalized_messages = normalize_messages_for_openai(messages)

    payload = {"model": model, "messages": normalized_messages, "stream": bool(on_thinking_delta)}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = {"type": "json_object"}

    n_msg = len(normalized_messages)
    n_tools = len(tools) if tools else 0

    try:
        response = requests.post(url, json=payload, timeout=120, stream=bool(on_thinking_delta))
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        body = response.text
        logger.error(
            "chat_api HTTP %s from llama.cpp: model=%r messages=%s tools=%s body=%s",
            response.status_code, model, n_msg, n_tools, body,
        )
        raise RuntimeError(
            f"llama.cpp API error {response.status_code} for model {model!r}: {body}"
        ) from exc

    if on_thinking_delta:
        return normalize_openai_stream_response(response, on_thinking_delta)
    raw = response.json()
    return normalize_openai_response(raw)
