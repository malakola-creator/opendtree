"""The code behind each decision, read from git, and the few writes dtree makes (branches, worktrees, merges).

Reads are bounded by a timeout and ask git only for commits and branch names that match a strict pattern first, so the server can
call them per request. ``Git(root)`` works on any repository: dtree is not tied to one project.

Merging (ค) follows the probes of git 2.43 made on 2026-11-01:

- Every call runs with ``GIT_OPTIONAL_LOCKS=0``. A plain ``git status`` rewrites the index to refresh stat data, and an observer
  polling it in an agent's worktree made 92 of the agent's 300 git calls fail on ``index.lock``; with optional locks off, none did.
- A preview is ``git merge-tree --write-tree`` of two commits. It writes only unreachable objects, never the index, a working tree
  or a ref. Exit 1 also means "no such ref" (with nothing on stdout), so a conflict is taken only when the first field is an object id.
  Paths come from ``-z`` output: without it, Thai names, tabs and newlines are quoted or split.
- A branch checked out nowhere is merged into by plumbing: ``merge-tree``, then ``commit-tree`` with a ``stripspace``d message, then
  ``update-ref`` with the old value (a compare-and-swap; on a race it starts over). That makes the same commit as
  ``git merge --no-ff -m``, but no hook runs.
- ``update-ref`` moves a branch even where it is checked out, which would leave that checkout stale, so where a branch is checked out
  is read first. A branch checked out in a worktree is merged only there, and only when nothing there can be lost: no tracked change,
  no merge or rebase in progress, and nothing git does not track where the merge writes — an untracked or ignored file at a path it
  adds, or a file or symbolic link where it makes a directory (git overwrites an ignored file silently, and replaces a link by the
  directory).
- A ``git merge`` that did not finish is never reported as having changed nothing: only exit 2 is git's refusal before it touched
  anything (a refused ``--ff-only`` exits 1, so a fast-forward is judged by where HEAD is). A merge left in progress by us (MERGE_HEAD
  is the commit merged: a conflict, a hook, a commit signature that failed, a stale ``index.lock``) is aborted. When that fails too, or
  git stopped otherwise (killed at the timeout, a write that failed), what ``git status`` shows is reported with how to put it back
  (``git merge --abort`` or ``git reset --merge``, after removing a stale ``index.lock``).
- How a merge ended is read from the commits, not from HEAD alone: a ``post-merge`` hook that commits moves HEAD past the merge commit,
  which is then found on HEAD's first-parent line (a merge made, with a warning). With ``rerere.autoupdate`` in the person's config, git
  stages a resolution it recorded earlier and stops (exit 1, MERGE_HEAD) with no path unmerged: still a conflict in progress for an
  update. A stale ``index.lock`` ends the same way with nothing written (probe 2026-11-02), so that one is not.
"""
from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SHA = re.compile(r"^[0-9a-f]{7,40}$")
#: a full object id: SHA-1, or SHA-256 (untested by the probe)
OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
BRANCH = re.compile(r"^(dec|alt)/[A-Za-z0-9_.-]{1,80}$")
TIMEOUT_S = 10.0
MAX_FILES = 200
#: how many times a plumbing merge starts over when the branch it moves moved meanwhile
RETRIES = 3
METHODS = ("auto", "plumbing", "worktree")
#: how long a ``git merge`` in a worktree may take (a person's checkout runs its hooks); then git is killed and what it left is read
MERGE_TIMEOUT_S = 600.0
#: what a parent git process (a hook that ran dtree) sets for its own repository. dtree always names the repository with -C
_LOCAL_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
              "GIT_PREFIX", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE",
              "GIT_INTERNAL_SUPER_PREFIX")
_MARKER = re.compile(r"^(.*):(\d+): leftover conflict marker$")
_DONE = {"rebasing": "rebased", "bisecting": "bisected"}


def _numstat(text: str) -> List[Dict[str, Any]]:
    files = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            add, rem, path = parts
            files.append({"path": path, "add": int(add) if add.isdigit() else None, "del": int(rem) if rem.isdigit() else None})
    return files[:MAX_FILES]


def shown(path: str) -> str:
    """A path as text that JSON and SQLite take: bytes that are not UTF-8 (kept as surrogates by ``raw`` calls) become U+FFFD."""
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


#: the most dtree reads of one of git's files (``read_bytes``)
READ_MAX = 1 << 20


def read_bytes(path: Any, follow: bool = True, limit: int = READ_MAX) -> bytes:
    """What the regular file ``path`` holds, at most ``limit`` bytes. Anything else there — a FIFO, a socket, a device, a directory, a
    larger file, and with ``follow`` False a symbolic link — is OSError, never read: a job can leave one where dtree reads (its shadow
    repository, a worktree's ``.git``, an admin directory's MERGE_HEAD, a file a chat tool reads), and reading a FIFO waits for a writer
    for ever, which held the runner after a job and every readiness poll (review of round 3)."""
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | (0 if follow else getattr(os, "O_NOFOLLOW", 0)))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        chunks: List[bytes] = []
        size = 0
        while size <= limit:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > limit:
        raise OSError(errno.EFBIG, "larger than %d bytes" % limit, str(path))
    return b"".join(chunks)


def read_file(path: Any, follow: bool = True, limit: int = READ_MAX) -> str:
    """``read_bytes`` as text (bytes that are not UTF-8 kept as they are, ``surrogateescape``)."""
    return read_bytes(path, follow, limit).decode("utf-8", errors="surrogateescape")


def _read(path: Path) -> str:
    try:
        return read_file(path).strip()
    except OSError:
        return ""


def _said(*parts: Any) -> str:
    """The end of a git command's output, on one line (a TimeoutExpired carries what came before the kill, as bytes)."""
    text = " ".join(p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p) for p in parts if p)
    return " ".join(text.split())[-600:]


def _porcelain(text: str) -> List[str]:
    """``status --porcelain -z`` as "XY path" entries; a rename's old name (the field after it) is dropped."""
    out, fields = [], iter(text.split("\0"))
    for f in fields:
        if len(f) > 3:
            out.append(f)
            if "R" in f[:2] or "C" in f[:2]:
                next(fields, None)
    return out


