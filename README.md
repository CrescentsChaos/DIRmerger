# dirmerge

**Merge two project folders the way git merges branches.** No repository required, no dependencies, one Python file.

`dirmerge` takes two directories, finds what each side changed, and merges them file by file using a real three-way (diff3) algorithm. Edits in different places combine automatically; only genuinely overlapping edits become conflicts, and you can solve those interactively, by rule, or automatically when it is provably safe.

```text
$ python dirmerge.py A B -o merged
Common ancestor: git commit 16e30ce init

  cfg.txt        CONFLICT (1 unresolved)
  core.py        merged cleanly
                   - renamed in A: app.py -> core.py
  deps.lock      rule: took B
  old_notes.txt  deleted (B)

5 files | 0 conflicts solved | 1 unresolved | 0 flagged invalid
```

## Features

- **Beginner wizard**: run it with no arguments and it asks everything step by step
- **Automatic common ancestor** via git (the equivalent of `git merge-base`) when both folders are git repos, or pass your own with `--base`
- **True three-way merge** per file, with the built-in engine or git's own `git merge-file`
- **Rename detection**: a file renamed on one side and edited on the other is merged, not duplicated
- **Conflict solving**: interactive prompts, safe auto-solve, fixed rules, and remembered answers (like `git rerere`)
- **Per-file rules**: e.g. always take B's lockfile, skip minified files
- **Syntax verification** after merging, so a merge that breaks valid code is flagged instead of silently written
- **Safe by default**: inputs are never modified, `--in-place` makes a backup first
- **Reports** as HTML, Markdown or JSON, plus a `--stat` overview of who changed what
- Respects `.gitignore`, preserves line endings (CRLF/LF), BOM, file modes, and non-UTF-8 bytes
- Pure Python standard library

## Requirements

- Python 3.8 or newer
- `git` on your `PATH` is **optional** but enables ancestor auto-detection, `.gitignore` support and `--engine git`

## Quick start

```bash
# Guided mode: just answer the questions
python dirmerge.py

# Two git clones of the same project: the ancestor is found automatically
python dirmerge.py ./my-version ./their-version -o ./merged

# Not git? Give the original folder both versions came from
python dirmerge.py ./A ./B -o ./merged --base ./original

# Update folder A itself (a backup folder is created first)
python dirmerge.py ./A ./B --in-place

# Preview: who changed what, and where can conflicts happen?
python dirmerge.py ./A ./B --stat
```

## Why an ancestor matters

With only two versions, no tool can tell whether a differing line was **added** on one side or **deleted** on the other. Git always has a common ancestor, which is why its merges are reliable.

| Situation | What dirmerge does |
|---|---|
| Both folders are git repos with shared history | Finds the merge-base commit automatically |
| You provide `--base DIR` | Uses it for a real three-way merge |
| No ancestor available | Falls back to a cautious two-way merge: every difference is a conflict, nothing is guessed |

## How it works

1. **Find the common ancestor** (git merge-base, or `--base`).
2. **Detect renames** between the ancestor and each side.
3. **Three-way merge** every file: ancestor vs A vs B, line by line.
4. **Solve conflicts** using your rules, remembered answers, safe heuristics, or by asking you.
5. **Verify** merged `.py`, `.json`, `.xml`, `.toml`, `.yaml`, `.js` and `.sh` files still parse.
6. **Report** what happened.

For each file the decision follows git's rules:

| Ancestor | A | B | Result |
|---|---|---|---|
| same | same | changed | take B |
| same | changed | same | take A |
| same | changed | changed identically | take either |
| same | changed | changed differently | merge line by line, conflict only where edits overlap |
| exists | deleted | unchanged | delete |
| exists | deleted | modified | conflict (you choose keep or delete) |
| missing | added | missing | keep A's file |
| missing | added | added differently | two-way merge |

## Solving conflicts

Choose a strategy with `--on-conflict`:

| Mode | Behaviour |
|---|---|
| `guided` | **Default in a terminal.** Auto-solve the safe ones, ask you about the rest |
| `interactive` | Ask about every conflict |
| `smart` | Auto-solve only the safe ones, leave the rest as markers |
| `markers` | Leave git-style `<<<<<<<` markers (default when not in a terminal) |
| `ours` / `theirs` | Always keep A / B |
| `union` | Keep both sides when both only added lines |

**What "safe" auto-solve means:**

- A whitespace-only difference is dropped in favour of the real change
- When both sides inserted different new lines at the same spot, both are kept
- Whitespace rules are skipped for whitespace-significant files (`.py`, `.yaml`, `.md`, ...)
- If an auto-solved file would no longer parse, the conflict is left for you instead

**Interactive prompt** shows the original, A and B with surrounding lines:

```text
== app.py  -  conflict 1 of 1 ==
   import os
  [a] A:
   + import sys
  [b] B:
   + import json
  Your choice [a/b/ab/ba/e/s/?]:
```

| Key | Action |
|---|---|
| `a` / `b` | keep A / keep B |
| `ab` / `ba` | keep both, in that order |
| `o` | go back to the original text |
| `e` | edit it yourself in `$EDITOR` |
| `s` | skip (leave markers) |
| `all-a` / `all-b` | same choice for all remaining conflicts |
| `quit` | stop asking, leave the rest as markers |

Left markers behind? Resolve them later:

