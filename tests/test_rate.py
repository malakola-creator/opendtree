"""Rate limits (the user's settings page, 2026-11-02: "keys, rate limits and the other settings"): an agent's ``requests_per_minute`` and
``requests_per_day`` hold its chat loop's requests to the model's API in any 60 s and in any 24 hours, every job of it together.

- the store's slot rule (``request_slot``): the windows are rolling, a slot is taken in one transaction, the wait is until the oldest
  request of a full window leaves it, and two processes taking slots at once never take more than the limit between them;
- the chat loop takes a slot before every request (a busy API's retries too): a full minute waits (a stop ends the wait), a day used up
  ends the job with nothing in flight, a day whose next slot comes soon is waited for;
- refusals before a job: open_job, followup and race refuse a day used up with code "rate" (the API's answer and a race entry carry
  it), the agents list shows the agent unavailable and why; 0, a wrong value and a limit on Claude Code or Codex are setting problems;
- describe(), the report and ``dtree spend`` show the limits and the requests now; the schema and the docs name them.

Stand-in model APIs only: nothing here spends quota."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import test_chat
import test_schema
from dtree import agents, cli, jobs, project, server, spend, store as store_mod
from dtree.project import Project
from dtree.store import DecisionError, DecisionStore

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "src" / "dtree" / "schema" / "config.schema.json").read_text(encoding="utf-8"))
DOC = (ROOT / "docs" / "sandbox-and-budget.md").read_text(encoding="utf-8")
FIN = test_chat.FIN


def _seed(s, agent, *ages):
    """Requests of ``agent`` that went out ``ages`` seconds ago (as earlier jobs of it would have left them); returns now."""
    now = time.time()
    with s._lock:
        s._db.executemany("INSERT INTO requests (agent, at) VALUES (?, ?)", [(agent, now - a) for a in ages])
        s._db.commit()
    return now


def _only(**agents_cfg):
    """These agents and none of dtree's defaults (whose available() would look for Claude Code, Codex or a local Ollama here)."""
    return dict({k: None for k in project.AGENTS}, **agents_cfg)


def _rows(s, agent):
    with s._lock:
        return [dict(r) for r in s._db.execute("SELECT * FROM requests WHERE agent = ? ORDER BY id", (agent,))]


class Model:
    """A stand-in OpenAI-compatible API answering ``script`` in order — (status, body) per request — and keeping when each request came
    (``times``); GET /models answers, as a local server's would."""

    def __init__(self, script):
        self.script, self.requests, self.times = list(script), [], []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data": []}')

            def do_POST(self):
                me.times.append(time.time())
                me.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                status, body = me.script.pop(0) if me.script else (200, _answer(("finish", FIN)))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _answer(*calls, usage=(50, 5)):
    msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                                                             for i, (n, a) in enumerate(calls)]}
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}}


LIST = (200, _answer(("list_files", {})))
DONE = (200, _answer(("finish", FIN)))


@pytest.fixture
def model():
    made = []

    def make(script):
        made.append(Model(script))
        return made[-1]
    yield make
    for m in made:
        m.close()


def _paced(repo, url, **cfg):
    """A project whose chat agent ``paced`` asks ``url``; ``cfg``: its rate limits and other settings."""
    return test_chat._project(repo, _only(paced=dict({"kind": "chat", "base_url": url, "model": "m"}, **cfg)))


def _run(p, lines=None, stopped=lambda: False):
    """The chat loop of agent ``paced`` on its own, in the project's checkout (no job: nothing is built)."""
    a = agents.make(p.agent("paced"), p)
    return a.run("the task", test_chat._ctx({"path": p.root}, lines, stopped=stopped))


# ================================================================================================================ the slot rule
def test_a_minute_holds_its_limit_and_says_when_the_oldest_request_leaves(tmp_path):
    s = DecisionStore(tmp_path / "d.sqlite")
    _seed(s, "g", 50, 10)
    got = s.request_slot("g", 2, None)
    assert (got["ok"], got["why"], got["minute"], got["day"]) == (False, "minute", 2, 2), got
    assert 9.5 <= got["wait_s"] <= 10.5, got                                  # the request of 50 s ago leaves in 10 s
    assert len(_rows(s, "g")) == 2                                            # refused: nothing taken
    got = s.request_slot("g", 3, None, job=7)
    assert (got["ok"], got["why"], got["wait_s"], got["minute"], got["day"]) == (True, None, 0.0, 3, 3), got
    assert _rows(s, "g")[-1]["job_id"] == 7 and abs(_rows(s, "g")[-1]["at"] - time.time()) < 5
    assert s.request_slot("g", 9, None, take=False)["ok"] and len(_rows(s, "g")) == 3          # only an answer: nothing taken
    assert s.request_counts("g") == {"minute": 3, "day": 3}


