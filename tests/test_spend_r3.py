"""spend.py and the agents' caps after the final review of 2026-11-02: a running job is held to what is left of the month and of the
agent's total (F7), an agent's all-time caps for a credit (F19), Codex's tokens unknown when it reported none (F14), tokens counted only
in a total are priced (F16), the jobs whose dollars are unknown are counted and shown (F17, E10), the settings that refuse every job
(F18), and Claude Code's tools that reach the account or the web disallowed by default (E11). No quota spent."""
import json
import sys
import time
from pathlib import Path

import pytest

from dtree import agents, cli, jobs, spend
from dtree.project import AGENTS, Project
from dtree.store import DecisionError
from test_chat import FIN, models  # noqa: F401  (models is a fixture)

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "sandbox-and-budget.md").read_text(encoding="utf-8")
SCHEMA = json.loads((ROOT / "src" / "dtree" / "schema" / "config.schema.json").read_text(encoding="utf-8"))
Q = {"kind": "decision", "source": "claude", "key": "way", "title_th": "ทางเดิน", "title_en": "The way", "question_th": "ทางไหน", "question_en": "Which way?",
     "options": [{"key": "left", "th": "ซ้าย", "en": "Left", "recommended": True}, {"key": "right", "th": "ขวา", "en": "Right"}]}
CLOUD = ("WebSearch", "WebFetch", "RemoteTrigger", "CronCreate", "CronDelete", "CronList", "Workflow", "SendMessage")
MONTH = time.strftime("%Y-%m")


def _base(repo):
    return {"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"), "db": str(repo["tmp"] / "d.sqlite"),
            "chat": {"sandbox": "none"}}


def _project(repo, **extra):
    cfg = dict(_base(repo), agents={"muse": {"base_url": "https://api.example.invalid/v1", "key_env": None, "price_in_per_mtok": 1.0,
                                             "price_out_per_mtok": 2.0},
                                    "plain": {"kind": "chat", "base_url": "https://api.example.invalid/v1", "model": "m"}})
    cfg.update(extra)
    return Project(repo["path"], cfg)


def _config(repo, extra):
    """A project read from its .dtree/config.json, as the CLI reads it."""
    d = repo["path"] / ".dtree"
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text(json.dumps(dict(_base(repo), **extra)), encoding="utf-8")
    return repo["path"]


def _trial(p):
    did = p.store.add(Q)["id"]
    p.store.answer(did, "left")
    return did, p.store.try_option(did, "right")


def _job(p, tid, agent, status="measured", cost=None, result=None, created=None, ran=True):
    """A job as the runner leaves it; ``ran``: it got as far as its agent (the runner records the worktree just before)."""
    j = p.store.job_open(tid, agent=agent, max_running=100)
    if ran:
        p.store.job_set(j["id"], worktree="/nonexistent/wt-%d" % j["id"])
    if cost is not None:
        p.store.job_set(j["id"], cost_usd=cost)
    if status != "running":
        p.store.job_end(j["id"], status, "done", result or {})
    if created is not None:
        with p.store._lock:
            p.store._db.execute("UPDATE jobs SET created = ?, ended_at = ? WHERE id = ?", (created, created + 60, j["id"]))
            p.store._db.commit()
    return j["id"]


def _last_month():
    t = time.localtime()
    return time.mktime((t.tm_year - (t.tm_mon == 1), (t.tm_mon - 2) % 12 + 1, 15, 12, 0, 0, 0, 0, -1))


def _ctx(repo, limits):
    ctx = agents.Context(repo["path"], repo["path"] / ".git", lambda l: None, lambda pid: None, lambda: False, time.time() + 60)
    ctx.limits = limits
    return ctx


def _line(text, name):
    return [l for l in text.splitlines() if l.split()[:1] == [name]][0]


