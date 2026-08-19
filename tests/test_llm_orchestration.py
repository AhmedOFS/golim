import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from io import StringIO

from openterm.config import Config
from openterm.core.agent import ToolAgent


class OrchestrationTests(unittest.TestCase):
    def test_agent_executes_tool_calls_and_returns_final_answer(self):
        chat_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            chat_calls.append([message.copy() for message in messages])
            if len(chat_calls) <= 3:
                return {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": "bash",
                                "arguments": {"command": "printf ok"},
                            }
                        }]
                    }
                }
            return {"message": {"role": "assistant", "content": "Final answer after three tool iterations."}}

        agent = ToolAgent("main")
        agent.tools = []
        agent.MAX_AGENT_ITERATIONS = 5

        with patch.object(agent, "_execute_tool", return_value={"ok": True, "results": []}) as execute_tool, \
             patch("openterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_action_agent("Do work.", selected_skills=[])

        self.assertEqual(result, {
            "ok": True,
            "LLM_response": "Final answer after three tool iterations.",
        })
        self.assertEqual(execute_tool.call_count, 3)
        self.assertEqual(len(chat_calls), 4)

    def test_agent_appends_tool_call_and_result_to_messages(self):
        chat_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            chat_calls.append([message.copy() for message in messages])
            if len(chat_calls) == 1:
                return {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": "bash",
                                "arguments": {"command": "printf ok"},
                            }
                        }]
                    }
                }
            return {"message": {"role": "assistant", "content": "Done."}}

        agent = ToolAgent("main")
        agent.tools = []
        agent.MAX_AGENT_ITERATIONS = 5

        with patch.object(agent, "_execute_tool", return_value={"ok": True, "results": []}), \
             patch("openterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_action_agent("Do work.", selected_skills=[])

        self.assertEqual(result, {"ok": True, "LLM_response": "Done."})
        self.assertEqual(len(chat_calls), 2)
        second_call = chat_calls[1]
        self.assertEqual(second_call[-2]["role"], "assistant")
        self.assertIn("tool_calls", second_call[-2])
        self.assertEqual(second_call[-1]["role"], "tool")

    def test_summarization_failure_keeps_history_for_followup(self):
        agent = ToolAgent("main")
        agent.MAX_AGENT_ITERATIONS = 1

        tool_call = {
            "message": {"role": "assistant", "content": "", "tool_calls": [{
                "function": {"name": "bash", "arguments": {"command": "printf ok"}}
            }]}
        }
        with patch.object(agent, "_execute_tool", return_value={"ok": True, "results": []}), \
             patch("openterm.core.agent.chat_with_model_api", side_effect=[tool_call, RuntimeError("summary unavailable")]):
            result = agent._run_action_agent("Do work.", selected_skills=[])

        self.assertIn("summary unavailable", result["LLM_response"])
        self.assertEqual(agent.execution_history[0]["tool"], "bash")
        self.assertEqual(agent.messages[-1]["role"], "tool")

    def test_agent_continues_from_initial_messages(self):
        chat_calls = []
        initial_messages = [
            {"role": "system", "content": "old system"},
            {"role": "user", "content": "Original task."},
            {"role": "assistant", "content": "Original answer."},
        ]

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            chat_calls.append([message.copy() for message in messages])
            return {"message": {"role": "assistant", "content": "Followup answer."}}

        agent = ToolAgent("main")
        agent.tools = []

        with patch("openterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_action_agent(
                "followup: Expand on that.",
                selected_skills=[],
                initial_messages=initial_messages,
                initial_tool_history=[],
            )

        self.assertEqual(result, {"ok": True, "LLM_response": "Followup answer."})
        self.assertEqual(chat_calls[0][-2]["content"], "Original answer.")
        self.assertEqual(chat_calls[0][-1], {"role": "user", "content": "followup: Expand on that."})

    def test_agent_includes_last_thinking_trace_and_content_in_context(self):
        chat_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None, on_thinking_delta=None):
            chat_calls.append([message.copy() for message in messages])
            if len(chat_calls) == 1:
                on_thinking_delta("looked ")
                on_thinking_delta("at plan")
                return {
                    "message": {
                        "role": "assistant",
                        "content": "I will inspect the file.",
                        "thinking": "provider-specific trace",
                        "tool_calls": [{
                            "function": {
                                "name": "bash",
                                "arguments": {"command": "printf ok"},
                            }
                        }]
                    }
                }
            return {"message": {"role": "assistant", "content": "Done."}}

        agent = ToolAgent("main")
        agent.tools = []
        agent.MAX_AGENT_ITERATIONS = 5

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}), \
             patch.object(agent, "_execute_tool", return_value={"ok": True, "results": []}), \
             patch("openterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            Config().set(Config.STREAM_THINKING_TRACES, True)
            result = agent._run_action_agent("Do work.", selected_skills=[])

        self.assertEqual(result, {"ok": True, "LLM_response": "Done."})
        self.assertEqual(len(chat_calls), 2)
        assistant_history = chat_calls[1][-2]
        self.assertEqual(assistant_history["role"], "assistant")
        self.assertIn("tool_calls", assistant_history)
        self.assertNotIn("thinking", assistant_history)
        self.assertIn("[assistant content]\nI will inspect the file.", assistant_history["content"])
        self.assertIn("[assistant thinking trace]\nlooked at plan", assistant_history["content"])

    def test_agent_keeps_only_last_thinking_trace_in_context(self):
        chat_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None, on_thinking_delta=None):
            chat_calls.append([message.copy() for message in messages])
            if len(chat_calls) == 1:
                on_thinking_delta("first trace")
                return {
                    "message": {
                        "role": "assistant",
                        "content": "first content",
                        "tool_calls": [{
                            "function": {
                                "name": "bash",
                                "arguments": {"command": "printf first"},
                            }
                        }]
                    }
                }
            if len(chat_calls) == 2:
                on_thinking_delta("second trace")
                return {
                    "message": {
                        "role": "assistant",
                        "content": "second content",
                        "tool_calls": [{
                            "function": {
                                "name": "bash",
                                "arguments": {"command": "printf second"},
                            }
                        }]
                    }
                }
            return {"message": {"role": "assistant", "content": "Done."}}

        agent = ToolAgent("main")
        agent.tools = []
        agent.MAX_AGENT_ITERATIONS = 5

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}), \
             patch.object(agent, "_execute_tool", return_value={"ok": True, "results": []}), \
             patch("openterm.core.agent.chat_with_model_api", side_effect=fake_chat):
            Config().set(Config.STREAM_THINKING_TRACES, True)
            result = agent._run_action_agent("Do work.", selected_skills=[])

        self.assertEqual(result, {"ok": True, "LLM_response": "Done."})
        self.assertEqual(len(chat_calls), 3)
        third_call_text = "\n\n".join(str(message.get("content", "")) for message in chat_calls[2])
        self.assertIn("first content", third_call_text)
        self.assertNotIn("first trace", third_call_text)
        self.assertIn("second content", third_call_text)
        self.assertIn("second trace", third_call_text)
        self.assertEqual(third_call_text.count("[assistant thinking trace]"), 1)

    def test_execution_summary_includes_bash_output_file(self):
        agent = ToolAgent("main")

        summary = agent._build_execution_summary([{
            "tool": "bash",
            "arguments": {"command": "seq 1 60"},
            "status": "success",
            "result": {
                "ok": True,
                "output_truncated": True,
                "output_file": "/tmp/openterm/data/bash_output.json",
                "output_line_count": 60,
                "message": "Output exceeded 50 lines.",
                "results": [{
                    "command": "seq 1 60",
                    "stdout": "1\n2",
                    "stderr": "",
                    "returncode": 0,
                }],
            },
        }])

        self.assertIn("output truncated: true", summary)
        self.assertIn("output file: /tmp/openterm/data/bash_output.json", summary)
        self.assertIn("Output exceeded 50 lines.", summary)

    def test_execution_summary_includes_read_file_page_content(self):
        agent = ToolAgent("main")

        summary = agent._build_execution_summary([{
            "tool": "read_file",
            "arguments": {"path": "/tmp/file.txt", "page": 2},
            "status": "success",
            "result": {
                "ok": True,
                "path": "/tmp/file.txt",
                "content": "line 51\nline 52",
                "page": 2,
                "total_pages": 3,
                "total_lines": 120,
                "has_next_page": True,
                "next_page": 3,
            },
        }])

        self.assertIn("page: 2 of 3", summary)
        self.assertIn("next page: 3", summary)
        self.assertIn("line 51", summary)


if __name__ == "__main__":
    unittest.main()
