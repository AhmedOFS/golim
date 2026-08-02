# AGENTS.md

Guidance for coding agents working in this repository.

## Project Shape

Cterm is a Python CLI, Textual TUI, and local MCP-like tool harness for completing desktop OS tasks with an Ollama-backed model, OpenRouter, or an OpenAI-compatible server.

The current architecture is a single action agent managed by a runtime object:

- `cterm/__main__.py`: CLI entry point. Handles `cterm --version`, `cterm -i` / `cterm -init`, `cterm --binary`, no-argument TUI mode, and one-shot `cterm "message"` mode. It resolves provider-specific model settings, creates session transcripts under `~/cterm/transcripts/` and diagnostic logs under `~/cterm/logs/`, runs the Textual app by default, and falls back to the basic terminal UI for direct message mode.
- `cterm/core/runtime.py`: owns MCP client lifecycle, tool discovery, skill selection, interrupt state, and run state copied from the agent (`messages`, `execution_history`, `result`, `last_thinking_trace`). It starts `cterm-mcp.service` with `systemctl --user start` when the socket is missing, retries tool discovery, and exposes `run()` plus `run_followup()`. Followups are user inputs starting with `//`: after a completed run they inject `followup: ...`; while a run is active the TUI queues them as an interrupt/elaboration and injects `clarification: ...`.
- `cterm/core/agent.py`: `ToolAgent`, the single tool-calling loop. `MAX_AGENT_ITERATIONS = 50`. There is no planner/worker/verifier flow. It builds chat history, calls `chat_with_model_api`, executes MCP tools, records tool results, supports interrupts, optionally summarizes at the iteration limit, strips/embeds provider thinking traces for valid followup context, and accepts `initial_messages` / `initial_tool_history` for followup runs.
- `cterm/core/agent_events.py`: common UI protocol consumed by `ToolAgent`. It includes status, message, tool-call, tool-output, privileged approval, Python approval/display, and thinking-trace hooks.
- `cterm/core/mcp_client.py`: Unix-domain-socket JSON-RPC-ish client. Supports `tools/list`, `tools/call`, and streaming bash output via newline-delimited `"stream"` frames before the final result frame.
- `cterm/core/utils.py`: shared helpers for socket paths, async bridging, output labels, thinking trace markers, final summary prompt, and Python-in-bash detection.
- `cterm/config/config.py`: client/UI `Config` class reading/writing `~/.config/cterm/config.json` (or `$XDG_CONFIG_HOME/cterm/config.json`) using nested `providers` and `attributes` objects. Runtime keys include `current_model`, `small_model`, `bash_unrestricted`, `thinking_traces`, `max_iteration_limit`, `api_provider`, `websearch_provider`, `exa_api_key`, and `parallel_api_key`.
- `cterm/config/utils.py`: configuration helper functions used by init flows for provider setup and model selection.
- `cterm/api/`: provider HTTP layer. `chat_api.py` dispatches based on `Config.api_provider`; `ollama.py`, `openrouter.py`, and `openai_compatible.py` implement provider calls; `utils.py` normalizes OpenAI-compatible messages/responses and streaming reasoning/tool-call chunks.
- `cterm/mcp/server.py`: persistent MCP-like tool server over a Unix domain socket at `/tmp/cterm_mcp_{username}.sock` using `pwd.getpwuid(os.getuid()).pw_name` with `$USER` fallback. It has a 20-minute inactivity shutdown and emits newline-delimited JSON frames for streaming output.
- `cterm/mcp/tools.py`: exposed MCP tool implementations. The registered tools are marked with `__mcp_tool__` and attached to the `mcp` object. Current exposed tool names include `finder`, `read_file`, `write_file`, `bash`, `exec` (backed by `exec_python`), `system_info`, and `websearch`. This is the source of truth; there is no `cterm/mcp/tools_mcp.py`.
- `cterm/mcp/utils/bash_utils.py`: restricted/unrestricted bash parsing and execution helpers, including command parsing, pipelines, `&&`, glob/env expansion, streaming, PTY streaming for apt/snap-style commands, output finalization, and privileged command routing.
- `cterm/mcp/config.py`: MCP configuration singleton and privileged-command whitelist state. It reads the nested cterm config, exposes MCP attributes and web-search credentials, and owns whitelist reads/updates. The whitelist path is `~/.config/cterm/privileged_whitelist`, overridable with `CTERM_PRIVILEGED_WHITELIST`.
- `cterm/mcp/vars.py`: MCP constants including `OUTPUT_LINE_LIMIT = 200`, file page size, wrapper path, parser restrictions, web-search endpoints, and server timeout.
- `cterm/mcp/utils/web_utils.py`: Exa/Parallel web search helpers. Provider is selected by `websearch_provider`, defaulting to Exa. API keys come from config or `EXA_API_KEY` / `PARALLEL_API_KEY`.
- `cterm/ui/basic/basic.py`: plain terminal UI used by one-shot chat mode. It owns terminal spinner output, tool rendering, thinking traces, approval prompts, and result formatting.
- `cterm/ui/tui/app/app_tui.py`: default Textual interface. It renders a transcript, pending live tool output, expandable/collapsible thinking traces, approval prompts, prompt history, followups, interrupt handling, and prompt placeholder hints. It reuses one `Runtime` across TUI runs so followup context survives, but refreshes `Runtime.ui` for each worker run because each run has a new run id and cancellation event.
- `cterm/ui/tui/app/agent_events_handler.py`: adapts `AgentEvents` to Textual rendering and writes visible progress through `TranscriptWriter`.
- `cterm/ui/tui/app/transcript_writer.py`: creates and owns transcript files under `~/cterm/transcripts/`; diagnostic logs remain separate under `~/cterm/logs/`.
- `cterm/ui/tui/config/config_tui.py`: Textual configuration wizard used by `cterm -i` / `cterm -init`.
- `cterm/ui/tui/app/history.py`: prompt history helper for the TUI.
- `cterm/ui/basic/basic_config.py`: older/basic configuration helpers kept for non-Textual-style flows.
- `cterm/packaging/cterm-mcp.service`: systemd user service unit (`Type=simple`, `Restart=on-failure`) installed under `/usr/lib/systemd/user/`.
- `cterm/packaging/sudocterm.sh`: installs/removes the privileged wrapper, sudoers file, and initial user-owned whitelist.
- `cterm/core/skill_loader.py`: markdown skill loading, strict skill selection, and prompt rendering.
- `cterm/skills/`: built-in skill markdown files. Each skill must have non-empty `## When to use` and `## Skill` sections or the loader ignores it. Current skill files have empty `## Skill` sections, so they are not active until content is added.
- `tests/`: stdlib `unittest` coverage for orchestration/runtime, skills, bash parsing, finder, exec, MCP client, tool approval, websearch, CLI/TUI entry behavior, provider thinking-stream normalization, thinking-trace UI/agent behavior, and TUI tool output rendering.

