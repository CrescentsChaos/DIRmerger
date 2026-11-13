#!/usr/bin/env python3
"""
dirmerge - merge two project folders the way git merges branches.

EASIEST WAY (just run it, it asks you everything):
    python dirmerge.py

COMMAND LINE:
    python dirmerge.py DIR_A DIR_B -o MERGED                    # auto-finds the git ancestor if possible
    python dirmerge.py DIR_A DIR_B -o MERGED --base DIR_ORIG    # give the original version yourself
    python dirmerge.py DIR_A DIR_B --in-place                   # update DIR_A itself (auto-backup first)
    python dirmerge.py DIR_A DIR_B --stat                       # who changed what, where overlaps are
    python dirmerge.py DIR_A DIR_B --diff                       # only show differences
    python dirmerge.py --resolve MERGED                         # fix leftover <<<<<<< conflicts later

THE "PERFECT MERGE" SEQUENCE (what this tool does for you, like git):
    1. Find the common ancestor  - if A and B are git repos, it runs the equivalent of
                                   `git merge-base` and exports that commit as the BASE.
    2. Detect renames            - a file renamed on one side and edited on the other is merged,
                                   not duplicated (like git's rename detection).
    3. Three-way merge each file - BASE vs A vs B, line by line (diff3), built-in engine or
                                   git's own `git merge-file` (--engine git; used for huge files).
    4. Solve conflicts           - rules per file, remembered answers (--rerere), auto-solve the
                                   obviously-safe ones, ask you about the rest.
    5. Verify                    - .py .json .xml .toml .yaml .js .sh results are syntax-checked.
    6. Report                    - --report merge.html shows what every side contributed.

Per-file rules (CLI or .dirmerge.json):
    --rule "*.lock=take-b" --rule "docs/*=ours" --rule "*.min.js=skip"
    actions: take-a, take-b, newer, skip, or any conflict mode (guided/interactive/smart/markers/ours/theirs/union)

Without an ancestor no tool can know if a line was added on one side or deleted on the
other, so the tool falls back to a cautious two-way merge (all differences = conflicts).

Safety: inputs are never modified (except --in-place, which backs up first), line endings /
BOM / file modes are preserved, binary files are never text-merged, any byte sequence
round-trips unchanged.
Exit code: 0 clean, 1 conflicts/flagged files remain, 2 usage error.
"""
import argparse
import difflib
import fnmatch
import hashlib
import html
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher

DEFAULT_IGNORE = [".git", ".hg", ".svn", "node_modules", "__pycache__", ".DS_Store", "*.pyc"]
WS_SENSITIVE = (".py", ".yaml", ".yml", ".md", ".rst", ".mk", ".pyi", ".haml", ".slim", ".coffee")
LINE_RE = re.compile(r"[^\n]*\n|[^\n]+")
MODES = ["guided", "interactive", "smart", "markers", "ours", "theirs", "union"]
RULE_ACTIONS = set(MODES) | {"take-a", "take-b", "newer", "skip"}
BIG_FILE_LINES = 6000          # --engine auto switches to `git merge-file` above this
CONFLICT_STYLE = "merge"       # or "diff3"

if os.name == "nt":
    os.system("")  # enable ANSI colours on Windows 10+


def color(text, code):
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        return f"\033[{code}m{text}\033[0m"
    return text


def stdin_is_tty():
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Basic helpers
# --------------------------------------------------------------------------- #
def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def is_binary(data):
    return b"\0" in data[:8192]


def decode(data):
    return data.decode("utf-8", errors="surrogateescape")


def encode(text):
    return text.encode("utf-8", errors="surrogateescape")


def dominant_eol(text):
    crlf = text.count("\r\n")
    return "\r\n" if crlf > text.count("\n") - crlf else "\n"


def normalize_eol(text, eol):
    t = text.replace("\r\n", "\n")
    return t.replace("\n", "\r\n") if eol == "\r\n" else t


def split_lines(text):
    return LINE_RE.findall(text)


def fixnl(chunk, eol):
    if chunk and not chunk[-1].endswith("\n"):
        return chunk[:-1] + [chunk[-1] + eol]
    return chunk


def ws_key(chunk):
    return "".join("".join(chunk).split())


# --------------------------------------------------------------------------- #
# Syntax validation (only used to catch merges that BREAK valid input)
# --------------------------------------------------------------------------- #
_WHICH = {}


def _which(name):
    if name not in _WHICH:
        _WHICH[name] = shutil.which(name)
    return _WHICH[name]


def _external_check(cmd, suffix, data):
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        name = f.name
    try:
        r = subprocess.run(cmd + [name], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return None
        return ((r.stderr or r.stdout).strip().splitlines() or ["check failed"])[0][:160]
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        os.unlink(name)


def syntax_error(path, data):
    low, text = path.lower(), decode(data)
    try:
        if low.endswith(".py"):
            compile(text, path, "exec")
        elif low.endswith(".json"):
            json.loads(text)
        elif low.endswith((".xml", ".svg", ".xhtml", ".plist", ".csproj")):
            ET.fromstring(data)
        elif low.endswith(".toml"):
            try:
                import tomllib
            except ImportError:
                return None
            tomllib.loads(text)
        elif low.endswith((".yaml", ".yml")):
            try:
                import yaml
            except ImportError:
                return None
            try:
                list(yaml.safe_load_all(text))
            except Exception as e:  # yaml.YAMLError
                return f"YAMLError: {str(e).splitlines()[0][:120]}"
        elif low.endswith((".js", ".mjs", ".cjs")) and _which("node"):
            return _external_check(["node", "--check"], os.path.splitext(low)[1], data)
        elif low.endswith((".sh", ".bash")) and _which("bash"):
            return _external_check(["bash", "-n"], ".sh", data)
    except (SyntaxError, ValueError) as e:  # ET.ParseError and TOMLDecodeError are subclasses
        return f"{type(e).__name__}: {e}"
    return None


# --------------------------------------------------------------------------- #
# Collecting files (honours .gitignore when the folder is a git repo)
# --------------------------------------------------------------------------- #
def _ignored(rel, patterns):
    rel = rel.replace(os.sep, "/")
    return any(fnmatch.fnmatch(part, p) for p in patterns for part in rel.split("/")) or \
        any(fnmatch.fnmatch(rel, p) for p in patterns)


def _git(*args, cwd=None, binary=False):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=not binary)


def git_toplevel(path):
    if not shutil.which("git"):
        return None
    r = _git("-C", path, "rev-parse", "--show-toplevel")
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 else None


