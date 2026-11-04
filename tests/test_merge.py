"""ค merge: readiness from ``git merge-tree`` (nothing touched), merging into the base without switching anyone's checkout (plumbing, or
the base's clean worktree), and bringing a trial up to date — an ``update`` job's agent resolves the conflicts and dtree checks the
result. Temporary repositories only; the agent is a script speaking Claude Code's JSON lines, so nothing spends quota."""
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ApiClient
from dtree import cli, jobs, merge, verify
from dtree.gitcode import Git
from dtree.project import Project
from dtree.store import DecisionError

Q = {"kind": "decision", "source": "claude", "key": "way", "title_th": "ทางเดิน", "title_en": "The way", "question_th": "ทางไหน", "question_en": "Which way?",
     "options": [{"key": "left", "th": "ซ้าย", "en": "Left", "recommended": True}, {"key": "right", "th": "ขวา", "en": "Right"}]}

#: an agent for update jobs: resolves every conflict by keeping both sides' lines (or leaves the markers in, or gives up), then commits
#: the merge with only git add + git commit, as the prompt asks — or breaks the rules ("plain": drops the merge with git reset and takes
#: the base's side of f.txt in a plain commit)
AGENT = r'''#!%s
import json, os, re, subprocess, sys
open(os.environ["FAKE_PROMPT"], "w").write(sys.stdin.read())
mode = os.environ.get("FAKE_MODE", "resolve")
def ev(d):
    print(json.dumps(d), flush=True)
ev({"type": "system", "subtype": "init", "session_id": "u1", "model": "fake"})
out = subprocess.run(["git", "ls-files", "-u", "-z"], capture_output=True, text=True).stdout
files = sorted({r.split("\t", 1)[1] for r in out.split("\0") if "\t" in r})
if mode == "plain":
    subprocess.run(["git", "reset", "-q", "--hard"], check=True)
    open("f.txt", "w").write("one\nmain\n")
    subprocess.run(["git", "commit", "-q", "-am", "take main to make it merge"], check=True)
if mode in ("resolve", "markers"):
    for f in files:
        if mode == "resolve":
            keep = [l for l in open(f).read().splitlines(True) if not re.match(r"^(<<<<<<<|=======|>>>>>>>)( |$)", l)]
            open(f, "w").write("".join(keep))
        subprocess.run(["git", "add", "--", f], check=True)
    subprocess.run(["git", "commit", "-q", "--no-edit", "--cleanup=strip"], check=True)
ev({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0.01, "session_id": "u1",
    "structured_output": {"built": mode != "give_up", "tests": "2 passed", "impact": "goes right; both lines of f.txt kept", "note": ""}})
'''


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True, check=check).stdout


def _sha(cwd, ref):
    return _git(cwd, "rev-parse", "--verify", ref + "^{commit}").strip()


def _commit_on(repo, branch, files, msg, start="main"):
    """Commit ``files`` ({path: text}) on ``branch`` (made from ``start`` when missing) in a throwaway worktree: the root is not touched."""
    if subprocess.run(["git", "-C", str(repo["path"]), "rev-parse", "--verify", "--quiet", "refs/heads/" + branch], capture_output=True).returncode:
        _git(repo["path"], "branch", branch, start)
    wt = repo["tmp"] / ("scratch-" + branch.replace("/", "-"))
    _git(repo["path"], "worktree", "add", "-q", str(wt), branch)
    try:
        for path, text in files.items():
            (wt / path).parent.mkdir(parents=True, exist_ok=True)
            (wt / path).write_text(text)
        _git(wt, "add", "-A")
        _git(wt, "commit", "-q", "-m", msg)
        return _sha(wt, "HEAD")
    finally:
        _git(repo["path"], "worktree", "remove", "--force", str(wt))


def _commit_root(repo, files, msg):
    for path, text in files.items():
        (repo["path"] / path).write_text(text)
    _git(repo["path"], "add", "-A")
    _git(repo["path"], "commit", "-q", "-m", msg)
    return _sha(repo["path"], "HEAD")


def _project(repo, **extra):
    agent = repo["tmp"] / "agent"
    agent.write_text(AGENT % sys.executable)
    agent.chmod(0o755)
    cfg = {"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"), "db": str(repo["tmp"] / "d.sqlite"),
           "agents": {"claude": {"command": str(agent), "sandbox": "none"}}, "test_command": "python -m pytest -q"}
    cfg.update(extra)
    return Project(repo["path"], cfg)


def _trial(p, repo, status="measured", files=None):
    """A question answered "left" and a trial of "right" with one commit on its branch, measured (and adopted when asked)."""
    did = p.store.add(Q)["id"]
    p.store.answer(did, "left")
    t = p.store.try_option(did, "right")
    _commit_on(repo, t["branch"], files or {"f.txt": "one\nright\n"}, "build right")
    p.store.record_code(did, p.git.commits_between("main", t["branch"]), trial=t["id"])
    if status in ("measured", "adopted"):
        p.store.trial_result(t["id"], "[claude] 1 passed", "goes right")
    if status == "adopted":
        p.store.trial_end(t["id"], "adopted")
    return did, p.store.trial(t["id"])


@pytest.fixture
def calls(monkeypatch, repo):
    """Jobs are run in this process (no runner is launched), and verification is only recorded."""
    seen = {"launched": [], "verify_bg": [], "verify_after": []}
    alive = jobs._alive
    monkeypatch.setattr(jobs, "launch", lambda proj, jid: seen["launched"].append(jid) or 999999)
    monkeypatch.setattr(jobs, "_alive", lambda pid, marker="": pid == 999999 or alive(pid, marker))     # the stand-in runner lives
    monkeypatch.setattr(verify, "start_background", lambda proj, tid, by="user", force=False, **kw: seen["verify_bg"].append(tid) or 4242)
    monkeypatch.setattr(verify, "after_job", lambda proj, job, trial, say: seen["verify_after"].append(trial["id"]) or {"status": "passed"})
    monkeypatch.setenv("FAKE_PROMPT", str(repo["tmp"] / "prompt.txt"))
    return seen


def _events(p, did, action="updated"):
    return [e["detail"] for e in p.store.get(did)["events"] if e["action"] == action]


# ----------------------------------------------------------------------------------------------------------- previews
def test_preview_clean_conflicts_merged_drift_missing_and_unrelated(repo):
    g, root = Git(repo["path"]), repo["path"]
    _commit_on(repo, "alt/1-clean", {"g.txt": "new\n"}, "add g")
    _commit_on(repo, "alt/1-conflict", {"f.txt": "one\ntrial\n"}, "trial edits f")
    _git(root, "branch", "alt/1-merged", "main")
    for i in range(3):
        _commit_root(repo, {"f.txt": "one\nmain %d\n" % i}, "main %d" % i)
    _git(root, "branch", "alone", _git(root, "commit-tree", "-m", "unrelated", "main^{tree}").strip())     # no common history
    index = (root / ".git" / "index").stat().st_mtime_ns
    clean = g.merge_preview("main", "alt/1-clean")
    assert clean["state"] == "clean" and clean["conflicts"] == [] and len(clean["tree"]) == 40
    assert (clean["behind"], clean["ahead"]) == (3, 1) and clean["merge_base"] == _sha(root, "main~3")
    conflict = g.merge_preview("main", "alt/1-conflict")
    assert conflict["state"] == "conflicts" and conflict["conflicts"] == ["f.txt"] and len(conflict["tree"]) == 40
    merged = g.merge_preview("main", "alt/1-merged")
    assert merged["state"] == "merged" and (merged["behind"], merged["ahead"]) == (3, 0)
    missing = g.merge_preview("main", "alt/nope")
    assert missing["state"] == "error" and "alt/nope" in missing["error"]
    unrelated = g.merge_preview("main", "alone")
    assert unrelated["state"] == "error" and "unrelated" in unrelated["error"]
    assert (root / ".git" / "index").stat().st_mtime_ns == index and _git(root, "status", "--porcelain") == ""   # nothing touched


