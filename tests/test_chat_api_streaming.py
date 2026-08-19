import json
import unittest
from unittest.mock import MagicMock, patch

from openterm.api import chat_api
from openterm.api import ollama as chat_api_ollama
from openterm.api import utils as chat_api_utils
from openterm.config import Config


class FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            if decode_unicode:
                yield line
            else:
                yield line.encode("utf-8")


class ChatApiStreamingTests(unittest.TestCase):
    def test_dispatch_uses_the_canonical_openrouter_provider_name(self):
        config = MagicMock(api_provider=Config.OPEN_ROUTER)
        expected = {"message": {"content": "ok"}}

        with patch.object(chat_api, "get_config", return_value=config), \
             patch.object(chat_api.openrouter, "chat", return_value=expected) as openrouter:
            result = chat_api.chat_with_model_api("model", [])

        self.assertIs(result, expected)
        openrouter.assert_called_once_with("model", [], None, None, config, None)

    def test_dispatch_passes_args_in_canonical_order_for_all_providers(self):
        # Guard against the provider chat() signatures drifting out of sync:
        # all must be (model, messages, tools, response_format, config, on_thinking_delta).
        for provider, module in [
            (Config.OPEN_ROUTER, "openrouter"),
            (Config.OPENAI_COMPATIBLE, "openai_compatible"),
            (Config.OLLAMA, "ollama"),
        ]:
            with self.subTest(provider=provider):
                config = MagicMock(api_provider=provider)
                delta_cb = lambda _delta: None

                with patch.object(chat_api, "get_config", return_value=config), \
                     patch.object(getattr(chat_api, module), "chat", return_value={}) as mocked:
                    chat_api.chat_with_model_api("m", [{"role": "user", "content": "hi"}], tools=["t"], response_format="json", on_thinking_delta=delta_cb)

                # config is injected from get_config below, pushed into the slot that
                # provider code expects it in; the trailing slot is on_thinking_delta.
                mocked.assert_called_once_with("m", [{"role": "user", "content": "hi"}], ["t"], "json", config, delta_cb)

    def test_noncanonical_provider_name_is_rejected(self):
        config = MagicMock(api_provider="openrouter")

        with patch.object(chat_api, "get_config", return_value=config):
            with self.assertRaisesRegex(ValueError, "Unsupported API provider"):
                chat_api.chat_with_model_api("model", [])

    def test_json_response_decodes_utf8_bytes_independent_of_response_charset(self):
        class Response:
            content = json.dumps({"content": "🌤️ — +32°C"}, ensure_ascii=False).encode("utf-8")

            def json(self):
                raise AssertionError("raw UTF-8 content should be parsed directly")

        parsed = chat_api_utils.parse_utf8_json_response(Response())

        self.assertEqual(parsed["content"], "🌤️ — +32°C")

    def test_ollama_stream_emits_thinking_and_preserves_tool_call(self):
        deltas = []
        response = FakeStreamResponse([
            json.dumps({"message": {"thinking": "checking "}}),
            json.dumps({"message": {"thinking": "files"}}),
            json.dumps({
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "bash",
                            "arguments": {"command": "pwd"},
                        }
                    }]
                }
            }),
            json.dumps({"done": True}),
        ])

        result = chat_api_ollama._normalize_ollama_stream_response(response, deltas.append)

        self.assertEqual(deltas, ["checking ", "files"])
        self.assertEqual(result["message"]["content"], "")
        self.assertEqual(
            result["message"]["tool_calls"][0]["function"]["arguments"],
            {"command": "pwd"},
        )

    def test_openai_compatible_chat_appends_v1_once_with_or_without_v1(self):
        from openterm.api import openai_compatible

        for stored_url in ("http://host:8000/v1", "http://host:8000"):
            with self.subTest(stored_url=stored_url):
                config = MagicMock(
                    openai_compatible_server_url=stored_url,
                    openai_compatible_api_key="key",
                )
                response = MagicMock()
                response.content = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

                with patch("openterm.api.openai_compatible.requests.post", return_value=response) as post:
                    result = openai_compatible.chat("model", [{"role": "user", "content": "hi"}], config=config)

                self.assertEqual(result["message"]["content"], "ok")
                self.assertEqual(post.call_args.args[0], "http://host:8000/v1/chat/completions")

    def test_openai_stream_reconstructs_reasoning_content_and_tool_args(self):
        deltas = []
        response = FakeStreamResponse([
            'data: {"choices":[{"delta":{"role":"assistant","reasoning_content":"plan "}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"step"}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"bash","arguments":"{\\"command\\":"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"pwd\\"}"}}]}}]}',
            "data: [DONE]",
        ])

        result = chat_api_utils.normalize_openai_stream_response(response, deltas.append)

        self.assertEqual(deltas, ["plan ", "step"])
        self.assertEqual(result["message"]["role"], "assistant")
        self.assertEqual(result["message"]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(
            result["message"]["tool_calls"][0]["function"]["arguments"],
            {"command": "pwd"},
        )

    def test_openai_stream_decodes_utf8_independent_of_response_charset(self):
        deltas = []
        response = FakeStreamResponse([
            "data: " + json.dumps({
                "choices": [{"delta": {
                    "content": "Clear 🌤️ — +32°C ↓",
                }}],
            }, ensure_ascii=False),
            "data: [DONE]",
        ])

        result = chat_api_utils.normalize_openai_stream_response(response, deltas.append)

        self.assertEqual(result["message"]["content"], "Clear 🌤️ — +32°C ↓")


if __name__ == "__main__":
    unittest.main()