def test_a_day_holds_its_limit_over_a_rolling_24_hours(tmp_path):
    s = DecisionStore(tmp_path / "d.sqlite")
    _seed(s, "g", 80000, 3600, 100, 90000)                                    # the last one is older than a day: it does not count
    got = s.request_slot("g", None, 3)
    assert (got["ok"], got["why"], got["minute"], got["day"]) == (False, "day", 0, 3), got
    assert 6399 <= got["wait_s"] <= 6401, got                                 # 86400 - 80000: when the oldest of the day leaves
    assert s.request_slot("g", None, 4)["ok"] and s.request_counts("g") == {"minute": 1, "day": 4}


def test_both_full_is_the_day_and_waits_until_both_have_room(tmp_path):
    s = DecisionStore(tmp_path / "d.sqlite")
    _seed(s, "g", 86390, 30, 20)                                              # the day's oldest leaves in 10 s, the minute's in 30 s
    got = s.request_slot("g", 2, 3)
    assert (got["ok"], got["why"]) == (False, "day") and 29.5 <= got["wait_s"] <= 30.5, got
    got = s.request_slot("g", 2, 4)                                           # the day has room: only the minute is full
    assert (got["ok"], got["why"]) == (False, "minute") and 29.5 <= got["wait_s"] <= 30.5, got
    got = s.request_slot("g", 5, 3)                                           # only the day
    assert (got["ok"], got["why"]) == (False, "day") and 9.5 <= got["wait_s"] <= 10.5, got


def test_a_limit_lowered_below_the_count_waits_until_enough_have_left(tmp_path):
    s = DecisionStore(tmp_path / "d.sqlite")
    _seed(s, "g", 50, 40, 30, 20, 10)
    got = s.request_slot("g", 3, None)                                        # 5 in the minute, 3 allowed: 3 must leave first
    assert got["why"] == "minute" and got["minute"] == 5 and 29.5 <= got["wait_s"] <= 30.5, got


def test_only_the_agent_s_own_requests_count(tmp_path):
    s = DecisionStore(tmp_path / "d.sqlite")
    _seed(s, "other", 5, 4, 3)
    assert s.request_slot("g", 1, 1)["ok"] and not s.request_slot("g", 1, 1)["ok"]
    assert s.request_counts("other") == {"minute": 3, "day": 3} and s.request_counts("nobody") == {"minute": 0, "day": 0}


def test_requests_older_than_two_days_are_pruned_now_and_then(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "REQUEST_PRUNE_EVERY", 5)
    s = DecisionStore(tmp_path / "d.sqlite")
    _seed(s, "g", 3 * 86400, 2 * 86400 + 60)
    _seed(s, "h", 3 * 86400, 86400 + 60)                                      # a day and a minute: of no window, but kept (2 days)
    for _ in range(3):
        assert s.request_slot("g", None, None)["ok"]
    ages = sorted(round((time.time() - r["at"]) / 3600) for r in _rows(s, "g") + _rows(s, "h"))
    assert ages == [0, 0, 0, 24], ages                                        # the 5th row pruned what is older than two days


@pytest.mark.parametrize("bad", [0, -1, True, 2.5, "5"])
def test_a_limit_that_is_not_a_whole_number_above_0_is_refused(tmp_path, bad):
    s = DecisionStore(tmp_path / "d.sqlite")
    with pytest.raises(DecisionError):
        s.request_slot("g", bad, None)
    with pytest.raises(DecisionError):
        s.request_slot("g", None, bad)
    assert _rows(s, "g") == []


CHILD = r'''
import json, sys, time
from dtree.store import DecisionStore
path, start, tries = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
s = DecisionStore(path)
while time.time() < start:
    time.sleep(0.002)
got = {"minute": 0, "day": 0}
for _ in range(tries):
    got["minute"] += s.request_slot("m", 7, None)["ok"]
    got["day"] += s.request_slot("d", None, 5)["ok"]
print(json.dumps(got))
'''


