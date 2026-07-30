import unittest
from unittest.mock import patch

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


class DiscoveryClient:
    def __init__(self, outcome):
        self.outcome = outcome

    async def list_tools(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class RuntimeTests(unittest.TestCase):
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

        with patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            result = runtime.run("Do work.")

        self.assertEqual(result, "Done.")
        self.assertIs(runtime.ui, ui)
        self.assertEqual(runtime.result, "Done.")
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

        with patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            first = runtime.run("Do work.")
            second = runtime.run_followup("// explain more")

        self.assertEqual(first, "First answer.")
        self.assertEqual(second, "Second answer.")
        self.assertEqual(chat_calls[1][-3]["content"], "Do work.")
        self.assertEqual(chat_calls[1][-2]["content"], "First answer.")
        self.assertEqual(chat_calls[1][-1], {"role": "user", "content": "followup: explain more"})

    def test_runtime_clarification_drops_interrupted_marker(self):
        token = active_agent_events_handler.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_events_handler.reset(token)
        runtime.mcp_client = FakeMCPClient()
        runtime.messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Do work."},
            {"role": "assistant", "content": "Interrupted."},
        ]
        chat_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            chat_calls.append([message.copy() for message in messages])
            return {"message": {"role": "assistant", "content": "Clarified answer."}}

        with patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            result = runtime.run_followup("use the smaller file", clarification=True)

        self.assertEqual(result, "Clarified answer.")
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

        with patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat), \
             patch("cterm.core.agent.ToolAgent._execute_tool", side_effect=interrupting_tool):
            result = runtime.run("Stop after this.")

        self.assertEqual(result, "Interrupted.")
        self.assertEqual(runtime.result, "Interrupted.")
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

        with patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat), \
             patch("cterm.core.agent.ToolAgent._execute_tool") as execute_tool:
            result = runtime.run("Stop before tool.")

        self.assertEqual(result, "Interrupted.")
        self.assertEqual(runtime.result, "Interrupted.")
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

        with patch.object(runtime, "select_skills", side_effect=[([], "first skill"), ([], "different skill")]) as select_skills, \
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

        with patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            runtime.run("First.")
            runtime.run("Second.")

        self.assertEqual(runtime.mcp_client.list_count, 2)

    def test_runtime_captures_ui_context_at_initialization(self):
        first_ui = FakeUI()
        second_ui = FakeUI()
        token = active_agent_events_handler.set(first_ui)
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_events_handler.reset(token)
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        token = active_agent_events_handler.set(second_ui)
        try:
            with patch.object(runtime, "select_skills", return_value=([], "")), \
                 patch("cterm.core.agent.chat_with_model_api", side_effect=fake_chat):
                runtime.run("Do work.")
        finally:
            active_agent_events_handler.reset(token)

        self.assertIs(runtime.ui, first_ui)

    def test_runtime_requires_ui_context_at_initialization(self):
        runtime = Runtime(model="main")

        with self.assertRaises(RuntimeError):
            runtime.run("Do work.")

    def test_initialize_tools_retries_invalid_tool_discovery(self):
        runtime = Runtime(model="main")
        tools = [object()]
        clients = [
            DiscoveryClient(ValueError("No tools in response")),
            DiscoveryClient(tools),
        ]

        with patch.object(runtime, "ensure_mcp_server", return_value="/tmp/cterm-test.sock"), \
             patch("cterm.core.runtime.FastMCPClient", side_effect=clients), \
             patch("cterm.core.runtime.time.sleep"):
            runtime.initialize_tools()

        self.assertEqual(runtime.tools, tools)

    
    def test_create_mcp_client_only_constructs_transport(self):
        runtime = Runtime(model="main")
        client = object()

        with patch("cterm.core.runtime.FastMCPClient", return_value=client) as constructor:
            result = runtime.create_mcp_client("/tmp/cterm-test.sock")

        constructor.assert_called_once_with("/tmp/cterm-test.sock")
        self.assertIs(result, client)
        self.assertIs(runtime.mcp_client, client)

    def test_initialize_tools_starts_service_then_retries_refused_socket(self):
        runtime = Runtime(model="main")
        tools = [object()]
        clients = [
            DiscoveryClient(ConnectionRefusedError("socket refused")),
            DiscoveryClient(tools),
        ]
        socket_path = type("SocketPath", (), {})()
        socket_states = iter([False, True, True])
        socket_path.exists = lambda: next(socket_states)

        with patch("cterm.core.runtime.get_socket_path", return_value=socket_path), \
             patch("cterm.core.runtime.FastMCPClient", side_effect=clients), \
             patch("cterm.core.runtime.subprocess.run") as run_service, \
             patch("cterm.core.runtime.time.sleep"):
            runtime.initialize_tools()

        run_service.assert_called_once_with(
            ["systemctl", "--user", "start", "cterm-mcp.service"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(runtime.tools, tools)


if __name__ == "__main__":
    unittest.main()