def test_conflicted_paths_keep_thai_tab_space_and_newline_names(repo):
    """Paths come from -z output: without it git quotes Thai names and tabs and splits a newline."""
    names = ["ไทย.txt", "tab\tname.txt", "space name.txt", "new\nline.txt"]
    _commit_root(repo, {n: "a\n" for n in names}, "the names")
    _commit_on(repo, "alt/2-x", {n: "trial\n" for n in names}, "trial")
    _commit_root(repo, {n: "main\n" for n in names}, "main")
    g = Git(repo["path"])
    assert sorted(g.merge_preview("main", "alt/2-x")["conflicts"]) == sorted(names)
    wt = repo["tmp"] / "x-wt"
    _git(repo["path"], "worktree", "add", "-q", str(wt), "alt/2-x")
    res = g.update_branch("alt/2-x", "main", wt)
    assert res["state"] == "conflicts" and res["in_progress"] and sorted(res["conflicts"]) == sorted(names)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "--no-edit")                                            # the markers committed as they are
    tip = _sha(wt, "HEAD")
    marks = g.leftover_markers(tip, tip, res["conflicts"])
    assert len(marks) == 3 * len(names) and any(m.startswith("ไทย.txt:") for m in marks)


def test_a_markdown_underline_from_the_base_is_not_a_leftover_marker(repo):
    """git diff --check against the first parent alone flags it (the probe's false positive); a real leftover is in neither parent."""
    _commit_root(repo, {"r.md": "Title\n"}, "readme")
    _commit_on(repo, "alt/3-x", {"r.md": "Trial title\n", "f.txt": "one\ntrial\n"}, "trial")
    _commit_root(repo, {"r.md": "Title\n=======\n", "f.txt": "one\nmain\n"}, "main underlines the title")
    g = Git(repo["path"])
    wt = repo["tmp"] / "x-wt"
    _git(repo["path"], "worktree", "add", "-q", str(wt), "alt/3-x")
    assert g.update_branch("alt/3-x", "main", wt)["state"] == "conflicts"
    (wt / "r.md").write_text("Trial title\n=======\n")                               # resolved: the base's underline kept
    (wt / "f.txt").write_text("one\ntrial\n<<<<<<< HEAD\nmain\n")                    # one marker left
    _git(wt, "add", "r.md", "f.txt")
    _git(wt, "commit", "-q", "--no-edit", "--cleanup=strip")
    tip = _sha(wt, "HEAD")
    assert "r.md" in _git(wt, "diff", "--check", "HEAD^1", "HEAD", check=False)       # what diff --check alone says
    assert g.leftover_markers(tip, tip, ["r.md", "f.txt"]) == ["f.txt:3"]


def test_where_a_branch_is_checked_out_sees_a_rebase_and_drops_a_deleted_worktree(repo):
    root, g = repo["path"], Git(repo["path"])
    assert [w["how"] for w in g.checked_out("main")] == ["checked out"] and g.worktrees()[0]["admin"].endswith(".git")
    _commit_on(repo, "conf", {"f.txt": "one\nconf\n"}, "conf")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    _git(root, "switch", "-q", "--detach")
    wt = repo["tmp"] / "main-wt"
    _git(root, "worktree", "add", "-q", str(wt), "main")
    assert subprocess.run(["git", "-C", str(wt), "rebase", "conf"], capture_output=True).returncode != 0      # stops on the conflict
    busy = g.checked_out("main")
    assert [(w["path"], w["how"]) for w in busy] == [(str(wt), "rebasing")]
    res = g.merge_into("main", "conf", "m")
    assert not res["ok"] and "being rebased" in res["error"] and _sha(root, "main") == _sha(root, "HEAD")
    _git(wt, "rebase", "--abort")
    shutil.rmtree(wt)
    gone = g.checked_out("main")
    assert gone and gone[0]["prunable"]                                                # git still lists it until a prune


def test_observers_take_no_optional_lock_in_a_worktree(repo):
    """A plain git status rewrites the index to refresh stat data; an agent's own git calls then fail on index.lock (the probe)."""
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo)
    f, index = root / "f.txt", root / ".git" / "index"
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))                  # same content, a stat the index does not know
    before = index.stat().st_mtime_ns
    assert p.git.changes(root) == [] and merge.readiness(p, t)["state"] == "clean"
    assert index.stat().st_mtime_ns == before
    subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, env=dict(os.environ, GIT_OPTIONAL_LOCKS="1"))
    assert index.stat().st_mtime_ns != before                                          # what the env var spares


# ----------------------------------------------------------------------------------------------------------- merging
def test_a_plumbing_merge_is_the_commit_git_merge_no_ff_makes(repo, monkeypatch):
    root, g = repo["path"], Git(repo["path"])
    for k in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(k, "2026-11-01T12:00:00+07:00")
    _commit_on(repo, "alt/4-x", {"g.txt": "new\n"}, "add g")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main moves")
    _git(root, "switch", "-q", "--detach")                                            # main is checked out nowhere now
    _git(root, "branch", "copy", "main")
    wt = repo["tmp"] / "copy-wt"
    _git(root, "worktree", "add", "-q", str(wt), "copy")
    msg = "Merge #12 alt/4-x: title   \n\n- add g\n\n\n\nDtree-Decision: 12\n\n"
    res = g.merge_into("main", "alt/4-x", msg)
    assert res["ok"] and res["how"] == "plumbing" and res["worktree"] is None
    _git(wt, "merge", "-q", "--no-ff", "-m", msg, "alt/4-x")
    assert res["sha"] == _sha(wt, "HEAD")                                               # same tree, parents, message: same commit
    assert _git(root, "log", "-1", "--format=%P", "main").split() == [res["base_sha"], _sha(root, "alt/4-x")]
    assert _git(root, "status", "--porcelain") == "" and _git(root, "rev-parse", "HEAD").strip() == res["base_sha"]  # the root untouched
    again = g.merge_into("main", "alt/4-x", msg)
    assert not again["ok"] and "already" in again["error"]                             # no empty merge commit


def test_a_plumbing_merge_starts_over_when_the_base_moves_meanwhile(repo, monkeypatch):
    root, g = repo["path"], Git(repo["path"])
    _commit_on(repo, "alt/5-x", {"g.txt": "new\n"}, "add g")
    _git(root, "switch", "-q", "--detach")
    real, moved = Git.run, []

    def racing(self, *args, **kw):
        if args and args[0] == "update-ref" and not moved:                           # someone else's commit lands first
            other = real(self, "commit-tree", "-p", "main", "-m", "someone else", "main^{tree}").stdout.strip()
            real(self, "update-ref", "refs/heads/main", other)
            moved.append(other)
        return real(self, *args, **kw)
    monkeypatch.setattr(Git, "run", racing)
    res = g.merge_into("main", "alt/5-x", "merge it")
    assert res["ok"] and moved and _git(root, "log", "-1", "--format=%P", "main").split()[0] == moved[0]


def test_a_merge_in_the_base_s_clean_checkout_and_the_refusals(repo):
    root, g = repo["path"], Git(repo["path"])
    _commit_on(repo, "alt/6-x", {"b.txt": "b\n"}, "add b")
    before = _sha(root, "main")
    (root / "f.txt").write_text("one\nlocal edit\n")                                   # dirty: refused, the edit kept
    res = g.merge_into("main", "alt/6-x", "m")
    assert not res["ok"] and res["how"] == "worktree" and "uncommitted" in res["error"] and (root / "f.txt").read_text() == "one\nlocal edit\n"
    _git(root, "checkout", "--", "f.txt")
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("b.txt\n")
    (root / "b.txt").write_text("precious\n")                                         # ignored, where the merge adds b.txt
    res = g.merge_into("main", "alt/6-x", "m")
    assert not res["ok"] and "overwrite" in res["error"] and (root / "b.txt").read_text() == "precious\n"
    assert not g.merge_into("main", "alt/6-x", "m", method="plumbing")["ok"]           # would leave the root's checkout stale
    assert _sha(root, "main") == before and not (root / ".git" / "MERGE_HEAD").exists()
    (root / "b.txt").unlink()
    res = g.merge_into("main", "alt/6-x", "Merge it")
    assert res["ok"] and res["how"] == "worktree" and res["worktree"] == str(root) and res["base_sha"] == before
    assert _sha(root, "HEAD") == res["sha"] and (root / "b.txt").read_text() == "b\n" and g.current_branch() == "main"
    assert _git(root, "status", "--porcelain", "--untracked-files=no") == ""


