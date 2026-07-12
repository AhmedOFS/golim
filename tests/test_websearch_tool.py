import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

from cterm.mcp.tools_mcp import websearch


SAMPLE_EXA_RESPONSE = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "content": [
            {
                "type": "text",
                "text": "1. **Result Title** - A description of the result. [Source](https://example.com)"
            }
        ]
    },
})

SAMPLE_PARALLEL_RESPONSE = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "content": [
            {
                "type": "text",
                "text": "Search results for query: test\n1. Example result from parallel."
            }
        ]
    },
})


class WebSearchToolTests(unittest.TestCase):

    @patch("cterm.mcp.utils.web_utils._read_cterm_config", return_value={})
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_exa_happy_path(self, mock_post, mock_config):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SAMPLE_EXA_RESPONSE
        mock_post.return_value = mock_response

        result = websearch("test query")

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "exa")
        self.assertIn("Result Title", result["text"])

        call_args = mock_post.call_args
        self.assertEqual(call_args[0][0], "https://mcp.exa.ai/mcp")
        payload = call_args[1]["json"]
        self.assertEqual(payload["method"], "tools/call")
        self.assertEqual(payload["params"]["name"], "web_search_exa")
        self.assertEqual(payload["params"]["arguments"]["query"], "test query")
        self.assertEqual(payload["params"]["arguments"]["numResults"], 8)
        self.assertEqual(payload["params"]["arguments"]["type"], "auto")
        self.assertEqual(payload["params"]["arguments"]["livecrawl"], "fallback")

    @patch("cterm.mcp.utils.web_utils._read_cterm_config")
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_exa_with_api_key(self, mock_post, mock_config):
        mock_config.return_value = {"exa_api_key": "test-key-123"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SAMPLE_EXA_RESPONSE
        mock_post.return_value = mock_response

        result = websearch("test query")

        self.assertTrue(result["ok"])
        call_url = mock_post.call_args[0][0]
        self.assertIn("exaApiKey=test-key-123", call_url)

    @patch("cterm.mcp.utils.web_utils._read_cterm_config", return_value={})
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_exa_with_custom_params(self, mock_post, mock_config):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SAMPLE_EXA_RESPONSE
        mock_post.return_value = mock_response

        websearch("test query", num_results=5, type="fast", livecrawl="preferred")

        payload = mock_post.call_args[1]["json"]
        args = payload["params"]["arguments"]
        self.assertEqual(args["numResults"], 5)
        self.assertEqual(args["type"], "fast")
        self.assertEqual(args["livecrawl"], "preferred")

    @patch("cterm.mcp.utils.web_utils._read_cterm_config", return_value={})
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_clamps_num_results(self, mock_post, mock_config):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SAMPLE_EXA_RESPONSE
        mock_post.return_value = mock_response

        websearch("test", num_results=100)
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["params"]["arguments"]["numResults"], 20)

        websearch("test", num_results=0)
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["params"]["arguments"]["numResults"], 1)

    @patch("cterm.mcp.utils.web_utils._read_cterm_config")
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_exa_failure_returns_no_results(self, mock_post, mock_config):
        mock_config.return_value = {}
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "unauthorized"
        mock_response.raise_for_status.side_effect = (
            __import__("requests").exceptions.HTTPError(response=mock_response)
        )
        mock_post.return_value = mock_response

        from cterm.mcp.tools_mcp import NO_RESULTS
        result = websearch("test")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], NO_RESULTS)

    @patch("cterm.mcp.utils.web_utils._read_cterm_config", return_value={})
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_network_error_returns_no_results(self, mock_post, mock_config):
        mock_post.side_effect = (
            __import__("requests").exceptions.ConnectionError("connection failed")
        )

        from cterm.mcp.tools_mcp import NO_RESULTS
        result = websearch("test")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], NO_RESULTS)

    @patch("cterm.mcp.utils.web_utils._read_cterm_config")
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_parallel_provider(self, mock_post, mock_config):
        mock_config.return_value = {"websearch_provider": "parallel"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SAMPLE_PARALLEL_RESPONSE
        mock_post.return_value = mock_response

        result = websearch("test query")

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "parallel")
        self.assertIn("Example result from parallel", result["text"])

        call_args = mock_post.call_args
        self.assertEqual(call_args[0][0], "https://search.parallel.ai/mcp")
        payload = call_args[1]["json"]
        self.assertEqual(payload["params"]["name"], "web_search")
        self.assertEqual(payload["params"]["arguments"]["objective"], "test query")

    @patch("cterm.mcp.utils.web_utils._read_cterm_config")
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_parallel_with_api_key(self, mock_post, mock_config):
        mock_config.return_value = {
            "websearch_provider": "parallel",
            "parallel_api_key": "par-key-456",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SAMPLE_PARALLEL_RESPONSE
        mock_post.return_value = mock_response

        websearch("test query")

        headers = mock_post.call_args[1].get("headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer par-key-456")

    @patch("cterm.mcp.utils.web_utils._read_cterm_config", return_value={})
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_parallel_failure_returns_no_results(self, mock_post, mock_config):
        mock_post.side_effect = (
            __import__("requests").exceptions.Timeout("timed out")
        )

        from cterm.mcp.tools_mcp import NO_RESULTS
        result = websearch("test")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], NO_RESULTS)

    @patch("cterm.mcp.utils.web_utils._read_cterm_config", return_value={})
    @patch("cterm.mcp.utils.web_utils.requests.post")
    def test_parallel_failure_explicit(self, mock_post, mock_config):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "internal error"
        mock_response.raise_for_status.side_effect = (
            __import__("requests").exceptions.HTTPError(response=mock_response)
        )
        mock_post.return_value = mock_response

        from cterm.mcp.tools_mcp import NO_RESULTS
        result = websearch("test")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], NO_RESULTS)


if __name__ == "__main__":
    unittest.main()
