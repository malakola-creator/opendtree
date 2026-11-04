"""dtree from the command line — the same store as the tree page. Any agent (Claude Code, Codex, a person) can use it.

  dtree init [--name NAME]                           # .dtree/config.json + .dtree/.gitignore in this repository
  dtree serve [--host H] [--port P]                  # the tree page and the API (default http://127.0.0.1:5077/)
  dtree mcp                                          # the store as MCP tools over stdio (for Claude Code, Codex, any MCP client)
  dtree agents                                       # which agents can run here, and why not
  dtree list [--status open|decided|done|dismissed|closed|all] [--q WORDS] [--json]
  dtree show ID
  dtree add FILE.json                                # one question or a list; "source" defaults to "agent"
  dtree answer ID OPTION [--note TEXT]
  dtree done ID --tests TEXT --impact TEXT           # the decided work is finished
  dtree dismiss|reopen ID [--note TEXT]   ·   dtree comment ID TEXT
  dtree follow ID --on N --why TEXT [--how topic|sequence]
  dtree try ID OPTION [--note TEXT]                  # another option of a decided question, on branch alt/ID-OPTION
  dtree trials [--status open|all|…]   ·   dtree measured TRIAL --tests --impact   ·   dtree end TRIAL adopted|switch|dropped
  dtree start ID                                     # work on a decided question on dec/ID-OPTION (git switch)
  dtree merge ID [--trial TRIAL] [--close]           # merge it into the base (--no-ff, no checkout switched), keep the branch, record the commits
  dtree merge-check TRIAL [--json]                   # whether a trial merges now (exit 1 when not): conflicts, drift, the base's checkout
  dtree update TRIAL [--agent NAME] [--abort]        # merge the base into the trial; an agent resolves conflicts (an update job)
  dtree code ID SHA... --why TEXT [--how recorded|backfilled] [--trial TRIAL]
  dtree ai TRIAL [--agent NAME] [--note TEXT]        # hand a trial to an agent: claude, codex, muse, grok, local, …
  dtree race ID OPTION --agents claude,codex,muse [--note TEXT]   # several agents build the same option at once, to compare
  dtree compare ID [--json]                          # every trial side by side: agent, cost, time, its claim, dtree's own checks, merge
  dtree jobs [--trial TRIAL]   ·   dtree log JOB [--lines N]   ·   dtree stop JOB
  dtree followup TRIAL --feedback TEXT [--agent NAME]   # go on with a trial on its branch, with feedback (Vibe Kanban's follow-up)
  dtree diff TRIAL                                   # what a trial's branch changes since it left the base
  dtree verify TRIAL [--force] [--no-baseline] [--background]   # dtree runs the tests and measures itself: tip and baseline
  dtree checks [--trial TRIAL] [--sha SHA]           # the checks dtree ran (latest per commit and command)
  dtree spend [--month YYYY-MM] [--json]             # what the agents spent this month, against the budgets
  dtree settings [--json]                            # every setting the settings page shows: in effect, in the file, the default
  dtree set PATH VALUE   ·   dtree unset PATH        # one setting in .dtree/config.json, checked as the page checks it
  dtree key AGENT [--remove]                         # a chat agent's key into a 0600 file (asked without echo; never printed)
  dtree test-agent AGENT                             # whether the agent can run here; a chat agent's GET /models with its key
  dtree supersede ID --by NEW [--note TEXT]          # an ADR's "superseded by" (--by 0 takes it back)
  dtree adr [--out DIR] [--status decided,done]      # every question as an ADR file (MADR) + an index, in docs/decisions
  dtree context                                      # the decisions in force, short, for an agent (AGENTS.md / CLAUDE.md)
  dtree projects   ·   dtree new NAME [--mode story|decisions] [--dir DIR]   # the registry; a new project (dtree init + git init)
  dtree export [--out FILE]   ·   dtree import FILE [--name NAME]   ·   dtree save-as NAME   # a project out and in as one JSON file
  dtree book [--out FILE]                            # a story (mode story) as a Markdown book: scenes, choices, where each leads

``--project DIR`` (default: the repository with .dtree/config.json at or above here) and ``--db FILE`` (or $DTREE_DB) choose the store.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import jobs, languages, merge, projects, settings, spend, story, verify

FEATURES = (verify, merge, spend, settings, projects, story, languages)                       # ก ข ค, ตั้งค่า: each adds its own commands
from .project import Project, init
from .store import DecisionError


def _when(t):
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"


def _line(d):
    ans = (" → " + d["answer"]) if d.get("answer") else ""
    note = (" · " + d["note"]) if d.get("note") else ""
    return "#%-4d %-9s %-8s %-6s %s  %s%s%s" % (d["id"], d["status"], d["kind"], d["source"], _when(d["updated"]), d.get("title_en") or d["title_th"], ans, note)


def _trial_line(t):
    res = (" · " + t["result_impact"]) if t.get("result_impact") else ""
    return "trial %-3d #%-4d %-9s option %-14s %s  %s%s" % (t["id"], t["decision_id"], t["status"], t["option"], t["branch"], _when(t["updated"]), res)


def _money(usd: Optional[float]) -> str:
    return "" if usd is None else ("$%.2f" % usd if usd >= 0.01 or usd == 0 else "$%.4f" % usd)


def _counts(side: Optional[Dict[str, Any]]) -> str:
    """One side of dtree's own check: "4 passed, 0 failed" (and errors, skipped), or its status and why."""
    if not side:
        return "-"
    st = side.get("status") or "none"
    if side.get("passed") is None:
        return st + ((": " + str(side["summary"])[:120]) if side.get("summary") and side.get("summary") != st else "")
    text = "%d passed, %d failed" % (side.get("passed") or 0, side.get("failed") or 0)
    for k in ("errors", "skipped"):
        if side.get(k):
            text += ", %d %s" % (side[k], k)
    return text if st in ("passed", "failed") else "%s · %s" % (st, text)


