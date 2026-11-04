"""Jobs and agents: Claude Code and Codex stand-ins (scripts that commit and answer in each CLI's JSON lines), a chat model behind a fake
OpenAI-compatible API, the sandbox, one job at a time, stop and reap. Nothing here spends quota."""
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dtree import jobs
from dtree.agents import chat as chat_mod
from dtree.project import Project
from dtree.store import DecisionError

Q = {"kind": "decision", "source": "claude", "key": "way", "title_th": "ทางเดิน", "title_en": "The way", "question_th": "ทางไหน", "question_en": "Which way?",
     "options": [{"key": "left", "th": "ซ้าย", "en": "Left", "recommended": True}, {"key": "right", "th": "ขวา", "en": "Right"}]}

CLAUDE = r'''#!%s
import json, os, subprocess, sys
open(os.environ["FAKE_PROMPT"], "w").write(sys.stdin.read())
mode = os.environ.get("FAKE_MODE", "build")
def ev(d):
    print(json.dumps(d), flush=True)
ev({"type": "system", "subtype": "init", "session_id": "s1", "model": "fake"})
if mode == "build":
    open("g.txt", "w").write("right\n")
    subprocess.run(["git", "add", "g.txt"], check=True); subprocess.run(["git", "commit", "-q", "-m", "build the other option"], check=True)
ev({"type": "assistant", "message": {"content": [{"type": "text", "text": "building"}, {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}})
ev({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.5, "session_id": "s1",
    "structured_output": {"built": mode == "build", "tests": "tests 3 passed", "impact": "goes right", "note": "built" if mode == "build" else "could not"}})
'''

CODEX = r'''#!%s
import json, os, subprocess, sys
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
assert args[0] == "exec" and "--json" in args and args[-1] == "-"
last = args[args.index("-o") + 1]
assert os.path.exists(args[args.index("--output-schema") + 1]) and "--add-dir" in args
open(os.environ["FAKE_PROMPT"], "w").write(sys.stdin.read())
def ev(d):
    print(json.dumps(d), flush=True)
ev({"type": "thread.started", "thread_id": "t1"})
open("g.txt", "w").write("right\n")
subprocess.run(["git", "add", "g.txt"], check=True); subprocess.run(["git", "commit", "-q", "-m", "codex builds right"], check=True)
ev({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "pytest -q", "exit_code": 0}})
ev({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "done"}})
ev({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}})
open(last, "w").write(json.dumps({"built": True, "tests": "3 passed", "impact": "right", "note": ""}))
'''


def _exe(path, text):
    path.write_text(text % sys.executable)
    path.chmod(0o755)
    return str(path)


def _project(repo, agents, **extra):
    cfg = {"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"), "db": str(repo["tmp"] / "d.sqlite"),
           "rules": ["Labels in Thai and English."], "test_command": "python -m pytest -q", "agents": agents}
    cfg.update(extra)
    return Project(repo["path"], cfg)


def _trial(p):
    did = p.store.add(Q)["id"]
    p.store.answer(did, "left")
    return did, p.store.try_option(did, "right")


@pytest.fixture
def fake_env(repo, monkeypatch):
    monkeypatch.setenv("FAKE_PROMPT", str(repo["tmp"] / "prompt.txt"))
    return repo


def test_claude_builds_the_trial_in_a_worktree_and_the_job_ends_measured(fake_env):
    repo = fake_env
    p = _project(repo, {"claude": {"command": _exe(repo["tmp"] / "claude", CLAUDE), "sandbox": "none"}})
    did, t = _trial(p)
    j = p.store.job_open(t["id"], "go right carefully", agent="claude")
    out = jobs.run(p, j["id"])
    assert out["status"] == "measured", out["reason"]
    tr = p.store.trial(t["id"])
    assert tr["status"] == "measured" and tr["result_tests"] == "[claude] tests 3 passed" and tr["result_impact"].startswith("goes right")
    assert [c["subject"] for c in tr["commits"]] == ["build the other option"] and p.store.get(did)["answer"] == "left"
    assert repo["git"]("rev-parse", "--abbrev-ref", "HEAD").strip() == "main" and repo["git"]("log", "-1", "--format=%s", "main").strip() == "first"
    assert not (repo["tmp"] / "wt" / ("alt-%d-right" % did)).exists() and p.git.has_branch(t["branch"])
    prompt = (repo["tmp"] / "prompt.txt").read_text()
    assert "[BUILD THIS ONE]" in prompt and "go right carefully" in prompt and "Labels in Thai and English." in prompt and "python -m pytest -q" in prompt
    log = "\n".join(jobs.log_tail(out))
    assert "🔧 Bash pytest -q" in log and "■ job measured" in log and out["cost_usd"] == 0.5 and out["agent"] == "claude"