def test_processes_taking_slots_at_once_never_take_more_than_the_limit(tmp_path):
    """Several runners of one agent share the store: six processes start at the same moment and each asks for 20 slots of agent m
    (7 a minute) and of agent d (5 a day). Between them they get exactly 7 and 5. (Without the one transaction, 8 to 10 of the 7 got
    through in 5 runs of 6, 2026-11-02.)"""
    db = tmp_path / "d.sqlite"
    DecisionStore(db).close()                                                 # made once, so the six only open it
    start = time.time() + 2.0
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(db), repr(start), "20"], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(6)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-2000:] for o in outs]
    got = [json.loads(o[0]) for o in outs]
    assert sum(g["minute"] for g in got) == 7 and sum(g["day"] for g in got) == 5, got
    s = DecisionStore(db)
    assert len(_rows(s, "m")) == 7 and len(_rows(s, "d")) == 5
    assert s.request_counts("m") == {"minute": 7, "day": 7}


# ================================================================================================================ the chat loop
def test_the_chat_loop_spaces_its_requests_to_the_minute_s_limit(repo, model):
    """Earlier jobs left two requests in the minute; with 3 a minute the loop's second and third requests each wait for one of them to
    leave, and say so in the job's log."""
    api = model([LIST, LIST, DONE])
    p = _paced(repo, api.url, requests_per_minute=3)
    lines = []
    seeded = _seed(p.store, "paced", 58.5, 57.5)                              # they leave 1.5 s and 2.5 s from now
    out = _run(p, lines)
    assert out.get("error") is None and out["turns"] == 3 and out["tokens"] == 165, out
    at = [t - seeded for t in api.times]                                      # never more than 3 in any 60 s, the earlier two included:
    assert at[0] < 1.0 and at[1] >= 1.5 and at[2] >= 2.5 and at[2] < 10, at   # the 2nd waits for the first to leave, the 3rd for the next
    waits = [l for l in lines if l.startswith("⏳ ")]
    assert len(waits) == 2 and all(re.match(r"^⏳ requests_per_minute 3: the next request in (\d|0\.\d) s$", w) for w in waits), lines
    assert p.store.request_counts("paced")["day"] == 5


def test_a_stop_while_the_minute_is_full_ends_the_job_at_once_with_nothing_asked(repo, model):
    api = model([DONE])
    p = _paced(repo, api.url, requests_per_minute=1)
    _seed(p.store, "paced", 10)                                               # the next request may go in 50 s
    stop, lines = [], []
    threading.Timer(1.0, lambda: stop.append(1)).start()
    t0 = time.time()
    out = _run(p, lines, stopped=lambda: bool(stop))
    assert out["error"] == "stopped" and time.time() - t0 < 6, (out, time.time() - t0)
    assert api.requests == [] and out["tokens"] == 0 and not out.get("usage_partial"), out
    assert any(re.match(r"^⏳ requests_per_minute 1: the next request in (49|50|51) s$", l) for l in lines), lines
    assert len(_rows(p.store, "paced")) == 1                                  # waiting took no slot


def test_a_day_used_up_ends_the_job_with_what_it_spent_and_nothing_in_flight(repo, model):
    api = model([LIST, LIST, DONE])
    p = _paced(repo, api.url, requests_per_day=3)
    _seed(p.store, "paced", 3600)
    lines = []
    out = _run(p, lines)
    assert re.match(r"^agents\.paced\.requests_per_day \(3\) is used: the next request in 23 h( \d+ min)?$", out["error"]), out
    assert len(api.requests) == 2 and out["tokens"] == 110 and out["turns"] == 2 and not out.get("usage_partial"), out
    assert "⚠ " + out["error"] in lines, lines


def test_a_job_that_finds_the_day_used_up_at_its_first_request_ends_having_sent_nothing(repo, model):
    """Two jobs sent at once with one request of the day left both start (the check before a job saw one left): the second to ask
    finds the day used up and ends at once."""
    api = model([DONE])
    p = _paced(repo, api.url, requests_per_day=2)
    _seed(p.store, "paced", 600, 5)                                           # the other job took the last one 5 s ago
    out = _run(p)
    assert api.requests == [] and out["tokens"] == 0 and out["turns"] == 0, out
    assert re.match(r"^agents\.paced\.requests_per_day \(2\) is used: the next request in 23 h 5\d min$", out["error"]), out


def test_a_day_whose_next_slot_comes_within_retry_max_wait_s_is_waited_for(repo, model):
    api = model([DONE])
    p = _paced(repo, api.url, requests_per_day=2)
    _seed(p.store, "paced", 86399, 3600)                                      # the oldest of the day leaves in a second
    lines = []
    out = _run(p, lines)
    assert out.get("error") is None and len(api.requests) == 1, out
    assert any(re.match(r"^⏳ requests_per_day 2: the next request in (0\.\d|1) s$", l) for l in lines), lines


