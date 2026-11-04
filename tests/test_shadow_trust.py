"""What a job writes into its trial's shadow repository never reaches dtree's own git on the host (F0, review of 2026-11-02), and a
shadow whose worktree was deleted by hand keeps the commits that were never brought back (F2).

F0: Codex's own sandbox lets it write the shadow's whole git directory (``--add-dir``): its config (a clean / smudge filter, a merge
driver, ``gpg.program`` with ``commit.gpgSign``, ``core.hooksPath``), its hooks and ``info/attributes``, a ``config.worktree``, and the
worktree's ``.git`` file or its admin ``commondir`` (git then reads a repository of the job's making). dtree's own git there —
``git status`` after the job and in every readiness poll, the merge of an update, ``git merge --abort``, ``worktree add`` — ran those
commands on the host, outside any sandbox. Every command a stand-in agent plants here touches a file in ``mark``: nothing may.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dtree import jobs, merge, sandbox
from dtree.project import Project
from dtree.store import DecisionError
from test_agents import FakeModel, Q, _trial

TESTS, SRC = str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / "src")
HOOKS = ("post-checkout", "post-merge", "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-merge-commit",
         "reference-transaction", "post-rewrite", "pre-auto-gc")
EVERYTHING = ("config", "attributes", "hooks")


def plant(gd, wt, mark, kinds):
    """What a job could leave in its shadow repository ``gd`` and its worktree ``wt``; each command it plants touches a file in ``mark``.
    ``kinds``: config (filters, a merge driver, a signing program, a hooks path, config.worktree), attributes (info/attributes), hooks,
    dotgit (``.git`` names the job's own repository), commondir (the admin dir's and the shadow's ``commondir`` name the job's own)."""
    gd, wt, mark = Path(gd), Path(wt), Path(mark)
    admin = Path((wt / ".git").read_text().split("gitdir:", 1)[1].strip())
    branch = (admin / "HEAD").read_text().strip()[len("ref: "):]

    def run(name, tail="cat"):
        return "sh -c 'touch %s; %s'" % (mark / name, tail)

    def git(*a):
        subprocess.run(["git"] + [str(x) for x in a], check=True, capture_output=True)

    def script(path, name):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\ntouch %s\nexit 1\n" % (mark / name))
        path.chmod(0o755)

    if "config" in kinds:
        script(gd / "evil-gpg", "gpg-program")
        for key, value in (("filter.x.clean", run("filter-clean")), ("filter.x.smudge", run("filter-smudge")),
                           ("merge.x.driver", run("merge-driver", "exit 1")), ("diff.x.textconv", run("textconv")),
                           ("commit.gpgSign", "true"), ("gpg.program", gd / "evil-gpg"), ("core.hooksPath", gd / "evil-hooks"),
                           ("extensions.worktreeConfig", "true")):
            git("config", "-f", gd / "config", key, value)
        for c in (gd / "config.worktree", admin / "config.worktree"):
            git("config", "-f", c, "filter.w.clean", run("worktree-config-filter"))
        git("config", "-f", admin / "config.worktree", "core.bare", "false")    # else the shared core.bare=true holds for the worktree too
        (wt / ".gitattributes").write_text("* merge=x diff=x\n*.txt filter=x\n*.md filter=w\n")
    if "attributes" in kinds:
        (gd / "info").mkdir(exist_ok=True)
        (gd / "info" / "attributes").write_text("* merge=x diff=x\n*.txt filter=x\n")
    if "hooks" in kinds:
        for d in (gd / "hooks", gd / "evil-hooks"):
            for name in HOOKS:
                script(d / name, "hook-" + name)
    if "dotgit" in kinds:
        evil = wt / ".evil"
        shutil.rmtree(evil, ignore_errors=True)                  # planted again: its own filter must not run while it is made
        git("init", "-q", evil)
        git("--git-dir", evil / ".git", "--work-tree", wt, "add", "f.txt")
        git("--git-dir", evil / ".git", "symbolic-ref", "HEAD", branch)
        git("config", "-f", evil / ".git" / "config", "filter.y.clean", run("dotgit-filter"))
        (evil / ".git" / "info").mkdir(exist_ok=True)
        (evil / ".git" / "info" / "attributes").write_text("* filter=y\n")
        (wt / ".git").write_text("gitdir: %s\n" % (evil / ".git"))
    if "commondir" in kinds:
        evil = wt / ".evilcommon"
        shutil.rmtree(evil, ignore_errors=True)
        git("init", "-q", "--bare", evil)
        git("config", "-f", evil / "config", "filter.z.clean", run("commondir-filter"))
        (evil / "info").mkdir(exist_ok=True)
        (evil / "info" / "attributes").write_text("* filter=z\n")
        for name in HOOKS:
            script(evil / "hooks" / name, "commondir-hook-" + name)
        (admin / "commondir").write_text("%s\n" % evil)
        (gd / "commondir").write_text("%s\n" % evil)
    later = time.time() + 30                     # stat-dirty, same content: git status must read them again (through a clean filter)
    for f in ("f.txt", "g.txt", "notes.md"):
        if (wt / f).exists():
            os.utime(wt / f, (later, later))


EVIL_CODEX = r'''#!%(python)s
import json, os, subprocess, sys
sys.path[:0] = [%(tests)r, %(src)r]
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
gd, last = args[args.index("--add-dir") + 1], args[args.index("-o") + 1]
sys.stdin.read()
def ev(d):
    print(json.dumps(d), flush=True)
ev({"type": "thread.started", "thread_id": "t1"})
open("g.txt", "w").write("right\n"); open("notes.md", "w").write("# right\n")
subprocess.run(["git", "add", "g.txt", "notes.md"], check=True); subprocess.run(["git", "commit", "-q", "-m", "codex builds right"], check=True)
from test_shadow_trust import plant
plant(gd, os.getcwd(), os.environ["FAKE_MARK"], os.environ["FAKE_PLANT"].split(","))
open("left.txt", "w").write("not committed\n")
ev({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
open(last, "w").write(json.dumps({"built": True, "tests": "1 passed", "impact": "right", "note": ""}))
'''

UPDATER = r'''#!%s
import json, subprocess, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "u1", "model": "fake"}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "total_cost_usd": 0.0, "session_id": "u1",
                  "structured_output": {"built": False, "tests": "-", "impact": "-", "note": "gave up"}}), flush=True)
'''


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd)] + [str(a) for a in args], capture_output=True, text=True, check=check).stdout


def _ran(mark):
    return sorted(os.listdir(mark))


def _project(repo, **extra):
    updater = repo["tmp"] / "updater"
    updater.write_text(UPDATER % sys.executable)
    updater.chmod(0o755)
    cfg = {"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"), "db": str(repo["tmp"] / "d.sqlite"),
           "agents": {"claude": {"command": str(updater), "sandbox": "none"}}, "verify": {"enabled": False}}
    cfg.update(extra)
    return Project(repo["path"], cfg)


@pytest.fixture
def kept(repo, monkeypatch):
    """An adopted trial whose worktree dtree kept (in its shadow repository) after a job built it: g.txt and notes.md, brought back."""
    monkeypatch.setattr(jobs, "launch", lambda proj, jid: 999999)            # update jobs are opened, never run here
    real = jobs._alive
    monkeypatch.setattr(jobs, "_alive", lambda pid, marker="": pid == 999999 or real(pid, marker))
    p = _project(repo)
    did, t = _trial(p)
    wt = jobs._ensure_worktree(p, t["branch"])
    (wt / "g.txt").write_text("right\n")
    (wt / "notes.md").write_text("# right\n")
    _git(wt, "add", "g.txt", "notes.md")
    _git(wt, "commit", "-q", "-m", "the agent's work")
    assert jobs._sync_back(p, t["branch"], wt) is None
    p.store.record_code(did, p.git.commits_between("main", t["branch"]), trial=t["id"])
    p.store.trial_result(t["id"], "[claude] 1 passed", "right")
    p.store.trial_end(t["id"], "adopted")
    gd = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    mark = repo["tmp"] / "ran"
    mark.mkdir()
    return {"p": p, "did": did, "t": p.store.trial(t["id"]), "wt": wt, "gd": gd, "mark": mark, "repo": repo}


def _on_branch(repo, branch, files, msg):
    """A commit on ``branch`` in the project, made in a throwaway worktree of the project (dtree's kept one is the shadow's)."""
    other = repo["tmp"] / "other"
    _git(repo["path"], "worktree", "add", "-q", other, branch)
    for name, text in files.items():
        (other / name).write_text(text)
    _git(other, "add", *files)
    _git(other, "commit", "-q", "-m", msg)
    _git(repo["path"], "worktree", "remove", "--force", other)


def _assert_trusted(k):
    """The shadow and the worktree as dtree made them: nothing the plant left that git would run or follow."""
    gd, wt = k["gd"], k["wt"]
    assert subprocess.run(["git", "config", "-f", str(gd / "config"), "--get-regexp", r"^(filter|merge|diff|gpg|commit|extensions)\.|hookspath"],
                          capture_output=True, text=True).stdout == ""
    assert not any((gd / "hooks").glob("*")) and not (gd / "info" / "attributes").exists() and not (gd / "config.worktree").exists()
    assert not (gd / "commondir").exists()
    if wt.exists():
        admin = Path((wt / ".git").read_text().split("gitdir:", 1)[1].strip())
        assert admin.parent == gd / "worktrees" and (admin / "commondir").read_text().strip() == "../.."
        assert not (admin / "config.worktree").exists()


# ------------------------------------------------------------------------------------------------------- F0: the stand-in Codex
@pytest.mark.parametrize("kinds", [EVERYTHING, ("dotgit",), ("commondir",)])
def test_a_codex_job_that_plants_commands_in_its_shadow_runs_none_of_them_on_the_host(repo, monkeypatch, kinds):
    """Through the real CodexAgent: the job builds, commits, plants, and leaves a file uncommitted (so dtree keeps its worktree and its
    git status reads the files). Then the person adopts it, the compare table polls readiness, and an update merges a new base commit
    in the kept worktree (a merge commit: signing, smudge of the new file, hooks)."""
    mark = repo["tmp"] / "ran"
    mark.mkdir()
    exe = repo["tmp"] / "codex"
    exe.write_text(EVIL_CODEX % {"python": sys.executable, "tests": TESTS, "src": SRC})
    exe.chmod(0o755)
    monkeypatch.setenv("FAKE_MARK", str(mark))
    monkeypatch.setenv("FAKE_PLANT", ",".join(kinds))
    p = _project(repo, agents={"codex": {"command": str(exe)}}, default_agent="codex")
    did, t = _trial(p)
    out = jobs.run(p, p.store.job_open(t["id"], agent="codex")["id"])
    assert _ran(mark) == [], "after the job, dtree's own git ran what the job planted: %s" % _ran(mark)
    assert out["status"] == "measured", out["reason"]
    wt = Path(out["worktree"])
    assert wt.exists() and "codex builds right" in _git(repo["path"], "log", "-1", "--format=%s", t["branch"])
    log = "\n".join(jobs.log_tail(out))
    assert "put back" in log, log                                           # the job's log says what it had changed there
    p.store.trial_end(t["id"], "adopted")
    merge.readiness(p, p.store.trial(t["id"]))
    assert _ran(mark) == []
    (repo["path"] / "n.txt").write_text("from main\n")
    _git(repo["path"], "add", "n.txt")
    _git(repo["path"], "commit", "-q", "-m", "main moves on")
    gd = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    plant(gd, wt, mark, kinds)                                             # again: as if something the job left running wrote it once more
    u = merge.update_trial(p, t["id"])
    assert _ran(mark) == [], "the update ran what the job planted: %s" % _ran(mark)
    assert u["status"] == "clean" and _git(repo["path"], "show", "%s:n.txt" % t["branch"]) == "from main\n"


# ------------------------------------------------------------------------------------------------------- F0: each of dtree's own commands
@pytest.mark.parametrize("kinds", [EVERYTHING, ("dotgit",), ("commondir",)])
def test_readiness_runs_nothing_planted_in_the_shadow(kept, kinds):
    plant(kept["gd"], kept["wt"], kept["mark"], kinds)
    merge.readiness(kept["p"], kept["t"])
    merge.compare_fields(kept["p"], kept["t"])
    assert _ran(kept["mark"]) == []
    _assert_trusted(kept)


@pytest.mark.parametrize("kinds", [EVERYTHING, ("dotgit",), ("commondir",)])
def test_a_kept_worktree_brought_forward_runs_nothing_planted(kept, kinds):
    _on_branch(kept["repo"], kept["t"]["branch"], {"p.txt": "the person's\n"}, "the person's commit")
    plant(kept["gd"], kept["wt"], kept["mark"], kinds)
    assert jobs._ensure_worktree(kept["p"], kept["t"]["branch"]) == kept["wt"]
    assert _ran(kept["mark"]) == []
    assert (kept["wt"] / "p.txt").read_text() == "the person's\n"
    _assert_trusted(kept)


@pytest.mark.parametrize("kinds", [EVERYTHING, ("dotgit",), ("commondir",)])
def test_an_update_in_the_kept_worktree_runs_nothing_planted(kept, kinds):
    root = kept["repo"]["path"]
    (root / "n.txt").write_text("from main\n")
    _git(root, "add", "n.txt")
    _git(root, "commit", "-q", "-m", "main moves on")
    plant(kept["gd"], kept["wt"], kept["mark"], kinds)
    u = merge.update_trial(kept["p"], kept["t"]["id"])
    assert _ran(kept["mark"]) == []
    assert u["status"] == "clean" and u["how"] == "worktree"
    _assert_trusted(kept)


@pytest.mark.parametrize("kinds", [EVERYTHING, ("dotgit",), ("commondir",)])
def test_an_aborted_update_runs_nothing_planted(kept, kinds):
    root = kept["repo"]["path"]
    (root / "g.txt").write_text("main's g\n")                                # add/add: the update conflicts and a job is sent
    _git(root, "add", "g.txt")
    _git(root, "commit", "-q", "-m", "main adds g.txt too")
    u = merge.update_trial(kept["p"], kept["t"]["id"])
    assert u["status"] == "conflicts"
    kept["p"].store.job_end(u["job"]["id"], "failed", "the agent gave up")
    plant(kept["gd"], kept["wt"], kept["mark"], kinds)
    a = merge.abort_update(kept["p"], kept["t"]["id"])
    assert _ran(kept["mark"]) == []
    assert a["status"] == "aborted"
    _assert_trusted(kept)


@pytest.mark.parametrize("kinds", [EVERYTHING, ("commondir",)])
def test_a_worktree_made_again_from_a_kept_shadow_runs_nothing_planted(kept, kinds):
    """worktree add (post-checkout, smudge) and update-ref (reference-transaction) run in the shadow when its worktree is made again."""
    plant(kept["gd"], kept["wt"], kept["mark"], kinds)
    shutil.rmtree(kept["wt"])
    assert jobs._ensure_worktree(kept["p"], kept["t"]["branch"]) == kept["wt"]
    assert _ran(kept["mark"]) == []
    assert (kept["wt"] / "g.txt").read_text() == "right\n"
    _assert_trusted(kept)


@pytest.mark.parametrize("kinds", [EVERYTHING, ("dotgit",), ("commondir",)])
def test_bringing_the_branch_back_and_removing_the_worktree_follow_dtree_s_repository(kept, kinds):
    wt = kept["wt"]
    (wt / "h.txt").write_text("more\n")
    _git(wt, "add", "h.txt")
    _git(wt, "commit", "-q", "-m", "more work")
    last = _git(wt, "rev-parse", "HEAD").strip()
    plant(kept["gd"], wt, kept["mark"], kinds)
    assert jobs._sync_back(kept["p"], kept["t"]["branch"], wt) is None        # from the shadow, not from the job's own repository
    assert _git(kept["repo"]["path"], "rev-parse", kept["t"]["branch"]).strip() == last
    assert jobs._remove_worktree(kept["p"], wt)
    assert _ran(kept["mark"]) == []
    assert not wt.exists() and not kept["gd"].exists() and not list(kept["gd"].parent.glob("*.pin.json"))


def test_a_link_where_dtree_keeps_a_directory_shuts_the_gate(kept, tmp_path):
    """A symbolic link in the shadow is nothing git or dtree makes there: dtree follows none and writes through none."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "attributes").write_text("keep me\n")
    shutil.rmtree(kept["gd"] / "info", ignore_errors=True)
    os.symlink(elsewhere, kept["gd"] / "info")
    r = merge.readiness(kept["p"], kept["t"])
    assert not r["can_merge"] and r["reason_code"] == "untrusted" and "info" in r["reason"]
    assert (elsewhere / "attributes").read_text() == "keep me\n"
    with pytest.raises(DecisionError):
        merge.update_trial(kept["p"], kept["t"]["id"])
    assert (elsewhere / "attributes").read_text() == "keep me\n"


