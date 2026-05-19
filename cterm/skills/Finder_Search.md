## When to use

Use this skill when the user asks to find files, folders, reports, CVs,
resumes, app installs, config files, service data, or similar known items on a
Linux desktop.

## Skill

Use the `finder` tool first for file and folder search. Keep searches targeted.

Rules:
- Do not modify files.

- Search likely locations before broad locations.
- Prefer exact names or small glob lists.
- Return full paths by combining the tool's `path` with each relative match.
- If too many results appear, narrow by folder, file type, or name.

Common starting paths:
- Desktop files: `~/Desktop`
- User documents: `~/Documents`, `~/Downloads`, `~`
- App configs: `~/.config`, `~/.local/share`, `/etc`
- App data: `~/.local/share`, `/var/lib`, `/opt`
- Installed commands: use `run_shell` with `command -v NAME` first

Finder examples:
- Reports:
  Use `finder` with `path="~"`, `include=["*report*", "*Report*", "*.pdf", "*.docx", "*.xlsx"]`, `type_filter="file"`, `max_depth=4`.
- spotify config:
  Search `~/.config`, `~/.local/share`, `/var/lib`, and `/etc` for names containing `spotify` or `Spotify`.

Report:
- what was searched
- matching full paths
- whether results were truncated
- what to try next if no match was found