def _measure_line(m: Dict[str, Any]) -> str:
    unit = (" " + m["unit"]) if m.get("unit") else ""
    if m.get("value") is None:
        return "%s: %s" % (m["name"], m.get("status") or "none")
    text = "%s %g%s" % (m["name"], m["value"], unit)
    if m.get("base") is None:
        return text + " (base: %s)" % (m.get("base_status") or "-")
    parts = ["base %g%s" % (m["base"], unit)]
    if m.get("delta") is not None:
        parts.append("%+g%s" % (m["delta"], unit))
    if m.get("delta_pct") is not None:
        parts.append("%+g%%" % m["delta_pct"])
    if m.get("verdict"):
        parts.append(m["verdict"])
    return text + " (%s)" % ", ".join(parts)


def compare_lines(r: Dict[str, Any]) -> List[str]:
    """One trial of ``dtree compare``: its row (agent, statuses, what the branch changes, the cost and time of all its jobs), the agent's
    own claim, and beside it what dtree checked itself — the tests on the branch's tip and on the base it left, the change, each measure
    — and whether it merges now (E3 of the real run: only --json had dtree's numbers)."""
    j = r.get("job") or {}
    cost = _money(j.get("cost_usd")) + (" + ?" if j.get("cost_usd") is not None and j.get("cost_unknown") else "")
    spent = cost or ("%s tok" % j["tokens"] if j.get("tokens") else "")
    lines = ["trial %-3d %-12s %-8s %-9s %-9s %2d commits %3d files +%-5d -%-5d %s %s" % (
        r["trial"], r["option"][:12], (r.get("agent") or "-")[:8], r["status"], j.get("status") or "-", r["commits"], r["files"], r["add"], r["del"],
        spent, ("%ds" % j["seconds"]) if j.get("seconds") else "")]
    lines.append("  claim: " + (r.get("tests") or ("job %s: %s" % (j.get("status"), j.get("reason") or "-") if j else "-")))
    v = r.get("verify") or {}
    if v.get("enabled") is False:
        lines.append("  dtree verify: off (verify.enabled is false)")
    elif v.get("running"):
        lines.append("  dtree verify: running now")
    elif not v.get("checked_at") and (v.get("status") or "none") == "none":
        lines.append("  dtree verify: not verified yet (dtree verify %d)" % r["trial"])
    else:
        line = "  dtree verify %s: %s" % ((v.get("sha") or "")[:7], v.get("status") or "none")
        if v.get("tests"):
            line += " · " + _counts(v["tests"])
        if v.get("base"):
            line += " · base %s: %s" % ((v.get("base_sha") or "")[:7], _counts(v["base"]))
        d = v.get("delta")
        if d:
            line += " · Δ passed %+d, failed %+d" % (d.get("passed") or 0, d.get("failed") or 0) + (", errors %+d" % d["errors"] if d.get("errors") else "")
        if v.get("stale"):
            line += " · the branch moved since (dtree verify %d again)" % r["trial"]
        if v.get("settings_changed"):
            line += " · the verify settings changed since"
        lines.append(line)
        lines += ["    " + _measure_line(m) for m in v.get("measures") or []]
    m = r.get("merge") or {}
    if m:
        lines.append("  merge: %s — %s" % ("yes" if m.get("can_merge") else "no (%s)" % (m.get("reason_code") or m.get("state") or "?"), m.get("reason") or "-"))
    return lines


