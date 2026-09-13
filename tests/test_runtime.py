import unittest
from unittest.mock import MagicMock, patch

from golim.core.runtime import Runtime


class FakeUI:
    def __init__(self):
        self.messages = []

    def message(self, text):
        self.messages.append(text)

    def status(self, message):
        pass

    def clear_status(self):
        pass

    def thinking_delta(self, text):
        pass

    def thinking_complete(self, text):
        pass

    def tool_call(self, tool_name, args):
        pass

    def tool_output(self, fd=None, line="", end="\n", result=None):
        pass

    def shell_output(self, result):
        pass

    def request_binary_approval(self, binary):
        return False

    def request_sudo_password(self):
        return None

    def request_python_approval(self, code):
        return False


class FakeMCPClient:
    def __init__(self):
        self.list_count = 0

    async def list_tools(self):
        self.list_count += 1
        return []


class AuthFakeMCPClient:
    def __init__(self, wrapper_installed=True, authenticated=False, auth_ok=True):
        self._wrapper_installed = wrapper_installed
        self._authenticated = authenticated
        self._auth_ok = auth_ok
        self.authenticate_calls = []

    def auth_status(self):
        return {
            "ok": True,
            "wrapper_installed": self._wrapper_installed,
            "authenticated": self._authenticated,
        }

    def authenticate(self, password):
        self.authenticate_calls.append(password)
        return {"ok": self._auth_ok}


class PromptingFakeUI(FakeUI):
    def __init__(self, password="session-password"):
        super().__init__()
        self._password = password
        self.prompts = 0

    def request_sudo_password(self):
        self.prompts += 1
        return self._password


