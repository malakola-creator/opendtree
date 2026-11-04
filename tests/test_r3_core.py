"""Round 3 of the review (2026-11-02), stream A — the store, the server, jobs, merge and the CLI: trial branches of their own (F1), a
refused follow-up keeps the measurement (F5/F8), cross-site writes and DNS rebinding refused (F9/E0), a refused question is not stored
(F10), the compare row counts the whole trial (F15), an agent its spend settings refuse is not offered (F18), any error is a JSON answer
(F20), dropping a trial stops its job (E2), `dtree compare` shows dtree's own checks (E3), an open question's prompt (E8), merge-check
of a dropped trial and its exit code (E9), and the docs (F26, F27)."""
import http.client
import io
import json
import re
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import ApiClient, client_for
from dtree import cli, gitcode, jobs, mcp, merge, server, spend, verify
from dtree.project import Project
from dtree.store import DecisionError, DecisionStore
from test_agents import CLAUDE, FakeModel, Q, _exe, _trial

ROOT = Path(__file__).resolve().parents[1]


def _project(repo, **extra):
    cfg = {"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"), "db": str(repo["tmp"] / "d.sqlite"),
           "agents": {"claude": {"command": _exe(repo["tmp"] / "claude", CLAUDE), "sandbox": "none"}}, "verify": {"enabled": False}}
    cfg.update(extra)
    return Project(repo["path"], cfg)


@pytest.fixture
def runner(monkeypatch):
    """Jobs are opened and their runner stands in (pid 999999, alive); nothing is launched."""
    launched = []
    real = jobs._alive
    monkeypatch.setattr(jobs, "launch", lambda proj, jid: launched.append(jid) or 999999)
    monkeypatch.setattr(jobs, "_alive", lambda pid, marker="": pid == 999999 or real(pid, marker))
    return launched


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd)] + [str(a) for a in args], capture_output=True, text=True, check=True).stdout


# ------------------------------------------------------------------------------------------------------------- F1
def test_options_whose_keys_make_the_same_branch_get_branches_of_their_own():
    """Review F1: `ซ้าย` and `ขวา` both became alt/<id>-option; `a/b` and `a-b`, `x!` and `x?` one branch each. Two trials then shared a
    branch and a worktree, and merging one option merged the other's commits too."""
    s = DecisionStore(":memory:")
    keys = ["ซ้าย", "ขวา", "a/b", "a-b", "x!", "x?", "a--claude", "a"]
    did = s.add(dict(Q, key="k", options=[{"key": k, "th": k} for k in keys]))["id"]
    tried = {k: s.try_option(did, k) for k in keys[:7]}
    raced = s.try_option(did, "a", variant="claude")                       # alt/<id>-a--claude: the same name as option "a--claude"
    branches = [t["branch"] for t in tried.values()] + [raced["branch"]]
    assert len(set(branches)) == len(branches), branches
    assert tried["ซ้าย"]["branch"] == "alt/%d-option" % did and tried["a/b"]["branch"] == "alt/%d-a-b" % did   # the first keeps the plain name
    assert all(gitcode.BRANCH.match(b) for b in branches), branches
    s.trial_end(tried["x?"]["id"], "dropped")
    assert s.try_option(did, "x?")["branch"] == tried["x?"]["branch"]        # the same option again: its own branch, with its earlier work
    s.trial_end(tried["x!"]["id"], "dropped")
    assert s.try_option(did, "x!")["branch"] == tried["x!"]["branch"]
    assert len({t["branch"] for t in s.trials("open")}) == len(s.trials("open"))


