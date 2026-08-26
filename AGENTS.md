# AGENTS.md

Guidance for coding agents working in this repository.

## Project Shape

Openterm is a Python 3.14 CLI, Textual TUI, and local MCP-like tool harness for completing desktop OS tasks with Ollama, OpenRouter, or an OpenAI-compatible server. It can also build relocatable Linux and macOS application runtimes.

The current architecture is a single action agent managed by a runtime object:

- `openterm/__main__.py`: CLI entry point. Handles `openterm --version`, `openterm -i` / `openterm -init`, `openterm --binary`, no-argument TUI mode, and one-shot `openterm "message"` mode. It initializes the application home, resolves provider-specific model settings, creates session transcripts under `~/.openterm/transcripts/` and diagnostic logs under `~/.openterm/logs/`, runs the Textual app by default, and uses the basic terminal UI for direct message mode.
- `openterm/config/app_home.py`: resolves the per-user application home at `~/.openterm` and binds it to the current context. Config, model history, prompt history, skills, transcripts, logs, and the privileged whitelist use this home.
- `openterm/core/runtime.py`: owns MCP client lifecycle, tool discovery, skill selection, interrupt state, and run state copied from the agent (`messages`, `execution_history`, `result`, `last_thinking_trace`). It starts `openterm-mcp.service` with `systemctl --user start` when the socket is missing, falls back to launching `openterm.mcp.server` directly when user systemd is unavailable, retries tool discovery, and exposes `run()` plus `run_followup()`. Followups are user inputs starting with `//`: after a completed run they inject `followup: ...`; while a run is active the TUI queues them as an interrupt/elaboration and injects `clarification: ...`.
- `openterm/core/agent.py`: `ToolAgent`, the single tool-calling loop. The iteration limit defaults to 50 and is controlled by `Config.max_iteration_limit`. There is no planner/worker/verifier flow. It builds chat history, calls `chat_with_model_api`, executes MCP tools, records tool results, supports interrupts and Python approvals, optionally summarizes at the iteration limit, strips/embeds provider thinking traces for valid followup context, and accepts `initial_messages` / `initial_tool_history` / `system_prompt` for followup runs.
- `openterm/core/agent_events.py`: common UI protocol consumed by `ToolAgent`. It includes status, message, tool-call, tool-output, privileged approval, Python approval/display, and thinking-trace hooks.
- `openterm/core/mcp_client.py`: Unix-domain-socket JSON-RPC-ish client. Supports `tools/list`, `tools/call`, and streaming bash output via newline-delimited `tools/progress` notifications (`params.token` correlation) before the final result frame. Handles out-of-band `approval_request` frames by consulting an `on_approval_request` callback and answering with `approval/respond`.
- `openterm/core/utils.py`: shared helpers for socket paths, async bridging, output labels, thinking trace markers, final summary prompt, and Python-in-bash detection.
- `openterm/config/config.py`: client/UI `Config` class reading/writing `~/.openterm/config/config.json` using nested `providers` and `attributes` objects. Model history is stored at `~/.openterm/data/models.json` as most-recent-first `{model, provider}` entries; the active model/provider are NOT config attributes. Each `Config` instance holds session state (`selected_model`/`api_provider`) seeded from the newest models.json entry, and `choose_model(model, provider)` updates the session pair plus the newest entry, so two running instances isolate their choices while new instances resume the latest used. Per-request LLM threads and Textual workers start with an empty contextvar context; `get_config()` therefore falls back to a single shared per-process `Config` singleton instead of constructing a fresh `Config`, which would re-seed the session pair from disk mid-run. Runtime attribute keys include `small_model`, `unrestricted_mode`, `thinking_traces`, `max_iteration_limit`, `dark_mode`, `websearch_provider`, `exa_api_key`, and `parallel_api_key`.
- `openterm/config/utils.py`: configuration helper functions used by init flows for provider setup and model selection.
- `openterm/api/`: provider HTTP layer. `chat_api.py` dispatches based on `Config.api_provider`; `ollama.py`, `openrouter.py`, and `openai_compatible.py` implement provider calls; `utils.py` normalizes OpenAI-compatible messages/responses and streaming reasoning/tool-call chunks.
- `openterm/mcp/server.py`: persistent MCP-like tool server over a Unix domain socket at `/tmp/openterm_mcp_{username}.sock` using `pwd.getpwuid(os.getuid()).pw_name` with `$USER` fallback. It has a 20-minute inactivity shutdown and emits newline-delimited JSON frames for streaming output.
- `openterm/mcp/tools.py`: exposed MCP tool implementations. The registered tools are marked with `__mcp_tool__` and attached to the `mcp` object. Current exposed tool names include `finder`, `read_file`, `write_file`, `bash`, `exec` (backed by `exec_python`), `system_info`, and `websearch`. This is the source of truth; there is no `openterm/mcp/tools_mcp.py`.
- `openterm/mcp/utils/bash_utils.py`: restricted/unrestricted bash parsing and execution helpers, including command parsing, pipelines, `&&`, glob/env expansion, streaming, PTY streaming for apt/snap-style commands, output finalization, and privileged command routing.
- `openterm/mcp/config.py`: MCP configuration singleton and privileged-command whitelist state. It reads the nested openterm config, exposes MCP attributes and web-search credentials, and owns whitelist reads/updates. The whitelist path is `~/.openterm/privileged_whitelist`, overridable with `OPENTERM_PRIVILEGED_WHITELIST`.
- `openterm/mcp/vars.py`: MCP constants including `OUTPUT_LINE_LIMIT = 200`, file page size, wrapper path, parser restrictions, web-search endpoints, and server timeout.
- `openterm/mcp/utils/web_utils.py`: Exa/Parallel web search helpers. Provider is selected by `websearch_provider`, defaulting to Exa. API keys come from config or `EXA_API_KEY` / `PARALLEL_API_KEY`.
- `openterm/ui/basic/basic.py`: plain terminal UI used by one-shot chat mode. It owns terminal spinner output, tool rendering, thinking traces, approval prompts, and result formatting.
- `openterm/ui/tui/app/app_tui.py`: default Textual interface. It renders a transcript, pending live tool output, expandable/collapsible thinking traces, approval prompts, prompt history, followups, interrupt handling, and prompt placeholder hints. It reuses one `Runtime` across TUI runs so followup context survives, but refreshes `Runtime.ui` for each worker run because each run has a new run id and cancellation event.
- `openterm/ui/tui/app/agent_events_handler.py`: adapts `AgentEvents` to Textual rendering and writes visible progress through `TranscriptWriter`.
- `openterm/ui/tui/app/transcript_writer.py`: creates and owns transcript files under `~/.openterm/transcripts/`; diagnostic logs remain separate under `~/.openterm/logs/`.
- `openterm/ui/tui/config/config_tui.py`: Textual configuration wizard used by `openterm -i` / `openterm -init`.
- `openterm/ui/tui/app/history.py`: prompt history helper for the TUI.
- `openterm/ui/basic/basic_config.py`: plain terminal configuration helpers used as the fallback flow when Textual is unavailable in `openterm --init`.
- `packaging/openterm-mcp.service`: systemd user service template (`Type=simple`, `Restart=on-failure`) installed under `/usr/lib/systemd/user/` by the Debian package.
- `packaging/postinstall.sh`: installs the privileged wrapper, sudoers file, and initial user-owned whitelist.
- `packaging/postrm.sh`: removes the privileged wrapper, sudoers file, systemd integration, and user Openterm state on package removal.
- `openterm/core/skill_loader.py`: markdown skill loading from `~/.openterm/skills`, strict skill selection using the configured small model, and system-prompt rendering.
- `openterm/skills/`: deprecated in-repository skill files; do not treat them as active source of truth or add new skills there.
- `scripts/build/`: downloads and installs pinned standalone CPython 3.14 runtimes for Linux and macOS.
- `scripts/release/`: assembles relocatable application runtimes in `dist/linux` and `dist/macos`, including `openterm` and `openterm-mcp` launchers.
- `scripts/packaging/linux/create_deb.py`: builds a Debian package from `dist/linux`, adding the CLI symlink, MCP service, post-install, and post-removal scripts.
- `launcher/openterm_launcher.c`: native launcher source used when assembling release runtimes.
- `tests/`: stdlib `unittest` coverage for app-home/config behavior, orchestration/runtime, skills, bash parsing, finder, exec, MCP client, tool approval, websearch, CLI/TUI entry behavior, provider streaming normalization, resilience, packaging layout, thinking-trace UI/agent behavior, transcript handling, and TUI rendering.