# ------------------------------------------------------------------------------------- F7: a running job is held to what is left
def test_f7_a_chat_job_is_held_to_what_is_left_of_its_agent_s_month_tokens(repo, models):
    """The review's probe D: 20 of the month's 25 tokens used, the next job ran to 120."""
    model = models([{"calls": [("list_files", {})]}] * 9 + [{"calls": [("finish", FIN)]}])
    p = _project(repo, agents={"muse2": {"kind": "chat", "base_url": model.url, "model": "m", "month_tokens": 25}})
    did, t = _trial(p)
    _job(p, t["id"], "muse2", result={"tokens": 20})
    spend.check(p, "muse2", "user", did)                                          # 5 left: it may start
    lim = spend.limits(p, p.agent("muse2"))
    assert lim["max_tokens_total"] == 5 and lim["from"] == {"max_tokens_total": "agents.muse2.month_tokens"}, lim
    j = jobs.run(p, p.store.job_open(t["id"], agent="muse2")["id"])
    assert j["status"] == "failed" and j["reason"] == "budget: 10 tokens of cap 5 (what was left of agents.muse2.month_tokens)", j["reason"]
    assert len(model.requests) == 1 and spend.spent_tokens(p, None, "muse2") == 30            # one answer past it, not nine


def test_f7_claude_s_dollar_cap_is_what_is_left_of_the_month(repo):
    """Probe D2: $9.99 of budget.month_usd $10 spent, and the next Claude Code job had no --max-budget-usd at all."""
    p = _project(repo, budget={"month_usd": 10.0})
    did, t = _trial(p)
    _job(p, t["id"], "claude", cost=9.99, result={"cost_usd": 9.99})
    spend.check(p, "claude", "user", did)
    lim = spend.limits(p, p.agent("claude"))
    assert lim["max_budget_usd"] == pytest.approx(0.01) and lim["max_tokens_total"] is None and lim["from"] == {"max_budget_usd": "budget.month_usd"}
    cmd = agents.make(p.agent("claude"), p)._command("/usr/bin/claude", True, lim["max_budget_usd"])
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.01"
    assert spend.limits(p, p.agent("local")) == {"max_budget_usd": None, "max_tokens_total": None}     # it costs nothing: never held by it
    # the agent's own month, when smaller, and a job cap smaller than both stays the job's
    p2 = _project(repo, budget={"month_usd": 20.0, "job_usd": 1.0}, agents={"claude": {"month_usd": 10.5}})
    assert spend.limits(p2, p2.agent("claude")) == {"max_budget_usd": pytest.approx(0.51), "max_tokens_total": None,
                                                      "from": {"max_budget_usd": "agents.claude.month_usd"}}
    p3 = _project(repo, budget={"month_usd": 20.0, "job_usd": 0.3})
    assert spend.limits(p3, p3.agent("claude")) == {"max_budget_usd": 0.3, "max_tokens_total": None}


def test_f7_dollars_left_become_tokens_for_an_agent_that_reports_tokens_only(repo):
    p = _project(repo, agents={"muse": {"month_usd": 1.0, "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0, "key_env": None,
                                        "base_url": "https://api.example.invalid/v1"}})
    did, t = _trial(p)
    _job(p, t["id"], "muse", cost=0.99, result={"cost_usd": 0.99, "cost_estimated": True, "tokens": 100})
    lim = spend.limits(p, p.agent("muse"))                                        # $0.01 left at the higher price, $2 per million
    assert lim == {"max_budget_usd": None, "max_tokens_total": 5000, "from": {"max_tokens_total": "agents.muse.month_usd"}}, lim
    p2 = _project(repo, budget={"month_usd": 1.0}, agents={"codex": {"price_in_per_mtok": 1.25, "price_out_per_mtok": 10.0}})
    assert spend.limits(p2, p2.agent("codex"))["max_tokens_total"] == 1000            # budget.month_usd's $0.01 at $10 per million
    assert spend.limits(p2, p2.agent("grok"))["max_tokens_total"] is None            # no prices: its dollars cannot hold it
    p3 = _project(repo, budget={"month_usd": 1.0, "job_tokens": 300}, agents={"codex": {"price_in_per_mtok": 1.25, "price_out_per_mtok": 10.0}})
    assert spend.limits(p3, p3.agent("codex")) == {"max_budget_usd": None, "max_tokens_total": 300}  # the job's own cap is smaller