```bash
python dirmerge.py --resolve ./merged
```

(`.orig` backups are written next to each fixed file unless you pass `--no-backup`.)

### Remember your answers

```bash
python dirmerge.py A B -o merged --rerere
```

Your choices are stored in `~/.dirmerge/rerere.json` (override with `--rerere-file` or the `DIRMERGE_HOME` environment variable) and reused when the exact same conflict appears again.

## Per-file rules

```bash
python dirmerge.py A B -o merged \
  --rule "*.lock=take-b" \
  --rule "docs/*=ours" \
  --rule "*.min.js=skip"
```

Rules are matched against the relative path or the file name; the first match wins.

| Action | Meaning |
|---|---|
| `take-a` / `take-b` | use that whole file, no merging |
| `newer` | use the file with the newer modification time |
| `skip` | leave the file out of the result |
| any conflict mode | use that mode for this file (`guided`, `ours`, `smart`, ...) |

Rules can also live in a `.dirmerge.json` in the current directory (or pass `--config FILE`):

```json
{
  "rules": { "*.lock": "take-b", "docs/*": "ours" },
  "ignore": ["dist", "*.log"],
  "on_conflict": "guided"
}
```

## Command reference

```text
python dirmerge.py [DIR_A DIR_B] [options]
```

| Option | Description |
|---|---|
| `-o, --output DIR` | write the merged project here |
| `--in-place` | write into `DIR_A` (backup made first) |
| `--base DIR` | common-ancestor folder (otherwise auto-detected through git) |
| `--on-conflict MODE` | `guided`, `interactive`, `smart`, `markers`, `ours`, `theirs`, `union` |
| `--rule PATTERN=ACTION` | per-file rule, repeatable |
| `--config FILE` | JSON config (default: `./.dirmerge.json` if present) |
| `--only PATTERN...` | merge only matching files |
| `--ignore PATTERN...` | extra patterns to ignore |
| `--no-renames` | turn off rename detection |
| `--rename-threshold N` | similarity 0-1 to count as a rename (default `0.6`) |
| `--engine auto\|builtin\|git` | text merge engine (`auto` = built-in, git's for very large files) |
| `--conflict-style merge\|diff3` | `diff3` also shows the original inside markers |
| `--rerere`, `--rerere-file F` | remember and reuse your conflict answers |
| `--binary a\|b\|newer` | how to resolve differing binary files |
| `--report FILE` | write a report (`.html`, `.md` or `.json`) |
| `--stat` | show who changed what and where overlaps are, then exit |
| `--diff` | print differences, then exit |
| `--resolve PATH` | resolve conflict markers already in a file or folder |
| `--dry-run` | report only, write nothing |
| `--json` | print the report as JSON |
| `--no-git` | don't use git to find the ancestor |
| `--no-gitignore` | don't honour `.gitignore` in git repos |
| `--no-validate` | skip syntax checks |
| `--no-backup` | skip `.orig` (`--resolve`) and folder (`--in-place`) backups |
| `--force` | allow writing into a non-empty output folder |
| `--wizard` | start the beginner wizard |

**Exit codes:** `0` clean merge, `1` conflicts or flagged files remain, `2` usage error. This makes it easy to use in scripts and CI.

## Safety

- Input folders are **never modified**. The only exception is `--in-place`, which first copies the folder (excluding `.git`, `node_modules`, `__pycache__`) next to it.
- If `-o` points at an existing non-empty folder, it refuses unless you pass `--force`.
- Binary files are never text-merged. By default A is kept and B is saved beside it as `file.B-version`.
- Line endings, BOM, trailing-newline state and file permissions are preserved. Mixed CRLF/LF inputs are normalised and the report says so.
- Files with invalid UTF-8 round-trip byte-for-byte.
- After a clean merge, a file is flagged `INVALID SYNTAX` only if **both inputs were valid and the merge result is not**.

## Limitations

- Ancestor auto-detection needs both folders to be **git repository roots** (not sub-folders) with shared history. It uses each repo's `HEAD`; uncommitted changes in the folders are still merged.
- Without an ancestor, every difference is a conflict. Pass `--base` for a git-exact merge.
- Like git, edits on adjacent lines are treated as one overlapping change.
- Rename detection works between the ancestor and one side. If the other side adds a different file at the new path, it is treated as an ordinary add/add.
- `--rerere` matches the exact text of both sides, so a slightly different conflict is asked again.
- Symlinks are skipped (the summary tells you how many).
- The built-in line diff is exact and can be slow on very large files; `--engine auto` hands those to `git merge-file` when git is installed.
- Syntax checks for `.yaml`, `.js` and `.sh` need PyYAML, `node` and `bash` respectively; `.toml` needs Python 3.11+. Missing tools mean that check is skipped.

## Example: two clones of one project

```bash
git clone repo A
git clone repo B
# ... both get edited independently ...

python dirmerge.py A B --stat                      # preview overlaps
python dirmerge.py A B -o merged --report merge.html
```

`merge.html` lists every file with its result and collapsible diffs showing what A and B each contributed to the merged output.

## Contributing

Issues and pull requests are welcome. The whole tool is a single file, `dirmerge.py`, using only the standard library. When changing merge behaviour, please include a small before/after directory example that reproduces the case.

## License

Add your preferred license here (for example MIT) and include a `LICENSE` file in the repository.