@pytest.mark.skipif(not sandbox.check()[0], reason="bwrap cannot run here")
def test_the_pin_is_outside_what_a_sandboxed_job_can_write(repo):
    """dtree keeps what it made beside the shadow, where a job's sandbox reaches nothing: a write there lands, if anywhere, in the
    sandbox's own tmpfs (the directories bwrap makes around its mounts), never on the host."""
    shadows = repo["tmp"] / "wt" / ".shadow"
    pin = shadows / "alt-1-right.pin.json"
    attack = "; ".join(["echo x >> %s && echo WROTE-PIN" % pin, "touch %s/new.pin.json && echo NEW-PIN" % shadows,
                        "echo right > g.txt && git add g.txt && git commit -q -m mine && echo COMMITTED"])
    model = FakeModel([{"calls": [("run", {"command": attack})]}, {"calls": [("finish", {"built": True, "tests": "-", "impact": "right", "note": ""})]}])
    p = _project(repo, agents={"local": {"kind": "chat", "base_url": model.url, "model": "tiny"}}, default_agent="local",
                 chat={"sandbox": "bwrap", "max_steps": 5})
    did, t = _trial(p)
    assert t["branch"] == "alt/1-right"
    jid = p.store.job_open(t["id"], agent="local")["id"]
    orig = jobs.agents.make(p.agent("local"), p).__class__.run
    seen = {}

    def run_and_look(self, prompt, ctx):
        seen["pin"] = pin.read_text()                                         # made with the shadow, before the agent ran
        return orig(self, prompt, ctx)
    jobs.agents.make(p.agent("local"), p).__class__.run = run_and_look
    try:
        out = jobs.run(p, jid)
    finally:
        jobs.agents.make(p.agent("local"), p).__class__.run = orig
    said = [m["content"] for r in model.requests for m in r["body"]["messages"] if m.get("role") == "tool"][-1]
    assert "COMMITTED" in said, said
    assert out["status"] == "measured", out["reason"]
    kept = json.loads(seen["pin"])
    assert kept["branch"] == t["branch"] and "config" in kept["shadow"]
    assert not (shadows / "new.pin.json").exists()                            # the job's writes never reached the host
    assert not pin.exists() or pin.read_text() == seen["pin"]                 # (removed with the shadow after the clean job)