def test_f7_nothing_left_is_a_cap_of_nothing_and_no_agent_starts_on_it(repo, models):
    """Two jobs of a race passed the check together and the first took the month past the cap: the second gets 0, never no cap."""
    model = models([{"calls": [("finish", FIN)]}])
    p = _project(repo, budget={"month_usd": 1.0},
                 agents={"muse2": {"kind": "chat", "base_url": model.url, "model": "m", "month_tokens": 25},
                         "codex": {"command": "/nonexistent/codex", "price_in_per_mtok": 1.25, "price_out_per_mtok": 10.0}})
    did, t = _trial(p)
    _job(p, t["id"], "muse2", result={"tokens": 30})
    _job(p, t["id"], "claude", cost=1.5, result={"cost_usd": 1.5})
    lim = spend.limits(p, p.agent("muse2"))
    assert lim["max_tokens_total"] == 0, lim
    out = agents.make(p.agent("muse2"), p).run("the task", _ctx(repo, lim))
    assert out["error"] == "budget: 0 tokens of cap 0 (what was left of agents.muse2.month_tokens)" and not model.requests, out
    lim = spend.limits(p, p.agent("claude"))
    assert lim["max_budget_usd"] == 0.0, lim
    out = agents.make(p.agent("claude"), p).run("the task", _ctx(repo, lim))
    assert out["error"] == "budget: nothing was left of budget.month_usd; Claude Code was not started", out
    lim = spend.limits(p, p.agent("codex"))
    out = agents.make(p.agent("codex"), p).run("the task", _ctx(repo, lim))
    assert lim["max_tokens_total"] == 0 and out["error"] == "budget: nothing was left of budget.month_usd; Codex was not started", out


# ------------------------------------------------------------------------------ F19: an agent's total, for a credit across months
def test_f19_a_total_cap_holds_muse_to_its_credit_across_months(repo):
    """The user's need (2026-11-02): Muse kept within its $150 hackathon credit — a total. A month cap starts again on the 1st."""
    cfg = {"month_usd": 150, "total_usd": 150, "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0, "key_env": None,
           "base_url": "https://api.example.invalid/v1"}
    p = _project(repo, agents={"muse": cfg})
    did, t = _trial(p)
    _job(p, t["id"], "muse", cost=100.0, result={"cost_usd": 100.0, "cost_estimated": True, "tokens": 1000}, created=_last_month())
    _job(p, t["id"], "muse", cost=40.0, result={"cost_usd": 40.0, "cost_estimated": True, "tokens": 400})
    spend.check(p, "muse", "user", did)                                           # $140 of the $150
    lim = spend.limits(p, p.agent("muse"))                                        # $10 left in all, at $2 per million
    assert lim == {"max_budget_usd": None, "max_tokens_total": 5_000_000, "from": {"max_tokens_total": "agents.muse.total_usd"}}, lim
    _job(p, t["id"], "muse", cost=15.0, result={"cost_usd": 15.0, "cost_estimated": True, "tokens": 150})
    with pytest.raises(DecisionError) as e:
        spend.check(p, "muse", "user", did)
    assert "$155.00 of $150.00" in str(e.value) and "agents.muse.total_usd" in str(e.value), str(e.value)
    ev = [x for x in p.store.get(did)["events"] if x["action"] == "budget_refused"]
    assert ev[-1]["detail"] == {"agent": "muse", "setting": "agents.muse.total_usd", "spent_usd": 155.0, "cap_usd": 150.0, "period": "total"}
    spend.check(_project(repo, agents={"muse": dict(cfg, total_usd=None)}), "muse", "user", did)   # the month alone: $55 of $150


