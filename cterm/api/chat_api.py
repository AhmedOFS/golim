import logging

from cterm.config import Config, get_config
from cterm.api import ollama, openrouter, openai_compatible

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

    if provider in {"open_router", "openrouter"}:
        return openrouter.chat(model, messages, tools, response_format, config, on_thinking_delta)
    if provider == "openai_compatible":
        return openai_compatible.chat(model, messages, tools, response_format, config, on_thinking_delta)
    return ollama.chat(model, messages, tools, response_format, on_thinking_delta, config)