Removed or stale paths from older revisions:

- There is no `cterm/llm.py`; use `cterm/core/agent.py`.
- There is no `cterm/config.py`; use `cterm/config/config.py` via `from cterm.config import Config`.
- There is no `cterm/cterm_server.py`; use `cterm/mcp/server.py`.
- There is no `cterm/mcp/tools_mcp.py`; use `cterm/mcp/tools.py`.
- There is no `cterm/skills_loader/`; use `cterm/core/skill_loader.py`.
- There is no `cterm/mcp/utils/mcp_utils.py` or `cterm/mcp/utils/privilege.py`; use the singleton in `cterm/mcp/config.py`.
- There is no current `cterm/task_tool.py`, `create_new_task`, `_verify_history`, or `_build_worker_handoff` path.
- There is no `cterm/llm_utils/`; current API and MCP helpers live under `cterm/api/` and `cterm/core/`.

## Development Commands

Run the fast local test suite with:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

Run focused tests while changing risky areas:

```bash
python3 -m unittest tests.test_bash_parsing
python3 -m unittest tests.test_llm_orchestration
python3 -m unittest tests.test_runtime
python3 -m unittest tests.test_skills_loader
python3 -m unittest tests.test_finder_tool
python3 -m unittest tests.test_exec_tool
python3 -m unittest tests.test_mcp_client
python3 -m unittest tests.test_tool_approval
python3 -m unittest tests.test_websearch_tool
python3 -m unittest tests.test_main_tui
python3 -m unittest tests.test_tui_tool_outputs
python3 -m unittest tests.test_chat_api_streaming
python3 -m unittest tests.test_thinking_traces
python3 -m unittest tests.test_transcript_writer
python3 -m unittest tests.test_mcp_config_schema
```

Useful manual CLI checks:

```bash
python3 -m cterm --version
python3 -m cterm -i
python3 -m cterm "your prompt"
python3 -m cterm
```

The repository intentionally contains only deterministic unit tests; live prompt and desktop-mutation smoke scripts are not part of the test suite.