def test_an_ignored_file_or_link_where_the_merge_makes_a_directory_is_in_the_way(repo, calls):
    """Review 2026-11-02 (major): only the paths the merge adds were looked at, so an ignored file or symbolic link at a leading directory
    of one (``cache`` for ``cache/new.txt``) was replaced by the directory without a word. A tracked file that the merge itself turns
    into a directory still merges."""
    root, g = repo["path"], Git(repo["path"])
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"cache/new.txt": "from the trial\n"})
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("cache\n")
    precious = repo["tmp"] / "precious"
    precious.mkdir()
    (precious / "keep.dat").write_text("precious\n")
    cache = root / "cache"
    for make in (lambda: cache.write_text("an ignored file\n"), lambda: os.symlink(str(precious), str(cache))):
        make()
        assert g.in_the_way(root, _sha(root, "main"), g.merge_preview("main", t["branch"])["tree"]) == ["cache"]
        r = merge.readiness(p, p.store.trial(t["id"]))
        assert not r["can_merge"] and r["reason_code"] == "in_the_way" and "cache" in r["reason"]
        res = g.merge_into("main", t["branch"], "m")
        assert not res["ok"] and "cache" in res["error"]
        assert cache.is_symlink() or cache.read_text() == "an ignored file\n"                     # left as it was
        cache.unlink()
    assert sorted(os.listdir(precious)) == ["keep.dat"] and _git(root, "log", "-1", "--format=%s", "main").strip() == "first"
    _commit_root(repo, {"a": "a file\n"}, "a is a file")
    _git(root, "branch", "alt/8-dir", "main")
    w = repo["tmp"] / "dir-wt"
    _git(root, "worktree", "add", "-q", str(w), "alt/8-dir")
    _git(w, "rm", "-q", "a")
    (w / "a").mkdir()
    (w / "a" / "b.txt").write_text("b\n")
    _git(w, "add", "-A")
    _git(w, "commit", "-q", "-m", "a becomes a directory")
    _git(root, "worktree", "remove", "--force", str(w))
    assert g.in_the_way(root, _sha(root, "main"), g.merge_preview("main", "alt/8-dir")["tree"]) == []
    res = g.merge_into("main", "alt/8-dir", "m")
    assert res["ok"] and (root / "a" / "b.txt").read_text() == "b\n"


def test_a_merge_that_does_not_finish_is_reported_with_what_it_left(repo, monkeypatch):
    """Review 2026-11-02: a git merge in the base's checkout that timed out (a hook that hangs) raised TimeoutExpired out of merge_into,
    leaving the merge staged and no MERGE_HEAD to abort; and every exit but a conflict was called "unchanged". Only exit 2 is git's
    refusal before it touched anything; the rest is reported with git status and how to put it back."""
    root, g = repo["path"], Git(repo["path"])
    _commit_on(repo, "alt/9-x", {"g.txt": "trial\n"}, "trial")
    pid, real = repo["tmp"] / "hook.pid", Git.run

    def short(self, *args, **kw):                                                    # the merge's 600 s, shortened to 1 s
        if "merge" in args and "--abort" not in args:
            kw["timeout"] = 1
        return real(self, *args, **kw)

    def hung(name):                                                                  # a merge under a hook that hangs, killed at 1 s
        hook = root / ".git" / "hooks" / name
        hook.write_text("#!/bin/sh\necho $$ > %s\nexec sleep 30\n" % pid)
        hook.chmod(0o755)
        monkeypatch.setattr(Git, "run", short)
        try:
            return g.merge_into("main", "alt/9-x", "m")
        finally:
            monkeypatch.setattr(Git, "run", real)
            hook.unlink()
            if pid.exists():
                os.kill(int(pid.read_text()), signal.SIGTERM)                        # the hook's sleep, started by this test's git
                pid.unlink()
    res = hung("pre-merge-commit")
    assert not res["ok"] and "did not finish" in res["error"] and "unchanged" not in res["error"]
    assert "A  g.txt" in res["error"] and "reset --merge" in res["error"]
    _git(root, "reset", "-q", "--merge")
    assert _git(root, "status", "--porcelain") == "" and _git(root, "log", "-1", "--format=%s", "main").strip() == "first"
    lock = root / ".git" / "index.lock"           # a stale lock: git merge cannot write the index, exits 1 with MERGE_HEAD, and the abort fails
    lock.write_text("")
    res = g.merge_into("main", "alt/9-x", "m")
    assert not res["ok"] and "unchanged" not in res["error"] and "index.lock" in res["error"] and "merge --abort" in res["error"]
    lock.unlink()
    _git(root, "merge", "--abort")                                                   # what the message says to do
    assert _git(root, "status", "--porcelain") == "" and not (root / ".git" / "MERGE_HEAD").exists()

    def no_git(self, *args, **kw):
        if "merge" in args and "--no-ff" in args:
            raise OSError(2, "No such file or directory", "git")
        return real(self, *args, **kw)
    monkeypatch.setattr(Git, "run", no_git)
    res = g.merge_into("main", "alt/9-x", "m")
    assert not res["ok"] and "could not run" in res["error"]
    monkeypatch.setattr(Git, "run", real)
    res = hung("post-merge")                                                         # killed after the merge commit was made
    assert res["ok"] and "after the merge commit" in res["warning"]
    assert _git(root, "log", "-1", "--format=%P", "main").split() == [res["base_sha"], _sha(root, "alt/9-x")]


def test_a_post_merge_hook_that_commits_does_not_turn_the_merge_into_a_failure(repo, calls):
    """Re-review 2026-11-02: in the base's checkout a post-merge hook made a commit of its own, so HEAD was the hook's commit and the
    merge — made — was reported as failed ("git merge failed … (exit 0)"): nothing was recorded, and readiness said "nothing" afterwards,
    so --close could not be used. The merge commit is now taken from HEAD's first-parent line, with a warning."""
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    hook = root / ".git" / "hooks" / "post-merge"
    hook.write_text("#!/bin/sh\ngit commit -q --allow-empty -m 'post-merge bookkeeping'\n")
    hook.chmod(0o755)
    before, theirs = _sha(root, "main"), _sha(root, t["branch"])
    out = merge.merge_trial(p, t["id"], close=True)
    m = out["merge"]
    assert m["ok"] and m["how"] == "worktree" and "post-merge" in m["warning"] and _git(root, "log", "-1", "--format=%s", "main").strip() == "post-merge bookkeeping"
    assert m["sha"] == _sha(root, "main~1") and _git(root, "log", "-1", "--format=%P", m["sha"]).split() == [before, theirs]
    assert out["closed"] and p.store.get(did)["status"] == "done" and p.store.get(did)["commits"][-1]["sha"] == m["sha"]
    assert _events(p, did, "merged")[-1]["sha"] == m["sha"] and merge.readiness(p, p.store.trial(t["id"]))["reason_code"] == "merged"


def test_method_worktree_merges_in_a_temporary_checkout_when_the_base_is_nowhere(repo):
    root, g = repo["path"], Git(repo["path"])
    _commit_on(repo, "alt/7-x", {"g.txt": "new\n"}, "add g")
    _git(root, "switch", "-q", "--detach")
    res = g.merge_into("main", "alt/7-x", "m", method="worktree", scratch=repo["tmp"] / "scratch")
    assert res["ok"] and res["how"] == "worktree" and res["worktree"] is None and _git(root, "log", "-1", "--format=%s", "main").strip() == "m"
    assert len(g.worktrees()) == 1 and not g.checked_out("main")                       # the temporary worktree is gone


def test_merge_trial_needs_an_adopted_trial_records_the_commits_and_closes(repo, calls):
    p = _project(repo)
    did, t = _trial(p, repo, status="measured")
    r = merge.readiness(p, t)
    assert r["state"] == "clean" and not r["can_merge"] and r["reason_code"] == "not_adopted" and r["how"] == "worktree"
    row = jobs.compare(p, did)["rows"][0]
    assert row["merge"]["state"] == "clean" and row["merge"]["reason_code"] == "not_adopted"
    with pytest.raises(DecisionError):
        merge.merge_trial(p, t["id"])
    p.store.trial_end(t["id"], "adopted")
    tip = _sha(repo["path"], t["branch"])
    p.store.note_event(did, "verified", "user", {"trial": t["id"], "sha": tip, "base_sha": _sha(repo["path"], "main"), "status": "passed",
                                                 "tests": "3 passed, 0 failed", "base_tests": "2 passed, 0 failed", "measures": {"score": [3.5, 3.0]}})
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"]
    out = merge.merge_trial(p, t["id"], by="user", close=True)
    m = out["merge"]
    assert m["ok"] and m["how"] == "worktree" and m["commits"] == 1 and out["closed"]
    d = p.store.get(did)
    assert d["status"] == "done" and d["result_impact"] == "goes right"
    assert d["result_tests"].startswith("dtree verify %s: 3 passed, 0 failed · base" % tip[:7]) and "score 3.5 (base 3.0)" in d["result_tests"]
    assert "[claude] 1 passed" in d["result_tests"]
    for kept in (d["commits"], p.store.trial(t["id"])["commits"]):
        assert [c["subject"] for c in kept][-2:] == ["build right", "Merge #%d %s: The way" % (did, t["branch"])] and kept[-1]["merge"]
    assert d["commits"][-1]["how"] == "merged"
    assert _events(p, did, "merged") == [{"trial": t["id"], "branch": t["branch"], "sha": m["sha"], "how": "worktree"}]
    after = merge.readiness(p, p.store.trial(t["id"]))
    assert after["state"] == "merged" and after["reason_code"] == "merged"
    with pytest.raises(DecisionError):
        merge.merge_trial(p, t["id"])


