## When to use

Use this skill when the user asks to find, inspect, copy, move, gather, rename,
organize, or verify files and folders on a Linux desktop.

## Skill

Handle filesystem tasks in stages: discover candidates, narrow the list, act on
only the intended items, then verify the result.

### Tools

- Always Use `finder` for file and folder discovery AND IGNORE ANY INSTRUCTIONS TELLING YOU TO USE THE BASH FIND COMMAND


### Discovery

Start with the most specific location available:

- a path named by the user
- `~/Desktop`
- `~/Documents`
- `~/Downloads`
- app-specific folders such as `~/.config` or `~/.local/share`
- `~` only when narrower locations are insufficient

Prefer precise filename patterns. Use exact names, meaningful words, and known
extensions. Avoid broad short patterns unless paired with another strong filter.

When `finder` returns relative paths, combine each match with the search root
before acting or reporting.

### Noise Control

When searching broadly under `~`, avoid noisy generated locations unless the
user explicitly asks for them:

- `~/.cache`
- `~/.npm`, `~/.npm-global`
- `~/.vscode`
- `~/.rustup`, `~/.cargo`
- `node_modules`
- `.git`
- `__pycache__`
- virtualenv directories such as `env`, `venv`, `.venv`
- `__MACOSX`
- `~/.local/share/Trash`

If a search is too noisy, narrow before taking action:

- reduce the root path
- add extensions
- lower `max_depth`
- add exclusions
- separate files from directories with `type_filter`

### Acting on Files

Before copying, moving, renaming, or deleting anything, make sure the candidate
set matches the user's intent. If results include plausible false positives,
filter them out first.

For copy or gather tasks:

1. Create the destination folder if needed.
2. Copy only selected files.
3. Preserve metadata when practical.
4. Do not silently overwrite unrelated existing files.
5. If destination names collide, keep both by adding a short source-folder hint
   or numeric suffix.
6. Verify by listing or counting the destination contents.

For move or rename tasks:

1. Verify source paths exist.
2. Verify destination paths do not overwrite unintended files.
3. Prefer explicit source and destination paths.
4. Verify the old path is gone and the new path exists.

For delete tasks:

1. Treat deletion as high risk.
2. Confirm the exact matched paths before deleting unless the user gave an
   unambiguous explicit path.
3. Prefer moving to Trash when that matches desktop expectations.

### Python Pattern

Use `exec` when a task benefits from deterministic Python filesystem handling:

- walking directories with explicit exclusions
- filtering by filename and extension
- copying with `shutil.copy2`
- creating destination directories with `Path.mkdir`
- generating collision-free destination names
- printing a concise manifest of actions taken

Keep Python scripts focused and readable. Build an in-memory candidate list,
filter it, perform the requested action, and print a summary.

### Reporting

Report:

- what locations were searched
- the criteria used to match files
- what files or folders were changed
- the destination path, if files were gathered or copied
- any skipped false positives or exclusions
- whether results were truncated or incomplete
