"""Authd session-token handling for privileged wrapper execution."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from golim.toolset import auth_session
from golim.toolset.utils import bash_utils


class SudoSessionTokenTests(unittest.TestCase):
    def tearDown(self):
        auth_session.clear_session_token()

    def test_wrapper_installed_checks_wrapper_path(self):
        with patch.object(auth_session, "PRIVILEGED_WRAPPER", "/nonexistent/golim-privileged"):
            self.assertFalse(auth_session.wrapper_installed())

    def test_authd_available_checks_socket_presence(self):
        with patch.object(auth_session, "AUTHD_SOCKET_PATH", "/nonexistent/authd.sock"):
            self.assertFalse(auth_session.authd_available())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "authd.sock"
            path.touch()
            with patch.object(auth_session, "AUTHD_SOCKET_PATH", str(path)):
                self.assertTrue(auth_session.authd_available())

    def test_token_cache_round_trip_and_zero_on_clear(self):
        auth_session.set_session_token("sekret-token")
        self.assertTrue(auth_session.has_session_token())
        self.assertEqual(auth_session.session_token(), "sekret-token")

        auth_session.clear_session_token()
        self.assertFalse(auth_session.has_session_token())
        self.assertIsNone(auth_session.session_token())

    def test_set_session_token_replaces_previous_secret(self):
        auth_session.set_session_token("first")
        auth_session.set_session_token("second")

        self.assertEqual(auth_session.session_token(), "second")

    def test_register_session_returns_authd_token(self):
        with patch.object(auth_session, "authd_available", return_value=True), \
             patch.object(auth_session, "_authd_request", return_value={"ok": True, "token": "tok"}) as request_mock:
            token = auth_session.register_session("sekret")

        self.assertEqual(token, "tok")
        request_mock.assert_called_once_with(
            "register", {"user": auth_session.current_user(), "password": "sekret"}
        )

    def test_register_session_caches_nothing_itself(self):
        with patch.object(auth_session, "authd_available", return_value=True), \
             patch.object(auth_session, "_authd_request", return_value={"ok": True, "token": "tok"}):
            auth_session.register_session("sekret")

        self.assertFalse(auth_session.has_session_token())

    def test_register_session_rejects_bad_password_and_authd_failure(self):
        with patch.object(auth_session, "_authd_request", return_value={"ok": False}):
            self.assertIsNone(auth_session.register_session("wrong"))
        with patch.object(auth_session, "_authd_request", side_effect=RuntimeError("authd down")):
            self.assertIsNone(auth_session.register_session("sekret"))
        with patch.object(auth_session, "authd_available", return_value=False):
            self.assertIsNone(auth_session.register_session("sekret"))

    def test_has_valid_session_token_checks_authd(self):
        auth_session.set_session_token("sekret-token")
        with patch.object(auth_session, "authd_available", return_value=True), \
             patch.object(auth_session, "_authd_request", return_value={"ok": True, "valid": True}) as request_mock:
            self.assertTrue(auth_session.has_valid_session_token())

        request_mock.assert_called_once_with(
            "verify", {"user": auth_session.current_user(), "token": "sekret-token"}
        )

    def test_has_valid_session_token_rejects_authd_failure(self):
        auth_session.set_session_token("sekret-token")
        with patch.object(auth_session, "authd_available", return_value=True), \
             patch.object(auth_session, "_authd_request", side_effect=OSError("down")):
            self.assertFalse(auth_session.has_valid_session_token())


class BashSessionTokenTests(unittest.TestCase):
    """The bash tool fails closed without a session token and forwards it
    to privileged wrapper invocations via the environment."""

    def _privileged_env(self, tmp):
        """A wrapper mirroring the installed one: token check, whitelist
        check, then exec — so tests exercise the real exec path."""
        wrapper = Path(tmp) / "golim-privileged"
        wrapper.write_text(
            "#!/bin/bash\n"
            "set -e\n"
            'if [ -z "${GOLIM_SESSION_TOKEN:-}" ]; then\n'
            '  echo "no token" >&2\n'
            "  exit 3\n"
            "fi\n"
            'if [ "$GOLIM_SESSION_TOKEN" != "sekret-token" ]; then\n'
            '  echo "bad token" >&2\n'
            "  exit 4\n"
            "fi\n"
            'BINARY="$(readlink -f "$1")"\n'
            'REQUESTED="$1"\n'
            "shift\n"
            'WHITELIST="${GOLIM_PRIVILEGED_WHITELIST:?}"\n'
            'FOUND=""\n'
            'while IFS= read -r allowed || [ -n "$allowed" ]; do\n'
            '  [ -n "$allowed" ] || continue\n'
            '  allowed="$(readlink -f "$allowed")"\n'
            '  if [ "$BINARY" = "$allowed" ]; then\n'
            '    FOUND="$allowed"\n'
            "    break\n"
            "  fi\n"
            'done < "$WHITELIST"\n'
            'if [ -n "$FOUND" ]; then\n'
            '  exec "$REQUESTED" "$@"\n'
            "fi\n"
            'echo "golim-privileged: binary not allowed: $BINARY" >&2\n'
            "exit 1\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        whitelist = Path(tmp) / "privileged_whitelist"
        return wrapper, whitelist

    def test_restricted_mode_fails_closed_without_token(self):
        def fake_stream_command(argv, cmd_str, results, suppress_stderr, session_token=None):
            raise AssertionError("privileged command must not execute without a token")
            yield

        with tempfile.TemporaryDirectory() as tmp:
            wrapper, whitelist = self._privileged_env(tmp)
            with patch.dict(os.environ, {"GOLIM_PRIVILEGED_WHITELIST": str(whitelist)}), \
                 patch.object(bash_utils, "PRIVILEGED_WRAPPER", str(wrapper)), \
                 patch.object(bash_utils, "_stream_command", fake_stream_command):
                result = bash_utils._run_shell(
                    "sudo test -d /",
                    approve_privileged=lambda info: True,
                    session_token=None,
                    always_approve=True,
                )

        self.assertFalse(result["ok"], result)
        self.assertIn("sudo authentication required", result["error"])

    def test_restricted_mode_routes_non_interactively_with_env_token(self):
        calls = []

        def fake_stream_command(argv, cmd_str, results, suppress_stderr, session_token=None):
            calls.append((argv, session_token))
            return {"command": cmd_str, "stdout": "ok", "stderr": "", "returncode": 0}, None
            yield

        with tempfile.TemporaryDirectory() as tmp:
            wrapper, whitelist = self._privileged_env(tmp)
            with patch.dict(os.environ, {"GOLIM_PRIVILEGED_WHITELIST": str(whitelist)}), \
                 patch.object(bash_utils, "PRIVILEGED_WRAPPER", str(wrapper)), \
                 patch.object(bash_utils, "_stream_command", fake_stream_command):
                result = bash_utils._run_shell(
                    "sudo test -d /",
                    approve_privileged=lambda info: True,
                    session_token="sekret-token",
                    always_approve=True,
                )

        resolved_test = bash_utils.shutil.which("test")
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            calls[0],
            (
                ["/bin/bash", "-c", f"sudo -n {wrapper} {resolved_test} -d /"],
                "sekret-token",
            ),
        )

    def test_sudo_use_requests_auth_when_session_token_is_missing(self):
        provider_calls = []

        def fake_stream_command(argv, cmd_str, results, suppress_stderr, session_token=None):
            return {"command": cmd_str, "stdout": "ok", "stderr": "", "returncode": 0}, None
            yield

        def provider():
            provider_calls.append(True)
            return "sekret-token"

        with tempfile.TemporaryDirectory() as tmp:
            wrapper, whitelist = self._privileged_env(tmp)
            with patch.dict(os.environ, {"GOLIM_PRIVILEGED_WHITELIST": str(whitelist)}), \
                 patch.object(bash_utils, "PRIVILEGED_WRAPPER", str(wrapper)), \
                 patch.object(bash_utils, "_stream_command", fake_stream_command):
                result = bash_utils._run_shell(
                    "sudo test -d /",
                    approve_privileged=lambda info: True,
                    session_token=provider,
                    always_approve=True,
                )

        self.assertTrue(result["ok"], result)
        self.assertEqual(provider_calls, [True])

    def test_restricted_mode_callable_provider_not_called_for_plain_commands(self):
        """Regression: plain commands must never trigger the provider or
        enter the privileged path."""
        seen_tokens = []

        def fake_stream_command(argv, cmd_str, results, suppress_stderr, session_token=None):
            seen_tokens.append(session_token)
            return {"command": cmd_str, "stdout": "hello", "stderr": "", "returncode": 0}, None
            yield

        def provider():
            raise AssertionError("provider must not be called for plain commands")

        with tempfile.TemporaryDirectory() as tmp:
            wrapper, whitelist = self._privileged_env(tmp)
            with patch.dict(os.environ, {"GOLIM_PRIVILEGED_WHITELIST": str(whitelist)}), \
                 patch.object(bash_utils, "PRIVILEGED_WRAPPER", str(wrapper)), \
                 patch.object(bash_utils, "_stream_command", fake_stream_command):
                result = bash_utils._run_shell(
                    "echo hello", session_token=provider, always_approve=True,
                )

        self.assertTrue(result["ok"], result)
        self.assertEqual(seen_tokens, [None])

    def test_restricted_mode_callable_provider_returning_none_fails_closed(self):
        def fake_stream_command(argv, cmd_str, results, suppress_stderr, session_token=None):
            raise AssertionError("must not execute without credentials")
            yield

        with tempfile.TemporaryDirectory() as tmp:
            wrapper, whitelist = self._privileged_env(tmp)
            with patch.dict(os.environ, {"GOLIM_PRIVILEGED_WHITELIST": str(whitelist)}), \
                 patch.object(bash_utils, "PRIVILEGED_WRAPPER", str(wrapper)), \
                 patch.object(bash_utils, "_stream_command", fake_stream_command):
                result = bash_utils._run_shell(
                    "sudo test -d /",
                    approve_privileged=lambda info: True,
                    session_token=lambda: None,
                    always_approve=True,
                )

        self.assertFalse(result["ok"], result)
        self.assertIn("sudo authentication required", result["error"])

    def test_stream_subprocess_forwards_token_via_environment(self):
        resolved_env = bash_utils.shutil.which("env")

        generator = bash_utils._stream_subprocess(
            [resolved_env], "env", [], suppress_stderr=False,
            use_pty=False, session_token="sekret-token",
        )
        _, entry, err = None, None, None
        while True:
            try:
                next(generator)
            except StopIteration as stop:
                entry, err = stop.value
                break

        self.assertIsNone(err)
        self.assertIn(f"{bash_utils.TOKEN_ENV_VAR}=sekret-token", entry["stdout"])

    def test_stream_subprocess_omits_token_env_for_plain_calls(self):
        resolved_env = bash_utils.shutil.which("env")

        generator = bash_utils._stream_subprocess(
            [resolved_env], "env", [], suppress_stderr=False,
            use_pty=False, session_token=None,
        )
        entry, err = None, None
        while True:
            try:
                next(generator)
            except StopIteration as stop:
                entry, err = stop.value
                break

        self.assertIsNone(err)
        self.assertNotIn(bash_utils.TOKEN_ENV_VAR, entry["stdout"])


if __name__ == "__main__":
    unittest.main()