def test_f19_a_total_in_tokens_needs_no_price(repo):
    p = _project(repo, agents={"muse": {"total_tokens": 1000, "key_env": None, "base_url": "https://api.example.invalid/v1",
                                        "price_in_per_mtok": None, "price_out_per_mtok": None}})
    did, t = _trial(p)
    _job(p, t["id"], "muse", result={"tokens": 700}, created=_last_month())
    _job(p, t["id"], "muse", result={"tokens_in": 200, "tokens_out": 50})
    spend.check(p, "muse", "user", did)
    assert spend.limits(p, p.agent("muse"))["max_tokens_total"] == 50
    _job(p, t["id"], "muse", status="failed", result={"tokens": 60})
    with pytest.raises(DecisionError) as e:
        spend.check(p, "muse", "user", did)
    assert "1,010 of 1,000 tokens" in str(e.value) and "agents.muse.total_tokens" in str(e.value), str(e.value)
    ev = [x for x in p.store.get(did)["events"] if x["action"] == "budget_refused"]
    assert ev[-1]["detail"] == {"agent": "muse", "setting": "agents.muse.total_tokens", "spent_tokens": 1010, "cap_tokens": 1000, "period": "total"}


def test_f19_a_total_in_dollars_needs_prices_and_every_total_must_be_a_number(repo):
    with pytest.raises(DecisionError) as e:
        spend.check(_project(repo, agents={"muse": {"total_usd": 150, "price_in_per_mtok": None, "price_out_per_mtok": None}}), "muse")
    assert "agents.muse.total_usd" in str(e.value) and "price_in_per_mtok" in str(e.value) and "agents.muse.total_tokens" in str(e.value)
    for bad, word in (({"total_usd": "lots"}, "total_usd"), ({"total_tokens": -5}, "total_tokens")):
        with pytest.raises(DecisionError) as e:
            spend.check(_project(repo, agents={"muse": bad}), "muse")
        assert "agents.muse." + word in str(e.value), str(e.value)
    spend.check(_project(repo, agents={"claude": {"total_usd": 5}}), "claude")             # Claude Code reports its dollars itself