## Architecture Rules

- Preserve the current single-agent `ToolAgent` loop unless intentionally reworking orchestration. There is no separate planner, worker, delegation, or verifier layer.
- `Runtime` is the owner of MCP client lifecycle, tool discovery, skill selection, interrupt state, and cross-run context for followups. Keep Textual-specific event handling in `cterm/ui/tui/app/app_tui.py` and `cterm/ui/tui/app/agent_events_handler.py`; keep model/tool-loop behavior in `cterm/core/agent.py`.
- Followups are part of the observable TUI behavior. A prompt beginning with `//` after a completed run should preserve previous `Runtime.messages` and `Runtime.execution_history` and append a `followup: ...` user message. A `//` prompt while a run is busy should queue a pending followup, interrupt the active run via `Runtime.interrupt()`, and continue with `clarification: ...`.
- The TUI reuses one `Runtime` for context, but each worker run must refresh `Runtime.ui` to the current `TUIAgentEventsHandler` instance. Otherwise later runs can be treated as stale/cancelled by the old run id.
- Diagnostic output is written to per-run files under `~/cterm/logs/` and is never rendered by either UI. Visible transcript output is written separately by `TranscriptWriter` under `~/cterm/transcripts/`. When changing orchestration logging, update tests intentionally. Existing event names include legacy `planner_skills_selected` even though there is no planner.
- Keep MCP server responses newline-delimited JSON. Streaming tool calls send zero or more `"stream"` frames followed by one final `"result"` frame.
- Thinking traces are controlled by the client/UI `Config.stream_thinking_traces` property, backed by the `thinking_traces` attribute; keep config, `ToolAgent._chat_with_optional_thinking`, provider stream parsing, and both UI implementations in sync when changing this behavior.
- Provider support is abstracted in `chat_with_model_api` in `cterm/api/chat_api.py`, dispatching to Ollama, OpenRouter, or OpenAI-compatible based on `Config.api_provider`. Keep `__main__.py` provider-specific model selection and `Config` keys in sync when changing provider setup.
- For OpenRouter and OpenAI-compatible providers, preserve `normalize_messages_for_openai`, `normalize_openai_response`, and `normalize_openai_stream_response` semantics when changing tool calls, tool-result history, or streaming thinking traces. Streamed tool-call argument chunks must be reassembled before the response reaches `ToolAgent`.
- Socket path logic exists in multiple places. `cterm/mcp/server.py` uses UID/pwd lookup; `cterm/core/utils.py` is used by the client/runtime. Keep them compatible when changing.
- Tool result history is fed back into model context and into followups. Keep tool-result messages provider-compatible and avoid adding non-chat fields to messages sent to providers.

## UI Notes

- `AgentEvents` is the boundary between `ToolAgent` and presentation. Add new agent-facing UI behavior to `cterm/core/agent_events.py` and implement it in both `cterm/ui/basic/basic.py` and `cterm/ui/tui/app/agent_events_handler.py`.
- The Textual transcript has distinct live-update surfaces: pending tool stream output (`append_stream`) and live thinking traces (`append_thinking_delta`). Do not reuse the pending tool stream slot for thinking traces; it can overwrite or be overwritten by bash stdout/stderr.
- Completed TUI thinking traces should remain a single collapsed line starting with `▶ THINKING:` and expand in place to `▼ THINKING:` when clicked. Avoid reintroducing a separate "Thinking trace" header line.
- Live bash stream text in the TUI should be sanitized before rendering. Preserve `_sanitize_stream_text` behavior so ANSI/OSC/control sequences from commands such as `snap` or `apt` do not create colored blocks or corrupt transcript layout.
- Final answer rendering in the TUI uses Markdown; live tool output uses `Text` renderables. Keep command-output summaries capped through the existing TUI output helpers rather than dumping long captured output after a streamed command finishes.
- Followup prompts are rendered into the transcript as `> query`. Fresh prompts remain pinned in `#query_bar` and clear the transcript.
- Prompt placeholder text is the followup affordance: initial `Type your Request...`, running `Type // to clarify this request...`, and completed `Type a new request, or // to follow up...`. Keep these as input placeholders rather than footer text.
- Approval prompts temporarily repurpose the same input widget with placeholder `y or n`; restore the correct prompt placeholder after approval handling.

## Bash Tool Safety

`cterm.mcp.tools.bash` must not become a raw shell escape hatch in restricted mode.

