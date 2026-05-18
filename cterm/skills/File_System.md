```markdown
## When to use

Use this skill when the user wants to locate files, collect files into a folder, organize items, remove files, empty trash, or perform general filesystem management on a Linux machine.

## Skill

Manage filesystem contents safely using explicit paths and non-destructive operations.

Deletion policy:
- Never permanently delete directly
- Move items to trash instead
- No user confirmation flow
- Empty trash only when explicitly requested

### Constraints

- Supported: `~`, `$HOME`, `${HOME}`, `2>/dev/null`
- Avoid `sudo` unless required
- Avoid broad scans of the whole filesystem
- Use explicit discovered paths only
- Prefer directory-by-directory exploration

### Workflow

1. Establish context:
   ```bash
   pwd
   ls -lah
   ```

2. Locate files/directories:

   Current directory:
   ```bash
   find PATH -maxdepth 1 -iname "NAME" 2>/dev/null
   ```

   Recursive search if needed:
   ```bash
   find PATH -type f -iname "NAME" 2>/dev/null
   find PATH -type d -iname "NAME" 2>/dev/null
   ```

3. Collect files into a folder:

   Create destination:
   ```bash
   mkdir -p DEST
   ```

   Copy discovered files:
   ```bash
   cp SOURCE DEST/
   ```

4. Organize / move files:
   ```bash
   mv SOURCE DEST/
   ```

5. Remove safely (move to trash only):
   ```bash
   gio trash PATH
   ```

6. Empty trash:
   ```bash
   gio trash --empty
   ```

### Rules

- Continue after partial failures
- Ignore permission noise unless blocking
- Never use:
   ```bash
   rm -rf
   rm -f
   ```
- Operate only on discovered paths
- Prefer targeted operations over recursion

### Report

Return:
- files/directories found
- actions performed (copied / moved / trashed)
- destination folders used
- trash actions
- failures or skipped paths
```