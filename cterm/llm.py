
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
from cterm.privilege import prompt_to_add_privileged_binary
from cterm.skills_loader import SkillsLoader



class ToolAgent:
    MAX_AGENT_ITERATIONS = 10

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
                if "matches" in result:
                    root = result.get("path", "")
                    matches = result.get("matches") or []
                    lines.append(f"   path: {root}")
                    lines.append(f"   total matches: {result.get('total', len(matches))}")
                    lines.append(f"   truncated: {bool(result.get('truncated'))}")
                    for match in matches[:50]:
                        if root:
                            full_path = os.path.join(root, match)
                        else:
                            full_path = match
                        lines.append(f"   match: {full_path}")
                    if len(matches) > 50:
                        lines.append(f"   ... {len(matches) - 50} more matches omitted ...")
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

    def _build_worker_handoff(self, result):
        output = result.get("output") or ""
        saved_path = self._save_last_finder_result(result.get("tool_history", []))
        if not saved_path:
            return output
        return (
            f"{output}\n\n"
            f"Finder results file for planner and next agent: {saved_path}. "
            "The JSON field `paths` is a list of path strings."
        ).strip()

    def _last_finder_history_item(self, tool_history):
        for item in reversed(tool_history):
            if item.get("tool") != "finder":
                continue
            if item.get("status") != "success":
                continue
            result = item.get("result")
            if not isinstance(result, dict) or "matches" not in result:
                continue
            if result.get("ok") is not True:
                continue
            return item
        return None

    def _finder_result_paths(self, finder_result):
        root = str(finder_result.get("path") or "")
        matches = finder_result.get("matches") or []
        if not isinstance(matches, list):
            return []
        return [
            os.path.join(root, str(match)) if root else str(match)
            for match in matches
        ]

    def _finder_results_dir(self):
        return Path(os.path.expanduser("~/cterm/data"))

    def _save_last_finder_result(self, tool_history):
        item = self._last_finder_history_item(tool_history)
        if not item:
            return None

        result = item.get("result") or {}
        data_dir = self._finder_results_dir()
        data_dir.mkdir(parents=True, exist_ok=True)

        base_name = f"finder_results_{int(time.time() * 1000)}_{os.getpid()}"
        path = data_dir / f"{base_name}.json"
        counter = 2
        while path.exists():
            path = data_dir / f"{base_name}_{counter}.json"
            counter += 1

        payload = {
            "tool": "finder",
            "arguments": item.get("arguments", {}),
            "status": item.get("status"),
            "result": result,
            "paths": self._finder_result_paths(result),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def _verify_history(self, user_message, tool_history):
        execution_summary = self._build_execution_summary(tool_history)
        verification_messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict task completion verifier.\n\n"
                    "Determine whether the assigned task has been fully completed "
                    "based solely on the tool execution history.\n\n"
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
                    f"{execution_summary}"
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
            content = (response.get("message", {}).get("content") or "").strip()
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

        def _on_shell_stream(fd, line, end="\n"):
            output = f"\033[33m{line}\033[0m" if fd == "stderr" else line
            spinner.write_above(output, end=end)

        def _call_once(call_args):
            return _run_async(
                self.mcp_client.call_tool(
                    tool_name,
                    call_args,
                    stream_output=is_shell,
                    on_stream=_on_shell_stream if is_shell else None,
                )
            )

        spinner = Spinner(label, reserve_above=is_shell)
        spinner.start()
        if is_shell:
            spinner.write_above(f"$ {label}")

        try:
            tool_result = _call_once(args)
        finally:
            spinner.stop()

        if (
            is_shell
            and isinstance(tool_result, dict)
            and tool_result.get("approval_required")
            and tool_result.get("approval_kind") == "privileged_whitelist"
        ):
            binary = tool_result.get("binary", "")
            if not prompt_to_add_privileged_binary(binary):
                tool_result = {
                    "ok": False,
                    "error": f"Privileged command not approved: {binary}",
                }
            else:
                retry_args = dict(args)
                retry_args["allow_privileged"] = True
                spinner = Spinner(label, reserve_above=is_shell)
                spinner.start()
                try:
                    tool_result = _call_once(retry_args)
                finally:
                    spinner.stop()

        self._debug_tool_result(tool_name, args, tool_result)
        return tool_result

    def _select_skills(self, user_message):
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

        return selected, loader.render_for_system_prompt(selected)

    def _select_skills_prompt(self, user_message):
        _, prompt = self._select_skills(user_message)
        return prompt

    def _ollama_tools(self, skills_prompt=""):
        allowed = None
        if "## Filesystem_Operations" in skills_prompt:
            allowed = {"finder", "bash", "exec", "read_file", "write_file"}

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
            if allowed is None or t.name in allowed
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
                    "Run one sequential worker agent on an atomic concrete action. "

                    
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

    def _worker_tools_notice(self):
        names = sorted(
            str(getattr(tool, "name", ""))
            for tool in self.tools
            if getattr(tool, "name", "")
        )
        if not names:
            return "Worker tools available: none loaded."
        return f"Worker tools available: {', '.join(names)}."

    def _run_action_agent(self, original_task, action, previous_output, step_index, total_steps, skills_prompt=""):
        system_prompt = self._agent_system_prompt()
        ollama_tools = self._ollama_tools(skills_prompt)
        self._debug_orchestration(
            "agent_tools_ready",
            step=step_index,
            tools=[tool["function"]["name"] for tool in ollama_tools],
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
        stopped_for_final_response = False

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
                    "role": "user",
                    "content": (
                        f"Tool execution result for `{tool_name}`:\n"
                        f"Arguments:\n{self._compact_json(args)}\n\n"
                        f"Result:\n{self._compact_json(compact_result)}\n\n"
                        "Continue the assigned action. If it is complete, "
                        "respond with the final concise summary."
                    ),
                })

                continue

            stopped_for_final_response = True
            break

        if not stopped_for_final_response:
            self._debug_orchestration(
                "agent_iteration_limit",
                step=step_index,
                max_iterations=self.MAX_AGENT_ITERATIONS,
                tool_calls=len(tool_history),
            )

        verification_task = (
            f"Assigned action:\n{action}\n\n"
            f"Context from original user request:\n{original_task}\n\n"
            f"Previous agent output, if relevant:\n{previous_output or '<none>'}\n\n"
            "Verify only whether the assigned action is complete. Do not require "
            "later or broader user-request steps to be complete."
        )
        complete, verifier_summary = self._verify_history(
            verification_task, tool_history
        )
        self._debug_orchestration(
            "agent_verified",
            step=step_index,
            complete=complete,
            summary=verifier_summary,
        )

        if complete:
            final_instruction = (
                "The verifier deemed the assigned action complete. Provide only "
                "the concise final output for this assigned action using the tool "
                "results already available. Do not mention the verifier."
            )
            if not stopped_for_final_response:
                final_instruction = (
                    f"You reached the {self.MAX_AGENT_ITERATIONS}-iteration tool "
                    "limit, but the verifier deemed the assigned action complete. "
                    "Provide only the concise final output for this assigned action "
                    "using the tool results already available. Do not mention the verifier."
                )
            final_messages = [
                {"role": "system", "content": no_tools_system_prompt},
                *messages[1:],
                {"role": "user", "content": final_instruction},
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
                final_answer = response.get("message", {}).get("content") or ""
            finally:
                spinner.stop()
            self._debug_agent_response(final_answer)
            self._debug_orchestration(
                "agent_final_answer",
                step=step_index,
                chars=len(final_answer),
            )
            if not final_answer:
                final_answer = (
                    "Agent completed the assigned action, but did not produce a final "
                    "response.\n\n"
                    f"Execution history:\n{self._build_execution_summary(tool_history)}"
                )
        else:
            final_answer = verifier_summary or "Verifier determined the assigned action is incomplete."

        return {
            "action": action,
            "output": final_answer,
            "complete": complete,
            "verifier_summary": verifier_summary,
            "tool_history": tool_history,
        }

    def _run_with_native_tools(self, user_message):
        try:
            selected_skills, skills_prompt = self._select_skills(user_message)
            self._debug_orchestration(
                "planner_skills_selected",
                skills=[s.name for s in selected_skills],
            )

            previous_output = ""
            step_results = []

            planner_system = (
                "You are a planner. First decompose the user's request "
                "into concrete sequential actions. Describe actions and tasks not commands"
                "You have exactly one "
                "tool: new_agent. Call new_agent once for each action, "
                "in order. Wait for each result before calling the next "
                "agent. "
 
            )

            planner_messages = [
                {"role": "system", "content": planner_system},
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
                    content = message.get("content") or ""
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
                try:
                    result = self._run_action_agent(
                        user_message, action, previous_output, step_index, "?", skills_prompt
                    )
                except Exception as exc:
                    print(
                        f"[debug] planner_agent_failed action={action!r} "
                        f"step={step_index} previous_output_len={len(previous_output) if previous_output else 0} "
                        f"error={exc}",
                        file=sys.stderr,
                    )
                    raise
                step_results.append(result)
                previous_output = self._build_worker_handoff(result)
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
                        "tool_name": "new_agent",
                        "content": self._compact_json({
                            "action": result["action"],
                            "output": previous_output,
                            "complete": result["complete"],
                        }),
                    })
                    # planner sees complete=false and can dispatch a follow-up agent
                else:
                    planner_messages.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "type": "function",
                            "function": {
                                "name": "new_agent",
                                "arguments": {"action": action},
                            }
                        }],
                    })
                    planner_messages.append({
                        "role": "tool",
                        "tool_name": "new_agent",
                        "content": self._compact_json({
                            "action": result["action"],
                            "output": previous_output,
                            "complete": result["complete"],
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