class RuntimeTests(unittest.TestCase):
    def test_runtime_runs_agent_and_copies_state(self):
        ui = FakeUI()
        runtime = Runtime(model="main")
        runtime.bind_ui(ui)
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
            result = runtime.run("Do work.")

        self.assertEqual(result, {"ok": True, "LLM_response": "Done."})
        self.assertIs(runtime.ui, ui)
        self.assertEqual(runtime.result, {"ok": True, "LLM_response": "Done."})
        self.assertEqual(runtime.execution_history, [])
        self.assertEqual(runtime.messages[-1]["content"], "Done.")

    def test_runtime_followup_reuses_previous_messages(self):
        runtime = Runtime(model="main")
        runtime.bind_ui(FakeUI())
        runtime.mcp_client = FakeMCPClient()
        chat_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            chat_calls.append([message.copy() for message in messages])
            if len(chat_calls) == 1:
                return {"message": {"role": "assistant", "content": "First answer."}}
            return {"message": {"role": "assistant", "content": "Second answer."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
            first = runtime.run("Do work.")
            second = runtime.run_followup("explain more")

        self.assertEqual(first, {"ok": True, "LLM_response": "First answer."})
        self.assertEqual(second, {"ok": True, "LLM_response": "Second answer."})
        self.assertEqual(chat_calls[1][-3]["content"], "Do work.")
        self.assertEqual(chat_calls[1][-2]["content"], "First answer.")
        self.assertEqual(chat_calls[1][-1], {"role": "user", "content": "followup: explain more"})

    def test_reset_conversation_makes_next_input_a_fresh_run(self):
        runtime = Runtime(model="main")
        runtime.bind_ui(FakeUI())
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Fresh answer."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")) as select_skills, \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
            runtime.run("Do work.")
            runtime.reset_conversation()
            result = runtime.run_followup("New task.")

        self.assertEqual(result, {"ok": True, "LLM_response": "Fresh answer."})
        # Escalated to a fresh run: the user message carries no followup
        # marker and skills are selected again.
        self.assertEqual(runtime.messages[-2], {"role": "user", "content": "New task."})
        self.assertEqual(select_skills.call_count, 2)

    def test_runtime_clarification_uses_safe_history_without_marker(self):
        runtime = Runtime(model="main")
        runtime.bind_ui(FakeUI())
        runtime.mcp_client = FakeMCPClient()
        runtime.messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Do work."},
        ]
        runtime.result = {"ok": False, "LLM_response": "Interrupted."}
        chat_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            chat_calls.append([message.copy() for message in messages])
            return {"message": {"role": "assistant", "content": "Clarified answer."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
            result = runtime.run_followup("use the smaller file", clarification=True)

        self.assertEqual(result, {"ok": True, "LLM_response": "Clarified answer."})
        contents = [message.get("content") for message in chat_calls[0]]
        self.assertNotIn("Interrupted.", contents)
        self.assertEqual(chat_calls[0][-1], {"role": "user", "content": "clarification: use the smaller file"})

    def test_runtime_interrupt_aborts_after_tool_iteration(self):
        runtime = Runtime(model="main")
        runtime.bind_ui(FakeUI())
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": "bash",
                            "arguments": {"command": "printf ok"},
                        }
                    }],
                }
            }

        def interrupting_tool(*_args, **_kwargs):
            runtime.interrupt()
            return {"ok": True}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat), \
             patch("golim.core.agent.ToolAgent._execute_tool", side_effect=interrupting_tool):
            result = runtime.run("Stop after this.")

        self.assertEqual(result, {"ok": False, "LLM_response": "Interrupted."})
        self.assertEqual(runtime.result, {"ok": False, "LLM_response": "Interrupted."})
        self.assertEqual(runtime.execution_history[0]["tool"], "bash")

    def test_runtime_interrupt_before_tool_execution_skips_tool(self):
        runtime = Runtime(model="main")
        runtime.bind_ui(FakeUI())
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            runtime.interrupt()
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": "bash",
                            "arguments": {"command": "printf ok"},
                        }
                    }],
                }
            }

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat), \
             patch("golim.core.agent.ToolAgent._execute_tool") as execute_tool:
            result = runtime.run("Stop before tool.")

        self.assertEqual(result, {"ok": False, "LLM_response": "Interrupted."})
        self.assertEqual(runtime.result, {"ok": False, "LLM_response": "Interrupted."})
        self.assertEqual(runtime.execution_history, [])
        execute_tool.assert_not_called()
        self.assertFalse(any(message.get("tool_calls") for message in runtime.messages))

    def test_followup_preserves_initial_system_prompt(self):
        runtime = Runtime(model="main")
        runtime.bind_ui(FakeUI())
        runtime.mcp_client = FakeMCPClient()
        prompts = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            prompts.append(messages[0]["content"])
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "select_skills", side_effect=[([], "first skill"), ([], "different skill")]) as select_skills, \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
            runtime.run("First.")
            runtime.run_followup("Again.")

        self.assertEqual(prompts, [prompts[0], prompts[0]])
        self.assertIn("first skill", prompts[0])
        self.assertEqual(select_skills.call_count, 1)

    def test_runtime_verifies_tools_on_every_run(self):
        runtime = Runtime(model="main")
        runtime.bind_ui(FakeUI())
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
            runtime.run("First.")
            runtime.run("Second.")

        self.assertEqual(runtime.mcp_client.list_count, 2)

    def test_runtime_run_without_bound_ui_raises(self):
        runtime = Runtime(model="main")

        with self.assertRaises(RuntimeError):
            runtime.run("Do work.")

    def test_create_mcp_client_only_constructs_transport(self):
        runtime = Runtime(model="main")
        client = object()

        with patch("golim.core.runtime.FastMCPClient", return_value=client) as constructor:
            result = runtime.create_mcp_client("/tmp/golim-test.sock")

        constructor.assert_called_once_with("/tmp/golim-test.sock")
        self.assertIs(result, client)
        self.assertIs(runtime.mcp_client, client)

    def test_runtime_hard_cancel_does_not_close_mcp_transport(self):
        runtime = Runtime(model="main")
        runtime.mcp_client = MagicMock()

        runtime.hard_cancel()

        self.assertTrue(runtime.should_interrupt())
        self.assertTrue(runtime.should_hard_cancel())
        runtime.mcp_client.close.assert_not_called()

    def test_auth_session_skipped_when_ticket_already_valid(self):
        ui = PromptingFakeUI()
        runtime = Runtime(model="main")
        runtime.bind_ui(ui)
        runtime.mcp_client = AuthFakeMCPClient(authenticated=True)

        runtime.authenticate_sudo()

        self.assertEqual(ui.prompts, 0)

    def test_auth_session_skipped_when_wrapper_not_installed(self):
        ui = PromptingFakeUI()
        runtime = Runtime(model="main")
        runtime.bind_ui(ui)
        runtime.mcp_client = AuthFakeMCPClient(wrapper_installed=False)

        runtime.authenticate_sudo()

    def test_auth_session_skipped_when_nosudo_is_enabled(self):
        ui = PromptingFakeUI()
        config = MagicMock(no_sudo=True)
        runtime = Runtime(config=config, model="main")
        runtime.bind_ui(ui)
        runtime.mcp_client = AuthFakeMCPClient()

        runtime.authenticate_sudo()

        self.assertEqual(ui.prompts, 0)
        self.assertEqual(runtime.mcp_client.authenticate_calls, [])

        self.assertEqual(ui.prompts, 0)

    def test_auth_session_prompts_once_and_authenticates_session(self):
        ui = PromptingFakeUI(password="session-password")
        runtime = Runtime(model="main")
        runtime.bind_ui(ui)
        client = AuthFakeMCPClient(authenticated=False, auth_ok=True)
        runtime.mcp_client = client

        runtime.authenticate_sudo()

        self.assertEqual(ui.prompts, 1)
        self.assertEqual(client.authenticate_calls, ["session-password"])
        self.assertTrue(any("authenticated" in message for message in ui.messages))

    def test_auth_session_reports_failure_without_retry(self):
        ui = PromptingFakeUI(password="wrong")
        runtime = Runtime(model="main")
        runtime.bind_ui(ui)
        client = AuthFakeMCPClient(authenticated=False, auth_ok=False)
        runtime.mcp_client = client

        runtime.authenticate_sudo()

        self.assertEqual(ui.prompts, 1)
        self.assertEqual(client.authenticate_calls, ["wrong"])
        self.assertTrue(any("failed" in message for message in ui.messages))

    def test_auth_session_skipped_when_user_declines_prompt(self):
        ui = PromptingFakeUI(password=None)
        runtime = Runtime(model="main")
        runtime.bind_ui(ui)
        client = AuthFakeMCPClient(authenticated=False)
        runtime.mcp_client = client

        runtime.authenticate_sudo()

        self.assertEqual(ui.prompts, 1)
        self.assertEqual(client.authenticate_calls, [])
        self.assertTrue(any("skipped" in message for message in ui.messages))

    def test_auth_session_survives_transport_without_auth_support(self):
        ui = PromptingFakeUI()
        runtime = Runtime(model="main")
        runtime.bind_ui(ui)
        runtime.mcp_client = FakeMCPClient()

        runtime.authenticate_sudo()

        self.assertEqual(ui.prompts, 0)

    def test_run_authenticates_sudo_after_tools_before_skills(self):
        config = MagicMock()
        config.proactive_auth = True
        runtime = Runtime(config=config, model="main")
        runtime.bind_ui(FakeUI())
        runtime.mcp_client = FakeMCPClient()
        order = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "initialize_tools", side_effect=lambda: order.append("tools")), \
             patch.object(runtime, "authenticate_sudo", side_effect=lambda: order.append("auth")), \
             patch.object(runtime, "select_skills", side_effect=lambda _msg: order.append("skills") or ([], "")), \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
            runtime.run("Do work.")

        self.assertEqual(order, ["tools", "auth", "skills"])

    def test_run_skips_proactive_auth_session_when_disabled(self):
        config = MagicMock()
        config.proactive_auth = False
        runtime = Runtime(config=config, model="main")
        runtime.bind_ui(FakeUI())
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/golim-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch.object(runtime, "authenticate_sudo") as authenticate, \
             patch("golim.core.agent.chat_with_model_api", side_effect=fake_chat):
            runtime.run("Do work.")

        authenticate.assert_not_called()

if __name__ == "__main__":
    unittest.main()
