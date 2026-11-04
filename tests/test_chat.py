"""The chat loop (agents/chat.py) after the final review of 2026-11-02: a job keeps every token it spent whatever ends it (F6), a stop or
the job's time limit holds while the model thinks (E1), request_timeout_s is named when the model does not answer (E6), ``finish``
takes ``built`` as a real yes or no (E4), the job's log and job-N.jsonl show what happened (E5), and a small context also cuts the
large arguments of older tool calls (E7). A stand-in OpenAI-compatible server answers; nothing here spends quota."""
import json
import select
import shutil
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dtree import agents, jobs
from dtree.agents import chat as chat_mod
from dtree.project import Project

ROOT = Path(__file__).resolve().parents[1]
Q = {"kind": "decision", "source": "claude", "key": "way", "title_th": "ทางเดิน", "title_en": "The way", "question_th": "ทางไหน", "question_en": "Which way?",
     "options": [{"key": "left", "th": "ซ้าย", "en": "Left", "recommended": True}, {"key": "right", "th": "ขวา", "en": "Right"}]}
FIN = {"built": False, "tests": "-", "impact": "-", "note": "n"}
COMMIT = "printf 'right\\n' > g.txt && git add g.txt && git commit -q -m 'chat commits' && echo ok"


class FakeModel:
    """An OpenAI-compatible /chat/completions answering a script, each answer costing 8 + 2 tokens. A step is {content, calls: [(name,
    args)]} and may also: ``delay`` its answer (watching whether the client gives the request up: ``abandoned``), give a call's
    arguments as they are (``raw``: an object, not a JSON string), ``cut`` its answer short, answer a ``body`` of its own, or other
    ``usage``. ``tls``: (certificate, key) to answer over https."""

    def __init__(self, script, tls=None):
        self.script, self.requests, self.abandoned = list(script), [], []
        model = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):                                        # available() asks a local server for /models
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data": []}')

            def do_POST(self):
                model.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                step = model.script.pop(0) if model.script else {"content": "nothing more"}
                if step.get("delay") and model._gone(self.connection, step["delay"]):
                    model.abandoned.append(len(model.requests))
                    return
                if step.get("cut"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "500")
                    self.end_headers()
                    self.wfile.write(b'{"choices": [')
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if "body" in step:
                    answer = step["body"]
                else:
                    msg = {"role": "assistant", "content": step.get("content", "")}
                    calls = [(n, a, False) for n, a in step.get("calls") or []] + [(n, a, True) for n, a in step.get("raw") or []]
                    if calls:
                        msg["tool_calls"] = [{"id": "c%d" % i, "type": "function",
                                              "function": {"name": n, "arguments": a if raw else json.dumps(a)}} for i, (n, a, raw) in enumerate(calls)]
                    answer = {"choices": [{"message": msg}], "usage": step.get("usage") or {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}}
                data = json.dumps(answer).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:                                      # the client gave up meanwhile
                    model.abandoned.append(len(model.requests))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*[str(x) for x in tls])
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "%s://127.0.0.1:%d/v1" % ("https" if tls else "http", self.httpd.server_address[1])

    @staticmethod
    def _gone(conn, seconds):
        """Wait ``seconds`` as a model thinking would; True as soon as the client closes its end (it gave the request up)."""
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([conn], [], [], 0.05)
            if ready:
                try:
                    if not socket.socket.recv(conn, 1, socket.MSG_PEEK):    # the raw socket: a TLS one too
                        return True
                except OSError:
                    return True
                time.sleep(0.05)
        return False

    def tool_results(self, i=-1):
        return [m["content"] for m in self.requests[i]["messages"] if m.get("role") == "tool"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def models():
    made = []

    def make(script):
        made.append(FakeModel(script))
        return made[-1]
    yield make
    for m in made:
        m.close()


def _project(repo, agents_cfg, **extra):
    cfg = {"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"), "db": str(repo["tmp"] / "d.sqlite"),
           "agents": agents_cfg, "chat": {"sandbox": "none"}}
    cfg.update(extra)
    return Project(repo["path"], cfg)