# ------------------------------------------------------------------------------------------------------- F2: a worktree deleted by hand
def test_a_worktree_deleted_by_hand_does_not_lose_the_commits_only_its_shadow_holds(kept):
    """Probe A of the review: readiness said ready once the directory was gone, and the next job's start reset the shadow's branch to the
    project's, so a later clean job pruned the commits for good."""
    p, t, wt, root = kept["p"], kept["t"], kept["wt"], kept["repo"]["path"]
    (wt / "h.txt").write_text("never brought back\n")
    _git(wt, "add", "h.txt")
    _git(wt, "commit", "-q", "-m", "work a runner never brought back")
    lost = _git(wt, "rev-parse", "HEAD").strip()
    assert merge.readiness(p, t)["reason_code"] == "not_brought_back"
    shutil.rmtree(wt)                                                         # "cleaning up"
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert not r["can_merge"] and r["reason_code"] == "not_brought_back", r["reason"]
    assert str(kept["gd"]) in r["reason"] and "dtree update %d" % t["id"] in r["reason"]
    with pytest.raises(DecisionError):
        merge.merge_trial(p, t["id"], close=True)
    assert jobs._ensure_worktree(p, t["branch"]) == wt                       # a job's start brings them back first
    assert _git(root, "rev-parse", t["branch"]).strip() == lost and _git(wt, "rev-parse", "HEAD").strip() == lost
    assert merge.readiness(p, p.store.trial(t["id"]))["can_merge"]


