"""Runtime lifecycle for cterm agent runs."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time

from cterm.core.agent_ui import AgentUI, active_agent_ui
from cterm.config import Config
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
        self.config = config or Config()
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

    def should_interrupt(self) -> bool:
        return self._interrupt_requested.is_set()

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
            except subprocess.CalledProcessError as exc:
                raise RuntimeError("Failed to activate tool server.") from exc

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

    def run(self, user_message: str) -> str:
        from cterm.core.agent import ToolAgent

        self._interrupt_requested.clear()
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
        )
        self.result = agent.run(
            user_message,
            selected_skills=selected_skills,
        )
        self.last_thinking_trace = agent.last_thinking_trace
        self.messages = list(agent.messages)
        self.execution_history = list(agent.execution_history)
        return self.result
