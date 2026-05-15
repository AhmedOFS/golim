## When to use

Use this skill when the user asks what is filling up their PC, what is taking
disk space, why storage is full, how to find large files/directories, or asks
for cleanup candidates on a Linux machine.

## Skill

Diagnose Linux disk usage without modifying anything unless the user explicitly
approves cleanup.

Prefer evidence-first investigation:
- avoid destructive commands
- avoid assumptions about apps/tools
- use `sudo` only if required

### Constraints

- Supported: `~`, `$HOME`, `${HOME}`, `2>/dev/null`
- Do not use `2>&1`
- Avoid globs, command substitution, or broad scans
- Use explicit discovered paths only

### Workflow

1. Check usage:
   ```bash
   df -h
   pwd
   ```

2. Pick the relevant filesystem:
   - near full
   - mentioned by user
   - contains current directory

3. Analyze top-level usage without crossing mounts:
   ```bash
   du -x -h -d 1 MOUNT 2>/dev/null
   ```

4. Drill into largest directories:
   ```bash
   du -x -h -d 1 PATH 2>/dev/null
   ```

5. If needed, find large files:
   ```bash
   find MOUNT -xdev -type f -size +1G -ls | sort -nr -k7 | head -20
   ```

6. If `df` usage is much larger than `du`, mention:
   - deleted-but-open files
   - permissions
   - reserved blocks
   - mount behavior

   Suggested follow-up:
   ```bash
   lsof +L1
   ```

### Rules

- Continue after partial failures
- Ignore permission noise unless blocking
- Never use:
  ```bash
  du -sh /*
  ```
- Focus on one filesystem at a time

### Report

Return:
- filesystem usage status
- largest directories and sizes
- largest files if checked
- likely cleanup candidates
- optional next cleanup commands for approval