def collect(root, ignore, use_git=True):
    """-> ({relpath: abspath}, skipped_symlinks, used_gitignore)"""
    files, skipped = {}, 0
    if use_git and git_toplevel(root) == os.path.realpath(root):
        r = _git("-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", binary=True)
        if r.returncode == 0:
            for raw in r.stdout.split(b"\0"):
                if not raw:
                    continue
                rel = os.fsdecode(raw)
                full = os.path.join(root, rel)
                if _ignored(rel, ignore):
                    continue
                if os.path.islink(full):
                    skipped += 1
                elif os.path.isfile(full):
                    files[rel] = full
            return dict(sorted(files.items())), skipped, True
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = sorted(d for d in dirnames
                             if not _ignored(os.path.normpath(os.path.join(rel_dir, d)), ignore)
                             and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            rel = os.path.normpath(os.path.join(rel_dir, name))
            full = os.path.join(dirpath, name)
            if _ignored(rel, ignore):
                continue
            if os.path.islink(full):
                skipped += 1
            else:
                files[rel.replace(os.sep, "/")] = full
    return files, skipped, False


# --------------------------------------------------------------------------- #
# Step 1: common ancestor through git (like `git merge-base`)
# --------------------------------------------------------------------------- #
def find_git_base(a, b, tmp):
    """-> (base_dir or None, human message)"""
    if not shutil.which("git"):
        return None, "git is not installed"
    ta, tb = git_toplevel(a), git_toplevel(b)
    if not ta or not tb:
        return None, "the folders are not both git repositories"
    if ta != os.path.realpath(a) or tb != os.path.realpath(b):
        return None, "auto-detect needs folders that are repository roots (not sub-folders)"
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    _git("init", "-q", cwd=repo)
    for name, top in (("a", ta), ("b", tb)):
        r = _git("fetch", "-q", "--no-tags", top, f"HEAD:refs/heads/{name}", cwd=repo)
        if r.returncode:
            return None, f"could not read git history of folder {name.upper()}: {r.stderr.strip()[:120]}"
    r = _git("merge-base", "a", "b", cwd=repo)
    if r.returncode or not r.stdout.strip():
        return None, "the two projects share no common git history"
    commit = r.stdout.strip()
    r = _git("archive", "--format=tar", commit, cwd=repo, binary=True)
    if r.returncode:
        return None, "could not export the common commit"
    dest = os.path.join(tmp, "base")
    os.makedirs(dest)
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        for m in tf:
            name = os.path.normpath(m.name)
            if os.path.isabs(name) or name.startswith(".."):
                continue
            target = os.path.join(dest, name)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
            elif m.isreg():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(tf.extractfile(m).read())
    info = _git("log", "-1", "--format=%h %s", commit, cwd=repo).stdout.strip()
    return dest, f"git commit {info}"


# --------------------------------------------------------------------------- #
# Step 2: rename detection
# --------------------------------------------------------------------------- #
def similarity(x, y, thr):
    if x == y:
        return 1.0
    if not x or not y or is_binary(x) or is_binary(y):
        return 0.0
    xl, yl = split_lines(decode(x)), split_lines(decode(y))
    if len(xl) > 4000 or len(yl) > 4000:
        return 0.0
    sm = SequenceMatcher(None, xl, yl, autojunk=False)
    if sm.real_quick_ratio() < thr or sm.quick_ratio() < thr:
        return 0.0
    return sm.ratio()


def detect_renames(BASE, X, rd, thr):
    """{old_path_in_base: new_path_in_X} for files that look moved."""
    removed = [p for p in BASE if p not in X]
    added = [p for p in X if p not in BASE]
    if not removed or not added:
        return {}
    big = len(removed) * len(added) > 3000
    pairs = []
    for r in removed:
        dr = rd(BASE[r])
        if not dr:
            continue
        for n in added:
            dn = rd(X[n])
            if not dn:
                continue
            same_name = os.path.basename(r) == os.path.basename(n)
            if big and dr != dn and not same_name:
                continue
            s = similarity(dr, dn, thr)
            if s >= thr:
                pairs.append((s + (0.01 if same_name else 0), r, n))
    pairs.sort(reverse=True)
    used_r, used_n, out = set(), set(), {}
    for _, r, n in pairs:
        if r not in used_r and n not in used_n:
            out[r] = n
            used_r.add(r)
            used_n.add(n)
    return out


# --------------------------------------------------------------------------- #
# Step 3: merge algorithms -> segments (plain lines or Conflict objects)
# --------------------------------------------------------------------------- #
@dataclass
class Conflict:
    base: object          # list of lines, or None when no ancestor is known
    a: list
    b: list
    la: str = "A"
    lb: str = "B"


@dataclass
class Ctx:
    path: str
    idx: int
    total: int
    before: list
    eol: str


def conflict_lines(c, eol):
    out = [f"<<<<<<< {c.la}{eol}"] + fixnl(c.a, eol)
    if CONFLICT_STYLE == "diff3" and c.base is not None:
        out += [f"||||||| base{eol}"] + fixnl(c.base, eol)
    return out + [f"======={eol}"] + fixnl(c.b, eol) + [f">>>>>>> {c.lb}{eol}"]


def _match_map(base, other):
    m = {}
    for blk in SequenceMatcher(None, base, other, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            m[blk.a + k] = blk.b + k
    return m


def merge3_segments(base, a, b, la, lb):
    ma, mb = _match_map(base, a), _match_map(base, b)
    stable = [i for i in range(len(base)) if i in ma and i in mb]
    segs, cur = [], []
    pbase = pa = pb = 0
    for i in stable + [len(base)]:
        ja, jb = (len(a), len(b)) if i == len(base) else (ma[i], mb[i])
        bc, ac, cc = base[pbase:i], a[pa:ja], b[pb:jb]
        if ac == cc:
            cur += ac
        elif ac == bc:
            cur += cc
        elif cc == bc:
            cur += ac
        else:  # both sides changed this region differently -> shrink the conflict
            p = 0
            while p < min(len(ac), len(cc)) and ac[p] == cc[p]:
                p += 1
            s = 0
            while s < min(len(ac), len(cc)) - p and ac[len(ac) - 1 - s] == cc[len(cc) - 1 - s]:
                s += 1
            hb = p if bc[:p] == ac[:p] else 0
            rest = bc[hb:]
            tb = s if (s and len(rest) >= s and rest[len(rest) - s:] == ac[len(ac) - s:]) else 0
            cur += ac[:p]
            if cur:
                segs.append(cur)
                cur = []
            segs.append(Conflict(rest[:len(rest) - tb], ac[p:len(ac) - s], cc[p:len(cc) - s], la, lb))
            cur += ac[len(ac) - s:]
        if i < len(base):
            cur.append(a[ja])
            pbase, pa, pb = i + 1, ja + 1, jb + 1
    if cur:
        segs.append(cur)
    return segs


def merge2_segments(a, b, la, lb):
    segs, cur = [], []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            cur += a[i1:i2]
        else:
            if cur:
                segs.append(cur)
                cur = []
            segs.append(Conflict(None, a[i1:i2], b[j1:j2], la, lb))
    if cur:
        segs.append(cur)
    return segs


def parse_markers(lines):
    """Turn text containing git conflict markers back into segments."""
    segs, cur, i = [], [], 0
    while i < len(lines):
        m = re.match(r"<{7}(?: (.*?))?\r?\n?$", lines[i])
        if m:
            j, state, a, base, b, lb, ok = i + 1, "a", [], None, [], "B", False
            while j < len(lines):
                s = lines[j]
                if state == "a" and re.match(r"\|{7}( |\r?\n|$)", s):
                    base, state = [], "base"
                elif state in ("a", "base") and re.match(r"={7}\r?\n?$", s):
                    state = "b"
                elif state == "b" and re.match(r">{7}( |\r?\n|$)", s):
                    mm = re.match(r">{7}(?: (.*?))?\r?\n?$", s)
                    lb, ok = (mm.group(1) if mm and mm.group(1) else "B"), True
                    break
                else:
                    (a if state == "a" else base if state == "base" else b).append(s)
                j += 1
            if ok:
                if cur:
                    segs.append(cur)
                    cur = []
                segs.append(Conflict(base, a, b, m.group(1) or "A", lb))
                i = j + 1
                continue
        cur.append(lines[i])
        i += 1
    if cur:
        segs.append(cur)
    return segs


def _git_merge_segments(tbase, ta, tb, la, lb):
    """Use git's own xdiff merge (`git merge-file`) and read its result back as segments."""
    if not shutil.which("git"):
        return None
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, t in (("a", ta), ("base", tbase), ("b", tb)):
            p = os.path.join(d, name)
            with open(p, "wb") as f:
                f.write(encode(t))
            paths.append(p)
        r = subprocess.run(["git", "merge-file", "-p", "--diff3", "-L", la, "-L", "BASE", "-L", lb] + paths,
                           capture_output=True)
    if r.returncode < 0 or r.returncode >= 128:
        return None
    return parse_markers(split_lines(decode(r.stdout)))


def text_segments(da, db, dbase, la, lb, engine="auto"):
    ta, tb = decode(da), decode(db)
    tbase = decode(dbase) if dbase is not None else None
    eol, notes = dominant_eol(ta), []
    if len({dominant_eol(t) for t in (ta, tb, tbase) if t and "\n" in t}) > 1:
        tb = normalize_eol(tb, eol)
        tbase = normalize_eol(tbase, eol) if tbase is not None else None
        notes.append("line endings differed; normalized to " + ("CRLF" if eol == "\r\n" else "LF"))
    if tbase is not None:
        big = ta.count("\n") + tb.count("\n") + tbase.count("\n") > BIG_FILE_LINES
        if engine == "git" or (engine == "auto" and big):
            segs = _git_merge_segments(tbase, ta, tb, la, lb)
            if segs is not None:
                notes.append("merged with git's own engine")
                return segs, eol, notes
        return merge3_segments(split_lines(tbase), split_lines(ta), split_lines(tb), la, lb), eol, notes
    return merge2_segments(split_lines(ta), split_lines(tb), la, lb), eol, notes


def render(segments, resolver, eol, path):
    """-> (lines, unresolved_count, resolved_count)"""
    total = sum(isinstance(s, Conflict) for s in segments)
    out, remaining, resolved, idx = [], 0, 0, 0
    for s in segments:
        if not isinstance(s, Conflict):
            out += s
            continue
        idx += 1
        res = resolver(s, Ctx(path, idx, total, out[-3:], eol)) if resolver else None
        if res is None:
            out += conflict_lines(s, eol)
            remaining += 1
        else:
            out += res
            resolved += 1
    return out, remaining, resolved


# --------------------------------------------------------------------------- #
# Step 4: conflict resolvers
# --------------------------------------------------------------------------- #
class Rerere:
    """Remembers how YOU resolved a conflict, and reuses it next time (like git rerere)."""

    def __init__(self, path):
        self.path, self.data, self.dirty, self.reused = path, {}, False, 0
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass

    @staticmethod
    def key(c):
        raw = "".join(c.a) + "\x00\x01\x00" + "".join(c.b)
        return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()

    def lookup(self, c, ctx):
        v = self.data.get(self.key(c))
        if v is None:
            return None
        self.reused += 1
        return split_lines(v)

    def record(self, c, lines):
        self.data[self.key(c)] = "".join(lines)
        self.dirty = True

    def save(self):
        if self.dirty:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)


def r_ours(c, ctx):
    return c.a


def r_theirs(c, ctx):
    return c.b


def r_union(c, ctx):
    if c.base is None:
        return fixnl(c.a, ctx.eol) + c.b if (not c.a or not c.b) else None
    return fixnl(c.a, ctx.eol) + c.b if (not c.base and c.a and c.b) else None


def r_smart(c, ctx):
    """Only rules that are safe: whitespace-only differences and 'both sides added code'."""
    if not ctx.path.endswith(WS_SENSITIVE):
        if ws_key(c.a) == ws_key(c.b):
            return c.a
        if c.base is not None:
            if ws_key(c.a) == ws_key(c.base):
                return c.b          # A only re-formatted; keep B's real change
            if ws_key(c.b) == ws_key(c.base):
                return c.a
    if c.base is not None and not c.base and c.a and c.b:
        return fixnl(c.a, ctx.eol) + c.b   # both sides inserted new lines here -> keep both
    return None


def prompt_choice(msg, valid):
    while True:
        try:
            s = input(msg).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if s in valid:
            return s
        print(f"  Please type one of: {', '.join(sorted(valid))}")


def _edit(c, ctx):
    block = "".join(conflict_lines(c, ctx.eol))
    suffix = os.path.splitext(ctx.path)[1] or ".txt"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8",
                                     errors="surrogateescape", newline="") as f:
        f.write(block)
        name = f.name
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or \
        ("notepad" if os.name == "nt" else "nano" if shutil.which("nano") else "vi")
    try:
        subprocess.call(shlex.split(editor, posix=(os.name != "nt")) + [name])
        with open(name, encoding="utf-8", errors="surrogateescape", newline="") as f:
            lines = split_lines(f.read())
    finally:
        os.unlink(name)
    if any(re.match(r"(<{7}|>{7})( |\r?\n|$)", l) for l in lines):
        print("  Conflict markers are still in your text - choose again.")
        return None
    return lines


