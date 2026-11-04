"""A job works in the trial's own shadow repository (jobs._shadow): the project's repository is read-only in the sandbox, and dtree
brings the branch back itself, only fast-forward and with every object checked. Review of 2026-11-01: with the project's git directory
writable, a job could move the base branch, the main checkout's HEAD, or write what git on the host runs later."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dtree import jobs, sandbox
from dtree.project import Project
from test_agents import FakeModel, Q, _trial

pytestmark = pytest.mark.skipif(not sandbox.check()[0], reason="bwrap cannot run here")


def _project(repo, model, **extra):
    cfg = {"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"), "db": str(repo["tmp"] / "d.sqlite"),
           "agents": {"local": {"kind": "chat", "base_url": model.url, "model": "tiny"}}, "default_agent": "local",
           "verify": {"enabled": False}, "chat": {"sandbox": "bwrap", "max_steps": 10}}
    cfg.update(extra)
    return Project(repo["path"], cfg)


def _run(p, tid):
    return jobs.run(p, p.store.job_open(tid, agent="local")["id"])


def test_a_job_commits_in_its_shadow_and_the_branch_comes_back(repo):
    model = FakeModel([{"calls": [("run", {"command": "echo right > g.txt && git add g.txt && git commit -q -m 'build right' && echo ok"})]},
                       {"calls": [("finish", {"built": True, "tests": "-", "impact": "right", "note": ""})]}])
    p = _project(repo, model)
    did, t = _trial(p)
    out = _run(p, t["id"])
    assert out["status"] == "measured", out["reason"]
    assert repo["git"]("log", "-1", "--format=%s", t["branch"]).strip() == "build right"
    assert not (repo["tmp"] / "wt" / ".shadow").exists() or not any((repo["tmp"] / "wt" / ".shadow").iterdir())   # gone with its worktree
    tool = [m for r in model.requests for m in r["body"]["messages"] if m.get("role") == "tool"]
    assert any("ok" in m["content"] for m in tool)


def test_from_its_shadow_a_job_cannot_touch_the_project_s_repository(repo):
    gd = repo["path"] / ".git"
    main_before = repo["git"]("rev-parse", "main").strip()
    attack = "; ".join([
        "git update-ref refs/heads/main HEAD",                               # the shadow's own main: harmless, never brought back
        "git --git-dir=%s update-ref refs/heads/main HEAD 2>/dev/null && echo MOVED-MAIN" % gd,
        "(echo '[core]' >> %s/config) 2>/dev/null && echo WROTE-CONFIG" % gd,
        "(mkdir -p %s/hooks && touch %s/hooks/post-commit) 2>/dev/null && echo WROTE-HOOK" % (gd, gd),
        "(echo ref: refs/heads/x > %s/HEAD) 2>/dev/null && echo WROTE-HEAD" % gd,
        "(echo x > %s/refs/heads/evil) 2>/dev/null && echo NEW-REF" % gd,
        "sg=$(git rev-parse --path-format=absolute --git-common-dir)",            # the shadow: where its objects come from stays fixed
        "(echo /tmp >> $sg/objects/info/alternates) 2>/dev/null && echo WROTE-ALT",
        "(mkdir -p $sg/info && touch $sg/info/attributes) 2>/dev/null && echo MADE-ATTR",
        "echo right > g.txt && git add g.txt && git commit -q -m mine && echo COMMITTED",
    ])
    model = FakeModel([{"calls": [("run", {"command": attack})]},
                       {"calls": [("finish", {"built": True, "tests": "-", "impact": "right", "note": ""})]}])
    p = _project(repo, model)
    did, t = _trial(p)
    out = _run(p, t["id"])
    said = [m["content"] for r in model.requests for m in r["body"]["messages"] if m.get("role") == "tool"][-1]
    assert "COMMITTED" in said and not any(w in said for w in ("MOVED-MAIN", "WROTE-CONFIG", "WROTE-HOOK", "WROTE-HEAD", "NEW-REF", "WROTE-ALT", "MADE-ATTR")), said
    assert out["status"] == "measured", out["reason"]
    assert repo["git"]("rev-parse", "main").strip() == main_before and not (gd / "refs" / "heads" / "evil").exists()
    assert not (gd / "hooks" / "post-commit").exists() and repo["git"]("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_a_job_that_rewrites_its_branch_history_is_not_brought_back(repo):
    model = FakeModel([{"calls": [("run", {"command": "echo a > g.txt && git add g.txt && git commit -q -m first-try && echo ok"})]},
                       {"calls": [("finish", {"built": True, "tests": "-", "impact": "x", "note": ""})]},
                       {"calls": [("run", {"command": "git reset -q --soft HEAD~2 2>/dev/null || git commit -q --amend -m rewritten; echo b > g.txt; git add g.txt; git commit -q --amend -m rewritten && echo ok"})]},
                       {"calls": [("finish", {"built": True, "tests": "-", "impact": "y", "note": ""})]}])
    p = _project(repo, model)
    did, t = _trial(p)
    first = _run(p, t["id"])
    assert first["status"] == "measured", first["reason"]
    tip = repo["git"]("rev-parse", t["branch"]).strip()
    p.store.trial_followup(t["id"], "again")
    second = _run(p, t["id"])
    assert second["status"] == "failed" and "brought back" in second["reason"]
    assert repo["git"]("rev-parse", t["branch"]).strip() == tip                       # the project's branch kept its history


def test_the_person_s_own_commits_on_the_base_during_a_job_are_left_alone(repo):
    model = FakeModel([{"calls": [("run", {"command": "echo right > g.txt && git add g.txt && git commit -q -m mine && echo ok"})]},
                       {"calls": [("finish", {"built": True, "tests": "-", "impact": "right", "note": ""})]}])
    p = _project(repo, model)
    did, t = _trial(p)
    jid = p.store.job_open(t["id"], agent="local")["id"]
    orig = jobs.agents.make(p.agent("local"), p).__class__.run

    def run_and_commit_meanwhile(self, prompt, ctx):
        (repo["path"] / "person.txt").write_text("mine\n")
        repo["git"]("add", "person.txt"); repo["git"]("commit", "-q", "-m", "the person's work")
        return orig(self, prompt, ctx)
    jobs.agents.make(p.agent("local"), p).__class__.run = run_and_commit_meanwhile
    try:
        out = jobs.run(p, jid)
    finally:
        jobs.agents.make(p.agent("local"), p).__class__.run = orig
    assert out["status"] == "measured", out["reason"]
    assert repo["git"]("log", "-1", "--format=%s", "main").strip() == "the person's work"


def test_a_nested_repository_s_filter_never_runs_in_dtree_s_own_git_commands(repo, tmp_path):
    """Re-review of 2026-11-02: a job could commit a nested repository (a gitlink) whose own config defines a clean filter; the runner's
    `git status` after the job looked into it and ran the filter on the host."""
    p = Project(repo["path"], {"worktrees": str(tmp_path / "wt")})
    did = p.store.add(Q)["id"]
    t = p.store.try_option(did, "right")
    wt = jobs._ensure_worktree(p, t["branch"])
    marker = tmp_path / "RAN"
    sub = wt / "sub"
    def git(*a, cwd):
        return subprocess.run(["git", "-C", str(cwd)] + list(a), capture_output=True, text=True)
    git("init", "-q", str(sub), cwd=wt)
    git("config", "user.email", "t@x", cwd=sub); git("config", "user.name", "t", cwd=sub)
    git("config", "filter.x.clean", "touch %s; cat" % marker, cwd=sub)
    (sub / ".gitattributes").write_text("* filter=x\n"); (sub / "f").write_text("a\n")
    git("add", ".", cwd=sub); git("commit", "-q", "-m", "s", cwd=sub)
    git("add", "sub", cwd=wt); git("commit", "-q", "-m", "gitlink", cwd=wt)
    (sub / "f").write_text("dirty\n")
    marker.unlink(missing_ok=True)
    p.git.run(*jobs.SAFE_GIT, "status", "--porcelain", cwd=wt)
    p.git.run("status", "--porcelain", "-z", "--untracked-files=no", cwd=wt)
    p.git.dirty(wt)
    assert not marker.exists()


def test_a_kept_worktree_is_never_brought_forward_over_files_git_does_not_track(repo, tmp_path):
    p = Project(repo["path"], {"worktrees": str(tmp_path / "wt")})
    did = p.store.add(Q)["id"]
    t = p.store.try_option(did, "right")
    wt = jobs._ensure_worktree(p, t["branch"])                                    # kept from an earlier job
    other = tmp_path / "other"                                                    # meanwhile the project's branch gains new.log
    repo["git"]("worktree", "add", "-q", str(other), t["branch"])
    (other / "new.log").write_text("theirs\n")
    subprocess.run(["git", "-C", str(other), "add", "-f", "new.log"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-q", "-m", "adds new.log"], check=True)
    repo["git"]("worktree", "remove", "--force", str(other))
    exclude = Path(subprocess.run(["git", "-C", str(wt), "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
                                  capture_output=True, text=True).stdout.strip())
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("*.log\n")                                                # an ignored file of the same name sits in the worktree
    (wt / "new.log").write_text("precious, ignored\n")
    with pytest.raises(jobs.DecisionError):
        jobs._ensure_worktree(p, t["branch"])
    assert (wt / "new.log").read_text() == "precious, ignored\n"


def test_a_worktree_made_in_the_project_before_shadows_moves_into_one_unless_work_is_in_it(repo, tmp_path):
    p = Project(repo["path"], {"worktrees": str(tmp_path / "wt")})
    did = p.store.add(Q)["id"]
    t = p.store.try_option(did, "right")
    repo["git"]("branch", t["branch"], "main")
    old = tmp_path / "wt" / t["branch"].replace("/", "-")
    repo["git"]("worktree", "add", "-q", str(old), t["branch"])
    wt = jobs._ensure_worktree(p, t["branch"])
    assert wt == old and ".shadow" in jobs._common_dir(p, wt).as_posix()
    assert str(old) not in repo["git"]("worktree", "list")
    jobs._remove_worktree(p, wt)
    repo["git"]("worktree", "add", "-q", str(old), t["branch"])
    (old / "f.txt").write_text("work in progress\n")
    with pytest.raises(jobs.DecisionError):
        jobs._ensure_worktree(p, t["branch"])
    assert (old / "f.txt").read_text() == "work in progress\n"


def test_a_branch_that_cannot_come_back_keeps_its_worktree_and_repository(repo):
    model = FakeModel([{"calls": [("run", {"command": "echo a > g.txt && git add g.txt && git commit -q -m mine && echo ok"})]},
                       {"calls": [("finish", {"built": True, "tests": "-", "impact": "x", "note": ""})]}])
    p = _project(repo, model)
    did, t = _trial(p)
    jid = p.store.job_open(t["id"], agent="local")["id"]
    orig = jobs.agents.make(p.agent("local"), p).__class__.run

    def meanwhile(self, prompt, ctx):
        out = orig(self, prompt, ctx)
        (repo["path"] / "h.txt").write_text("elsewhere\n")                      # the project's branch moves on: no fast-forward
        repo["git"]("checkout", "-q", t["branch"]); repo["git"]("add", "h.txt"); repo["git"]("commit", "-q", "-m", "elsewhere")
        repo["git"]("checkout", "-q", "main")
        return out
    jobs.agents.make(p.agent("local"), p).__class__.run = meanwhile
    try:
        out = jobs.run(p, jid)
    finally:
        jobs.agents.make(p.agent("local"), p).__class__.run = orig
    wt = repo["tmp"] / "wt" / t["branch"].replace("/", "-")
    assert out["status"] == "failed" and "brought back" in out["reason"] and "rejected" in out["reason"]
    assert wt.exists() and subprocess.run(["git", "-C", str(wt), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip() == "mine"


def test_the_chat_loop_s_commands_keep_seeing_the_shared_data_command_after_command(repo):
    """Review of 2026-11-02: bwrap's mount point for an ro_bind stayed in the worktree, so the next command lost the bind."""
    (repo["path"] / ".gitignore").write_text("data/\n")
    repo["git"]("add", ".gitignore"); repo["git"]("commit", "-q", "-m", "ignore data")
    (repo["path"] / "data").mkdir()
    (repo["path"] / "data" / "model.txt").write_text("weights\n")
    model = FakeModel([{"calls": [("run", {"command": "cat data/model.txt"})]},
                       {"calls": [("run", {"command": "cat data/model.txt"})]},
                       {"calls": [("finish", {"built": False, "tests": "-", "impact": "-", "note": "only reading"})]}])
    p = _project(repo, model, sandbox={"ro_binds": ["data"]})
    did, t = _trial(p)
    _run(p, t["id"])
    said = [m["content"] for m in model.requests[-1]["body"]["messages"] if m.get("role") == "tool"]   # the whole conversation, once
    assert len(said) == 2 and all("weights" in x for x in said), said