def _in_the_way(wt: Any, way: Optional[List[str]]) -> str:
    """The refusal for ``Git.in_the_way``'s answer."""
    if way is None:
        return "the paths the merge writes could not be listed in %s" % wt
    return "the merge would overwrite what git does not track in %s: %s (move it away first)" % (
        wt, ", ".join(shown(p) for p in way[:10]) + (" …" if len(way) > 10 else ""))


def _busy_with(admin: Optional[str], branch: str) -> Optional[str]:
    """Whether the worktree whose git directory is ``admin`` is rebasing or bisecting ``branch``: git's own wt_status_check_rebase and
    wt_status_check_bisect, which make it refuse to check the branch out elsewhere."""
    if not admin:
        return None
    a, names = Path(admin), (branch, "refs/heads/" + branch)
    if (a / "rebase-apply").is_dir():
        if not (a / "rebase-apply" / "applying").exists() and _read(a / "rebase-apply" / "head-name") in names:   # "applying": git am
            return "rebasing"
    elif (a / "rebase-merge").is_dir() and _read(a / "rebase-merge" / "head-name") in names:
        return "rebasing"
    if (a / "BISECT_LOG").exists() and _read(a / "BISECT_START") in names:
        return "bisecting"
    return None


#: for every git command dtree runs: no fsmonitor command, never into submodules. A worktree an agent used can hold a nested repository
#: of its making whose own config git would honour (a clean/smudge filter is a command); git status looks into submodules unless told
#: not to, so ``status`` always gets --ignore-submodules=all (_ignoring_submodules). Hooks stay on here: a person's own checkout keeps its
#: hooks; commands in an agent's worktree add jobs.SAFE_GIT.
HARDEN = ("-c", "core.fsmonitor=false", "-c", "submodule.recurse=false")


def _ignoring_submodules(args: List[str]) -> List[str]:
    """``args`` with --ignore-submodules=all after ``status`` (the subcommand, past any ``-c k=v`` given before it)."""
    i = 0
    while i < len(args) and args[i] in ("-c", "-C"):
        i += 2
    while i < len(args) and args[i].startswith("--") and args[i] != "--":
        i += 1                                                       # global options such as --literal-pathspecs
    if i < len(args) and args[i] == "status" and not any(a.startswith("--ignore-submodules") for a in args[i + 1:]):
        return args[:i + 1] + ["--ignore-submodules=all"] + args[i + 1:]
    return args