# ------------------------------------------------------------------------------------------------------------- F5 / F8
def test_a_follow_up_that_is_refused_keeps_the_trial_s_measurement(repo, runner):
    """Review F5/F8: followup sent the measured trial back to trying (results cleared) before the budget, a job slot or the agent refused
    the job; the Adopt button was gone until a job measured it again."""
    p = _project(repo, budget={"month_usd": 0.5}, max_parallel_jobs=1,
                 agents={"claude": {"command": _exe(repo["tmp"] / "claude", CLAUDE), "sandbox": "none"},
                         "ghost": {"kind": "claude", "command": str(repo["tmp"] / "none")}})
    did, t = _trial(p)
    j = p.store.job_open(t["id"], agent="claude")
    p.store.job_set(j["id"], cost_usd=0.6)
    p.store.job_end(j["id"], "measured", "built", {"cost_usd": 0.6})
    p.store.trial_result(t["id"], "[claude] 3 passed", "goes right")

    def unchanged():
        tr = p.store.trial(t["id"])
        assert (tr["status"], tr["result_tests"], tr["result_impact"]) == ("measured", "[claude] 3 passed", "goes right")
        assert not [e for e in p.store.get(did)["events"] if e["action"] == "trial_followup"]
        assert [x["id"] for x in p.store.jobs(tid=t["id"])] == [j["id"]]
    with pytest.raises(DecisionError) as err:
        jobs.followup(p, t["id"], "say more in the notes")                    # the month's budget is used
    assert "budget" in str(err.value)
    unchanged()
    with pytest.raises(DecisionError):
        jobs.followup(p, t["id"], "say more", agent="ghost")                  # the agent cannot run here
    unchanged()
    p.config["budget"]["month_usd"] = None
    other = p.store.try_option(did, "right", variant="other")
    p.store.job_open(other["id"], agent="claude")                             # the one job slot is taken
    with pytest.raises(DecisionError) as err:
        jobs.followup(p, t["id"], "say more")
    assert "max_parallel_jobs" in str(err.value)
    unchanged()
    p.config["max_parallel_jobs"] = 2
    sent = jobs.followup(p, t["id"], "say more")
    assert sent["status"] == "running" and sent["runner_pid"] == 999999 and p.store.trial(t["id"])["status"] == "trying"
    ev = [e for e in p.store.get(did)["events"] if e["action"] == "trial_followup"]
    assert len(ev) == 1 and ev[0]["detail"]["was"]["tests"] == "[claude] 3 passed"


# ------------------------------------------------------------------------------------------------------------- F9 / E0
def _serve(p):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(p))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _http(port, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    hdrs = {"Host": host or "127.0.0.1:%d" % port}
    hdrs.update(headers or {})
    data = body.encode("utf-8") if isinstance(body, str) else (body or b"")
    for k, v in hdrs.items():
        if v is not None:
            conn.putheader(k, v)
    if method == "POST":
        conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data if method == "POST" else None)
    r = conn.getresponse()
    out = r.status, r.read()
    conn.close()
    return out


