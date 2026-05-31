
def chat_with_model_api(model, messages, tools=None, binary="ollama", response_format=None):
    import json
    import sys
    import requests

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

    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["format"] = response_format
    n_msg = len(messages)
    n_tools = len(tools) if tools else 0
    last_role = messages[-1]["role"] if messages else "none"

    try:
        response = requests.post("http://localhost:11434/api/chat", json=payload, timeout=60)
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        body = response.text
        tools_payload = (
            json.dumps(tools, indent=2, sort_keys=True)
            if tools
            else "<none>"
        )
        messages_payload = json.dumps(
            compact_messages_for_debug(messages),
            indent=2,
            sort_keys=True,
        )
        print(
            f"[debug] chat_api HTTP {response.status_code} from Ollama:\n"
            f"  request: model={model!r} messages={n_msg} tools={n_tools}\n"
            f"  response body: {body}\n"
            f"  messages payload:\n{messages_payload}\n"
            f"  tools payload:\n{tools_payload}",
            file=sys.stderr,
        )
        raise RuntimeError(
            f"Ollama API error {response.status_code} for model {model!r}: {body}"
        ) from exc
    return response.json()
