import json
import logging

import requests

from cterm.api.utils import extract_thinking_delta

logger = logging.getLogger(__name__)


def chat(model, messages, tools=None, response_format=None, on_thinking_delta=None, config=None):
    payload = {"model": model, "messages": messages, "stream": bool(on_thinking_delta)}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["format"] = response_format

    n_msg = len(messages)
    n_tools = len(tools) if tools else 0

    try:
        response = requests.post(
            (config.ollama_server_url if config else "http://localhost:11434").rstrip("/") + "/api/chat",
            json=payload, timeout=60, stream=bool(on_thinking_delta)
        )
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        body = response.text
        logger.error(
            "chat_api HTTP %s from Ollama: model=%r messages=%s tools=%s body=%s",
            response.status_code, model, n_msg, n_tools, body,
        )
        raise RuntimeError(
            f"Ollama API error {response.status_code} for model {model!r}: {body}"
        ) from exc
    if on_thinking_delta:
        return _normalize_ollama_stream_response(response, on_thinking_delta)
    return response.json()


def _normalize_ollama_stream_response(response, on_thinking_delta) -> dict:
    content_parts = []
    tool_calls = None
    final_message = {"role": "assistant", "content": ""}
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        chunk = json.loads(raw_line)
        message = chunk.get("message") or {}
        thinking = extract_thinking_delta(message) or extract_thinking_delta(chunk)
        if thinking:
            on_thinking_delta(thinking)
        content = message.get("content")
        if content:
            content_parts.append(content)
        if message.get("tool_calls"):
            tool_calls = message.get("tool_calls")
        if message:
            final_message.update(message)

    final_message["content"] = "".join(content_parts) or final_message.get("content") or ""
    if tool_calls:
        final_message["tool_calls"] = tool_calls
    return {"message": final_message}