def test_writes_from_another_site_and_dns_rebinding_are_refused(repo):
    """Review F9 / E0: any web page the person opened could POST a text/plain body to 127.0.0.1:5077 (no CORS preflight) — merge a trial,
    start paid jobs — and a rebound DNS name could read the store. The page itself (JSON, its own origin) and the LAN use keep working."""
    p = _project(repo, server={"allowed_hosts": ["mybox"]})
    did = p.store.add(Q)["id"]
    httpd, port = _serve(p)
    me = "http://127.0.0.1:%d" % port
    note = lambda text: json.dumps({"note": text})
    try:
        def notes():
            return [e["detail"]["note"] for e in p.store.get(did)["events"] if e["action"] == "commented"]
        path = "/api/decisions/%d/comment" % did
        refused = [
            ({"Content-Type": "text/plain;charset=UTF-8", "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}, None),
            ({"Content-Type": "text/plain;charset=UTF-8"}, None),                                         # a simple request, no Origin
            ({"Content-Type": "application/x-www-form-urlencoded"}, None),
            ({}, None),                                                                                    # no Content-Type at all
            ({"Content-Type": "application/json", "Origin": "https://evil.example"}, None),
            ({"Content-Type": "application/json", "Origin": "null"}, None),
            ({"Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % (port + 1)}, None),  # another port: another origin
            ({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, None),
            ({"Content-Type": "application/json", "Origin": "http://rebind.evil.example:%d" % port}, "rebind.evil.example:%d" % port),
        ]
        for headers, host in refused:
            status, body = _http(port, "POST", path, note("from another site"), headers, host)
            assert status in (403, 415, 421), (headers, host, status, body)
            assert json.loads(body)["ok"] is False
        assert notes() == []
        status, body = _http(port, "GET", "/api/decisions/%d" % did, host="rebind.evil.example:%d" % port)
        assert status in (403, 421) and b"Which way" not in body                        # DNS rebinding reads nothing either
        for headers, host in [({"Content-Type": "application/json", "Origin": me}, None),                  # the page itself
                              ({"Content-Type": "application/json; charset=utf-8"}, None),                 # a script on this machine
                              ({"Content-Type": "application/json", "Origin": "http://localhost:%d" % port}, "localhost:%d" % port),
                              ({"Content-Type": "application/json", "Origin": "http://192.0.2.24:5077"}, "192.0.2.24:5077"),   # the LAN
                              ({"Content-Type": "application/json", "Origin": "http://mybox:5077"}, "mybox:5077")]:          # allowed_hosts
            status, body = _http(port, "POST", path, note("ok %s" % (host or "")), headers, host)
            assert status == 200, (headers, host, status, body)
        assert len(notes()) == 5
        for host in ("127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port, "192.0.2.24:5077", "MyBox:5077"):
            status, body = _http(port, "GET", "/api/project", host=host)
            assert status == 200, (host, body)
        status, page = _http(port, "GET", "/", host="127.0.0.1:%d" % port)
        assert status == 200 and b"<html" in page.lower()
    finally:
        httpd.shutdown()


# ------------------------------------------------------------------------------------------------------------- F10
def test_a_question_refused_for_how_it_follows_is_not_created(tmp_path):
    """Review F10: add() wrote the question and its raised event, then refused the follows link; nothing rolled back, so the answer was
    an error and the question was there anyway (and the next write committed it)."""
    s = DecisionStore(tmp_path / "d.sqlite")
    c = client_for(s, tmp_path)
    q = {k: v for k, v in Q.items() if k != "key"}                              # a key would count an open question again instead
    assert c.post("/api/decisions", json=q).get_json()["ok"]
    for bad, code in ((dict(q, follows=1), 400), (dict(q, follows=99, follows_why="it does"), 400), (dict(q, follows="x", follows_why="w"), 400),
                      (dict(q, follows=1, follows_why="w", follows_how="sideways"), 400)):
        r = c.post("/api/decisions", json=bad)
        assert r.status_code == code and not r.get_json()["ok"], (bad, r.get_json())
        assert s.ids() == [1]
    assert c.post("/api/decisions/1/comment", json={"note": "an unrelated write"}).get_json()["ok"]
    assert s.ids() == [1] and DecisionStore(tmp_path / "d.sqlite").ids() == [1]          # nothing was left to commit
    ok = c.post("/api/decisions", json=dict(q, follows=1, follows_why="it goes on")).get_json()
    assert ok["ok"] and ok["item"]["id"] == 2 and ok["item"]["follows"] == 1


def test_mcp_dtree_ask_refused_for_its_follows_leaves_no_question(tmp_path):
    p = Project(tmp_path, {"db": str(tmp_path / "d.sqlite")})
    q = {k: v for k, v in Q.items() if k != "key"}
    ask = lambda i, args: {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "dtree_ask", "arguments": args}}
    msgs = [ask(1, dict(q, title_th="แรก")), ask(2, dict(q, title_th="ต่อ", follows=1)), ask(3, dict(q, title_th="ต่อ", follows=1, follows_why="ต่อจาก #1")),
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "dtree_list", "arguments": {"status": "all"}}}]
    out = io.StringIO()
    mcp.serve_stdio(p, io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), out)
    got = [json.loads(l) for l in out.getvalue().splitlines()]
    assert got[1]["result"]["isError"] and "say why" in got[1]["result"]["content"][0]["text"]
    listed = json.loads(got[3]["result"]["content"][0]["text"])
    assert [(q["id"], q["title_th"], q["follows"]) for q in listed] == [(2, "ต่อ", 1), (1, "แรก", None)]


# ------------------------------------------------------------------------------------------------------------- F15
def test_the_compare_row_is_the_whole_trial_and_its_own_agent(repo):
    """Review F15: the row's cost, tokens and time were its last job's, and its agent the last job's (an update by another agent)."""
    p = _project(repo)
    did, t = _trial(p)
    m = p.store.try_option(did, "right", variant="muse")
    spent = []
    for tid, agent, cost, tokens, kind, est in ((t["id"], "claude", 1.20, 20000, "build", False), (t["id"], "claude", 0.30, 5000, "build", False),
                                                (t["id"], "local", None, 700, "update", False), (m["id"], "muse", 0.8, 9000, "build", True)):
        if kind == "update" and p.store.trial(tid)["status"] == "trying":
            p.store.trial_result(tid, "1 passed", "right")
        j = p.store.job_open(tid, agent=agent, kind=kind)
        if cost is not None:
            p.store.job_set(j["id"], cost_usd=cost)
        time.sleep(0.01)
        spent.append(p.store.job_end(j["id"], "measured", "done", {"tokens": tokens, "turns": 2, "cost_usd": cost, "cost_estimated": est}))
    rows = {r["trial"]: r for r in jobs.compare(p, did)["rows"]}
    mine, muse = rows[t["id"]], rows[m["id"]]
    assert mine["agent"] == "claude" and muse["agent"] == "muse"
    assert mine["job"]["cost_usd"] == pytest.approx(1.5) and mine["job"]["tokens"] == 25700 and mine["job"]["jobs"] == 3
    assert mine["job"]["seconds"] == pytest.approx(sum(round(j["ended_at"] - j["created"], 1) for j in spent[:3]), abs=0.15)
    assert mine["job"]["kind"] == "update" and mine["job"]["id"] == spent[2]["id"]          # the last job's own status, reason and kind
    assert mine["job"]["cost_estimated"] is False and muse["job"]["cost_estimated"] is True and muse["job"]["cost_usd"] == pytest.approx(0.8)


# ------------------------------------------------------------------------------------------------------------- F18
def test_an_agent_its_spend_settings_refuse_is_not_offered(repo, monkeypatch):
    """Review F18: agents.<name>.month_usd without prices (the README's credit example) refuses every job, yet the agent was listed as
    available with its cap; every send was then refused."""
    model = FakeModel([])
    p = _project(repo, agents={"credit": {"kind": "chat", "base_url": model.url, "model": "m", "month_usd": 150},
                               "fine": {"kind": "chat", "base_url": model.url, "model": "m", "month_tokens": 1000}}, chat={"sandbox": "none"})
    rows = {a["name"]: a for a in jobs.agent_list(p)}
    assert rows["fine"]["available"] and not rows["credit"]["available"]
    assert "month_usd" in rows["credit"]["reason"]
    monkeypatch.setattr(spend, "settings_problem", lambda project, cfg: "x refuses %s" % cfg["name"] if cfg["name"] == "fine" else None, raising=False)
    rows = {a["name"]: a for a in jobs.agent_list(p)}
    assert not rows["fine"]["available"] and rows["fine"]["reason"] == "x refuses fine"


# ------------------------------------------------------------------------------------------------------------- F20
def test_any_other_error_is_a_json_answer_not_a_dropped_connection(repo, monkeypatch):
    p = _project(repo)
    c = ApiClient(p)
    did = c.post("/api/decisions", json=Q).get_json()["item"]["id"]
    r = c.post("/api/decisions", json=dict(Q, refs=5))
    assert r.status_code == 400 and not r.get_json()["ok"]
    r = c.post("/api/decisions/%d/supersede" % did, json={"by": 99999999999999999999})
    assert r.status_code == 404 and not r.get_json()["ok"]                       # no such question: an answer, not an OverflowError
    assert c.get("/api/decisions/99999999999999999999").status_code == 404
    monkeypatch.setattr(p.store, "tree", lambda: (_ for _ in ()).throw(RuntimeError("the disk is gone")))
    r = c.get("/api/decisions/tree")
    assert r.status_code == 500 and r.get_json() == {"ok": False, "error": "RuntimeError: the disk is gone"}
    httpd, port = _serve(p)
    try:
        status, body = _http(port, "GET", "/api/decisions/tree")
        assert status == 500 and json.loads(body)["error"] == "RuntimeError: the disk is gone" and b"Traceback" not in body
        status, body = _http(port, "POST", "/api/decisions", b"{}", {"Content-Type": "application/json", "Content-Length": "x"})
        assert status == 400 and not json.loads(body)["ok"]
    finally:
        httpd.shutdown()


# ------------------------------------------------------------------------------------------------------------- E2
def test_dropping_a_trial_stops_its_running_job(repo, runner):
    """E2 of the real run: `dtree end 1 adopted --drop-others` left the other agents' jobs running and spending."""
    p = _project(repo)
    did = p.store.add(Q)["id"]
    fast = p.store.try_option(did, "right", variant="fast")
    slow = p.store.try_option(did, "right", variant="slow")
    alone = p.store.try_option(did, "left")
    jf, js, ja = (jobs.send(p, x["id"], "claude") for x in (fast, slow, alone))
    p.store.job_end(jf["id"], "measured", "built")
    p.store.trial_result(fast["id"], "1 passed", "right")
    p.store.trial_end(alone["id"], "dropped")
    assert p.store.job(ja["id"])["stop_requested"]
    p.store.trial_end(fast["id"], "adopted", drop_others=True)
    assert p.store.job(js["id"])["stop_requested"] and p.store.trial(slow["id"])["status"] == "dropped"
    ev = [e["detail"] for e in p.store.get(did)["events"] if e["action"] == "trial_dropped"]
    assert [d.get("stopped") for d in ev] == [[ja["id"]], [js["id"]]]


@pytest.mark.parametrize("when", ["during the job", "during verification"])
def test_a_job_whose_trial_was_dropped_meanwhile_ends_stopped(repo, monkeypatch, when):
    model = FakeModel([{"calls": [("run", {"command": "echo right > g.txt && git add g.txt && git commit -q -m 'build right' && echo ok"})]},
                       {"calls": [("finish", {"built": True, "tests": "1 passed", "impact": "right", "note": ""})]}])
    p = _project(repo, agents={"local": {"kind": "chat", "base_url": model.url, "model": "tiny"}}, chat={"sandbox": "none", "max_steps": 5},
                 verify={"enabled": True, "tests": "true", "auto": True})
    did, t = _trial(p)
    jid = p.store.job_open(t["id"], agent="local")["id"]
    seen = []
    orig = jobs.agents.make(p.agent("local"), p).__class__.run

    def run_then_drop(self, prompt, ctx):
        out = orig(self, prompt, ctx)
        if when == "during the job":
            p.store.trial_end(t["id"], "dropped", "another agent was quicker")
        return out

    def verify_then_drop(project, job, trial, say):
        seen.append(trial["id"])
        p.store.trial_end(t["id"], "dropped", "another agent was quicker")
        return {"status": "passed"}
    monkeypatch.setattr(jobs.agents.make(p.agent("local"), p).__class__, "run", run_then_drop)
    monkeypatch.setattr(verify, "after_job", verify_then_drop)
    out = jobs.run(p, jid)
    assert out["status"] == "stopped" and "dropped" in out["reason"], out["reason"]
    assert p.store.trial(t["id"])["status"] == "dropped"
    assert seen == ([] if when == "during the job" else [t["id"]])
    assert "build right" in _git(repo["path"], "log", "-1", "--format=%s", t["branch"])      # the work it did is kept on the branch


# ------------------------------------------------------------------------------------------------------------- E3
def test_dtree_compare_prints_dtree_s_own_checks_and_the_merge_state(repo, capsys):
    """E3 of the real run: the text rows showed only the agent's claim; dtree's counts, change, measures and merge state were only in
    --json."""
    cfg_dir = repo["path"] / ".dtree"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"),
                                                     "db": str(repo["tmp"] / "d.sqlite")}))
    (repo["path"] / ".dtree" / ".gitignore").write_text("*\n")
    p = Project(repo["path"])
    did, t = _trial(p)
    _git(repo["path"], "branch", t["branch"], "main")
    other = repo["tmp"] / "other"
    _git(repo["path"], "worktree", "add", "-q", other, t["branch"])
    (other / "g.txt").write_text("right\n")
    _git(other, "add", "g.txt")
    _git(other, "commit", "-q", "-m", "build right")
    _git(repo["path"], "worktree", "remove", "--force", other)
    tip = _git(repo["path"], "rev-parse", t["branch"]).strip()
    base = _git(repo["path"], "rev-parse", "main").strip()
    p.store.trial_result(t["id"], "[claude] pytest (tests/test_calc.py): 4 passed, 0 failed", "multiplies")
    side = lambda n: {"status": "passed", "passed": n, "failed": 0, "errors": 0, "skipped": 0, "total": n, "seconds": 1.0, "check": 1}
    p.store.note_event(did, "verified", "user", {"trial": t["id"], "sha": tip, "base_sha": base, "status": "passed", "tests": "4 passed, 0 failed",
                                                "base_tests": "2 passed, 0 failed", "measures": {"score": [2.0, 1.0]},
                                                "result": {"tests": side(4), "base": side(2), "delta": {"passed": 2, "failed": 0, "errors": 0},
                                                           "measures": [{"name": "score", "unit": "", "better": "higher", "value": 2.0, "base": 1.0,
                                                                         "delta": 1.0, "delta_pct": 100.0, "verdict": "better", "status": "passed"}]},
                                                "settings": verify._fingerprint(p, verify.settings(p))})
    assert cli.main(["--project", str(repo["path"]), "compare", str(did)]) == 0
    out = capsys.readouterr().out
    assert "[claude] pytest (tests/test_calc.py): 4 passed, 0 failed" in out                    # the agent's claim, whole
    assert re.search(r"dtree verify %s: passed · 4 passed, 0 failed · base %s: 2 passed, 0 failed · Δ passed \+2, failed \+0" % (tip[:7], base[:7]), out), out
    assert re.search(r"score 2 \(base 1, \+1, \+100(\.0)?%, better\)", out), out
    assert re.search(r"merge: no \(not_adopted\)", out), out