class Interactive:
    HELP = ("  a = keep A   b = keep B   ab = A then B   ba = B then A   o = original (before both)\n"
            "  e = edit it yourself   s = skip (leave markers)   all-a / all-b = same choice for the rest\n"
            "  quit = stop asking, leave the rest as markers")

    def __init__(self, rerere=None):
        self.sticky, self.quit, self.rerere = None, False, rerere

    def __call__(self, c, ctx):
        if self.quit:
            return None
        if self.sticky:
            return c.a if self.sticky == "a" else c.b

        def done(res):
            if self.rerere is not None and res is not None:
                self.rerere.record(c, res)
            return res

        print("\n" + color(f"== {ctx.path}  -  conflict {ctx.idx} of {ctx.total} ==", "1;36"))
        for l in ctx.before:
            print("   " + l.rstrip("\r\n"))
        if c.base:
            print(color("  ORIGINAL (before both changes):", "33"))
            for l in c.base:
                print(color("   | " + l.rstrip("\r\n"), "33"))
        print(color(f"  [a] {c.la}:", "32") + ("   (deleted these lines)" if not c.a else ""))
        for l in c.a:
            print(color("   + " + l.rstrip("\r\n"), "32"))
        print(color(f"  [b] {c.lb}:", "31") + ("   (deleted these lines)" if not c.b else ""))
        for l in c.b:
            print(color("   + " + l.rstrip("\r\n"), "31"))
        valid = {"a", "b", "ab", "ba", "e", "s", "all-a", "all-b", "quit", "?"} | ({"o"} if c.base is not None else set())
        while True:
            ans = prompt_choice("  Your choice [a/b/ab/ba/e/s/?]: ", valid)
            if ans is None or ans == "quit":
                self.quit = True
                return None
            if ans == "?":
                print(self.HELP)
            elif ans == "a":
                return done(c.a)
            elif ans == "b":
                return done(c.b)
            elif ans == "ab":
                return done(fixnl(c.a, ctx.eol) + c.b)
            elif ans == "ba":
                return done(fixnl(c.b, ctx.eol) + c.a)
            elif ans == "o":
                return done(c.base)
            elif ans == "s":
                return None
            elif ans in ("all-a", "all-b"):
                self.sticky = ans[-1]
                return c.a if self.sticky == "a" else c.b
            elif ans == "e":
                res = _edit(c, ctx)
                if res is not None:
                    return done(res)


