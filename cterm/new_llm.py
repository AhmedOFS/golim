
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
from cterm.llm_utils.utils import Spinner, _indent, _clip_label, _run_async, get_socket_path
from cterm.privilege import prompt_to_add_privileged_binary
from cterm.skills_loader import SkillsLoader
from cterm import task_tool



class ToolAgent:
    MAX_AGENT_ITERATIONS = 5

    def __init__(self, model, binary="ollama", small_model=None, debug=False):
        self.model = model
        self.small_model = small_model
        self.binary = binary
        self.debug = debug
        self.mcp_client = None
        self.tools = []

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
                if result.get("message"):
                    lines.append(f"   message: {result.get('message')}")
                if result.get("output_truncated"):
                    lines.append(f"   output truncated: true")
                    lines.append(f"   output file: {result.get('output_file')}")
                    lines.append(f"   output lines: {result.get('output_line_count')}")
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
                if "content" in result:
                    if result.get("path"):
                        lines.append(f"   path: {result.get('path')}")
                    if "page" in result:
                        lines.append(
                            f"   page: {result.get('page')} of {result.get('total_pages')}"
                        )
                        lines.append(f"   total lines: {result.get('total_lines')}")
                        if result.get("has_next_page"):
                            lines.append(f"   next page: {result.get('next_page')}")
                    content = _clip(result.get("content", ""))
                    if content:
                        lines.append(f"   content:\n{_indent(content, '      ')}")
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
        tool_history = result.get("tool_history", [])
        handoff_lines = []

        saved_path = self._save_last_finder_result(tool_history)
        if saved_path:
            handoff_lines.append(
                f"Finder results file for planner and next agent: {saved_path}. "
                "The JSON field `paths` is a list of path strings."
            )

        bash_output_path = self._last_bash_output_file(tool_history)
        if bash_output_path:
            handoff_lines.append(
                f"Bash output file for planner and next agent: {bash_output_path}. "
                "The JSON field `results` contains full stdout and stderr."
            )

        if not handoff_lines:
            return output
        handoff_text = "\n".join(handoff_lines)
        return f"{output}\n\n{handoff_text}".strip()

    def _last_bash_output_file(self, tool_history):
        for item in reversed(tool_history):
            if item.get("tool") != "bash":
                continue
            if item.get("status") != "success":
                continue
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            output_file = result.get("output_file")
            if output_file:
                return str(output_file)
        return None

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



    def run(self, user_message):
        return self._run_action_agent(user_message)

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
        label = _clip_label(args.get("command", tool_name)) if tool_name == "bash" else tool_name

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

        tools = [
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
        tools.append(task_tool.get_tool_definition())
        return tools

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

    def _run_action_agent(self, user_message):
        try:
            selected_skills, skills_prompt = self._select_skills(user_message)
            self._debug_orchestration(
                "planner_skills_selected",
                skills=[s.name for s in selected_skills],
            )

            system_prompt = self._agent_system_prompt()
            ollama_tools = self._ollama_tools(skills_prompt)
            self._debug_orchestration(
                "agent_tools_ready",
                tools=[tool["function"]["name"] for tool in ollama_tools],
            )

            no_tools_system_prompt = (
                f"{system_prompt} Do not call any tools in this final response; "
                "use the previous tool results to answer the task."
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            tool_history = []
            stopped_for_final_response = False

            for iteration in range(1, self.MAX_AGENT_ITERATIONS + 1):
                self._debug_orchestration(
                    "agent_iteration",
                    iteration=iteration,
                    max_iterations=self.MAX_AGENT_ITERATIONS,
                )

                spinner = Spinner("Thinking")
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

                    if tool_name == "create_new_task":
                        tool_result = task_tool.execute_task(
                            task=args.get("task"),
                            subagent_type=args.get("subagent_type"),
                            thoroughness_level=args.get("thoroughness_level"),
                        )
                    else:
                        tool_result = self._execute_tool(tool_name, args)

                    status = "failed" if (
                        isinstance(tool_result, dict) and
                        (tool_result.get("ok") is False or tool_result.get("error"))
                    ) else "success"

                    tool_history.append({
                        "tool": tool_name,
                        "arguments": args,
                        "result": tool_result,
                        "status": status,
                    })
                    self._debug_orchestration(
                        "agent_tool_result",
                        iteration=iteration,
                        tool=tool_name,
                        status=status,
                    )

                    messages.append(message)
                    messages.append({
                        "role": "tool",
                        "tool_name": tool_name,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    })

                    continue

                stopped_for_final_response = True
                break

            if not stopped_for_final_response:
                self._debug_orchestration(
                    "agent_iteration_limit",
                    max_iterations=self.MAX_AGENT_ITERATIONS,
                    tool_calls=len(tool_history),
                )

            if stopped_for_final_response:
                content = message.get("content") or ""
                self._debug_agent_response(content)
                self._debug_orchestration(
                    "agent_final_answer",
                    chars=len(content),
                )
                if not content:
                    content = (
                        "Agent completed the task, but did not produce a final "
                        "response.\n\n"
                        f"Execution history:\n{self._build_execution_summary(tool_history)}"
                    )
                return content

            final_messages = [
                {"role": "system", "content": no_tools_system_prompt},
                *messages[1:],
                {"role": "user", "content": "Provide a concise final summary of what was accomplished."},
            ]
            spinner = Spinner("Summarizing Task")
            spinner.start()
            try:
                response = chat_with_model_api(
                    self.model,
                    final_messages,
                    tools=None,
                    binary=self.binary,
                )
                content = response.get("message", {}).get("content") or ""
            finally:
                spinner.stop()
            self._debug_agent_response(content)
            self._debug_orchestration(
                "agent_final_answer",
                chars=len(content),
            )
            if not content:
                content = (
                    "Agent reached the iteration limit.\n\n"
                    f"Tool execution history:\n{self._build_execution_summary(tool_history)}"
                )
            return content

        except Exception as e:
            import traceback
            print(f"\nException in _run_action_agent: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return f"Error: {e}"




def chat_with_tools(model, message, binary="ollama", small_model=None, debug=False):
    with ToolAgent(model, binary, small_model, debug=debug) as agent:
        return agent.run(message)
