import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from io import StringIO

from cterm.new_llm import ToolAgent


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

        with patch.object(agent, "_select_skills", return_value=([], "")) as select_skills, \
             patch.object(agent, "_execute_tool", return_value={"ok": True, "results": []}) as execute_tool, \
             patch("cterm.new_llm.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_action_agent("Do work.")

        self.assertEqual(result, "Final answer after three tool iterations.")
        select_skills.assert_called_once_with("Do work.")
        self.assertEqual(execute_tool.call_count, 3)
        self.assertEqual(len(chat_calls), 4)

    def test_agent_reaches_iteration_limit(self):
        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            if tools is None:
                return {"message": {"role": "assistant", "content": "Hit the iteration limit summary."}}
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

        agent = ToolAgent("main")
        agent.tools = []
        agent.MAX_AGENT_ITERATIONS = 2

        with patch.object(agent, "_select_skills", return_value=([], "")), \
             patch.object(agent, "_execute_tool", return_value={"ok": True, "results": []}), \
             patch("cterm.new_llm.chat_with_model_api", side_effect=fake_chat) as chat:
            result = agent._run_action_agent("Do work.")

        self.assertIn("iteration limit", result.lower())
        self.assertEqual(chat.call_count, 3)

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

        with patch.object(agent, "_select_skills", return_value=([], "")), \
             patch.object(agent, "_execute_tool", return_value={"ok": True, "results": []}), \
             patch("cterm.new_llm.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_action_agent("Do work.")

        self.assertEqual(result, "Done.")
        self.assertEqual(len(chat_calls), 2)
        second_call = chat_calls[1]
        self.assertEqual(second_call[-2]["role"], "assistant")
        self.assertIn("tool_calls", second_call[-2])
        self.assertEqual(second_call[-1]["role"], "tool")

    def test_incomplete_worker_still_hands_off_successful_finder_results(self):
        agent = ToolAgent("main")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            handoff = agent._build_worker_handoff({
                "output": "Search did not finish.",
                "complete": False,
                "tool_history": [{
                    "tool": "finder",
                    "arguments": {"path": "~", "pattern": "*CV*"},
                    "status": "success",
                    "result": {
                        "ok": True,
                        "path": "~",
                        "matches": ["Documents/Ahmed_CV.pdf"],
                        "total": 1,
                        "truncated": False,
                    },
                }],
            })

            self.assertIn("Finder results file for planner and next agent:", handoff)
            self.assertNotIn("~/Documents/Ahmed_CV.pdf", handoff)
            saved_path_text = handoff.split(
                "Finder results file for planner and next agent: ", 1
            )[1].split(". The JSON field", 1)[0]
            saved = json.loads(Path(saved_path_text).read_text(encoding="utf-8"))

        self.assertEqual(saved["paths"], ["~/Documents/Ahmed_CV.pdf"])

    def test_failed_finder_result_is_not_handed_off(self):
        agent = ToolAgent("main")
        handoff = agent._build_worker_handoff({
            "output": "Search failed.",
            "complete": False,
            "tool_history": [{
                "tool": "finder",
                "arguments": {"path": "~", "pattern": "*CV*"},
                "status": "failed",
                "result": {
                    "ok": False,
                    "path": "~",
                    "matches": ["Documents/Ahmed_CV.pdf"],
                    "total": 1,
                    "truncated": False,
                },
            }],
        })

        self.assertEqual(handoff, "Search failed.")
        self.assertNotIn("Finder results file", handoff)

    def test_only_last_successful_finder_result_is_saved(self):
        agent = ToolAgent("main")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            handoff = agent._build_worker_handoff({
                "output": "Search complete.",
                "complete": True,
                "tool_history": [
                    {
                        "tool": "finder",
                        "arguments": {"path": "~/Documents", "pattern": "*.pdf"},
                        "status": "success",
                        "result": {
                            "ok": True,
                            "path": "~/Documents",
                            "matches": ["old.pdf"],
                            "total": 1,
                            "truncated": False,
                        },
                    },
                    {
                        "tool": "finder",
                        "arguments": {"path": "~/Downloads", "pattern": "*.pdf"},
                        "status": "success",
                        "result": {
                            "ok": True,
                            "path": "~/Downloads",
                            "matches": ["new.pdf"],
                            "total": 1,
                            "truncated": False,
                        },
                    },
                ],
            })

            saved_path_text = handoff.split(
                "Finder results file for planner and next agent: ", 1
            )[1].split(". The JSON field", 1)[0]
            saved_path = Path(saved_path_text)
            saved = json.loads(saved_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["result"]["path"], "~/Downloads")
        self.assertNotIn("full_paths", saved)
        self.assertEqual(saved["paths"], ["~/Downloads/new.pdf"])

    def test_non_finder_tool_results_are_not_included_in_planner_handoff(self):
        agent = ToolAgent("main")
        handoff = agent._build_worker_handoff({
            "output": "Printed files.",
            "complete": True,
            "tool_history": [{
                "tool": "bash",
                "arguments": {"command": "printf secret"},
                "status": "success",
                "result": {
                    "ok": True,
                    "results": [{
                        "command": "printf secret",
                        "stdout": "secret\n",
                        "stderr": "",
                        "returncode": 0,
                    }],
                },
            }],
        })

        self.assertEqual(handoff, "Printed files.")
        self.assertNotIn("secret", handoff)

    def test_bash_output_file_is_included_in_planner_handoff(self):
        agent = ToolAgent("main")
        handoff = agent._build_worker_handoff({
            "output": "Measured disk usage.",
            "complete": True,
            "tool_history": [{
                "tool": "bash",
                "arguments": {"command": "du -a /tmp"},
                "status": "success",
                "result": {
                    "ok": True,
                    "output_file": "/tmp/cterm/data/bash_output.json",
                    "output_truncated": True,
                    "results": [],
                },
            }],
        })

        self.assertIn("Measured disk usage.", handoff)
        self.assertIn("Bash output file for planner and next agent:", handoff)
        self.assertIn("/tmp/cterm/data/bash_output.json", handoff)
        self.assertIn("The JSON field `results` contains full stdout and stderr.", handoff)

    def test_finder_and_bash_output_files_share_planner_handoff(self):
        agent = ToolAgent("main")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            handoff = agent._build_worker_handoff({
                "output": "Found files and measured usage.",
                "complete": True,
                "tool_history": [
                    {
                        "tool": "finder",
                        "arguments": {"path": "~", "pattern": "*.txt"},
                        "status": "success",
                        "result": {
                            "ok": True,
                            "path": "~",
                            "matches": ["a.txt"],
                            "total": 1,
                            "truncated": False,
                        },
                    },
                    {
                        "tool": "bash",
                        "arguments": {"command": "du -a ~"},
                        "status": "success",
                        "result": {
                            "ok": True,
                            "output_file": "/tmp/cterm/data/bash_output.json",
                            "output_truncated": True,
                            "results": [],
                        },
                    },
                ],
            })

        self.assertIn("Finder results file for planner and next agent:", handoff)
        self.assertIn("Bash output file for planner and next agent:", handoff)

    def test_execution_summary_includes_bash_output_file(self):
        agent = ToolAgent("main")

        summary = agent._build_execution_summary([{
            "tool": "bash",
            "arguments": {"command": "seq 1 60"},
            "status": "success",
            "result": {
                "ok": True,
                "output_truncated": True,
                "output_file": "/tmp/cterm/data/bash_output.json",
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
        self.assertIn("output file: /tmp/cterm/data/bash_output.json", summary)
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