def test_f19_the_report_the_cli_and_describe_show_each_agent_s_total(repo, capsys):
    root = _config(repo, {"agents": {"muse": {"total_usd": 150, "total_tokens": 1_000_000, "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0},
                                     "codex": {"total_tokens": 500}}})
    p = Project(root)
    did, t = _trial(p)
    _job(p, t["id"], "muse", cost=100.0, result={"cost_usd": 100.0, "cost_estimated": True, "tokens": 400_000}, created=_last_month())
    _job(p, t["id"], "muse", cost=12.5, result={"cost_usd": 12.5, "cost_estimated": True, "tokens": 100_000})
    r = spend.report(p)
    m, cx = r["agents"]["muse"], r["agents"]["codex"]
    assert m["usd"] == 12.5 and (m["cap_total_usd"], m["spent_total_usd"], m["remaining_total_usd"]) == (150.0, 112.5, 37.5), m
    assert (m["cap_total_tokens"], m["spent_total_tokens"], m["remaining_total_tokens"]) == (1_000_000, 500_000, 500_000)
    assert cx["jobs"] == 0 and (cx["cap_total_tokens"], cx["remaining_total_tokens"], cx["cap_total_usd"]) == (500, 500, None)
    assert "cap_total_usd" not in r["totals"]
    d = agents.make(p.agent("muse"), p).describe()
    assert (d["total_usd"], d["total_tokens"]) == (150.0, 1_000_000)
    assert agents.make(p.agent("claude"), p).describe()["total_usd"] is None
    assert cli.main(["--project", str(root), "spend"]) == 0
    out = capsys.readouterr().out
    assert ("muse: $112.50 of $150.00 in all (agents.muse.total_usd), $37.50 left · 500,000 of 1,000,000 tokens in all "
            "(agents.muse.total_tokens), 500,000 left") in out, out
    assert "codex: 0 of 500 tokens in all (agents.codex.total_tokens), 500 left" in out


def test_f19_the_docs_keep_a_credit_with_a_total_cap():
    credit = [p for p in DOC.split("\n\n") if "credit" in p and "{" in p]                 # the examples given for a credit
    assert credit and all('"total_usd"' in p or '"total_tokens"' in p for p in credit), credit
    assert all('"month_usd"' not in p and '"month_tokens"' not in p for p in credit), credit
    props = SCHEMA["$defs"]["agent"]["properties"]
    assert "total_usd" in props and "total_tokens" in props and "credit" in props["total_usd"]["description"]


# -------------------------------------------------------------------------------------- F14: Codex's tokens unknown, not zero
CODEX_NO_USAGE = r'''#!%s
import json, sys
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
sys.stdin.read()
def ev(d):
    print(json.dumps(d), flush=True)
ev({"type": "thread.started", "thread_id": "t7"})
ev({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "pytest -q", "exit_code": 0}})
ev({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}})
sys.exit(1)
'''


def test_f14_a_codex_job_that_reported_no_usage_has_unknown_tokens_not_zero(repo):
    fake = repo["tmp"] / "codex"
    fake.write_text(CODEX_NO_USAGE % sys.executable)
    fake.chmod(0o755)
    p = _project(repo, agents={"codex": {"command": str(fake), "price_in_per_mtok": 1.25, "price_out_per_mtok": 10.0, "month_usd": 5}})
    did, t = _trial(p)
    j = jobs.run(p, p.store.job_open(t["id"], agent="codex")["id"])
    r = j["result"]
    assert j["status"] == "failed" and "Codex failed" in j["reason"], j["reason"]
    assert (r["tokens"], r["tokens_in"], r["tokens_out"]) == (None, None, None) and j["cost_usd"] is None and not r.get("cost_estimated"), r
    assert "no usage" in "\n".join(jobs.log_tail(j))
    row = spend.report(p)["agents"]["codex"]
    assert row["usd_unknown_jobs"] == 1 and row["usd"] == 0.0 and row["remaining_usd"] == 5.0


# --------------------------------------------------------------------------------------------- F16: a total beyond in + out priced
def test_f16_tokens_counted_only_in_the_total_are_priced_at_the_higher_price(repo, models):
    cfg = {"price_in_per_mtok": 3.0, "price_out_per_mtok": 15.0}
    assert spend.estimate(cfg, {"tokens_in": 1000, "tokens_out": 100, "tokens": 5100}) == pytest.approx((1000 * 3 + 100 * 15 + 4000 * 15) / 1e6)
    assert spend.estimate(cfg, {"tokens_in": 1000, "tokens_out": 100, "tokens": 1100}) == pytest.approx(0.0045)
    assert spend.estimate({"price_in_per_mtok": 15.0, "price_out_per_mtok": 3.0}, {"tokens_in": 10, "tokens_out": 0, "tokens": 20}) == \
        pytest.approx((10 * 15 + 10 * 15) / 1e6)
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 5100, "completion_tokens_details": {"reasoning_tokens": 4000}}
    model = models([{"calls": [("finish", FIN)], "usage": usage}])
    p = _project(repo, agents={"grok2": {"kind": "chat", "base_url": model.url, "model": "grok-4", "price_in_per_mtok": 3.0, "price_out_per_mtok": 15.0}})
    did, t = _trial(p)
    j = jobs.run(p, p.store.job_open(t["id"], agent="grok2")["id"])
    assert j["result"]["tokens"] == 5100 and j["cost_usd"] == pytest.approx((1000 * 3.0 + 4100 * 15.0) / 1e6), j


# ---------------------------------------------------------------------------------- F17, E10: unknown dollars counted and shown
def test_f17_rows_and_the_total_count_the_jobs_whose_dollars_are_unknown(repo, capsys):
    root = _config(repo, {"budget": {"month_usd": 5}})
    p = Project(root)
    did, t = _trial(p)
    _job(p, t["id"], "claude", cost=1.25, result={"cost_usd": 1.25, "tokens": 50000})
    _job(p, t["id"], "claude", status="stopped", result={"cost_usd": None, "tokens_in": 400000, "tokens_out": 900, "tokens": 400900})
    _job(p, t["id"], "claude", status="failed", ran=False)                     # the runner never reached its agent: nothing spent
    r = spend.report(p)
    c = r["agents"]["claude"]
    assert (c["jobs"], c["usd_unknown_jobs"], c["usd_known_jobs"], r["totals"]["usd_unknown_jobs"]) == (3, 1, 1, 1), c
    assert cli.main(["--project", str(root), "spend"]) == 0
    out = capsys.readouterr().out
    assert "spend %s · $1.25 + ? of $5.00 (budget.month_usd) · remaining at most $3.75 · 1 job of unknown cost" % MONTH in out, out
    assert "$1.25 + ?" in _line(out, "claude") and "$1.25 + ?" in _line(out, "total")


