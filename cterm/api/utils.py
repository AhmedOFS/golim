import json
import logging
import uuid

def extract_thinking_delta(obj: dict) -> str:
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


def normalize_messages_for_openai(messages: list) -> list:
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


def normalize_openai_response(raw: dict) -> dict:
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


def normalize_openai_stream_response(response, on_thinking_delta) -> dict:
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

        thinking = extract_thinking_delta(delta) or extract_thinking_delta(choice)
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