Removed or stale paths from older revisions:

- There is no `openterm/llm.py`; use `openterm/core/agent.py`.
- There is no `openterm/config.py`; use `openterm/config/config.py` via `from openterm.config import Config`.
- There is no `openterm/openterm_server.py`; use `openterm/mcp/server.py`.
- There is no `openterm/mcp/tools_mcp.py`; use `openterm/mcp/tools.py`.
- There is no `openterm/skills_loader/`; use `openterm/core/skill_loader.py`.
- There is no `openterm/mcp/utils/mcp_utils.py` or `openterm/mcp/utils/privilege.py`; use the singleton in `openterm/mcp/config.py`.
- There is no `openterm/packaging/` source tree or `sudoopenterm.sh`; use `packaging/postinstall.sh`, `packaging/postrm.sh`, and `scripts/packaging/linux/create_deb.py`.
- There is no current `openterm/task_tool.py`, `create_new_task`, `_verify_history`, or `_build_worker_handoff` path.
- There is no `openterm/llm_utils/`; current API and MCP helpers live under `openterm/api/` and `openterm/core/`.

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
python3 -m unittest tests.test_app_home tests.test_config_schema
python3 -m unittest tests.test_resilience tests.test_tui_errors
python3 -m unittest tests.test_openrouter_models tests.test_basic_ui
python3 -m unittest tests.test_packaging_layout
```

Useful manual CLI checks:

```bash
python3 -m openterm --version
python3 -m openterm -i
python3 -m openterm "your prompt"
python3 -m openterm
```

Build and package checks (these download a pinned standalone Python runtime and require the platform toolchain):

```bash
python3 scripts/build/linux/build_runtime.py
python3 scripts/release/linux/release.py [--skip-build]
python3 scripts/packaging/linux/create_deb.py
python3 scripts/build/macos/build_runtime.py
python3 scripts/release/macos/release.py [--skip-build]
```

The Linux and macOS build scripts use separate `build/<platform>/` trees. Release artifacts are written to `dist/<platform>/`; Debian packages are written to `release/linux/`.

The repository intentionally contains only deterministic unit tests; live prompt and desktop-mutation smoke scripts are not part of the test suite.

## Architecture Rules

- Preserve the current single-agent `ToolAgent` loop unless intentionally reworking orchestration. There is no separate planner, worker, delegation, or verifier layer.
- `Runtime` is the owner of MCP client lifecycle, tool discovery, skill selection, interrupt state, and cross-run context for followups. Keep Textual-specific event handling in `openterm/ui/tui/app/app_tui.py` and `openterm/ui/tui/app/agent_events_handler.py`; keep model/tool-loop behavior in `openterm/core/agent.py`.
- Followups are part of the observable TUI behavior. A prompt beginning with `//` after a completed run should preserve previous `Runtime.messages` and `Runtime.execution_history` and append a `followup: ...` user message. A `//` prompt while a run is busy should queue a pending followup, interrupt the active run via `Runtime.interrupt()`, and continue with `clarification: ...`.
- The TUI reuses one `Runtime` for context, but each worker run must refresh `Runtime.ui` to the current `TUIAgentEventsHandler` instance. Otherwise later runs can be treated as stale/cancelled by the old run id.
- Diagnostic output is written to per-run files under `~/.openterm/logs/` and is never rendered by either UI. Visible transcript output is written separately by `TranscriptWriter` under `~/.openterm/transcripts/`. When changing orchestration logging, update tests intentionally. Current orchestration event names are `agent_iteration`, `agent_tool_result`, `agent_iteration_limit`, and `agent_final_answer`; skill selection is reported through the UI protocol (`status`/`message`), not a named orchestration event, and there is no planner.
- Keep MCP server responses newline-delimited JSON. Streaming tool calls send zero or more `tools/progress` notifications (JSON-RPC frames with a `method` and no `id`, correlated by `params.token`) followed by one final `"result"` frame. A held privileged bash call may also send an `approval_request` frame, resolved out-of-band by a separate `approval/respond` request; the approval exchange is never returned to the model as a tool result.
- Requests without an `id` are JSON-RPC notifications: the server processes them but never writes any response frame, not even an error reply, and privileged approvals inside a notification tool call auto-deny. Keep this behavior in `handle_request`/`_run_tool_call` when changing the protocol.
- Thinking traces are controlled by the client/UI `Config.stream_thinking_traces` property, backed by the `thinking_traces` attribute; keep config, `ToolAgent._chat_with_optional_thinking`, provider stream parsing, and both UI implementations in sync when changing this behavior.
- Provider support is abstracted in `chat_with_model_api` in `openterm/api/chat_api.py`, dispatching to Ollama, OpenRouter, or OpenAI-compatible based on `Config.api_provider`. Keep `__main__.py` provider-specific model selection and `Config` keys in sync when changing provider setup.
- For OpenRouter and OpenAI-compatible providers, preserve `normalize_messages_for_openai`, `normalize_openai_response`, and `normalize_openai_stream_response` semantics when changing tool calls, tool-result history, or streaming thinking traces. Streamed tool-call argument chunks must be reassembled before the response reaches `ToolAgent`.
- Socket path logic exists in multiple places. `openterm/mcp/server.py` uses UID/pwd lookup; `openterm/core/utils.py` is used by the client/runtime. Keep them compatible when changing.
- Application-home resolution is shared through `openterm/config/app_home.py`; preserve `~/.openterm` and the context-bound behavior when changing config, history, skills, transcript, logging, or MCP state paths.
- Tool result history is fed back into model context and into followups. Keep tool-result messages provider-compatible and avoid adding non-chat fields to messages sent to providers.

