"""Unit tests for the root-side openterm-authd token daemon."""

import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

AUTHD_SOURCE = Path(__file__).resolve().parent.parent / "packaging" / "openterm-authd.py"


def _load_authd_module():
    spec = importlib.util.spec_from_file_location("openterm_authd", AUTHD_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AuthDRequestTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_authd_module()
        self.authd = self.module.AuthD(socket_path="/tmp/unused.sock")

    def tearDown(self):
        self.authd._register_failures.clear()
        self.authd._tokens.clear()

    def test_ping(self):
        result = self.authd.handle("ping", {}, peer_uid=1000)
        self.assertTrue(result["ok"])

    def test_register_rejects_peer_mismatch(self):
        result = self.authd.handle(
            "register", {"user": "someoneelse", "password": "pw"}, peer_uid=1000
        )
        self.assertFalse(result["ok"])

    def test_register_rejects_invalid_request(self):
        for params in ({}, {"user": "ahmed"}, {"user": "ahmed", "password": ""}):
            result = self.authd.handle("register", params, peer_uid=1000)
            self.assertFalse(result["ok"], params)

    def test_register_rejects_wrong_password(self):
        with patch.object(self.authd, "_verify_password", return_value=False):
            result = self.authd.handle(
                "register", {"user": "ahmed", "password": "wrong"}, peer_uid=1000
            )
        self.assertFalse(result["ok"])
        self.assertEqual(self.authd._tokens, {})

    def test_register_mints_verifiable_and_revocable_token(self):
        with patch.object(self.authd, "_verify_password", return_value=True):
            registered = self.authd.handle(
                "register", {"user": "ahmed", "password": "sekret"}, peer_uid=1000
            )
        self.assertTrue(registered["ok"])
        token = registered["token"]
        self.assertTrue(token)

        verified = self.authd.handle(
            "verify", {"user": "ahmed", "token": token}, peer_uid=0
        )
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["valid"])

        # Tokens are stored only as digests, never in plaintext.
        raw = repr(self.authd._tokens)
        self.assertNotIn(token, raw)

        revoked = self.authd.handle(
            "revoke", {"user": "ahmed", "token": token}, peer_uid=0
        )
        self.assertTrue(revoked["revoked"])
        verified = self.authd.handle(
            "verify", {"user": "ahmed", "token": token}, peer_uid=0
        )
        self.assertFalse(verified["valid"])

    def test_verify_rejects_unknown_and_foreign_tokens(self):
        with patch.object(self.authd, "_verify_password", return_value=True):
            registered = self.authd.handle(
                "register", {"user": "ahmed", "password": "sekret"}, peer_uid=1000
            )
        token = registered["token"]

        verified = self.authd.handle(
            "verify", {"user": "ahmed", "token": "bogus"}, peer_uid=0
        )
        self.assertFalse(verified["valid"])
        verified = self.authd.handle(
            "verify", {"user": "mallory", "token": token}, peer_uid=0
        )
        self.assertFalse(verified["valid"])

    def test_non_root_verify_and_revoke_are_peer_bound(self):
        with patch.object(self.authd, "_verify_password", return_value=True):
            registered = self.authd.handle(
                "register", {"user": "ahmed", "password": "sekret"}, peer_uid=1000
            )
        token = registered["token"]

        def peer_entry(uid):
            name = "ahmed" if uid == 1000 else "mallory"
            return type("Entry", (), {"pw_name": name})()

        with patch.object(self.module.pwd, "getpwuid", side_effect=peer_entry):
            verified = self.authd.handle(
                "verify", {"user": "ahmed", "token": token}, peer_uid=1001
            )
            revoked = self.authd.handle(
                "revoke", {"user": "ahmed", "token": token}, peer_uid=1001
            )

        self.assertFalse(verified["valid"])
        self.assertFalse(revoked["revoked"])

    def test_expired_tokens_stop_verifying(self):
        with patch.object(self.authd, "_verify_password", return_value=True):
            registered = self.authd.handle(
                "register", {"user": "ahmed", "password": "sekret"}, peer_uid=1000
            )
        token = registered["token"]
        user_tokens = self.authd._tokens["ahmed"]
        digest = __import__("hashlib").sha256(token.encode()).digest()
        user_tokens[digest] = 0  # expired

        verified = self.authd.handle(
            "verify", {"user": "ahmed", "token": token}, peer_uid=0
        )
        self.assertFalse(verified["valid"])

        self.authd.cleanup_expired()
        self.assertNotIn(digest, self.authd._tokens.get("ahmed", {}))

    def test_register_is_rate_limited_after_repeated_failures(self):
        with patch.object(self.authd, "_verify_password", return_value=False):
            for _ in range(5):
                result = self.authd.handle(
                    "register", {"user": "ahmed", "password": "wrong"}, peer_uid=1000
                )
                self.assertFalse(result["ok"])

            with patch.object(self.authd, "_verify_password", return_value=True):
                result = self.authd.handle(
                    "register", {"user": "ahmed", "password": "sekret"}, peer_uid=1000
                )
        self.assertFalse(result["ok"])
        self.assertIn("too many failed attempts", result["error"])

    def test_unknown_method_reports_error(self):
        result = self.authd.handle("nope", {}, peer_uid=1000)
        self.assertFalse(result["ok"])

    def test_handle_line_parses_and_replies_with_id(self):
        line = json.dumps({
            "jsonrpc": "2.0", "id": 7, "method": "ping", "params": {},
        }).encode("utf-8")
        reply = self.authd._handle_line(line, peer_uid=1000)
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["result"], {"ok": True})

    def test_handle_line_survives_garbage(self):
        reply = self.authd._handle_line(b"not json", peer_uid=1000)
        self.assertEqual(reply["error"]["code"], -32700)
        self.assertIsNone(reply["id"])


class AuthDPasswordVerificationTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_authd_module()
        self.authd = self.module.AuthD(socket_path="/tmp/unused.sock")

    def test_verify_password_fails_for_unknown_user(self):
        self.assertFalse(self.authd._verify_password("no-such-user-xyz", "pw"))

    def test_verify_password_rejects_empty_password(self):
        self.assertFalse(self.authd._verify_password("root", ""))

    def test_verify_password_child_keeps_pipe_read_end_open(self):
        entry = self.module.pwd.getpwuid(os.getuid())

        def fake_run(*_args, **kwargs):
            # This runs in the forked child. A closed descriptor here means
            # openterm-authd cannot ever validate a password.
            os.fstat(kwargs["stdin"])
            return type("Result", (), {"returncode": 0})()

        with patch.object(self.module.pwd, "getpwnam", return_value=entry), \
             patch.object(self.module.os, "setgid"), \
             patch.object(self.module.os, "initgroups"), \
             patch.object(self.module.os, "setuid"), \
             patch.object(self.module.subprocess, "run", side_effect=fake_run):
            self.assertTrue(self.authd._verify_password(entry.pw_name, "pw"))


if __name__ == "__main__":
    unittest.main()