# ------------------------------------------------------------------------------------------------------------- E8
def test_the_prompt_of_an_open_question_speaks_of_no_answer(repo):
    """E8 of the real run: Claude reported its impact "Against the 'none' answer" — the prompt of a question nobody had answered asked
    for the impact against the answer taken now and for the tests the answer was measured with."""
    p = _project(repo)
    did = p.store.add(Q)["id"]                                                   # open
    t = p.store.try_option(did, "right")
    for kind in ("build", "update"):
        j = p.store.job_open(t["id"], agent="claude", kind=kind)
        prompt = jobs.prompt_for(p, p.store.job(j["id"]), repo["tmp"] / "wt" / "x")
        p.store.job_end(j["id"], "failed", "only a prompt")
        assert "the answer" not in prompt.lower(), [l for l in prompt.splitlines() if "the answer" in l.lower()]
    did2 = p.store.add(dict(Q, key="decided"))["id"]                              # decided: the comparison with the answer stays
    p.store.answer(did2, "left")
    t2 = p.store.try_option(did2, "right")
    j2 = p.store.job_open(t2["id"], agent="claude")
    prompt = jobs.prompt_for(p, p.store.job(j2["id"]), repo["tmp"] / "wt" / "y")
    assert "against the answer taken now" in prompt and "the answer was measured with" in prompt


