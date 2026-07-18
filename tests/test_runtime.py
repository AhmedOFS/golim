import unittest
from unittest.mock import patch

from cterm.core.agent_ui import active_agent_ui
from cterm.core.runtime import Runtime


class FakeUI:
    def message(self, text):
        pass

    def update_spinner(self, message):
        pass

    def stop_spinner(self):
        pass

    def thinking_trace_delta(self, text):
        pass

    def thinking_trace_complete(self, text):
        pass

    def tool_call(self, tool_name, args):
        pass

    def handle_tool_output(self, fd=None, line="", end="\n", result=None):
        pass

    def handle_shell_result_output(self, result):
        pass

    def approve_privileged_binary(self, binary):
        return False

    def approve_python_code(self, code):
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
        token = active_agent_ui.set(ui)
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_ui.reset(token)
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.llm.chat_with_model_api", side_effect=fake_chat):
            result = runtime.run("Do work.")

        self.assertEqual(result, "Done.")
        self.assertIs(runtime.ui, ui)
        self.assertEqual(runtime.result, "Done.")
        self.assertEqual(runtime.execution_history, [])
        self.assertEqual(runtime.messages[-1]["content"], "Done.")

    def test_runtime_interrupt_aborts_after_tool_iteration(self):
        token = active_agent_ui.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_ui.reset(token)
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
             patch("cterm.llm.chat_with_model_api", side_effect=fake_chat), \
             patch("cterm.llm.ToolAgent._execute_tool", side_effect=interrupting_tool):
            result = runtime.run("Stop after this.")

        self.assertEqual(result, "Interrupted.")
        self.assertEqual(runtime.result, "Interrupted.")
        self.assertEqual(runtime.execution_history[0]["tool"], "bash")

    def test_runtime_interrupt_before_tool_execution_skips_tool(self):
        token = active_agent_ui.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_ui.reset(token)
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
             patch("cterm.llm.chat_with_model_api", side_effect=fake_chat), \
             patch("cterm.llm.ToolAgent._execute_tool") as execute_tool:
            result = runtime.run("Stop before tool.")

        self.assertEqual(result, "Interrupted.")
        self.assertEqual(runtime.result, "Interrupted.")
        self.assertEqual(runtime.execution_history, [])
        execute_tool.assert_not_called()

    def test_runtime_verifies_tools_on_every_run(self):
        token = active_agent_ui.set(FakeUI())
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_ui.reset(token)
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        with patch.object(runtime, "select_skills", return_value=([], "")), \
             patch("cterm.llm.chat_with_model_api", side_effect=fake_chat):
            runtime.run("First.")
            runtime.run("Second.")

        self.assertEqual(runtime.mcp_client.list_count, 2)

    def test_runtime_captures_ui_context_at_initialization(self):
        first_ui = FakeUI()
        second_ui = FakeUI()
        token = active_agent_ui.set(first_ui)
        try:
            runtime = Runtime(model="main")
        finally:
            active_agent_ui.reset(token)
        runtime.mcp_client = FakeMCPClient()

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            return {"message": {"role": "assistant", "content": "Done."}}

        token = active_agent_ui.set(second_ui)
        try:
            with patch.object(runtime, "select_skills", return_value=([], "")), \
                 patch("cterm.llm.chat_with_model_api", side_effect=fake_chat):
                runtime.run("Do work.")
        finally:
            active_agent_ui.reset(token)

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

        with patch("cterm.runtime.FastMCPClient", side_effect=clients), \
             patch("cterm.runtime.time.sleep"):
            runtime.initialize_tools()

        self.assertEqual(runtime.tools, tools)

    def test_initialize_tools_starts_service_then_retries_refused_socket(self):
        runtime = Runtime(model="main")
        tools = [object()]
        clients = [
            DiscoveryClient(ConnectionRefusedError("socket refused")),
            DiscoveryClient(tools),
        ]

        with patch("cterm.runtime.FastMCPClient", side_effect=clients), \
             patch("cterm.runtime.subprocess.run") as run_service, \
             patch("cterm.runtime.time.sleep"):
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
