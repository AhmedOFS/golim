#!/usr/bin/env python3
"""golim privileged-session token daemon.

A small root-side service that anchors the privileged wrapper's access
control. The user authenticates sudo once per session (a single password
entry at golim start); golim-authd verifies that password via
sudo/PAM, mints a random session token, and answers token-verification
requests from the privileged wrapper. Sudo runs the wrapper through a
NOPASSWD rule, so golim-authd is the only gate — it must fail closed.

Protocol: newline-delimited JSON-RPC-ish frames over a unix socket.
  register {user, password} -> {ok, token}   (peer uid must match user)
  verify   {user, token}    -> {ok, valid}
  revoke   {user, token}    -> {ok}
  ping                      -> {ok}

Passwords and tokens are never logged. Tokens are stored only as SHA-256
digests with a TTL. Registration is rate-limited per peer uid.

This script is installed root-owned and runs on the system Python with
stdlib only; it must not import the golim package.
"""

import hashlib
import json
import os
import pwd
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time

SOCKET_PATH = "/run/golim/authd.sock"
TOKEN_TTL_SECONDS = 12 * 3600
REGISTER_FAILURE_LIMIT = 5
REGISTER_BAN_SECONDS = 300
REGISTER_RATE_WINDOW_SECONDS = 3600
SUDO_VALIDATE_TIMEOUT_SECONDS = 60
CLEANUP_INTERVAL_SECONDS = 300


def peer_credentials(conn):
    """Return the peer's (pid, uid, gid) for a connected unix socket."""
    creds = conn.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    return struct.unpack("3i", creds)