def test_an_update_brings_back_what_only_the_shadow_holds(kept):
    p, t, wt, root = kept["p"], kept["t"], kept["wt"], kept["repo"]["path"]
    (wt / "h.txt").write_text("never brought back\n")
    _git(wt, "add", "h.txt")
    _git(wt, "commit", "-q", "-m", "work a runner never brought back")
    lost = _git(wt, "rev-parse", "HEAD").strip()
    shutil.rmtree(wt)
    (root / "n.txt").write_text("from main\n")
    _git(root, "add", "n.txt")
    _git(root, "commit", "-q", "-m", "main moves on")
    u = merge.update_trial(p, t["id"])
    assert u["status"] == "clean"
    assert subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", lost, t["branch"]]).returncode == 0
    assert lost in [c["sha"] for c in p.store.trial(t["id"])["commits"]]


def test_a_shadow_gone_separate_ways_is_never_reset(kept):
    p, t, wt, root = kept["p"], kept["t"], kept["wt"], kept["repo"]["path"]
    (wt / "h.txt").write_text("only in the shadow\n")
    _git(wt, "add", "h.txt")
    _git(wt, "commit", "-q", "-m", "only in the shadow")
    mine = _git(wt, "rev-parse", "HEAD").strip()
    shutil.rmtree(wt)
    _on_branch(kept["repo"], t["branch"], {"p.txt": "the person's\n"}, "the person's commit")
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert not r["can_merge"] and r["reason_code"] == "not_brought_back" and "separate ways" in r["reason"]
    with pytest.raises(DecisionError) as err:
        jobs._ensure_worktree(p, t["branch"])
    assert "separate ways" in str(err.value)
    assert _git(kept["gd"], "rev-parse", "refs/heads/" + t["branch"]).strip() == mine          # the shadow's branch is kept as it was


