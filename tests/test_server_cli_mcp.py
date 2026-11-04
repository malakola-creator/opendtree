"""The web server over real HTTP, the CLI's git flow (start → commit → merge) and init, and the MCP tools over stdio."""
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from dtree import cli, mcp, server
from dtree.project import Project
from dtree.store import DecisionError

Q = {"kind": "decision", "title_th": "ทาง", "title_en": "Way", "question_th": "ไปทางไหน", "question_en": "Which way?",
     "options": [{"key": "yes", "th": "ไป", "en": "Go", "recommended": True}, {"key": "no", "th": "ไม่ไป", "en": "Stay"}]}


def _project(repo, **extra):
    cfg = {"name": "demo", "db": str(repo["tmp"] / "d.sqlite"), "logs": str(repo["tmp"] / "logs"), "agents": {"local": {"kind": "chat", "base_url": "http://127.0.0.1:9/v1", "model": "m"}}}
    cfg.update(extra)
    return Project(repo["path"], cfg)


def test_the_server_serves_the_page_and_the_api(repo):
    p = _project(repo)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(p))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "<html" in page.lower() and "/api/agents" in page
        def call(path, body=None):
            req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
            try:
                return json.load(urllib.request.urlopen(req))
            except urllib.error.HTTPError as exc:
                return dict(json.load(exc), status=exc.code)
        assert call("/api/project")["name"] == "demo"
        ag = call("/api/agents")
        assert ag["default"] == "claude" and {a["name"] for a in ag["items"]} >= {"claude", "codex", "muse", "grok", "local"}
        local = [a for a in ag["items"] if a["name"] == "local"][0]
        assert not local["available"] and "does not answer" in local["reason"]
        d = call("/api/decisions", Q)
        assert d["ok"] and d["item"]["source"] == "user" and d["counts"]["open"] == 1
        did = d["item"]["id"]
        assert call("/api/decisions/%d/answer" % did, {"option": "yes", "note": "n"})["item"]["status"] == "decided"
        t = call("/api/decisions/%d/try" % did, {"option": "no"})["item"]
        bad = call("/api/decisions/trials/%d/ai" % t["id"], {"agent": "local"})
        assert not bad["ok"] and bad["status"] == 400 and "cannot run here" in bad["error"]
        assert call("/api/decisions/tree")["nodes"][0]["options"][1]["trials"][0]["id"] == t["id"]
        assert call("/api/decisions/%d/code" % did)["git"] is True
        assert call("/api/decisions/999")["status"] == 404 and call("/api/nothing")["status"] == 404
        assert call("/api/decisions/jobs")["items"] == []
    finally:
        httpd.shutdown()
    assert (repo["path"] / ".dtree" / "write_token").exists()


def test_start_commit_merge_keeps_a_decision_s_branch_and_commits(repo):
    p = _project(repo, merge_trailer="Co-Authored-By: someone <x@example.invalid>")
    s = p.store
    a = s.add(Q)["id"]
    with pytest.raises(DecisionError):
        cli.start(p, a)
    s.answer(a, "yes")
    assert "new, from main" in cli.start(p, a) and p.git.current_branch() == "dec/%d-yes" % a
    (repo["path"] / "f.txt").write_text("one\ntwo\n")
    with pytest.raises(DecisionError):
        cli.merge(p, a)                                                       # uncommitted
    repo["git"]("commit", "-q", "-am", "the work")
    assert "1 commits" in cli.merge(p, a) and p.git.current_branch() == "dec/%d-yes" % a      # merged by plumbing: no checkout switched
    kept = s.get(a)["commits"]
    assert kept[0]["subject"] == "the work" and kept[-1]["merge"] and "someone" in repo["git"]("log", "-1", "--format=%B", "main")
    assert p.git.branch_info("dec/%d-yes" % a)["merged"]
    assert "the work" in cli.code(p, a, [kept[0]["sha"][:7]], "again", "recorded") and len(s.get(a)["commits"]) == 2


def test_init_writes_the_config_once_and_the_cli_runs(repo, capsys, monkeypatch):
    path = cli.main(["--project", str(repo["path"]), "init", "--name", "demo"]) or (repo["path"] / ".dtree" / "config.json")
    cfg = json.loads((repo["path"] / ".dtree" / "config.json").read_text())
    assert cfg["name"] == "demo" and "write_token" in (repo["path"] / ".dtree" / ".gitignore").read_text()
    (repo["path"] / ".dtree" / "config.json").write_text(json.dumps(dict(cfg, rules=["keep"])))
    cli.main(["--project", str(repo["path"]), "init"])
    assert json.loads((repo["path"] / ".dtree" / "config.json").read_text())["rules"] == ["keep"]      # not overwritten
    qf = repo["tmp"] / "q.json"
    qf.write_text(json.dumps(Q, ensure_ascii=False))
    monkeypatch.chdir(repo["path"])
    assert cli.main(["add", str(qf)]) == 0 and cli.main(["answer", "1", "yes"]) == 0 and cli.main(["agents"]) == 0
    out = capsys.readouterr().out
    assert "#1" in out and "decided" in out and "claude" in out and Project.find().store.get(1)["source"] == "agent"


