import requests

from ..config import get_config

from ..vars import (
    EXA_MCP_URL,
    MAX_NUM_RESULTS,
    MAX_RESPONSE_BYTES,
    NO_RESULTS,
    PARALLEL_MCP_URL,
)


def _response_body_utf8(response) -> str:
    """Read an HTTP body as UTF-8, regardless of a bad server charset."""
    body = getattr(response, "content", None)
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8")
    return response.text


def _websearch_mcp_call(url: str, tool: str, args: dict, headers: dict | None = None) -> tuple[str | None, str | None]:
    """
    Call an MCP tool over HTTP.

    Returns (text, None) on success, or (None, error_message) on failure.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json, text/event-stream")
    req_headers.setdefault("Content-Type", "application/json")
    try:
        resp = requests.post(
            url,
            json=payload,
            headers=req_headers,
            timeout=25,
        )
        resp.raise_for_status()
        body = _response_body_utf8(resp)
    except requests.exceptions.HTTPError as exc:
        status = resp.status_code if isinstance(exc, requests.exceptions.HTTPError) and hasattr(exc, 'response') and exc.response is not None else 0
        detail = resp.text[:200] if hasattr(exc, 'response') and exc.response is not None else str(exc)
        return None, f"HTTP {status}: {detail}"
    except requests.exceptions.RequestException as exc:
        return None, str(exc)

    if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return None, f"Response exceeded {MAX_RESPONSE_BYTES} bytes"

    text = _parse_mcp_response(body)
    if text is None:
        return None, "Could not parse search results from MCP response"
    return text, None


def _parse_mcp_response(body: str) -> str | None:
    """
    Parse an MCP JSON-RPC response body.

    Accepts both direct JSON and SSE/NDJSON frames (lines starting with 'data: ').
    Returns the first text content block found, or None.
    """
    import json

    def _try_extract(payload: str) -> str | None:
        trimmed = payload.strip()
        if not trimmed.startswith("{"):
            return None
        try:
            parsed = json.loads(trimmed)
        except json.JSONDecodeError:
            return None
        content = parsed.get("result", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("text"):
                    return item["text"]
        return None

    body = body.strip()
    direct = _try_extract(body)
    if direct:
        return direct

    for line in body.splitlines():
        if line.startswith("data: "):
            found = _try_extract(line[6:])
            if found:
                return found

    return None


def _websearch_provider() -> str:
    """Return the configured web search provider, defaulting to 'exa'."""
    return get_config().websearch_provider


def _exa_api_key() -> str | None:
    """Read Exa API key from config, then env var."""
    return get_config().exa_api_key


def _parallel_api_key() -> str | None:
    """Read Parallel API key from config, then env var."""
    return get_config().parallel_api_key
