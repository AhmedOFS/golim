# AGENTS.md

Guidance for coding agents working in this repository.

## Project Shape

Cterm is a Python CLI and local tool harness for completing desktop OS tasks with an Ollama-backed model, OpenRouter, or a llama.cpp-compatible server.

The main pieces are:

- `cterm/__main__.py`: CLI entry point. Handles `cterm --version`, `cterm -i` (provider/model/bash-unrestricted/thinking-trace init), `cterm --binary`, `cterm --debug`, `cterm -d`, and `cterm "message"`. Manages socket-based server lifecycle (`ensure_server_running` starts `cterm-mcp.service` via `systemctl --user`). Provider setup supports Ollama, OpenRouter, and llama.cpp. Ollama init records `OLLAMA_HOST` or `http://localhost:11434` as `ollama_server_url`, tries to start `ollama.service` when the server is unreachable, and can loop back to provider selection. Chat mode chooses provider-specific model/small-model fields for OpenRouter and llama.cpp. `cterm -i` also prompts for `stream_thinking_traces`, which controls whether model reasoning/thinking deltas are streamed into the UI. Socket path uses `os.getlogin()`.
- `cterm/config.py`: `Config` class reading/writing `~/.config/cterm/config.json` (or `$XDG_CONFIG_HOME/cterm/config.json`). Keys: `selected_model`, `small_model`, `bash_unrestricted`, `stream_thinking_traces`, `api_provider` (ollama/openrouter/llamacpp), `ollama_server_url`, `openrouter_api_key`, `openrouter_model`, `openrouter_small_model`, `llamacpp_server_url`, `llamacpp_model`, `llamacpp_small_model`, `websearch_provider`, `exa_api_key`, `parallel_api_key`.
- `cterm/llm.py`: single `ToolAgent` tool loop for Ollama/OpenRouter/llama.cpp tool-call integration, skill selection, debug logging, optional thinking-trace streaming, and MCP client lifecycle. `MAX_AGENT_ITERATIONS = 50`; there is no separate planner/worker/verifier flow in the current implementation. Selected skills currently affect tool filtering: when the rendered skill prompt contains `## Filesystem_Operations`, the available MCP tools are restricted to `{finder, bash, exec, read_file, write_file}` before `create_new_task` is appended. `create_new_task` comes from `cterm/task_tool.py`; its executor currently raises `NotImplementedError`, so treat it as an unfinished delegation surface. The sandboxed Python implementation is `exec_python`, but it is exposed to agents as `mcp.exec` for compatibility. Privileged tool approval (`approval_required=True` / `approval_kind="privileged_whitelist"`) prompts the user via `privilege.prompt_to_add_privileged_binary` and retries with `allow_privileged=True`. When `Config.stream_thinking_traces` is true, `_chat_with_optional_thinking` passes an `on_thinking_delta` callback to `chat_with_model_api`, streams each delta to the active `AgentUI`, then sends a single completion event with the accumulated trace. `_debug_orchestration` currently logs events including `planner_skills_selected` (legacy event name for skill selection), `agent_tools_ready`, `agent_iteration`, `agent_tool_result`, `agent_final_answer`, and `agent_iteration_limit`; skill-selection exceptions are logged as `skills_selection_failed`.
- `cterm/cterm_server.py`: persistent MCP-like tool server over a Unix domain socket in `/tmp/cterm_mcp_{username}.sock` (uses `pwd.getpwuid(os.getuid()).pw_name` with a `$USER` fallback), with 20-minute inactivity shutdown and newline-delimited JSON frames for streaming output. Supports `tools/list` and `tools/call` via `hasattr(tool_func, '__mcp_tool__')`.
- `cterm/mcp/tools_mcp.py`: exposed tool implementations. The `bash` tool is the highest-risk surface and must stay constrained. Also provides `exec_python` (sandboxed Python, exposed as `mcp.exec`), `finder` (file search), paginated `read_file`, `write_file`, `system_info`, and `websearch` (Exa/Parallel web search). `finder` matches basenames only, excludes hidden files/directories and common build/cache directories by default, searches only user-content roots when `path="/"` unless `system_inclusive=True`, and sorts all matches before applying the `max_results` lexical cap. For long `find` and `du` bash output only, `bash` caps visible output at 50 lines and saves longer full result payloads under `~/cterm/data/` (falling back to `/tmp/cterm/data/` if needed); other commands keep their full inline output. `bash` accepts `timeout` (default 120 seconds, `None` for no timeout in code paths that pass it through). `read_file(path, page=1)` returns one 50-line page with pagination metadata. `write_file` creates parent directories and supports `mode="overwrite"` or `mode="append"`. `websearch` supports Exa (`https://mcp.exa.ai/mcp`) and Parallel (`https://search.parallel.ai/mcp`) MCP backends; provider is selected via `websearch_provider` config key (`"exa"` or `"parallel"`), defaulting to Exa. API keys are optional and read from config or env vars (`EXA_API_KEY`, `PARALLEL_API_KEY`). `_BLOCKED_BINARIES` restricts sudo-target or shell binaries in favor of cterm tool equivalents; it is currently empty except for commented examples. `_is_bash_unrestricted()` reads `bash_unrestricted` from config; unrestricted path uses `/bin/bash -c`. `calculate` and `fetch_json` were removed.
- `cterm/task_tool.py`: tool definition for `create_new_task` delegation (`task`, `subagent_type`, `thoroughness_level`). The executor is not implemented yet.
- `cterm/agent_ui.py`: common UI protocol consumed by `ToolAgent`. It includes spinner, message, tool-call, tool-output, approval, and thinking-trace hooks (`thinking_trace_delta`, `thinking_trace_complete`).
- `cterm/ui/basic.py`: plain terminal UI layer used by `ToolAgent` for spinner lifecycle, tool-call display, stream output, thinking trace display, and result formatting.
- `cterm/ui/tui.py`: Textual default interface. It renders a transcript with pending-line support for live tool output, a separate live thinking-trace entry that updates in place, and a completed collapsed thinking-trace entry (`▶ THINKING: ...`) that expands in place to `▼ THINKING: ...` on click. Live bash stream text is sanitized for ANSI/OSC/control sequences before rendering; keep terminal presentation changes here rather than re-embedding formatting helpers in `cterm/llm.py`.
- `cterm/mcp/utils/bash_utils.py`: safe command parsing and execution helpers for `bash`, including pipelines, `&&`, glob/env expansion, streaming, output finalization, and privileged command routing. `FORBIDDEN_CHARS = set("><\`\\'()")` rejects shell metacharacters.
- `cterm/privilege.py`: shared privileged-command whitelist helpers (`read_privileged_whitelist`, `add_privileged_binary`, `prompt_to_add_privileged_binary`). `DEFAULT_PRIVILEGED_BINARIES` includes `/usr/bin/apt`, `/usr/bin/apt-get`, `/usr/bin/tee`, `/usr/bin/snap`. Whitelist stored at `~/.config/cterm/privileged_whitelist` (overridable via `CTERM_PRIVILEGED_WHITELIST` env var).
- `cterm/resolve_url.py`: `SoftwareResolver` class for finding Linux download URLs for given app names (search, rank, fetch, extract, validate pipeline using `requests` and `BeautifulSoup`).
- `cterm/cterm-mcp.service`: systemd user service unit (`Type=simple`, `Restart=on-failure`), installed to `/usr/lib/systemd/user/`.
- `cterm/llm_utils/`: Ollama/OpenRouter/llama.cpp HTTP client (`chat_api.py`), Unix-socket MCP client (`mcp_client.py`), spinner/async/socket-path helpers (`utils.py`). `chat_api.py` normalizes OpenRouter and llama.cpp OpenAI-style chat responses into the Ollama-shaped response expected by `llm.py`; tool-result messages are converted to user messages for those providers. When `on_thinking_delta` is supplied, `chat_api.py` uses streaming requests for all providers: Ollama JSONL streams and OpenAI-compatible SSE streams for OpenRouter/llama.cpp. It extracts thinking/reasoning deltas from fields such as `thinking`, `reasoning`, `reasoning_content`, `reasoning_text`, and `reasoning_details`, while reconstructing final content and streamed tool-call arguments into the same normalized response shape. `Spinner` supports updating its message for `TerminalUI`. Uses implicit namespace package (no `__init__.py`).
- `cterm/skills_loader/`: markdown skill loading, strict skill selection, and prompt rendering.
- `cterm/skills/`: built-in skill markdown files. Each skill needs non-empty `## When to use` and `## Skill` sections or the loader ignores it. Includes `Filesystem_Operations.md` (replaced `Finder_Search.md`) and `Storage_Analysis.md`. Both currently have empty `## Skill` sections, so they are not loaded until content is added.
- `tests/`: stdlib `unittest` coverage for orchestration (`test_llm_orchestration.py`), skill loading (`test_skills_loader.py`), bash parsing (`test_bash_parsing.py`), finder (`test_finder_tool.py`), exec (`test_exec_tool.py`), MCP client (`test_mcp_client.py`), tool approval (`test_tool_approval.py`), websearch (`test_websearch_tool.py`), main/TUI entry behavior (`test_main_tui.py`), provider thinking-stream normalization (`test_chat_api_streaming.py`), and thinking trace UI/agent behavior (`test_thinking_traces.py`; Textual-specific tests skip when Textual is unavailable).