## UI Notes

- `AgentEvents` is the boundary between `ToolAgent` and presentation. Add new agent-facing UI behavior to `openterm/core/agent_events.py` and implement it in both `openterm/ui/basic/basic.py` and `openterm/ui/tui/app/agent_events_handler.py`.
- The Textual transcript has distinct live-update surfaces: pending tool stream output (`append_stream`) and live thinking traces (`append_thinking_delta`). Do not reuse the pending tool stream slot for thinking traces; it can overwrite or be overwritten by bash stdout/stderr.
- Completed TUI thinking traces should remain a single collapsed line starting with `▶ THINKING:` and expand in place to `▼ THINKING:` when clicked. Avoid reintroducing a separate "Thinking trace" header line.
- Live bash stream text in the TUI should be sanitized before rendering. Preserve `_sanitize_stream_text` behavior so ANSI/OSC/control sequences from commands such as `snap` or `apt` do not create colored blocks or corrupt transcript layout.
- Final answer rendering in the TUI uses Markdown; live tool output uses `Text` renderables. Keep command-output summaries capped through the existing TUI output helpers rather than dumping long captured output after a streamed command finishes.
- Followup prompts are rendered into the transcript as `> query`. Fresh prompts remain pinned in `#query_bar` and clear the transcript.
- Prompt placeholder text is the followup affordance: initial `Type your Request...`, running `Type // to clarify this request...`, and completed `Type a new request, or // to follow up...`. Keep these as input placeholders rather than footer text.
- Approval prompts temporarily repurpose the same input widget with placeholder `y or n`; restore the correct prompt placeholder after approval handling.

