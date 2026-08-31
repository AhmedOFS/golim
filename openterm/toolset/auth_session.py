"""Session-token client for openterm-authd.

The privileged wrapper runs through a NOPASSWD sudoers rule and gates
access on a session token issued by the root-side openterm-authd daemon.
The user may be proactively offered sudo authentication at app start. If
that is disabled or declined, or the token later expires, the MCP server
asks again when a sudo command needs a token. The server registers the
password with openterm-authd (which verifies it via sudo/PAM as the user),
caches the returned token in memory for the session, and the wrapper
verifies it per invocation. The token never appears on disk, in argv, or
in logs; openterm-authd stores only its SHA-256 digest.
"""

import json
import os
import socket
import threading

from openterm.toolset.vars import PRIVILEGED_WRAPPER

AUTHD_SOCKET_PATH = "/run/openterm/authd.sock"
AUTHD_TIMEOUT_SECONDS = 60

_cached_token: bytearray | None = None
_token_lock = threading.RLock()


def current_user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER", "")


def wrapper_installed() -> bool:
    return os.path.isfile(PRIVILEGED_WRAPPER)


def authd_available() -> bool:
    return os.path.exists(AUTHD_SOCKET_PATH)


def _authd_request(method, params):
    """Send one request to openterm-authd and return its result."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(AUTHD_TIMEOUT_SECONDS)
    try:
        sock.connect(AUTHD_SOCKET_PATH)
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("No response from openterm-authd")
            buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    finally:
        sock.close()
    if "error" in reply:
        error = reply["error"]
        message = error.get("message", "unknown error") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"authd error: {message}")
    result = reply.get("result", {})
    if not isinstance(result, dict):
        raise ValueError("invalid authd response")
    return result


def register_session(password: str) -> str | None:
    """Verify the password with openterm-authd and return the session token."""
    if not password or not authd_available():
        return None
    try:
        result = _authd_request(
            "register", {"user": current_user(), "password": password}
        )
    except (OSError, RuntimeError, json.JSONDecodeError, ValueError):
        return None
    token = result.get("token")
    return token if isinstance(token, str) and token else None


def has_session_token() -> bool:
    with _token_lock:
        return _cached_token is not None


def session_token() -> str | None:
    with _token_lock:
        if _cached_token is None:
            return None
        return _cached_token.decode("utf-8")


def has_valid_session_token() -> bool:
    """Check the cached token against openterm-authd without exposing it."""
    token = session_token()
    if not token or not authd_available():
        return False
    try:
        result = _authd_request(
            "verify", {"user": current_user(), "token": token}
        )
    except (OSError, RuntimeError, json.JSONDecodeError, ValueError):
        return False
    return bool(result.get("ok") and result.get("valid"))


def set_session_token(token: str) -> None:
    global _cached_token
    with _token_lock:
        clear_session_token()
        _cached_token = bytearray(token.encode("utf-8"))


def clear_session_token() -> None:
    """Zero the in-memory token; best effort on mutable buffers."""
    global _cached_token
    with _token_lock:
        if _cached_token is not None:
            for index in range(len(_cached_token)):
                _cached_token[index] = 0
        _cached_token = None