class AuthD:
    def __init__(self, socket_path=SOCKET_PATH, token_ttl=TOKEN_TTL_SECONDS):
        self.socket_path = socket_path
        self.token_ttl = token_ttl
        self._lock = threading.Lock()
        self._register_lock = threading.Lock()
        # user -> {token_sha256: expiry}
        self._tokens = {}
        # uid -> [failure_count, banned_until]
        self._register_failures = {}

    # -- password verification ------------------------------------------------

    def _verify_password(self, user, password):
        """Validate the password through sudo itself, as the target user.

        Runs `sudo -S -k -v` in a child that has dropped to the user's uid;
        `-k` forces a fresh password check instead of accepting any existing
        timestamp ticket. Returns True only when sudo accepts the password.
        """
        try:
            entry = pwd.getpwnam(user)
        except KeyError:
            return False

        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(write_fd)
            status = 2
            try:
                os.setgid(entry.pw_gid)
                os.initgroups(user, entry.pw_gid)
                os.setuid(entry.pw_uid)
                proc = subprocess.run(
                    ["sudo", "-S", "-k", "-p", "", "-v"],
                    stdin=read_fd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=SUDO_VALIDATE_TIMEOUT_SECONDS,
                )
                status = 0 if proc.returncode == 0 else 1
            except BaseException:
                status = 2
            finally:
                os._exit(status)

        try:
            os.close(read_fd)
            os.write(write_fd, (password + "\n").encode("utf-8"))
        except OSError:
            pass
        os.close(write_fd)
        _, wait_status = os.waitpid(child, 0)
        return os.waitstatus_to_exitcode(wait_status) == 0

    # -- rate limiting ---------------------------------------------------------

    def _register_allowed(self, uid, now):
        state = self._register_failures.get(uid)
        if state is None:
            return True
        failures, banned_until, window_start = state
        if banned_until and now < banned_until:
            return False
        if now - window_start > REGISTER_RATE_WINDOW_SECONDS:
            state[0] = 0
            state[2] = now
        return True

    def _register_failed(self, uid, now):
        state = self._register_failures.setdefault(uid, [0, 0.0, now])
        state[0] += 1
        state[2] = now
        if state[0] >= REGISTER_FAILURE_LIMIT:
            state[1] = now + REGISTER_BAN_SECONDS
            state[0] = 0

    @staticmethod
    def _peer_can_use_user(user, peer_uid):
        if peer_uid == 0:
            return True
        try:
            return pwd.getpwuid(peer_uid).pw_name == user
        except KeyError:
            return False

    # -- request handlers -------------------------------------------------------

    def handle(self, method, params, peer_uid):
        if method == "ping":
            return {"ok": True}

        if method == "register":
            user = str(params.get("user") or "")
            password = params.get("password")
            if not user or not isinstance(password, str) or not password:
                return {"ok": False, "error": "invalid register request"}
            try:
                entry = pwd.getpwuid(peer_uid)
                peer_name = entry.pw_name
            except KeyError:
                return {"ok": False, "error": "unknown peer"}
            if peer_name != user:
                return {"ok": False, "error": "register denied for peer user"}

            now = time.time()
            with self._register_lock:
                if not self._register_allowed(peer_uid, now):
                    return {"ok": False, "error": "too many failed attempts; try later"}

            if not self._verify_password(user, password):
                with self._register_lock:
                    self._register_failed(peer_uid, now)
                return {"ok": False, "error": "sudo authentication failed"}

            token = secrets.token_urlsafe(32)
            with self._lock:
                self._tokens.setdefault(user, {})[
                    hashlib.sha256(token.encode()).digest()
                ] = now + self.token_ttl
                self._register_failures.pop(peer_uid, None)
            return {"ok": True, "token": token}

        if method == "verify":
            user = str(params.get("user") or "")
            token = str(params.get("token") or "")
            if not self._peer_can_use_user(user, peer_uid):
                return {"ok": False, "valid": False}
            digest = hashlib.sha256(token.encode()).digest()
            now = time.time()
            with self._lock:
                expiry = self._tokens.get(user, {}).get(digest)
                valid = expiry is not None and expiry > now
            return {"ok": True, "valid": bool(valid)}

        if method == "revoke":
            user = str(params.get("user") or "")
            token = str(params.get("token") or "")
            if not self._peer_can_use_user(user, peer_uid):
                return {"ok": False, "revoked": False}
            digest = hashlib.sha256(token.encode()).digest()
            with self._lock:
                users_tokens = self._tokens.get(user, {})
                existed = users_tokens.pop(digest, None) is not None
            return {"ok": True, "revoked": existed}

        return {"ok": False, "error": f"unknown method: {method}"}

    def cleanup_expired(self):
        now = time.time()
        with self._lock:
            for user in list(self._tokens):
                users_tokens = self._tokens[user]
                for digest in [
                    d for d, expiry in users_tokens.items() if expiry <= now
                ]:
                    del users_tokens[digest]
                if not users_tokens:
                    del self._tokens[user]

    # -- server loop --------------------------------------------------------------

    def serve_forever(self):
        directory = os.path.dirname(self.socket_path)
        os.makedirs(directory, mode=0o755, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o666)
        server.listen(16)
        print(f"golim-authd listening on {self.socket_path}", flush=True)

        def cleanup_loop():
            while True:
                time.sleep(CLEANUP_INTERVAL_SECONDS)
                self.cleanup_expired()

        threading.Thread(target=cleanup_loop, daemon=True).start()

        try:
            while True:
                conn, _ = server.accept()
                threading.Thread(
                    target=self._handle_connection, args=(conn,), daemon=True
                ).start()
        finally:
            server.close()
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)

    def _handle_connection(self, conn):
        try:
            with conn:
                buf = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        _, pid, uid = peer_credentials(conn)
                        reply = self._handle_line(line, uid)
                        conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except OSError:
            return

    def _handle_line(self, line, peer_uid):
        try:
            request = json.loads(line.decode("utf-8"))
            method = request.get("method", "")
            params = request.get("params") or {}
            request_id = request.get("id")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        try:
            result = self.handle(method, params, peer_uid)
        except Exception as exc:  # never leak a broken request into a crash
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": f"authd error: {exc}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    authd = AuthD()
    try:
        authd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