## Bash Tool Safety

`openterm.mcp.tools.bash` must not become a raw shell escape hatch in restricted mode.

- Restricted mode executes parsed argv with `subprocess`, not `shell=True`.
- Restricted mode supports only the command features implemented in `openterm/mcp/utils/bash_utils.py`: unquoted `&&`, unquoted `|`, double-quoted args, environment variable and `~` expansion, glob expansion, and stderr suppression as `2>/dev/null` or `2> /dev/null`.
- Other redirection and shell metacharacters are intentionally rejected through `FORBIDDEN_CHARS`.
- The `bash` tool exposes no `timeout` parameter and applies no wall-clock timeout server-side. Commands run until they complete or are cancelled through the long-tool UI prompt.
- A leading `sudo` enters privileged routing. The service never returns an approval-required result to the model. It holds the bash tool call, sends an out-of-band `approval_request` frame to the client, and waits for a separate `approval/respond` request. The client prompts the user through the UI, and on approval the service updates the whitelist before routing through `/usr/lib/openterm/openterm-privileged`. Restricted mode asks for approval on every `sudo` call, even when the binary is already whitelisted; unrestricted mode only asks when the binary is missing from the whitelist. Denial surfaces as a normal failed tool result. The model has no `allow_privileged` tool argument and never sees the approval exchange.
- Leading `sudo` options are handled before binary detection (`_partition_sudo_options`). Boolean options that cannot affect wrapper execution (`-n`, `--non-interactive`, `-k`, `-K`, `-E`, `--preserve-env`, `--`) are dropped; target-changing options (`-u`/`--user`, `-g`/`--group`) and unknown options fail with a clear error instead of falling back to raw password-prompting sudo. Both restricted and unrestricted parsing share this behavior.
- Unrestricted mode runs commands through `/bin/bash -c` with normal shell syntax when `unrestricted_mode` is true. Before execution, unrestricted mode scans common `sudo <binary>` forms and goes through the same out-of-band approval request when the resolved binary is not whitelisted.
- Restricted and unrestricted paths both scan `_BLOCKED_BINARIES`; it is currently empty. Add entries intentionally with tests.
- apt/snap-style privileged commands use PTY streaming where needed so commands that detect a TTY behave correctly.
- Bash final result output is capped at the current `openterm/mcp/vars.py:OUTPUT_LINE_LIMIT` (currently 200) lines, preserving the first and last halves with a truncation marker. Streaming frames remain uncapped; the final result is truncated inline and no output file is created by the current implementation.
- Any change to parsing, return-code handling, streaming, timeout handling, output finalization, unrestricted mode, PTY behavior, or privileged routing needs focused coverage in `tests/test_bash_parsing.py`.
- The test suite should not require root, package installation, external services, or real desktop mutation.

