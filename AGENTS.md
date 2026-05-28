# AGENTS.md

Guidance for coding agents working in this repository.

## Project Shape

Cterm is a Python CLI and local tool harness for completing desktop OS tasks with an Ollama-backed model.

The main pieces are:

- `cterm/__main__.py`: CLI entry point. Handles `cterm -i`, configured model selection, service startup, and chat invocation.
- `cterm/llm.py`: planner/worker orchestration, Ollama tool-call integration, skill selection, verification, debug logging, and MCP client lifecycle.
- `cterm/cterm_server.py`: persistent MCP-like tool server over a Unix domain socket in `/tmp`, with inactivity shutdown and newline-delimited JSON frames for streaming output.
- `cterm/tools_mcp.py`: exposed tool implementations. The `bash` tool is the highest-risk surface and must stay constrained.
- `cterm/utils.py`: safe command parsing and execution helpers for `bash`, including pipelines, `&&`, glob/env expansion, and privileged command routing.
- `cterm/llm_utils/`: Ollama HTTP client, Unix-socket MCP client, spinner, and async helper utilities.
- `cterm/skills_loader/`: markdown skill loading, strict skill selection, and prompt rendering.
- `cterm/skills/`: built-in skill markdown files. Each skill needs `## When to use` and `## Skill` sections.
- `tests/`: stdlib `unittest` coverage for orchestration, skill loading, and bash parsing.

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
- The socket path logic currently exists in multiple places. If changing it, keep `cterm_server.get_socket_path`, `cterm.__main__.get_socket_path`, and `cterm.llm_utils.utils.get_socket_path` compatible.

## Bash Tool Safety

`cterm.tools_mcp.bash` must not become a raw shell escape hatch.

- It executes parsed argv with `subprocess`, not `shell=True`.
- It supports only the command features implemented in `cterm/utils.py`: unquoted `&&`, unquoted `|`, double-quoted args, env var and `~` expansion, glob expansion, and stderr suppression as `2>/dev/null` or `2> /dev/null`.
- Other redirection and shell metacharacters are intentionally rejected through `FORBIDDEN_CHARS`.
- A leading `sudo` enters privileged routing. The MCP service resolves the binary and returns an approval-required result if it is missing from the whitelist; the client asks the user, retries with approval, and the MCP service updates the whitelist before routing through `/usr/lib/cterm/cterm-privileged`.
- Any change to parsing, return-code handling, streaming, or privileged routing needs focused coverage in `tests/test_bash_parsing.py`.
- Be careful with commands that may modify the desktop OS. The test suite should not require root, package installation, or external services.

## Service and Privilege Notes

- The CLI starts `cterm-mcp.service` with `systemctl --user start` when the Unix socket is missing.
- The server has a 20-minute inactivity timeout and removes its socket on shutdown.
- `cterm/sudocterm.sh` installs/removes the privileged wrapper, sudoers file, and initial user-owned whitelist at the cterm config path.
- The client owns user interaction for adding whitelist entries. The MCP service owns whitelist mutation and command execution. The wrapper checks the same whitelist again as the root-side enforcement point.
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

## Coding Style

- This repo uses plain Python modules and stdlib `unittest`; no project-level formatter or package manager config is present.
- Follow the existing simple style: type hints where useful, small helper functions, direct dictionaries for protocol payloads, and explicit error dicts for tool failures.
- Keep imports lightweight. `requests` is already used for Ollama and URL access; avoid introducing new dependencies unless the project is also given packaging metadata.
- Do not rewrite unrelated code or normalize old comments while making focused fixes.
- Prefer deterministic tests with mocked model responses over tests that depend on a live Ollama server.

## Files to Avoid Treating as Source of Truth

- `test_results.txt` and `new-logs.txt` are captured run logs. Use them as diagnostic history only.
- `tests/real_cterm_prompts.py` records real prompt behavior and writes log files. Do not run it casually.