def test_a_busy_api_s_retries_take_slots_too(repo, model):
    busy = (429, {"error": {"code": 429, "message": "quota", "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0.2s"}]}})
    api = model([busy, DONE])
    p = _paced(repo, api.url, requests_per_minute=10)
    out = _run(p)
    assert out.get("error") is None and len(api.requests) == 2, out
    assert p.store.request_counts("paced") == {"minute": 2, "day": 2}


def test_an_agent_without_limits_counts_its_requests_only(repo, model):
    api = model([LIST, DONE])
    p = _paced(repo, api.url)
    out = _run(p)
    assert out.get("error") is None and p.store.request_counts("paced") == {"minute": 2, "day": 2}, out


def test_through_the_runner_a_day_used_up_fails_the_job_and_each_request_names_its_job(repo, model):
    api = model([LIST, LIST, DONE])
    p = _paced(repo, api.url, requests_per_day=2)
    did, t = test_chat._trial(p)
    jid = p.store.job_open(t["id"], agent="paced")["id"]
    job = jobs.run(p, jid)
    assert job["status"] == "failed" and re.match(r"^agents\.paced\.requests_per_day \(2\) is used: the next request in (23 h 5\d min|24 h)$",
                                                   job["reason"]), job["reason"]
    assert [r["job_id"] for r in _rows(p.store, "paced")] == [jid, jid] and job["result"]["tokens"] == 110
    assert "⚠ agents.paced.requests_per_day (2) is used" in "\n".join(jobs.log_tail(job))


# ================================================================================================================ refusals before a job
def _gemini(repo, url, **cfg):
    return test_chat._project(repo, _only(gemini=dict({"kind": "chat", "base_url": url, "model": "gemini-3.5-flash", "key_env": None}, **cfg)))


def test_a_day_used_up_refuses_a_send_with_code_rate_and_says_so_on_the_question(repo, model):
    api = model([])
    p = _gemini(repo, api.url, requests_per_day=2)
    did, t = test_chat._trial(p)
    _seed(p.store, "gemini", 100, 50)
    with pytest.raises(spend.RateRefused) as e:
        jobs.open_job(p, t["id"], "gemini")
    exc = e.value
    assert (exc.code, exc.setting) == ("rate", "agents.gemini.requests_per_day") and isinstance(exc, DecisionError)
    assert re.match(r"^agents\.gemini\.requests_per_day \(2\) is used: the next request in 23 h 59 min; gemini was not started$", str(exc)), str(exc)
    assert exc.error_th.startswith("gemini ใช้คำขอครบ 2 ครั้งใน 24 ชั่วโมงแล้ว (agents.gemini.requests_per_day)") and "ชม." in exc.error_th
    assert 86000 < exc.wait_s < 86400 and p.store.jobs() == []
    ev = [x for x in p.store.get(did)["events"] if x["action"] == "rate_refused"]
    assert len(ev) == 1 and {k: ev[0]["detail"][k] for k in ("agent", "setting", "requests", "limit", "period")} == {
        "agent": "gemini", "setting": "agents.gemini.requests_per_day", "requests": 2, "limit": 2, "period": "day"}, ev
    assert ev[0]["detail"]["note"] in str(exc) and ev[0]["detail"]["wait_s"] > 86000
    code, answer = server.Api(p).handle("POST", "/api/decisions/trials/%d/ai" % t["id"], {}, {"agent": "gemini"})
    assert code == 400 and answer["code"] == "rate" and answer["setting"] == "agents.gemini.requests_per_day", answer


def test_a_race_entry_and_a_follow_up_are_refused_the_same_way(repo, model, monkeypatch):
    api = model([])
    p = _gemini(repo, api.url, requests_per_day=1)
    p.config["agents"]["other"] = {"kind": "chat", "base_url": api.url, "model": "m", "key_env": None}
    monkeypatch.setattr(jobs, "launch", lambda proj, jid: 999999)            # nothing is started for real
    did, t = test_chat._trial(p)
    _seed(p.store, "gemini", 30)
    entries = jobs.race(p, did, "right", ["gemini", "other"])
    g, o = entries
    assert not g["ok"] and (g["code"], g["setting"]) == ("rate", "agents.gemini.requests_per_day") and "ใช้คำขอครบ" in g["error_th"], g
    assert o["ok"], o                                                         # another agent's requests are its own
    p.store.trial_result(t["id"], "1 passed", "built")
    with pytest.raises(spend.RateRefused):
        jobs.followup(p, t["id"], "again", agent="gemini")
    assert p.store.trial(t["id"])["status"] == "measured"                    # refused before the trial was touched