## Service and Privilege Notes

- `Runtime.ensure_mcp_server()` starts `openterm-mcp.service` with `systemctl --user start openterm-mcp.service` when the Unix socket is missing. If user systemd is unavailable, it starts `python -m openterm.mcp.server` directly as a detached fallback.
- The server has a 20-minute inactivity timeout and removes its socket on shutdown.
- `packaging/postinstall.sh` installs the privileged wrapper, sudoers file, and initial user-owned whitelist at `~/.openterm/privileged_whitelist` for the invoking user.
- `packaging/postrm.sh` removes those package integrations and the user's Openterm state.
- `packaging/openterm-mcp.service` is the systemd user service template; the assembled package installs it under `/usr/lib/systemd/user/` and launches `/usr/lib/openterm/openterm-mcp`.
- Privileged whitelist state lives in the singleton in `openterm/mcp/config.py`.
- The root-side wrapper canonicalizes the requested binary with `readlink -f` for the whitelist check but execs the path as invoked, so symlink-dispatched multiplexers (kmod applets such as `modprobe` -> `kmod`) keep their argv[0] and behave correctly. Keep the check/exec split intact when editing `packaging/postinstall.sh`.
- Privileged approval is transport-level, not model-level. The server injects a hidden `_approve_privileged` callback into the bash tool (underscore-prefixed parameters are excluded from `tools/list` schemas), emits `approval_request` frames on the held tool call, and resolves them through the `approval/respond` method. The client (`FastMCPClient.on_approval_request`, wired by `ToolAgent` to the UI) owns user interaction; the MCP service owns whitelist mutation and command execution after approval. The root-side wrapper checks the same whitelist again.
- Avoid changing privileged command behavior unless the MCP check, wrapper check, tests, and docs are all updated together.

## Skills

Skills are user-owned markdown files under `~/.openterm/skills/`. The deprecated repository directory `openterm/skills/` is not an active source of skills and should be ignored for implementation work.

Each skill must include:

```markdown
## When to use

...

## Skill

...
```

The loader ignores `README.md`, selects skills by filename stem, and only loads files where both `## When to use` and `## Skill` have content. `render_for_system_prompt` returns selected `## Skill` content. Keep `## When to use` strict and specific; the selector prompt asks the model to prefer a single skill.

There is no authoritative in-repository skill inventory. The loader ignores `README.md`, selects skills by filename stem, and only loads files where both sections have content.

## Coding Style

- This repo uses plain Python modules and stdlib `unittest`; `pyproject.toml` defines the package metadata, Python `==3.14.0` requirement, pinned `requests`, `rich`, and `textual` dependencies, and the `openterm` console entry point.
- Follow the existing simple style: type hints where useful, small helper functions, direct dictionaries for protocol payloads, and explicit error dicts for tool failures.
- Keep imports lightweight. `requests` is already used for provider and web access; avoid introducing new dependencies unless the project is also given packaging metadata.
- Do not rewrite unrelated code or normalize old comments while making focused fixes.
- Prefer deterministic tests with mocked model/tool responses over tests that depend on a live Ollama server, OpenRouter, OpenAI-compatible server, systemd service, or desktop state.
- Use current import paths in tests. Patch `openterm.core.agent.chat_with_model_api`, `openterm.core.runtime.FastMCPClient`, and `openterm.mcp.tools.*` rather than stale `openterm.llm`, `openterm.runtime`, or `openterm.mcp.tools_mcp` paths.
- Build/release scripts are intentionally platform-specific and may download external CPython archives; unit tests should mock their subprocess and filesystem boundaries rather than invoke a real build.

## Files to Avoid Treating as Source of Truth

- Captured run logs such as `test_results.txt`, `new-logs.txt`, and ad hoc log files are diagnostic history only.
- Live integration scripts are intentionally not kept in the repository because they can invoke real model APIs, systemd, or desktop mutations.
