
"""LLM interaction module for openterm using Ollama's native tool calling"""
import logging
import json
import os
import shlex
import queue
import threading
import time

from openterm.core.agent_events import AgentEvents
from openterm.core.permissions import Permissions
from openterm.core.run_result import RunResult, make_run_result
from openterm.config import Config, get_config
from openterm.api.chat_api import chat_with_model_api
from openterm.logger import log_diagnostic_section
from openterm.core.utils import _CONTENT_MARKER, _DIRECT_THINKING_KEYS, _FINAL_SUMMARY_PROMPT, _REASONING_DETAIL_KEYS, _THINKING_KEYS, _TRACE_MARKER, _TRACE_ONLY_MARKER, _clip_label, _clip_text, _detect_python_in_bash, _indent, _run_async

logger = logging.getLogger(__name__)

class ToolAgent:
    MAX_AGENT_ITERATIONS = 50
    LONG_TOOL_NOTICE_SECONDS = 30
    TOOL_POLL_INTERVAL_SECONDS = 0.1

    def __init__(
        self,
        model,
        binary="ollama",
        small_model=None,
        ui: AgentEvents | None = None,
        mcp_client=None,
        tools=None,
        should_interrupt=None,
        should_hard_cancel=None,
        permissions: Permissions | None = None,
    ):
        self.model = model
        self.small_model = small_model
        self.binary = binary
        self.mcp_client = mcp_client
        self.tools = list(tools or [])
        if ui is None:
            raise ValueError("ToolAgent requires an AgentEvents ui handler.")
        self.ui = ui
        self.permissions = permissions if permissions is not None else Permissions(ui)
        self._last_thinking_trace = ""
        self.messages = []
        self.execution_history = []
        self.result: RunResult | None = None
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

    def _log_orchestration(self, event, **fields):
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
        for match in matches[:200]:
            full_path = os.path.join(root, match) if root else match
            lines.append(f"   match: {full_path}")
        if len(matches) > 200:
            lines.append(f"   ... {len(matches) - 200} more matches omitted ...")

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
    ) -> RunResult:
        return self._run_action_agent(
            user_message,
            selected_skills,
            skills_prompt=skills_prompt,
            initial_messages=initial_messages,
            initial_tool_history=initial_tool_history,
            system_prompt=system_prompt,
        )

    def _log_tool_result(self, tool_name, args, result):
        ok = result.get("ok") if isinstance(result, dict) else None
        if ok is True:
            state = "success"
        elif ok is False:
            state = "failed"
        else:
            state = "unknown"

        logger.debug("tool=%s state=%s args=%s", tool_name, state, json.dumps(args))

    def _log_agent_response(self, content):
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
            self.ui.thinking_delta(text)

        try:
            response = chat_with_model_api(*args, on_thinking_delta=_on_thinking_delta, **kwargs)
        except TypeError as exc:
            if "on_thinking_delta" not in str(exc):
                raise
            response = chat_with_model_api(*args, **kwargs)
        finally:
            if thinking_parts:
                self._last_thinking_trace = "".join(thinking_parts)
                log_diagnostic_section("thinking_trace", self._last_thinking_trace)
                self.ui.thinking_complete(self._last_thinking_trace)

        if not self._last_thinking_trace and isinstance(response, dict):
            message = response.get("message", {})
            if isinstance(message, dict):
                self._last_thinking_trace = self._extract_message_thinking(message)
        if self._last_thinking_trace and not thinking_parts:
            log_diagnostic_section("thinking_trace", self._last_thinking_trace)
            self.ui.thinking_complete(self._last_thinking_trace)
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

    def _tool_cancelled_result(self):
        """Consistent cancellation result for a tool stopped by the model's
        long-tool terminate decision.

        Matches the shape the MCP server produces when it cooperatively
        cancels a bash call on client disconnect, so the model always sees
        the same ``"Tool execution cancelled"`` result instead of a transport
        error from closing the socket.
        """
        return {"ok": False, "error": "Tool execution cancelled"}

    def _prepare_tool_call(self, tool_name, args, is_shell=False):
        display_args = self._shell_display_args(args) if is_shell else args
        self.ui.tool_call(tool_name, display_args)
        if is_shell:
            label = _clip_label(display_args.get("command", tool_name), 120)
        else:
            label = tool_name
        return label, self.permissions.is_approved(tool_name, args)

    @staticmethod
    def _shell_display_args(args):
        """Mask embedded Python before the command is shown to the user."""
        command = str(args.get("command", ""))
        py_code = _detect_python_in_bash(command)
        if py_code is None or py_code == command:
            return args
        masked = dict(args)
        masked["command"] = command.replace(py_code, "<python>")
        return masked

    def _call_tool_with_spinner(self, label, call_once, args):
        self.ui.status(label)
        try:
            return call_once(args)
        finally:
            self.ui.clear_status()

    def _long_tool_decision(self, tool_name, args, streamed_output, elapsed):
        """Ask the model whether a long-running tool may be stopped.

        `elapsed` is the total seconds the tool call has been running. Stopping
        is deliberately opt-in: malformed/unavailable model answers keep
        waiting, so the runtime never kills useful work on its own.
        """
        prompt = {
            "role": "user",
            "content": (
                f"A tool call has run for {elapsed:.0f} seconds. Decide whether "
                "to keep waiting or terminate it. Reply with JSON only in this "
                "schema: {\"action\": \"keep\"|\"terminate\"}. "
                f"Tool: {tool_name}; arguments: {self._compact_json(args)}; "
                f"partial output: {_clip_text(''.join(streamed_output), 4000)}"
            ),
        }
        try:
            response = self._chat_with_hard_cancel(
                self.model,
                [*self._active_messages, prompt],
                tools=None,
                binary=self.binary,
                response_format="json",
            )
            content = response.get("message", {}).get("content", "")
            decision = json.loads(content) if isinstance(content, str) else content
            return isinstance(decision, dict) and decision.get("action") == "terminate"
        except InterruptedError:
            if self._should_hard_cancel():
                raise
            logger.warning("long tool decision interrupted; keeping tool alive")
            return False
        except Exception as exc:
            logger.warning("long tool decision unavailable; keeping tool alive: %s", exc)
            return False

    def _call_tool_with_long_running_policy(self, label, call_once, args, tool_name, streamed_output):
        # Tool calls can block in socket I/O, so keep the request in a daemon
        # worker and poll from the agent thread.  This mirrors model-call
        # cancellation: the agent thread remains responsive without trying to
        # kill a Python thread, while closing the MCP transport gives the
        # server a chance to clean up any subprocess-backed tool.
        outcome = queue.Queue(maxsize=1)

        def _run_tool():
            try:
                outcome.put((True, self._call_tool_with_spinner(label, call_once, args)))
            except BaseException as exc:
                outcome.put((False, exc))

        tool_thread = threading.Thread(target=_run_tool, daemon=True)
        tool_thread.start()
        started = time.monotonic()
        deadline = started + self.LONG_TOOL_NOTICE_SECONDS
        terminated = False

        while True:
            if self._should_hard_cancel():
                if self.mcp_client is not None:
                    self.mcp_client.close()
                raise InterruptedError("Tool call hard-cancelled")

            remaining = deadline - time.monotonic()
            poll_timeout = min(self.TOOL_POLL_INTERVAL_SECONDS, remaining) if remaining > 0 else 0
            try:
                ok, value = outcome.get(timeout=poll_timeout)
            except queue.Empty:
                if remaining > 0:
                    continue
                try:
                    should_terminate = self._long_tool_decision(
                        tool_name, args, streamed_output,
                        time.monotonic() - started,
                    )
                except InterruptedError:
                    # The decision request can be in flight while the user
                    # hard-cancels the run. Close the active request socket so
                    # the MCP server cancels the child tool as well.
                    if self.mcp_client is not None:
                        self.mcp_client.close()
                    raise
                if should_terminate:
                    # The model chose to stop the tool. Closing active MCP
                    # sockets unblocks the server request and cooperatively
                    # cancels the subprocess-backed tool; the model sees the
                    # same cancellation result as a client side disconnect.
                    terminated = True
                    if self.mcp_client is not None:
                        self.mcp_client.close()
                    deadline = float("inf")
                else:
                    self.ui.status(f"Continuing {label}")
                    # A keep-waiting decision applies only to this interval;
                    # ask again if the same tool is still running later.
                    deadline = time.monotonic() + self.LONG_TOOL_NOTICE_SECONDS
                continue

            if terminated:
                # The model chose to stop the tool, so the socket close above
                # is the expected cause of any outcome (including transport
                # errors from the torn-down MCP connection). Surface the
                # consistent cancellation result regardless of how the worker
                # unwound, so the model ties the closure back to its decision.
                return self._tool_cancelled_result()
            if ok:
                return value
            raise value

    def _wire_privileged_approval(self):
        """Route server approval requests to the user through Permissions.

        The approval exchange happens inside the MCP tool call, out-of-band
        from the model: the server holds the bash call, the client prompts
        the user via ``Permissions.approve_binary``, and the model only ever
        receives the final tool result. Rewired per call because the MCP
        client can be recreated between runs.
        """
        client = self.mcp_client
        if client is not None and hasattr(client, "on_approval_request"):
            client.on_approval_request = self.permissions.approve_binary

    def _tool_status(self, tool_result):
        if (
            isinstance(tool_result, dict)
            and (tool_result.get("ok") is False or tool_result.get("error"))
        ):
            return "failed"
        return "success"

    def _execute_tool(self, tool_name, args):
        is_shell = tool_name == "bash"
        shell_stream_seen = False
        streamed_output = []
        log_diagnostic_section(f"tool_call {tool_name}", args or {})

        def _on_shell_stream(fd, line, end="\n"):
            nonlocal shell_stream_seen
            shell_stream_seen = True
            streamed_output.append(str(line) + end)
            log_diagnostic_section(
                f"tool_output {tool_name} {fd}",
                f"{line}{end}",
            )
            self.ui.tool_output(fd=fd, line=line, end=end)

        def _call_once(call_args):
            return _run_async(
                self.mcp_client.call_tool(
                    tool_name,
                    call_args,
                    stream_output=is_shell,
                    on_stream=_on_shell_stream if is_shell else None,
                )
            )

        self._wire_privileged_approval()
        label, tool_result = self._prepare_tool_call(tool_name, args, is_shell)
        if tool_result is not None:
            log_diagnostic_section(f"tool_result {tool_name}", tool_result)
            self._log_tool_result(tool_name, args, tool_result)
            return tool_result

        tool_result = self._call_tool_with_long_running_policy(
            label, _call_once, args, tool_name, streamed_output,
        )

        if not is_shell and isinstance(tool_result, dict):
            self.ui.tool_output(result=tool_result)

        if is_shell:
            if shell_stream_seen:
                self.ui.tool_output(result=tool_result)
            else:
                self.ui.shell_output(tool_result)

        self._log_tool_result(tool_name, args, tool_result)
        log_diagnostic_section(f"tool_result {tool_name}", tool_result)
        return tool_result

    def _reset_run_state(self):
        self.result = None
        self.messages = []
        self.execution_history = []

    def _chat_with_hard_cancel(self, *args, **kwargs):
        """Run a model request while allowing the agent to release promptly."""
        if self._should_hard_cancel():
            raise InterruptedError("LLM request hard-cancelled")

        # ``requests`` has no safe cross-thread cancellation primitive. Keep
        # the provider request in a daemon worker and poll the hard
        # cancellation signal. Any late response is discarded and never
        # enters conversation history.
        outcome = queue.Queue(maxsize=1)

        def _request_model():
            try:
                outcome.put((True, self._chat_with_optional_thinking(*args, **kwargs)))
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

    def _chat_for_next_action(self, messages):
        self.ui.status("Thinking")
        try:
            return self._chat_with_hard_cancel(
                self.model,
                messages,
                tools=self.tools,
                binary=self.binary,
            )
        finally:
            self.ui.clear_status()

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
        self._log_orchestration(
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
        self.result = make_run_result(False, "Interrupted.")
        # Keep the provider history free of a synthetic assistant message.
        # The structured result carries the interruption state for the UI;
        # the safe history is already ready for a followup.
        self.messages = safe_messages
        self.execution_history = list(tool_history)
        return self.result

    def _log_final_answer(self, content):
        log_diagnostic_section("summary", str(content or ""))
        self._log_agent_response(content)
        self._log_orchestration("agent_final_answer", chars=len(content))

    def _finalize_direct_response(self, messages, tool_history, message):
        content = message.get("content") or ""
        self._log_final_answer(content)
        if not content:
            content = (
                "Agent completed the task, but did not produce a final "
                "response.\n\n"
                f"Execution history:\n{self._build_execution_summary(tool_history)}"
            )
        self.result = make_run_result(True, content)
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
        self.ui.status("Summarizing Task")
        try:
            response = self._chat_with_optional_thinking(
                self.model,
                final_messages,
                tools=None,
                binary=self.binary,
            )
            content = response.get("message", {}).get("content") or ""
        finally:
            self.ui.clear_status()

        self._log_final_answer(content)
        if not content:
            content = (
                "Agent reached the iteration limit.\n\n"
                f"Tool execution history:\n{self._build_execution_summary(tool_history)}"
            )
        self.result = make_run_result(True, content)
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
    ) -> RunResult:
        try:
            self._reset_run_state()
            selected_skills = selected_skills or []
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
                self._log_orchestration(
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
                self._log_orchestration(
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
            self.result = make_run_result(False, f"Error: {e}")
            # Preserve the last valid history for Runtime followups even if a
            # summary/provider call fails.
            self.messages = list(locals().get("messages", self.messages))
            self.execution_history = list(locals().get("tool_history", self.execution_history))
            return self.result