def test_the_base_checkout_is_looked_at_once_for_the_rows_of_a_compare(repo, calls, monkeypatch):
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    seen, real = [], Git.ready_worktree
    monkeypatch.setattr(Git, "ready_worktree", lambda self, wt, branch: seen.append(str(wt)) or real(self, wt, branch))
    assert merge.readiness(p, t)["can_merge"] and merge.readiness(p, t)["can_merge"] and len(seen) == 1
    (repo["path"] / "f.txt").write_text("one\nedited\n")
    _git(repo["path"], "add", "f.txt")                                                 # the index changes: looked at again
    r = merge.readiness(p, t)
    assert len(seen) == 2 and r["reason_code"] == "base_dirty" and "uncommitted" in r["reason"]
    with pytest.raises(DecisionError):
        merge.merge_trial(p, t["id"])                                                 # a merge always looks itself


def test_cli_merge_no_longer_switches_branches(repo, calls):
    root = repo["path"]
    p = _project(repo, merge_trailer="Co-Authored-By: someone <x@example.invalid>")
    a = p.store.add(dict(Q, key="a"))["id"]
    p.store.answer(a, "left")
    assert "new, from main" in cli.start(p, a) and p.git.current_branch() == "dec/%d-left" % a
    (root / "f.txt").write_text("one\nleft\n")
    with pytest.raises(DecisionError) as err:
        cli.merge(p, a)                                                               # the work is not committed yet
    assert "uncommitted" in str(err.value)
    _git(root, "commit", "-q", "-am", "the work")
    out = cli.merge(p, a)
    assert "1 commits" in out and "plumbing" in out and p.git.current_branch() == "dec/%d-left" % a      # main was checked out nowhere
    assert "someone" in _git(root, "log", "-1", "--format=%B", "main") and p.git.branch_info("dec/%d-left" % a)["merged"]
    _git(root, "switch", "-q", "main")
    b, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    with pytest.raises(DecisionError):
        cli.merge(p, a, t["id"])                                                      # not this question's trial
    out = cli.merge(p, b, t["id"], close=True)
    assert "git merge in %s" % root in out and "#%d is done" % b in out and p.git.current_branch() == "main"
    assert (root / "g.txt").read_text() == "right\n" and p.store.get(b)["status"] == "done"
    with pytest.raises(DecisionError):
        cli.merge(p, b, close=True)                                                   # --close goes with --trial


# ----------------------------------------------------------------------------------------------------------- updating
def test_update_clean_by_plumbing_then_in_the_trial_s_worktree(repo, calls):
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    tip = _sha(root, t["branch"])
    base1 = _commit_root(repo, {"f.txt": "one\nmain\n"}, "main moves")
    assert merge.readiness(p, t)["behind"] == 1
    u = merge.update_trial(p, t["id"])
    assert u["status"] == "clean" and u["how"] == "plumbing" and u["verify"] == 4242 and calls["verify_bg"] == [t["id"]]
    assert _git(root, "log", "-1", "--format=%P%n%s", t["branch"]).split("\n")[:2] == ["%s %s" % (tip, base1), "Merge branch 'main' into %s" % t["branch"]]
    assert _events(p, did)[-1] == {"trial": t["id"], "base_sha": base1, "status": "clean", "conflicts": [], "sha": u["sha"], "how": "plumbing"}
    assert p.store.trial(t["id"])["commits"][-1]["sha"] == u["sha"] and p.store.trial(t["id"])["status"] == "adopted"
    assert merge.update_trial(p, t["id"])["status"] == "up to date"
    wt = jobs._ensure_worktree(p, t["branch"])                                        # a worktree left by an earlier job
    base2 = _commit_root(repo, {"h.txt": "h\n"}, "main again")
    u = merge.update_trial(p, t["id"])
    assert u["status"] == "clean" and u["how"] == "worktree" and (wt / "h.txt").exists() and _sha(wt, "HEAD^2") == base2
    fresh = p.store.add(dict(Q, key="fresh"))["id"]
    ft = p.store.try_option(fresh, "right")
    _git(root, "branch", ft["branch"], "main~1")                                      # no commits of its own
    u = merge.update_trial(p, ft["id"])
    assert u["how"] == "fast-forward" and _sha(root, ft["branch"]) == _sha(root, "main")
    _commit_root(repo, {"i.txt": "i\n"}, "main a third time")
    _git(root, "switch", "-q", ft["branch"])                                          # a person's own checkout of the trial
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, ft["id"])
    assert "checked out at %s" % root in str(err.value) and p.git.current_branch() == ft["branch"]
    _git(root, "switch", "-q", "main")
    plumbing = _project(repo, merge={"method": "plumbing"})
    r = merge.readiness(plumbing, p.store.trial(t["id"]))
    assert r["reason_code"] == "method" and not r["can_merge"]                       # main is checked out in the root


def test_update_with_conflicts_is_resolved_by_an_update_job(repo, calls):
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    tip = _sha(root, t["branch"])
    base = _commit_root(repo, {"f.txt": "one\nmain\n"}, "main edits the same line")
    r = merge.readiness(p, t)
    assert r["state"] == "conflicts" and r["conflicts"] == ["f.txt"] and r["reason_code"] == "conflicts"
    u = merge.update_trial(p, t["id"])
    wt = repo["tmp"] / "wt" / t["branch"].replace("/", "-")
    assert u["status"] == "conflicts" and u["conflicts"] == ["f.txt"] and u["worktree"] == str(wt) and calls["launched"] == [u["job"]["id"]]
    assert u["job"]["kind"] == "update" and p.git.merge_head(wt) == base and _sha(root, t["branch"]) == tip
    assert _events(p, did)[-1]["status"] == "conflicts" and _events(p, did)[-1]["base_sha"] == base
    assert merge.readiness(p, p.store.trial(t["id"]))["reason_code"] == "job_running"
    with pytest.raises(DecisionError):
        merge.update_trial(p, t["id"])                                                # its job is running
    out = jobs.run(p, u["job"]["id"])
    assert out["status"] == "measured", out["reason"]
    prompt = (repo["tmp"] / "prompt.txt").read_text()
    assert "UPDATE" in prompt and "  f.txt" in prompt and base[:10] in prompt and "git commit --no-edit --cleanup=strip" in prompt
    assert _git(root, "log", "-1", "--format=%P", t["branch"]).split() == [tip, base]
    assert _git(root, "show", "%s:f.txt" % t["branch"]) == "one\nright\nmain\n"
    assert _events(p, did)[-1] == {"trial": t["id"], "base_sha": base, "status": "resolved", "conflicts": ["f.txt"], "sha": _sha(root, t["branch"]),
                                   "job": u["job"]["id"]}
    assert p.store.trial(t["id"])["status"] == "adopted" and calls["verify_after"] == [t["id"]] and not wt.exists()
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"]


def test_a_measured_trial_stays_measured_after_its_update_job(repo, calls):
    p = _project(repo)
    did, t = _trial(p, repo, status="measured")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    assert jobs.run(p, u["job"]["id"])["status"] == "measured"
    tr = p.store.trial(t["id"])
    assert tr["status"] == "measured" and tr["result_impact"].startswith("goes right; both lines")


def test_leftover_markers_fail_the_update_job(repo, calls, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "markers")
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    out = jobs.run(p, u["job"]["id"])
    assert out["status"] == "failed" and "conflict markers are left in f.txt:2" in out["reason"]
    assert p.store.trial(t["id"])["status"] == "adopted" and _events(p, did)[-1]["status"] == "conflicts"