- Restricted mode executes parsed argv with `subprocess`, not `shell=True`.
- Restricted mode supports only the command features implemented in `cterm/mcp/utils/bash_utils.py`: unquoted `&&`, unquoted `|`, double-quoted args, environment variable and `~` expansion, glob expansion, and stderr suppression as `2>/dev/null` or `2> /dev/null`.
- Other redirection and shell metacharacters are intentionally rejected through `FORBIDDEN_CHARS`.
- `bash` normalizes `timeout` at the tool boundary; numeric strings from model tool arguments are accepted, invalid values return a clear error, and `None` is preserved.
- A leading `sudo` enters privileged routing. The MCP service resolves the binary and returns an approval-required result if it is missing from the whitelist. The client UI asks the user, retries with `allow_privileged=True`, and the service updates the whitelist before routing through `/usr/lib/cterm/cterm-privileged`.
- Unrestricted mode runs commands through `/bin/bash -c` with normal shell syntax when `bash_unrestricted` is true. Before execution, unrestricted mode scans common `sudo <binary>` forms and returns `approval_required` when the resolved binary is not whitelisted.
- Restricted and unrestricted paths both scan `_BLOCKED_BINARIES`; it is currently empty. Add entries intentionally with tests.
- apt/snap-style privileged commands use PTY streaming where needed so commands that detect a TTY behave correctly.
- Bash final result output is capped at the current `cterm/mcp/vars.py:OUTPUT_LINE_LIMIT` (currently 200) lines, preserving the first and last halves with a truncation marker. Streaming frames remain uncapped; the final result is truncated inline and no output file is created by the current implementation.
- Any change to parsing, return-code handling, streaming, timeout handling, output finalization, unrestricted mode, PTY behavior, or privileged routing needs focused coverage in `tests/test_bash_parsing.py`.
- The test suite should not require root, package installation, external services, or real desktop mutation.

## Service and Privilege Notes

- `Runtime.initialize_tools()` starts `cterm-mcp.service` with `systemctl --user start cterm-mcp.service` when the Unix socket is missing.
- The server has a 20-minute inactivity timeout and removes its socket on shutdown.
- `cterm/packaging/sudocterm.sh` installs/removes the privileged wrapper, sudoers file, and initial user-owned whitelist at the cterm config path.
- `cterm/packaging/cterm-mcp.service` is the systemd user service unit.
- Privileged whitelist state lives in the singleton in `cterm/mcp/config.py`.
- The client owns user interaction for adding whitelist entries. The MCP service owns whitelist mutation and command execution after approval. The root-side wrapper checks the same whitelist again.
- Avoid changing privileged command behavior unless the MCP check, wrapper check, tests, and docs are all updated together.

## Skills

Skills are plain markdown files under `cterm/skills/`.

Each skill must include:

```markdown
## When to use

...

## Skill

...
```

The loader ignores `README.md`, selects skills by filename stem, and only loads files where both `## When to use` and `## Skill` have content. `render_for_system_prompt` returns selected `## Skill` content. Keep `## When to use` strict and specific; the selector prompt asks the model to prefer a single skill.

Current skill files:

- `App_Install.md` - install software on Ubuntu from official instructions
- `Filesystem_Operations.md` - file search and manipulation
- `Storage_Analysis.md` - disk usage and cleanup candidate analysis

Note: the current skill files have empty `## Skill` sections, so they are ignored until content is added.

## Coding Style

- This repo uses plain Python modules and stdlib `unittest`; no project-level formatter or package manager config is present.
- Follow the existing simple style: type hints where useful, small helper functions, direct dictionaries for protocol payloads, and explicit error dicts for tool failures.
- Keep imports lightweight. `requests` is already used for provider and web access; avoid introducing new dependencies unless the project is also given packaging metadata.
- Do not rewrite unrelated code or normalize old comments while making focused fixes.
- Prefer deterministic tests with mocked model/tool responses over tests that depend on a live Ollama server, OpenRouter, OpenAI-compatible server, systemd service, or desktop state.
- Use current import paths in tests. Patch `cterm.core.agent.chat_with_model_api`, `cterm.core.runtime.FastMCPClient`, and `cterm.mcp.tools.*` rather than stale `cterm.llm`, `cterm.runtime`, or `cterm.mcp.tools_mcp` paths.

## Files to Avoid Treating as Source of Truth

- Captured run logs such as `test_results.txt`, `new-logs.txt`, and ad hoc log files are diagnostic history only.
- Live integration scripts are intentionally not kept in the repository because they can invoke real model APIs, systemd, or desktop mutations.
