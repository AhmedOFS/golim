
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



class ToolAgent:
    MAX_TOOL_ITERATIONS = 10

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

    def _build_execution_summary(self, tool_history):
        if not tool_history:
            return "No tools have been used yet."

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
        return "\n".join(lines)

    def _verify_history(self, user_message, tool_history, final_answer=""):
        execution_summary = self._build_execution_summary(tool_history)
        verification_messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict task completion verifier.\n\n"
                    "Determine whether the original task has been fully completed.\n\n"
                    "Respond ONLY with valid JSON:\n"
                    '{ "complete": true|false, "summary": "..." }'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Original user request:\n\n"
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
            return bool(verification.get("complete")), verification.get("summary", "")
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
        is_shell = tool_name == "run_shell"
        label = args.get("command", tool_name) if tool_name == "run_shell" else tool_name

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

    def _run_with_native_tools(self, user_message):
        ollama_tools = [
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

        system_prompt = (
            "Use the tools available to you to perform the tasks "
            "or answer the questions asked of you on the user's system. "
            "Use the run_shell tool to execute commands, and use snap "
            "or apt for app installations when relevant. "
            "When the user asks about a specific file, inspect that file "
            "and answer from it; do not inspect unrelated files unless the "
            "specific file cannot be located or imports are required to "
            "answer the question. Use exact file paths from tool results; "
            "do not invent or rename paths in the final answer. "
            "When you have fully completed the task, respond with a plain text "
            "summary of what was done — do not make any further tool calls."
        )

        # Built once, appended to in-place
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        tool_history = []

        try:
            for iteration in range(1, self.MAX_TOOL_ITERATIONS + 1):

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

                # ----------------------------------------------------------
                # TOOL CALL
                # ----------------------------------------------------------
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

                    # Append tool exchange to running message list
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

                # ----------------------------------------------------------
                # NO TOOL CALLS — agent thinks it's done, run verifier
                # ----------------------------------------------------------
                content = message.get("content", "No response")

                complete, verifier_summary = self._verify_history(
                    user_message, tool_history, final_answer=content
                )

                if not complete:
                    if self.debug:
                        print(f"\n[debug] verifier=incomplete summary={verifier_summary!r}", file=sys.stderr)
                    messages.append({
                        "role": "user",
                        "content": f"Verifier says task is incomplete:\n{verifier_summary}\nPlease continue.",
                    })
                    continue

                self._debug_agent_response(content)
                print(f"\nTask complete (iteration {iteration})", file=sys.stderr)
                return content

            # ------------------------------------------------------------------
            # MAX ITERATIONS REACHED
            # ------------------------------------------------------------------
            print(
                f"\nReached maximum iterations ({self.MAX_TOOL_ITERATIONS})",
                file=sys.stderr
            )

            final_messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Original task:\n{user_message}\n\n"
                        f"Execution history:\n{self._build_execution_summary(tool_history)}\n\n"
                        "Summarise what was accomplished and what still needs to be done."
                    )
                }
            ]

            spinner = Spinner("Summarising")
            spinner.start()
            try:
                final = chat_with_model_api(
                    self.model, final_messages, ollama_tools, self.binary
                )
            finally:
                spinner.stop()

            return final.get("message", {}).get("content", "No response")

        except Exception as e:
            import traceback
            print(f"\n💥 Exception in _run_with_native_tools: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return f"Error: {e}"


def chat_with_tools(model, message, binary="ollama", small_model=None, debug=False):
    with ToolAgent(model, binary, small_model, debug=debug) as agent:
        return agent.run(message)

