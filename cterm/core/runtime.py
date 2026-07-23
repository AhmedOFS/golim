"""Runtime lifecycle for cterm agent runs."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
import time

from cterm.core.agent_ui import AgentUI, active_agent_ui
from cterm.config import Config, get_config
from cterm.api.chat_api import chat_with_model_api
from cterm.core.mcp_client import FastMCPClient
from cterm.core.utils import get_socket_path
from cterm.skills_loader import SkillsLoader


logger = logging.getLogger(__name__)

TOOL_DISCOVERY_ATTEMPTS = 10
TOOL_DISCOVERY_RETRY_DELAY_SECONDS = 0.2


class Runtime:
    """Owns the tool server client and one or more ToolAgent runs."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        model: str | None = None,
        binary: str = "ollama",
        small_model: str | None = None,
        debug: bool = False,
        ui: AgentUI | None = None,
    ):
        self.config = config or get_config()
        self.model = model
        self.small_model = small_model
        self.binary = binary
        self.debug = debug
        self.ui = ui or active_agent_ui.get()
        self.mcp_client: FastMCPClient | None = None
        self.tools = []
        self.result: str | None = None
        self.last_thinking_trace = ""
        self.messages = []
        self.execution_history = []
        self._interrupt_requested = threading.Event()
        self._hard_cancel_requested = threading.Event()
        self._system_prompt: str | None = None

    @staticmethod
    def is_followup_message(user_message: str) -> bool:
        return str(user_message).lstrip().startswith("//")

    @staticmethod
    def followup_text(user_message: str) -> str:
        text = str(user_message).lstrip()
        if text.startswith("//"):
            return text[2:].strip()
        return str(user_message).strip()

    def __enter__(self):
        self.initialize_tools()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        if self.mcp_client is not None:
            if hasattr(self.mcp_client, "close"):
                self.mcp_client.close()
            self.mcp_client = None

    def terminate(self) -> None:
        self.interrupt()
        self.close()

    def interrupt(self) -> None:
        self._interrupt_requested.set()

    def hard_cancel(self) -> None:
        """Immediately unblock a pending MCP request; used by a second Esc."""
        self._interrupt_requested.set()
        self._hard_cancel_requested.set()
        if self.mcp_client is not None:
            self.mcp_client.close()

    def should_interrupt(self) -> bool:
        return self._interrupt_requested.is_set()

    def should_hard_cancel(self) -> bool:
        return self._hard_cancel_requested.is_set()

    def initialize_tools(self) -> None:
        socket_path = get_socket_path()

        def _try_connect():
            if self.mcp_client is None:
                self.mcp_client = FastMCPClient(socket_path)
            self.tools = asyncio.run(self.mcp_client.list_tools())

        def _start_service():
            try:
                subprocess.run(
                    ["systemctl", "--user", "start", "cterm-mcp.service"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                # A user systemd bus is not guaranteed (notably in SSH,
                # containers, and graphical-less sessions).  Start the same
                # server directly as a local fallback.
                logger.warning("systemd user service unavailable; starting MCP server directly: %s", exc)
                subprocess.Popen(
                    [sys.executable, "-m", "cterm.mcp.server"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

        try:
            _try_connect()
        except (ConnectionRefusedError, FileNotFoundError):
            self.close()
            _start_service()
            for _ in range(TOOL_DISCOVERY_ATTEMPTS):
                time.sleep(TOOL_DISCOVERY_RETRY_DELAY_SECONDS)
                try:
                    self.mcp_client = FastMCPClient(socket_path)
                    _try_connect()
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    self.close()
                    continue
            else:
                raise TimeoutError("Tool server started but socket never became available.")
        except Exception:
            self.close()
            last_error = None
            for _ in range(TOOL_DISCOVERY_ATTEMPTS):
                time.sleep(TOOL_DISCOVERY_RETRY_DELAY_SECONDS)
                try:
                    self.mcp_client = FastMCPClient(socket_path)
                    _try_connect()
                    break
                except Exception as exc:
                    self.close()
                    last_error = exc
            else:
                raise RuntimeError("Tool server did not return a valid tool list.") from last_error

    def select_skills(self, user_message: str, ui: AgentUI):
        loader = SkillsLoader(debug=self.debug)
        skills = loader.load()
        if skills:
            ui.message(f"Available Skills: {', '.join(s.name for s in skills)}")
        ui.update_spinner("Selecting Skills")
        try:
            selected = loader.select(
                user_message,
                self.small_model or self.model,
                chat_with_model_api,
                binary=self.binary,
            )
        except Exception as exc:
            logger.debug("skills_selection_failed error=%s", exc)
            selected = []
        finally:
            ui.stop_spinner()

        names = [skill.name for skill in selected]
        if names:
            ui.message(f"\033[32m✓\033[0m {' '.join(names)}")
        if self.debug:
            logger.debug("selected_skills=%s", names)

        return selected, loader.render_for_system_prompt(selected)

    def _build_ollama_tools(self, raw_tools):
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": getattr(t, 'description', ''),
                    "parameters": getattr(t, 'parameters', {}),
                }
            }
            for t in raw_tools
        ]

    def _context_messages_for_followup(self) -> list[dict]:
        messages = [dict(message) for message in self.messages]
        if messages and messages[-1].get("role") == "assistant" and messages[-1].get("content") == "Interrupted.":
            messages.pop()
        return messages

    def run_followup(self, user_message: str, *, clarification: bool = False) -> str:
        from cterm.core.agent import ToolAgent

        text = self.followup_text(user_message)
        if not text:
            return self.result or ""
        if not self.messages:
            return self.run(text)

        self._interrupt_requested.clear()
        self._hard_cancel_requested.clear()
        active_ui = self.ui
        if active_ui is None:
            raise RuntimeError("Runtime requires an active AgentUI context at initialization.")
        self.initialize_tools()
        # Followups retain the original prompt and its selected skill content.
        # Re-selecting here only produces duplicate "Available Skills" UI
        # output and cannot affect the already-persisted prompt.
        if self._system_prompt is None:
            selected_skills, skills_prompt = self.select_skills(text, active_ui)
        else:
            selected_skills, skills_prompt = [], ""

        ollama_tools = self._build_ollama_tools(self.tools)
        agent = ToolAgent(
            self.model,
            self.binary,
            self.small_model,
            debug=self.debug,
            ui=active_ui,
            mcp_client=self.mcp_client,
            tools=ollama_tools,
            should_interrupt=self.should_interrupt,
            should_hard_cancel=self.should_hard_cancel,
        )
        prefix = "clarification" if clarification else "followup"
        self.result = agent.run(
            f"{prefix}: {text}",
            selected_skills=selected_skills,
            skills_prompt=skills_prompt,
            initial_messages=self._context_messages_for_followup(),
            initial_tool_history=self.execution_history,
            system_prompt=self._system_prompt,
        )
        self.last_thinking_trace = agent.last_thinking_trace
        self._system_prompt = agent.system_prompt
        self.messages = list(agent.messages)
        self.execution_history = list(agent.execution_history)
        return self.result

    def run(self, user_message: str) -> str:
        from cterm.core.agent import ToolAgent

        self._interrupt_requested.clear()
        self._hard_cancel_requested.clear()
        active_ui = self.ui
        if active_ui is None:
            raise RuntimeError("Runtime requires an active AgentUI context at initialization.")
        self.initialize_tools()
        selected_skills, skills_prompt = self.select_skills(user_message, active_ui)

        ollama_tools = self._build_ollama_tools(self.tools)

        agent = ToolAgent(
            self.model,
            self.binary,
            self.small_model,
            debug=self.debug,
            ui=active_ui,
            mcp_client=self.mcp_client,
            tools=ollama_tools,
            should_interrupt=self.should_interrupt,
            should_hard_cancel=self.should_hard_cancel,
        )
        self.result = agent.run(
            user_message,
            selected_skills=selected_skills,
            skills_prompt=skills_prompt,
        )
        self._system_prompt = agent.system_prompt
        self.last_thinking_trace = agent.last_thinking_trace
        self.messages = list(agent.messages)
        self.execution_history = list(agent.execution_history)
        return self.result