def chain(*fs):
    def r(c, ctx):
        for f in fs:
            x = f(c, ctx)
            if x is not None:
                return x
        return None
    return r


def effective_mode(mode, can_prompt):
    if mode == "guided" and not can_prompt:
        return "smart"
    if mode == "interactive" and not can_prompt:
        return "markers"
    return mode


def make_resolver(mode, rerere=None):
    rr = [rerere.lookup] if rerere is not None else []
    inter = Interactive(rerere)
    return {"markers": None, "ours": r_ours, "theirs": r_theirs, "union": r_union,
            "smart": chain(*rr, r_smart), "interactive": chain(*rr, inter),
            "guided": chain(*rr, r_smart, inter)}[mode]


def default_rerere_path():
    home = os.environ.get("DIRMERGE_HOME") or os.path.join(os.path.expanduser("~"), ".dirmerge")
    return os.path.join(home, "rerere.json")


def run_resolve(args):
    target = args.resolve
    if not os.path.exists(target):
        print(f"Not found: {target}")
        return 2
    mode = effective_mode(args.on_conflict or "guided", stdin_is_tty())
    if mode == "markers":
        print("Nothing to do: no way to ask you (not a terminal). Use --on-conflict ours/theirs/smart.")
        return 2
    rerere = Rerere(args.rerere_file or default_rerere_path()) if args.rerere else None
    resolver = make_resolver(mode, rerere)
    if os.path.isfile(target):
        files, root = [target], os.path.dirname(target)
    else:
        root = target
        files = [os.path.join(dp, f) for dp, dn, fn in os.walk(target)
                 if not any(x in (".git", "node_modules") for x in dp.split(os.sep)) for f in fn]
    left = fixed = touched = 0
    for path in sorted(files):
        data = read_bytes(path)
        if is_binary(data) or b"<<<<<<<" not in data:
            continue
        text = decode(data)
        segs = parse_markers(split_lines(text))
        if not any(isinstance(s, Conflict) for s in segs):
            continue
        lines, remaining, resolved = render(segs, resolver, dominant_eol(text), os.path.relpath(path, root))
        if resolved:
            if not args.no_backup:
                shutil.copy2(path, path + ".orig")
            with open(path, "wb") as f:
                f.write(encode("".join(lines)))
        touched += 1
        left += remaining
        fixed += resolved
    if rerere:
        rerere.save()
    print(f"\n{touched} file(s) had conflicts | {fixed} resolved | {left} still unresolved"
          + (f" | {rerere.reused} from memory" if rerere and rerere.reused else ""))
    return 1 if left else 0


# --------------------------------------------------------------------------- #
# Stat, rules, config, reports
# --------------------------------------------------------------------------- #
def delta(old, new):
    if old == new:
        return None
    if old is None:
        return "new" + ("" if is_binary(new) else f" +{len(split_lines(decode(new)))}")
    if new is None:
        return "deleted"
    if is_binary(old) or is_binary(new):
        return "binary changed"
    add = rem = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, split_lines(decode(old)), split_lines(decode(new)),
                                               autojunk=False).get_opcodes():
        if tag != "equal":
            rem += i2 - i1
            add += j2 - j1
    return f"+{add} -{rem}"


def parse_rules(items):
    rules = []
    for it in items or []:
        if "=" not in it:
            print(f"Bad --rule '{it}' (use PATTERN=ACTION)")
            sys.exit(2)
        pat, act = it.rsplit("=", 1)
        if act not in RULE_ACTIONS:
            print(f"Bad rule action '{act}'. Choose from: {', '.join(sorted(RULE_ACTIONS))}")
            sys.exit(2)
        rules.append((pat.strip(), act))
    return rules


def load_config(args):
    path = args.config or (".dirmerge.json" if os.path.isfile(".dirmerge.json") else None)
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print(f"Could not read config {path}: {e}")
        sys.exit(2)
    args.rule = list(args.rule or []) + [f"{k}={v}" for k, v in cfg.get("rules", {}).items()]
    args.ignore = list(args.ignore or []) + list(cfg.get("ignore", []))
    if not args.on_conflict and cfg.get("on_conflict") in MODES:
        args.on_conflict = cfg["on_conflict"]