def test_an_unfinished_update_is_sent_again_or_aborted(repo, calls, monkeypatch):
    root = repo["path"]
    monkeypatch.setenv("FAKE_MODE", "give_up")
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    tip = _sha(root, t["branch"])
    base = _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    out = jobs.run(p, u["job"]["id"])
    wt = repo["tmp"] / "wt" / t["branch"].replace("/", "-")
    assert out["status"] == "failed" and "not finished" in out["reason"] and "f.txt" in out["reason"] and p.git.merge_head(wt) == base
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert r["reason_code"] == "updating" and r["updating"]["unresolved"] == ["f.txt"]
    again = merge.update_trial(p, t["id"])                                           # the merge in progress gets a new job
    assert again["resumed"] and again["job"]["id"] != u["job"]["id"] and p.git.merge_head(wt) == base
    with pytest.raises(DecisionError):
        merge.abort_update(p, t["id"])                                                # its job is running
    assert jobs.run(p, again["job"]["id"])["status"] == "failed"
    a = merge.abort_update(p, t["id"])
    assert a["status"] == "aborted" and a["removed"] and a["branch_sha"] == tip and not wt.exists()
    assert _events(p, did)[-1]["status"] == "aborted"
    with pytest.raises(DecisionError):
        merge.abort_update(p, t["id"])                                                # nothing in progress now


def test_an_update_whose_job_cannot_start_changes_nothing(repo, calls, monkeypatch):
    root = repo["path"]
    p = _project(repo, agents={"claude": {"command": str(repo["tmp"] / "agent"), "sandbox": "none"}, "ghost": {"kind": "claude", "command": str(repo["tmp"] / "none")}},
                 max_parallel_jobs=1)
    did, t = _trial(p, repo, status="adopted")
    tip = _sha(root, t["branch"])
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    wt = repo["tmp"] / "wt" / t["branch"].replace("/", "-")
    with pytest.raises(DecisionError):
        merge.update_trial(p, t["id"], agent="ghost")                                 # checked before git is touched
    assert not wt.exists() and _events(p, did) == [] and p.store.jobs(tid=t["id"]) == []
    other = p.store.add(dict(Q, key="other"))["id"]
    p.store.answer(other, "left")
    busy = jobs.send(p, p.store.try_option(other, "right")["id"], "claude")           # the only job slot is taken
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, t["id"])
    assert "at most 1" in str(err.value) and not wt.exists() and _events(p, did) == []   # refused before git too
    p.store.job_end(busy["id"], "stopped", "the test frees the slot")

    def no_runner(proj, jid):
        raise OSError("the interpreter is gone")
    monkeypatch.setattr(jobs, "launch", no_runner)
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, t["id"])                                                # the merge was made; its runner cannot start
    assert "aborted" in str(err.value) and "could not start" in str(err.value) and _sha(root, t["branch"]) == tip and not wt.exists()
    assert [e["status"] for e in _events(p, did)] == ["conflicts", "aborted"] and p.store.jobs(tid=t["id"])[-1]["status"] == "failed"


def test_a_check_that_breaks_fails_the_job_and_a_missing_branch_is_a_row_not_a_crash(repo, calls, monkeypatch):
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])

    def broken(self, *a, **k):
        raise RuntimeError("git diff --check failed (exit 128)")
    monkeypatch.setattr(Git, "leftover_markers", broken)
    out = jobs.run(p, u["job"]["id"])                                                # the runner calls after_job unguarded
    assert out["status"] == "failed" and out["reason"].startswith("the update could not be checked: RuntimeError")
    lone = p.store.try_option(p.store.add(dict(Q, key="lone"))["id"], "left")       # no branch made for it
    row = merge.compare_fields(p, lone)["merge"]
    assert row["state"] == "error" and not row["can_merge"] and "no branch" in row["reason"]


def _common(wt):
    return _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()


def test_an_update_dtree_did_not_accept_is_not_ready_until_it_is_checked_again(repo, calls, monkeypatch):
    """Review 2026-11-02 (major): the update job's agent committed the merge with its conflict markers; dtree's check failed the job,
    yet readiness said ready and merge_trial(close) put the markers into the base, and ``dtree update`` answered "up to date". Now the
    merge gate runs the same marker check, and ``dtree update`` checks such an update again: refused while a marker is left, accepted
    once a person fixed them on the branch."""
    monkeypatch.setenv("FAKE_MODE", "markers")
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    out = jobs.run(p, u["job"]["id"])
    assert out["status"] == "failed" and "conflict markers are left in f.txt:2" in out["reason"]
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert not r["can_merge"] and r["reason_code"] == "markers" and "f.txt:2" in r["reason"] and "dtree update %d" % t["id"] in r["reason"]
    with pytest.raises(DecisionError):
        merge.merge_trial(p, t["id"], close=True)
    assert "<<<<<<<" not in _git(root, "show", "main:f.txt") and p.store.get(did)["status"] != "done"
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, t["id"])                                                # checked again: the markers are still there
    assert "f.txt:2" in str(err.value) and _events(p, did)[-1]["status"] == "conflicts"
    _commit_on(repo, t["branch"], {"f.txt": "one\nright\nmain\n"}, "the markers fixed by hand")
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert not r["can_merge"] and r["reason_code"] == "update_unchecked" and "job %d failed" % u["job"]["id"] in r["reason"]
    again = merge.update_trial(p, t["id"])
    assert again["status"] == "resolved" and calls["verify_bg"] == [t["id"]]
    assert _events(p, did)[-1]["status"] == "resolved" and _events(p, did)[-1]["sha"] == again["sha"]
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"]
    merge.merge_trial(p, t["id"], close=True)
    assert _git(root, "show", "main:f.txt") == "one\nright\nmain\n" and p.store.get(did)["status"] == "done"


def test_an_update_job_that_ended_before_dtree_checked_it_is_not_ready(repo, calls, monkeypatch):
    """The agent resolved and committed, but its job ended before dtree's check (a timeout, a stop, a crash): the update is not
    accepted, so not merged, until ``dtree update`` checks it."""
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    cls = jobs.agents.make(p.agent("claude"), p).__class__
    real = cls.run
    monkeypatch.setattr(cls, "run", lambda self, prompt, ctx: dict(real(self, prompt, ctx), error="timeout"))   # committed, then timed out
    out = jobs.run(p, u["job"]["id"])
    assert out["status"] == "failed" and out["reason"].startswith("longer than timeout_s")
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert not r["can_merge"] and r["reason_code"] == "update_unchecked" and "job %d failed" % u["job"]["id"] in r["reason"]
    again = merge.update_trial(p, t["id"])
    assert again["status"] == "resolved" and merge.readiness(p, p.store.trial(t["id"]))["can_merge"]


def test_an_update_left_in_the_trial_s_worktree_by_a_runner_that_died_is_checked_not_taken_as_clean(repo, calls, monkeypatch):
    """The agent committed the merge with its markers in the trial's shadow worktree and the runner died before it brought the branch
    back (or checked it). A later update merged the base "again" there — git said already up to date — and took the markers into the
    project as a clean update. Now the worktree's commits are brought back first and the update is checked."""
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    wt = Path(u["worktree"])
    subprocess.run([str(repo["tmp"] / "agent")], cwd=str(wt), input="the prompt", capture_output=True, text=True, check=True,
                   env=dict(os.environ, FAKE_MODE="markers"))                         # commits the markers; then the runner is gone
    p.store.job_end(u["job"]["id"], "failed", "the runner is gone")
    assert p.git.merge_head(wt) is None and _sha(wt, "HEAD") != _sha(repo["path"], t["branch"])
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, t["id"])
    assert "f.txt:2" in str(err.value) and _events(p, did)[-1]["status"] == "conflicts" and calls["verify_bg"] == []
    assert _sha(repo["path"], t["branch"]) == _sha(wt, "HEAD")                       # brought back: the gate sees it
    assert merge.readiness(p, p.store.trial(t["id"]))["reason_code"] == "markers"