def test_tidying_never_removes_a_shadow_that_holds_commits_the_project_lacks(kept):
    """Probe B: after an abort, _tidy removed the shadow repository with commits a person had made in dtree's kept worktree."""
    p, t, wt = kept["p"], kept["t"], kept["wt"]
    (wt / "h.txt").write_text("a person's hand commit\n")
    _git(wt, "add", "h.txt")
    _git(wt, "commit", "-q", "-m", "a person's hand commit")
    mine = _git(wt, "rev-parse", "HEAD").strip()
    assert merge._tidy(p, wt) is False and jobs._remove_worktree(p, wt) is False
    assert wt.exists() and _git(kept["gd"], "rev-parse", "refs/heads/" + t["branch"]).strip() == mine


def test_a_worktree_of_the_project_itself_at_dtree_s_place_is_left_alone(kept):
    """A person deleted dtree's kept worktree and made one of the project itself at the same place: its .git is theirs (the project's
    own admin directory names it back, which no job can write), not dtree's to put back."""
    p, t, wt, root = kept["p"], kept["t"], kept["wt"], kept["repo"]["path"]
    shutil.rmtree(wt)
    _git(root, "worktree", "add", "-q", wt, t["branch"])
    theirs = (wt / ".git").read_text()
    assert jobs.trust(p, wt) == [] and (wt / ".git").read_text() == theirs
    merge.readiness(p, p.store.trial(t["id"]))
    assert (wt / ".git").read_text() == theirs
    assert Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()) == (root / ".git").resolve()
