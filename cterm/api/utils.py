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
    # if not isinstance(raw, dict):
    #     raise ValueError("malformed OpenAI response: expected object")
    choices = raw.get("choices") if isinstance(raw, dict) else None
    # if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
    #     raise ValueError("malformed OpenAI response: missing choices")
    openai_message = choices[0].get("message") if isinstance(choices, list) and choices else {}
    # if not isinstance(openai_message, dict):
    #     raise ValueError("malformed OpenAI response: missing assistant message")
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
            # if not isinstance(func, dict) or not func.get("name"):
            #     raise ValueError("malformed OpenAI response: invalid tool call")
            raw_args = func.get("arguments", "{}") if isinstance(func, dict) else "{}"
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {}
            #     except json.JSONDecodeError as exc:
            #         raise ValueError("malformed OpenAI response: invalid tool arguments") from exc
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
        if line == "[DONE]":
            break
        if not line.startswith("data:"):
            continue
        line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in stream response line: {exc}. "
                f"Line preview: {line[:200]}"
            ) from exc
        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        # if not isinstance(choice, dict):
        #     raise ValueError("malformed OpenAI stream chunk: invalid choice")
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
        #     raise ValueError("malformed OpenAI stream response: invalid tool arguments") from exc
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
    # if not message["content"] and not normalized_calls:
    #     raise ValueError("malformed OpenAI stream response: no assistant message")
    return {"message": message}