def _job_line(j):
    cost = (" " + _money(j["cost_usd"])) if j.get("cost_usd") else ""
    return "job %-3d trial %-3d #%-4d %-8s %-9s %s%s  %s" % (j["id"], j["trial_id"], j["decision_id"], j.get("agent") or "?", j["status"], _when(j["updated"]), cost, j.get("reason") or "")


def _branch_for(p: Project, name: str) -> str:
    g = p.git
    if not g.available():
        return "no git repository: branch %s not made" % name
    if g.has_branch(name):
        return "branch %s is there already" % name
    r = g.run("branch", name, p.base)
    return ("git branch %s failed: %s" % (name, r.stderr.strip())) if r.returncode else "branch %s made from %s (no checkout)" % (name, p.base)


def start(p: Project, did: int) -> str:
    """Switch the working tree to the decision's branch, made from the base the first time."""
    s, g = p.store, p.git
    d = s.get(did)
    if d["status"] != "decided":
        raise DecisionError("question %d is %s: only a decided question is started" % (did, d["status"]))
    name = d.get("branch") or "dec/%d-%s" % (did, re.sub(r"[^A-Za-z0-9_.-]+", "-", d["answer"] or "note").strip("-.") or "note")
    if g.current_branch() == name:
        return "on %s already" % name
    exists = g.has_branch(name)
    r = g.run("switch", name) if exists else g.run("switch", "-c", name, p.base)
    if r.returncode != 0:
        raise DecisionError("git switch %s failed: %s" % (name, r.stderr.strip()))
    if not d.get("branch"):
        s.record_code(did, [], branch=name, how="recorded", why="branch started", by="cli")
    return "on %s (%s); commit there, then: dtree merge %d" % (name, "existing" if exists else "new, from " + p.base, did)


def merge(p: Project, did: int, trial: Optional[int] = None, close: bool = False) -> str:
    """Merge the decision's branch (or its adopted trial's) into the base without fast-forward and without switching anyone's checkout
    (merge.py: plumbing when the base is checked out nowhere, else ``git merge`` in its clean worktree); the branch is kept and the
    commits are recorded. ``close`` (with ``trial``): the question is marked done with the trial's measurement."""
    from .merge import merge_decision, merge_trial                  # the module: this function has taken its name here
    if trial is not None:
        if p.store.trial(trial)["decision_id"] != did:
            raise DecisionError("trial %d is not on question %d" % (trial, did))
        out = merge_trial(p, trial, by="cli", close=close)
    elif close:
        raise DecisionError("--close goes with --trial; a question's own work is closed with: dtree done %d --tests … --impact …" % did)
    else:
        out = merge_decision(p, did, by="cli")
    m = out["merge"]
    where = {"plumbing": "by plumbing, no checkout touched", "worktree": "git merge in %s" % m.get("worktree")}.get(m["how"], m["how"])
    if m["how"] == "worktree" and not m.get("worktree"):
        where = "git merge in a temporary worktree"
    text = "merged %s into %s: %d commits + merge %s (%s; the branch is kept)" % (m["branch"], p.base, m["commits"], m["sha"][:7], where)
    if m.get("warning"):                                             # made, but git did not end well after it (a post-merge hook)
        text += "\nwarning: " + m["warning"]
    if close:
        text += "\n" + ("#%d is done" % did if out.get("closed") else "#%d is not marked done: %s" % (did, out.get("close_error")))
    return text


