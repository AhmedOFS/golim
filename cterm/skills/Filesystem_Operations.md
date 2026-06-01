## When to use

Use this skill when the user asks to find, inspect, copy, move, gather, rename,
organize, or verify files and folders on a Linux desktop.

## Skill

This skill follows the planner/worker architecture. The planner decomposes the
user request into sequential atomic actions, and each worker executes one action
independently. Finder results are automatically persisted between workers through
saved result files.

### Available Tools

Workers have access to: `finder`, `bash`, `exec`, `read_file`, `write_file`.

### Finder Discovery

Use `finder` for all file and folder discovery. Do not use `bash find`.

The `finder` tool returns relative paths from the search root. Combine each
match with the root before acting or reporting.

After a worker completes, the system automatically saves the last successful
`finder` result to `~/cterm/data/finder_results_<timestamp>_<pid>.json` and
appends a reference to the worker's output like:

```
Finder results file for planner and next agent: ~/cterm/data/finder_results_....json. The JSON field `paths` is a list of path strings.
```

The saved JSON contains the full `paths` array (absolute paths), the original
search arguments, and the raw result. Downstream workers can reference said list to write the code for copying or deleted the listed files/folders

### Worker Execution

Each worker receives one atomic action. It should:

1. Use `finder` for discovery, starting from the most specific path:
   - a path named by the user
   - `~/Desktop`, `~/Documents`, `~/Downloads`
   - app-specific folders such as `~/.config` or `~/.local/share`
   - `~` only when narrower locations are insufficient
2. Prefer precise filename patterns (exact names, meaningful words, known
   extensions). Avoid short broad patterns unless paired with another filter.
3. If the handoff mentions a finder results file, read it with `read_file` to
   reuse previous search results instead of re-searching.
4. Perform the assigned action (read, copy, move, delete, etc.).
5. Report concisely: what was found, what was changed, and the destination.

### Noise Control

When searching broadly under `~`
If a search is too noisy, narrow before taking action:

- reduce the root path
- add extensions
- lower `max_depth`
- add exclusions
- separate files from directories with `type_filter`

### Acting on Files

Before copying, moving, renaming, or deleting anything, confirm the candidate
set matches the user's intent. If results are too long, have a worker inspect them



### Python Pattern

Agents can use `exec` when a task benefits from deterministic Python filesystem handling:

- walking directories with explicit exclusions
- filtering by filename and extension
- copying with `shutil.copy2`
- creating destination directories with `Path.mkdir`
- generating collision-free destination names
- printing a concise manifest of actions taken