def test_e10_spend_shows_a_dash_for_unknown_dollars_a_running_column_and_small_amounts(repo, capsys):
    """The real run: the Orin's row read $0.00 while its job ran and after it failed (no price), and Claude's $0.0058 read $0.01."""
    root = _config(repo, {"agents": {"orin": {"kind": "chat", "base_url": "http://127.0.0.1:9/v1", "model": "m", "key_env": None}}})
    p = Project(root)
    did, t = _trial(p)
    _job(p, t["id"], "claude", cost=0.0058, result={"cost_usd": 0.0058, "tokens": 78000})
    _job(p, t["id"], "orin", status="failed", result={"tokens": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": None})
    t2 = p.store.try_option(did, "right", variant="orin")
    _job(p, t2["id"], "orin", status="running")
    assert cli.main(["--project", str(root), "spend"]) == 0
    out = capsys.readouterr().out
    head = _line(out, "agent").split()
    assert head[:7] == ["agent", "jobs", "measured", "failed", "stopped", "running", "usd"], head
    orin, claude, total = _line(out, "orin").split(), _line(out, "claude").split(), _line(out, "total").split()
    assert orin[1:7] == ["2", "0", "1", "0", "1", "-"], orin
    assert claude[6] == "$0.0058" and total[6:9] == ["$0.0058", "+", "?"] and "$0.01" not in out, out
    assert out.startswith("spend %s · $0.0058 + ?" % MONTH)


# ------------------------------------------------------------------------------------ F18: the settings that refuse every job
def test_f18_settings_problem_names_the_settings_that_would_refuse_every_job(repo):
    p = _project(repo, agents={"muse": {"month_usd": 150, "price_in_per_mtok": None, "price_out_per_mtok": None},
                               "grok": {"price_in_per_mtok": "x"}, "off": {"kind": "chat", "base_url": "https://x.invalid/v1", "model": "m", "month_tokens": 0}})
    sp = spend.settings_problem
    assert "agents.muse.month_usd cannot be counted" in sp(p, p.agent("muse"))
    with pytest.raises(DecisionError) as e:
        spend.check(p, "muse")
    assert str(e.value) == sp(p, p.agent("muse"))                                 # what a send would be refused with
    assert "agents.grok.price_in_per_mtok" in sp(p, p.agent("grok"))
    assert "agents.off.month_tokens is 0" in sp(p, p.agent("off"))
    assert sp(p, p.agent("claude")) is None and sp(p, p.agent("local")) is None
    p0 = _project(repo, budget={"month_usd": 0})
    assert "budget.month_usd is 0" in sp(p0, p0.agent("claude")) and sp(p0, p0.agent("local")) is None     # a free agent still runs
    pb = _project(repo, budget={"job_usd": "x"})
    assert "budget.job_usd must be a number" in sp(pb, pb.agent("claude"))


# ------------------------------------------------------------------------------- E11: Claude Code's cloud tools disallowed by default
def test_e11_claude_code_s_tools_that_reach_the_account_or_the_web_are_disallowed_by_default(repo):
    assert set(CLOUD) <= set(AGENTS["claude"]["disallowed_tools"])
    p = _project(repo)
    cmd = agents.make(p.agent("claude"), p)._command("/usr/bin/claude", True, None)
    assert set(CLOUD) <= set(cmd[cmd.index("--disallowedTools") + 1:])
    assert all(n in DOC for n in CLOUD)
    assert "WebSearch" in SCHEMA["$defs"]["agent"]["properties"]["disallowed_tools"]["description"]