def _chat(model, **more):
    return dict({"kind": "chat", "base_url": model.url, "model": "m", "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0}, **more)


def _trial(p):
    did = p.store.add(Q)["id"]
    p.store.answer(did, "left")
    return did, p.store.try_option(did, "right")


def _run(p, agent="cheap"):
    did, t = _trial(p)
    return jobs.run(p, p.store.job_open(t["id"], agent=agent)["id"]), did, t


def _ctx(repo, lines=None, stopped=lambda: False, deadline=None):
    return agents.Context(repo["path"], repo["path"] / ".git", (lines if lines is not None else []).append, lambda pid: None, stopped,
                          deadline or time.time() + 60)


# ------------------------------------------------------------------------------------------------ F6: every token kept, whatever ends it
def test_f6_a_tool_argument_of_the_wrong_type_is_told_to_the_model_and_the_job_keeps_its_tokens(repo, models):
    """The review's probes B and B3: read_file with a number for its path, write_file with an object for its content (TypeError)."""
    model = models([{"calls": [("list_files", {})]}, {"calls": [("read_file", {"path": 5})]},
                    {"calls": [("write_file", {"path": "cfg.json", "content": {"a": 1}})]}, {"calls": [("finish", FIN)]}])
    p = _project(repo, {"cheap": _chat(model, month_tokens=1000)})
    j, _, _ = _run(p)
    assert len(model.requests) == 4, (j["reason"], len(model.requests))                # the model was told, and went on to finish
    told = model.tool_results()
    assert told[1].startswith("error: TypeError") and told[2].startswith("error: TypeError"), told
    assert (j["result"]["tokens"], j["result"]["tokens_in"], j["result"]["tokens_out"]) == (40, 32, 8), j["result"]
    assert j["cost_usd"] == pytest.approx((32 * 1.0 + 8 * 2.0) / 1e6) and j["reason"].startswith("the agent did not build it")


def test_f6_arguments_given_as_an_object_are_taken_as_they_are(repo, models):
    """Probe B2: some OpenAI-compatible servers give function.arguments as a JSON object, not a string."""
    model = models([{"raw": [("list_files", {"path": "."})]}, {"raw": [("finish", FIN)]}])
    p = _project(repo, {"cheap": _chat(model)})
    j, _, _ = _run(p)
    assert j["result"]["tokens"] == 20 and j["result"]["error"] is None and j["reason"] == "the agent did not build it: n", j
    assert "f.txt" in model.tool_results()[0]


def test_f6_an_answer_cut_short_ends_the_job_with_the_tokens_it_spent(repo, models):
    """Probe B4: http.client.IncompleteRead is not an OSError. The answer cut short was generated (a 200 came), so its request is
    counted too, as the previous prompt it carried whole (review of round 4), and the job's usage is partial."""
    model = models([{"calls": [("list_files", {})]}, {"calls": [("list_files", {})]}, {"cut": True}])
    p = _project(repo, {"cheap": _chat(model)})
    j, _, _ = _run(p)
    assert j["status"] == "failed" and "the model could not be asked" in j["reason"] and "IncompleteRead" in j["reason"], j["reason"]
    assert (j["result"]["tokens"], j["result"]["usage_partial"]) == (28, True), j["result"]
    assert j["cost_usd"] == pytest.approx((24 * 1.0 + 4 * 2.0) / 1e6)


def test_f6_an_answer_that_is_not_a_chat_completion_ends_the_job_with_its_tokens_and_says_why(repo, models):
    model = models([{"calls": [("list_files", {})]}, {"body": ["not", "an", "answer"]}])
    p = _project(repo, {"cheap": _chat(model)})
    j, _, _ = _run(p)
    assert j["status"] == "failed" and j["result"]["tokens"] == 10, (j["reason"], j["result"])
    assert "not a chat completion" in j["reason"], j["reason"]


def test_f6_any_other_failure_of_the_loop_keeps_the_tokens_and_is_reported(repo, models, monkeypatch):
    model = models([{"calls": [("list_files", {})]}, {"calls": [("list_files", {})]}, {"calls": [("finish", FIN)]}])
    p = _project(repo, {"cheap": _chat(model)})
    real = chat_mod._fit
    calls = []

    def breaks(messages, limit):                                     # an unexpected failure inside the loop, on its third request
        calls.append(1)
        if len(calls) == 3:
            raise ZeroDivisionError("a bug")
        return real(messages, limit)
    monkeypatch.setattr(chat_mod, "_fit", breaks)
    j, _, _ = _run(p)
    assert j["status"] == "failed" and j["result"]["tokens"] == 20, (j["reason"], j["result"])
    assert "ZeroDivisionError: a bug" in j["reason"] and "ZeroDivisionError" in "\n".join(jobs.log_tail(j)), j["reason"]


# ------------------------------------------------------------------------------- E1: a stop and the time limit hold while the model thinks
def test_e1_a_stop_while_the_model_thinks_ends_the_job_at_once_and_runs_nothing_it_answers(repo, models):
    model = models([{"delay": 20, "calls": [("write_file", {"path": "late.txt", "content": "after the stop"}), ("finish", FIN)]}])
    p = _project(repo, {"cheap": _chat(model)})
    did, t = _trial(p)
    jid = p.store.job_open(t["id"], agent="cheap")["id"]

    def stop_while_it_thinks():
        for _ in range(300):
            if model.requests:
                break
            time.sleep(0.05)
        time.sleep(0.5)
        p.store.job_set(jid, stop_requested=1)
        asked.append(time.time())
    asked = []
    threading.Thread(target=stop_while_it_thinks, daemon=True).start()
    j = jobs.run(p, jid)
    took = time.time() - asked[0]
    assert j["status"] == "stopped" and took < 5, (j["status"], j["reason"], took)                 # was the model's 20 s
    assert not (repo["tmp"] / "wt" / ("alt-%d-right" % did) / "late.txt").exists()
    for _ in range(40):                                              # the request was given up: the server sees the client leave
        if model.abandoned:
            break
        time.sleep(0.1)
    assert model.abandoned == [1]


def test_e1_the_job_s_time_limit_holds_while_the_model_thinks(repo, models):
    model = models([{"delay": 20, "calls": [("write_file", {"path": "late.txt", "content": "after the deadline"}), ("finish", FIN)]}])
    p = _project(repo, {"cheap": _chat(model)}, timeout_s=2)
    t0 = time.time()
    j, did, _ = _run(p)
    took = time.time() - t0
    assert j["status"] == "failed" and j["reason"] == "longer than timeout_s (2 s)" and took < 8, (j["reason"], took)
    assert not (repo["tmp"] / "wt" / ("alt-%d-right" % did) / "late.txt").exists()


def test_e1_an_answer_that_arrives_after_a_stop_is_not_acted_on(repo, models):
    """The model answers while the stop is being asked: its tool calls are not run (the review saw a write_file run after a stop)."""
    model = models([{"calls": [("list_files", {})]},
                    {"calls": [("write_file", {"path": "late.txt", "content": "after the stop"}), ("finish", FIN)]}])
    p = _project(repo, {"cheap": _chat(model)})
    a = agents.make(p.agent("cheap"), p)
    ctx = _ctx(repo, stopped=lambda: len(model.requests) >= 2)                      # the stop comes with the second answer
    out = a.run("the task", ctx)
    assert out["error"] == "stopped" and out["tokens"] in (10, 20), out             # 10: the wait for it saw the stop first
    assert not (repo["path"] / "late.txt").exists()


@pytest.mark.skipif(not shutil.which("openssl"), reason="no openssl to make a throwaway certificate")
def test_e1_over_https_an_answer_comes_and_a_stop_gives_the_request_up(tmp_path, monkeypatch):
    """Muse and Grok are https: the request thread's own handlers must work there, and giving up must shut a TLS connection too."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))                                 # trusted for this test only
    model = FakeModel([{"content": "over tls"}, {"delay": 20, "content": "late"}], tls=(cert, key))
    try:
        a = agents.make({"name": "tls", "kind": "chat", "base_url": model.url, "model": "m"}, Project(tmp_path, {}))
        ms = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        ctx = agents.Context(tmp_path, tmp_path, lambda l: None, lambda pid: None, lambda: False, time.time() + 60)
        assert a._ask(ms, a._settings(), ctx)["choices"][0]["message"]["content"] == "over tls"
        stop_at = time.time() + 1.0
        ctx = agents.Context(tmp_path, tmp_path, lambda l: None, lambda pid: None, lambda: time.time() > stop_at, time.time() + 60)
        t0 = time.time()
        with pytest.raises(chat_mod._Ended) as e:
            a._ask(ms, a._settings(), ctx)
        assert str(e.value) == "stopped" and time.time() - t0 < 5
        for _ in range(40):
            if model.abandoned:
                break
            time.sleep(0.1)
        assert model.abandoned == [2]
    finally:
        model.close()


# --------------------------------------------------------------------------------------- E6: request_timeout_s, named when it is reached
def test_e6_a_model_that_does_not_answer_within_request_timeout_s_is_named_in_the_reason(repo, models):
    model = models([{"delay": 10, "calls": [("finish", FIN)]}])
    p = _project(repo, {"slow": _chat(model, request_timeout_s=1)})
    t0 = time.time()
    j, _, _ = _run(p, "slow")
    assert j["status"] == "failed" and "request_timeout_s" in j["reason"] and "agents.slow.request_timeout_s" in j["reason"], j["reason"]
    assert time.time() - t0 < 8 and j["result"]["tokens"] == 0


def test_e6_request_timeout_s_is_documented():
    c = json.loads((ROOT / "src" / "dtree" / "schema" / "config.schema.json").read_text(encoding="utf-8"))
    for where in (c["$defs"]["agent"]["properties"], c["$defs"]["chat"]["properties"]):
        assert "600" in where["request_timeout_s"].get("description", ""), where["request_timeout_s"]
    doc = (ROOT / "docs" / "sandbox-and-budget.md").read_text(encoding="utf-8")
    assert "request_timeout_s" in doc


# --------------------------------------------------------------------------------------------------- E4: built is a real yes or no
def test_e4_finish_with_built_false_as_text_is_not_built(repo, models):
    """The review's stand-in committed, then finished with {"built": "false"}: the trial was taken as measured."""
    model = models([{"calls": [("run", {"command": COMMIT})]},
                    {"calls": [("finish", {"built": "false", "tests": "-", "impact": "-", "note": "it is not done"})]}])
    p = _project(repo, {"cheap": _chat(model)})
    j, _, t = _run(p)
    assert j["status"] == "failed" and j["reason"] == "the agent did not build it: it is not done", j["reason"]
    assert j["result"]["built"] is False and p.store.trial(t["id"])["status"] == "trying"


def test_e4_built_is_taken_only_as_true_or_false(repo, models):
    model = models([{"calls": [("finish", dict(FIN, built="True"))]}])
    p = _project(repo, {"cheap": _chat(model)})
    assert agents.make(p.agent("cheap"), p).run("the task", _ctx(repo))["built"] is True     # "true" / "false" as text, any case
    model = models([{"calls": [("finish", dict(FIN, built="yes"))]}, {"calls": [("finish", dict(FIN, built=1))]},
                    {"calls": [("finish", FIN)]}])
    p = _project(repo, {"cheap": _chat(model)})
    out = agents.make(p.agent("cheap"), p).run("the task", _ctx(repo))
    assert out["built"] is False and len(model.requests) == 3, (out, len(model.requests))
    told = [model.tool_results(1)[-1], model.tool_results(2)[-1]]
    assert all("built must be true or false" in x for x in told), told


# ------------------------------------------------------------------------------------------------------- E5: a log one can audit
def test_e5_the_job_s_log_and_jsonl_show_its_sandbox_each_tool_s_result_and_the_calls(repo, models, monkeypatch):
    box = "bwrap" if shutil.which("bwrap") else "none"
    model = models([{"calls": [("run", {"command": "git commit -q -m 'nothing here'"})]}, {"calls": [("finish", dict(FIN, built=True))]}])
    monkeypatch.setenv("FAKE_CHAT_KEY", "sk-test-not-real")
    p = _project(repo, {"cheap": _chat(model, key_env="FAKE_CHAT_KEY")}, chat={"sandbox": box})
    j, _, _ = _run(p)
    assert j["reason"].startswith("the agent said built but"), j["reason"]
    log = "\n".join(jobs.log_tail(j))
    assert ("🔒 sandbox: bwrap" if box == "bwrap" else "⚠ sandbox: none") in log, log
    assert "↳ exit 1" in log and "nothing to commit" in log, log
    raw = (repo["tmp"] / "logs" / ("job-%d.jsonl" % j["id"])).read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw.splitlines()]
    kinds = [e["type"] for e in events]
    assert "answer" in kinds and "tool_result" in kinds and kinds[-1] == "end", kinds
    answer = [e for e in events if e["type"] == "answer"][0]
    assert answer["message"]["tool_calls"][0]["function"]["name"] == "run" and answer["usage"]["total_tokens"] == 10
    result = [e for e in events if e["type"] == "tool_result"][0]
    assert result["name"] == "run" and result["content"].startswith("exit 1") and "nothing to commit" in result["content"]
    assert "sk-test-not-real" not in raw + log


# --------------------------------------------------------------------------------------- E7: the context also cuts old tool arguments
def test_e7_a_small_context_also_cuts_the_large_arguments_of_older_tool_calls():
    big = json.dumps({"path": "big.py", "content": "x" * 24000})
    ms = [{"role": "system", "content": "s"}, {"role": "user", "content": "task " * 50},
          {"role": "assistant", "content": "", "tool_calls": [{"id": "c0", "type": "function", "function": {"name": "write_file", "arguments": big}}]},
          {"role": "tool", "tool_call_id": "c0", "content": "written big.py"},
          {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]},
          {"role": "tool", "tool_call_id": "c1", "content": "big.py\nf.txt"}, {"role": "assistant", "content": "last"}]
    size = lambda m: sum(len(str(x.get("content") or "")) + len(json.dumps(x.get("tool_calls") or "")) for x in m)
    assert size(ms) > 16000
    out = chat_mod._fit(ms, 16000)
    assert size(out) <= 16000, size(out)
    args = json.loads(out[2]["tool_calls"][0]["function"]["arguments"])                # still JSON: a server may parse it
    assert args["path"] == "big.py" and args["content"].endswith("[cut to fit the context]") and len(args["content"]) < 400
    assert out[2]["tool_calls"][0]["id"] == "c0" and out[2]["tool_calls"][0]["function"]["name"] == "write_file"
    assert ms[2]["tool_calls"][0]["function"]["arguments"] == big                       # the conversation itself is untouched
    assert out[1] == ms[1] and out[-3:] == ms[-3:]
