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
    MAX_SHELL_RETRIES = 3
    MAX_TOOL_ITERATIONS = 10

    def __init__(self, model, binary="ollama", small_model=None):
        self.model = model
        self.small_model = small_model
        self.binary = binary
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
    def verify_task_completion(self, user_message, messages, tool_history=None):
        """
        Analyse previous tool calls and update execution history,
        then ask the verifier LLM whether the task is complete.

        Returns:
            complete (bool)
            summary (str)
            tool_history (list)
            execution_summary (str)
        """

        import json

        # ------------------------------------------------------------------
        # Persistent execution history
        # ------------------------------------------------------------------
        if tool_history is None:
            tool_history = []

        current_tool = None

        # ------------------------------------------------------------------
        # Extract NEW tool calls + outcomes from current messages
        # ------------------------------------------------------------------
        for msg in messages:
            role = msg.get("role")

            # --------------------------------------------------------------
            # Assistant tool call
            # --------------------------------------------------------------
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    function = tc.get("function", {})

                    current_tool = {
                        "tool": function.get("name"),
                        "arguments": function.get("arguments", {}),
                        "status": "unknown",
                        "result": None,
                    }

            # --------------------------------------------------------------
            # Tool result
            # --------------------------------------------------------------
            elif role == "tool" and current_tool:
                try:
                    result = json.loads(msg.get("content", "{}"))
                except Exception:
                    result = {"raw": msg.get("content")}

                current_tool["result"] = result

                # ----------------------------------------------------------
                # Determine success/failure
                # ----------------------------------------------------------
                if isinstance(result, dict):
                    ok = result.get("ok")

                    if ok is True:
                        current_tool["status"] = "success"
                    elif ok is False:
                        current_tool["status"] = "failed"
                    else:
                        if result.get("error"):
                            current_tool["status"] = "failed"
                        else:
                            current_tool["status"] = "success"
                else:
                    current_tool["status"] = "success"

                tool_history.append(current_tool)
                current_tool = None

        # ------------------------------------------------------------------
        # Build readable execution summary
        # ------------------------------------------------------------------
        tool_summary_lines = []

        if not tool_history:
            tool_summary_lines.append("No tools were used.")
        else:
            for idx, tool in enumerate(tool_history, start=1):

                line = f"{idx}. {tool['tool']} -> {tool['status']}"
                tool_summary_lines.append(line)

                args = tool.get("arguments")
                if args:
                    tool_summary_lines.append(
                        f"   arguments: {json.dumps(args)}"
                    )

                result = tool.get("result")

                if isinstance(result, dict):

                    error = result.get("error")
                    if error:
                        tool_summary_lines.append(
                            f"   error: {error}"
                        )

                    # Optional compact result preview
                    preview = {
                        k: v
                        for k, v in result.items()
                        if k not in ["stdout", "stderr"]
                    }

                    if preview:
                        tool_summary_lines.append(
                            f"   result: {json.dumps(preview)[:300]}"
                        )

        execution_summary = "\n".join(tool_summary_lines)

        # ------------------------------------------------------------------
        # Verification context
        # ------------------------------------------------------------------
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
                    f"{execution_summary}"
                ),
            },
        ]

        # ------------------------------------------------------------------
        # Ask verifier model
        # ------------------------------------------------------------------

        spinner = Spinner("Evaluating Completion")
        spinner.start()



        response = chat_with_model_api(
            self.small_model,
            verification_messages,
            tools=None,
            binary=self.binary,
        )
        spinner.stop()
        content = (
            response.get("message", {})
            .get("content", "")
            .strip()
        )

        # ------------------------------------------------------------------
        # Parse verifier response
        # ------------------------------------------------------------------
        try:
            verification = json.loads(content)

            complete = bool(verification.get("complete"))
            summary = verification.get("summary", "")

        except Exception:
            complete = False
            summary = (
                "Failed to parse verifier response.\n\n"
                f"Raw response:\n{content}"
            )
            print("failure to parse")
        return complete, summary, tool_history, execution_summary
    

    def run(self, user_message):
        return self._run_with_native_tools(user_message) if self.use_native_tools else print("tools aren't supported by ")

    def _execute_tool_with_retry(self, tool_name, args, messages, ollama_tools):
        attempt = 0
        while True:
            is_shell = tool_name == "run_shell"


            label = args.get("command", tool_name) if tool_name == "run_shell" else tool_name

            spinner = Spinner(label, reserve_above=is_shell)
            spinner.start()

            def _on_shell_stream(fd, line, end="\n"):
                output = f"\033[33m{line}\033[0m" if fd == "stderr" else line
                spinner.write_above(output, end=end)

            if is_shell:
                spinner.write_above(f"$ {label}")

            tool_result = _run_async(
                self.mcp_client.call_tool(
                    tool_name,
                    args,
                    stream_output=is_shell,
                    on_stream=_on_shell_stream if is_shell else None,
                )
            )
            spinner.stop()
            if not is_shell or tool_result.get("ok", True):
                return tool_result, messages

            attempt += 1
            if attempt >= self.MAX_SHELL_RETRIES:
                return tool_result, messages

            failed_cmd = args.get("command", "<unknown>")
            error_msg  = tool_result.get("error", "")
            stdout_out = "".join(r.get("stdout", "") + "\n" for r in tool_result.get("results", []))
            stderr_out = "".join(r.get("stderr", "") + "\n" for r in tool_result.get("results", []))

            failure_summary = (
                f"The command `{failed_cmd}` failed.\nError: {error_msg}\n"
                + (f"Stdout output:\n{stdout_out.strip()}\n" if stdout_out.strip() else "")
                + (f"Stderr output:\n{stderr_out.strip()}\n" if stderr_out.strip() else "")
                + "Please analyse the error and call run_shell again with a corrected command that addresses the problem."
            )
            messages = list(messages)
            messages += [{"role": "tool", "content": json.dumps(tool_result)},
                        {"role": "user",  "content": failure_summary}]

            spinner = Spinner("Retrying")
            spinner.start()
            retry_response = chat_with_model_api(self.model, messages, ollama_tools, self.binary)
            spinner.stop()

            retry_message = retry_response.get("message", {})
            if not retry_message.get("tool_calls"):
                return tool_result, messages

            retry_call = retry_message["tool_calls"][0]
            tool_name  = retry_call["function"]["name"]
            args       = retry_call["function"].get("arguments", {})
            messages.append(retry_message)

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
            "or apt for app installations when relevant."
        )

        tool_history = []
        execution_summary = "No tools have been used yet."

        try:

            for iteration in range(1, self.MAX_TOOL_ITERATIONS + 1):

                # print(
                #     f"\nIteration {iteration}/{self.MAX_TOOL_ITERATIONS}",
                #     file=sys.stderr
                # )

                # ----------------------------------------------------------
                # RESET CONTEXT EVERY ITERATION
                # ----------------------------------------------------------
                messages = [
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Original task:\n{user_message}\n\n"
                            f"Execution history:\n{execution_summary}\n\n"
                            "Continue the task using tools if required."
                        )
                    }
                ]

                spinner = Spinner("Thinking")
                spinner.start()

                response = chat_with_model_api(
                    self.model,
                    messages,
                    ollama_tools,
                    self.binary
                )

                spinner.stop()

                message = response.get("message", {})

                # ----------------------------------------------------------
                # TOOL CALL
                # ----------------------------------------------------------
                if message.get("tool_calls"):

                    function = message["tool_calls"][0].get("function", {})

                    tool_name = function.get("name")
                    args = function.get("arguments", {})

                    messages.append(message)

                    tool_result, messages = self._execute_tool_with_retry(
                        tool_name,
                        args,
                        messages,
                        ollama_tools
                    )

                    # Ensure tool message exists
                    if messages[-1].get("role") != "tool":
                        messages.append({
                            "role": "tool",
                            "content": json.dumps(tool_result)
                        })

                    # ------------------------------------------------------
                    # VERIFY TASK + UPDATE EXECUTION HISTORY
                    # ------------------------------------------------------
                    complete, summary, tool_history, execution_summary = (
                        self.verify_task_completion(
                            user_message,
                            messages,
                            tool_history
                        )
                    )


                    if complete:
                        print(
                            f"\nTask complete",
                            file=sys.stderr
                        )
                        return summary

                    continue

                # ----------------------------------------------------------
                # NO TOOL CALLS
                # ----------------------------------------------------------
                complete, summary, tool_history, execution_summary = (
                    self.verify_task_completion(
                        user_message,
                        messages,
                        tool_history
                    )
                )

                if complete:
                    print(
                        f"\nTask complete (iteration {iteration})",
                        file=sys.stderr
                    )
                    return summary

                # ----------------------------------------------------------
                # MODEL FAILED TO CONTINUE
                # ----------------------------------------------------------
                print(
                    "\nModel returned no tool call and task "
                    "is not complete.",
                    file=sys.stderr
                )

            # --------------------------------------------------------------
            # MAX ITERATIONS REACHED
            # --------------------------------------------------------------
            print(
                f"\nReached maximum iterations "
                f"({self.MAX_TOOL_ITERATIONS})",
                file=sys.stderr
            )

            final_messages = [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": (
                        f"Original task:\n{user_message}\n\n"
                        f"Execution history:\n{execution_summary}\n\n"
                        "Summarise what was accomplished and "
                        "what still needs to be done."
                    )
                }
            ]

            spinner = Spinner("Summarising")
            spinner.start()

            final = chat_with_model_api(
                self.model,
                final_messages,
                ollama_tools,
                self.binary
            )

            spinner.stop()

            return final.get("message", {}).get(
                "content",
                "No response"
            )

        except Exception as e:

            import traceback

            print(
                f"\n💥 Exception in _run_with_native_tools: {e}",
                file=sys.stderr
            )

            traceback.print_exc(file=sys.stderr)

            return self._run_with_fallback(user_message)

def chat_with_tools(model, message, binary="ollama", small_model=None):
    with ToolAgent(model, binary, small_model) as agent:
        return agent.run(message)
