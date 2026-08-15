"""Runtime lifecycle for cterm agent runs."""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import sys
import threading
import time

from cterm.core.agent_events import AgentEvents, active_agent_events_handler
from cterm.config import Config, get_config
from cterm.api.chat_api import chat_with_model_api
from cterm.core.mcp_client import FastMCPClient
from cterm.core.run_result import RunResult, make_run_result
from cterm.core.utils import get_socket_path
from cterm.core.skill_loader import SkillsLoader


logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 50
RETRY_DELAY_SECONDS = 0.2
SOCKET_CONNECT_TIMEOUT_SECONDS = 0.2


class Runtime:
    """Owns the tool server client and one or more ToolAgent runs."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        model: str | None = None,
        binary: str = "ollama",
        small_model: str | None = None,
    ):
        self.config = config or get_config()
        self.model = model
        self.small_model = small_model
        self.binary = binary
        self.mcp_client: FastMCPClient | None = None
        self.tools = []
        self.result: RunResult | None = None
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
        self.ensure_mcp_server()
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
        """Signal hard cancellation; the active agent owns transport cleanup."""
        self._interrupt_requested.set()
        self._hard_cancel_requested.set()

    def should_interrupt(self) -> bool:
        return self._interrupt_requested.is_set()

    def should_hard_cancel(self) -> bool:
        return self._hard_cancel_requested.is_set()

    @staticmethod
    def _active_ui() -> AgentEvents:
        ui = active_agent_events_handler.get()
        if ui is None:
            raise RuntimeError("Runtime requires an active AgentEvents context.")
        return ui
    
    def create_mcp_client(self, socket_path=None):
        """Create the transport client without performing tool discovery."""
        self.mcp_client = FastMCPClient(socket_path or get_socket_path())
        return self.mcp_client

    @staticmethod
    def _socket_is_ready(socket_path) -> bool:
        """Return whether the Unix socket exists and accepts connections."""
        if not socket_path.exists():
            return False

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(SOCKET_CONNECT_TIMEOUT_SECONDS)
        try:
            probe.connect(str(socket_path))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _wait_for_socket(self, socket_path) -> bool:
        for _ in range(RETRY_ATTEMPTS):
            if self._socket_is_ready(socket_path):
                self.create_mcp_client(socket_path)
                return True
            time.sleep(RETRY_DELAY_SECONDS)
        return False
    
    def ensure_mcp_server(self):
        """Wake the MCP server when needed and wait for its socket."""
        socket_path = get_socket_path()
        if self._socket_is_ready(socket_path):
            self.create_mcp_client(socket_path)
            return socket_path

        systemd_started = False
        try:
            subprocess.run(
                ["systemctl", "--user", "start", "cterm-mcp.service"],
                check=True,
                capture_output=True,
                text=True,
            )
            systemd_started = True

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

        if self._wait_for_socket(socket_path):
            return socket_path

        # A service can remain marked active after its server loop has lost
        # the socket (for example, after an interrupted streamed subprocess).
        # `systemctl start` is a no-op in that state, so restart it once before
        # giving up. Do not emit service-status diagnostics here.
        if systemd_started:
            try:
                subprocess.run(
                    ["systemctl", "--user", "restart", "cterm-mcp.service"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
            if self._wait_for_socket(socket_path):
                return socket_path

        raise TimeoutError("Tool server started but socket never became available.")


   

    def initialize_tools(self) -> None:


        def _try_connect():

            self.tools = asyncio.run(self.mcp_client.list_tools())

        try:
            _try_connect()

        except Exception:

            last_error = None
            for _ in range(RETRY_ATTEMPTS):
                time.sleep(RETRY_DELAY_SECONDS)
                try:
                    _try_connect()
                    break
                except Exception as exc:

                    last_error = exc
            else:
                raise RuntimeError("Failed to retrieve tool list.") from last_error

    def select_skills(self, user_message: str):
        ui = self._active_ui()
        loader = SkillsLoader()
        skills = loader.load()
        if skills:
            ui.message(f"Available Skills: {', '.join(s.name for s in skills)}")
        ui.status("Selecting Skills")
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
            ui.clear_status()

        names = [skill.name for skill in selected]
        if names:
            ui.message(f"\033[32m✓\033[0m {' '.join(names)}")
        logger.debug("selected_skills=%s", names)

        return selected, loader.render_for_system_prompt(selected)

    def _build_tools(self, raw_tools):
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
        # ToolAgent removes any incomplete tool call before saving interrupted
        # history, so no synthetic interruption message needs special-casing.
        return [dict(message) for message in self.messages]

    def run_followup(self, user_message: str, *, clarification: bool = False) -> RunResult:
        from cterm.core.agent import ToolAgent
        self.ensure_mcp_server()
        text = self.followup_text(user_message)
        if not text:
            return self.result or make_run_result(False, "")
        if not self.messages:
            return self.run(text)

        self._interrupt_requested.clear()
        self._hard_cancel_requested.clear()
        self._active_ui()

        # Followups retain the original prompt and its selected skill content.
        # Re-selecting here only produces duplicate "Available Skills" UI
        # output and cannot affect the already-persisted prompt.
        if self._system_prompt is None:
            selected_skills, skills_prompt = self.select_skills(text)
        else:
            selected_skills, skills_prompt = [], ""

        tools = self._build_tools(self.tools)
        agent = ToolAgent(
            self.model,
            self.binary,
            self.small_model,
            mcp_client=self.mcp_client,
            tools=tools,
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

    def run(self, user_message: str) -> RunResult:
        from cterm.core.agent import ToolAgent

        self._interrupt_requested.clear()
        self._hard_cancel_requested.clear()
        self._active_ui()
        self.ensure_mcp_server()
        self.initialize_tools()
        selected_skills, skills_prompt = self.select_skills(user_message)

        tools = self._build_tools(self.tools)

        agent = ToolAgent(
            self.model,
            self.binary,
            self.small_model,
            mcp_client=self.mcp_client,
            tools=tools,
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