def code(p: Project, did: int, shas: List[str], why: str, how: str, trial: Optional[int] = None) -> str:
    found = []
    for sha in shas:
        info = p.git.commit_info(sha.lower())
        if info is None:
            raise DecisionError("no commit %s in this repository" % sha)
        found.append({"sha": info["sha"], "subject": info["subject"], "merge": info["merge"]})
    p.store.record_code(did, found, how=how, why=why, trial=trial, by="cli")
    return "#%d: %s" % (did, ", ".join(c["sha"][:7] + " " + c["subject"][:50] for c in found))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="dtree", description="decision tree for people and AI agents")
    ap.add_argument("--project", default=None)
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--name", default=None)
    p = sub.add_parser("serve"); p.add_argument("--host", default=None); p.add_argument("--port", type=int, default=None)
    sub.add_parser("mcp")
    sub.add_parser("agents")
    p = sub.add_parser("list"); p.add_argument("--status", default="open"); p.add_argument("--q", default=""); p.add_argument("--json", action="store_true")
    p = sub.add_parser("show"); p.add_argument("id", type=int)
    p = sub.add_parser("add"); p.add_argument("file")
    p = sub.add_parser("answer"); p.add_argument("id", type=int); p.add_argument("option"); p.add_argument("--note", default="")
    p = sub.add_parser("done"); p.add_argument("id", type=int); p.add_argument("--tests", required=True); p.add_argument("--impact", required=True)
    for name in ("dismiss", "reopen"):
        p = sub.add_parser(name); p.add_argument("id", type=int); p.add_argument("--note", default="")
    p = sub.add_parser("comment"); p.add_argument("id", type=int); p.add_argument("text")
    p = sub.add_parser("follow"); p.add_argument("id", type=int); p.add_argument("--on", type=int, required=True)
    p.add_argument("--how", default="topic", choices=("topic", "sequence")); p.add_argument("--why", required=True)
    p = sub.add_parser("try"); p.add_argument("id", type=int); p.add_argument("option"); p.add_argument("--note", default="")
    p.add_argument("--no-branch", action="store_true")
    p = sub.add_parser("trials"); p.add_argument("--status", default="open")
    p = sub.add_parser("branch"); p.add_argument("trial", type=int)
    p = sub.add_parser("measured"); p.add_argument("trial", type=int); p.add_argument("--tests", required=True); p.add_argument("--impact", required=True)
    p = sub.add_parser("end"); p.add_argument("trial", type=int); p.add_argument("outcome", choices=("adopted", "switch", "dropped")); p.add_argument("--note", default="")
    p.add_argument("--drop-others", action="store_true")
    p = sub.add_parser("start"); p.add_argument("id", type=int)
    p = sub.add_parser("merge"); p.add_argument("id", type=int); p.add_argument("--trial", type=int, default=None)
    p = sub.add_parser("code"); p.add_argument("id", type=int); p.add_argument("sha", nargs="+"); p.add_argument("--why", required=True)
    p.add_argument("--how", default="recorded", choices=("recorded", "backfilled")); p.add_argument("--trial", type=int, default=None)
    p = sub.add_parser("ai"); p.add_argument("trial", type=int); p.add_argument("--agent", default=None); p.add_argument("--note", default="")
    p = sub.add_parser("race"); p.add_argument("id", type=int); p.add_argument("option"); p.add_argument("--agents", required=True); p.add_argument("--note", default="")
    p = sub.add_parser("compare"); p.add_argument("id", type=int); p.add_argument("--json", action="store_true")
    p = sub.add_parser("followup"); p.add_argument("trial", type=int); p.add_argument("--feedback", required=True); p.add_argument("--agent", default=None)
    p = sub.add_parser("diff"); p.add_argument("trial", type=int)
    p = sub.add_parser("supersede"); p.add_argument("id", type=int); p.add_argument("--by", type=int, required=True); p.add_argument("--note", default="")
    p = sub.add_parser("adr"); p.add_argument("--out", default=None); p.add_argument("--status", default=None)
    sub.add_parser("context")
    p = sub.add_parser("jobs"); p.add_argument("--trial", type=int, default=None)
    p = sub.add_parser("log"); p.add_argument("job", type=int); p.add_argument("--lines", type=int, default=60)
    p = sub.add_parser("stop"); p.add_argument("job", type=int)
    for mod in FEATURES:
        mod.register_cli(sub)
    args = ap.parse_args(argv)
    if args.cmd == "init":
        print(init(args.project or os.getcwd(), args.name))
        return 0
    if args.db:
        os.environ["DTREE_DB"] = os.path.abspath(args.db)
    proj = Project(args.project) if args.project else Project.find()
    if args.cmd == "serve":
        from .server import serve
        serve(proj, args.host, args.port)
        return 0
    if args.cmd == "mcp":
        from .mcp import serve_stdio
        serve_stdio(proj)
        return 0
    s = proj.store
    try:
        if any(mod.run_cli(args, proj) for mod in FEATURES):
            return 0
        if args.cmd == "agents":
            for a in jobs.agent_list(proj):
                print("%-8s %-6s %-4s %-28s %s" % (a["name"], a["kind"], "yes" if a["available"] else "no", a["label"], a["reason"] or a.get("model") or ""))
        elif args.cmd == "list":
            items = s.list(args.status, 500, args.q)
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=1))
            else:
                for d in items:
                    print(_line(d))
                print("counts:", s.counts())
        elif args.cmd == "show":
            print(json.dumps(s.get(args.id), ensure_ascii=False, indent=1))
        elif args.cmd == "add":
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            for entry in (data if isinstance(data, list) else [data]):
                entry.setdefault("source", "agent")
                print(_line(s.add(entry, by=entry["source"])))
        elif args.cmd == "answer":
            print(_line(s.answer(args.id, args.option, args.note, by="cli")))
        elif args.cmd == "done":
            print(_line(s.done(args.id, args.tests, args.impact, by="cli")))
        elif args.cmd in ("dismiss", "reopen"):
            print(_line(getattr(s, args.cmd)(args.id, args.note, by="cli")))
        elif args.cmd == "comment":
            print(_line(s.comment(args.id, args.text, by="cli")))
        elif args.cmd == "follow":
            print(_line(s.follow(args.id, args.on or None, args.how, args.why, by="cli")))
        elif args.cmd == "try":
            t = s.try_option(args.id, args.option, args.note, by="cli")
            print(_trial_line(t))
            if not args.no_branch:
                print(_branch_for(proj, t["branch"]))
            if t["review"]:
                print("to look at again if it is adopted (grew on the answer now): " + " ".join("#%d" % i for i in t["review"]))
        elif args.cmd == "trials":
            for t in s.trials(args.status):
                print(_trial_line(t))
        elif args.cmd == "branch":
            print(_branch_for(proj, s.trial(args.trial)["branch"]))
        elif args.cmd == "measured":
            print(_trial_line(s.trial_result(args.trial, args.tests, args.impact, by="cli")))
        elif args.cmd == "end":
            print(_trial_line(s.trial_end(args.trial, args.outcome, args.note, by="cli", drop_others=args.drop_others)))
        elif args.cmd == "start":
            print(start(proj, args.id))
        elif args.cmd == "merge":
            print(merge(proj, args.id, args.trial))
        elif args.cmd == "code":
            print(code(proj, args.id, args.sha, args.why, args.how, args.trial))
        elif args.cmd == "ai":
            print(_job_line(jobs.send(proj, args.trial, args.agent, args.note, by="cli")))
        elif args.cmd == "race":
            for r in jobs.race(proj, args.id, args.option, [x.strip() for x in args.agents.split(",")], args.note, by="cli"):
                print("%-8s %s" % (r["agent"], _job_line(r["job"]) if r["ok"] else "not started: " + r["error"]))
        elif args.cmd == "compare":
            c = jobs.compare(proj, args.id)
            if args.json:
                print(json.dumps(c, ensure_ascii=False, indent=1))
            else:
                print("#%d %s · answer %s · base %s" % (args.id, c["status"], c["answer"] or "-", c["base"]))
                for r in c["rows"]:
                    print("\n".join(compare_lines(r)))
        elif args.cmd == "followup":
            print(_job_line(jobs.followup(proj, args.trial, args.feedback, args.agent, by="cli")))
        elif args.cmd == "diff":
            d = proj.git.diff(proj.base, s.trial(args.trial)["branch"])
            print(d["diff"] + ("\n…(cut)" if d["cut"] else ""))
        elif args.cmd == "supersede":
            print(_line(s.supersede(args.id, args.by or None, args.note, by="cli")))
        elif args.cmd == "adr":
            from . import adr
            paths = adr.export(proj, args.out, args.status.split(",") if args.status else None)
            print("%d files in %s" % (len(paths), paths[-1].parent if paths else "-"))
        elif args.cmd == "context":
            from . import adr
            print(adr.context(proj), end="")
        elif args.cmd == "jobs":
            jobs.reap(s)
            for j in s.jobs(tid=args.trial):
                print(_job_line(j))
        elif args.cmd == "log":
            print("\n".join(jobs.log_tail(s.job(args.job), args.lines)))
        elif args.cmd == "stop":
            print(_job_line(jobs.stop(s, args.job, by="cli")))
    except (DecisionError, KeyError) as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