# ------------------------------------------------------------------------------------------------------------- E9
def test_merge_check_of_a_dropped_trial_and_its_exit_code(repo, capsys):
    """E9 of the real run: a dropped trial was told to "adopt it first: dtree end 1 adopted", which is refused; and merge-check exited 0
    when it could not merge."""
    cfg_dir = repo["path"] / ".dtree"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"),
                                                     "db": str(repo["tmp"] / "d.sqlite"), "verify": {"enabled": False}}))
    (cfg_dir / ".gitignore").write_text("*\n")
    p = Project(repo["path"])
    did, t = _trial(p)
    _git(repo["path"], "branch", t["branch"], "main")
    other = repo["tmp"] / "other"
    _git(repo["path"], "worktree", "add", "-q", other, t["branch"])
    (other / "g.txt").write_text("right\n")
    _git(other, "add", "g.txt")
    _git(other, "commit", "-q", "-m", "build right")
    _git(repo["path"], "worktree", "remove", "--force", other)
    p.store.trial_end(t["id"], "dropped")
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert not r["can_merge"] and "adopt it first" not in r["reason"] and "dtree end" not in r["reason"] and "dropped" in r["reason"]
    with pytest.raises(SystemExit) as code:
        cli.main(["--project", str(repo["path"]), "merge-check", str(t["id"])])
    assert code.value.code == 1 and "can merge: no" in capsys.readouterr().out
    t2 = p.store.try_option(did, "right")                                        # the option again, on its branch (the earlier work)
    assert t2["branch"] == t["branch"]
    p.store.trial_result(t2["id"], "1 passed", "right")
    measured = merge.readiness(p, p.store.trial(t2["id"]))
    assert "dtree end %d adopted" % t2["id"] in measured["reason"]
    p.store.trial_end(t2["id"], "adopted")
    assert cli.main(["--project", str(repo["path"]), "merge-check", str(t2["id"])]) == 0
    assert "can merge: yes" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------------------- F26 / F27
def test_the_docs_name_five_tables_and_no_stale_limit():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    schema = (ROOT / "docs" / "schema.md").read_text(encoding="utf-8")
    merge_doc = (ROOT / "docs" / "merge.md").read_text(encoding="utf-8")
    assert "four tables" not in readme and "five tables (`decisions`, `events`, `trials`, `jobs`, `checks`)" in readme
    assert "four tables" not in schema and "five tables" in schema                    # the docs are English first (2026-11-04)
    assert "without the in-the-way check" not in merge_doc