## Development Commands

Run the fast local test suite with:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

Run focused tests while changing risky areas:

```bash
python3 -m unittest tests.test_bash_parsing
python3 -m unittest tests.test_llm_orchestration
python3 -m unittest tests.test_skills_loader
python3 -m unittest tests.test_finder_tool
python3 -m unittest tests.test_exec_tool
python3 -m unittest tests.test_mcp_client
python3 -m unittest tests.test_tool_approval
python3 -m unittest tests.test_websearch_tool
python3 -m unittest tests.test_main_tui
python3 -m unittest tests.test_chat_api_streaming
python3 -m unittest tests.test_thinking_traces
```

Useful manual CLI checks:

```bash
python3 -m cterm --version
python3 -m cterm -i
python3 -m cterm -d "your prompt"
```

`tests/streaming.py` and `tests/real_cterm_prompts.py` are integration/smoke scripts. They can start the user systemd service, call a real model API, run desktop commands, or install/remove software. Do not treat them as routine unit tests.

Current caveats:

- `tests/test_llm_orchestration.py` still contains legacy planner handoff tests for `_build_worker_handoff`, which no longer exists after the single-agent orchestration change. Update those tests intentionally when working on orchestration.
- Some older tests still patch stale import paths such as `cterm.tools_mcp`; the current tool module is `cterm.mcp.tools_mcp`. Treat those failures as test-maintenance work, not runtime evidence that the old path exists.