def test_an_agent_that_does_not_build_it_fails_and_the_trial_stays_being_tried(fake_env, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "give_up")
    p = _project(fake_env, {"claude": {"command": _exe(fake_env["tmp"] / "claude", CLAUDE), "sandbox": "none"}})
    did, t = _trial(p)
    out = jobs.run(p, p.store.job_open(t["id"], agent="claude")["id"])
    assert out["status"] == "failed" and "could not" in out["reason"] and p.store.trial(t["id"])["status"] == "trying"


def test_codex_builds_it_too(fake_env):
    p = _project(fake_env, {"codex": {"command": _exe(fake_env["tmp"] / "codex", CODEX)}})
    ok, _ = jobs.agents.make(p.agent("codex"), p).available()
    assert ok
    did, t = _trial(p)
    out = jobs.run(p, p.store.job_open(t["id"], agent="codex")["id"])
    assert out["status"] == "measured", out["reason"]
    assert out["result"]["tokens"] == 120 and out["result"]["session"] == "t1"
    assert [c["subject"] for c in p.store.trial(t["id"])["commits"]] == ["codex builds right"]
    assert "🔧 pytest -q" in "\n".join(jobs.log_tail(out))


class FakeModel:
    """An OpenAI-compatible /chat/completions that answers with a script of tool calls, and records the requests."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        model = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"data": []}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model.requests.append({"body": body, "auth": self.headers.get("Authorization")})
                step = model.script.pop(0) if model.script else {"content": "nothing more"}
                msg = {"role": "assistant", "content": step.get("content", "")}
                if step.get("calls"):
                    msg["tool_calls"] = [{"id": "c%d" % i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(step["calls"])]
                data = json.dumps({"choices": [{"message": msg}], "usage": {"total_tokens": 10}}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]


def test_a_chat_model_builds_it_with_dtree_s_tools_in_the_sandbox(fake_env, monkeypatch):
    sandbox = "bwrap" if shutil.which("bwrap") else "none"
    model = FakeModel([
        {"content": "looking", "calls": [("list_files", {}), ("read_file", {"path": "f.txt"})]},
        {"calls": [("write_file", {"path": "../outside.txt", "content": "x"}), ("edit_file", {"path": "f.txt", "old": "one", "new": "one\nright"})]},
        {"calls": [("run", {"command": "git push origin main"}), ("run", {"command": "git add f.txt && git commit -q -m 'chat builds right' && echo ok"})]},
        {"calls": [("finish", {"built": True, "tests": "1 passed", "impact": "right", "note": "n"})]},
    ])
    monkeypatch.setenv("FAKE_MUSE_KEY", "sk-test-not-real")
    p = _project(fake_env, {"muse": {"kind": "chat", "base_url": model.url, "model": "muse-spark-1.3", "key_env": "FAKE_MUSE_KEY"}},
                 chat={"sandbox": sandbox, "max_steps": 10})
    assert jobs.agents.make(p.agent("muse"), p).available()[0]
    did, t = _trial(p)
    out = jobs.run(p, p.store.job_open(t["id"], agent="muse")["id"])
    assert out["status"] == "measured", out["reason"]
    assert [c["subject"] for c in p.store.trial(t["id"])["commits"]] == ["chat builds right"]
    assert not (fake_env["tmp"] / "outside.txt").exists()
    results = [m["content"] for m in model.requests[-1]["body"]["messages"] if m.get("role") == "tool"]
    assert any("outside the worktree" in r for r in results) and any(r.startswith("refused") for r in results) and any("ok" in r for r in results)
    assert model.requests[0]["auth"] == "Bearer sk-test-not-real" and model.requests[0]["body"]["model"] == "muse-spark-1.3"
    assert "sk-test-not-real" not in "\n".join(jobs.log_tail(out)) + open(fake_env["tmp"] / "logs" / ("job-%d.jsonl" % out["id"])).read()


@pytest.mark.skipif(not shutil.which("bwrap"), reason="no bubblewrap")
def test_the_sandbox_writes_only_the_worktree_and_has_no_network(tmp_path):
    wt, gd = tmp_path / "wt", tmp_path / "git"
    wt.mkdir(); gd.mkdir()
    a = chat_mod.ChatAgent({"name": "x", "kind": "chat"}, Project(tmp_path, {}))
    ctx = jobs.agents.Context(wt, gd, lambda l: None, lambda pid: None, lambda: False, 1e12)
    s = {"sandbox": "bwrap", "command_timeout_s": 20}
    assert a._run(ctx, "touch ok && echo done", None, s).startswith("exit 0")
    assert (wt / "ok").exists()
    import tempfile
    outside = tempfile.mkdtemp(prefix="dtree-test-", dir=os.path.expanduser("~"))        # /tmp is the sandbox's own: test outside it
    try:
        out = a._run(ctx, "touch %s/escape" % outside, None, s)
        assert not os.path.exists(os.path.join(outside, "escape")) and not out.startswith("exit 0")
    finally:
        shutil.rmtree(outside, ignore_errors=True)
    assert a._run(ctx, "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\"", None, s).split()[1] != "0"


def test_a_model_that_does_not_call_tools_fails_the_job(fake_env):
    model = FakeModel([{"content": "I would do it like this…"}] * 3)
    p = _project(fake_env, {"local": {"kind": "chat", "base_url": model.url, "model": "tiny"}}, chat={"sandbox": "none"})
    did, t = _trial(p)
    out = jobs.run(p, p.store.job_open(t["id"], agent="local")["id"])
    assert out["status"] == "failed" and "tool calls" in out["reason"]


def test_send_refuses_what_cannot_run_and_a_lost_runner_is_reaped(fake_env, monkeypatch):
    p = _project(fake_env, {"claude": {"command": _exe(fake_env["tmp"] / "claude", CLAUDE), "sandbox": "none"}, "grok": {"kind": "chat", "base_url": "https://api.x.ai/v1", "model": "grok-4", "key_env": "NO_SUCH_KEY_ENV"},
                            "ghost": {"kind": "claude", "command": str(fake_env["tmp"] / "none")}})
    monkeypatch.delenv("NO_SUCH_KEY_ENV", raising=False)
    did, t = _trial(p)
    avail = {a["name"]: a for a in jobs.agent_list(p)}
    assert avail["claude"]["available"] and not avail["grok"]["available"] and "NO_SUCH_KEY_ENV" in avail["grok"]["reason"] and not avail["ghost"]["available"]
    for name in ("grok", "ghost", "nobody"):
        with pytest.raises(DecisionError):
            jobs.send(p, t["id"], name)
    monkeypatch.setattr(jobs, "launch", lambda proj, jid: 999999)
    j = jobs.send(p, t["id"], "claude")
    assert j["runner_pid"] == 999999 and j["agent"] == "claude"
    real_alive = jobs._alive
    monkeypatch.setattr(jobs, "_alive", lambda pid, marker="": pid == 999999)            # the runner lives
    with pytest.raises(DecisionError):
        jobs.send(p, t["id"], "claude")                                                # one at a time
    monkeypatch.setattr(jobs, "_alive", real_alive)                                     # now it is gone
    assert jobs.reap(p.store) == [j["id"]] and p.store.job(j["id"])["status"] == "failed"
    j2 = jobs.send(p, t["id"])
    assert jobs.stop(p.store, j2["id"])["status"] == "stopped"


def test_the_real_runner_process_runs_a_job_end_to_end(fake_env):
    """``send`` → ``python -m dtree.jobs run`` as its own process → measured (the path the page takes)."""
    import time
    p = _project(fake_env, {"claude": {"command": _exe(fake_env["tmp"] / "claude", CLAUDE), "sandbox": "none"}})
    cfg_dir = fake_env["path"] / ".dtree"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({k: v for k, v in p.config.items() if k in ("name", "worktrees", "logs", "db", "agents")}))
    p = Project(fake_env["path"])
    did, t = _trial(p)
    j = jobs.send(p, t["id"], "claude")
    for _ in range(200):
        if p.store.job(j["id"])["status"] != "running":
            break
        time.sleep(0.1)
    assert p.store.job(j["id"])["status"] == "measured", p.store.job(j["id"])["reason"]


def test_a_race_builds_an_open_question_s_option_with_three_agents_and_the_person_adopts_one(fake_env):
    """The user, 2026-11-01: "choose a decision by having several AIs do the same work, to compare the results"."""
    import time
    repo = fake_env
    model = FakeModel([{"calls": [("run", {"command": "printf 'right\\n' > g.txt && git add g.txt && git commit -q -m 'chat builds right' && echo ok"})]},
                       {"calls": [("finish", {"built": True, "tests": "1 passed", "impact": "right, by chat", "note": ""})]}])
    agents_cfg = {"claude": {"command": _exe(repo["tmp"] / "claude", CLAUDE), "sandbox": "none"}, "codex": {"command": _exe(repo["tmp"] / "codex", CODEX)},
                  "local": {"kind": "chat", "base_url": model.url, "model": "tiny"}}
    (repo["path"] / ".dtree").mkdir()
    (repo["path"] / ".dtree" / "config.json").write_text(json.dumps({"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"),
                                                                     "db": str(repo["tmp"] / "d.sqlite"), "agents": agents_cfg, "max_parallel_jobs": 3,
                                                                     "chat": {"sandbox": "bwrap" if shutil.which("bwrap") else "none"}}))
    p = Project(repo["path"])
    did = p.store.add(Q)["id"]                                                         # open: nobody has chosen yet
    with pytest.raises(DecisionError):
        jobs.race(p, did, "right", ["claude", "codex", "local", "claude"] + ["x%d" % i for i in range(3)])     # more than max_parallel_jobs
    started = jobs.race(p, did, "right", ["claude", "codex", "local"], "same work for all")
    assert [r["ok"] for r in started] == [True, True, True], started
    assert sorted(r["trial"]["branch"] for r in started) == ["alt/%d-right--%s" % (did, a) for a in ("claude", "codex", "local")]
    for _ in range(300):
        if all(j["status"] != "running" for j in p.store.jobs(did=did)):
            break
        time.sleep(0.1)
    c = jobs.compare(p, did)
    assert c["status"] == "open" and len(c["rows"]) == 3
    assert {r["agent"]: r["status"] for r in c["rows"]} == {"claude": "measured", "codex": "measured", "local": "measured"}, [r["job"] for r in c["rows"]]
    assert all(r["commits"] == 1 and r["files"] == 1 and r["add"] == 1 for r in c["rows"])
    by_agent = {r["agent"]: r for r in c["rows"]}
    assert by_agent["claude"]["job"]["cost_usd"] == 0.5 and by_agent["codex"]["job"]["tokens"] == 120 and by_agent["local"]["job"]["tokens"] == 20
    with pytest.raises(DecisionError):
        p.store.trial_end(by_agent["codex"]["trial"], "switch")                         # open: nothing to keep beside
    p.store.trial_end(by_agent["codex"]["trial"], "adopted", "codex's is the cleanest", drop_others=True)
    d = p.store.get(did)
    assert d["status"] == "decided" and d["answer"] == "right"
    assert {t["variant"]: t["status"] for t in d["trials"]} == {"claude": "dropped", "codex": "adopted", "local": "dropped"}


def test_a_small_context_keeps_the_task_and_the_latest_and_cuts_old_tool_results():
    ms = [{"role": "system", "content": "s"}, {"role": "user", "content": "task " * 50}] + \
         [{"role": "tool", "tool_call_id": str(i), "content": "x" * 2000} for i in range(6)] + [{"role": "assistant", "content": "last"}]
    out = chat_mod._fit(ms, 5000)
    assert out[1] == ms[1] and out[-1] == ms[-1] and out[-2]["content"] == "x" * 2000              # task and latest kept whole
    assert out[2]["content"].endswith("[cut to fit the context]") and ms[2]["content"] == "x" * 2000   # old ones cut, the original untouched
    assert chat_mod._fit(ms, None) is ms
