"""spend.py and the agents' caps: a job's caps from the agent or the project, cost estimated from tokens and prices, the month's budget
refusing a job (with its event), the report per agent (API and CLI), and the token caps of the chat loop and Codex. No quota spent."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import ApiClient
from dtree import agents, cli, jobs, spend
from dtree.project import AGENTS, Project
from dtree.store import DecisionError

Q = {"kind": "decision", "source": "claude", "key": "way", "title_th": "ทางเดิน", "title_en": "The way", "question_th": "ทางไหน", "question_en": "Which way?",
     "options": [{"key": "left", "th": "ซ้าย", "en": "Left", "recommended": True}, {"key": "right", "th": "ขวา", "en": "Right"}]}


def _project(repo, **extra):
    cfg = {"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"), "db": str(repo["tmp"] / "d.sqlite"),
           "agents": {"muse": {"base_url": "https://api.example.invalid/v1", "key_env": None, "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0},
                      "plain": {"kind": "chat", "base_url": "https://api.example.invalid/v1", "model": "m"}}}
    cfg.update(extra)
    return Project(repo["path"], cfg)


def _trial(p):
    did = p.store.add(Q)["id"]
    p.store.answer(did, "left")
    return did, p.store.try_option(did, "right")


def _job(p, tid, agent, status="measured", cost=None, result=None, created=None):
    """A finished job as the runner leaves it in the store."""
    j = p.store.job_open(tid, agent=agent, max_running=100)
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


def test_a_job_s_caps_come_from_the_agent_else_the_project():
    p = Project("/nonexistent-dtree-test", {"budget": {"job_usd": 1.5, "job_tokens": 1000}})
    assert spend.limits(p, {}) == {"max_budget_usd": 1.5, "max_tokens_total": 1000}
    assert spend.limits(p, {"max_budget_usd": 0.5, "max_tokens_total": 50}) == {"max_budget_usd": 0.5, "max_tokens_total": 50}
    assert spend.limits(Project("/nonexistent-dtree-test", {}), {}) == {"max_budget_usd": None, "max_tokens_total": None}
    for bad in ("abc", -1, True, 0):                                            # never a crash in the runner: no cap instead
        assert spend.limits(Project("/nonexistent-dtree-test", {}), {"max_budget_usd": bad})["max_budget_usd"] is None


def test_prices_estimate_the_cost_of_tokens():
    assert spend.prices({}) is None and spend.prices({"price_in_per_mtok": 3}) == {"in": 3.0, "out": 3.0}
    cfg = {"price_in_per_mtok": 3, "price_out_per_mtok": 15}
    assert spend.estimate(cfg, {"tokens_in": 1_000_000, "tokens_out": 500_000}) == 10.5
    assert spend.estimate(cfg, {"tokens": 1000}) == 0.015                       # no split: the higher price
    assert spend.estimate(cfg, {"tokens": 1000, "tokens_in": 0, "tokens_out": 0}) == 0.015
    assert spend.estimate(cfg, {}) is None and spend.estimate(cfg, {"tokens": 0}) == 0.0 and spend.estimate({}, {"tokens": 5}) is None
    assert spend.free(AGENTS["local"]) and not spend.free(AGENTS["muse"]) and not spend.free({"price_in_per_mtok": 0})
    p = Project("/nonexistent-dtree-test", {})
    assert spend.after_job(p, cfg, {"cost_usd": 0.4, "tokens": 10}) == {"cost_usd": 0.4, "tokens": 10}          # reported: kept
    assert spend.after_job(p, cfg, {"tokens_in": 1000, "tokens_out": 100}) == {"tokens_in": 1000, "tokens_out": 100, "cost_usd": 0.0045,
                                                                                 "cost_estimated": True}
    assert spend.after_job(p, {}, {"tokens": 10}) == {"tokens": 10}                                              # no price: tokens only


def test_the_month_s_budget_refuses_a_job_and_says_so_on_the_question(repo):
    p = _project(repo, budget={"month_usd": 1.0})
    did, t = _trial(p)
    _job(p, t["id"], "claude", cost=0.7)
    _job(p, t["id"], "muse", cost=0.5, result={"tokens": 10, "cost_usd": 0.5, "cost_estimated": True})
    _job(p, t["id"], "claude", cost=5.0, created=_last_month())                  # last month does not count
    assert spend.spent(p) == pytest.approx(1.2)
    with pytest.raises(DecisionError) as e:
        spend.check(p, "muse", "user", did)
    assert "$1.20 of $1.00" in str(e.value) and "budget.month_usd" in str(e.value) and time.strftime("%Y-%m") in str(e.value)
    ev = [x for x in p.store.get(did)["events"] if x["action"] == "budget_refused"]
    assert len(ev) == 1 and ev[0]["by"] == "user"
    assert ev[0]["detail"] == {"agent": "muse", "setting": "budget.month_usd", "spent_usd": 1.2, "cap_usd": 1.0, "period": time.strftime("%Y-%m")}
    spend.check(p, "local", "user", did)                                          # a free agent still runs
    spend.check(_project(repo, budget={"month_usd": 2.0}), "muse", "user", did)  # under the cap
    spend.check(_project(repo), "muse", "user", did)                             # no cap


def test_send_and_race_refuse_when_the_budget_is_used(repo):
    p = _project(repo, budget={"month_usd": 0.5})
    did, t = _trial(p)
    _job(p, t["id"], "plain", cost=0.6)
    t2 = p.store.try_option(did, "right", variant="plain")
    with pytest.raises(DecisionError):
        jobs.send(p, t2["id"], "plain")
    assert [j["agent"] for j in p.store.jobs()] == ["plain"]                       # nothing started
    out = jobs.race(p, did, "left", ["muse"])
    assert out[0]["ok"] is False and "budget" in out[0]["error"]
    assert sum(x["action"] == "budget_refused" for x in p.store.get(did)["events"]) == 2


def test_an_agent_s_own_month_usd_stops_its_jobs_and_no_other_agent_s(repo):
    """The user, 2026-11-02: limit an agent to what it may spend, e.g. Muse to its $150 hackathon credit."""
    p = _project(repo, agents={"muse": {"month_usd": 1.0, "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0, "key_env": None,
                                        "base_url": "https://api.example.invalid/v1"}})
    did, t = _trial(p)
    month = time.strftime("%Y-%m")
    _job(p, t["id"], "muse", cost=0.7, result={"cost_usd": 0.7, "cost_estimated": True, "tokens": 10})
    _job(p, t["id"], "muse", cost=9.0, created=_last_month())                    # last month does not count
    _job(p, t["id"], "claude", cost=5.0)                                          # another agent's spend is not muse's
    spend.check(p, "muse", "user", did)                                           # $0.70 of $1.00
    _job(p, t["id"], "muse", status="stopped", cost=0.4, result={"cost_usd": 0.4, "cost_estimated": True})
    with pytest.raises(DecisionError) as e:
        spend.check(p, "muse", "user", did)
    assert "$1.10 of $1.00" in str(e.value) and "agents.muse.month_usd" in str(e.value) and month in str(e.value), str(e.value)
    spend.check(p, "claude", "user", did)                                         # no cap of its own, no budget.month_usd
    ev = [x for x in p.store.get(did)["events"] if x["action"] == "budget_refused"]
    assert [x["detail"] for x in ev] == [{"agent": "muse", "setting": "agents.muse.month_usd", "spent_usd": 1.1, "cap_usd": 1.0, "period": month}]
    t2 = p.store.try_option(did, "right", variant="muse")
    assert agents.make(p.agent("muse"), p).available()[0]
    with pytest.raises(DecisionError) as e:
        jobs.send(p, t2["id"], "muse")                                            # the runner's own door refuses it too
    assert "agents.muse.month_usd" in str(e.value)
    assert sum(j["agent"] == "muse" for j in p.store.jobs()) == 3


def test_month_tokens_caps_an_agent_that_reports_tokens_only_without_prices(repo):
    """Muse's API reports tokens only: its month's cap can be counted in tokens, with no price set."""
    p = _project(repo, agents={"muse": {"month_tokens": 1000}})
    assert spend.prices(p.agent("muse")) is None
    did, t = _trial(p)
    _job(p, t["id"], "muse", result={"tokens_in": 500, "tokens_out": 100, "tokens": 600})
    _job(p, t["id"], "muse", result={"tokens_in": 4000, "tokens_out": 100, "tokens": 4100}, created=_last_month())
    spend.check(p, "muse", "user", did)
    _job(p, t["id"], "muse", status="failed", result={"tokens_in": 400, "tokens_out": 50})        # no total: in + out
    with pytest.raises(DecisionError) as e:
        spend.check(p, "muse", "user", did)
    assert "1,050 of 1,000 tokens" in str(e.value) and "agents.muse.month_tokens" in str(e.value), str(e.value)
    ev = [x for x in p.store.get(did)["events"] if x["action"] == "budget_refused"]
    assert ev[-1]["detail"] == {"agent": "muse", "setting": "agents.muse.month_tokens", "spent_tokens": 1050, "cap_tokens": 1000,
                                "period": time.strftime("%Y-%m")}
    spend.check(_project(repo, agents={"muse": {"month_tokens": 2000}}), "muse", "user", did)


def test_a_dollar_cap_on_an_agent_that_reports_tokens_only_needs_its_prices(repo):
    """Without prices a chat or Codex job has no dollars, so a month_usd there would never stop anything: it is refused, saying why."""
    with pytest.raises(DecisionError) as e:
        spend.check(_project(repo, agents={"muse": {"month_usd": 150}}), "muse")
    assert "agents.muse.month_usd" in str(e.value) and "price_in_per_mtok" in str(e.value) and "agents.muse.month_tokens" in str(e.value)
    with pytest.raises(DecisionError):
        spend.check(_project(repo, agents={"codex": {"month_usd": 5}}), "codex")
    spend.check(_project(repo, agents={"muse": {"month_usd": 150, "price_in_per_mtok": 1, "price_out_per_mtok": 2}}), "muse")
    spend.check(_project(repo, agents={"claude": {"month_usd": 5}}), "claude")             # Claude Code reports its dollars itself
    for bad, word in (({"month_usd": "lots"}, "month_usd"), ({"month_tokens": -1}, "month_tokens"), ({"month_tokens": "x"}, "month_tokens")):
        with pytest.raises(DecisionError) as e:
            spend.check(_project(repo, agents={"muse": dict(bad, price_in_per_mtok=1)}), "muse")
        assert "agents.muse." + word in str(e.value), str(e.value)
    with pytest.raises(DecisionError):
        spend.check(_project(repo, agents={"muse": {"month_tokens": 0}}), "muse")          # 0: this agent is not to run this month


def test_the_report_the_cli_and_the_agents_show_each_agent_s_month_caps(repo, capsys):
    (repo["path"] / ".dtree").mkdir()
    agents_cfg = {"muse": {"month_usd": 150, "month_tokens": 1000, "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0},
                  "grok": {"month_usd": 20, "price_in_per_mtok": 3, "price_out_per_mtok": 15}, "codex": {"month_tokens": 500}}
    (repo["path"] / ".dtree" / "config.json").write_text(json.dumps({"name": "t", "db": str(repo["tmp"] / "d.sqlite"), "agents": agents_cfg}))
    p = Project(repo["path"])
    did, t = _trial(p)
    _job(p, t["id"], "muse", cost=0.25, result={"cost_usd": 0.25, "cost_estimated": True, "tokens_in": 300, "tokens_out": 100, "tokens": 400})
    _job(p, t["id"], "claude", cost=1.0, result={"cost_usd": 1.0})
    r = spend.report(p)
    m, g, c = r["agents"]["muse"], r["agents"]["grok"], r["agents"]["claude"]
    assert (m["cap_usd"], m["remaining_usd"], m["cap_tokens"], m["remaining_tokens"]) == (150.0, 149.75, 1000, 600)
    assert g["jobs"] == 0 and (g["cap_usd"], g["remaining_usd"], g["cap_tokens"], g["remaining_tokens"]) == (20.0, 20.0, None, None)
    assert r["agents"]["codex"]["remaining_tokens"] == 500 and c["cap_usd"] is None and c["remaining_usd"] is None   # caps without jobs are shown
    assert r["totals"]["jobs"] == 2 and "cap_usd" not in r["totals"] and r["cap"] is None
    for name, want in (("muse", (150.0, 1000)), ("codex", (None, 500)), ("claude", (None, None)), ("local", (None, None))):
        d = agents.make(p.agent(name), p).describe()
        assert (d["month_usd"], d["month_tokens"]) == want, (name, d)
    assert cli.main(["--project", str(repo["path"]), "spend"]) == 0
    text = capsys.readouterr().out
    assert "muse: $0.25 of $150.00 (agents.muse.month_usd), $149.75 left · 400 of 1,000 tokens (agents.muse.month_tokens), 600 left" in text, text
    assert "grok: $0.00 of $20.00 (agents.grok.month_usd), $20.00 left" in text and "codex: 0 of 500 tokens" in text


def test_the_runner_keeps_the_estimate_mark_and_the_report_counts_it(repo):
    """Ported from the review of 2026-11-01 (test_rv_runner_keys): jobs.run kept a fixed list of the agent's keys and dropped
    cost_estimated, so the report showed an estimated cost as reported."""
    model = FakeModel([{"calls": [("finish", {"built": False, "tests": "-", "impact": "-", "note": "n"})]}])
    p = _project(repo, agents={"cheap": {"kind": "chat", "base_url": model.url, "model": "m", "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0}},
                 chat={"sandbox": "none"})
    did, t = _trial(p)
    j = jobs.run(p, p.store.job_open(t["id"], agent="cheap")["id"])
    assert j["cost_usd"] == pytest.approx((8 * 1.0 + 2 * 2.0) / 1e6) and j["result"]["cost_estimated"] is True
    row = spend.report(p)["agents"]["cheap"]
    assert row["usd_estimated"] == row["usd"] == pytest.approx(1.2e-05)


def test_settings_that_are_not_numbers_are_refused_before_a_job(repo):
    p = _project(repo, agents={"muse": {"max_budget_usd": "lots"}, "grok": {"price_in_per_mtok": -3}})
    for name, word in (("muse", "agents.muse.max_budget_usd"), ("grok", "agents.grok.price_in_per_mtok")):
        with pytest.raises(DecisionError) as e:
            spend.check(p, name)
        assert word in str(e.value)
    with pytest.raises(DecisionError):
        spend.check(_project(repo, budget={"job_tokens": 0}), "muse")


def test_the_report_counts_each_agent_s_jobs_dollars_tokens_and_time(repo):
    p = _project(repo, budget={"month_usd": 10.0})
    did, t = _trial(p)
    _job(p, t["id"], "claude", cost=0.8, result={"cost_usd": 0.8, "tokens_in": 1000, "tokens_out": 100, "tokens": 1100})
    _job(p, t["id"], "claude", status="failed", cost=0.2, result={"cost_usd": 0.2})
    _job(p, t["id"], "muse", cost=0.05, result={"cost_usd": 0.05, "cost_estimated": True, "tokens_in": 40000, "tokens_out": 5000, "tokens": 45000})
    _job(p, t["id"], "plain", result={"tokens": 900})
    _job(p, t["id"], "claude", cost=3.0, created=_last_month())
    _job(p, t["id"], "muse", status="running")
    r = spend.report(p)
    assert r["month"] == time.strftime("%Y-%m") and r["cap"] == 10.0 and r["remaining"] == pytest.approx(8.95)
    c, m, pl = r["agents"]["claude"], r["agents"]["muse"], r["agents"]["plain"]
    assert (c["jobs"], c["measured"], c["failed"], c["usd"], c["usd_estimated"], c["tokens_in"], c["tokens_out"]) == (2, 1, 1, 1.0, 0.0, 1000, 100)
    assert (m["jobs"], m["running"], m["usd"], m["usd_estimated"], m["tokens"]) == (2, 1, 0.05, 0.05, 45000)
    assert pl["no_price"] == 1 and pl["usd"] == 0.0 and pl["tokens"] == 900
    assert r["totals"]["jobs"] == 5 and r["totals"]["usd"] == pytest.approx(1.05) and r["totals"]["seconds"] >= 0
    old = spend.report(p, time.strftime("%Y-%m", time.localtime(_last_month())))
    assert old["agents"]["claude"]["usd"] == 3.0 and old["totals"]["jobs"] == 1
    with pytest.raises(DecisionError):
        spend.report(p, "2026-13")


def test_the_spend_route_and_the_cli(repo, capsys):
    (repo["path"] / ".dtree").mkdir()
    (repo["path"] / ".dtree" / "config.json").write_text(json.dumps({"name": "t", "db": str(repo["tmp"] / "d.sqlite"), "budget": {"month_usd": 5}}))
    p = Project(repo["path"])
    did, t = _trial(p)
    _job(p, t["id"], "claude", cost=1.25, result={"cost_usd": 1.25})
    client = ApiClient(p)
    r = client.get("/api/spend?month=" + time.strftime("%Y-%m"))
    assert r.status_code == 200 and r.get_json()["ok"] and r.get_json()["agents"]["claude"]["usd"] == 1.25 and r.get_json()["remaining"] == 3.75
    assert client.get("/api/spend").get_json()["totals"]["jobs"] == 1
    assert client.get("/api/spend?month=nope").status_code == 400
    assert cli.main(["--project", str(repo["path"]), "spend"]) == 0
    text = capsys.readouterr().out
    assert "spend %s · $1.25 of $5.00 (budget.month_usd) · remaining $3.75" % time.strftime("%Y-%m") in text and "claude" in text
    assert cli.main(["--project", str(repo["path"]), "spend", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["usd"] == 1.25
    assert cli.main(["--project", str(repo["path"]), "spend", "--month", "2026-00"]) == 1


# ------------------------------------------------------------------------------------------------------------- the agents' token caps
class FakeModel:
    """An OpenAI-compatible /chat/completions answering with a script of tool calls, each answer costing 8 + 2 tokens."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        model = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                model.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                step = model.script.pop(0) if model.script else {"content": "nothing more"}
                msg = {"role": "assistant", "content": step.get("content", "")}
                if step.get("calls"):
                    msg["tool_calls"] = [{"id": "c%d" % i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                                         for i, (n, a) in enumerate(step["calls"])]
                data = json.dumps({"choices": [{"message": msg}], "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]


def test_the_chat_loop_stops_at_its_token_cap_and_the_job_says_why(repo):
    model = FakeModel([{"calls": [("list_files", {})]}, {"calls": [("read_file", {"path": "f.txt"})]},
                       {"calls": [("write_file", {"path": "late.txt", "content": "after the cap"})]}, {"calls": [("finish", {"built": True})]}])
    p = _project(repo, agents={"cheap": {"kind": "chat", "base_url": model.url, "model": "m", "max_tokens_total": 25,
                                         "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0}}, chat={"sandbox": "none"})
    did, t = _trial(p)
    j = jobs.run(p, p.store.job_open(t["id"], agent="cheap")["id"])
    assert j["status"] == "failed" and j["reason"] == "budget: 30 tokens of cap 25", j["reason"]
    assert len(model.requests) == 3 and not (repo["tmp"] / "wt" / ("alt-%d-right" % did) / "late.txt").exists()   # its tools were not run
    assert (j["result"]["tokens"], j["result"]["tokens_in"], j["result"]["tokens_out"]) == (30, 24, 6)
    assert j["cost_usd"] == pytest.approx((24 * 1.0 + 6 * 2.0) / 1e6)               # estimated from the tokens
    assert "■ budget: 30 tokens of cap 25" in "\n".join(jobs.log_tail(j))


def test_a_finish_that_arrives_with_the_cap_is_kept(repo, tmp_path):
    model = FakeModel([{"calls": [("list_files", {})]}, {"calls": [("finish", {"built": False, "tests": "-", "impact": "-", "note": "n"})]}])
    p = _project(repo, agents={"cheap": {"kind": "chat", "base_url": model.url, "model": "m", "max_tokens_total": 15}}, chat={"sandbox": "none"})
    a = agents.make(p.agent("cheap"), p)
    ctx = agents.Context(repo["path"], repo["path"] / ".git", lambda l: None, lambda pid: None, lambda: False, time.time() + 60)
    out = a.run("the task", ctx)
    assert "error" not in out and out["note"] == "n" and out["tokens"] == 20 and out["tokens_in"] == 16


CODEX = r'''#!%s
import json, os, subprocess, sys, time
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
sys.stdin.read()
def ev(d):
    print(json.dumps(d), flush=True)
ev({"type": "thread.started", "thread_id": "t9"})
ev({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 20}})
subprocess.Popen(["sleep", "%s"])
time.sleep(60)
ev({"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 200}})
'''


def test_codex_is_stopped_when_a_turn_takes_it_past_its_token_cap(repo):
    marker = "302.%06d" % (os.getpid() % 1000000)                         # `sleep 302.<pid>` (dash has no exec -a: no sleeper was left)
    fake = repo["tmp"] / "codex"
    fake.write_text(CODEX % (sys.executable, marker))
    fake.chmod(0o755)
    p = _project(repo, agents={"codex": {"command": str(fake), "max_tokens_total": 100}})
    a = agents.make(p.agent("codex"), p)
    assert a.describe()["max_tokens_total"] == 100
    lines = []
    ctx = agents.Context(repo["path"], repo["path"] / ".git", lines.append, lambda pid: None, lambda: False, time.time() + 120)
    ctx.limits = spend.limits(p, a.cfg)
    t0 = time.time()
    out = a.run("the task", ctx)
    assert out["error"] == "budget: 120 tokens of cap 100" and time.time() - t0 < 30
    assert (out["tokens"], out["tokens_in"], out["tokens_out"], out["session"]) == (120, 100, 20, "t9")
    assert any("Codex is stopped" in l for l in lines)
    time.sleep(0.5)
    assert "sleep " + marker not in subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
