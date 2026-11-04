"""A trial handed to an AI agent: the job from its start to the measurement.

1. ``send`` opens a job in the store (one at a time) and starts the runner as its own process (``python -m dtree.jobs run JOB``),
   detached, so a restart of the server does not stop it.
2. The runner makes a git worktree of the trial's branch (``alt/<id>-<option>``) outside the project (``worktrees`` in the config),
   gives the agent the question, the answer taken now and how it was measured, the option to build, the project's rules and dtree's
   own rules (commit on that branch only; never merge, push or switch; never touch the main checkout), and logs what it does.
3. The agent's last word is the structured result. Built with commits → the trial is ``measured`` with that result and its commits are
   kept on the trial; otherwise the job ``failed`` with the reason (the commits it made are still kept). The person then chooses
   adopt / switch / drop. Nothing here merges.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import agents, gitcode, merge, sandbox, spend, verify
from .project import Project
from .store import DecisionError, DecisionStore

LOG_TAIL_BYTES = 256 * 1024
PKG_SRC = Path(__file__).resolve().parents[1]


def _alive(pid: Optional[int], marker: str = "") -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    if marker:
        try:
            return marker in Path("/proc/%d/cmdline" % int(pid)).read_bytes().decode("utf-8", "replace")
        except OSError:
            return True
    return True


def _spend_problem(project: Project, cfg: Dict[str, Any]) -> Optional[str]:
    """Why the agent's spend settings refuse every job it would be sent (a cap or a price that is not a number, a dollar cap it cannot
    count), or None: ``spend.settings_problem`` where spend has it, else the check ``spend.check`` runs before a job."""
    problem = getattr(spend, "settings_problem", None)
    if callable(problem):
        return problem(project, cfg)
    check = getattr(spend, "_check_settings", None)
    if callable(check):
        try:
            check(project, cfg.get("name"), cfg)
        except DecisionError as exc:
            return str(exc)
    return None


def _rate_used(project: Project, name: str) -> Optional[Dict[str, Any]]:
    """The agent's day of requests used up now (``spend.rate_used``: {reason, reason_th, …}), or None — also when the store cannot say:
    the check before a job and the chat loop count again."""
    try:
        return spend.rate_used(project, name, project.agent(name))
    except Exception:
        return None


def agent_list(project: Project) -> List[Dict[str, Any]]:
    """Every configured agent with whether it can run here now, and why not: one its spend settings refuse is not offered either, nor
    one whose ``requests_per_day`` is used up (``reason_th`` then says it in Thai too)."""
    out = []
    for name in (project.config.get("agents") or {}):
        why_th = None
        try:
            a = agents.make(project.agent(name), project)
            ok, why = a.available()
            if ok:
                problem = _spend_problem(project, project.agent(name))
                ok, why = (False, problem) if problem else (ok, why)
            if ok:
                used = _rate_used(project, name)
                if used:
                    ok, why, why_th = False, used["reason"], used["reason_th"]
        except (ValueError, KeyError) as exc:
            ok, why = False, str(exc)
        cfg = project.agent(name)
        row = {"name": name, "label": cfg.get("label") or name, "kind": cfg.get("kind"), "model": cfg.get("model"), "available": ok,
               "reason": "" if ok else why}
        if why_th and not ok:
            row["reason_th"] = why_th
        try:
            row.update(agents.make(cfg, project).describe())         # sandbox, network, caps (agents that tell)
        except (AttributeError, ValueError, KeyError):
            pass
        out.append(row)
    return out


def reap(store: DecisionStore, grace_s: float = 120.0) -> List[int]:
    """A job whose runner is gone (the machine restarted, the runner crashed) is marked failed, not running for ever."""
    gone = []

    def again(j: Dict[str, Any]) -> str:                             # it never reached its agent (no worktree, no agent recorded): a
        if j.get("worktree") or j.get("agent_pid") or not store.followup_undo(j["id"]):   # follow-up's trial is measured again
            return ""
        return "; trial %d is measured again, with the result it had before this follow-up" % j["trial_id"]
    for j in store.jobs(status="running"):
        if j["runner_pid"] is None:
            if time.time() - j["created"] > grace_s:
                store.job_end(j["id"], "failed", "the runner never started" + again(j))
                gone.append(j["id"])
        elif not _alive(j["runner_pid"], "dtree.jobs"):
            store.job_end(j["id"], "stopped" if j["stop_requested"] else "failed",
                          "the runner is gone" + (" (stopped)" if j["stop_requested"] else "") + again(j))
            gone.append(j["id"])
    return gone


def send(project: Project, tid: int, agent: Optional[str] = None, note: str = "", by: str = "user", kind: str = "build") -> Dict[str, Any]:
    """Hand trial ``tid`` to ``agent`` (the default agent when None). Refused when the agent is unknown or cannot run here, the budget is
    used up (``spend.BudgetRefused``) or the agent's day of requests is (``requests_per_day``: ``spend.RateRefused``, code "rate"), the
    trial is not in a state for this kind of job, or too many jobs run."""
    return start_job(project, open_job(project, tid, agent, note, by=by, kind=kind))


def open_job(project: Project, tid: int, agent: Optional[str] = None, note: str = "", by: str = "user", kind: str = "build") -> Dict[str, Any]:
    """``send``'s first half: its checks and the job's row, with no runner yet. The row already holds the trial (one running job per
    trial) and a job slot, so merge.update_trial opens its update job before it touches git — a job sent meanwhile is refused instead of
    being given the same worktree — and starts it (``start_job``) once the merge is in progress there. A row with no runner is ended
    by ``reap`` after its grace (120 s)."""
    store = project.store
    name = agent or project.config["default_agent"]
    try:
        a = agents.make(project.agent(name), project)
    except (KeyError, ValueError):
        raise DecisionError("no agent %r (agents in .dtree/config.json)" % name)
    ok, why = a.available()
    if not ok:
        raise DecisionError("agent %s cannot run here: %s" % (name, why))
    spend.check(project, name, by, store.trial(tid)["decision_id"])
    reap(store)
    return store.job_open(tid, note, by=by, agent=name, max_running=int(project.config.get("max_parallel_jobs") or 1), kind=kind)


def start_job(project: Project, job: Dict[str, Any], undo: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
    """``send``'s second half: the runner of an opened job, as its own process. When it cannot start, ``undo`` runs first (while the
    row still holds the trial) and the job is ended failed."""
    store = project.store
    try:
        pid = launch(project, job["id"])
    except OSError as exc:
        try:
            if undo is not None:
                undo()
        finally:
            ended = store.job_end(job["id"], "failed", "the runner could not start: %s" % exc)
        return ended
    return store.job_set(job["id"], runner_pid=pid)


def race(project: Project, did: int, option: str, names: List[str], note: str = "", by: str = "user") -> List[Dict[str, Any]]:
    """The same work for several agents at once (the user asked 2026-11-01: "choose a decision by having several AIs do the same work to
    compare the results"): one trial of ``option`` per agent (variant = the agent's name, branch ``alt/<id>-<option>--<agent>``) and a
    job for each. Every agent is checked first; one that cannot run here is reported and the others still start. Returns one entry per
    agent: {agent, ok, trial, job} or {agent, ok: false, error}."""
    names = [n for i, n in enumerate(names or []) if n and n not in names[:i]]
    if not names:
        raise DecisionError("name at least one agent")
    limit = int(project.config.get("max_parallel_jobs") or 1)
    if len(names) > limit:
        raise DecisionError("%d agents but at most %d jobs at once (max_parallel_jobs in .dtree/config.json)" % (len(names), limit))
    out = []
    for name in names:
        try:
            a = agents.make(project.agent(name), project)
            ok, why = a.available()
            if not ok:
                raise DecisionError("cannot run here: %s" % why)
            spend.check(project, name, by, did)
        except (KeyError, ValueError, DecisionError) as exc:
            out.append(_refused(name, exc))
            continue
        try:
            t = project.store.try_option(did, option, note, by=by, variant=name)
            if project.git.available() and not project.git.has_branch(t["branch"]):
                project.git.run("branch", t["branch"], project.base)
            out.append({"agent": name, "ok": True, "trial": t, "job": send(project, t["id"], name, "", by=by)})
        except DecisionError as exc:
            out.append(_refused(name, exc))
    return out


def _refused(name: str, exc: Exception) -> Dict[str, Any]:
    """A race's entry for an agent that was not started: why, with the refusal's ``code`` / ``setting`` when it has them (a budget,
    a rate limit) and its ``error_th`` (a rate limit's)."""
    out = {"agent": name, "ok": False, "error": str(exc) if not isinstance(exc, KeyError) else "no such agent"}
    out.update({k: getattr(exc, k) for k in ("code", "setting", "error_th") if getattr(exc, k, None)})
    return out


def followup(project: Project, tid: int, feedback: str, agent: Optional[str] = None, by: str = "user") -> Dict[str, Any]:
    """Go on with a trial on the same branch, with the person's feedback (Vibe Kanban's follow-up). The agent is the trial's last one
    unless another is named; the job's prompt shows the commits already on the branch and the feedback. Every refusal comes before the
    trial is touched — the agent cannot run here, the budget, its day of requests, a job running on it, every job slot taken — and the
    trial goes back to trying in one transaction with its job's row, so a refused follow-up leaves a measured trial measured, with its
    result — and so does a job that never reaches its agent (``store.followup_undo``: its runner cannot start, dies before the agent,
    or finds no agent or no worktree)."""
    store = project.store
    last = [j for j in store.jobs(tid=tid)]
    t = store.trial(tid)
    name = agent or (last[-1].get("agent") if last else None) or t.get("variant") or project.config["default_agent"]
    try:
        ok, why = agents.make(project.agent(name), project).available()
    except (KeyError, ValueError):
        raise DecisionError("no agent %r" % name)
    if not ok:
        raise DecisionError("agent %s cannot run here: %s" % (name, why))
    if any(j["status"] == "running" for j in last):
        raise DecisionError("trial %d is being built now: stop it or wait" % tid)
    spend.check(project, name, by, t["decision_id"])
    reap(store)
    job = store.followup_job(tid, feedback, by=by, agent=name, max_running=int(project.config.get("max_parallel_jobs") or 1))
    return start_job(project, job, undo=lambda: store.followup_undo(job["id"]))   # no runner: measured again (review of round 3)


def _whole(js: List[Dict[str, Any]], now: float) -> Dict[str, Any]:
    """A trial's jobs as its compare row's ``job``: the cost, tokens, turns and time of all of them (a build, its follow-ups, its updates),
    and the last one's id, status, reason and kind. ``cost_usd`` sums the jobs whose dollars are known (None when none are);
    ``cost_estimated``: some of it was estimated from tokens and prices; ``cost_unknown``: how many jobs have no dollars."""
    last = js[-1]

    def total(values: List[Any]) -> Any:
        known = [v for v in values if v is not None]
        return sum(known) if known else None
    costs = [j.get("cost_usd") if j.get("cost_usd") is not None else (j.get("result") or {}).get("cost_usd") for j in js]
    cost = total([float(c) for c in costs if c is not None])
    return {"id": last["id"], "status": last["status"], "reason": last.get("reason"), "kind": last.get("kind") or "build", "jobs": len(js),
            "cost_usd": None if cost is None else round(cost, 6), "cost_unknown": sum(1 for c in costs if c is None),
            "cost_estimated": any(c is not None and (j.get("result") or {}).get("cost_estimated") for j, c in zip(js, costs)),
            "tokens": total([(j.get("result") or {}).get("tokens") for j in js]), "turns": total([(j.get("result") or {}).get("turns") for j in js]),
            "seconds": round(sum(round((j.get("ended_at") or now) - j["created"], 1) for j in js), 1)}


def compare(project: Project, did: int) -> Dict[str, Any]:
    """Every trial of a question side by side: its agent (the trial's variant, else its first build job's), its jobs together (status
    and reason of the last, the cost, tokens and time of all: ``_whole``), the measured result, and what its branch changes against the
    base (commits, files, lines)."""
    store, g = project.store, project.git
    reap(store)
    item = store.get(did)
    by_trial: Dict[int, List[Dict[str, Any]]] = {}
    for j in item.get("jobs") or []:
        by_trial.setdefault(j["trial_id"], []).append(j)
    rows, now = [], time.time()
    for t in item.get("trials") or []:
        js = by_trial.get(t["id"]) or []
        builds = [j for j in js if (j.get("kind") or "build") == "build" and j.get("agent")]
        b = g.branch_info(t["branch"], project.base) if g.available() else {"exists": False}
        files = b.get("files") or []
        rows.append({"trial": t["id"], "option": t["option"], "variant": t.get("variant"),
                     "agent": t.get("variant") or (builds[0]["agent"] if builds else None) or (js[0].get("agent") if js else None),
                     "status": t["status"], "tests": t.get("result_tests"), "impact": t.get("result_impact"), "branch": t["branch"],
                     "commits": len(b.get("ahead") or []), "files": len(files), "add": sum(f.get("add") or 0 for f in files),
                     "del": sum(f.get("del") or 0 for f in files), "merged": b.get("merged"), "job": _whole(js, now) if js else None})
        rows[-1].update(verify.compare_fields(project, t))
        rows[-1].update(merge.compare_fields(project, t))
    return {"base": project.base, "answer": item.get("answer"), "status": item["status"], "rows": rows}


def launch(project: Project, jid: int) -> int:
    logs = project.path(project.config["logs"])
    logs.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=str(PKG_SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""), DTREE_DB=str(project.store.path))
    with open(logs / ("job-%d.runner.log" % jid), "a", encoding="utf-8") as out:
        proc = subprocess.Popen([sys.executable, "-m", "dtree.jobs", "run", str(jid), "--project", str(project.root)], cwd=str(project.root), env=env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    return proc.pid


def stop(store: DecisionStore, jid: int, by: str = "user") -> Dict[str, Any]:
    """Stop a running job. While its runner lives, the runner stops the agent itself (it watches ``stop_requested``: SIGINT first, so
    Claude Code still writes its result and its cost is kept); a second stop, or a runner that is gone, ends the agent's process group
    at once and the job is recorded stopped here."""
    j = store.job(jid)
    if j["status"] != "running":
        raise DecisionError("job %d is %s already" % (jid, j["status"]))
    again = bool(j["stop_requested"])
    store.job_set(jid, stop_requested=1)
    runner = _alive(j["runner_pid"], "dtree.jobs")
    if (again or not runner) and _alive(j["agent_pid"]):
        try:
            os.killpg(int(j["agent_pid"]), signal.SIGTERM)
        except OSError:
            pass
    if not runner:
        return store.job_end(jid, "stopped", "stopped by %s" % by, by=by)
    return store.job(jid)


def log_tail(job: Dict[str, Any], lines: int = 200) -> List[str]:
    path = job.get("log")
    if not path or not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
        text = f.read().decode("utf-8", "replace")
    return text.splitlines()[-max(1, min(int(lines), 2000)):]


# --------------------------------------------------------------------------------------------------------------------- the prompt
def prompt_for(project: Project, job: Dict[str, Any], worktree: Path) -> str:
    store = project.store
    d = store.get(job["decision_id"])
    t = store.trial(job["trial_id"])
    want = {o["key"]: o for o in d["options"]}.get(t["option"], {})
    cfg = project.config
    lines = [
        ("You are building one option of a decided question of the project %r, on its own git branch, so the person can compare it with the" % cfg["name"]
         if d.get("answer") else "You are building one option of a question of the project %r that is not decided yet, on its own git branch, so" % cfg["name"]),
        ("answer taken now and choose." if d.get("answer") else "the person can choose from what is built and measured, not only from the words.")
        + (" Other agents build the same option on their own branches; the person compares the results." if t.get("variant") else "")
        + " You work alone: nobody answers questions during this run.",
        "",
        "Working directory: %s — a git worktree of branch %s (made from %s)." % (worktree, t["branch"], project.base),
        "The main checkout at %s is not yours: never edit, switch, commit or merge there. Files git ignores there (data, models) are not in" % project.root,
        ("the worktree; in the sandbox the main checkout is not visible, and those the project shares (sandbox.ro_binds) appear read-only at"
         if sandbox.project_settings(project)["root"] == "allowlist" else
         "the worktree; in the sandbox the main checkout is visible read-only, and those the project shares (sandbox.ro_binds) appear at"),
        "the same place in the worktree.",
        "",
        "Question #%d: %s / %s" % (d["id"], d["title_en"], d["title_th"]),
        d.get("question_en") or "", d.get("question_th") or "",
    ]
    if d.get("context_en") or d.get("context_th"):
        lines += ["Context: " + (d.get("context_en") or ""), d.get("context_th") or ""]
    lines += ["", "Options:"]
    for o in d["options"]:
        mark = " [the answer taken now]" if o["key"] == d.get("answer") else " [BUILD THIS ONE]" if o["key"] == t["option"] else ""
        desc = o.get("desc_en") or o.get("desc_th")
        lines.append("- %s: %s / %s%s%s" % (o["key"], o.get("en", ""), o.get("th", ""), mark, (" — " + desc) if desc else ""))
    if d.get("note"):
        lines.append("The person's note on the answer: " + d["note"])
    if d.get("result_tests") or d.get("result_impact"):
        lines += ["How the answer was measured when it was finished:", "  tests: " + (d.get("result_tests") or "-"), "  impact: " + (d.get("result_impact") or "-")]
    if d.get("commits"):
        lines.append("The answer's commits (git show them to see what it changed; build the option in their place where they differ):" if d.get("answer")
                     else "Commits recorded on this question, from an earlier answer (git show them to see what it changed):")
        lines += ["  %s %s" % (c["sha"][:10], c.get("subject", "")) for c in d["commits"][:40]]
    if d.get("refs"):
        lines.append("References: " + ", ".join(d["refs"]))
    grown = store.grown_on(d["id"], d.get("answer"))
    if grown:
        lines.append("Later questions grew on %s (look at them if this option changes their ground): " % ("the answer taken now" if d.get("answer") else "this question")
                     + " ".join("#%d" % i for i in grown))
    lines += ["", "Build: option %r — %s / %s" % (t["option"], want.get("en", ""), want.get("th", ""))]
    for side in ("pros", "cons"):
        if want.get(side):
            lines.append("  %s noted when it was asked: %s" % ("Good" if side == "pros" else "Bad", "; ".join(want[side])))
    if t.get("note"):
        lines.append("The person's note for this trial: " + t["note"])
    if t.get("commits"):                                             # the branch has earlier work (an agent's or a person's)
        lines += ["", "FOLLOW-UP: this branch already has earlier work on this option (keep it, change it where the feedback asks):"]
        lines += ["  %s %s" % (c["sha"][:10], c.get("subject", "")) for c in t["commits"][-20:]]
        ups = [e for e in d.get("events") or [] if e["action"] == "trial_followup" and (e.get("detail") or {}).get("trial") == t["id"]]
        last = ((ups[-1]["detail"].get("was") or {}) if ups else {})
        if last.get("tests") or last.get("impact"):
            lines.append("  the last result: tests %s · impact %s" % (last.get("tests") or "-", last.get("impact") or "-"))
        if job.get("note"):
            lines.append("The person's feedback: " + job["note"])
    elif job.get("note") and job.get("note") != t.get("note"):
        lines.append("The person's note for this job: " + job["note"])
    lines += merge.prompt_lines(project, job, t, worktree)
    lines += ["", "Rules:",
              "- Commit only on %s, in this directory." % t["branch"],
              "- Never merge, push, rebase, switch or delete branches, and never run git worktree. The person chooses after your measurement.",
              "- Never print or commit keys, tokens or passwords. Do not use sudo."]
    lines += ["- " + str(r) for r in cfg.get("rules") or []]
    if cfg.get("test_command"):
        lines.append("- Tests: %s (or the suites named above)." % cfg["test_command"])
    # an open question has no answer yet: the options are compared with each other and with the project as it is (E8 of the real run)
    lines.append("- Measure with the same tests and scenarios the answer was measured with, so the two can be compared. Report honestly:"
                 if d.get("answer") else
                 "- Measure with the project's tests and scenarios, the same ones another option would be measured with. Report honestly:")
    lines.append("  a failure stays a failure, a number you did not measure is not given.")
    try:
        checked = verify.auto_on(project)
    except DecisionError:
        checked = False
    if checked:
        lines.append("- dtree runs the tests and measures itself afterwards, on your branch and on the base, and shows them beside your report.")
    if cfg.get("docs"):
        lines.append("- Update %s where the behaviour changes." % cfg["docs"])
    lines += ["", "Finish with the structured result: built (true only when the option is built and committed), tests (suites and pass / fail",
              "counts), impact (what changes %s, with numbers), note (what the person should know before choosing)."
              % ("against the answer taken now" if d.get("answer") else "against the project as it is on %s" % project.base)]
    return "\n".join(l for l in lines if l is not None)


# --------------------------------------------------------------------------------------------------------------------- the runner
#: what a test run leaves in a worktree and nobody needs: not a reason to keep it
_CACHE = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache)(/|$)|\.pyc$")


def _left_over(porcelain: str) -> List[str]:
    """The paths ``git status --porcelain`` shows, without caches."""
    paths = [l[3:].strip().strip('"') for l in porcelain.splitlines() if l.strip()]
    return [x for x in paths if not _CACHE.search(x)]


#: git options for the runner's own commands in an agent's worktree: whatever the job left there runs nothing on the host
SAFE_GIT = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


def _common_dir(project: Project, cwd: Any = None) -> Optional[Path]:
    out = project.git.out("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd)
    return Path(out.strip()).resolve() if out and out.strip() else None


# --------------------------------------------------------------------------------------------------------- the trial's own repository
#: in a shadow repository, the files git follows or reads commands from, kept as dtree made them (the pin) and put back by ``trust``
_PINNED = ("HEAD", "config", "objects/info/alternates")
#: in a shadow repository, what neither dtree nor git makes there, each of which git would run or follow: removed by ``trust``
_SCRUBBED = ("hooks", "info/attributes", "info/grafts", "config.worktree", "commondir", "shallow")
#: the same in the admin directory of its worktree (``<shadow>/worktrees/<name>``)
_SCRUBBED_ADMIN = ("config.worktree", "hooks", "info/attributes")


def _shadows(project: Project) -> Path:
    return project.path(project.config["worktrees"]) / ".shadow"


def _shadow_path(project: Project, branch: str) -> Path:
    """Where the trial's shadow repository is (``_shadow`` makes it there)."""
    return _shadows(project) / (branch.replace("/", "-") + ".git")


def _pin_path(shadow: Path) -> Path:
    """``<worktrees>/.shadow/<name>.pin.json``: beside the shadow, not in it — a job's sandbox can write the shadow (Codex's own sandbox
    the whole of it, the others its objects and its branch), never this."""
    return shadow.parent / (shadow.name[:-len(".git")] + ".pin.json")


def _read(path: Path) -> str:
    """A small regular file, never through a link (``gitcode.read_file``): a FIFO a job left there does not hold dtree."""
    return gitcode.read_file(path, follow=False)


def _below(base: Path, rel: str) -> Path:
    """``base / rel`` with no symbolic link on the way below ``base``: dtree makes none there, and one a job made could point anywhere —
    dtree then writes or removes nothing through it (DecisionError)."""
    p = base
    for part in Path(rel).parts[:-1]:
        p = p / part
        if os.path.islink(str(p)):
            raise DecisionError("%s is a symbolic link, which neither dtree nor git makes there: something other than git changed the trial's "
                                "repository — look at it, then remove the link" % p)
    return base / rel


def _put(path: Path, text: str) -> bool:
    """``path`` holds ``text`` as a plain file: a link, a directory or other content there is replaced (never written through); written
    beside it and renamed over it. True when it had to change."""
    try:
        if not os.path.islink(str(path)) and path.is_file() and _read(path) == text:
            return False
    except OSError:
        pass
    if os.path.isdir(str(path)) and not os.path.islink(str(path)):
        shutil.rmtree(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (".%s.dtree-%d-%d" % (path.name, os.getpid(), threading.get_ident()))
    _scrub(tmp)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)
    os.replace(str(tmp), str(path))
    return True


def _scrub(path: Path) -> bool:
    """Nothing at ``path``: a link is removed itself, never followed. True when something was there; an empty directory (a mount point a
    sandbox left, as for ``hooks``) is removed without saying so."""
    if not os.path.lexists(str(path)):
        return False
    try:
        if os.path.isdir(str(path)) and not os.path.islink(str(path)):
            with os.scandir(str(path)) as entries:
                had = any(True for _ in entries)
            shutil.rmtree(str(path))
            return had
        os.unlink(str(path))
    except FileNotFoundError:                                        # removed meanwhile by another of dtree's own calls (a poll)
        return False
    return True


def _read_pin(shadow: Path) -> Optional[Dict[str, Any]]:
    try:
        pin = json.loads(_read(_pin_path(shadow)))
    except (OSError, ValueError):
        return None
    return pin if isinstance(pin, dict) and isinstance(pin.get("shadow"), dict) else None


def _write_pin(shadow: Path, pin: Dict[str, Any]) -> None:
    _put(_pin_path(shadow), json.dumps(pin, ensure_ascii=False, indent=1) + "\n")


def _init_bare(project: Project, path: Path) -> None:
    """``git init --bare`` with no template (so no hooks), and the project's own commit identity (its local ``user.name`` / ``user.email``)."""
    g = project.git
    path.mkdir(parents=True, exist_ok=True)
    r = g.run("init", "-q", "--bare", "--template=", str(path))
    if r.returncode != 0:
        raise DecisionError("git init %s failed: %s" % (path, r.stderr.strip()))
    for key in ("user.name", "user.email"):
        value = g.out("config", "--local", "--get", key)
        if value and value.strip():
            g.run("config", key, value.strip(), cwd=path)


def _shadow(project: Project, branch: str) -> Path:
    """The trial's own repository: ``<worktrees>/.shadow/<branch>.git``, bare, borrowing the project's objects read-only
    (``objects/info/alternates``) and holding nothing but the trial's branch. An agent commits there; the project's repository —
    its other branches, HEAD, config and hooks — is read-only in the sandbox, and dtree brings the branch back itself (``_sync_back``).
    No hooks (an empty template); the project's own commit identity. What it is made with is kept beside it (the pin: ``trust``)."""
    path = _shadow_path(project, branch)
    if not (path / "HEAD").exists():
        _init_bare(project, path)
        (path / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (path / "objects" / "info" / "alternates").write_text(str(_common_dir(project) / "objects") + "\n", encoding="utf-8")
        _write_pin(path, {"version": 1, "branch": branch, "shadow": {rel: _read(path / rel) for rel in _PINNED}, "worktree": None})
    return path


def _branch_of(shadow: Path) -> Optional[str]:
    """The branch a shadow was made for, from its name (``alt-1-right.git`` → ``alt/1-right``)."""
    name = shadow.name[:-len(".git")]
    return name[:3] + "/" + name[4:] if name[:4] in ("alt-", "dec-") else None


def _admin_of(shadow: Path, wt: Path) -> Path:
    """The admin directory in ``shadow`` of worktree ``wt``, for a worktree whose pin has none: the one whose ``gitdir`` names ``wt``,
    else the only one there is (git names it after the worktree)."""
    root = _below(shadow, "worktrees/x").parent
    names = sorted(d.name for d in root.iterdir() if d.is_dir() and not d.is_symlink()) if root.is_dir() else []
    want = os.path.normpath(str(wt / ".git"))

    def names_it(n: str) -> bool:
        try:
            said = _read(root / n / "gitdir").strip()
        except OSError:
            return False
        return os.path.normpath(os.path.join(str(root / n), said)) == want
    pick = [n for n in names if names_it(n)] or names
    if len(pick) != 1:
        raise DecisionError("dtree cannot tell which entry of %s is the worktree %s (%s): look at them, then remove what is not its"
                            % (root, wt, ", ".join(names) or "none"))
    return root / pick[0]


def _pin_worktree(shadow: Path, wt: Path, pin: Dict[str, Any], made: bool = False) -> Dict[str, Any]:
    """The pin's entry for the shadow's worktree ``wt``: ``made`` — dtree's own ``git worktree add`` just wrote it — what git wrote;
    else (a worktree made before dtree kept its pin) what git writes: absolute paths to the admin directory and back."""
    if made:
        said = _read(wt / ".git").split("gitdir:", 1)[-1].strip()
        admin = Path(os.path.normpath(os.path.join(str(wt), said)))
        files = {".git": _read(wt / ".git"), "gitdir": _read(admin / "gitdir"), "commondir": _read(admin / "commondir")}
    else:
        admin = _admin_of(shadow, wt)
        files = {".git": "gitdir: %s\n" % admin, "gitdir": "%s\n" % (wt / ".git"), "commondir": "../..\n"}
    if os.path.normpath(str(admin.parent)) != os.path.normpath(str(shadow / "worktrees")):
        raise DecisionError("the worktree %s is not one of %s" % (wt, shadow))
    pin["worktree"] = {"path": str(wt), "admin": admin.name, "files": files}
    _write_pin(shadow, pin)
    return pin["worktree"]


def _pin_anew(project: Project, shadow: Path) -> Dict[str, Any]:
    """The pin of a shadow made before dtree kept one: what ``_shadow`` makes, made again in a scratch repository beside it — not read
    from the shadow, where a job may have written."""
    scratch = shadow.parent / (".pin-%d-%d.git" % (os.getpid(), threading.get_ident()))
    try:
        _init_bare(project, scratch)
        files = {"HEAD": _read(scratch / "HEAD"), "config": _read(scratch / "config"),
                 "objects/info/alternates": str(_common_dir(project) / "objects") + "\n"}
    finally:
        shutil.rmtree(str(scratch), ignore_errors=True)
    pin = {"version": 1, "branch": _branch_of(shadow), "shadow": files, "worktree": None}
    _write_pin(shadow, pin)
    return pin


def trust(project: Project, wt: Any, restore_missing: Optional[Tuple[int, int]] = None) -> List[str]:
    """Before dtree's own git runs in a trial's shadow repository or in its worktree ``wt`` (on the host, outside any sandbox): what git
    there would follow or take a command from is put back as dtree made it — the shadow's ``HEAD``, ``config`` and
    ``objects/info/alternates``, the worktree's ``.git`` file and its admin directory's ``gitdir`` / ``commondir`` (the pin beside the
    shadow) — and what neither git nor dtree makes there is removed: ``hooks``, ``info/attributes``, ``info/grafts``, ``config.worktree``,
    ``commondir``, ``shallow`` (of the shadow and of the admin directory). A job may have written them (Codex's own sandbox lets it write
    the whole shadow; review of 2026-11-02): git would run a clean or smudge filter, a merge driver, ``gpg.program`` or a hook from them,
    or work in a repository of the job's making. Returns what was put back or removed, for the job's log; nothing to do for a worktree
    that is not a shadow's. DecisionError when a link stands where dtree keeps a directory: no git of dtree's runs there then. A
    directory with no ``.git`` is no worktree of the shadow (made again by hand where dtree's was deleted) and gets none — except with
    ``restore_missing``, the ``_identity`` (device, inode) of the directory the runner handed its job: while the directory is still
    that one (a job cannot replace it: the directory above is not its to write), a missing ``.git`` is the job's doing and is put back
    when the pin and the shadow's admin entry still name that worktree. A directory a person made there while the job ran is another
    one, and gets none (review of round 4)."""
    try:
        return _trust(project, Path(wt), restore_missing)
    except OSError as exc:                                           # it could not be put back: dtree's git does not run there either
        raise DecisionError("dtree could not put the trial's repository back as it made it (%s): look at it" % exc)


def _trust(project: Project, wt: Path, restore_missing: Optional[Tuple[int, int]] = None) -> List[str]:
    shadow = _shadows(project) / (wt.name + ".git")
    if not os.path.lexists(str(shadow)):
        return []
    if os.path.islink(str(shadow)) or not shadow.is_dir():
        raise DecisionError("%s is not the directory dtree made: look at it, then remove it" % shadow)
    pin = _read_pin(shadow) or _pin_anew(project, shadow)
    fixed: List[str] = []
    for rel, text in sorted(pin["shadow"].items()):
        if _put(_below(shadow, rel), text):
            fixed.append(rel + " put back")
    for rel in _SCRUBBED:
        if _scrub(_below(shadow, rel)):
            fixed.append(rel + " removed")
    if not os.path.isdir(str(wt)) or os.path.islink(str(wt)):
        return fixed
    dotgit = os.path.lexists(str(wt / ".git"))                       # none: a directory made again where dtree's worktree was deleted
    w = pin.get("worktree") or {}                                    # by hand is no worktree of the shadow, and gets no .git (review r3)
    if os.path.normpath(str(w.get("path") or "")) != os.path.normpath(str(wt)):
        if not dotgit or _project_worktree(project, wt):
            return fixed
        w = _pin_worktree(shadow, wt, pin)
    admin = "worktrees/%s/" % w["admin"]
    registered = _below(shadow, admin + "gitdir").parent.is_dir()
    if registered:
        for rel in ("gitdir", "commondir"):
            if _put(_below(shadow, admin + rel), w["files"][rel]):
                fixed.append(admin + rel + " put back")
        for rel in _SCRUBBED_ADMIN:
            if _scrub(_below(shadow, admin + rel)):
                fixed.append(admin + rel + " removed")
    if not dotgit and not (restore_missing and registered and _identity(wt) == restore_missing):   # the job removed its .git
        return fixed
    if os.path.isdir(str(wt / ".git")) and not os.path.islink(str(wt / ".git")):  # dtree's worktrees have a .git file: a repository of
        raise DecisionError("%s is a directory, a repository dtree did not make: look at it, then move it away"   # someone's, never removed
                            % (wt / ".git"))
    try:
        same = not os.path.islink(str(wt / ".git")) and _read(wt / ".git") == w["files"][".git"]
    except OSError:
        same = False
    if not same and _project_worktree(project, wt):                  # a person's worktree of the project now stands there: theirs
        return fixed
    if _put(wt / ".git", w["files"][".git"]):
        fixed.append("the worktree's .git put back")
    return fixed


def _identity(path: Any) -> Optional[Tuple[int, int]]:
    """(device, inode) of the directory ``path`` — not of what a link there names — or None when there is none."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if stat.S_ISDIR(st.st_mode) else None


def _project_worktree(project: Project, wt: Path) -> bool:
    """Whether ``wt`` is a worktree of the project itself, registered there: its ``.git`` names an admin directory of the project's own
    repository that names ``wt`` back — a person's (made where dtree's was, after deleting it), or one made before shadows. A job cannot
    make one (the project's git directory is out of its reach), so dtree leaves it as it is."""
    try:
        if os.path.islink(str(wt / ".git")):
            return False
        said = _read(wt / ".git").split("gitdir:", 1)
        if len(said) != 2:
            return False
        admin = os.path.realpath(os.path.join(str(wt), said[1].strip()))
        main = _common_dir(project)
        if main is None or os.path.dirname(admin) != os.path.realpath(str(main / "worktrees")):
            return False
        back = _read(Path(admin) / "gitdir").strip()
    except OSError:
        return False
    return os.path.realpath(os.path.join(admin, back)) == os.path.realpath(str(wt / ".git"))


def _fetch_back(project: Project, branch: str, repo: Path) -> Optional[str]:
    """The trial's branch fetched from repository ``repo`` (its shadow) into the project: only a fast-forward (an agent that rewrote the
    branch's history is refused), every object it brings checked (``transfer.fsckObjects``). None when done, else why not."""
    r = project.git.run(*SAFE_GIT, "-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true", "fetch", "--no-tags",
                        "--no-write-fetch-head", str(repo), "refs/heads/%s:refs/heads/%s" % (branch, branch), timeout=600)
    if r.returncode != 0:
        return "%s could not be brought back from %s: %s" % (branch, repo, " ".join((r.stderr or r.stdout).split())[:500])
    return None


def _bring_back_shadow(project: Project, branch: str, shadow: Path, tip: Optional[str]) -> Optional[str]:
    """A shadow kept without its worktree (the directory was deleted by hand) may hold commits of the branch that never came back (a
    runner that ended first): they are brought into the project, never reset away (review F2: the next job's start set the shadow's
    branch to the project's and a later clean job removed the shadow, with them). Refused when the two went separate ways. Returns the
    project's branch commit afterwards."""
    g = project.git
    held = g.sha("refs/heads/" + branch, cwd=shadow)
    if not held or held == tip:
        return tip
    ahead = g.ancestor(tip, held, cwd=shadow) if tip else None
    if ahead:
        back = _fetch_back(project, branch, shadow)
        if back:
            raise DecisionError(back)
        return g.sha("refs/heads/" + branch)
    if ahead is None or not g.ancestor(held, tip, cwd=shadow):
        raise DecisionError("%s in %s (dtree's repository of the trial; its worktree is gone) and in the project have gone separate ways: bring "
                            "one into the other first (git fetch %s %s) — dtree does not reset either" % (branch, shadow, shadow, branch))
    return tip                                                       # behind: the project's branch moved on (an update dtree merged)


def _ensure_worktree(project: Project, branch: str) -> Path:
    """The trial's worktree ``<worktrees>/alt-<id>-<option>`` (merge.py finds it at the same place), made from the trial's shadow
    repository (``_shadow``) at the branch's commit in the project. One kept from an earlier job is brought forward to that commit when
    the project's branch moved (an update merged by dtree) and nothing is in progress there; one ahead of it (work not brought back yet)
    is used as it is. A shadow kept without its worktree first brings back what only it holds (``_bring_back_shadow``). A worktree of
    the project itself at that place (made before shadows) is used as it is. Nothing a job left in the shadow runs here (``trust``)."""
    g = project.git
    if not g.has_branch(branch):
        r = g.run("branch", branch, project.base)
        if r.returncode != 0:
            raise DecisionError("git branch %s failed: %s" % (branch, r.stderr.strip()))
    tip = g.sha("refs/heads/" + branch)
    wt = project.path(project.config["worktrees"]) / branch.replace("/", "-")
    trust(project, wt)
    if wt.exists():
        if g.worktree_dir(wt) is None or g.symbolic_head(wt) != "refs/heads/" + branch:   # its own top, not a repository above it
            raise DecisionError("%s exists and is not a worktree of %s" % (wt, branch))
        if _common_dir(project, wt) == _common_dir(project):         # made in the project itself before shadows: moved into a shadow
            if g.in_progress(wt) or _left_over(g.run(*SAFE_GIT, "status", "--porcelain", cwd=wt).stdout):
                raise DecisionError("%s is a worktree of the project itself with work in it: commit or clear it there, then try again "
                                    "(jobs now run in the trial's own repository)" % wt)
            _remove_worktree(project, wt)
            return _ensure_worktree(project, branch)
        here = g.sha("HEAD", cwd=wt)
        if not here:
            raise DecisionError("the HEAD of %s cannot be read: look at it there" % wt)
        if here != tip and not g.in_progress(wt):
            def ancestor(a: str, b: str) -> bool:
                return g.run("merge-base", "--is-ancestor", a, b, cwd=wt).returncode == 0
            if ancestor(here, tip):
                way = g.in_the_way(wt, here, tip)                    # an ignored file where the branch now has one: git would overwrite it
                if way is None or way:
                    raise DecisionError("%s cannot be brought to %s's %s: %s" % (wt, branch, tip[:10], "files git does not track are in the way: "
                                        + ", ".join(way[:10]) if way else "the paths could not be listed"))
                r = g.run(*SAFE_GIT, "merge", "--ff-only", "-q", tip, cwd=wt)
                if r.returncode != 0:
                    raise DecisionError("%s could not be brought to %s's %s: %s" % (wt, branch, tip[:10], (r.stderr or r.stdout).strip()[:300]))
            elif not ancestor(tip, here):
                raise DecisionError("%s in %s and in the project have gone separate ways: bring one into the other first" % (branch, wt))
        return wt
    shadow = _shadow(project, branch)
    tip = _bring_back_shadow(project, branch, shadow, tip)
    g.run(*SAFE_GIT, "update-ref", "refs/heads/" + branch, tip, cwd=shadow)
    g.run(*SAFE_GIT, "worktree", "prune", cwd=shadow)
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = g.run(*SAFE_GIT, "worktree", "add", "-q", str(wt), branch, cwd=shadow)
    if r.returncode != 0:
        raise DecisionError("git worktree add failed: %s" % r.stderr.strip())
    _pin_worktree(shadow, wt, _read_pin(shadow) or _pin_anew(project, shadow), made=True)
    return wt


def _sync_back(project: Project, branch: str, wt: Path) -> Optional[str]:
    """Bring the trial's branch from its shadow repository into the project (``_fetch_back``: fast-forward only, every object checked),
    after ``trust``: from the repository dtree made, not one a job named. None when done or nothing to do, else why not."""
    try:
        trust(project, wt)
    except DecisionError as exc:
        return "%s was not brought back: %s" % (branch, exc)
    shadow = _shadow_path(project, branch)
    if not os.path.lexists(str(wt / ".git")) and shadow.is_dir() and not shadow.is_symlink():
        return _fetch_back(project, branch, shadow)                  # the job removed its .git: what it committed is still in the shadow
    repo, main = _common_dir(project, wt), _common_dir(project)
    if repo is None or repo == main:
        return None
    return _fetch_back(project, branch, repo)


def _unsynced(project: Project, repo: Path, wt: Path) -> bool:
    """Whether the shadow ``repo`` holds commits of its trial's branch — or its worktree ``wt`` is at a commit — that the project's branch
    lacks (or git cannot tell)."""
    g = project.git
    branch = (_read_pin(repo) or {}).get("branch") or (g.symbolic_head(wt) or "")[len("refs/heads/"):] or None
    tip = g.sha("refs/heads/" + branch) if branch else None
    for c in ((g.sha("refs/heads/" + branch, cwd=repo) if branch else None), g.sha("HEAD", cwd=wt)):
        if c and (not tip or g.ancestor(c, tip, cwd=repo) is not True):
            return True
    return False


def _remove_worktree(project: Project, wt: Path) -> bool:
    """Remove a worktree through the repository it belongs to, and a shadow repository with no worktree left (with its pin) — never one
    whose branch or worktree holds commits the project's branch lacks: they would be lost (review F2); the worktree stays for the
    person, False."""
    try:
        trust(project, wt)
    except DecisionError:
        return False
    repo = _common_dir(project, wt)
    if repo is None:
        return False
    shadow = repo.parent == _shadows(project).resolve()
    if shadow and _unsynced(project, repo, wt):
        return False
    ok = project.git.run(*SAFE_GIT, "worktree", "remove", "--force", str(wt), cwd=repo).returncode == 0
    if ok and shadow:
        project.git.run(*SAFE_GIT, "worktree", "prune", cwd=repo)
        if not any((repo / "worktrees").glob("*")):
            shutil.rmtree(str(repo), ignore_errors=True)
            try:
                _pin_path(repo).unlink()
            except OSError:
                pass
    return ok


def run(project: Project, jid: int) -> Dict[str, Any]:
    """The runner: one job from start to its end in the store."""
    store = project.store
    job = store.job(jid)
    logs = project.path(project.config["logs"])
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / ("job-%d.log" % jid)
    store.job_set(jid, runner_pid=os.getpid(), log=str(log_path))
    log = open(log_path, "a", encoding="utf-8")

    def say(line: str) -> None:
        log.write(time.strftime("%H:%M:%S ") + line + "\n")
        log.flush()

    def end(status: str, reason: str, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        say("■ job %s: %s" % (status, reason))
        log.close()
        return store.job_end(jid, status, reason, result)

    if job["status"] != "running":
        return end(job["status"], "the job was not running")
    t = store.trial(job["trial_id"])
    tip = project.git.sha("refs/heads/" + t["branch"])

    def before_the_agent(reason: str) -> Dict[str, Any]:            # nothing ran: a follow-up's trial keeps its measurement (review r4)
        if project.git.sha("refs/heads/" + t["branch"]) == tip and store.followup_undo(jid):
            reason += "; trial %d is measured again, with the result it had before this follow-up" % t["id"]
        return end("failed", reason)
    try:
        agent = agents.make(project.agent(job.get("agent")), project)
    except (KeyError, ValueError) as exc:
        return before_the_agent("no agent %s" % exc)
    try:
        wt = _ensure_worktree(project, t["branch"])
        own = _identity(wt)                                          # the directory handed to the job (see trust's restore_missing)
    except (DecisionError, OSError, subprocess.TimeoutExpired) as exc:
        return before_the_agent(str(exc))
    git_dir = Path((project.git.run("rev-parse", "--git-common-dir", cwd=wt).stdout or ".git").strip())
    git_dir = git_dir if git_dir.is_absolute() else (wt / git_dir).resolve()
    store.job_set(jid, worktree=str(wt))
    say("⎇ trial %d · #%d option %s · %s · agent %s · worktree %s" % (t["id"], t["decision_id"], t["option"], t["branch"], agent.name, wt))
    raw = open(logs / ("job-%d.jsonl" % jid), "a", encoding="utf-8")

    def set_pid(pid: Optional[int]) -> None:
        store.job_set(jid, agent_pid=pid)
    ctx = agents.Context(wt, git_dir, say, set_pid, lambda: store.job(jid)["stop_requested"],
                         time.time() + float(project.config["timeout_s"]), lambda c: store.job_set(jid, cost_usd=c))
    ctx.raw_log, ctx.job = raw, jid
    ctx.limits = spend.limits(project, agent.cfg)                    # the job's caps (tokens, dollars)
    try:
        out = agent.run(prompt_for(project, job, wt), ctx)
    except Exception as exc:                                         # an agent's own failure ends the job, never the runner silently
        out = {"error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        raw.close()
    out = spend.after_job(project, agent.cfg, out)
    result = {k: out.get(k) for k in ("built", "tests", "impact", "note", "session", "turns", "tokens", "tokens_in", "tokens_out", "cost_usd",
                                      "cost_estimated", "usage_partial", "egress", "error")}
    if out.get("cost_usd") is not None:
        store.job_set(jid, cost_usd=float(out["cost_usd"]))
    try:
        fixed = trust(project, wt, restore_missing=own)              # before dtree's own git there: nothing the job left runs on the host
    except DecisionError as exc:
        say("⚠ the worktree and its repository are kept: %s" % wt)
        return end("failed", "dtree runs no git in %s: %s" % (wt, exc), result)
    if fixed:
        say("⚠ the job changed what git there runs or follows; put back as dtree made it: " + ", ".join(fixed))
    back = _sync_back(project, t["branch"], wt)
    if back:
        say("⚠ " + back)
    commits = project.git.commits_between(project.base, t["branch"])
    if commits:
        store.record_code(t["decision_id"], commits, how="recorded", why="built by %s (job %d)" % (agent.name, jid), trial=t["id"], by=agent.name)
    left = _left_over(project.git.run(*SAFE_GIT, "status", "--porcelain", cwd=wt).stdout)
    if back:
        say("⚠ the worktree and its repository are kept: %s" % wt)        # the agent's commits live only there now
    elif not left:
        _remove_worktree(project, wt)                                    # only caches were left: the branch keeps the work
    else:
        say("⚠ uncommitted changes are left in %s: %s" % (wt, ", ".join(left[:10])))
    if back:
        return end("failed", back, result)
    dropped = _dropped(store, t["id"])
    if dropped:                                                      # E2: the person dropped the trial meanwhile (a race won by another)
        return end("stopped", dropped, result)
    if store.job(jid)["stop_requested"] or out.get("error") == "stopped":
        return end("stopped", "stopped by the person", result)
    if out.get("error"):
        return end("failed", "longer than timeout_s (%s s)" % project.config["timeout_s"] if out["error"] == "timeout" else str(out["error"]), result)
    if job.get("kind") == "update":
        why = merge.after_job(project, job, t, say)
        if why:
            return end("failed", why, result)
    if not out.get("built"):
        return end("failed", "the agent did not build it: %s" % (out.get("note") or out.get("impact") or "-"), result)
    if not commits:
        return end("failed", "the agent said built but %s has no commits that %s lacks" % (t["branch"], project.base), result)
    impact = str(out.get("impact") or "").strip() + (("\n" + str(out["note"]).strip()) if out.get("note") else "")
    status = store.trial(t["id"])["status"]
    # an update keeps the trial's place: an adopted one stays adopted, one never built stays trying; both are verified again
    if status == "measured" or (status == "trying" and job.get("kind") != "update"):
        try:
            store.trial_result(t["id"], ("[%s] " % agent.name + str(out.get("tests") or "-"))[:4000], impact[:4000] or "-", by=agent.name)
        except DecisionError as exc:
            return end("failed", "the result could not be kept: %s" % exc, result)
    try:
        result["verify"] = verify.after_job(project, store.job(jid), store.trial(t["id"]), say)
    except Exception as exc:                                         # verification never turns a built trial into a failed job
        say("⚠ verification failed to run: %s: %s" % (type(exc).__name__, exc))
        result["verify"] = {"status": "error", "summary": str(exc)}
    dropped = _dropped(store, t["id"])                               # dropped while it was verified: not measured
    if dropped:
        return end("stopped", dropped, result)
    return end("measured", "%d commits on %s" % (len(commits), t["branch"]), result)


def _dropped(store: DecisionStore, tid: int) -> Optional[str]:
    """Why a job ends stopped when its trial was dropped while it ran (``store.trial_end`` also asks its runner to stop), else None."""
    t = store.trial(tid)
    if t["status"] != "dropped":
        return None
    return "trial %d was dropped meanwhile%s%s" % (tid, " by %s" % t["ended_by"] if t.get("ended_by") else "",
                                                   ": " + t["ended_note"] if t.get("ended_note") else "")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="dtree's job runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run"); p.add_argument("job", type=int); p.add_argument("--project", required=True)
    args = ap.parse_args(argv)
    job = run(Project(args.project), args.job)
    print("job %d %s: %s" % (job["id"], job["status"], job.get("reason") or ""))
    return 0 if job["status"] == "measured" else 1


if __name__ == "__main__":
    sys.exit(main())