def test_a_failed_update_job_s_plain_commits_are_not_merged(repo, calls, monkeypatch):
    """Re-review 2026-11-02 (major): the update job's agent dropped the merge (git reset, which the prompt forbids) and took the base's side
    in a plain commit. dtree's check failed the job (no merge commit of the base), yet the gate let the commit through — no merge on the
    branch read as "nothing of the update would be merged" — and merge --close put it into the base: the option gone, the question done.
    The conflicts event now keeps the commit the update started from (branch_sha); commits past it without the merge are refused until
    ``dtree update`` starts the update again."""
    monkeypatch.setenv("FAKE_MODE", "plain")
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    start = _sha(root, t["branch"])
    base = _commit_root(repo, {"f.txt": "one\nmain\n"}, "main edits the same line")
    u = merge.update_trial(p, t["id"])
    out = jobs.run(p, u["job"]["id"])
    assert out["status"] == "failed" and "the merge was not committed" in out["reason"]
    tip = _sha(root, t["branch"])
    assert _git(root, "log", "-1", "--format=%P", tip).split() == [start]                # a plain commit, brought back
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert not r["can_merge"] and r["reason_code"] == "update_unchecked" and "job %d failed" % u["job"]["id"] in r["reason"]
    assert _events(p, did)[-1]["status"] == "conflicts" and _events(p, did)[-1]["branch_sha"] == start
    assert "%s..%s" % (start[:7], t["branch"]) in r["reason"] and "dtree update %d" % t["id"] in r["reason"]
    assert "dtree update %d" % t["id"] in r["reason_th"]
    with pytest.raises(DecisionError):
        merge.merge_trial(p, t["id"], close=True)
    assert _sha(root, "main") == base and p.store.get(did)["status"] != "done"
    again = merge.update_trial(p, t["id"])                                           # starts again from the branch as it is
    assert again["status"] == "clean" and _events(p, did)[-1]["status"] == "clean" and calls["verify_bg"] == [t["id"]]
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"]


def test_dtree_update_takes_a_branch_that_holds_the_base_after_a_failed_update(repo, calls, monkeypatch):
    """After an update job left plain commits, the person merged the newer base into the branch in a checkout of their own. ``dtree
    update`` has nothing to merge then: answering only "up to date" left the gate shut for good. It takes the commits as they are, with
    an updated event that says so, and the trial is verified again."""
    monkeypatch.setenv("FAKE_MODE", "plain")
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    assert jobs.run(p, u["job"]["id"])["status"] == "failed"
    base2 = _commit_root(repo, {"h.txt": "h\n"}, "main moves on")
    mine = repo["tmp"] / "mine"
    _git(root, "worktree", "add", "-q", str(mine), t["branch"])
    _git(mine, "merge", "-q", "--no-edit", "main")                                   # the person brings main in by hand
    _git(root, "worktree", "remove", str(mine))
    tip = _sha(root, t["branch"])
    assert merge.readiness(p, p.store.trial(t["id"]))["reason_code"] == "update_unchecked"
    taken = merge.update_trial(p, t["id"])
    assert taken["status"] == "up to date" and taken["taken"] == 2 and taken["verify"] == 4242 and _sha(root, t["branch"]) == tip
    ev = _events(p, did)[-1]
    assert (ev["status"], ev["how"], ev["base_sha"], ev["sha"]) == ("clean", "up to date", base2, tip) and "taken as" in ev["note"]
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"] and calls["verify_bg"] == [t["id"]]
    assert merge.update_trial(p, t["id"])["status"] == "up to date" and len(_events(p, did)) == 2       # once


def test_readiness_sees_dtree_s_own_worktree_of_the_trial(repo, calls):
    """Re-review 2026-11-02: jobs run in the trial's shadow repository, so dtree's worktree of a trial is not in the project's list of
    worktrees, and readiness did not look at it. Uncommitted changes a job left there, and commits there never brought back (a runner
    that ended first), both read "ready", and the merge left them out. Now they are branch_dirty and not_brought_back."""
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    wt = jobs._ensure_worktree(p, t["branch"])                                       # kept after a job
    assert "/.shadow/" in _common(wt) and str(wt) not in _git(root, "worktree", "list")
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"]                   # clean, at the branch's commit
    _commit_on(repo, t["branch"], {"p.txt": "the person's\n"}, "the person's commit")
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"]                   # behind: a job's start brings it forward
    jobs._ensure_worktree(p, t["branch"])
    (wt / "g.txt").write_text("right, edited, not committed\n")
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert not r["can_merge"] and r["reason_code"] == "branch_dirty" and str(wt) in r["reason"] and "g.txt" in r["reason"]
    _git(wt, "commit", "-qam", "the agent's last commit, never brought back")
    last = _sha(wt, "HEAD")
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert not r["can_merge"] and r["reason_code"] == "not_brought_back" and str(wt) in r["reason"] and str(wt) in r["reason_th"]
    assert "dtree update %d" % t["id"] in r["reason"]
    before = _sha(root, "main")
    with pytest.raises(DecisionError):
        merge.merge_trial(p, t["id"], close=True)
    assert _sha(root, "main") == before and p.store.get(did)["status"] != "done"
    assert merge.update_trial(p, t["id"])["status"] == "up to date" and _sha(root, t["branch"]) == last      # brought back
    assert merge.merge_trial(p, t["id"], close=True)["closed"] and _git(root, "show", "main:g.txt") == "right, edited, not committed\n"
    did2, t2 = _trial(p, repo, status="adopted", files={"k.txt": "k\n"})
    w2 = jobs._ensure_worktree(p, t2["branch"])
    (w2 / "k.txt").write_text("k, in dtree's worktree\n")
    _git(w2, "commit", "-qam", "in dtree's worktree")
    _commit_on(repo, t2["branch"], {"k2.txt": "k2\n"}, "on the branch meanwhile")
    r = merge.readiness(p, p.store.trial(t2["id"]))
    assert not r["can_merge"] and r["reason_code"] == "not_brought_back" and "separate ways" in r["reason"]


def test_a_conflict_git_rerere_resolved_is_still_an_update_for_a_job(repo, calls, monkeypatch):
    """Re-review 2026-11-02: with rerere.enabled and rerere.autoupdate in the person's git config, git merge stages a resolution recorded
    earlier itself and stops with exit 1 and MERGE_HEAD but no unmerged path. That read as a merge stopped "without a conflict": it was
    aborted and the update refused. It is an update in progress, and the job checks the resolution and commits it. A stale index.lock
    ends the same way with nothing written, so that stays an error."""
    root = repo["path"]
    cfg = repo["tmp"] / "gitconfig"
    cfg.write_text("[rerere]\n\tenabled = true\n\tautoupdate = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    tip = _sha(root, t["branch"])
    base = _commit_root(repo, {"f.txt": "one\nmain\n"}, "main edits the same line")
    wt = jobs._ensure_worktree(p, t["branch"])                                       # kept from an earlier job
    _git(wt, "merge", "-q", base, check=False)                                       # resolved once by hand there: rerere records it
    (wt / "f.txt").write_text("one\nright\nmain\n")
    _git(wt, "add", "f.txt")
    _git(wt, "commit", "-q", "--no-edit")
    _git(wt, "reset", "-q", "--hard", tip)                                           # and put back: only the recorded resolution is left
    u = merge.update_trial(p, t["id"])
    assert u["status"] == "conflicts" and u["conflicts"] == ["f.txt"] and calls["launched"] == [u["job"]["id"]]
    assert p.git.merge_head(wt) == base and p.git.unmerged(wt) == [] and (wt / "f.txt").read_text() == "one\nright\nmain\n"
    assert _events(p, did)[-1]["conflicts"] == ["f.txt"]
    out = jobs.run(p, u["job"]["id"])
    assert out["status"] == "measured", out["reason"]
    prompt = (repo["tmp"] / "prompt.txt").read_text()
    assert "  f.txt" in prompt and "rerere" in prompt and "git show MERGE_HEAD:<path>" in prompt
    assert _git(root, "show", "%s:f.txt" % t["branch"]) == "one\nright\nmain\n" and _events(p, did)[-1]["status"] == "resolved"
    _commit_root(repo, {"f.txt": "one\nleft\nmain\n"}, "main edits it again")       # conflicts with the resolution
    wt = jobs._ensure_worktree(p, t["branch"])
    lock = Path(_git(wt, "rev-parse", "--absolute-git-dir").strip()) / "index.lock"
    lock.write_text("")                                                               # stale: git merge cannot write the index
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, t["id"])
    assert "index.lock" in str(err.value) and len(calls["launched"]) == 1 and _events(p, did)[-1]["status"] == "resolved"


