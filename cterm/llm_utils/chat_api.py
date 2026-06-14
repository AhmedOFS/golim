import logging
logger = logging.getLogger(__name__)

def chat_with_model_api(model, messages, tools=None, binary="ollama", response_format=None):
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
        return _chat_openrouter(model, messages, tools, response_format, config)
    if provider == "llamacpp":
        return _chat_llamacpp(model, messages, tools, response_format, config)
    return _chat_ollama(model, messages, tools, response_format)


def _chat_ollama(model, messages, tools=None, response_format=None):
    import json
    import requests

    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["format"] = response_format

    n_msg = len(messages)
    n_tools = len(tools) if tools else 0

    try:
        response = requests.post(
            "http://localhost:11434/api/chat", json=payload, timeout=60
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
    return response.json()


_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_HEADERS = {
    "Content-Type": "application/json",
}


def _chat_openrouter(model, messages, tools=None, response_format=None, config=None):
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

    payload = {"model": model, "messages": normalized_messages, "stream": False}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = {"type": "json_object"}

    n_msg = len(normalized_messages)
    n_tools = len(tools) if tools else 0

    try:
        response = requests.post(
            _CHAT_COMPLETIONS_URL, headers=headers, json=payload, timeout=120
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

    raw = response.json()
    return _normalize_openai_response(raw)


def _chat_llamacpp(model, messages, tools=None, response_format=None, config=None):
    import json
    import requests

    server_url = config.llamacpp_server_url if config else "http://127.0.0.1:8083"
    url = server_url.rstrip("/") + "/v1/chat/completions"

    normalized_messages = _normalize_messages_for_openai(messages)

    payload = {"model": model, "messages": normalized_messages, "stream": False}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = {"type": "json_object"}

    n_msg = len(normalized_messages)
    n_tools = len(tools) if tools else 0

    try:
        response = requests.post(url, json=payload, timeout=120)
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

    raw = response.json()
    return _normalize_openai_response(raw)


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