## Architecture Rules

- Preserve the current single-agent `ToolAgent` loop unless intentionally reworking orchestration. Skills are selected before the loop and influence available tools through `_ollama_tools`.
- `create_new_task` is appended to the tool list after MCP tool filtering. Because `task_tool.execute_task` is not implemented, changes that make delegation reachable in normal flows need implementation and tests together.
- There is no current `_verify_history` verifier or `_build_worker_handoff` path. Do not add guidance or tests that assume planner handoff unless you are intentionally restoring that architecture.
- Debug output is part of the observable behavior for tests. When changing orchestration logging, update tests intentionally.
- Keep MCP server responses newline-delimited JSON. Streaming tool calls send zero or more `"stream"` frames followed by one final `"result"` frame.
- Thinking traces are controlled by `Config.stream_thinking_traces`; keep the `cterm -i` prompt, config key, `ToolAgent._chat_with_optional_thinking`, provider stream parsing, and both UI implementations in sync when changing this behavior.
- The socket path logic exists in multiple places with different implementations. `cterm_server.get_socket_path` uses `pwd.getpwuid(os.getuid()).pw_name`; `cterm.__main__.get_socket_path` uses `os.getlogin()`; `cterm.llm_utils.utils.get_socket_path` mirrors the `__main__` variant. Keep them compatible when changing.
- Privileged tool approval in the restricted bash path is a two-sided flow: the MCP service returns `approval_required`, the client (`llm.py`) calls `privilege.prompt_to_add_privileged_binary` on `/dev/tty`, retries with `allow_privileged=True`, and the service updates the whitelist before executing.
- Provider support is abstracted in `chat_with_model_api` in `chat_api.py`, dispatching to Ollama, OpenRouter, or llama.cpp based on `config.api_provider`. Keep `__main__.py` provider-specific model selection and `Config` keys in sync when changing provider setup.
- For OpenRouter and llama.cpp, preserve `_normalize_messages_for_openai`, `_normalize_openai_response`, and `_normalize_openai_stream_response` semantics when changing tool calls, tool-result history, or streaming thinking traces. Streamed tool-call argument chunks must be reassembled before the response reaches `ToolAgent`.
- `_BLOCKED_BINARIES` in `utils.py` blocks specific sudo-target or shell binaries from restricted and unrestricted bash. Currently empty in behavior; add entries intentionally with tests.

## UI Notes