def test_an_update_fixed_in_a_person_s_own_checkout_is_checked_there(repo, calls, monkeypatch):
    """Re-review 2026-11-02: the markers message says "fix them there and commit, then dtree update N checks it again". dtree's worktree
    is gone by then (it was clean), so the person fixes the branch in a checkout of their own — and ``dtree update`` refused because the
    branch was checked out there, before it reached the re-check, which reads only commits. The re-check comes first now; an update that
    needs git still leaves that checkout alone."""
    monkeypatch.setenv("FAKE_MODE", "markers")
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    assert jobs.run(p, u["job"]["id"])["status"] == "failed" and not Path(u["worktree"]).exists()
    assert merge.readiness(p, p.store.trial(t["id"]))["reason_code"] == "markers"
    mine = repo["tmp"] / "my-fix"
    _git(root, "worktree", "add", "-q", str(mine), t["branch"])                     # where the person fixes it, as the message says
    (mine / "f.txt").write_text("one\nright\nmain\n")
    _git(mine, "commit", "-qam", "fix the markers")
    assert merge.readiness(p, p.store.trial(t["id"]))["reason_code"] == "update_unchecked"
    again = merge.update_trial(p, t["id"])                                           # still checked out there
    assert again["status"] == "resolved" and calls["verify_bg"] == [t["id"]] and _events(p, did)[-1]["status"] == "resolved"
    assert p.git.current_branch() == "main" and _git(mine, "symbolic-ref", "--short", "HEAD").strip() == t["branch"]
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"]
    _commit_root(repo, {"h.txt": "h\n"}, "main moves on")
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, t["id"])                                               # an update that needs git: not in their checkout
    assert "checked out at %s" % mine in str(err.value)


def test_an_update_holds_its_trial_before_it_touches_git(repo, calls, monkeypatch):
    """Review 2026-11-02: update_trial opened its job only after the merge, so a build job sent meanwhile was given the same worktree;
    the update then could not open its own, and its abort reset the build agent's staged work and removed the worktree. Now the update
    job's row is opened first: a build job sent meanwhile is refused, and the update goes on."""
    p = _project(repo)
    did, t = _trial(p, repo, status="trying")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main edits the same line")
    real, refused = Git.update_branch, []

    def meanwhile(self, *args, **kw):
        res = real(self, *args, **kw)
        try:
            jobs.send(p, t["id"], "claude", "build it", by="user")                   # someone clicks "AI" on the page now
        except DecisionError as exc:
            refused.append(str(exc))
        return res
    monkeypatch.setattr(Git, "update_branch", meanwhile)
    u = merge.update_trial(p, t["id"])
    assert refused and "job %d" % u["job"]["id"] in refused[0]
    assert u["status"] == "conflicts" and calls["launched"] == [u["job"]["id"]] and p.git.merge_head(Path(u["worktree"]))
    assert [j["kind"] for j in p.store.jobs(tid=t["id"]) if j["status"] == "running"] == ["update"]
    assert _events(p, did)[-1]["job"] == u["job"]["id"]


def test_an_update_removes_only_a_worktree_it_made(repo, calls):
    """Review 2026-11-02: a clean update removed dtree's worktree of the trial although it had not made it — there someone had gone
    detached and committed, and that commit, only in the trial's shadow repository, was lost with it."""
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    wt = jobs._ensure_worktree(p, t["branch"])
    _git(wt, "checkout", "-q", "--detach")
    (wt / "h.txt").write_text("detached work\n")
    _git(wt, "add", "h.txt")
    _git(wt, "commit", "-q", "-m", "work only on a detached HEAD")
    lost = _sha(wt, "HEAD")
    _commit_root(repo, {"m.txt": "main\n"}, "main moves")
    u = merge.update_trial(p, t["id"])
    assert u["status"] == "clean" and u["how"] == "plumbing"
    assert wt.exists() and _sha(wt, "HEAD") == lost


def test_a_plumbing_update_that_loses_a_race_to_the_same_update_is_up_to_date(repo, monkeypatch):
    """Review 2026-11-02: two updates of one trial at once (a double click); the loser started over without asking whether the branch
    was up to date now, and made an empty second merge commit."""
    root, g = repo["path"], Git(repo["path"])
    _commit_on(repo, "alt/9-x", {"g.txt": "trial\n"}, "trial")
    _commit_root(repo, {"h.txt": "main\n"}, "main moves")
    real, done = Git.run, []

    def racing(self, *args, **kw):
        if args and args[0] == "update-ref" and not done:                           # the other update lands first
            done.append(1)
            monkeypatch.setattr(Git, "run", real)
            assert Git(root).update_branch("alt/9-x", "main")["state"] == "clean"
            monkeypatch.setattr(Git, "run", racing)
        return real(self, *args, **kw)
    monkeypatch.setattr(Git, "run", racing)
    res = g.update_branch("alt/9-x", "main")
    assert done and res["state"] == "up to date"
    assert len(_git(root, "log", "--merges", "--format=%H", "main..alt/9-x").split()) == 1


def test_a_clean_update_that_git_stops_without_a_conflict_is_an_error_not_a_job(repo, calls, monkeypatch):
    """Review 2026-11-02: when git merge stopped in the trial's worktree for a reason other than a conflict, update_trial sent an update
    job to "resolve" 0 files (a hook), or — exit 128, here a commit signature that fails — called it "unchanged" with the merge left in
    progress. Now such a merge is aborted and git's reason is the error; no job is sent."""
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    wt = jobs._ensure_worktree(p, t["branch"])                                        # a worktree kept from an earlier job (clean)
    tip = _sha(root, t["branch"])
    _commit_root(repo, {"h.txt": "h\n"}, "main moves")
    cfg = repo["tmp"] / "gitconfig"
    cfg.write_text("[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = false\n")         # the person signs commits; signing fails here
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, t["id"])
    assert "without a conflict" in str(err.value) and "aborted" in str(err.value) and "gpg" in str(err.value)
    assert calls["launched"] == [] and p.git.merge_head(wt) is None and _sha(root, t["branch"]) == tip and _events(p, did) == []
    assert _git(wt, "status", "--porcelain") == ""


def test_dtree_s_own_update_merge_runs_no_hook(repo, calls):
    """The plumbing update runs no hook, nor do the agents' commits in the sandbox; the update's git merge in a worktree ran them, and a
    commit-msg hook refusing git's message became an update job for 0 conflicting files (review 2026-11-02). dtree's own git there runs
    as jobs.SAFE_GIT says: no hooks, no fsmonitor."""
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    wt = repo["tmp"] / "wt" / t["branch"].replace("/", "-")
    _git(root, "worktree", "add", "-q", str(wt), t["branch"])                          # a worktree of the project itself (before shadows)
    _commit_root(repo, {"h.txt": "h\n"}, "main moves")
    hook = root / ".git" / "hooks" / "commit-msg"
    hook.write_text("#!/bin/sh\necho 'a ticket number is required' >&2\nexit 1\n")
    hook.chmod(0o755)
    u = merge.update_trial(p, t["id"])
    assert u["status"] == "clean" and u["how"] == "worktree" and calls["launched"] == []
    assert _git(root, "log", "-1", "--format=%P", t["branch"]).split() == [_sha(wt, "HEAD^1"), _sha(root, "main")]


def test_a_kept_worktree_behind_its_branch_is_brought_forward_before_an_update(repo, calls):
    """A shadow worktree kept from a job, while a person committed on the trial's branch in a checkout of their own: the update merged
    the base into the worktree's older commit, and bringing that back was refused (not a fast-forward)."""
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    wt = jobs._ensure_worktree(p, t["branch"])
    assert "/.shadow/" in _common(wt) and str(wt) not in _git(root, "worktree", "list")
    mine = _commit_on(repo, t["branch"], {"p.txt": "the person's\n"}, "the person's commit")
    base = _commit_root(repo, {"h.txt": "h\n"}, "main moves")
    u = merge.update_trial(p, t["id"])
    assert u["status"] == "clean" and u["how"] == "worktree"
    assert _git(root, "log", "-1", "--format=%P", t["branch"]).split() == [mine, base] and _sha(wt, "HEAD") == _sha(root, t["branch"])


def test_an_update_job_leaves_a_trial_that_was_never_built_trying(repo, calls):
    """Review 2026-11-02: an update job of a trial still being tried made it measured, so it could be adopted and merged unbuilt. The
    lead's jobs.run keeps a trying trial trying after an update."""
    p = _project(repo)
    did, t = _trial(p, repo, status="trying")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    assert jobs.run(p, u["job"]["id"])["status"] == "measured"
    tr = p.store.trial(t["id"])
    assert tr["status"] == "trying" and tr["result_tests"] is None and _events(p, did)[-1]["status"] == "resolved"