def _diff_text(old, new, la, lb, cap=300):
    if old is None or new is None or old == new or is_binary(old) or is_binary(new):
        return ""
    lines = list(difflib.unified_diff(split_lines(decode(old)), split_lines(decode(new)), la, lb, n=2))
    if len(lines) > cap:
        lines = lines[:cap] + ["... (truncated)\n"]
    return "".join(lines)


def _colorize_diff(text):
    out = []
    for l in text.splitlines():
        cls = "add" if l.startswith("+") and not l.startswith("+++") else \
            "del" if l.startswith("-") and not l.startswith("---") else "hdr" if l.startswith(("@@", "---", "+++")) else ""
        out.append(f'<span class="{cls}">{html.escape(l)}</span>')
    return "\n".join(out)


def write_report(path, results, info):
    ext = os.path.splitext(path)[1].lower()
    summary = (f"{info['files']} files | {info['solved']} conflicts solved | "
               f"{info['unresolved']} unresolved | ancestor: {info['ancestor']}")
    if ext == ".json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"summary": info, "files": [r.to_dict() for r in results if r.status != "identical"]}, f, indent=2)
    elif ext in (".md", ".markdown"):
        rows = ["# Merge report", "", summary, "", "| File | Result | Notes |", "|---|---|---|"]
        for r in results:
            if r.status != "identical":
                rows.append(f"| `{r.path}` | {r.status} | {'; '.join(r.notes)} |")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
    else:
        body = []
        for r in results:
            if r.status == "identical":
                continue
            bad = r.conflicts or r.flagged
            diffs = ""
            if r.out is not None:
                for label, old in ((f"{info['la']} -> merged", r.da), (f"{info['lb']} -> merged", r.db)):
                    d = _diff_text(old, r.out, label.split(" -> ")[0], "merged")
                    if d:
                        diffs += f"<h4>{html.escape(label)}</h4><pre>{_colorize_diff(d)}</pre>"
            notes = "".join(f"<div class='note'>{html.escape(n)}</div>" for n in r.notes)
            body.append(f"<details><summary><code>{html.escape(r.path)}</code> "
                        f"<span class='{'bad' if bad else 'ok'}'>{html.escape(r.status)}</span></summary>{notes}{diffs}</details>")
        page = f"""<!doctype html><meta charset="utf-8"><title>Merge report</title>
<style>body{{font:14px system-ui;margin:2rem auto;max-width:960px;padding:0 1rem}}
details{{border:1px solid #8884;border-radius:6px;margin:6px 0;padding:6px 10px}}summary{{cursor:pointer}}
pre{{overflow:auto;background:#8881;padding:8px;border-radius:4px;font-size:12px}}
.ok{{color:#1a7f37}}.bad{{color:#cf222e;font-weight:600}}.note{{color:#9a6700;margin:4px 0}}
.add{{color:#1a7f37}}.del{{color:#cf222e}}.hdr{{color:#8250df}}</style>
<h1>Merge report</h1><p>{html.escape(summary)}</p>{''.join(body)}"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)


# --------------------------------------------------------------------------- #
# The merge itself
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    path: str
    status: str = ""
    conflicts: int = 0
    resolved: int = 0
    notes: list = field(default_factory=list)
    flagged: bool = False
    da: object = field(default=None, repr=False)
    db: object = field(default=None, repr=False)
    out: object = field(default=None, repr=False)

    def to_dict(self):
        return {k: getattr(self, k) for k in ("path", "status", "conflicts", "resolved", "notes", "flagged")}


def _run_merge(args, tmpdir):
    global CONFLICT_STYLE
    CONFLICT_STYLE = args.conflict_style
    for d in filter(None, [args.dir_a, args.dir_b, args.base]):
        if not os.path.isdir(d):
            print(f"Not a directory: {d}")
            return 2
    load_config(args)
    rules = parse_rules(args.rule)
    ignore = DEFAULT_IGNORE + list(args.ignore or [])
    use_git = not args.no_gitignore
    A, sa, _ = collect(args.dir_a, ignore, use_git)
    B, sb, _ = collect(args.dir_b, ignore, use_git)

    base_dir, ancestor = args.base, "none (two-way)"
    if base_dir:
        ancestor = args.base_label or f"folder {base_dir}"
        print(f"Common ancestor: {ancestor}")
    elif not args.no_git:
        base_dir, msg = find_git_base(args.dir_a, args.dir_b, tmpdir)
        ancestor = msg if base_dir else "none (two-way)"
        print(f"Common ancestor: {msg}" if base_dir else f"No git ancestor ({msg}) -> cautious two-way merge")
    BASE, sbase, _ = collect(base_dir, ignore, False) if base_dir else ({}, 0, False)
    skipped = sa + sb + sbase

    if args.only:
        keep = lambda p: any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat) for pat in args.only)
        A, B, BASE = ({p: v for p, v in d.items() if keep(p)} for d in (A, B, BASE))

    na, nb = os.path.basename(os.path.abspath(args.dir_a)), os.path.basename(os.path.abspath(args.dir_b))
    la, lb = (na, nb) if na != nb else (f"{na} (A)", f"{nb} (B)")

    cache = {}

    def rd(p):
        if p is None:
            return None
        if p not in cache:
            cache[p] = read_bytes(p)
        return cache[p]

    # ---- build the work list (path -> versions), applying rename detection ---- #
    entries = {p: {"a": A.get(p), "b": B.get(p), "base": BASE.get(p), "notes": [], "rename_conflict": False}
               for p in set(A) | set(B) | set(BASE)}
    if BASE and not args.no_renames:
        ra = detect_renames(BASE, A, rd, args.rename_threshold)
        rb = detect_renames(BASE, B, rd, args.rename_threshold)
        ra = {o: n for o, n in ra.items() if not (n in B and rb.get(o) != n)}
        rb = {o: n for o, n in rb.items() if not (n in A and ra.get(o) != n)}
        for old in sorted(set(ra) | set(rb)):
            na_, nb_ = ra.get(old), rb.get(old)
            if na_ and nb_:
                target, a_p, b_p = na_, A[na_], B[nb_]
                if na_ == nb_:
                    note, rc = f"renamed {old} -> {na_} on both sides", False
                else:
                    note, rc = f"renamed differently (A: {na_}, B: {nb_}); used A's name", True
                    entries.pop(nb_, None)
            elif na_:
                target, a_p, b_p = na_, A[na_], B.get(old)
                note, rc = f"renamed in A: {old} -> {na_}" + ("" if b_p else " (B deleted it)"), False
            else:
                target, a_p, b_p = nb_, A.get(old), B[nb_]
                note, rc = f"renamed in B: {old} -> {nb_}" + ("" if a_p else " (A deleted it)"), False
            entries.pop(old, None)
            e = entries.setdefault(target, {"notes": []})
            e.update(a=a_p, b=b_p, base=BASE[old], rename_conflict=rc)
            e["notes"] = e.get("notes", []) + [note]

    if args.diff:
        for p in sorted(entries):
            e = entries[p]
            da, db = rd(e["a"]), rd(e["b"])
            if da is not None and db is not None:
                if da == db:
                    continue
                if is_binary(da) or is_binary(db):
                    print(f"Binary files differ: {p}")
                else:
                    sys.stdout.writelines(difflib.unified_diff(
                        split_lines(decode(da)), split_lines(decode(db)), f"{la}/{p}", f"{lb}/{p}"))
            elif da is not None:
                print(f"Only in {la}: {p}")
            elif db is not None:
                print(f"Only in {lb}: {p}")
        return 0

    if args.stat:
        both = 0
        print(f"\n  {'file'.ljust(40)} {('A: ' + la)[:18].ljust(18)} {('B: ' + lb)[:18].ljust(18)}")
        for p in sorted(entries):
            e = entries[p]
            da, db, dbase = rd(e["a"]), rd(e["b"]), rd(e["base"])
            if da == db:
                continue
            if BASE:
                x, y = delta(dbase, da), delta(dbase, db)
                flag = color("  <-- both sides changed it", "33") if x and y else ""
                both += bool(x and y)
                print(f"  {p[:40].ljust(40)} {(x or '-').ljust(18)} {(y or '-').ljust(18)}{flag}")
            else:
                print(f"  {p[:40].ljust(40)} {(delta(da, db) or '-')}")
            for n in e["notes"]:
                print(f"      ({n})")
        print(f"\n  {both} file(s) changed on both sides - those are where conflicts can happen." if BASE else
              "\n  (no ancestor: showing A -> B differences only)")
        return 0

    if args.in_place:
        if args.output:
            print("Use either -o or --in-place, not both.")
            return 2
        args.output, args.force = args.dir_a, True
    if not args.output and not args.dry_run:
        print("Please give an output folder with -o (or use --dry-run / --in-place).")
        return 2
    if args.output and not args.dry_run:
        out_real = os.path.realpath(args.output)
        if not args.in_place and out_real in {os.path.realpath(x) for x in filter(None, [args.dir_a, args.dir_b, args.base])} \
                and not args.force:
            print("The output folder is one of the inputs. Choose a new folder, or use --in-place.")
            return 2
        if os.path.isdir(args.output) and os.listdir(args.output) and not args.force:
            print(f"Output folder '{args.output}' already exists and is not empty. Choose another or add --force.")
            return 2

    default_mode = args.on_conflict or ("guided" if stdin_is_tty() else "markers")
    rerere = Rerere(args.rerere_file or default_rerere_path()) if args.rerere else None
    resolvers, warned = {}, [False]

    def get_res(mode_name):
        can = mode_name in ("guided", "interactive") and stdin_is_tty() and not args.dry_run
        em = effective_mode(mode_name, can)
        if em != mode_name and not args.dry_run and not warned[0]:
            print(f"(Not a terminal, so '{mode_name}' is running as '{em}'.)")
            warned[0] = True
        if em not in resolvers:
            resolvers[em] = make_resolver(em, rerere)
        return em, resolvers[em]

    def rule_for(p):
        for pat, act in rules:
            if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat):
                return act
        return None

    results, writes, deletes = [], {}, []

    def put(p, data, src):
        writes[p] = (data, src)

    def handle_binary(p, pa, pb, da, db, res, em):
        choice = args.binary or {"ours": "a", "theirs": "b"}.get(em)
        if not choice and em in ("guided", "interactive"):
            print(color(f"\n== {p} is a binary file that differs ==", "1;36"))
            ans = prompt_choice(f"  Keep [a] {la}, [b] {lb}, or [k] keep A and save B next to it? ", {"a", "b", "k"})
            choice = {"a": "a", "b": "b", "k": "conflict"}.get(ans)
        if choice == "a":
            put(p, da, pa); res.status = "binary: took A"
        elif choice == "b":
            put(p, db, pb); res.status = "binary: took B"
        elif choice == "newer":
            newer_a = os.path.getmtime(pa) >= os.path.getmtime(pb)
            put(p, da if newer_a else db, pa if newer_a else pb)
            res.status = "binary: took newer (" + ("A" if newer_a else "B") + ")"
        else:
            put(p, da, pa)
            put(p + ".B-version", db, pb)
            res.status, res.conflicts = "CONFLICT (binary)", res.conflicts + 1
            res.notes.append(f"kept A; B saved as {p}.B-version")

    def do_merge(p, pa, pb, da, db, dbase, res, mode_name):
        em, resolver = get_res(mode_name)
        if is_binary(da) or is_binary(db) or (dbase is not None and is_binary(dbase)):
            return handle_binary(p, pa, pb, da, db, res, em)
        for side, data in (("A", da), ("B", db)):
            if re.search(rb"^<<<<<<< ", data, re.M):
                res.notes.append(f"input {side} already contains conflict markers")
        segs, eol, notes = text_segments(da, db, dbase, la, lb, args.engine)
        lines, remaining, resolved = render(segs, resolver, eol, p)
        data = encode("".join(lines))
        inputs_ok = not syntax_error(p, da) and not syntax_error(p, db)
        if em == "smart" and resolved and not remaining and not args.no_validate \
                and syntax_error(p, data) and inputs_ok:
            lines, remaining, _ = render(segs, None, eol, p)
            data, resolved = encode("".join(lines)), 0
            notes.append("auto-solve would have broken the syntax, so conflicts were left for you")
        put(p, data, pa)
        res.conflicts += remaining
        res.resolved += resolved
        res.notes += notes
        if remaining:
            res.status = f"CONFLICT ({remaining} unresolved" + (f", {resolved} solved)" if resolved else ")")
        elif resolved:
            res.status = f"merged, {resolved} conflict(s) solved"
        else:
            res.status = "merged cleanly"
        if not remaining and not args.no_validate:
            err = syntax_error(p, data)
            if err and inputs_ok:
                res.flagged, res.status = True, res.status + " but INVALID SYNTAX"
                res.notes.append(err)

    def modify_delete(p, deleted_in, keep_data, keep_src, res):
        em = get_res(default_mode)[0]
        choice = None
        if em in ("ours", "theirs"):
            choice = "delete" if (em == "ours") == (deleted_in == "A") else "keep"
        elif em in ("guided", "interactive"):
            ans = prompt_choice(f"\n{p}: deleted in {deleted_in} but modified in the other.\n  [k]eep the modified file or [d]elete it? ", {"k", "d"})
            choice = {"k": "keep", "d": "delete"}.get(ans)
        if choice == "delete":
            deletes.append(p); res.status = "deleted (your choice)"
        elif choice == "keep":
            put(p, keep_data, keep_src); res.status = "kept modified file (your choice)"
        else:
            put(p, keep_data, keep_src)
            res.status, res.conflicts = f"CONFLICT (deleted in {deleted_in}, modified in other)", res.conflicts + 1

    for p in sorted(entries):
        e = entries[p]
        pa, pb, pbase = e["a"], e["b"], e["base"]
        da, db, dbase = rd(pa), rd(pb), rd(pbase)
        res = Result(p, notes=list(e["notes"]))
        act = rule_for(p)
        if act == "skip":
            res.status = "skipped (rule)"
        elif act in ("take-a", "take-b", "newer"):
            if act == "newer" and da is not None and db is not None:
                pick = "a" if os.path.getmtime(pa) >= os.path.getmtime(pb) else "b"
            else:
                pick = "b" if act == "take-b" or (act == "newer" and da is None) else "a"
            data, src = (da, pa) if pick == "a" else (db, pb)
            if data is None:
                deletes.append(p)
            else:
                put(p, data, src)
            res.status = f"rule: took {pick.upper()}"
        elif da is not None and db is not None and da == db:
            put(p, da, pa); res.status = "identical"
        elif dbase is None or not BASE:
            if da is None:
                put(p, db, pb); res.status = "added (B)"
            elif db is None:
                put(p, da, pa); res.status = "added (A)"
            else:
                do_merge(p, pa, pb, da, db, None, res, act if act in MODES else default_mode)
        else:
            if da is None and db is None:
                deletes.append(p); res.status = "deleted (both)"
            elif da is None:
                if db == dbase:
                    deletes.append(p); res.status = "deleted (A)"
                else:
                    modify_delete(p, "A", db, pb, res)
            elif db is None:
                if da == dbase:
                    deletes.append(p); res.status = "deleted (B)"
                else:
                    modify_delete(p, "B", da, pa, res)
            elif da == dbase:
                put(p, db, pb); res.status = "took B (A unchanged)"
            elif db == dbase:
                put(p, da, pa); res.status = "took A (B unchanged)"
            else:
                do_merge(p, pa, pb, da, db, dbase, res, act if act in MODES else default_mode)
        if e.get("rename_conflict"):
            res.conflicts += 1
            res.status += " [RENAME CONFLICT]"
        if args.report:
            res.da, res.db, res.out = da, db, writes.get(p, (None, None))[0]
        results.append(res)

    # ---- write output ---------------------------------------------------- #
    if args.output and not args.dry_run:
        if args.in_place and not args.no_backup:
            backup = os.path.realpath(args.dir_a) + ".dirmerge-backup-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.copytree(args.dir_a, backup, ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"))
            print(f"Backup of {args.dir_a} saved to: {backup}")
        os.makedirs(args.output, exist_ok=True)
        for p, (data, src) in writes.items():
            dst = os.path.join(args.output, *p.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isfile(dst) and os.path.getsize(dst) == len(data) and read_bytes(dst) == data:
                continue
            with open(dst, "wb") as f:
                f.write(data)
            try:
                shutil.copymode(src, dst)
            except OSError:
                pass
        for p in deletes:
            stale = os.path.join(args.output, *p.split("/"))
            if os.path.isfile(stale):
                os.remove(stale)
    if rerere and not args.dry_run:
        rerere.save()

    n_conf = sum(r.conflicts for r in results)
    n_fix = sum(r.resolved for r in results)
    n_flag = sum(r.flagged for r in results)
    if args.report:
        write_report(args.report, results, {"files": len(results), "solved": n_fix, "unresolved": n_conf,
                                            "ancestor": ancestor, "la": la, "lb": lb})
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        shown = [r for r in results if r.status != "identical"]
        width = max((len(r.path) for r in shown), default=10)
        print()
        for r in shown:
            tag = color(r.status, "1;31" if r.conflicts or r.flagged else "32" if "merged" in r.status else "0")
            print(f"  {r.path.ljust(width)}  {tag}")
            for note in r.notes:
                print(f"  {' ' * width}    - {note}")
        print(f"\n{len(results)} files | {n_fix} conflicts solved | {n_conf} unresolved | {n_flag} flagged invalid"
              + (f" | {skipped} symlink(s) skipped" if skipped else "")
              + (f" | {rerere.reused} answered from memory" if rerere and rerere.reused else ""))
        if not BASE:
            print("Note: no common ancestor, so every difference was treated as a conflict. "
                  "Pass --base for a git-exact merge.")
        if args.output and not args.dry_run:
            print(f"Merged project written to: {args.output}")
        if args.report:
            print(f"Report written to: {args.report}")
        if n_conf and not args.dry_run:
            print(f"Finish later with:  python {os.path.basename(sys.argv[0])} --resolve \"{args.output}\"")
    return 1 if (n_conf or n_flag) else 0


def run_merge(args):
    tmp = tempfile.TemporaryDirectory()
    try:
        return _run_merge(args, tmp.name)
    finally:
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Beginner wizard
# --------------------------------------------------------------------------- #
def _ask(prompt, default=""):
    try:
        s = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(130)
    return s or default


def _clean_path(s):
    s = s.strip().strip('"').strip("'")
    if os.name != "nt":
        s = s.replace("\\ ", " ")
    return os.path.abspath(os.path.expanduser(s)) if s else ""


def _ask_dir(prompt):
    while True:
        p = _clean_path(_ask(prompt))
        if p and os.path.isdir(p):
            return p
        print("  That folder doesn't exist. Tip: drag the folder into this window to paste its path.")


def wizard():
    print(color("\n  dirmerge - merge two project folders safely, like git\n", "1;36"))
    print("  I'll ask a few questions. Your original folders are never changed\n"
          "  (unless you choose 'inplace', and then a backup is made first).\n")
    a = _ask_dir("  1) Folder 1 (e.g. your version): ")
    b = _ask_dir("  2) Folder 2 (the other version): ")
    default_out = os.path.join(os.path.dirname(a), f"merged_{os.path.basename(a)}_{os.path.basename(b)}")
    force, inplace, out = False, False, None
    while True:
        raw = _ask(f"  3) Where should the merged result go?\n"
                   f"     [Enter = new folder {default_out}]\n"
                   f"     [type 'inplace' = update Folder 1 itself, automatic backup first]: ")
        if raw.lower() == "inplace":
            inplace = True
            break
        out = _clean_path(raw) if raw else default_out
        if out in (a, b):
            print("  Output can't be one of the inputs (use 'inplace' to update Folder 1).")
            continue
        if os.path.isdir(out) and os.listdir(out):
            if _ask(f"     '{out}' is not empty. Merge into it anyway? [y/N]: ").lower() == "y":
                force = True
                break
            continue
        break

    session = tempfile.TemporaryDirectory()
    try:
        print("\n  Looking for the common ancestor (the version both folders came from)...")
        base, msg = find_git_base(a, b, session.name)
        if base:
            print(color(f"  Found it via git: {msg}", "32"))
            print("  -> I can do a true three-way merge, exactly like git (with rename detection).")
        else:
            print(f"  No git ancestor ({msg}).")
            print("  If you still have the ORIGINAL folder both versions came from, give it for a perfect merge.")
            print("  Otherwise press Enter: every difference will be shown to you as a conflict (safe).")
            while True:
                raw = _ask("  Original folder [Enter = skip]: ")
                if not raw:
                    break
                cand = _clean_path(raw)
                if os.path.isdir(cand):
                    base = cand
                    break
                print("  That folder doesn't exist.")

        print("\n  How should conflicts (both sides changed the same lines) be handled?")
        print("    1) Guided (recommended): auto-solve the safe ones, ask me about the rest")
        print("    2) Ask me about every conflict")
        print("    3) Don't solve - leave git-style <<<<<<< markers")
        print("    4) Always keep Folder 1's version")
        print("    5) Always keep Folder 2's version")
        mode = None
        while mode is None:
            mode = {"1": "guided", "2": "interactive", "3": "markers", "4": "ours", "5": "theirs"}.get(
                _ask("  Choose 1-5 [1]: ", "1"))

        parser = build_parser()
        argv = [a, b, "--on-conflict", mode]
        argv += ["--in-place"] if inplace else ["-o", out]
        argv += (["--base", base, "--base-label", msg or "folder you chose"] if base else ["--no-git"])
        if force:
            argv.append("--force")
        print("\n  PREVIEW (nothing is written yet)")
        run_merge(parser.parse_args(argv + ["--dry-run"]))
        if _ask("\n  Go ahead? [Y/n]: ", "y").lower() not in ("y", "yes"):
            print("  Cancelled. Nothing was written.")
            return 0
        print()
        code = run_merge(parser.parse_args(argv))
        print(color("\n  Done - clean merge!", "1;32") if code == 0 else
              color("\n  Merge finished, but something needs attention (see above).", "1;33"))
        return code
    finally:
        session.cleanup()


# --------------------------------------------------------------------------- #
def build_parser():
    ap = argparse.ArgumentParser(description="Merge two project folders like git. Run with no arguments for a guided wizard.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("THE \"PERFECT MERGE\"")[0])
    ap.add_argument("dir_a", nargs="?", help="first project (\"A\")")
    ap.add_argument("dir_b", nargs="?", help="second project (\"B\")")
    ap.add_argument("-o", "--output", help="folder to write the merged project into")
    ap.add_argument("--in-place", action="store_true", help="write the result into DIR_A (a backup is made first)")
    ap.add_argument("--base", help="common-ancestor folder (otherwise auto-detected through git)")
    ap.add_argument("--base-label", help=argparse.SUPPRESS)
    ap.add_argument("--on-conflict", choices=MODES,
                    help="guided = auto-solve safe ones + ask the rest (default in a terminal); "
                         "interactive = ask for every conflict; smart = auto-solve safe ones only; "
                         "markers = leave <<<<<<< markers (default when not a terminal); "
                         "ours/theirs = always A/B; union = keep both sides of pure insertions")
    ap.add_argument("--rule", action="append", metavar="PATTERN=ACTION",
                    help="per-file rule, e.g. '*.lock=take-b' (take-a/take-b/newer/skip or a conflict mode); repeatable")
    ap.add_argument("--config", help="JSON file with rules/ignore/on_conflict (default: ./.dirmerge.json if present)")
    ap.add_argument("--only", nargs="*", help="merge only files matching these patterns")
    ap.add_argument("--ignore", nargs="*", default=[], help="extra glob patterns to ignore")
    ap.add_argument("--no-renames", action="store_true", help="turn off rename detection")
    ap.add_argument("--rename-threshold", type=float, default=0.6, help="similarity (0-1) to call a file renamed (default 0.6)")
    ap.add_argument("--engine", choices=["auto", "builtin", "git"], default="auto",
                    help="text merge engine; auto = built-in, git's own for very large files")
    ap.add_argument("--conflict-style", choices=["merge", "diff3"], default="merge",
                    help="diff3 also shows the original text inside conflict markers")
    ap.add_argument("--rerere", action="store_true", help="remember your conflict answers and reuse them next time")
    ap.add_argument("--rerere-file", help="where to keep remembered answers (default ~/.dirmerge/rerere.json)")
    ap.add_argument("--binary", choices=["a", "b", "newer"], help="how to resolve differing binary files")
    ap.add_argument("--report", metavar="FILE", help="write a report (.html, .md or .json)")
    ap.add_argument("--stat", action="store_true", help="show who changed what and where overlaps are, then exit")
    ap.add_argument("--resolve", metavar="PATH", help="resolve conflict markers already in a file/folder")
    ap.add_argument("--no-backup", action="store_true", help="skip .orig (--resolve) / folder (--in-place) backups")
    ap.add_argument("--wizard", action="store_true", help="start the beginner wizard")
    ap.add_argument("--no-git", action="store_true", help="don't try to find the ancestor through git")
    ap.add_argument("--no-gitignore", action="store_true", help="don't honour .gitignore in git repos")
    ap.add_argument("--no-validate", action="store_true", help="skip syntax checks")
    ap.add_argument("--force", action="store_true", help="allow writing into a non-empty output folder")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--diff", action="store_true", help="print differences and exit")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.resolve:
        return run_resolve(args)
    if args.wizard or (not args.dir_a and not args.dir_b):
        return wizard()
    if not (args.dir_a and args.dir_b):
        ap.error("please give two folders (or no arguments to start the wizard)")
    return run_merge(args)


if __name__ == "__main__":
    sys.exit(main())
