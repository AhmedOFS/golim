import json
import unittest

from cterm.llm_utils import chat_api


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

        result = chat_api._normalize_ollama_stream_response(response, deltas.append)

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

        result = chat_api._normalize_openai_stream_response(response, deltas.append)

        self.assertEqual(deltas, ["plan ", "step"])
        self.assertEqual(result["message"]["role"], "assistant")
        self.assertEqual(result["message"]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(
            result["message"]["tool_calls"][0]["function"]["arguments"],
            {"command": "pwd"},
        )


if __name__ == "__main__":
    unittest.main()
