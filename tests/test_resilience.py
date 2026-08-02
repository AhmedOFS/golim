import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from cterm.api.retry import with_retries
from cterm.core.agent import ToolAgent
from cterm.core.mcp_client import FastMCPClient
from cterm.core.runtime import Runtime
from cterm.core.utils import get_socket_path


class _UI:
    def status(self, *_): pass
    def clear_status(self): pass
    def thinking_delta(self, *_): pass
    def thinking_complete(self, *_): pass
    def status(self, *_): pass
    def clear_status(self, *_): pass


class ResilienceTests(unittest.TestCase):
    def test_socket_path_uses_effective_uid_username(self):
        with patch("cterm.core.utils.pwd.getpwuid") as lookup, \
             patch("cterm.core.utils.os.getuid", return_value=123):
            lookup.return_value.pw_name = "service-user"
            self.assertEqual(get_socket_path(), Path("/tmp/cterm_mcp_service-user.sock"))
            lookup.assert_called_once_with(123)

    def test_request_failures_retry_three_times(self):
        operation = MagicMock(side_effect=requests.exceptions.ConnectionError("offline"))
        with patch("cterm.api.retry.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                with_retries(operation, provider="Example")
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_client_close_aborts_all_active_sockets(self):
        client = FastMCPClient("/tmp/unused.sock")
        first, second = MagicMock(), MagicMock()
        client._active_sockets.update({first, second})

        client.close()

        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertEqual(client._active_sockets, set())

    def test_runtime_falls_back_when_user_systemd_is_unavailable(self):
        runtime = Runtime(model="main")
        socket_path = MagicMock()
        socket_path.exists.side_effect = [False, True]
        with patch("cterm.core.runtime.get_socket_path", return_value=socket_path), \
             patch("cterm.core.runtime.subprocess.run", side_effect=subprocess_error()), \
             patch("cterm.core.runtime.subprocess.Popen") as popen, \
             patch("cterm.core.runtime.time.sleep"):
            runtime.ensure_mcp_server()
        self.assertTrue(popen.called)

    def test_long_tool_termination_requires_explicit_model_schema(self):
        agent = ToolAgent("model", ui=_UI())
        agent._active_messages = [{"role": "system", "content": "system"}]
        agent._chat_with_optional_thinking = MagicMock(return_value={"message": {"content": json.dumps({"action": "terminate"})}})
        self.assertTrue(agent._long_tool_decision("bash", {"command": "sleep 60"}, []))

        agent._chat_with_optional_thinking.return_value = {"message": {"content": "not json"}}
        self.assertFalse(agent._long_tool_decision("bash", {}, []))

    def test_long_tool_policy_closes_transport_only_after_terminate_decision(self):
        agent = ToolAgent("model", ui=_UI())
        agent.LONG_TOOL_NOTICE_SECONDS = 0
        agent.mcp_client = MagicMock()
        agent._long_tool_decision = MagicMock(return_value=True)

        def slow_call(_args):
            time.sleep(0.02)
            return {"ok": False, "error": "cancelled"}

        result = agent._call_tool_with_long_running_policy(
            "bash", slow_call, {"command": "sleep 60"}, "bash", [],
        )

        self.assertFalse(result["ok"])
        agent.mcp_client.close.assert_called_once()

    def test_hard_cancel_releases_agent_while_llm_request_is_blocked(self):
        hard_cancel = threading.Event()
        release_request = threading.Event()
        agent = ToolAgent("model", ui=_UI(), should_hard_cancel=hard_cancel.is_set)

        def blocked_chat(*_args, **_kwargs):
            release_request.wait(2)
            return {"message": {"role": "assistant", "content": "late answer"}}

        timer = threading.Timer(0.05, hard_cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch("cterm.core.agent.chat_with_model_api", side_effect=blocked_chat):
                with self.assertRaises(InterruptedError):
                    agent._chat_for_next_action([{"role": "user", "content": "hi"}])
        finally:
            release_request.set()
            timer.cancel()
        self.assertLess(time.monotonic() - started, 0.5)


def subprocess_error():
    return __import__("subprocess").CalledProcessError(1, ["systemctl"])


if __name__ == "__main__":
    unittest.main()