class Git:
    def __init__(self, root: Any) -> None:
        self.root = Path(root)

    def run(self, *args: str, cwd: Optional[Any] = None, timeout: float = 120.0, stdin: Optional[str] = None,
            raw: bool = False) -> subprocess.CompletedProcess:
        """git in ``cwd`` (the root by default), never taking an optional lock and never using a repository a parent git set in the
        environment. ``raw``: bytes that are not UTF-8 come back as surrogates, so a path can be given to git again unchanged. git never
        reads dtree's own stdin (``dtree mcp`` speaks over it)."""
        env = {k: v for k, v in os.environ.items() if k not in _LOCAL_ENV}
        env.update(GIT_OPTIONAL_LOCKS="0", GIT_MERGE_AUTOEDIT="no")
        feed: Dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(["git", "-C", str(cwd or self.root)] + list(HARDEN) + _ignoring_submodules(list(args)), capture_output=True,
                              timeout=timeout, env=env, encoding="utf-8", errors="surrogateescape" if raw else "replace", **feed)

    def out(self, *args: str, cwd: Optional[Any] = None, raw: bool = False) -> Optional[str]:
        """git's output, or None when git is missing, this is not a repository or the command fails."""
        try:
            r = self.run(*args, cwd=cwd, timeout=TIMEOUT_S, raw=raw)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    def available(self) -> bool:
        return self.out("rev-parse", "--is-inside-work-tree") is not None

    def first_commit_time(self) -> Optional[float]:
        out = self.out("log", "--reverse", "--format=%ct", "--max-parents=0")
        try:
            return float(out.split()[0]) if out else None
        except (ValueError, IndexError):
            return None

    def current_branch(self) -> str:
        return (self.out("rev-parse", "--abbrev-ref", "HEAD") or "").strip()

    def has_branch(self, name: str) -> bool:
        return self.out("rev-parse", "--verify", "--quiet", "refs/heads/" + name) is not None

    def dirty(self, cwd: Optional[Path] = None) -> bool:
        return bool(self.run("status", "--porcelain", "--untracked-files=no", cwd=cwd).stdout.strip())

    def commit_info(self, sha: str) -> Optional[Dict[str, Any]]:
        """One commit: its subject, time, parents (a merge has two) and the files it changed against its first parent."""
        if not SHA.match(sha or ""):
            return None
        out = self.out("show", "--first-parent", "-m", "--numstat", "--format=%x1e%H%x1f%h%x1f%ct%x1f%P%x1f%s", sha)
        if not out or "\x1e" not in out:
            return None
        head, _, rest = out.split("\x1e", 1)[1].partition("\n")
        full, short, ct, parents, subject = (head.split("\x1f") + [""] * 5)[:5]
        return {"sha": full, "short": short, "time": float(ct or 0), "merge": len(parents.split()) > 1, "subject": subject, "files": _numstat(rest)}

    def branch_info(self, name: str, base: str = "main") -> Dict[str, Any]:
        """A decision's or a trial's branch: whether it exists, its commits not in ``base``, whether it is merged, and the files that
        differ from where it left ``base``."""
        info: Dict[str, Any] = {"branch": name, "exists": False}
        if not BRANCH.match(name or "") or not self.has_branch(name):
            return info
        info["exists"] = True
        log = self.out("log", "--format=%h%x1f%ct%x1f%s", base + ".." + name) or ""
        info["ahead"] = [dict(zip(("short", "time", "subject"), l.split("\x1f"))) for l in log.splitlines() if l]
        info["merged"] = self.out("merge-base", "--is-ancestor", name, base) is not None
        info["files"] = _numstat(self.out("diff", "--numstat", base + "..." + name) or "")
        return info

    def commits_between(self, base: str, branch: str) -> List[Dict[str, str]]:
        """The commits on ``branch`` not in ``base``, oldest first."""
        out = self.out("log", "--reverse", "--format=%H%x1f%s", base + ".." + branch) or ""
        return [dict(zip(("sha", "subject"), l.split("\x1f", 1))) for l in out.splitlines() if l]

    def diff(self, base: str, branch: str, max_bytes: int = 400_000) -> Dict[str, Any]:
        """What ``branch`` changes since it left ``base``: the files with their lines and the unified diff (cut at ``max_bytes``)."""
        if not BRANCH.match(branch or "") or not self.has_branch(branch):
            return {"branch": branch, "base": base, "exists": False, "files": [], "diff": "", "cut": False}
        text = self.out("diff", "--no-color", base + "..." + branch) or ""
        cut = len(text.encode("utf-8")) > max_bytes
        if cut:
            text = text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
        return {"branch": branch, "base": base, "exists": True, "files": _numstat(self.out("diff", "--numstat", base + "..." + branch) or ""),
                "diff": text, "cut": cut}

    # ------------------------------------------------------------------------------------------------------- where things are
    def sha(self, ref: str, cwd: Optional[Any] = None) -> Optional[str]:
        """The full id of the commit ``ref`` names (a branch first, as ``refs/heads/<ref>``, then anything git resolves), or None."""
        ref = str(ref or "")
        if not ref or ref.startswith("-"):
            return None
        for c in ([ref] if ref.startswith("refs/") or SHA.match(ref) else ["refs/heads/" + ref, ref]):
            out = (self.out("rev-parse", "--verify", "--quiet", c + "^{commit}", cwd=cwd) or "").strip()
            if OID.match(out):
                return out
        return None

    def ancestor(self, a: str, b: str, cwd: Optional[Any] = None) -> Optional[bool]:
        """Whether commit ``a`` is in ``b``'s history (``merge-base --is-ancestor``); None when git cannot tell. ``cwd``: a worktree
        whose commits may not be in the root's repository yet (a trial's shadow)."""
        try:
            r = self.run("merge-base", "--is-ancestor", a, b, cwd=cwd, timeout=TIMEOUT_S * 6)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return {0: True, 1: False}.get(r.returncode)

    def common_dir(self) -> Optional[Path]:
        out = (self.out("rev-parse", "--path-format=absolute", "--git-common-dir", raw=True) or "").strip()
        return Path(out) if out else None

    def worktrees(self) -> List[Dict[str, Any]]:
        """Every worktree of the repository, the main one first (``worktree list --porcelain -z``): path, head, branch (None when
        detached), detached, bare, locked, prunable (its directory is gone), and ``admin``, git's own directory for it."""
        out = self.out("worktree", "list", "--porcelain", "-z", raw=True)
        if out is None:
            return []
        items: List[Dict[str, Any]] = []
        cur: Optional[Dict[str, Any]] = None
        for field in out.split("\0"):
            key, _, val = field.partition(" ")
            if key == "worktree":
                cur = {"path": val, "head": None, "branch": None, "detached": False, "bare": False, "locked": False, "prunable": False, "admin": None}
                items.append(cur)
            elif cur is not None and key == "HEAD":
                cur["head"] = val
            elif cur is not None and key == "branch":
                cur["branch"] = val[len("refs/heads/"):] if val.startswith("refs/heads/") else val
            elif cur is not None and key in ("detached", "bare", "locked", "prunable"):
                cur[key] = True
        common = self.common_dir()
        if common is not None and items:
            items[0]["admin"] = str(common)
            linked = {}
            for d in sorted((common / "worktrees").glob("*")):
                gitfile = _read(d / "gitdir")                       # "<worktree>/.git", absolute or (newer git) relative to d
                if gitfile:
                    linked[os.path.normpath(str(d / Path(gitfile).parent))] = str(d)
            for w in items[1:]:
                w["admin"] = linked.get(os.path.normpath(w["path"]))
        return items

    def checked_out(self, branch: str, trees: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """The worktrees (of ``trees``, a ``worktrees()`` listing, or a fresh one) that hold ``branch``, each with ``how``: "checked
        out", or "rebasing" / "bisecting" (still git's "used by worktree", though the listing shows those as detached). Read from git's
        state files: ``git branch -f b b`` would answer too, but it takes the branch's lock and writes it back without an old value, so
        it could undo a merge landing at that moment."""
        out = []
        for w in (self.worktrees() if trees is None else trees):
            if w["bare"]:
                continue
            how = "checked out" if w["branch"] == branch else (_busy_with(w["admin"], branch) if w["detached"] else None)
            if how:
                out.append(dict(w, how=how))
        return out

    def worktree_dir(self, wt: Any) -> Optional[Path]:
        """git's own directory for the worktree whose top is ``wt`` (MERGE_HEAD, the rebase state), or None when ``wt`` is not the top
        of a worktree (git would otherwise answer for a repository above it)."""
        if not wt or not os.path.isdir(str(wt)):
            return None
        out = (self.out("rev-parse", "--show-toplevel", "--absolute-git-dir", cwd=wt, raw=True) or "").splitlines()
        if len(out) != 2 or os.path.realpath(out[0]) != os.path.realpath(str(wt)):
            return None
        return Path(out[1])

    def symbolic_head(self, wt: Any) -> Optional[str]:
        """The branch a worktree is on (``refs/heads/…``), or None when it is detached."""
        out = (self.out("symbolic-ref", "-q", "HEAD", cwd=wt, raw=True) or "").strip()
        return out or None

    def in_progress(self, wt: Any) -> Optional[str]:
        """What a worktree is in the middle of: merge, rebase, cherry-pick, revert or bisect, or None."""
        d = self.worktree_dir(wt)
        if d is None:
            return None
        for name, what in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("REBASE_HEAD", "rebase"),
                           ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
            if (d / name).exists():
                return what
        return None

    def merge_head(self, wt: Any) -> Optional[str]:
        """The commit being merged in worktree ``wt`` (its MERGE_HEAD), or None."""
        d = self.worktree_dir(wt)
        first = _read(d / "MERGE_HEAD").split() if d is not None else []
        return first[0] if first and OID.match(first[0]) else None

    def changes(self, wt: Any) -> Optional[List[str]]:
        """The tracked files with uncommitted changes in worktree ``wt`` (``status --porcelain --untracked-files=no``, no optional lock;
        untracked and ignored files are not seen), or None when git cannot tell."""
        try:
            r = self.run("status", "--porcelain", "-z", "--untracked-files=no", cwd=wt, timeout=TIMEOUT_S * 6, raw=True)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        return [f[3:] for f in _porcelain(r.stdout)]

    def unmerged(self, wt: Any) -> List[str]:
        """The paths of worktree ``wt`` with a conflict not resolved yet, once each (``ls-files -u``, which never writes the index)."""
        out = self.out("ls-files", "-u", "-z", cwd=wt, raw=True) or ""
        paths: List[str] = []
        for rec in out.split("\0"):
            path = rec.partition("\t")[2]
            if path and path not in paths:
                paths.append(path)
        return paths

    # ------------------------------------------------------------------------------------------------------- previews
    def merge_tree(self, ours: str, theirs: str, cwd: Optional[Any] = None) -> Dict[str, Any]:
        """``git merge-tree --write-tree`` of two commits, nothing touched: {state clean|conflicts|error, tree, conflicts, error}.
        Run it where the real merge runs (``cwd``): the merge drivers in .gitattributes come from that worktree's index."""
        try:
            r = self.run("merge-tree", "--write-tree", "-z", "--name-only", "--no-messages", ours, theirs, cwd=cwd, timeout=600, raw=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"state": "error", "tree": None, "conflicts": [], "error": "git merge-tree did not run: %s" % exc}
        fields = r.stdout.split("\0")
        first = fields[0].strip()
        if r.returncode in (0, 1) and OID.match(first):
            return {"state": "clean" if r.returncode == 0 else "conflicts", "tree": first, "conflicts": [f for f in fields[1:] if f], "error": None}
        why = (r.stderr or r.stdout).strip() or "no output"
        return {"state": "error", "tree": None, "conflicts": [], "error": "git merge-tree failed (exit %d): %s" % (r.returncode, why[:500])}

    def merge_preview(self, base: str, branch: str, cwd: Optional[Any] = None) -> Dict[str, Any]:
        """Whether ``branch`` merges into ``base`` (nothing touched): {state clean|conflicts|merged|error, tree, conflicts (paths),
        behind (commits in the base since the branch left it), ahead (the branch's own), base_sha, branch_sha, merge_base, error}.
        ``merged``: the branch has nothing the base lacks (merged, or never committed to); merge-tree alone would call it clean, and a
        plumbing merge would then make an empty merge commit. Drift is ``rev-list --left-right --count``: the merge-base form
        overcounts on criss-cross histories and gives 0 for unrelated ones."""
        out: Dict[str, Any] = {"state": "error", "base": base, "branch": branch, "base_sha": self.sha(base), "branch_sha": self.sha(branch),
                               "merge_base": None, "behind": None, "ahead": None, "tree": None, "conflicts": [], "error": None}
        if not out["base_sha"] or not out["branch_sha"]:
            out["error"] = "no branch %s" % (base if not out["base_sha"] else branch)
            return out
        counts = (self.out("rev-list", "--left-right", "--count", "%s...%s" % (out["base_sha"], out["branch_sha"])) or "").split()
        if len(counts) == 2 and all(c.isdigit() for c in counts):
            out["behind"], out["ahead"] = int(counts[0]), int(counts[1])
        out["merge_base"] = (self.out("merge-base", out["base_sha"], out["branch_sha"]) or "").strip() or None
        if out["merge_base"] is None:
            out["error"] = "%s and %s have no common history (unrelated histories)" % (base, branch)
            return out
        if out["ahead"] == 0:
            out["state"] = "merged"
            return out
        out.update(self.merge_tree(out["base_sha"], out["branch_sha"], cwd=cwd))
        return out

    def in_the_way(self, wt: Any, ours: str, tree: str) -> Optional[List[str]]:
        """What git does not track in worktree ``wt`` that the merge (``tree`` — or a commit — against ``ours``) would write over: a path
        it adds that is there already (untracked or ignored), or a leading directory of one that is a file or a symbolic link there. git
        refuses an untracked file, but an ignored one it overwrites without a word, and an ignored file or link where it needs a directory
        it replaces by the directory. A tracked file or link that the merge itself removes to make that directory is not in the way.
        None when the paths cannot be listed."""
        out = self.out("diff", "--name-status", "--no-renames", "--diff-filter=ADT", "-z", ours, tree, cwd=wt, raw=True)   # the tree may be in a shadow
        if out is None:
            return None
        fields = out.split("\0")
        added, removed = [], set()
        for status, path in zip(fields[0::2], fields[1::2]):
            if status == "A":
                added.append(path)
            elif path:
                removed.add(path)
        top, seen, way = str(wt), {}, []                             # seen: path → "dir", "other" (a file, a link, …) or None (nothing)

        def kind(rel: str) -> Optional[str]:
            if rel not in seen:
                try:
                    seen[rel] = "dir" if stat.S_ISDIR(os.lstat(os.path.join(top, rel)).st_mode) else "other"
                except OSError:
                    seen[rel] = None
            return seen[rel]
        for p in added:
            parts = p.split("/")
            for i in range(1, len(parts) + 1):
                rel = "/".join(parts[:i])
                if i < len(parts) and rel in removed:
                    break                                            # tracked, and replaced by the directory: git's own business
                k = kind(rel)
                if k is None:
                    break                                            # nothing there, so nothing below it either
                if i == len(parts) or k != "dir":
                    if rel not in way:
                        way.append(rel)
                    break
        return way

    def merge_of(self, tip: str, theirs: str, limit: int = 100) -> Optional[str]:
        """The merge commit on ``tip``'s first-parent line (within ``limit`` commits) whose second parent is ``theirs``, or None."""
        for line in (self.out("rev-list", "--first-parent", "--parents", "-n", str(limit), tip) or "").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == theirs:
                return parts[0]
        return None

    def leftover_markers(self, merge: str, tip: str, paths: Sequence[str] = ()) -> List[str]:
        """Conflict markers that the resolution of merge commit ``merge`` left in ``paths`` (all files when empty), as of ``tip``:
        "<path>:<line>" for each marker line that comes from neither parent. ``git diff --check`` against the first parent alone also
        flags a Markdown "=======" underline the base brought in (a false positive in the probe), and it reports whitespace too."""
        parents = (self.out("rev-list", "--parents", "-n", "1", merge) or "").split()[1:3]
        if len(parents) < 2:
            return []
        found: Optional[set] = None
        for parent in parents:
            r = self.run("-c", "core.quotePath=false", "--literal-pathspecs", "diff", "--no-color", "--no-ext-diff", "--check", parent, tip,
                         "--", *paths, timeout=600, raw=True)
            if r.returncode not in (0, 1, 2):                         # 2: problems found; anything else: git could not look
                raise RuntimeError("git diff --check failed (exit %d): %s" % (r.returncode, r.stderr.strip()[:300]))
            marks = set()
            for line in r.stdout.splitlines():
                m = _MARKER.match(line)
                if m:
                    marks.add((m.group(1), int(m.group(2))))
            found = marks if found is None else found & marks
        return ["%s:%d" % pl for pl in sorted(found or ())]

    # ------------------------------------------------------------------------------------------------------- merges
    def _commit_tree(self, tree: str, parents: Sequence[str], message: str) -> Tuple[Optional[str], str]:
        """A commit of ``tree`` with ``parents``; the message goes through ``git stripspace`` as ``git merge -m`` cleans it. It writes
        only an object nothing points to yet, so a failure here changed nothing."""
        try:
            clean = self.run("stripspace", stdin=message)
            if clean.returncode != 0:
                return None, "git stripspace failed: %s" % clean.stderr.strip()
            args = ["commit-tree"]
            for p in parents:
                args += ["-p", p]
            r = self.run(*(args + [tree]), stdin=clean.stdout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, "git commit-tree did not finish: %s" % exc
        new = r.stdout.strip()
        if r.returncode != 0 or not OID.match(new):
            return None, "git commit-tree failed: %s" % (r.stderr or r.stdout).strip()[:500]
        return new, ""

    def _move(self, branch: str, new: str, old: str, why: str) -> str:
        """``update-ref`` of ``branch`` from ``old`` to ``new``, a compare-and-swap: "" when it moved, "race" when the branch had moved
        meanwhile (nothing changed), else git's error. When git did not finish, the branch is read to tell which."""
        try:
            r = self.run("update-ref", "-m", why, "refs/heads/" + branch, new, old)
        except (OSError, subprocess.TimeoutExpired) as exc:
            now = self.sha("refs/heads/" + branch)
            if now == new:
                return ""                                            # it moved before git was stopped
            return "race" if now != old else "git update-ref did not finish: %s" % exc
        if r.returncode == 0:
            return ""
        # at ``new`` already is a race too: the same update made by someone else in the same second (an identical commit)
        return "race" if self.sha("refs/heads/" + branch) != old else (r.stderr or r.stdout).strip() or "git update-ref failed"

    def _git_merge(self, wt: Path, args: Sequence[str], opts: Sequence[str] = ()) -> Dict[str, Any]:
        """``git <opts> merge <args>`` in worktree ``wt``, killed after ``MERGE_TIMEOUT_S``, and what it left there, read rather than
        assumed from its exit: {ran (False: git could not start, so nothing changed), exit (None after a kill), said (the end of its
        output), error (why it did not finish), head and parents (HEAD after it), merge_head (MERGE_HEAD after it)}."""
        m: Dict[str, Any] = {"ran": True, "exit": None, "said": "", "error": None, "head": None, "parents": [], "merge_head": None}
        try:
            r = self.run(*opts, "merge", *args, cwd=wt, timeout=MERGE_TIMEOUT_S)
            m.update(exit=r.returncode, said=_said(r.stdout, r.stderr))
        except subprocess.TimeoutExpired as exc:
            m.update(said=_said(exc.stdout, exc.stderr), error="git merge did not finish in %d s and was killed" % exc.timeout)
        except OSError as exc:
            m.update(ran=False, error="git merge could not run: %s" % exc)
            return m
        m["head"] = self.sha("HEAD", cwd=wt)
        if m["head"]:
            m["parents"] = (self.out("rev-list", "--parents", "-n", "1", m["head"], cwd=wt) or "").split()[1:]
        m["merge_head"] = self.merge_head(wt)
        return m

    def _made_below(self, wt: Path, head: Optional[str], ours: str, theirs: str) -> Optional[str]:
        """The merge commit of ``theirs`` into ``ours`` on ``head``'s first-parent line, when HEAD moved on past it after the merge (a
        post-merge hook that commits), or None."""
        if not head or head == ours:
            return None
        for line in (self.out("rev-list", "--first-parent", "--parents", "-n", "100", head, cwd=wt) or "").splitlines():
            parts = line.split()
            if parts[1:] == [ours, theirs]:
                return parts[0]
            if parts[0] == ours:
                break                                                        # back where the merge started: it is not on this line
        return None

    def _staged(self, wt: Any, opts: Sequence[str] = ()) -> bool:
        """Whether worktree ``wt``'s index differs from its HEAD: what a merge stopped midway really wrote. rerere's autoupdate leaves its
        resolution staged; a merge that could not take index.lock (a stale lock, or another git holding it for a moment) wrote nothing."""
        try:
            return self.run(*opts, "diff-index", "--cached", "--quiet", "HEAD", "--", cwd=wt, timeout=TIMEOUT_S * 6).returncode == 1
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _left_state(self, wt: Path, head: str) -> str:
        """What a git merge that stopped midway left in worktree ``wt`` (HEAD still ``head``), and how the person puts it back: it is
        their checkout, so dtree only says (a hook may still be running there)."""
        try:
            r = self.run("status", "--porcelain", "-z", cwd=wt, timeout=TIMEOUT_S * 6, raw=True)
            entries: Optional[List[str]] = _porcelain(r.stdout) if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            entries = None
        d = self.worktree_dir(wt)
        lock = d / "index.lock" if d is not None else None
        stale = "a stale %s is left: once no git runs there, remove it; then " % lock if lock is not None and lock.exists() else ""
        listed = ("failed" if entries is None else ", ".join(shown(e) for e in entries[:8]) + (" …" if len(entries) > 8 else "")) or "no change"
        if self.merge_head(wt):
            return "a merge is still in progress in %s (git status: %s): %s`git -C %s merge --abort` puts it back at %s" % (
                wt, listed, stale, wt, head[:10])
        if entries == []:
            return "git status shows no change in %s%s" % (wt, ("; " + stale.split(";")[0]) if stale else "")
        return ("%s may be left half-merged (git status: %s): %s`git -C %s reset --merge` puts its tracked files back at %s (what the merge "
                "wrote that git does not track yet stays, to remove by hand)" % (wt, listed, stale, wt, head[:10]))

    def ready_worktree(self, wt: Path, branch: str) -> str:
        """Why a merge into ``branch`` cannot run in worktree ``wt`` now (empty when it can): it is not there or on another branch, an
        operation is in progress, or tracked files have uncommitted changes."""
        if self.worktree_dir(wt) is None:
            return "the checkout of %s at %s is missing" % (branch, wt)
        if self.symbolic_head(wt) != "refs/heads/" + branch:
            return "%s is not on %s" % (wt, branch)
        op = self.in_progress(wt)
        if op:
            return "a %s is in progress in %s: finish it first" % (op, wt)
        changed = self.changes(wt)
        if changed is None:
            return "git status failed in %s" % wt
        if changed:
            return "%s has uncommitted changes (%s%s): commit or stash them first" % (wt, ", ".join(shown(p) for p in changed[:5]),
                                                                                       " …" if len(changed) > 5 else "")
        return ""

    def merge_into(self, base: str, branch: str, message: str, method: str = "auto", scratch: Optional[Any] = None,
                   at: Optional[str] = None) -> Dict[str, Any]:
        """Merge ``branch`` (a name or a commit; ``at``: that commit of it, the one a preview showed) into the local branch ``base``
        with a merge commit (``--no-ff``) and ``message``, switching nobody's checkout:

        - ``base`` checked out nowhere (``method`` auto or plumbing): plumbing, started over when the base moves meanwhile;
        - checked out in one worktree (auto or worktree): ``git merge --no-ff --no-log -m`` there, when it is clean (``ready_worktree``)
          and the merge adds no path that is already there; a conflict there is aborted; a refusal (exit 2) changed nothing; a
          ``post-merge`` hook there may commit after the merge (``sha`` is still the merge commit, and a ``warning`` says so);
        - ``method`` worktree and checked out nowhere: the same in a temporary worktree under ``scratch`` (so hooks run), removed after.

        Refused while the base is being rebased or bisected. Returns {ok, how (plumbing|worktree), sha (the merge commit), base_sha,
        branch_sha, worktree, conflicts, error}."""
        res: Dict[str, Any] = {"ok": False, "how": None, "sha": None, "base": base, "base_sha": None, "branch_sha": self.sha(at or branch),
                               "worktree": None, "conflicts": [], "error": None}
        if method not in METHODS:
            res["error"] = "merge.method is %s, not %r" % (" / ".join(METHODS), method)
        elif not self.sha("refs/heads/" + base):
            res["error"] = "the base %s is not a local branch" % base
        elif not res["branch_sha"]:
            res["error"] = "no branch %s" % branch
        if res["error"]:
            return res
        for _ in range(RETRIES):
            where = [w for w in self.checked_out(base) if not w["prunable"]]
            busy = [w for w in where if w["how"] != "checked out"]
            if busy:
                res["error"] = "%s is being %s in %s: finish that first" % (base, _DONE[busy[0]["how"]], busy[0]["path"])
                return res
            if len(where) > 1:
                res["error"] = "%s is checked out in %d worktrees (%s)" % (base, len(where), ", ".join(w["path"] for w in where))
                return res
            if where:
                if method == "plumbing":
                    res["error"] = ("merge.method is plumbing but %s is checked out at %s: moving the branch would leave that checkout stale"
                                    % (base, where[0]["path"]))
                    return res
                return self._merge_in(Path(where[0]["path"]), base, branch, message, res)
            if method == "worktree":
                return self._merge_in_scratch(base, branch, message, scratch, res)
            if self._merge_plumbing(base, branch, message, res):
                return res
        res["error"] = "%s kept moving while it was merged (%d tries): try again" % (base, RETRIES)
        return res

    def _merge_plumbing(self, base: str, branch: str, message: str, res: Dict[str, Any]) -> bool:
        """One try of a plumbing merge into ``base`` (checked out nowhere). False only when the base moved meanwhile: try again."""
        ours, theirs = self.sha("refs/heads/" + base), res["branch_sha"]
        res.update(how="plumbing", base_sha=ours)
        if self.ancestor(theirs, ours):
            res["error"] = "%s is merged into %s already" % (branch, base)
            return True
        mt = self.merge_tree(ours, theirs)
        if mt["state"] != "clean":
            res.update(conflicts=mt["conflicts"], error=mt["error"] or "%d files conflict with %s" % (len(mt["conflicts"]), base))
            return True
        new, err = self._commit_tree(mt["tree"], [ours, theirs], message)
        if not new:
            res["error"] = err
            return True
        moved = self._move(base, new, ours, "dtree: merge %s into %s" % (branch, base))
        if moved == "race":
            return False
        res.update(ok=not moved, sha=new if not moved else None, error=moved or None)
        return True

    def _merge_in(self, wt: Path, base: str, branch: str, message: str, res: Dict[str, Any], temporary: bool = False) -> Dict[str, Any]:
        """``git merge --no-ff`` in the worktree where ``base`` is checked out, when nothing there can be lost. ``temporary``: a checkout
        dtree made for this merge and removes after, so a failure leaves nothing there to put back."""
        theirs = res["branch_sha"]
        res.update(how="worktree", worktree=str(wt))
        why = self.ready_worktree(wt, base)
        if why:
            res["error"] = why
            return res
        ours = self.sha("HEAD", cwd=wt)
        res["base_sha"] = ours
        if self.ancestor(theirs, ours, cwd=wt):
            res["error"] = "%s is merged into %s already" % (branch, base)
            return res
        mt = self.merge_tree(ours, theirs, cwd=wt)
        if mt["state"] != "clean":
            res.update(conflicts=mt["conflicts"], error=mt["error"] or "%d files conflict with %s" % (len(mt["conflicts"]), base))
            return res
        way = self.in_the_way(wt, ours, mt["tree"])
        if way is None or way:
            res["error"] = _in_the_way(wt, way)
            return res
        m = self._git_merge(wt, ("--no-ff", "--no-log", "-m", message, theirs))
        said = m["said"] or m["error"] or "no output"

        def left() -> str:
            return "the temporary checkout is removed and %s did not move" % base if temporary else self._left_state(wt, ours)
        below = None if not m["ran"] or m["merge_head"] or m["parents"] == [ours, theirs] else self._made_below(wt, m["head"], ours, theirs)
        if not m["ran"]:
            res["error"] = "%s in %s (%s is unchanged)" % (m["error"], wt, base)
        elif m["parents"] == [ours, theirs]:                                 # made — also when git was killed after it (a post-merge hook)
            res.update(ok=True, sha=m["head"])
            if m["exit"] != 0:
                res["warning"] = "%s after the merge commit %s was made" % (m["error"] or "git merge exited %s" % m["exit"], m["head"][:10])
        elif below:                                                          # made, and HEAD moved on: a post-merge hook committed
            res.update(ok=True, sha=below)
            res["warning"] = "%s moved on past the merge commit %s to %s: a post-merge hook committed%s" % (
                base, below[:10], m["head"][:10], ("; " + (m["error"] or "git merge exited %s" % m["exit"])) if m["exit"] != 0 else "")
        elif m["merge_head"] == theirs:                                      # stopped with our merge in progress: a conflict, a hook, a signature
            res["conflicts"] = self.unmerged(wt)
            undo = self.abort_merge(wt)
            res["error"] = ("git merge stopped in %s and was aborted (%s is unchanged): %s" % (wt, base, said) if not undo else
                            "git merge stopped in %s (%s), and aborting it failed: %s — %s" % (wt, said, undo, left()))
        elif m["exit"] == 2:                                                 # git's refusal before it touched anything
            res["error"] = "git merge refused in %s (exit 2; %s is unchanged): %s" % (wt, base, said)
        else:                                                                # killed, a write that failed, a stale index.lock …
            res["error"] = "git merge failed in %s (%s): %s — %s" % (wt, m["error"] or "exit %s" % m["exit"], m["said"] or "no output", left())
        return res

    def _merge_in_scratch(self, base: str, branch: str, message: str, scratch: Optional[Any], res: Dict[str, Any]) -> Dict[str, Any]:
        if not scratch:
            res["error"] = "merge.method is worktree and %s is checked out nowhere, but no place for a temporary worktree was given" % base
            return res
        tmp = Path(scratch) / ("merge-%s-%d" % (re.sub(r"[^A-Za-z0-9_.-]+", "-", base), os.getpid()))
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = self.run("worktree", "add", "--quiet", str(tmp), base, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            res["error"] = "git worktree add (a temporary checkout of %s) did not finish: %s" % (base, exc)
            return res
        if r.returncode != 0:
            res["error"] = "git worktree add (a temporary checkout of %s) failed: %s" % (base, r.stderr.strip())
            return res
        try:
            self._merge_in(tmp, base, branch, message, res, temporary=True)
        finally:
            self._remove_temporary(tmp)
        res["worktree"] = None
        return res

    def _remove_temporary(self, tmp: Path) -> None:
        """A temporary worktree of dtree's own removed, entry and all. When ``git worktree remove`` fails (a file it cannot delete),
        the directory goes first and then the entry (``remove --force`` of the path again works for a missing directory): nothing
        of dtree's prunes the project's worktrees any more, so an entry left here would stay."""
        for first in (True, False):
            try:
                if self.run("worktree", "remove", "--force", str(tmp), timeout=600).returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
            if first:
                shutil.rmtree(str(tmp), ignore_errors=True)

    def update_branch(self, branch: str, base: str, worktree: Optional[Any] = None, message: Optional[str] = None,
                      opts: Sequence[str] = ()) -> Dict[str, Any]:
        """Bring ``branch`` up to date with ``base`` by merging the base into it (git's own message, "Merge branch '<base>' into
        <branch>"; a fast-forward when the branch has no commits of its own):

        - ``worktree`` None: the branch must be checked out nowhere. A clean merge is plumbing (``update-ref`` with the old value); a
          conflict changes nothing and comes back as state conflicts with ``in_progress`` False;
        - a ``worktree`` of the branch: ``git merge --no-ff --no-edit`` there, after the same checks as ``merge_into``. A conflict is LEFT
          in progress (``in_progress`` True) with its paths, for whoever resolves it with ``git add`` and ``git commit``.

        A merge that stops in the worktree for another reason than a conflict (a hook, a commit signature that fails, the timeout, a
        stale ``index.lock``) is aborted and comes back as an error: it is not for an agent to resolve. A conflict whose every path git
        rerere resolved and staged (``rerere.autoupdate``) is still a conflict: ``conflicts`` are then the preview's paths. ``opts``: git
        options for dtree's own git in that worktree (``jobs.SAFE_GIT`` there: no hook, no fsmonitor — as the plumbing path, which runs
        none).

        Returns {state clean|conflicts|up to date|error, how (plumbing|fast-forward|worktree), sha, base_sha, branch_sha, conflicts,
        in_progress, worktree, error}."""
        res: Dict[str, Any] = {"state": "error", "how": None, "sha": None, "base_sha": self.sha(base), "branch_sha": self.sha("refs/heads/" + branch),
                               "conflicts": [], "in_progress": False, "worktree": str(worktree) if worktree else None, "error": None}
        theirs, msg = res["base_sha"], message or "Merge branch '%s' into %s" % (base, branch)
        if not res["branch_sha"] or not theirs:
            res["error"] = "no branch %s" % (branch if not res["branch_sha"] else base)
            return res
        if self.ancestor(theirs, res["branch_sha"]):
            res["state"] = "up to date"
            return res
        if worktree is not None:
            return self._update_in(Path(worktree), branch, base, msg, res, opts)
        for _ in range(RETRIES):
            ours = self.sha("refs/heads/" + branch)
            res["branch_sha"] = ours
            if not ours:
                res["error"] = "no branch %s (deleted meanwhile)" % branch
                return res
            if self.ancestor(theirs, ours):                                   # also after losing a race to the same update (a double click)
                res["state"] = "up to date"
                return res
            where = [w for w in self.checked_out(branch) if not w["prunable"]]
            if where:
                res["error"] = "%s is checked out at %s: it is updated there" % (branch, where[0]["path"])
                return res
            if self.ancestor(ours, theirs):                                   # nothing of its own: it only moves forward
                moved = self._move(branch, theirs, ours, "dtree: fast-forward %s to %s" % (branch, base))
                new = theirs
                res["how"] = "fast-forward"
            else:
                mt = self.merge_tree(ours, theirs)
                if mt["state"] != "clean":
                    res.update(state=mt["state"], conflicts=mt["conflicts"], error=mt["error"])
                    return res
                new, err = self._commit_tree(mt["tree"], [ours, theirs], msg)
                if not new:
                    res["error"] = err
                    return res
                moved = self._move(branch, new, ours, "dtree: merge %s into %s" % (base, branch))
                res["how"] = "plumbing"
            if moved != "race":
                res.update(state="clean" if not moved else "error", sha=new if not moved else None, error=moved or None)
                return res
        res["error"] = "%s kept moving while it was updated (%d tries): try again" % (branch, RETRIES)
        return res

    def _update_in(self, wt: Path, branch: str, base: str, msg: str, res: Dict[str, Any], opts: Sequence[str] = ()) -> Dict[str, Any]:
        """The update in worktree ``wt``, whose HEAD is the branch's state (a trial's shadow may hold commits the root lacks yet)."""
        theirs = res["base_sha"]
        res["how"] = "worktree"
        why = self.ready_worktree(wt, branch)
        if why:
            res["error"] = why
            return res
        ours = self.sha("HEAD", cwd=wt)
        res["branch_sha"] = ours
        if self.ancestor(theirs, ours, cwd=wt):                               # git merge would say "Already up to date" and make nothing
            res["state"] = "up to date"
            return res
        if self.ancestor(ours, theirs, cwd=wt):                               # nothing of its own: a fast-forward
            res["how"] = "fast-forward"
            way = self.in_the_way(wt, ours, theirs)
            if way is None or way:
                res["error"] = _in_the_way(wt, way)
                return res
            m = self._git_merge(wt, ("--ff-only", theirs), opts)
            if m["head"] == theirs:
                res.update(state="clean", sha=theirs)
            elif not m["ran"]:
                res["error"] = "%s in %s (%s is unchanged)" % (m["error"], wt, branch)
            else:                                                             # a refused --ff-only exits 1: where HEAD is tells
                res["error"] = "git merge --ff-only failed in %s (%s): %s — %s" % (wt, m["error"] or "exit %s" % m["exit"], m["said"] or "no output",
                                                                                   self._left_state(wt, ours))
            return res
        mt = self.merge_tree(ours, theirs, cwd=wt)
        if mt["state"] == "error":
            res["error"] = mt["error"]
            return res
        way = self.in_the_way(wt, ours, mt["tree"])
        if way is None or way:
            res["error"] = _in_the_way(wt, way)
            return res
        m = self._git_merge(wt, ("--no-ff", "--no-log", "--no-edit", "-m", msg, theirs), opts)
        said = m["said"] or m["error"] or "no output"
        if not m["ran"]:
            res["error"] = "%s in %s (%s is unchanged)" % (m["error"], wt, branch)
        elif m["parents"] == [ours, theirs]:
            res.update(state="clean", sha=m["head"])
        elif m["merge_head"] == theirs:
            left = self.unmerged(wt)
            # conflicts, left in progress for whoever resolves them. With rerere.autoupdate git stages a resolution it recorded earlier,
            # so none may be unmerged; a merge that could not take index.lock ends the same way (exit 1, MERGE_HEAD) but wrote nothing:
            # told apart by what is staged, never by whether a lock is there (another git may have held it and let go meanwhile)
            if m["exit"] == 1 and (left or (mt["state"] == "conflicts" and self._staged(wt, opts))):
                every = mt["conflicts"] if mt["state"] == "conflicts" else left          # rerere may have staged some: all are named
                res.update(state="conflicts", conflicts=every or left, in_progress=True, staged=[p for p in every if p not in left])
            else:                                                             # stopped by something else: not for an agent to resolve
                why = "exit %s" % m["exit"] if m["exit"] is not None else m["error"]
                undo = self.abort_merge(wt, opts)
                res["error"] = ("git merge stopped in %s without a conflict (%s): %s; it was aborted and %s is unchanged" % (wt, why, said, branch)
                                if not undo else "git merge stopped in %s without a conflict (%s): %s; aborting it failed: %s — %s"
                                % (wt, why, said, undo, self._left_state(wt, ours)))
        elif m["exit"] == 2:                                                  # git's refusal before it touched anything
            res["error"] = "git merge refused in %s (exit 2; %s is unchanged): %s" % (wt, branch, said)
        else:
            res["error"] = "git merge failed in %s (%s): %s — %s" % (wt, m["error"] or "exit %s" % m["exit"], m["said"] or "no output",
                                                                     self._left_state(wt, ours))
        return res

    def abort_merge(self, wt: Any, opts: Sequence[str] = ()) -> str:
        """``git merge --abort`` in worktree ``wt``: back to its tip, unrelated edits kept. Empty when done, else why not."""
        if self.merge_head(wt) is None:
            return "no merge is in progress in %s" % wt
        try:
            r = self.run(*opts, "merge", "--abort", cwd=wt, timeout=MERGE_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "git merge --abort did not finish in %s: %s" % (wt, exc)
        return "" if r.returncode == 0 else "git merge --abort failed in %s: %s" % (wt, (r.stderr or r.stdout).strip()[:500])
