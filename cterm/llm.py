
"""LLM interaction module for cterm using Ollama's native tool calling"""
import subprocess
import sys
import threading
import time
import json
import asyncio
import os
import socket
import select as _select
from pathlib import Path

from cterm.llm_utils.chat_api import chat_with_model_api
from cterm.llm_utils.mcp_client import FastMCPClient
from cterm.llm_utils.utils import Spinner, _indent, _run_async, get_socket_path
from cterm.skills_loader import SkillsLoader



class ToolAgent:
    MAX_AGENT_ITERATIONS = 3

    def __init__(self, model, binary="ollama", small_model=None, debug=False):
        self.model = model
        self.small_model = small_model
        self.binary = binary
        self.debug = debug
        self.mcp_client = None
        self.tools = []
        self.use_native_tools = self._check_native_tool_support()

    def _check_native_tool_support(self):
        try:
            import requests
            return requests.get("http://localhost:11434/api/tags", timeout=5).status_code == 200
        except Exception:
            return False

    def __enter__(self):
        socket_path = get_socket_path()
        def _try_connect():
            self.mcp_client = FastMCPClient(socket_path)
            self.tools = asyncio.run(self.mcp_client.list_tools())

        try:
            _try_connect()
        except (ConnectionRefusedError, FileNotFoundError):
            try:
                subprocess.run(["systemctl", "--user", "start", "cterm-mcp.service"],
                               check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError:
                raise RuntimeError("Failed to activate tool server.")
            for _ in range(10):
                time.sleep(0.2)
                try:
                    _try_connect()
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    continue
            else:
                raise TimeoutError("Tool server started but socket never became available.")
        return self

    def __exit__(self, *_):
        if self.mcp_client:
            self.mcp_client.close()

    def _compact_json(self, value):
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return json.dumps(str(value), ensure_ascii=False)

    def _debug_orchestration(self, event, **fields):
        if not self.debug:
            return

        details = " ".join(
            f"{key}={self._compact_json(value)}" for key, value in fields.items()
        )
        suffix = f" {details}" if details else ""
        print(f"\n[debug] orchestration event={event}{suffix}", file=sys.stderr)

    def _build_execution_summary(self, tool_history):
        if not tool_history:
            return "No tools have been used yet."

        def _clip(text, limit=1200):
            if not text:
                return ""
            text = str(text)
            if len(text) <= limit:
                return text
            return text[:limit] + "\n...<truncated>..."

        lines = []
        for idx, item in enumerate(tool_history, start=1):
            status = item.get("status", "unknown")
            lines.append(f"{idx}. {item.get('tool')} -> {status}")
            args = item.get("arguments")
            if args:
                lines.append(f"   arguments: {self._compact_json(args)}")
            result = item.get("result")
            if isinstance(result, dict):
                if result.get("error"):
                    lines.append(f"   error: {result.get('error')}")
                for result_idx, entry in enumerate(result.get("results", [])[:3], start=1):
                    if not isinstance(entry, dict):
                        continue
                    if "returncode" in entry:
                        lines.append(f"   result {result_idx} returncode: {entry.get('returncode')}")
                    stdout = _clip(entry.get("stdout", ""))
                    if stdout:
                        lines.append(f"   result {result_idx} stdout:\n{_indent(stdout, '      ')}")
                    stderr = _clip(entry.get("stderr", ""), limit=500)
                    if stderr:
                        lines.append(f"   result {result_idx} stderr:\n{_indent(stderr, '      ')}")
        return "\n".join(lines)

    def _verify_history(self, user_message, tool_history, final_answer=""):
        execution_summary = self._build_execution_summary(tool_history)
        verification_messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict task completion verifier.\n\n"
                    "Determine whether the assigned task has been fully completed.\n\n"
                    "Respond ONLY with valid JSON:\n"
                    '{ "complete": true|false, "summary": "..." }'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Assigned task:\n\n"
                    f"{user_message}\n\n"
                    f"Tool execution history:\n\n"
                    f"{execution_summary}\n\n"
                    f"Assistant final answer:\n\n"
                    f"{final_answer or '<none>'}"
                ),
            },
        ]

        spinner = Spinner("Evaluating Completion")
        spinner.start()
        try:
            response = chat_with_model_api(
                self.small_model or self.model,
                verification_messages,
                tools=None,
                binary=self.binary,
                response_format="json",
            )
            content = response.get("message", {}).get("content", "").strip()
            verification = self._parse_json_object(content)
            complete = bool(verification.get("complete"))
            summary = verification.get("summary", "")
            self._debug_orchestration(
                "verifier_result",
                complete=complete,
                summary=summary,
            )
            return complete, summary
        except Exception as e:
            if self.debug:
                print(f"\n[debug] verifier_failed error={e}", file=sys.stderr)
            return False, "Verifier could not determine completion."
        finally:
            spinner.stop()

    def run(self, user_message):
        return self._run_with_native_tools(user_message) if self.use_native_tools else print("tools aren't supported by ")

    def _parse_json_object(self, content):
        decoder = json.JSONDecoder()
        content = content.strip()
        for idx, char in enumerate(content):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(content[idx:])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
        raise ValueError(f"No JSON object found in response: {content!r}")

    def _debug_tool_result(self, tool_name, args, result):
        if not self.debug:
            return

        ok = result.get("ok") if isinstance(result, dict) else None
        if ok is True:
            state = "success"
        elif ok is False:
            state = "failed"
        else:
            state = "unknown"

        print(
            f"\n[debug] tool={tool_name} state={state} args={json.dumps(args)}",
            file=sys.stderr,
        )

    def _debug_agent_response(self, content):
        if not self.debug:
            return

        print(f"\n[debug] agent_response={content!r}", file=sys.stderr)

    def _execute_tool(self, tool_name, args):
        is_shell = tool_name == "bash"
        label = args.get("command", tool_name) if tool_name == "bash" else tool_name

        spinner = Spinner(label, reserve_above=is_shell)
        spinner.start()

        def _on_shell_stream(fd, line, end="\n"):
            output = f"\033[33m{line}\033[0m" if fd == "stderr" else line
            spinner.write_above(output, end=end)

        if is_shell:
            spinner.write_above(f"$ {label}")

        try:
            tool_result = _run_async(
                self.mcp_client.call_tool(
                    tool_name,
                    args,
                    stream_output=is_shell,
                    on_stream=_on_shell_stream if is_shell else None,
                )
            )
        finally:
            spinner.stop()

        self._debug_tool_result(tool_name, args, tool_result)
        return tool_result

    def _select_skills_prompt(self, user_message):
        loader = SkillsLoader(debug=self.debug)
        spinner = Spinner("Selecting Skills")
        spinner.start()
        try:
            selected = loader.select(
                user_message,
                self.small_model or self.model,
                chat_with_model_api,
                binary=self.binary,
            )
        except Exception as e:
            if self.debug:
                print(f"\n[debug] skills_selection_failed error={e}", file=sys.stderr)
            selected = []
        finally:
            spinner.stop()

        if self.debug:
            names = [skill.name for skill in selected]
            print(f"\n[debug] selected_skills={json.dumps(names)}", file=sys.stderr)

        return loader.render_for_system_prompt(selected)

    def _ollama_tools(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": getattr(t, 'description', ''),
                    "parameters": getattr(t, 'parameters', {})
                }
            }
            for t in self.tools
        ]

    def _agent_system_prompt(self):
        return (
            "Use the tools available to you to perform the tasks "
            "assigned to you on the user's system. "
            "Use the bash tool to execute commands, and use snap "
            "or apt for app installations when relevant. "
            "When the user asks about a specific file, inspect that file "
            "and answer from it; do not inspect unrelated files unless the "
            "specific file cannot be located or imports are required to "
            "answer the question. Use exact file paths from tool results; "
            "do not invent or rename paths in the final answer. "
            "Work only on the assigned action. When the action is complete, "
            "respond with a concise plain text summary of what was done and "
            "any important result for the next agent."
        )

    def _planner_tools(self):
        return [{
            "type": "function",
            "function": {
                "name": "new_agent",
                "description": (
                    "Run one sequential worker agent on a concrete action. "
                    "The worker receives the previous worker's final output, "
                    "has at most three iterations, and is verified before the "
                    "planner can continue."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "The concrete action for the next worker agent to complete.",
                        },
                    },
                    "required": ["action"],
                },
            },
        }]

    def _run_action_agent(self, original_task, action, previous_output, step_index, total_steps):
        ollama_tools = self._ollama_tools()
        system_prompt = self._agent_system_prompt()
        skills_prompt = self._select_skills_prompt(action)
        if skills_prompt:
            system_prompt = f"{system_prompt}\n\n{skills_prompt}"
        self._debug_orchestration(
            "agent_skills_ready",
            step=step_index,
            injected=bool(skills_prompt),
        )
        no_tools_system_prompt = (
            f"{system_prompt} Do not call any tools in this final response; "
            "use the previous tool results to answer the assigned action."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Original user request:\n{original_task}\n\n"
                    f"Assigned action ({step_index}/{total_steps}):\n{action}\n\n"
                    f"Previous agent output:\n{previous_output or '<none>'}"
                ),
            },
        ]

        tool_history = []
        final_answer = ""

        for iteration in range(1, self.MAX_AGENT_ITERATIONS + 1):
            self._debug_orchestration(
                "agent_iteration",
                step=step_index,
                iteration=iteration,
                max_iterations=self.MAX_AGENT_ITERATIONS,
            )

            spinner = Spinner(f"Agent {step_index}/{total_steps}")
            spinner.start()
            try:
                response = chat_with_model_api(
                    self.model,
                    messages,
                    ollama_tools,
                    self.binary
                )
            finally:
                spinner.stop()

            message = response.get("message", {})

            if message.get("tool_calls"):
                function = message["tool_calls"][0].get("function", {})
                tool_name = function.get("name")
                args = function.get("arguments", {})

                tool_result = self._execute_tool(tool_name, args)

                compact_result = tool_result

                status = "failed" if (
                    isinstance(tool_result, dict) and
                    (tool_result.get("ok") is False or tool_result.get("error"))
                ) else "success"

                tool_history.append({
                    "tool": tool_name,
                    "arguments": args,
                    "result": compact_result,
                    "status": status,
                })
                self._debug_orchestration(
                    "agent_tool_result",
                    step=step_index,
                    iteration=iteration,
                    tool=tool_name,
                    status=status,
                )

                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": tool_name,
                            "arguments": args,
                        }
                    }],
                })
                messages.append({
                    "role": "tool",
                    "content": self._compact_json(compact_result),
                })

                continue

            final_answer = message.get("content", "No response")
            self._debug_agent_response(final_answer)
            self._debug_orchestration(
                "agent_final_answer",
                step=step_index,
                iteration=iteration,
                chars=len(final_answer),
            )
            break

        if not final_answer:
            self._debug_orchestration(
                "agent_iteration_limit",
                step=step_index,
                max_iterations=self.MAX_AGENT_ITERATIONS,
                tool_calls=len(tool_history),
            )
            final_messages = [
                {"role": "system", "content": no_tools_system_prompt},
                *messages[1:],
                {
                    "role": "user",
                    "content": (
                        f"You have reached the {self.MAX_AGENT_ITERATIONS}-iteration "
                        "tool limit. Provide the best final response for this assigned "
                        "action using the tool results already available."
                    ),
                },
            ]
            spinner = Spinner(f"Agent {step_index}/{total_steps} Final")
            spinner.start()
            try:
                response = chat_with_model_api(
                    self.model,
                    final_messages,
                    tools=None,
                    binary=self.binary,
                )
                final_answer = response.get("message", {}).get("content", "")
            finally:
                spinner.stop()
            self._debug_orchestration(
                "agent_forced_final_answer",
                step=step_index,
                chars=len(final_answer),
            )
            if not final_answer:
                final_answer = (
                    f"Agent reached the {self.MAX_AGENT_ITERATIONS}-iteration limit.\n\n"
                    f"Execution history:\n{self._build_execution_summary(tool_history)}"
                )

        verification_task = (
            f"Assigned action:\n{action}\n\n"
            f"Context from original user request:\n{original_task}\n\n"
            f"Previous agent output, if relevant:\n{previous_output or '<none>'}\n\n"
            "Verify only whether the assigned action is complete. Do not require "
            "later or broader user-request steps to be complete."
        )
        complete, verifier_summary = self._verify_history(
            verification_task, tool_history, final_answer=final_answer
        )
        self._debug_orchestration(
            "agent_verified",
            step=step_index,
            complete=complete,
            summary=verifier_summary,
        )

        return {
            "action": action,
            "output": final_answer,
            "complete": complete,
            "verifier_summary": verifier_summary,
            "tool_history": tool_history,
        }

    def _run_with_native_tools(self, user_message):
        try:
            previous_output = ""
            step_results = []
            planner_messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a planner. First decompose the user's request "
                        "into concrete sequential actions. You have exactly one "
                        "tool: new_agent. Call new_agent once for each action, "
                        "in order. Wait for each result before calling the next "
                        "agent. Do not use task-specific skills yourself; skills "
                        "are selected only inside worker agents. When all actions "
                        "are complete, respond to the user with a concise final "
                        "answer based on the worker outputs. Keep naturally atomic "
                        "tasks together: for example, restarting a service is one "
                        "action that includes stopping, starting, and checking status; "
                        "installing an app is one action that includes checking, "
                        "installing if needed, and verifying. Actions should not embed "
                        "unsupported shell syntax such as cd, ||, ;, command substitution, "
                        "or extra tool arguments; worker agents can use separate bash "
                        "calls when needed."
                    ),
                },
                {"role": "user", "content": user_message},
            ]

            for planner_iteration in range(1, 25):
                spinner = Spinner("Planning")
                spinner.start()
                try:
                    response = chat_with_model_api(
                        self.model,
                        planner_messages,
                        self._planner_tools(),
                        self.binary,
                    )
                finally:
                    spinner.stop()

                message = response.get("message", {})
                if not message.get("tool_calls"):
                    content = message.get("content", "")
                    self._debug_orchestration(
                        "planner_final_answer",
                        iteration=planner_iteration,
                        completed_agents=len(step_results),
                        chars=len(content),
                    )
                    if content:
                        print(f"\nTask complete ({len(step_results)} agents)", file=sys.stderr)
                        return content
                    if step_results:
                        return step_results[-1]["output"]
                    return "No response"

                function = message["tool_calls"][0].get("function", {})
                tool_name = function.get("name")
                args = function.get("arguments", {})
                if tool_name != "new_agent":
                    self._debug_orchestration(
                        "planner_unknown_tool",
                        iteration=planner_iteration,
                        tool=tool_name,
                    )
                    return f"Planner requested unknown tool: {tool_name}"

                action = str(args.get("action", "")).strip()
                if not action:
                    self._debug_orchestration(
                        "planner_missing_action",
                        iteration=planner_iteration,
                        arguments=args,
                    )
                    return "Planner requested new_agent without an action."

                step_index = len(step_results) + 1
                self._debug_orchestration(
                    "planner_dispatch_agent",
                    iteration=planner_iteration,
                    step=step_index,
                    action=action,
                    previous_output=previous_output or None,
                )
                result = self._run_action_agent(
                    user_message, action, previous_output, step_index, "?"
                )
                step_results.append(result)
                previous_output = result["output"]
                self._debug_orchestration(
                    "planner_agent_result",
                    iteration=planner_iteration,
                    step=step_index,
                    action=action,
                    complete=result["complete"],
                    output_chars=len(result["output"]),
                    verifier_summary=result["verifier_summary"],
                )

                if not result["complete"]:
                    if self.debug:
                        print(
                            f"\n[debug] verifier=incomplete action={action!r} "
                            f"summary={result['verifier_summary']!r}",
                            file=sys.stderr,
                        )
                    # Instead of returning, just record it and let the planner continue
                    planner_messages.append({
                        "role": "tool",
                        "content": self._compact_json({
                            "action": result["action"],
                            "output": result["output"],
                            "complete": result["complete"],
                            "verifier_summary": result["verifier_summary"],
                        }),
                    })
                    # planner sees complete=false and can dispatch a follow-up agent
                else:
                    planner_messages.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": "new_agent",
                                "arguments": {"action": action},
                            }
                        }],
                    })
                    planner_messages.append({
                        "role": "tool",
                        "content": self._compact_json({
                            "action": result["action"],
                            "output": result["output"],
                            "complete": result["complete"],
                            "verifier_summary": result["verifier_summary"],
                        }),
                    })
                self._debug_orchestration(
                    "planner_handoff_recorded",
                    iteration=planner_iteration,
                    step=step_index,
                    planner_messages=len(planner_messages),
                )

            self._debug_orchestration(
                "planner_iteration_limit",
                completed_agents=len(step_results),
                last_output_chars=len(previous_output),
            )
            return (
                "Planner reached its iteration limit.\n\n"
                f"Last agent output:\n{previous_output or 'No agent was run.'}"
            )

        except Exception as e:
            import traceback
            print(f"\n💥 Exception in _run_with_native_tools: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return f"Error: {e}"


def chat_with_tools(model, message, binary="ollama", small_model=None, debug=False):
    with ToolAgent(model, binary, small_model, debug=debug) as agent:
        return agent.run(message)
