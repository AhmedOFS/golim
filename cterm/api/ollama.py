import json
import logging

import requests

from cterm.api.utils import extract_thinking_delta
from cterm.api.retry import with_retries

logger = logging.getLogger(__name__)


def chat(model, messages, tools=None, response_format=None, on_thinking_delta=None, config=None):
    payload = {"model": model, "messages": messages, "stream": bool(on_thinking_delta)}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["format"] = response_format

    n_msg = len(messages)
    n_tools = len(tools) if tools else 0

    def _request():
        response = requests.post(
            (config.ollama_server_url if config else "http://localhost:11434").rstrip("/") + "/api/chat",
            json=payload, timeout=(10, 120), stream=bool(on_thinking_delta)
        )
        response.raise_for_status()
        if on_thinking_delta:
            return _normalize_ollama_stream_response(response, on_thinking_delta)
        raw = response.json()
        # if not isinstance(raw, dict) or not isinstance(raw.get("message"), dict):
        #     raise ValueError("malformed Ollama chat response: missing message object")
        return raw

    return with_retries(_request, provider="Ollama")


def _normalize_ollama_stream_response(response, on_thinking_delta) -> dict:
    content_parts = []
    tool_calls = None
    final_message = {"role": "assistant", "content": ""}
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        chunk = json.loads(raw_line)
        # if not isinstance(chunk, dict):
        #     raise ValueError("malformed Ollama stream chunk")
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
    # if not final_message.get("content") and not final_message.get("tool_calls"):
    #     raise ValueError("malformed Ollama stream response: no assistant message")
    return {"message": final_message}