- `AgentUI` is the boundary between `ToolAgent` and presentation. Add new agent-facing UI behavior to `cterm/agent_ui.py` and implement it in both `cterm/ui/basic.py` and `cterm/ui/tui.py`.
- The Textual transcript has two distinct live-update surfaces: pending tool stream output (`append_stream`) and live thinking traces (`append_thinking_delta`). Do not reuse the pending tool stream slot for thinking traces; it can overwrite or be overwritten by bash stdout/stderr.
- Completed TUI thinking traces should remain a single collapsed line starting with `▶ THINKING:` and expand in place to `▼ THINKING:` when clicked. Avoid reintroducing a separate "Thinking trace" header line.
- Live bash stream text in the TUI should be sanitized before rendering. Preserve `_sanitize_stream_text` behavior so ANSI/OSC/control sequences from commands such as `snap` or `apt` do not create colored blocks or corrupt transcript layout.
- Final answer rendering in the TUI uses Markdown; live tool output uses `Text` renderables. Keep command-output summaries capped through `_emit_capped` rather than dumping long captured output after a streamed command finishes.

## Bash Tool Safety

`cterm.mcp.tools_mcp.bash` must not become a raw shell escape hatch.

- It executes parsed argv with `subprocess`, not `shell=True`.
- It supports only the command features implemented in `cterm/mcp/utils/bash_utils.py`: unquoted `&&`, unquoted `|`, double-quoted args, env var and `~` expansion, glob expansion, and stderr suppression as `2>/dev/null` or `2> /dev/null`.
- Other redirection and shell metacharacters are intentionally rejected through `FORBIDDEN_CHARS`.
- A leading `sudo` enters privileged routing. The MCP service resolves the binary and returns an approval-required result if it is missing from the whitelist; the client asks the user, retries with approval, and the MCP service updates the whitelist before routing through `/usr/lib/cterm/cterm-privileged`.
- The unrestricted mode runs commands through `/bin/bash -c` with normal shell syntax when the config key `bash_unrestricted` is true (set via `~/.config/cterm/config.json`). Before execution, the unrestricted path scans common `sudo <binary>` forms and returns `approval_required` when the resolved binary is not whitelisted.
- The unrestricted path also scans for blocked binaries via `_BLOCKED_BINARIES` and rejects them before execution.
- Any change to parsing, return-code handling, streaming, timeout handling, output-file persistence, unrestricted mode, or privileged routing needs focused coverage in `tests/test_bash_parsing.py`.
- Be careful with commands that may modify the desktop OS. The test suite should not require root, package installation, or external services.

## Service and Privilege Notes

- The CLI starts `cterm-mcp.service` with `systemctl --user start` when the Unix socket is missing.
- The server has a 20-minute inactivity timeout and removes its socket on shutdown.
- `cterm/sudocterm.sh` installs/removes the privileged wrapper, sudoers file, and initial user-owned whitelist at the cterm config path.
- `cterm/cterm-mcp.service` is the systemd unit file (installed to `/usr/lib/systemd/user/`).
- `cterm/privilege.py` provides shared whitelist helpers: `read_privileged_whitelist`, `add_privileged_binary`, `prompt_to_add_privileged_binary`. `DEFAULT_PRIVILEGED_BINARIES` includes `/usr/bin/apt`, `/usr/bin/apt-get`, `/usr/bin/tee`, `/usr/bin/snap`.
- The client owns user interaction for adding whitelist entries. In the restricted path, the MCP service owns whitelist mutation and command execution. The wrapper checks the same whitelist again as the root-side enforcement point.
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

The loader ignores `README.md`, selects skills by filename stem, and only loads files where both `## When to use` and `## Skill` have content. `render_for_system_prompt` returns selected `## Skill` content, and `ToolAgent._ollama_tools` currently uses that rendered text to restrict available tools for `Filesystem_Operations`. Keep `## When to use` strict and specific; the selector prompt asks the model to prefer a single skill.

Current skills:

- `Filesystem_Operations.md` - file search and manipulation
- `Storage_Analysis.md` - disk usage and cleanup candidate analysis

Note: both skills currently have empty `## Skill` sections.

## Coding Style

- This repo uses plain Python modules and stdlib `unittest`; no project-level formatter or package manager config is present.
- Follow the existing simple style: type hints where useful, small helper functions, direct dictionaries for protocol payloads, and explicit error dicts for tool failures.
- Keep imports lightweight. `requests` is already used for Ollama, OpenRouter, llama.cpp, and URL access; avoid introducing new dependencies unless the project is also given packaging metadata.
- Do not rewrite unrelated code or normalize old comments while making focused fixes.
- Prefer deterministic tests with mocked model responses over tests that depend on a live Ollama server.

## Files to Avoid Treating as Source of Truth

- `test_results.txt` and `new-logs.txt` are captured run logs. Use them as diagnostic history only.
- `tests/real_cterm_prompts.py` records real prompt behavior and writes log files. Do not run it casually.