def test_a_full_minute_never_refuses_a_job(repo, model):
    api = model([])
    p = _gemini(repo, api.url, requests_per_minute=2, requests_per_day=100)
    did, t = test_chat._trial(p)
    _seed(p.store, "gemini", 5, 3)
    assert jobs.open_job(p, t["id"], "gemini")["status"] == "running"        # its requests wait for the minute instead


def test_the_agents_list_shows_an_agent_whose_day_is_used_up_as_unavailable(repo, model):
    api = model([])
    p = _gemini(repo, api.url, requests_per_minute=5, requests_per_day=2)
    row = {r["name"]: r for r in jobs.agent_list(p)}["gemini"]
    assert row["available"] and "reason_th" not in row and (row["requests_minute"], row["requests_day"]) == (0, 0), row
    _seed(p.store, "gemini", 100, 50)
    row = {r["name"]: r for r in jobs.agent_list(p)}["gemini"]
    assert not row["available"] and re.match(r"^agents\.gemini\.requests_per_day \(2\) is used: the next request in 23 h 59 min$", row["reason"]), row
    assert row["reason_th"].startswith("gemini ใช้คำขอครบ 2 ครั้ง") and row["requests_day"] == 2 and row["requests_wait_s"] > 86000, row


def test_settings_problem_refuses_0_a_wrong_value_and_a_limit_on_claude_or_codex(repo):
    def p_with(**agents_cfg):
        return test_chat._project(repo, _only(**agents_cfg))
    remote = {"kind": "chat", "base_url": "https://api.example.invalid/v1", "model": "m", "key_env": None}
    for bad, words in ((0, "is 0: no request may go out, so no job of x starts"), ("lots", "must be a whole number above 0"),
                       (2.5, "must be a whole number above 0"), (True, "must be a whole number above 0"), (-1, "must be a whole number above 0")):
        for key in spend.RATE:
            p = p_with(x=dict(remote, **{key: bad}))
            problem = spend.settings_problem(p, p.agent("x"))
            assert problem and problem.startswith("agents.x.%s " % key) and words in problem, (key, bad, problem)
            with pytest.raises(DecisionError) as e:
                spend.check(p, "x")
            assert str(e.value) == problem and not isinstance(e.value, spend.RateRefused)
            row = {r["name"]: r for r in jobs.agent_list(p)}["x"]
            assert not row["available"] and row["reason"] == problem, row
    for good in (5, 5.0, None):
        p = p_with(x=dict(remote, requests_per_minute=good, requests_per_day=good))
        assert spend.settings_problem(p, p.agent("x")) is None, good
    for kind, who in (("claude", "Claude Code makes its own requests"), ("codex", "Codex makes its own requests")):
        p = p_with(**{kind: {"requests_per_day": 20}})
        problem = spend.settings_problem(p, p.agent(kind))
        assert problem.startswith("agents.%s.requests_per_day is a setting of dtree's chat loop" % kind) and who in problem, problem
        with pytest.raises(DecisionError) as e:
            spend.check(p, kind)
        assert str(e.value) == problem
        p = p_with(**{kind: {"requests_per_day": None}})                      # null is no limit: nothing to refuse
        assert spend.settings_problem(p, p.agent(kind)) is None


# ================================================================================================================ what is shown
def test_describe_and_the_report_carry_the_limits_and_the_requests_now(repo):
    p = test_chat._project(repo, {"gemini": {"requests_per_minute": 5, "requests_per_day": 20, "key_env": None},
                                  "plain": {"kind": "chat", "base_url": "https://api.example.invalid/v1", "model": "m"}})
    _seed(p.store, "gemini", 10, 20, 3000)
    _seed(p.store, "plain", 5)
    d = agents.make(p.agent("gemini"), p).describe()
    assert {k: d[k] for k in ("requests_per_minute", "requests_per_day", "requests_minute", "requests_day", "requests_wait_s")} == {
        "requests_per_minute": 5, "requests_per_day": 20, "requests_minute": 2, "requests_day": 3, "requests_wait_s": None}, d
    d = agents.make(p.agent("plain"), p).describe()
    assert (d["requests_per_minute"], d["requests_per_day"], d["requests_minute"], d["requests_day"]) == (None, None, 1, 1), d
    did, t = test_chat._trial(p)
    j = p.store.job_open(t["id"], agent="claude", max_running=9)
    p.store.job_end(j["id"], "measured", "built", {"cost_usd": 0.5})
    r = spend.report(p)
    g, c = r["agents"]["gemini"], r["agents"]["claude"]
    assert g["jobs"] == 0 and (g["requests_per_minute"], g["requests_per_day"], g["requests_minute"], g["requests_day"]) == (5, 20, 2, 3), g
    assert "plain" not in r["agents"]                                         # no limit and no job this month: not listed
    assert (c["requests_per_minute"], c["requests_minute"], c["requests_day"]) == (None, None, None), c   # Claude Code's own requests
    assert "requests_day" not in r["totals"]


