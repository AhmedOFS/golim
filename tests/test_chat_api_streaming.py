import json
import unittest
from unittest.mock import MagicMock, patch

from golim.api import chat_api
from golim.api import ollama as chat_api_ollama
from golim.api import utils as chat_api_utils
from golim.config import Config


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
            with self.assertRaisesRegex(ValueError, "Unsupported AI provider"):
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
        from golim.api import openai_compatible

        for stored_url in ("http://host:8000/v1", "http://host:8000"):
            with self.subTest(stored_url=stored_url):
                config = MagicMock(
                    openai_compatible_server_url=stored_url,
                    openai_compatible_api_key="key",
                )
                response = MagicMock()
                response.content = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

                with patch("golim.api.openai_compatible.requests.post", return_value=response) as post:
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

    def test_normalize_pairs_tool_results_with_tool_call_ids(self):
        from golim.api.utils import normalize_messages_for_openai

        messages = [
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "bash", "arguments": {"command": "pwd"}},
            }]},
            {"role": "tool", "tool_name": "bash", "content": "result text"},
        ]

        normalized = normalize_messages_for_openai(messages)

        self.assertEqual(normalized[1]["tool_calls"][0]["function"]["arguments"], json.dumps({"command": "pwd"}))
        self.assertEqual(normalized[2]["role"], "tool")
        self.assertEqual(normalized[2]["tool_call_id"], "call_1")
        self.assertEqual(normalized[2]["name"], "bash")
        self.assertEqual(normalized[2]["content"], "result text")

    def test_normalize_assigns_and_pairs_missing_tool_call_ids(self):
        from golim.api.utils import normalize_messages_for_openai

        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{
                "type": "function", "function": {"name": "bash", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_name": "bash", "content": "out"},
        ]

        normalized = normalize_messages_for_openai(messages)

        call_id = normalized[0]["tool_calls"][0]["id"]
        self.assertTrue(call_id.startswith("call_"))
        self.assertEqual(normalized[1]["tool_call_id"], call_id)

    def test_normalize_pairs_sequential_same_name_tool_calls_in_order(self):
        from golim.api.utils import normalize_messages_for_openai

        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_a", "type": "function", "function": {"name": "bash", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_name": "bash", "content": "first"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_b", "type": "function", "function": {"name": "bash", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_name": "bash", "content": "second"},
        ]

        normalized = normalize_messages_for_openai(messages)

        self.assertEqual(normalized[1]["tool_call_id"], "call_a")
        self.assertEqual(normalized[3]["tool_call_id"], "call_b")

    def test_normalize_collapses_orphan_tool_result_to_user_message(self):
        from golim.api.utils import normalize_messages_for_openai

        normalized = normalize_messages_for_openai([
            {"role": "tool", "tool_name": "bash", "content": "orphan"},
        ])

        self.assertEqual(normalized[0]["role"], "user")
        self.assertEqual(normalized[0]["content"], "[tool result]\norphan")
        self.assertNotIn("tool_name", normalized[0])

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

    def test_openrouter_chat_sends_configured_max_tokens_cap(self):
        import requests

        from golim.api import openrouter as chat_api_openrouter

        response = requests.models.Response()
        response.status_code = 200
        response._content = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode("utf-8")
        config = MagicMock(openrouter_api_key="key", openrouter_max_tokens=8000)

        def _passthrough(operation, provider=""):
            return operation()

        with patch("golim.api.openrouter.requests.post", return_value=response) as post, \
             patch.object(chat_api_openrouter, "with_retries", _passthrough):
            chat_api_openrouter.chat("model", [{"role": "user", "content": "hi"}], config=config)

        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 8000)

    def test_openrouter_chat_omits_max_tokens_when_unset_or_invalid(self):
        import requests

        from golim.api import openrouter as chat_api_openrouter

        response = requests.models.Response()
        response.status_code = 200
        response._content = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode("utf-8")

        def _passthrough(operation, provider=""):
            return operation()

        for value in (None, 0, "bad", -1):
            with self.subTest(value=value):
                config = MagicMock(openrouter_api_key="key", openrouter_max_tokens=value)
                with patch("golim.api.openrouter.requests.post", return_value=response) as post, \
                     patch.object(chat_api_openrouter, "with_retries", _passthrough):
                    chat_api_openrouter.chat("model", [{"role": "user", "content": "hi"}], config=config)

                self.assertNotIn("max_tokens", post.call_args.kwargs["json"])

    def test_openrouter_402_error_includes_afforded_amount_from_body(self):
        import requests

        from golim.api import openrouter as chat_api_openrouter

        response = MagicMock()
        response.status_code = 402
        response.reason = "Payment Required"
        response.json.return_value = {
            "error": {
                "message": (
                    "This request requires more credits, or fewer max_tokens. "
                    "You requested up to 32000 tokens, but can only afford 29262. "
                    "To increase, visit https://openrouter.ai/settings/credits and add more credits"
                ),
                "code": 402,
            }
        }
        config = MagicMock(openrouter_api_key="key")

        def _passthrough(operation, provider=""):
            return operation()

        with patch("golim.api.openrouter.requests.post", return_value=response), \
             patch.object(chat_api_openrouter, "with_retries", _passthrough):
            with self.assertRaisesRegex(requests.HTTPError, "can only afford 29262"):
                chat_api_openrouter.chat(
                    "anthropic/claude-fable-5.1",
                    [{"role": "user", "content": "hi"}],
                    config=config,
                )

    def test_openrouter_402_falls_back_to_raw_body_when_not_json(self):
        import requests

        from golim.api import openrouter as chat_api_openrouter

        response = requests.models.Response()
        response.status_code = 402
        response.reason = "Payment Required"
        response.url = chat_api_openrouter._CHAT_COMPLETIONS_URL
        response._content = b"insufficient credits"

        config = MagicMock(openrouter_api_key="key")

        def _passthrough(operation, provider=""):
            return operation()

        with patch("golim.api.openrouter.requests.post", return_value=response), \
             patch.object(chat_api_openrouter, "with_retries", _passthrough):
            with self.assertRaisesRegex(requests.HTTPError, "402 Payment Required: insufficient credits"):
                chat_api_openrouter.chat("model", [{"role": "user", "content": "hi"}], config=config)

    def test_openrouter_non_402_errors_keep_standard_raise_for_status(self):
        import requests

        from golim.api import openrouter as chat_api_openrouter

        response = requests.models.Response()
        response.status_code = 502
        response.reason = "Bad Gateway"
        response.url = chat_api_openrouter._CHAT_COMPLETIONS_URL
        response._content = json.dumps({"error": {"message": "upstream connect error"}}).encode("utf-8")

        config = MagicMock(openrouter_api_key="key")

        def _passthrough(operation, provider=""):
            return operation()

        with patch("golim.api.openrouter.requests.post", return_value=response), \
             patch.object(chat_api_openrouter, "with_retries", _passthrough):
            with self.assertRaises(requests.HTTPError) as ctx:
                chat_api_openrouter.chat("model", [{"role": "user", "content": "hi"}], config=config)

        self.assertIn("502 Server Error: Bad Gateway", str(ctx.exception))
        self.assertNotIn("upstream connect error", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
