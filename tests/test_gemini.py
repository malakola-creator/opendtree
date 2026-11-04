"""Gemini as a dtree agent (the user gave a key on 2026-11-02): Google's OpenAI-compatible endpoint drives dtree's chat loop as it is,
and a busy API — 429, a rate or quota limit (the free tier takes 5 requests a minute: a real job was refused at its 7th), or 503,
overloaded — is asked again after the wait it names instead of ending the job. A stand-in API answers here; nothing spends quota."""
import json
import os
import threading
import time

import pytest

import test_chat
from dtree import agents, jobs, project
from test_r4 import _Answers, _calls, _chat_run

QUOTA = [{"error": {"code": 429, "message": "You exceeded your current quota", "status": "RESOURCE_EXHAUSTED",
                    "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": "PerMinute-FreeTier"}]},
                                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "%s"}]}}]


def _quota(delay):
    return json.loads(json.dumps(QUOTA).replace("%s", delay))


def test_gemini_is_one_of_dtree_s_agents_and_reads_its_key_from_a_file(repo, tmp_path):
    g = project.AGENTS["gemini"]
    assert (g["kind"], g["base_url"], g["key_env"]) == ("chat", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY")
    key = tmp_path / "gemini.key"
    key.write_text("not-a-real-key\n")
    os.chmod(key, 0o600)
    p = test_chat._project(repo, {"gemini": {"key_file": str(key)}}, chat={"sandbox": "none"})
    a = agents.make(p.agent("gemini"), p)
    assert a.available() == (True, "") or a.available()[0], a.available()
    assert a.cfg["model"] == g["model"] and a._key() == "not-a-real-key"


def test_a_quota_per_minute_waits_the_delay_gemini_names_and_goes_on(repo):
    api = _Answers([(200, _calls(("list_files", {}), usage=(54, 16))), (429, _quota("1s")), (200, _calls(("finish", test_chat.FIN), usage=(80, 10)))])
    lines = []
    try:
        a, ctx = _chat_run(repo, api.url, lines)
        t0 = time.time()
        out = a.run("the task", ctx)
        took = time.time() - t0
    finally:
        api.close()
    assert out.get("error") is None and out["built"] is False and out["turns"] == 2, out
    assert (out["tokens"], out["tokens_in"], out["tokens_out"]) == (160, 134, 26) and not out.get("usage_partial"), out   # the 429 ran nothing
    assert any("⏳ the API is busy (429): asked again in 1 s (1 of 8)" in l for l in lines), lines
    assert 1.0 <= took < 10, took
    assert len(api.requests) == 3 and api.requests[1] == api.requests[2]      # the same request, asked again


def test_an_overloaded_api_is_asked_again_after_its_retry_after(repo):
    api = _Answers([(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}}, {"Retry-After": "0.2"}),
                    (200, _calls(("finish", test_chat.FIN), usage=(54, 16)))])
    lines = []
    try:
        a, ctx = _chat_run(repo, api.url, lines)
        out = a.run("the task", ctx)
    finally:
        api.close()
    assert out.get("error") is None and out["tokens"] == 70 and not out.get("usage_partial"), out
    assert any("⏳ the API is busy (503): asked again in 0.2 s (1 of 8)" in l for l in lines), lines


def test_a_quota_for_the_day_is_not_waited_for(repo):
    api = _Answers([(429, _quota("40000s"))])
    try:
        a, ctx = _chat_run(repo, api.url)
        t0 = time.time()
        out = a.run("the task", ctx)
    finally:
        api.close()
    assert time.time() - t0 < 5 and out["error"].startswith("the model could not be asked: the API answered 429"), out
    assert "longer than retry_max_wait_s (300): a quota for the day?" in out["error"] and out["tokens"] == 0 and not out.get("usage_partial"), out


def test_still_busy_after_every_try_ends_the_job_saying_so(repo):
    api = _Answers([(429, _quota("0.1s")), (429, _quota("0.1s")), (429, _quota("0.1s"))])
    try:
        a, ctx = _chat_run(repo, api.url, retries=2)
        out = a.run("the task", ctx)
    finally:
        api.close()
    assert "still busy after 3 tries (chat.retries 2)" in out["error"] and len(api.requests) == 3, (out, len(api.requests))


def test_a_stop_ends_the_wait_at_once_and_counts_nothing_of_it(repo):
    api = _Answers([(200, _calls(("list_files", {}), usage=(54, 16))), (429, _quota("30s"))])      # within the job's 60 s
    stop = []
    try:
        a, ctx = _chat_run(repo, api.url)
        ctx.stopped = lambda: bool(stop)
        threading.Timer(1.5, lambda: stop.append(1)).start()
        t0 = time.time()
        out = a.run("the task", ctx)
        took = time.time() - t0
    finally:
        api.close()
    assert out["error"] == "stopped" and took < 5, (out, took)
    assert out["tokens"] == 70 and not out.get("usage_partial"), out


def test_a_job_of_a_rate_limited_agent_builds_through_the_runner(repo):
    """The real job's shape: tools, a 429 in the middle, then the work goes on and is committed."""
    commit = "printf 'def sub(a, b):\\n    return a - b\\n' > sub.py && git add sub.py && git commit -q -m 'gemini adds sub' && echo ok"
    api = _Answers([(200, _calls(("list_files", {}), usage=(54, 16))), (429, _quota("0.5s")),
                    (200, _calls(("run", {"command": commit}), usage=(90, 30))),
                    (200, _calls(("finish", {"built": True, "tests": "1 passed", "impact": "sub", "note": ""}), usage=(120, 20)))])
    try:
        p = test_chat._project(repo, {"gemini": {"kind": "chat", "base_url": api.url, "model": "gemini-3.5-flash"}})
        did, t = test_chat._trial(p)
        job = jobs.run(p, p.store.job_open(t["id"], agent="gemini")["id"])
    finally:
        api.close()
    assert job["status"] == "measured", job["reason"]
    assert job["result"]["tokens"] == 330 and "⏳ the API is busy (429)" in "\n".join(jobs.log_tail(job))
    assert "gemini adds sub" in repo["git"]("log", "-1", "--format=%s", t["branch"])
