import unittest
from unittest.mock import MagicMock, patch

from cterm.core.agent_events import active_agent_events_handler
from cterm.core.runtime import Runtime


class FakeUI:
    def message(self, text):
        pass

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

    def request_python_approval(self, code):
        return False


class FakeMCPClient:
    def __init__(self):
        self.list_count = 0

    async def list_tools(self):
        self.list_count += 1
        return []


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.ui = FakeUI()
        self.ui_token = active_agent_events_handler.set(self.ui)

    def tearDown(self):
        active_agent_events_handler.reset(self.ui_token)

    def test_runtime_runs_agent_and_copies_state(self):
        ui = FakeUI()
        token = active_agent_events_handler.set(ui)
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_events_handler.reset(token)
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/cterm-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            result = runtime.run("Do work.")

        self.assertEqual(result, {"ok": True, "LLM_response": "Done."})
        self.assertFalse(hasattr(runtime, "ui"))
        self.assertEqual(runtime.result, {"ok": True, "LLM_response": "Done."})
        self.assertEqual(runtime.execution_history, [])
        self.assertEqual(runtime.messages[-1]["content"], "Done.")

    def test_runtime_followup_reuses_previous_messages(self):
        token = active_agent_events_handler.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_events_handler.reset(token)
        runtime.mcp_client = FakeMCPClient()
        chat_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            chat_calls.append([message.copy() for message in messages])
            if len(chat_calls) == 1:
                return {"message": {"role": "assistant", "content": "First answer."}}
            return {"message": {"role": "assistant", "content": "Second answer."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/cterm-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            first = runtime.run("Do work.")
            second = runtime.run_followup("// explain more")

        self.assertEqual(first, {"ok": True, "LLM_response": "First answer."})
        self.assertEqual(second, {"ok": True, "LLM_response": "Second answer."})
        self.assertEqual(chat_calls[1][-3]["content"], "Do work.")
        self.assertEqual(chat_calls[1][-2]["content"], "First answer.")
        self.assertEqual(chat_calls[1][-1], {"role": "user", "content": "followup: explain more"})

    def test_runtime_clarification_uses_safe_history_without_marker(self):
        token = active_agent_events_handler.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_events_handler.reset(token)
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

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/cterm-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            result = runtime.run_followup("use the smaller file", clarification=True)

        self.assertEqual(result, {"ok": True, "LLM_response": "Clarified answer."})
        contents = [message.get("content") for message in chat_calls[0]]
        self.assertNotIn("Interrupted.", contents)
        self.assertEqual(chat_calls[0][-1], {"role": "user", "content": "clarification: use the smaller file"})

    def test_runtime_interrupt_aborts_after_tool_iteration(self):
        token = active_agent_events_handler.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_events_handler.reset(token)
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

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/cterm-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat), \
             patch("cterm.core.agent.ToolAgent._execute_tool", side_effect=interrupting_tool):
            result = runtime.run("Stop after this.")

        self.assertEqual(result, {"ok": False, "LLM_response": "Interrupted."})
        self.assertEqual(runtime.result, {"ok": False, "LLM_response": "Interrupted."})
        self.assertEqual(runtime.execution_history[0]["tool"], "bash")

    def test_runtime_interrupt_before_tool_execution_skips_tool(self):
        token = active_agent_events_handler.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_events_handler.reset(token)
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

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/cterm-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat), \
             patch("cterm.core.agent.ToolAgent._execute_tool") as execute_tool:
            result = runtime.run("Stop before tool.")

        self.assertEqual(result, {"ok": False, "LLM_response": "Interrupted."})
        self.assertEqual(runtime.result, {"ok": False, "LLM_response": "Interrupted."})
        self.assertEqual(runtime.execution_history, [])
        execute_tool.assert_not_called()
        self.assertFalse(any(message.get("tool_calls") for message in runtime.messages))

    def test_followup_preserves_initial_system_prompt(self):
        token = active_agent_events_handler.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_events_handler.reset(token)
        runtime.mcp_client = FakeMCPClient()
        prompts = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            prompts.append(messages[0]["content"])
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/cterm-test.sock"), \
             patch.object(runtime, "select_skills", side_effect=[([], "first skill"), ([], "different skill")]) as select_skills, \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            runtime.run("First.")
            runtime.run_followup("// Again.")

        self.assertEqual(prompts, [prompts[0], prompts[0]])
        self.assertIn("first skill", prompts[0])
        self.assertEqual(select_skills.call_count, 1)

    def test_runtime_verifies_tools_on_every_run(self):
        token = active_agent_events_handler.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_events_handler.reset(token)
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/cterm-test.sock"), \
             patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            runtime.run("First.")
            runtime.run("Second.")

        self.assertEqual(runtime.mcp_client.list_count, 2)

    def test_runtime_requires_ui_context_at_initialization(self):
        token = active_agent_events_handler.set(None)
        runtime = Runtime(model="main")
        try:
            with self.assertRaises(RuntimeError):
                runtime.run("Do work.")
        finally:
            active_agent_events_handler.reset(token)

    def test_create_mcp_client_only_constructs_transport(self):
        runtime = Runtime(model="main")
        client = object()

        with patch("cterm.core.runtime.FastMCPClient", return_value=client) as constructor:
            result = runtime.create_mcp_client("/tmp/cterm-test.sock")

        constructor.assert_called_once_with("/tmp/cterm-test.sock")
        self.assertIs(result, client)
        self.assertIs(runtime.mcp_client, client)

    def test_runtime_hard_cancel_does_not_close_mcp_transport(self):
        runtime = Runtime(model="main")
        runtime.mcp_client = MagicMock()

        runtime.hard_cancel()

        self.assertTrue(runtime.should_interrupt())
        self.assertTrue(runtime.should_hard_cancel())
        runtime.mcp_client.close.assert_not_called()

if __name__ == "__main__":
    unittest.main()
