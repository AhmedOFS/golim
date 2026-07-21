import logging

from cterm.config import Config
from cterm.api import ollama, openrouter, llamacpp

logger = logging.getLogger(__name__)


def chat_with_model_api(
    model,
    messages,
    tools=None,
    binary="ollama",
    response_format=None,
    on_thinking_delta=None,
):
    config = Config()
    provider = config.api_provider

    if provider in {"open_router", "openrouter"}:
        return openrouter.chat(model, messages, tools, response_format, config, on_thinking_delta)
    if provider in {"openai_compatible", "llamacpp"}:
        return llamacpp.chat(model, messages, tools, response_format, config, on_thinking_delta)
    return ollama.chat(model, messages, tools, response_format, on_thinking_delta, config)