def test_dtree_spend_prints_each_rate_limited_agent_s_requests(repo, capsys):
    d = repo["path"] / ".dtree"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"name": "t", "db": str(repo["tmp"] / "d.sqlite"), "agents": {
        "gemini": {"requests_per_minute": 5, "requests_per_day": 20},
        "gemini2": {"kind": "chat", "base_url": "https://api.example.invalid/v1", "model": "m", "requests_per_day": 3},
        "plain": {"kind": "chat", "base_url": "https://api.example.invalid/v1", "model": "m"}}}))
    p = Project(repo["path"])
    _seed(p.store, "gemini", *([10, 20] + [3000] * 10))
    _seed(p.store, "gemini2", 100, 200, 300)
    _seed(p.store, "plain", 1)
    assert cli.main(["--project", str(repo["path"]), "spend"]) == 0
    out = capsys.readouterr().out
    assert "each agent's requests (its rate limits; every job of it, now):" in out, out
    assert ("  gemini: 2 of 5 in the last minute (agents.gemini.requests_per_minute) · 12 of 20 in the last 24 h "
            "(agents.gemini.requests_per_day), 8 left") in out, out
    assert re.search(r"  gemini2: 3 of 3 in the last 24 h \(agents\.gemini2\.requests_per_day\), used up: the next request in 23 h 5\d min\n", out), out
    assert "plain:" not in out
    assert cli.main(["--project", str(repo["path"]), "spend", "--json"]) == 0
    g = json.loads(capsys.readouterr().out)["agents"]["gemini2"]
    assert (g["requests_per_day"], g["requests_day"]) == (3, 3) and g["requests_wait_s"] > 86000


def test_an_agents_list_makes_no_store_where_there_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("DTREE_DB", raising=False)
    p = Project(tmp_path, {"agents": _only(gemini={"requests_per_day": 20, "key_env": None}), "chat": {"sandbox": "none"}})
    rows = {r["name"]: r for r in jobs.agent_list(p)}
    assert (rows["gemini"]["requests_minute"], rows["gemini"]["requests_day"]) == (0, 0)
    assert not (tmp_path / ".dtree").exists()


# ================================================================================================================ the schema and the docs
def test_the_config_schema_names_both_limits_for_every_chat_agent():
    props = SCHEMA["$defs"]["agent"]["properties"]
    for key in spend.RATE:
        p = props[key]
        assert p["type"] == ["integer", "null"] and p["minimum"] == 1, p
        text = p["description"]
        assert text.startswith("chat:") and "shared by every job of this agent" in text and "Claude Code and Codex" in text, text
        assert "Gemini's free tier takes" in text and "5" in text and "20" in text and "per model" in text, text


def test_the_requests_table_is_in_the_schema_page_with_every_column():
    s = DecisionStore(":memory:")
    cols = [r["name"] for r in s._db.execute("PRAGMA table_info(requests)")]
    assert cols == ["id", "agent", "at", "job_id"]
    text = test_schema._section("requests")
    assert not [c for c in cols if "`%s`" % c not in text] and "requests_agent (agent, at)" in text, text
    assert "`requests`: created whole when missing" in test_schema.DOC and "`rate_refused`" in test_schema.DOC


def test_the_budget_doc_says_how_the_limits_hold():
    for words in ("requests_per_minute", "requests_per_day", "every job of the agent", "rolling", "5 requests a minute", "20 a day",
                  "code `rate`", "rate_refused", "⏳ requests_per_minute 5: the next request in 9 s",
                  "agents.gemini.requests_per_day (20) is used: the next request in 3 h 12 min"):
        assert words in DOC, words
