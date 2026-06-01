import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from io import StringIO

from cterm.llm import ToolAgent


class OrchestrationTests(unittest.TestCase):
    def test_planner_prompt_lists_available_worker_tools(self):
        captured_messages = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            captured_messages.append([message.copy() for message in messages])
            return {"message": {"content": "Done."}}

        agent = ToolAgent("main")
        agent.tools = [
            type("Tool", (), {"name": "bash"})(),
            type("Tool", (), {"name": "finder"})(),
            type("Tool", (), {"name": "exec"})(),
        ]

        with patch("cterm.llm.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_with_native_tools("Check files.")

        self.assertEqual(result, "Done.")
        planner_prompt = captured_messages[0][0]["content"]
        self.assertIn("Worker tools available: bash, exec, finder.", planner_prompt)

    def test_planner_runs_before_sequential_skill_injected_agents(self):
        captured_messages = []
        planner_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            captured_messages.append([message.copy() for message in messages])
            is_planner = tools and tools[0]["function"]["name"] == "new_agent"
            if is_planner:
                planner_calls.append(messages)
            if is_planner and len(planner_calls) == 1:
                return {
                    "message": {
                        "tool_calls": [{
                            "function": {
                                "name": "new_agent",
                                "arguments": {"action": "Find CV files."},
                            }
                        }]
                    }
                }
            if not is_planner and "Find CV files." in messages[1]["content"]:
                return {"message": {"content": "Found two CV files."}}
            if is_planner and len(planner_calls) == 2:
                return {
                    "message": {
                        "tool_calls": [{
                            "function": {
                                "name": "new_agent",
                                "arguments": {"action": "Copy the matching files."},
                            }
                        }]
                    }
                }
            if not is_planner and "Copy the matching files." in messages[1]["content"]:
                return {"message": {"content": "Copied both CV files."}}
            return {"message": {"content": "Done."}}

        agent = ToolAgent("main")
        agent.tools = []

        with patch.object(agent, "_select_skills_prompt", return_value="SKILL PROMPT") as select_skills, \
             patch.object(agent, "_verify_history", return_value=(True, "")), \
             patch("cterm.llm.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_with_native_tools("Find CVs and copy them.")

        self.assertEqual(result, "Done.")
        select_skills.assert_any_call("Find CV files.")
        select_skills.assert_any_call("Copy the matching files.")
        self.assertEqual(select_skills.call_count, 2)

        planner_messages = captured_messages[0]
        self.assertIn("skills are selected only inside worker agents", planner_messages[0]["content"])
        self.assertNotIn("SKILL PROMPT", planner_messages[0]["content"])

        first_agent_messages = captured_messages[1]
        self.assertIn("SKILL PROMPT", first_agent_messages[0]["content"])
        self.assertIn("Assigned action (1/?):\nFind CV files.", first_agent_messages[1]["content"])
        self.assertIn("Previous agent output:\n<none>", first_agent_messages[1]["content"])

        second_planner_messages = captured_messages[2]
        self.assertEqual(second_planner_messages[-1]["role"], "tool")
        self.assertIn("Found two CV files.", second_planner_messages[-1]["content"])

        second_agent_messages = captured_messages[3]
        self.assertIn("SKILL PROMPT", second_agent_messages[0]["content"])
        self.assertIn("Assigned action (2/?):\nCopy the matching files.", second_agent_messages[1]["content"])
        self.assertIn("Previous agent output:\nFound two CV files.", second_agent_messages[1]["content"])

    def test_action_agent_is_limited_to_three_iterations_before_verification(self):
        chat_calls = []

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            chat_calls.append([message.copy() for message in messages])
            if tools is None:
                return {"message": {"content": "Final answer after three tool iterations."}}
            return {
                "message": {
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

        with patch.object(agent, "_select_skills_prompt", return_value=""), \
             patch.object(agent, "_execute_tool", return_value={"ok": True, "results": []}) as execute_tool, \
             patch.object(agent, "_verify_history", return_value=(True, "verified")) as verify, \
             patch("cterm.llm.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_action_agent(
                "Do work.",
                "Run bounded work.",
                "",
                1,
                1,
            )

        self.assertTrue(result["complete"])
        self.assertEqual(result["output"], "Final answer after three tool iterations.")
        self.assertEqual(execute_tool.call_count, 3)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(len(chat_calls), 4)

    def test_verifier_is_scoped_to_assigned_action(self):
        agent = ToolAgent("main")
        agent.tools = []

        with patch.object(agent, "_select_skills_prompt", return_value=""), \
             patch.object(agent, "_verify_history", return_value=(True, "")) as verify, \
             patch("cterm.llm.chat_with_model_api", return_value={"message": {"content": "Stopped Plex."}}):
            agent._run_action_agent(
                "Restart Plex.",
                "Stop Plex.",
                "",
                1,
                "?",
            )

        verification_task = verify.call_args.args[0]
        self.assertIn("Verify only whether the assigned action is complete", verification_task)
        self.assertIn("Assigned action:\nStop Plex.", verification_task)

    def test_debug_logs_orchestration_events(self):
        planner_calls = 0

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            nonlocal planner_calls
            is_planner = tools and tools[0]["function"]["name"] == "new_agent"
            if is_planner:
                planner_calls += 1
                if planner_calls == 1:
                    return {
                        "message": {
                            "tool_calls": [{
                                "function": {
                                    "name": "new_agent",
                                    "arguments": {"action": "Check CPU count."},
                                }
                            }]
                        }
                    }
                return {"message": {"content": "CPU count checked."}}
            return {"message": {"content": "CPU count is 16."}}

        agent = ToolAgent("main", debug=True)
        agent.tools = []

        with patch.object(agent, "_select_skills_prompt", return_value=""), \
             patch.object(agent, "_verify_history", return_value=(True, "")), \
             patch("cterm.llm.chat_with_model_api", side_effect=fake_chat), \
             patch("sys.stderr", new_callable=StringIO) as stderr:
            result = agent._run_with_native_tools("How many CPUs?")

        self.assertEqual(result, "CPU count checked.")
        debug_output = stderr.getvalue()
        self.assertIn("event=planner_dispatch_agent", debug_output)
        self.assertIn("event=agent_verified", debug_output)
        self.assertIn("event=planner_handoff_recorded", debug_output)
        self.assertIn("event=planner_final_answer", debug_output)
        self.assertNotIn("event=planner_start", debug_output)
        self.assertNotIn("event=planner_iteration", debug_output)
        self.assertNotIn("event=agent_tool_call", debug_output)
        self.assertNotIn("event=agent_start", debug_output)

    def test_completed_finder_matches_are_saved_and_only_path_is_handed_to_planner(self):
        captured_messages = []
        planner_calls = 0
        worker_calls = 0

        def fake_chat(model, messages, tools=None, binary="ollama", response_format=None):
            nonlocal planner_calls, worker_calls
            captured_messages.append([message.copy() for message in messages])
            is_planner = tools and tools[0]["function"]["name"] == "new_agent"
            if is_planner:
                planner_calls += 1
                if planner_calls == 1:
                    return {
                        "message": {
                            "tool_calls": [{
                                "function": {
                                    "name": "new_agent",
                                    "arguments": {"action": "Find CV files."},
                                }
                            }]
                        }
                    }
                return {"message": {"content": "Done."}}

            worker_calls += 1
            if worker_calls == 1:
                return {
                    "message": {
                        "tool_calls": [{
                            "function": {
                                "name": "finder",
                                "arguments": {
                                    "path": "~",
                                    "pattern": "*CV*",
                                    "type_filter": "file",
                                },
                            }
                        }]
                    }
                }
            return {"message": {"content": "Found CV files."}}

        agent = ToolAgent("main")
        agent.tools = []
        finder_result = {
            "ok": True,
            "path": "~",
            "matches": ["Documents/Ahmed_CV.pdf", "Desktop/Resume.docx"],
            "total": 2,
            "truncated": False,
        }

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}), \
             patch.object(agent, "_select_skills_prompt", return_value=""), \
             patch.object(agent, "_execute_tool", return_value=finder_result), \
             patch.object(agent, "_verify_history", return_value=(True, "")), \
             patch("cterm.llm.chat_with_model_api", side_effect=fake_chat):
            result = agent._run_with_native_tools("Find CVs.")

            self.assertEqual(result, "Done.")
            second_planner_messages = captured_messages[-1]
            handoff = second_planner_messages[-1]["content"]
            payload = json.loads(handoff)
            output = payload["output"]
            self.assertIn("Finder results file for planner and next agent:", output)
            self.assertIn("The JSON field `paths` is a list of path strings.", output)
            self.assertNotIn("~/Documents/Ahmed_CV.pdf", output)
            self.assertNotIn("~/Desktop/Resume.docx", output)
            self.assertIn('"complete": true', handoff)

            saved_path_text = output.split(
                "Finder results file for planner and next agent: ", 1
            )[1].split(". The JSON field", 1)[0]
            saved_path = Path(saved_path_text)
            self.assertEqual(saved_path.parent, Path(tmp) / "cterm" / "data")
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["result"], finder_result)
            self.assertNotIn("full_paths", saved)
            self.assertIsInstance(saved["paths"], list)
            self.assertTrue(all(isinstance(path, str) for path in saved["paths"]))
            self.assertEqual(
                saved["paths"],
                ["~/Documents/Ahmed_CV.pdf", "~/Desktop/Resume.docx"],
            )

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


if __name__ == "__main__":
    unittest.main()
