import json
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import requests

from cterm.api.retry import with_retries
from cterm.core.agent import ToolAgent
from cterm.core.agent_events import active_agent_events_handler
from cterm.core.mcp_client import FastMCPClient
from cterm.core.runtime import Runtime
from cterm.core.utils import get_socket_path
from cterm.mcp.utils import bash_utils


class _UI:
    def status(self, *_): pass
    def clear_status(self): pass
    def thinking_delta(self, *_): pass
    def thinking_complete(self, *_): pass
    def status(self, *_): pass
    def clear_status(self, *_): pass


class ResilienceTests(unittest.TestCase):
    def test_agent_uses_active_ui_context_when_not_passed(self):
        ui = _UI()
        token = active_agent_events_handler.set(ui)
        try:
            agent = ToolAgent("model")
        finally:
            active_agent_events_handler.reset(token)

        self.assertIs(agent.ui, ui)

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
        with patch("cterm.core.runtime.get_socket_path", return_value=socket_path), \
             patch("cterm.core.runtime.subprocess.run", side_effect=subprocess_error()), \
             patch("cterm.core.runtime.subprocess.Popen") as popen, \
             patch.object(runtime, "_socket_is_ready", side_effect=[False, True]), \
             patch("cterm.core.runtime.time.sleep"):
            runtime.ensure_mcp_server()
        self.assertTrue(popen.called)

    def test_runtime_restarts_active_service_when_socket_is_stale(self):
        runtime = Runtime(model="main")
        socket_path = MagicMock()
        with patch("cterm.core.runtime.get_socket_path", return_value=socket_path), \
             patch("cterm.core.runtime.subprocess.run") as run, \
             patch.object(runtime, "_socket_is_ready", return_value=False), \
             patch.object(runtime, "_wait_for_socket", side_effect=[False, True]):
            runtime.ensure_mcp_server()

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["systemctl", "--user", "start", "cterm-mcp.service"],
                ["systemctl", "--user", "restart", "cterm-mcp.service"],
            ],
        )

    def test_interrupted_pty_stream_terminates_process_group(self):
        started = []
        real_popen = subprocess.Popen

        def tracking_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            started.append(proc)
            return proc

        with patch("cterm.mcp.utils.bash_utils.subprocess.Popen", side_effect=tracking_popen):
            stream = bash_utils._stream_subprocess(
                ["/bin/sh", "-c", "printf 'ready\\n'; sleep 30"],
                "test command",
                [],
                timeout=30,
                suppress_stderr=False,
                use_pty=True,
            )
            self.assertEqual(next(stream)["line"], "ready")
            started_at = time.monotonic()
            stream.close()

        self.assertLess(time.monotonic() - started_at, 3)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())

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

    def test_long_tool_policy_rechecks_after_keep_decision(self):
        agent = ToolAgent("model", ui=_UI())
        agent.LONG_TOOL_NOTICE_SECONDS = 0
        agent.mcp_client = MagicMock()
        agent._long_tool_decision = MagicMock(side_effect=[False, True])
        release_tool = threading.Event()

        def blocked_tool(_args):
            release_tool.wait(2)
            return {"ok": True}

        timer = threading.Timer(0.05, release_tool.set)
        timer.start()
        try:
            result = agent._call_tool_with_long_running_policy(
                "tool", blocked_tool, {}, "tool", [],
            )
        finally:
            release_tool.set()
            timer.cancel()

        self.assertTrue(result["ok"])
        self.assertEqual(agent._long_tool_decision.call_count, 2)
        agent.mcp_client.close.assert_called_once()

    def test_long_tool_policy_updates_status_after_keep_decision(self):
        ui = MagicMock()
        agent = ToolAgent("model", ui=ui)
        agent.LONG_TOOL_NOTICE_SECONDS = 0
        release_tool = threading.Event()

        def blocked_tool(_args):
            release_tool.wait(1)
            return {"ok": True}

        def keep_waiting(*_args):
            release_tool.set()
            return False

        agent._long_tool_decision = MagicMock(side_effect=keep_waiting)
        label = "$ sudo apt install example"
        result = agent._call_tool_with_long_running_policy(
            label,
            blocked_tool,
            {"command": "sudo apt install example"},
            "bash",
            [],
        )

        self.assertTrue(result["ok"])
        ui.status.assert_any_call(f"Continuing {label}")

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

    def test_hard_cancel_releases_long_tool_decision_while_llm_request_is_blocked(self):
        hard_cancel = threading.Event()
        decision_started = threading.Event()
        release_request = threading.Event()
        agent = ToolAgent("model", ui=_UI(), should_hard_cancel=hard_cancel.is_set)

        def blocked_chat(*_args, **_kwargs):
            decision_started.set()
            release_request.wait(2)
            return {"message": {"role": "assistant", "content": '{"action":"keep"}'}}

        def cancel_when_decision_starts():
            if decision_started.wait(1):
                hard_cancel.set()

        cancel_thread = threading.Thread(target=cancel_when_decision_starts, daemon=True)
        cancel_thread.start()
        started = time.monotonic()
        try:
            with patch.object(agent, "_chat_with_optional_thinking", side_effect=blocked_chat):
                with self.assertRaises(InterruptedError):
                    agent._long_tool_decision("bash", {"command": "sleep 60"}, [])
        finally:
            release_request.set()
            cancel_thread.join(timeout=1)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_hard_cancel_during_long_tool_decision_closes_mcp_transport(self):
        hard_cancel = threading.Event()
        decision_started = threading.Event()
        release_request = threading.Event()
        release_tool = threading.Event()
        agent = ToolAgent("model", ui=_UI(), should_hard_cancel=hard_cancel.is_set)
        agent.LONG_TOOL_NOTICE_SECONDS = 0
        agent.mcp_client = MagicMock()

        def blocked_chat(*_args, **_kwargs):
            decision_started.set()
            release_request.wait(2)
            return {"message": {"role": "assistant", "content": '{"action":"keep"}'}}

        def blocked_tool(_args):
            release_tool.wait(2)
            return {"ok": True}

        def cancel_when_decision_starts():
            if decision_started.wait(1):
                hard_cancel.set()

        cancel_thread = threading.Thread(target=cancel_when_decision_starts, daemon=True)
        cancel_thread.start()
        try:
            with patch.object(agent, "_chat_with_optional_thinking", side_effect=blocked_chat):
                with self.assertRaises(InterruptedError):
                    agent._call_tool_with_long_running_policy(
                        "tool",
                        blocked_tool,
                        {},
                        "tool",
                        [],
                    )
        finally:
            release_request.set()
            release_tool.set()
            cancel_thread.join(timeout=1)

        agent.mcp_client.close.assert_called_once()

    def test_hard_cancel_releases_agent_while_tool_call_is_blocked(self):
        hard_cancel = threading.Event()
        release_tool = threading.Event()
        agent = ToolAgent("model", ui=_UI(), should_hard_cancel=hard_cancel.is_set)
        agent.mcp_client = MagicMock()

        def blocked_tool(_args):
            release_tool.wait(2)
            return {"ok": True}

        timer = threading.Timer(0.05, hard_cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                agent._call_tool_with_long_running_policy(
                    "tool", blocked_tool, {}, "tool", [],
                )
        finally:
            release_tool.set()
            timer.cancel()

        self.assertLess(time.monotonic() - started, 0.5)
        agent.mcp_client.close.assert_called_once()


def subprocess_error():
    return subprocess.CalledProcessError(1, ["systemctl"])


if __name__ == "__main__":
    unittest.main()
