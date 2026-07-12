import logging
logger = logging.getLogger(__name__)

def chat_with_model_api(
    model,
    messages,
    tools=None,
    binary="ollama",
    response_format=None,
    on_thinking_delta=None,
):
    import json
    import requests
    from cterm.config import Config

    def compact_messages_for_debug(items):
        compacted = []
        for item in items:
            copied = dict(item)
            content = copied.get("content")
            if isinstance(content, str) and len(content) > 4000:
                copied["content"] = (
                    content[:4000]
                    + f"\n...[truncated {len(content) - 4000} chars]"
                )
            compacted.append(copied)
        return compacted

    config = Config()
    provider = config.api_provider

    if provider == "openrouter":
        return _chat_openrouter(model, messages, tools, response_format, config, on_thinking_delta)
    if provider == "llamacpp":
        return _chat_llamacpp(model, messages, tools, response_format, config, on_thinking_delta)
    return _chat_ollama(model, messages, tools, response_format, on_thinking_delta)


def _chat_ollama(model, messages, tools=None, response_format=None, on_thinking_delta=None):
    import json
    import requests

    payload = {"model": model, "messages": messages, "stream": bool(on_thinking_delta)}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["format"] = response_format

    n_msg = len(messages)
    n_tools = len(tools) if tools else 0

    try:
        response = requests.post(
            "http://localhost:11434/api/chat", json=payload, timeout=60, stream=bool(on_thinking_delta)
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


_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_HEADERS = {
    "Content-Type": "application/json",
}


def _chat_openrouter(model, messages, tools=None, response_format=None, config=None, on_thinking_delta=None):
    import json
    import requests

    api_key = config.openrouter_api_key if config else None
    if not api_key:
        raise RuntimeError(
            "OpenRouter API key is not configured. Run 'cterm -i' to set it up."
        )

    headers = dict(_OPENROUTER_HEADERS)
    headers["Authorization"] = f"Bearer {api_key}"

    normalized_messages = _normalize_messages_for_openai(messages)

    payload = {"model": model, "messages": normalized_messages, "stream": bool(on_thinking_delta)}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = {"type": "json_object"}

    n_msg = len(normalized_messages)
    n_tools = len(tools) if tools else 0

    try:
        response = requests.post(
            _CHAT_COMPLETIONS_URL, headers=headers, json=payload, timeout=120, stream=bool(on_thinking_delta)
        )
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        body = response.text
        logger.error(
            "chat_api HTTP %s from OpenRouter: model=%r messages=%s tools=%s body=%s",
            response.status_code, model, n_msg, n_tools, body,
        )
        raise RuntimeError(
            f"OpenRouter API error {response.status_code} for model {model!r}: {body}"
        ) from exc

    if on_thinking_delta:
        return _normalize_openai_stream_response(response, on_thinking_delta)
    raw = response.json()
    return _normalize_openai_response(raw)


def _chat_llamacpp(model, messages, tools=None, response_format=None, config=None, on_thinking_delta=None):
    import json
    import requests

    server_url = config.llamacpp_server_url if config else "http://127.0.0.1:8083"
    url = server_url.rstrip("/") + "/v1/chat/completions"

    normalized_messages = _normalize_messages_for_openai(messages)

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
        return _normalize_openai_stream_response(response, on_thinking_delta)
    raw = response.json()
    return _normalize_openai_response(raw)


def _extract_thinking_delta(obj: dict) -> str:
    for key in ("thinking", "reasoning", "reasoning_content", "reasoning_text"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    details = obj.get("reasoning_details")
    if isinstance(details, list):
        parts = []
        for item in details:
            if not isinstance(item, dict):
                continue
            for key in ("delta", "text", "content", "reasoning"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
                    break
        return "".join(parts)
    return ""


def _normalize_ollama_stream_response(response, on_thinking_delta) -> dict:
    import json

    content_parts = []
    tool_calls = None
    final_message = {"role": "assistant", "content": ""}
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        chunk = json.loads(raw_line)
        message = chunk.get("message") or {}
        thinking = _extract_thinking_delta(message) or _extract_thinking_delta(chunk)
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


def _normalize_openai_stream_response(response, on_thinking_delta) -> dict:
    import json

    content_parts = []
    role = "assistant"
    tool_calls: dict[int, dict] = {}

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        chunk = json.loads(line)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        role = delta.get("role") or role

        thinking = _extract_thinking_delta(delta) or _extract_thinking_delta(choice)
        if thinking:
            on_thinking_delta(thinking)

        content = delta.get("content")
        if content:
            content_parts.append(content)

        for tc in delta.get("tool_calls") or []:
            index = tc.get("index", 0)
            entry = tool_calls.setdefault(
                index,
                {"type": "function", "function": {"name": "", "arguments": ""}},
            )
            if tc.get("id"):
                entry["id"] = tc["id"]
            if tc.get("type"):
                entry["type"] = tc["type"]
            func = tc.get("function") or {}
            if func.get("name"):
                entry["function"]["name"] += func["name"]
            if func.get("arguments"):
                entry["function"]["arguments"] += func["arguments"]

    normalized_calls = []
    for index in sorted(tool_calls):
        entry = tool_calls[index]
        raw_args = entry["function"].get("arguments") or "{}"
        try:
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            parsed_args = {}
        normalized = {
            "type": entry.get("type", "function"),
            "function": {
                "name": entry["function"].get("name", ""),
                "arguments": parsed_args,
            },
        }
        if "id" in entry:
            normalized["id"] = entry["id"]
        normalized_calls.append(normalized)

    message = {"role": role, "content": "".join(content_parts)}
    if normalized_calls:
        message["tool_calls"] = normalized_calls
    return {"message": message}


def _normalize_messages_for_openai(messages: list) -> list:
    import json
    import uuid

    normalized = []
    for msg in messages:
        m = dict(msg)

        if m.get("role") == "tool":
            m["role"] = "user"
            m.pop("tool_name", None)
            if "content" in m:
                m["content"] = f"[tool result]\n{m['content']}"

        tool_calls = m.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                if "id" not in tc:
                    tc["id"] = f"call_{uuid.uuid4().hex[:12]}"
                func = tc.get("function", {})
                if isinstance(func.get("arguments"), dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)

        normalized.append(m)
    return normalized


def _normalize_openai_response(raw: dict) -> dict:
    openai_message = raw.get("choices", [{}])[0].get("message", {})
    normalized = {
        "message": {
            "role": openai_message.get("role", "assistant"),
            "content": openai_message.get("content"),
        }
    }
    tool_calls = openai_message.get("tool_calls")
    if tool_calls:
        normalized_calls = []
        for tc in tool_calls:
            func = tc.get("function", {})
            raw_args = func.get("arguments", "{}")
            if isinstance(raw_args, str):
                try:
                    import json
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {}
            entry = {
                "type": "function",
                "function": {
                    "name": func.get("name", ""),
                    "arguments": raw_args,
                },
            }
            if "id" in tc:
                entry["id"] = tc["id"]
            normalized_calls.append(entry)
        normalized["message"]["tool_calls"] = normalized_calls
    return normalized
