# AGENTS.md

Guidance for coding agents working in this repository.

## Project Shape

Cterm is a Python CLI and local tool harness for completing desktop OS tasks with an Ollama-backed model or OpenRouter.

The main pieces are:

- `cterm/__main__.py`: CLI entry point. Handles `cterm --version`, `cterm -i` (provider/model/bash-unrestricted init), `cterm --binary`, `cterm --debug`, `cterm -d`, and `cterm "message"`. Manages socket-based server lifecycle (`ensure_server_running` starts `cterm-mcp.service` via systemctl). Provider dispatch supports both Ollama and OpenRouter. Socket path uses `os.getlogin()`.
- `cterm/config.py`: `Config` class reading/writing `~/.config/cterm/config.json`. Keys: `selected_model`, `small_model`, `bash_unrestricted`, `api_provider` (ollama/openrouter), `openrouter_api_key`, `openrouter_model`, `openrouter_small_model`.
- `cterm/llm.py`: planner/worker orchestration, Ollama/OpenRouter tool-call integration, skill selection, verification, debug logging, and MCP client lifecycle. `MAX_AGENT_ITERATIONS = 10` per worker; planner limit is 24 tool-call iterations (`range(1, 25)`). Worker tool filtering is driven by the selected skill: when `## Filesystem_Operations` is injected, the worker's tool set is restricted to `{finder, bash, exec, read_file, write_file}`. The sandboxed Python implementation is `exec_python`, but it is exposed to agents as `mcp.exec` for compatibility. Finder results are persisted to `~/cterm/data/` and handed off to the next agent. `_last_finder_history_item` only considers finder calls with `status == "success"` and `result.ok is True`. `_verify_history` uses the small model when available with `response_format="json"`. Privileged tool approval (`approval_required=True` / `approval_kind="privileged_whitelist"`) prompts the user via `privilege.prompt_to_add_privileged_binary` and retries. `_debug_orchestration` logs events including `planner_skills_selected`, `planner_dispatch_agent`, `planner_agent_result`, `planner_handoff_recorded`, `planner_final_answer`, `planner_unknown_tool`, `planner_missing_action`, `planner_iteration_limit`, `agent_tools_ready`, `agent_iteration`, `agent_tool_result`, `agent_final_answer`, `agent_iteration_limit`, `agent_forced_final_answer`, `agent_verified`, `verifier_result`, `verifier_failed`, and `skills_selection_failed`.
- `cterm/cterm_server.py`: persistent MCP-like tool server over a Unix domain socket in `/tmp/cterm_mcp_{username}.sock` (uses `pwd.getpwuid(os.getuid()).pw_name` with a `$USER` fallback), with 20-minute inactivity shutdown and newline-delimited JSON frames for streaming output. Supports `tools/list` and `tools/call` via `hasattr(tool_func, '__mcp_tool__')`.
- `cterm/tools_mcp.py`: exposed tool implementations. The `bash` tool is the highest-risk surface and must stay constrained. Also provides `exec_python` (sandboxed Python, exposed as `mcp.exec`), `finder` (file search), `read_file`, `write_file`, and `system_info`. `_BLOCKED_BINARIES` restricts sudo-target or shell binaries in favor of cterm tool equivalents; it is currently empty except for commented examples. `_is_bash_unrestricted()` reads `bash_unrestricted` from config; unrestricted path uses `/bin/bash -c`. `calculate` and `fetch_json` were removed.
- `cterm/utils.py`: safe command parsing and execution helpers for `bash`, including pipelines, `&&`, glob/env expansion, and privileged command routing. `FORBIDDEN_CHARS = set("><\`\\'()")` rejects shell metacharacters.
- `cterm/privilege.py`: shared privileged-command whitelist helpers (`read_privileged_whitelist`, `add_privileged_binary`, `prompt_to_add_privileged_binary`). `DEFAULT_PRIVILEGED_BINARIES` includes `/usr/bin/apt`, `/usr/bin/apt-get`, `/usr/bin/tee`, `/usr/bin/snap`. Whitelist stored at `~/.config/cterm/privileged_whitelist` (overridable via `CTERM_PRIVILEGED_WHITELIST` env var).
- `cterm/resolve_url.py`: `SoftwareResolver` class for finding Linux download URLs for given app names (search, rank, fetch, extract, validate pipeline using `requests` and `BeautifulSoup`).
- `cterm/cterm-mcp.service`: systemd user service unit (`Type=simple`, `Restart=on-failure`), installed to `/usr/lib/systemd/user/`.
- `cterm/llm_utils/`: Ollama/OpenRouter HTTP client (`chat_api.py`), Unix-socket MCP client (`mcp_client.py`), spinner/async/socket-path helpers (`utils.py`). Uses implicit namespace package (no `__init__.py`).
- `cterm/skills_loader/`: markdown skill loading, strict skill selection, and prompt rendering.
- `cterm/skills/`: built-in skill markdown files. Each skill needs `## When to use` and `## Skill` sections. Includes `Filesystem_Operations.md` (replaced `Finder_Search.md`) and `Storage_Analysis.md`. Both currently have empty `## Skill` sections.
- `tests/`: stdlib `unittest` coverage for orchestration (`test_llm_orchestration.py`), skill loading (`test_skills_loader.py`), bash parsing (`test_bash_parsing.py`), finder (`test_finder_tool.py`), exec (`test_exec_tool.py`), MCP client (`test_mcp_client.py`), and tool approval (`test_tool_approval.py`).

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
```

Useful manual CLI checks:

```bash
python3 -m cterm --version
python3 -m cterm -i
python3 -m cterm -d "your prompt"
```

`tests/streaming.py` and `tests/real_cterm_prompts.py` are integration/smoke scripts. They can start the user systemd service, call the real Ollama API, run desktop commands, or install/remove software. Do not treat them as routine unit tests.

## Architecture Rules

- Preserve the planner/worker split in `ToolAgent`: the planner only calls `new_agent`; task-specific skills are injected inside worker agents.
- Keep worker actions naturally atomic. The planner prompt intentionally warns against unsupported shell syntax such as `cd`, `||`, `;`, command substitution, and extra tool arguments.
- The verifier in `_verify_history` is scoped to the assigned action. Do not make worker verification require broader or later request steps.
- Debug output is part of the observable behavior for tests. When changing orchestration logging, update tests intentionally.
- Keep MCP server responses newline-delimited JSON. Streaming tool calls send zero or more `"stream"` frames followed by one final `"result"` frame.
- The socket path logic exists in multiple places with different implementations. `cterm_server.get_socket_path` uses `pwd.getpwuid(os.getuid()).pw_name`; `cterm.__main__.get_socket_path` uses `os.getlogin()`; `cterm.llm_utils.utils.get_socket_path` mirrors the `__main__` variant. Keep them compatible when changing.
- Privileged tool approval in the restricted bash path is a two-sided flow: the MCP service returns `approval_required`, the client (`llm.py`) calls `privilege.prompt_to_add_privileged_binary` on `/dev/tty`, retries with `allow_privileged=True`, and the service updates the whitelist before executing.
- Provider support is abstracted in `chat_with_model_api` in `chat_api.py`, dispatching to either Ollama or OpenRouter based on `config.api_provider`.
- `_BLOCKED_BINARIES` in `utils.py` blocks specific sudo-target or shell binaries from restricted and unrestricted bash. Currently empty in behavior; add entries intentionally with tests.

## Bash Tool Safety

`cterm.tools_mcp.bash` must not become a raw shell escape hatch.

- It executes parsed argv with `subprocess`, not `shell=True`.
- It supports only the command features implemented in `cterm/utils.py`: unquoted `&&`, unquoted `|`, double-quoted args, env var and `~` expansion, glob expansion, and stderr suppression as `2>/dev/null` or `2> /dev/null`.
- Other redirection and shell metacharacters are intentionally rejected through `FORBIDDEN_CHARS`.
- A leading `sudo` enters privileged routing. The MCP service resolves the binary and returns an approval-required result if it is missing from the whitelist; the client asks the user, retries with approval, and the MCP service updates the whitelist before routing through `/usr/lib/cterm/cterm-privileged`.
- The unrestricted mode runs commands through `/bin/bash -c` with normal shell syntax when the config key `bash_unrestricted` is true (set via `~/.config/cterm/config.json`). Before execution, the unrestricted path scans common `sudo <binary>` forms and returns `approval_required` when the resolved binary is not whitelisted.
- The unrestricted path also scans for blocked binaries via `_BLOCKED_BINARIES` and rejects them before execution.
- Any change to parsing, return-code handling, streaming, unrestricted mode, or privileged routing needs focused coverage in `tests/test_bash_parsing.py`.
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

The loader ignores `README.md`, selects skills by filename stem, and only injects selected `## Skill` content into worker agents. Keep `## When to use` strict and specific; the selector prompt asks the model to prefer a single skill.

Current skills:

- `Filesystem_Operations.md` - file search and manipulation
- `Storage_Analysis.md` - disk usage and cleanup candidate analysis

Note: both skills currently have empty `## Skill` sections.

## Coding Style

- This repo uses plain Python modules and stdlib `unittest`; no project-level formatter or package manager config is present.
- Follow the existing simple style: type hints where useful, small helper functions, direct dictionaries for protocol payloads, and explicit error dicts for tool failures.
- Keep imports lightweight. `requests` is already used for Ollama and OpenRouter URL access; avoid introducing new dependencies unless the project is also given packaging metadata.
- Do not rewrite unrelated code or normalize old comments while making focused fixes.
- Prefer deterministic tests with mocked model responses over tests that depend on a live Ollama server.

## Files to Avoid Treating as Source of Truth

- `test_results.txt` and `new-logs.txt` are captured run logs. Use them as diagnostic history only.
- `tests/real_cterm_prompts.py` records real prompt behavior and writes log files. Do not run it casually.