def test_an_abort_leaves_a_worktree_a_job_was_given_meanwhile(repo, calls, monkeypatch):
    """The same race as an update's: a job sent while the abort runs is given the trial's worktree, which the abort must not remove."""
    monkeypatch.setenv("FAKE_MODE", "give_up")
    p = _project(repo)
    did, t = _trial(p, repo, status="trying")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    assert jobs.run(p, u["job"]["id"])["status"] == "failed"
    wt = Path(u["worktree"])
    real = Git.abort_merge

    def meanwhile(self, *args, **kw):
        why = real(self, *args, **kw)
        jobs.send(p, t["id"], "claude", "build it", by="user")
        return why
    monkeypatch.setattr(Git, "abort_merge", meanwhile)
    a = merge.abort_update(p, t["id"])
    assert a["status"] == "aborted" and not a["removed"] and wt.exists() and p.git.merge_head(wt) is None


def test_ensure_worktree_prunes_a_worktree_deleted_by_hand(repo):
    """In the trial's shadow repository only: the project's own list keeps what others registered (git worktree prune of the project
    would clear, e.g., another tool's worktree entries whose directories are gone)."""
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="trying")
    _git(root, "branch", "someone-else", "main")
    theirs = repo["tmp"] / "someone-else"
    _git(root, "worktree", "add", "-q", str(theirs), "someone-else")
    shutil.rmtree(theirs)                                                             # prunable, and not dtree's
    wt = jobs._ensure_worktree(p, t["branch"])
    shutil.rmtree(wt)                                                                 # git still holds the branch for it
    assert jobs._ensure_worktree(p, t["branch"]) == wt and (wt / "f.txt").exists() and "/.shadow/" in _common(wt)
    assert "prunable" in _git(root, "worktree", "list", "--porcelain") and str(theirs) in _git(root, "worktree", "list")
    (repo["tmp"] / "wt" / "alt-1-elsewhere").mkdir()
    with pytest.raises(DecisionError):
        jobs._ensure_worktree(p, "alt/1-elsewhere")                                   # a directory that is not its worktree


# ----------------------------------------------------------------------------------------------------------- API and CLI
def test_routes_and_the_commands(repo, calls, capsys):
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    api = ApiClient(p)
    r = api.get("/api/decisions/trials/%d/merge-check" % t["id"]).get_json()
    assert r["ok"] and r["merge"]["can_merge"] and r["merge"]["reason_th"].startswith("พร้อมรวม")
    assert api.get("/api/decisions/trials/999/merge-check").status_code == 404
    assert api.post("/api/decisions/trials/%d/merge" % t["id"], json={"close": "yes"}).status_code == 400
    assert api.post("/api/decisions/%d/merge" % did).get_json()["error"].startswith("question %d has no branch" % did)
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = api.post("/api/decisions/trials/%d/update" % t["id"], json={}).get_json()
    assert u["ok"] and u["update"]["status"] == "clean" and "counts" in u
    assert api.post("/api/decisions/trials/%d/update/abort" % t["id"]).get_json()["error"].startswith("no update")
    m = api.post("/api/decisions/trials/%d/merge" % t["id"], json={"close": True}).get_json()
    assert m["ok"] and m["merge"]["how"] == "worktree" and m["closed"] and m["question"]["status"] == "done"
    (root / ".dtree").mkdir()
    (root / ".dtree" / "config.json").write_text(json.dumps({k: p.config[k] for k in ("name", "worktrees", "logs", "db", "agents")}))
    t2 = p.store.try_option(did, "left")
    _commit_on(repo, t2["branch"], {"k.txt": "k\n"}, "left again")
    with pytest.raises(SystemExit) as code:                                        # it cannot be merged now: exit 1 (E9, 2026-11-02)
        cli.main(["--project", str(root), "merge-check", str(t2["id"])])
    assert code.value.code == 1
    text = capsys.readouterr().out
    assert "%s → main: clean · 0 behind, 1 ahead" % t2["branch"] in text and "can merge: no — trial %d is trying" % t2["id"] in text
    with pytest.raises(SystemExit):
        cli.main(["--project", str(root), "merge-check", str(t2["id"]), "--json"])
    assert json.loads(capsys.readouterr().out)["reason_code"] == "not_adopted"
    _commit_root(repo, {"m.txt": "m\n"}, "main once more")
    assert cli.main(["--project", str(root), "update", str(t2["id"])]) == 0
    assert "updated with main" in capsys.readouterr().out
    assert cli.main(["--project", str(root), "update", str(t2["id"]), "--abort"]) == 1
    assert "no update of trial" in capsys.readouterr().err
    p.store.trial_result(t2["id"], "1 passed", "left again")
    p.store.trial_end(t2["id"], "adopted")
    assert cli.main(["--project", str(root), "merge", str(did), "--trial", str(t2["id"]), "--close"]) == 0
    out = capsys.readouterr().out
    assert "merged %s into main" % t2["branch"] in out and "#%d is done" % did in out and p.git.current_branch() == "main"


def test_a_merge_that_could_not_take_the_index_lock_is_not_sent_as_a_conflict(repo, calls, monkeypatch):
    """Check of 2026-11-02: when another git held the worktree's index.lock during dtree's update merge and let go before dtree looked,
    git ended with exit 1 and MERGE_HEAD but wrote nothing. Taken as a rerere resolution, the job got an index without the base's
    changes: its merge commit would have dropped them. Decided from what is staged, never from whether a lock is there."""
    from dtree.gitcode import Git
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n", "m.txt": "main's other change\n"}, "main edits f.txt and adds m.txt")
    orig = Git._git_merge

    def held(self, wt, args, opts=()):
        d = self.worktree_dir(wt)
        if "--no-ff" not in args or d is None:
            return orig(self, wt, args, opts)
        lock = d / "index.lock"
        lock.write_text("")
        try:
            return orig(self, wt, args, opts)
        finally:
            lock.unlink()
    monkeypatch.setattr(Git, "_git_merge", held)
    with pytest.raises(DecisionError):
        merge.update_trial(p, t["id"])
    assert calls["launched"] == [] and [e["status"] for e in _events(p, did)] in ([], ["conflicts", "aborted"])


def test_plain_commits_a_dead_runner_left_in_the_shadow_are_checked_before_an_update_takes_them(repo, calls, monkeypatch):
    """Check of 2026-11-02: an update job's agent dropped the merge and committed plainly, then its runner died before bringing the
    branch back. dtree update brought the commits back and merged the base over them in one call, unchecked."""
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    wt = Path(u["worktree"])
    _git(wt, "reset", "-q", "--hard")
    (wt / "f.txt").write_text("one\nmain\n")
    _git(wt, "commit", "-q", "-am", "take main to make it merge")
    p.store.job_end(u["job"]["id"], "failed", "the runner is gone")
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, t["id"])
    assert "brought back" in str(err.value)
    assert merge.readiness(p, p.store.trial(t["id"]))["reason_code"] == "update_unchecked"


def test_a_hand_merge_of_a_newer_base_with_markers_is_not_taken(repo, calls, monkeypatch):
    """Check of 2026-11-02: `dtree update` took a person's hand merge of a newer base as it was, conflict markers included."""
    monkeypatch.setenv("FAKE_MODE", "give_up")
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted")
    _commit_root(repo, {"f.txt": "one\nmain\n"}, "main")
    u = merge.update_trial(p, t["id"])
    assert jobs.run(p, u["job"]["id"])["status"] == "failed"
    wt = Path(u["worktree"])
    _git(wt, "merge", "--abort")
    _git(wt, "commit", "-q", "--allow-empty", "-m", "a note before redoing it")
    jobs._sync_back(p, t["branch"], wt)
    jobs._remove_worktree(p, wt)
    _commit_root(repo, {"h.txt": "h\n"}, "main moves on")
    mine = repo["tmp"] / "mine"
    _git(root, "worktree", "add", "-q", str(mine), t["branch"])
    _git(mine, "merge", "-q", "main", check=False)
    _git(mine, "commit", "-q", "-am", "merge main (markers left)")
    with pytest.raises(DecisionError) as err:
        merge.update_trial(p, t["id"])
    assert "markers" in str(err.value)
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"] is False


def test_not_brought_back_says_to_switch_a_person_s_checkout_away_first(repo, calls):
    root = repo["path"]
    p = _project(repo)
    did, t = _trial(p, repo, status="adopted", files={"g.txt": "right\n"})
    wt = jobs._ensure_worktree(p, t["branch"])
    (wt / "g.txt").write_text("right, more\n")
    _git(wt, "commit", "-qam", "never brought back")
    mine = repo["tmp"] / "mine"
    _git(root, "worktree", "add", "-q", str(mine), t["branch"])
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert r["reason_code"] == "not_brought_back" and ("switch %s away" % mine) in r["reason"]
