import logging

from openterm.config import Config, get_config
from openterm.api import ollama, openrouter, openai_compatible

logger = logging.getLogger(__name__)


def chat_with_model_api(
    model,
    messages,
    tools=None,
    binary="ollama",
    response_format=None,
    on_thinking_delta=None,
):
    config = get_config()
    provider = config.api_provider

    if provider == Config.OPEN_ROUTER:
        return openrouter.chat(model, messages, tools, response_format, config, on_thinking_delta)
    if provider == Config.OPENAI_COMPATIBLE:
        return openai_compatible.chat(model, messages, tools, response_format, config, on_thinking_delta)
    if provider == Config.OLLAMA:
        return ollama.chat(model, messages, tools, response_format, config, on_thinking_delta)
    raise ValueError(f"Unsupported API provider: {provider!r}")
