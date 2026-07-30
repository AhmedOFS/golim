import json
import unittest

from cterm.api import ollama as chat_api_ollama
from cterm.api import utils as chat_api_utils


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