def test_mcp_tools_raise_questions_and_close_work(repo):
    p = _project(repo)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "dtree_ask", "arguments": Q}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "dtree_done", "arguments": {"id": 1, "tests": "t", "impact": "i"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "dtree_get", "arguments": {"id": 77}}},
            {"jsonrpc": "2.0", "id": 6, "method": "nope"}]
    out = io.StringIO()
    mcp.serve_stdio(p, io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\nnot json\n"), out)
    got = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [g.get("id") for g in got] == [1, 2, 3, 4, 5, 6, None]                  # the notification has no answer
    assert got[0]["result"]["serverInfo"]["name"] == "dtree" and {t["name"] for t in got[1]["result"]["tools"]} >= {"dtree_ask", "dtree_list", "dtree_done"}
    asked = json.loads(got[2]["result"]["content"][0]["text"])
    assert asked["id"] == 1 and asked["source"] == "agent" and asked["status"] == "open"
    assert got[3]["result"]["isError"] and "decided" in got[3]["result"]["content"][0]["text"]        # not answered: not done
    assert got[4]["result"]["isError"] and got[5]["error"]["code"] == -32601 and got[6]["error"]["code"] == -32700


def test_adr_files_supersede_pros_cons_and_the_context_for_agents(repo):
    """ADR's strengths (2026-11-01): a Markdown record per question kept with the code, superseded-by, the good and bad of each
    option, and the decisions in force for an agent."""
    from dtree import adr
    p = _project(repo)
    s = p.store
    q = dict(Q, options=[dict(Q["options"][0], pros=["quick"], cons=["no rollback"]), Q["options"][1]], context_en="We need to move.")
    a = s.add(q)["id"]
    s.answer(a, "yes", "go now")
    b = s.add(dict(Q, key="b", title_en="Way again"))["id"]
    with pytest.raises(DecisionError):
        s.supersede(b, a)                                                              # not decided, and earlier
    with pytest.raises(DecisionError):
        s.add(dict(Q, key="c", options=[dict(Q["options"][0], pros="not a list")]))
    s.answer(b, "no")
    s.supersede(a, b, "changed our mind")
    tree = {n["id"]: n for n in s.tree()["nodes"]}
    assert tree[a]["superseded_by"] == b and tree[b]["supersedes"] == [a] and tree[a]["options"][0]["pros"] == ["quick"]
    paths = adr.export(p)
    names = sorted(x.name for x in paths)
    assert names == ["0001-way.md", "0002-way-again.md", "README.md"]
    one = (repo["path"] / "docs" / "decisions" / "0001-way.md").read_text()
    assert "Status: **superseded by [#2 Way again](0002-way-again.md)**" in one and "- Good, because quick" in one and "- Bad, because no rollback" in one
    assert "Chosen option: **`yes`**" in one and "> go now" in one and "We need to move." in one
    assert "Supersedes: [#1 Way](0001-way.md)" in (repo["path"] / "docs" / "decisions" / "0002-way-again.md").read_text()
    ctx = adr.context(p)
    assert "#2 Way again → **no**" in ctx and "#1 " not in ctx                          # superseded: not in force
    assert adr.export(p) == paths                                                       # the same files again, unchanged
    s.supersede(a, None)
    assert s.get(a)["superseded_by"] is None


def test_follow_up_and_diff_go_on_with_a_trial_on_its_branch(repo, monkeypatch):
    """Vibe Kanban's strengths (2026-11-01): the full diff of a trial, and a follow-up with feedback on the same branch."""
    from dtree import jobs
    p = _project(repo, agents={"claude": {"command": str(repo["tmp"] / "fake")}})
    (repo["tmp"] / "fake").write_text("#!/bin/sh\n"); (repo["tmp"] / "fake").chmod(0o755)
    s = p.store
    did = s.add(Q)["id"]
    t = s.try_option(did, "no")
    p.git.run("branch", t["branch"], "main")
    wt = repo["tmp"] / "w"
    p.git.run("worktree", "add", str(wt), t["branch"])
    (wt / "g.txt").write_text("stay\n")
    p.git.run("add", "g.txt", cwd=wt); p.git.run("commit", "-q", "-m", "stay put", cwd=wt)
    d = p.git.diff("main", t["branch"])
    assert d["exists"] and d["files"] == [{"path": "g.txt", "add": 1, "del": 0}] and "+stay" in d["diff"] and not d["cut"]
    assert not p.git.diff("main", "alt/nope")["exists"]
    s.record_code(did, p.git.commits_between("main", t["branch"]), trial=t["id"])
    s.trial_result(t["id"], "1 passed", "stays")
    started = []
    monkeypatch.setattr(jobs, "launch", lambda proj, jid: started.append(jid) or 999997)
    j = jobs.followup(p, t["id"], "also write why", None)
    assert started == [j["id"]] and j["agent"] == "claude" and j["note"] == "also write why"
    tr = s.trial(t["id"])
    assert tr["status"] == "trying" and tr["result_tests"] is None
    ev = [e for e in s.get(did)["events"] if e["action"] == "trial_followup"][0]
    assert ev["detail"]["was"]["tests"] == "1 passed"
    prompt = jobs.prompt_for(p, s.job(j["id"]), wt)
    assert "FOLLOW-UP" in prompt and "stay put" in prompt and "The person's feedback: also write why" in prompt
