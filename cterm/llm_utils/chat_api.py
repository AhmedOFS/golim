
def chat_with_model_api(model, messages, tools=None, binary="ollama", response_format=None):
    import requests
    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["format"] = response_format
    response = requests.post("http://localhost:11434/api/chat", json=payload, timeout=60)
    response.raise_for_status()
    return response.json()
