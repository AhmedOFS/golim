
"""LLM interaction module for cterm using Ollama's native tool calling"""
import logging
import json
import os
import shlex
import concurrent.futures
import queue
import threading

from cterm.core.agent_ui import AgentUI
from cterm.ui.basic.basic import TerminalUI
from cterm.config import Config, get_config
from cterm.api.chat_api import chat_with_model_api
from cterm.core.utils import _CONTENT_MARKER, _DIRECT_THINKING_KEYS, _FINAL_SUMMARY_PROMPT, _PYTHON_DENIED_RESULT, _REASONING_DETAIL_KEYS, _THINKING_KEYS, _TRACE_MARKER, _TRACE_ONLY_MARKER, _detect_python_in_bash, _indent, _clip_label, _run_async

logger = logging.getLogger(__name__)



def _clip_text(text, limit=1200):
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...<truncated>..."


class ToolAgent:
    MAX_AGENT_ITERATIONS = 50
    LONG_TOOL_NOTICE_SECONDS = 30

    def __init__(
        self,
        model,
        binary="ollama",
        small_model=None,
        debug=False,
        ui: AgentUI | None = None,
        mcp_client=None,
        tools=None,
        should_interrupt=None,
        should_hard_cancel=None,
    ):
        self.model = model
        self.small_model = small_model
        self.binary = binary
        self.debug = debug
        self.mcp_client = mcp_client
        self.tools = list(tools or [])
        self.ui = ui or TerminalUI()
        self._last_thinking_trace = ""
        self.messages = []
        self.execution_history = []
        self.result = None
        self.system_prompt = None
        self._should_interrupt = should_interrupt or (lambda: False)
        self._should_hard_cancel = should_hard_cancel or (lambda: False)
        self._active_messages = []
        self.MAX_AGENT_ITERATIONS = get_config().max_iteration_limit

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

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
        logger.debug("orchestration event=%s%s", event, suffix)

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
                self._append_result_summary(lines, result)
        return "\n".join(lines)

    def _append_result_summary(self, lines, result):
        if result.get("error"):
            lines.append(f"   error: {result.get('error')}")
        if result.get("message"):
            lines.append(f"   message: {result.get('message')}")
        if result.get("output_truncated"):
            lines.append(f"   output truncated: true")
            lines.append(f"   output file: {result.get('output_file')}")
            lines.append(f"   output lines: {result.get('output_line_count')}")
        if "matches" in result:
            self._append_matches_summary(lines, result)
        if "content" in result:
            self._append_content_summary(lines, result)
        self._append_command_results_summary(lines, result)

    def _append_matches_summary(self, lines, result):
        root = result.get("path", "")
        matches = result.get("matches") or []
        lines.append(f"   path: {root}")
        lines.append(f"   total matches: {result.get('total', len(matches))}")
        lines.append(f"   truncated: {bool(result.get('truncated'))}")
        for match in matches[:50]:
            full_path = os.path.join(root, match) if root else match
            lines.append(f"   match: {full_path}")
        if len(matches) > 50:
            lines.append(f"   ... {len(matches) - 50} more matches omitted ...")

    def _append_content_summary(self, lines, result):
        if result.get("path"):
            lines.append(f"   path: {result.get('path')}")
        if "page" in result:
            lines.append(f"   page: {result.get('page')} of {result.get('total_pages')}")
            lines.append(f"   total lines: {result.get('total_lines')}")
            if result.get("has_next_page"):
                lines.append(f"   next page: {result.get('next_page')}")
        content = _clip_text(result.get("content", ""))
        if content:
            lines.append(f"   content:\n{_indent(content, '      ')}")

    def _append_command_results_summary(self, lines, result):
        for result_idx, entry in enumerate(result.get("results", [])[:3], start=1):
            if not isinstance(entry, dict):
                continue
            if "returncode" in entry:
                lines.append(f"   result {result_idx} returncode: {entry.get('returncode')}")
            stdout = _clip_text(entry.get("stdout", ""))
            if stdout:
                lines.append(f"   result {result_idx} stdout:\n{_indent(stdout, '      ')}")
            stderr = _clip_text(entry.get("stderr", ""), limit=500)
            if stderr:
                lines.append(f"   result {result_idx} stderr:\n{_indent(stderr, '      ')}")

    @property
    def last_thinking_trace(self):
        return self._last_thinking_trace

    def run(
        self,
        user_message,
        selected_skills=None,
        skills_prompt="",
        initial_messages=None,
        initial_tool_history=None,
        system_prompt=None,
    ):
        return self._run_action_agent(
            user_message,
            selected_skills,
            skills_prompt=skills_prompt,
            initial_messages=initial_messages,
            initial_tool_history=initial_tool_history,
            system_prompt=system_prompt,
        )

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

        logger.debug("tool=%s state=%s args=%s", tool_name, state, json.dumps(args))

    def _debug_agent_response(self, content):
        if not self.debug:
            return

        logger.debug("agent_response=%r", content)

    def _assistant_message_for_history(self, message):
        """Return an assistant message suitable for the next model call.

        Provider responses may contain thinking/reasoning fields that are
        useful context but are not valid chat message fields for every
        provider. Fold the latest trace into content instead.
        """
        history_message = dict(message)
        for key in _THINKING_KEYS:
            history_message.pop(key, None)

        content = history_message.get("content") or ""
        trace = str(self._last_thinking_trace or "").strip()
        if trace:
            parts = []
            if str(content).strip():
                parts.append(f"{_CONTENT_MARKER}{content}")
            parts.append(f"{_TRACE_ONLY_MARKER}{trace}")
            history_message["content"] = "\n\n".join(parts)
        else:
            history_message["content"] = content
        return history_message

    def _remove_thinking_traces_from_history(self, messages):
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for key in _THINKING_KEYS:
                message.pop(key, None)
            message["content"] = self._strip_history_thinking_trace(message.get("content") or "")

    def _strip_history_thinking_trace(self, content):
        text = str(content or "")

        if text.startswith(_CONTENT_MARKER) and _TRACE_MARKER in text:
            return text[len(_CONTENT_MARKER): text.index(_TRACE_MARKER)]
        if text.startswith(_TRACE_ONLY_MARKER):
            return ""
        if _TRACE_MARKER in text:
            return text[: text.index(_TRACE_MARKER)]
        return text

    def _chat_with_optional_thinking(self, *args, **kwargs):
        self._last_thinking_trace = ""
        if not get_config().stream_thinking_traces:
            response = chat_with_model_api(*args, **kwargs)
            message = response.get("message", {}) if isinstance(response, dict) else {}
            self._last_thinking_trace = self._extract_message_thinking(message)
            return response

        thinking_parts = []

        def _on_thinking_delta(delta):
            if not delta:
                return
            text = str(delta)
            thinking_parts.append(text)
            self.ui.thinking_trace_delta(text)

        try:
            response = chat_with_model_api(*args, on_thinking_delta=_on_thinking_delta, **kwargs)
        except TypeError as exc:
            if "on_thinking_delta" not in str(exc):
                raise
            response = chat_with_model_api(*args, **kwargs)
        finally:
            if thinking_parts:
                self._last_thinking_trace = "".join(thinking_parts)
                self.ui.thinking_trace_complete(self._last_thinking_trace)

        if not self._last_thinking_trace and isinstance(response, dict):
            message = response.get("message", {})
            if isinstance(message, dict):
                self._last_thinking_trace = self._extract_message_thinking(message)
        return response

    def _extract_message_thinking(self, message):
        if not isinstance(message, dict):
            return ""
        for key in _DIRECT_THINKING_KEYS:
            value = message.get(key)
            if isinstance(value, str) and value:
                return value
        details = message.get("reasoning_details")
        if isinstance(details, list):
            parts = []
            for item in details:
                if not isinstance(item, dict):
                    continue
                for key in _REASONING_DETAIL_KEYS:
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        parts.append(value)
                        break
            return "".join(parts)
        return ""

    def _python_denied_result(self):
        return dict(_PYTHON_DENIED_RESULT)

    def _prepare_tool_call(self, tool_name, args, is_shell, is_exec):
        if is_shell:
            return self._prepare_shell_tool_call(tool_name, args)

        self.ui.tool_call(tool_name, args)
        if is_exec and not get_config().unrestricted_bash:
            code = args.get("code") or args.get("script") or args.get("source") or ""
            if not self.ui.approve_python_code(code):
                tool_result = self._python_denied_result()
                self.ui.handle_tool_output(result=tool_result)
                return tool_name, tool_result
        return tool_name, None

    def _prepare_shell_tool_call(self, tool_name, args):
        command = args.get("command", "")
        py_code = _detect_python_in_bash(command)
        if py_code is None or py_code == command:
            self.ui.tool_call(tool_name, args)
            return _clip_label(command, 120), None

        display_args = dict(args)
        display_args["command"] = command.replace(py_code, "<python>")
        self.ui.tool_call(tool_name, display_args)

        if get_config().unrestricted_bash:
            self.ui.show_python_code(py_code)
        elif not self.ui.approve_python_code(py_code):
            tool_result = self._python_denied_result()
            self.ui.handle_tool_output(result=tool_result)
            return _clip_label(display_args.get("command", tool_name), 120), tool_result

        return _clip_label(display_args.get("command", tool_name), 120), None

    def _call_tool_with_spinner(self, label, call_once, args):
        self.ui.update_spinner(label)
        try:
            return call_once(args)
        finally:
            self.ui.stop_spinner()

    def _long_tool_decision(self, tool_name, args, streamed_output):
        """Ask the model whether a tool that exceeded 30s may be stopped.

        Stopping is deliberately opt-in: malformed/unavailable model answers
        keep waiting, so the runtime never kills useful work on its own.
        """
        prompt = {
            "role": "user",
            "content": (
                "A tool call has run for more than 30 seconds. Decide whether "
                "to keep waiting or terminate it. Reply with JSON only in this "
                "schema: {\"action\": \"keep\"|\"terminate\"}. "
                f"Tool: {tool_name}; arguments: {self._compact_json(args)}; "
                f"partial output: {_clip_text(''.join(streamed_output), 4000)}"
            ),
        }
        try:
            response = self._chat_with_optional_thinking(
                self.model,
                [*self._active_messages, prompt],
                tools=None,
                binary=self.binary,
                response_format="json",
            )
            content = response.get("message", {}).get("content", "")
            decision = json.loads(content) if isinstance(content, str) else content
            return isinstance(decision, dict) and decision.get("action") == "terminate"
        except Exception as exc:
            logger.warning("long tool decision unavailable; keeping tool alive: %s", exc)
            return False

    def _call_tool_with_long_running_policy(self, label, call_once, args, tool_name, streamed_output):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._call_tool_with_spinner, label, call_once, args)
            try:
                return future.result(timeout=self.LONG_TOOL_NOTICE_SECONDS)
            except concurrent.futures.TimeoutError:
                if self._long_tool_decision(tool_name, args, streamed_output):
                    # Closing active MCP sockets unblocks the server request;
                    # its partial streamed output is already in the prompt.
                    self.mcp_client.close()
                return future.result()

    def _needs_privileged_approval(self, tool_result):
        return (
            isinstance(tool_result, dict)
            and tool_result.get("approval_required")
            and tool_result.get("approval_kind") == "privileged_whitelist"
        )

    def _retry_privileged_shell_tool(self, tool_name, args, label, tool_result, call_once):
        if not self._needs_privileged_approval(tool_result):
            return tool_result

        binary = tool_result.get("binary", "")
        if not self.ui.approve_privileged_binary(binary):
            return {
                "ok": False,
                "error": f"Privileged command not approved: {binary}",
            }

        retry_args = dict(args)
        retry_args["allow_privileged"] = True
        retry_command = tool_result.get("retry_command")
        if retry_command:
            retry_args["command"] = retry_command
        self.ui.tool_call(tool_name, retry_args)
        retried_result = self._call_tool_with_spinner(label, call_once, retry_args)
        if retry_command and isinstance(retried_result, dict):
            # Keep the full chain's result available to the model without
            # replaying already-completed commands or duplicating their UI.
            earlier_results = tool_result.get("results", [])
            retry_results = retried_result.get("results", [])
            retried_result = dict(retried_result)
            retried_result["command"] = args.get("command", retry_command)
            retried_result["results"] = [*earlier_results, *retry_results]
        return retried_result

    def _tool_status(self, tool_result):
        if (
            isinstance(tool_result, dict)
            and (tool_result.get("ok") is False or tool_result.get("error"))
        ):
            return "failed"
        return "success"

    def _execute_tool(self, tool_name, args):
        is_shell = tool_name == "bash"
        is_exec = tool_name in ("exec_python", "exec")
        shell_stream_seen = False
        streamed_output = []

        def _on_shell_stream(fd, line, end="\n"):
            nonlocal shell_stream_seen
            shell_stream_seen = True
            streamed_output.append(str(line) + end)
            self.ui.handle_tool_output(fd=fd, line=line, end=end)

        def _call_once(call_args):
            return _run_async(
                self.mcp_client.call_tool(
                    tool_name,
                    call_args,
                    stream_output=is_shell,
                    on_stream=_on_shell_stream if is_shell else None,
                )
            )

        label, tool_result = self._prepare_tool_call(tool_name, args, is_shell, is_exec)
        if tool_result is not None:
            self._debug_tool_result(tool_name, args, tool_result)
            return tool_result

        tool_result = self._call_tool_with_long_running_policy(
            label, _call_once, args, tool_name, streamed_output,
        )

        if not is_shell and isinstance(tool_result, dict):
            self.ui.handle_tool_output(result=tool_result)

        if is_shell:
            tool_result = self._retry_privileged_shell_tool(
                tool_name,
                args,
                label,
                tool_result,
                _call_once,
            )

        if is_shell:
            if shell_stream_seen:
                self.ui.handle_tool_output(result=tool_result)
            else:
                self.ui.handle_shell_result_output(tool_result)

        self._debug_tool_result(tool_name, args, tool_result)
        return tool_result

    def _reset_run_state(self):
        self.result = None
        self.messages = []
        self.execution_history = []

    def _chat_for_next_action(self, messages):
        self.ui.update_spinner("Thinking")
        try:
            # ``requests`` has no safe cross-thread cancellation primitive.
            # Keep the provider request in a daemon worker and poll the hard
            # cancellation signal so a second Escape immediately releases the
            # agent/UI.  Any late response is discarded and never enters
            # conversation history.
            outcome = queue.Queue(maxsize=1)

            def _request_model():
                try:
                    outcome.put((True, self._chat_with_optional_thinking(
                        self.model, messages, tools=self.tools, binary=self.binary,
                    )))
                except BaseException as exc:
                    outcome.put((False, exc))

            request_thread = threading.Thread(target=_request_model, daemon=True)
            request_thread.start()
            while True:
                if self._should_hard_cancel():
                    raise InterruptedError("LLM request hard-cancelled")
                try:
                    ok, value = outcome.get(timeout=0.1)
                except queue.Empty:
                    continue
                if ok:
                    return value
                raise value
        finally:
            self.ui.stop_spinner()

    def _tool_call_from_message(self, message):
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return False, None, None
        function = tool_calls[0].get("function", {})
        return True, function.get("name"), function.get("arguments", {})

    def _record_tool_result(
        self,
        messages,
        tool_history,
        message,
        tool_name,
        args,
        tool_result,
        iteration,
    ):
        status = self._tool_status(tool_result)
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

        self._remove_thinking_traces_from_history(messages)
        messages.append(self._assistant_message_for_history(message))
        tool_content = json.dumps(tool_result, ensure_ascii=False)
        messages.append({
            "role": "tool",
            "tool_name": tool_name,
            "content": _clip_text(tool_content, limit=15000),
        })
        self.messages = list(messages)
        self.execution_history = list(tool_history)

    def _interrupt_result(self, messages, tool_history):
        # Do not preserve an assistant tool-call without its matching result.
        # OpenAI-compatible APIs reject that sequence on a later followup.
        safe_messages = [dict(item) for item in messages]
        if safe_messages and safe_messages[-1].get("role") == "assistant" and safe_messages[-1].get("tool_calls"):
            safe_messages.pop()
        self.result = "Interrupted."
        self.messages = [
            *safe_messages,
            {"role": "assistant", "content": self.result},
        ]
        self.execution_history = list(tool_history)
        return self.result

    def _debug_final_answer(self, content):
        self._debug_agent_response(content)
        self._debug_orchestration("agent_final_answer", chars=len(content))

    def _finalize_direct_response(self, messages, tool_history, message):
        content = message.get("content") or ""
        self._debug_final_answer(content)
        if not content:
            content = (
                "Agent completed the task, but did not produce a final "
                "response.\n\n"
                f"Execution history:\n{self._build_execution_summary(tool_history)}"
            )
        self.result = content
        self.messages = [
            *messages,
            {"role": "assistant", "content": content},
        ]
        self.execution_history = list(tool_history)
        return self.result

    def _summarize_after_iteration_limit(self, messages, tool_history, system_prompt):
        final_messages = [
            {
                "role": "system",
                "content": (
                    f"{system_prompt} Do not call any tools in this final response; "
                    "use the previous tool results to answer the task."
                ),
            },
            *messages[1:],
            {"role": "user", "content": _FINAL_SUMMARY_PROMPT},
        ]
        self.ui.update_spinner("Summarizing Task")
        try:
            response = self._chat_with_optional_thinking(
                self.model,
                final_messages,
                tools=None,
                binary=self.binary,
            )
            content = response.get("message", {}).get("content") or ""
        finally:
            self.ui.stop_spinner()

        self._debug_final_answer(content)
        if not content:
            content = (
                "Agent reached the iteration limit.\n\n"
                f"Tool execution history:\n{self._build_execution_summary(tool_history)}"
            )
        self.result = content
        self.messages = [
            *final_messages,
            {"role": "assistant", "content": content},
        ]
        self.execution_history = list(tool_history)
        return self.result

    def _agent_system_prompt(self, skills_prompt=""):
        base = (
            "Use the tools available to you to perform the tasks "
            "assigned to you on the user's system. "
            "Use the bash tool to execute commands, and use snap "
            "or apt for app installations when relevant. "
            "When the user asks about a specific file, inspect that file "
            "and answer from it; do not inspect unrelated files unless the "
            "specific file cannot be located or imports are required to "
            "answer the question. Use exact file paths from tool results; "
            "do not invent or rename paths in the final answer. "
            "be careful with destructive operations"
            "Work only on the assigned action. When the action is complete, "
            "respond with a concise plain text summary of what was done"
        )
        if skills_prompt:
            base += "\n\n" + skills_prompt
        return base

    def _run_action_agent(
        self,
        user_message,
        selected_skills=None,
        skills_prompt="",
        initial_messages=None,
        initial_tool_history=None,
        system_prompt=None,
    ):
        try:
            self._reset_run_state()
            selected_skills = selected_skills or []
            self._debug_orchestration(
                "planner_skills_selected",
                skills=[s.name for s in selected_skills],
            )

            system_prompt = system_prompt or self._agent_system_prompt(skills_prompt)
            self.system_prompt = system_prompt
            if initial_messages:
                messages = [dict(message) for message in initial_messages]
                if not messages or messages[0].get("role") != "system":
                    messages.insert(0, {"role": "system", "content": system_prompt})
                else:
                    messages[0] = {"role": "system", "content": system_prompt}
                self._remove_thinking_traces_from_history(messages)
                messages.append({"role": "user", "content": user_message})
            else:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ]
            tool_history = [dict(item) for item in (initial_tool_history or [])]
            self._active_messages = messages
            stopped_for_final_response = False
            message = {}

            for iteration in range(1, self.MAX_AGENT_ITERATIONS + 1):
                self._debug_orchestration(
                    "agent_iteration",
                    iteration=iteration,
                    max_iterations=self.MAX_AGENT_ITERATIONS,
                )

                response = self._chat_for_next_action(messages)
                message = response.get("message", {})
                has_tool_call, tool_name, args = self._tool_call_from_message(message)

                if has_tool_call:
                    if self._should_interrupt():
                        return self._interrupt_result(messages, tool_history)

                    tool_result = self._execute_tool(tool_name, args)
                    self._record_tool_result(
                        messages,
                        tool_history,
                        message,
                        tool_name,
                        args,
                        tool_result,
                        iteration=iteration,
                    )
                    if self._should_interrupt():
                        return self._interrupt_result(messages, tool_history)

                    continue

                stopped_for_final_response = True
                self.messages = list(messages)
                self.execution_history = list(tool_history)
                if self._should_interrupt():
                    return self._interrupt_result(messages, tool_history)
                break

            if not stopped_for_final_response:
                self._debug_orchestration(
                    "agent_iteration_limit",
                    max_iterations=self.MAX_AGENT_ITERATIONS,
                    tool_calls=len(tool_history),
                )

            if stopped_for_final_response:
                return self._finalize_direct_response(messages, tool_history, message)

            return self._summarize_after_iteration_limit(
                messages,
                tool_history,
                system_prompt,
            )

        except Exception as e:
            if self._should_hard_cancel():
                return self._interrupt_result(
                    locals().get("messages", self.messages),
                    locals().get("tool_history", self.execution_history),
                )
            logger.error("Exception in _run_action_agent: %s", e)
            self.result = f"Error: {e}"
            # Preserve the last valid history for Runtime followups even if a
            # summary/provider call fails.
            self.messages = list(locals().get("messages", self.messages))
            self.execution_history = list(locals().get("tool_history", self.execution_history))
            return self.result
