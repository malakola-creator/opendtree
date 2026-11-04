"""What each question's AI jobs used, in the tree (the user asked 2026-11-02 "มีสรุป token แต่ละการตัดสินใจให้ใน tree ด้วยไหม"):
spend.usage counts every job of a question by the month report's own rule; the tree's nodes carry it and their branch's total; the
question's API answer, MCP dtree_get, `dtree usage` and the ADR show it by agent and trial; the page shows it as 🪙."""
import json
import time
import types

import pytest

import test_spend_r3 as s3
from dtree import adr, cli, mcp, server, spend
from test_page import NODE, j, run, text

MONTH = time.strftime("%Y-%m")


def _project(repo):
    """Agents of every kind of counting: claude reports dollars, muse is priced (estimated), plain has no price (unknown dollars)."""
    return s3._project(repo, agents={"claude": {"kind": "claude"},
                                     "muse": {"kind": "chat", "base_url": "https://api.example.invalid/v1", "model": "m",
                                              "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0},
                                     "plain": {"kind": "chat", "base_url": "https://api.example.invalid/v1", "model": "m"}})


def _questions(p):
    """#1 a root; #2 grew from #1 (topic), #3 from #2. Jobs on #1 (claude $0.25, muse 3000+1000 tokens) and #3 (plain 500+100 tokens,
    no price; and one still running)."""
    ids = []
    for i in range(3):
        did, t = s3._trial(p)
        ids.append((did, t))
    p.store.follow(ids[1][0], ids[0][0], how="topic", why="the same work")
    p.store.follow(ids[2][0], ids[1][0], how="topic", why="the same work")
    (d1, t1), _, (d3, t3) = ids
    s3._job(p, t1["id"], "claude", cost=0.25, result={"cost_usd": 0.25, "tokens": 12000, "tokens_in": 11800, "tokens_out": 200})
    m = p.store.try_option(d1, "right", variant="muse")
    s3._job(p, m["id"], "muse", cost=0.005, result={"tokens": 4000, "tokens_in": 3000, "tokens_out": 1000, "cost_usd": 0.005,
                                                   "cost_estimated": True})   # as the runner leaves it: estimated from muse's prices
    s3._job(p, t3["id"], "plain", result={"tokens": 600, "tokens_in": 500, "tokens_out": 100})
    s3._job(p, p.store.try_option(d3, "right", variant="again")["id"], "muse", status="running")
    return d1, ids[1][0], d3, t1, m, t3


def test_each_question_s_usage_is_counted_as_the_month_s_report_counts_it(repo):
    p = _project(repo)
    d1, d2, d3, t1, m, t3 = _questions(p)
    use, rep = spend.usage(p), spend.report(p, MONTH)
    assert sorted(use) == [d1, d3]                                             # #2 has no job of its own
    for k in spend.BRIEF:                                                      # every job is this month's: the sums agree
        assert round(sum(use[d][k] for d in use), 6) == round(rep["totals"][k], 6), k
    for name in ("claude", "muse", "plain"):
        mine = [use[d]["agents"][name] for d in use if name in use[d]["agents"]]
        for k in spend.BRIEF:
            assert round(sum(r[k] for r in mine), 6) == round(rep["agents"][name][k], 6), (name, k)
    q1 = use[d1]
    muse = (3000 * 1.0 + 1000 * 2.0) / 1e6                                     # estimated from muse's prices
    assert (q1["jobs"], q1["tokens"], q1["usd"], q1["usd_unknown_jobs"]) == (2, 16000, round(0.25 + muse, 6), 0), q1
    assert q1["agents"]["claude"]["usd"] == 0.25 and q1["agents"]["muse"]["usd_estimated"] == round(muse, 6)
    assert set(q1["trials"]) == {t1["id"], m["id"]} and q1["trials"][m["id"]]["tokens"] == 4000
    q3 = use[d3]
    assert (q3["jobs"], q3["running"], q3["tokens"], q3["usd_known_jobs"], q3["usd_unknown_jobs"]) == (2, 1, 600, 0, 1), q3


def test_a_question_s_usage_counts_its_jobs_of_every_month(repo):
    p = _project(repo)
    d1, d2, d3, t1, m, t3 = _questions(p)
    s3._job(p, p.store.try_option(d1, "right", variant="old")["id"], "muse", cost=0.002, created=s3._last_month(),
            result={"tokens": 1000, "tokens_in": 800, "tokens_out": 200, "cost_usd": 0.002, "cost_estimated": True})
    assert spend.usage(p)[d1]["tokens"] == 17000 and spend.usage(p)[d1]["jobs"] == 3
    assert spend.report(p, MONTH)["totals"]["tokens"] == 16600                                  # the month's report leaves it out


def test_a_deep_chain_of_questions_is_summed_without_recursion(repo):
    """branch() recursed once per level: a chain of ~1000 questions (dtree follow --how sequence) was a RecursionError, a 500."""
    from dtree.store import DecisionStore
    p = _project(repo)
    ids = []
    for i in range(1200):
        ids.append(p.store.add(dict(s3.Q, key="c%d" % i))["id"])
        if i:
            p.store.follow(ids[-1], ids[-2], how="sequence", why="next")
    t = p.store.try_option(ids[-1], "right")
    s3._job(p, t["id"], "plain", result={"tokens": 5, "tokens_in": 4, "tokens_out": 1})
    nodes = {n["id"]: n for n in spend.tree_usage(p, p.store.tree())["nodes"]}
    assert nodes[ids[0]]["usage_branch"]["tokens"] == 5 and nodes[ids[-1]]["usage"]["tokens"] == 5 and nodes[ids[-1]]["usage_branch"] is None


def test_the_cli_total_keeps_its_estimate_mark(repo, capsys):
    """0.1 + 0.7 is 0.7999999999999999 < round(0.8): the total lost its "~" though every row had it."""
    p = _project(repo)
    for cost in (0.1, 0.7):
        did, t = s3._trial(p)
        s3._job(p, t["id"], "muse", cost=cost, result={"tokens": 100000, "tokens_in": 100000, "tokens_out": 0, "cost_usd": cost, "cost_estimated": True})
    spend.run_cli(types.SimpleNamespace(cmd="usage", id=None, json=False), p)
    lines = capsys.readouterr().out.splitlines()
    assert "~$0.10" in lines[0] and "~$0.70" in lines[1] and "~$0.80" in lines[2], lines


def test_the_tree_carries_each_question_s_usage_and_its_branch(repo):
    p = _project(repo)
    d1, d2, d3, *_ = _questions(p)
    nodes = {n["id"]: n for n in spend.tree_usage(p, p.store.tree())["nodes"]}
    assert nodes[d1]["usage"]["tokens"] == 16000 and nodes[d2]["usage"] is None and nodes[d3]["usage"]["tokens"] == 600
    assert nodes[d1]["usage_branch"]["tokens"] == 16600 and nodes[d1]["usage_branch"]["jobs"] == 4          # #1 with #2 and #3
    assert nodes[d2]["usage_branch"]["tokens"] == 600 and nodes[d2]["usage_branch"]["running"] == 1          # #2 with #3
    assert nodes[d3]["usage_branch"] is None                                                                  # nothing grew from #3
    assert set(nodes[d1]["usage"]) == set(spend.BRIEF)                                                        # the tree carries totals only


def test_the_api_and_mcp_give_a_question_s_usage_by_agent_and_trial(repo):
    p = _project(repo)
    d1, d2, d3, t1, m, t3 = _questions(p)
    api = server.Api(p)
    code, out = api.handle("GET", "/api/decisions/%d" % d1, {}, {})
    assert code == 200 and set(out["item"]["usage"]["agents"]) == {"claude", "muse"}, out["item"].get("usage")
    assert set(out["item"]["usage"]["trials"]) == {t1["id"], m["id"]}                # JSON makes them strings (below)
    code, out = api.handle("GET", "/api/decisions/%d" % d2, {}, {})
    assert code == 200 and out["item"]["usage"] is None
    code, tree = api.handle("GET", "/api/decisions/tree", {}, {})
    assert code == 200 and {n["id"]: (n["usage"] or {}).get("tokens") for n in tree["nodes"]} == {d1: 16000, d2: None, d3: 600}
    got = mcp.Server(p).call("dtree_get", {"id": d3})
    assert got["usage"]["agents"]["plain"]["tokens"] == 600 and got["usage"]["running"] == 1
    # through real HTTP and JSON too: trial ids become strings as JSON keys
    import test_r3_core as r3
    httpd, port = r3._serve(p)
    try:
        status, body = r3._http(port, "GET", "/api/decisions/%d" % d1)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 200 and set(json.loads(body)["item"]["usage"]["trials"]) == {str(t1["id"]), str(m["id"])}


def test_dtree_usage_lists_the_questions_and_breaks_one_down(repo, capsys):
    p = _project(repo)
    d1, d2, d3, *_ = _questions(p)
    spend.run_cli(types.SimpleNamespace(cmd="usage", id=None, json=False), p)
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("#%-4d" % d1) and "2 jobs · 16,000 tok (in 14,800 / out 1,200) · %s · " % spend._usd(0.255) in lines[0], out
    assert lines[1].startswith("#%-4d" % d3) and "2 jobs · 600 tok (in 500 / out 100) · - · " in lines[1] and "1 running" in lines[1], out
    assert lines[2].startswith("total · 2 questions · 4 jobs · 16,600 tok") and "+ ?" in lines[2], out
    spend.run_cli(types.SimpleNamespace(cmd="usage", id=d2, json=False), p)
    out = capsys.readouterr().out
    assert "no AI job of this question" in out and "with the questions that grew from it: 2 jobs · 600 tok" in out, out
    spend.run_cli(types.SimpleNamespace(cmd="usage", id=d1, json=False), p)
    out = capsys.readouterr().out
    assert "agent claude" in out and "agent muse" in out and "trial " in out and "right--muse" in out, out
    assert "with the questions that grew from it: 4 jobs · 16,600 tok" in out, out
    spend.run_cli(types.SimpleNamespace(cmd="usage", id=d1, json=True), p)
    data = json.loads(capsys.readouterr().out)
    assert data["usage"]["tokens"] == 16000 and data["usage_branch"]["tokens"] == 16600


def test_dtree_usage_is_a_command_and_says_what_it_counts(repo, capsys):
    root = s3._config(repo, {"agents": {"claude": {"kind": "claude"}}})
    assert cli.main(["--project", str(root), "usage"]) == 0
    assert "only the jobs dtree runs are counted" in capsys.readouterr().out
    assert cli.main(["--project", str(root), "usage", "99"]) == 1


def test_the_adr_names_what_the_ai_jobs_used(repo):
    p = _project(repo)
    d1, d2, d3, *_ = _questions(p)
    paths = {x.name: x for x in adr.export(p, out=str(repo["tmp"] / "adr"))}
    first = [x for name, x in paths.items() if name.startswith("%04d-" % d1)][0].read_text(encoding="utf-8")
    assert "- AI jobs run by dtree (finished): 2 jobs · 16,000 tokens (in 14,800 / out 1,200) · %s · " % spend._usd(0.255) in first, first
    third = [x for name, x in paths.items() if name.startswith("%04d-" % d3)][0].read_text(encoding="utf-8")
    assert "- AI jobs run by dtree (finished): 1 job · 600 tokens (in 500 / out 100) · dollars not known · " in third, third   # not the running one
    time.sleep(1.1)                                                            # the running job's time moves on: the record does not
    again = {x.name: x.read_text(encoding="utf-8") for x in adr.export(p, out=str(repo["tmp"] / "adr"))}
    assert again[[n for n in again if n.startswith("%04d-" % d3)][0]] == third
    code, out = server.Api(p).handle("GET", "/api/decisions/%d/adr" % d1, {}, {})
    assert code == 200 and "AI jobs run by dtree (finished): 2 jobs" in out["markdown"]


# ------------------------------------------------------------------------------------------------------------------- the page
U1 = {"jobs": 2, "running": 0, "tokens": 16000, "tokens_in": 14800, "tokens_out": 1200, "usd": 0.255, "usd_estimated": 0.005,
      "usd_known_jobs": 2, "usd_unknown_jobs": 0, "tokens_unknown_jobs": 0, "seconds": 130.0}
U3 = {"jobs": 2, "running": 1, "tokens": 600, "tokens_in": 500, "tokens_out": 100, "usd": 0.0, "usd_estimated": 0.0,
      "usd_known_jobs": 0, "usd_unknown_jobs": 1, "tokens_unknown_jobs": 1, "seconds": 61.0}


@pytest.mark.skipif(NODE is None, reason="no node to run the page's script")
def test_the_page_says_what_a_question_s_ai_jobs_used():
    got = run(["useLine(%s)" % j(U1), "useLine(%s)" % j(U3), "useTok(%s)" % j(U1), "useTok(%s)" % j(U3), "moneyText(%s)" % j(U3),
               "useLine(null)"])
    assert got[0] == "2 jobs · 16,000 tok (in 14,800 / out 1,200) · $0.26 · 2:10", got[0]
    assert got[1] == "2 jobs · 600 + ? tok (in 500 / out 100) · — · 1:01 · ⏳ 1", got[1]
    assert got[2:] == ["16k", "600+?⏳", "—", ""], got[2:]                       # ⏳: one of its jobs still runs
    th = run(["useLine(%s)" % j(U1)], lang="th")[0]
    assert th.startswith("2 งาน · 16,000 tok (เข้า 14,800 / ออก 1,200)"), th


@pytest.mark.skipif(NODE is None, reason="no node to run the page's script")
def test_the_side_panel_splits_it_by_ai_and_trial_and_names_the_branch():
    full = dict(U1, agents={"claude": dict(U1, jobs=1, tokens=12000, tokens_in=11800, tokens_out=200, usd=0.25, usd_estimated=0, usd_known_jobs=1),
                            "muse": dict(U1, jobs=1, tokens=4000, tokens_in=3000, tokens_out=1000, usd=0.005, usd_known_jobs=1)},
                trials={"1": dict(U1, jobs=1, tokens=12000), "4": dict(U1, jobs=1, tokens=4000)})
    node = {"id": 1, "trials": [{"id": 1, "option": "right", "variant": None}, {"id": 4, "option": "right", "variant": "muse"}],
            "usage": U1, "usage_branch": dict(U1, jobs=4, tokens=16600)}
    h = run(["usageHtml(%s, %s)" % (j(full), j(node)), "usageHtml(null, %s)" % j(dict(node, usage=None)), "usageHtml(null, {})"])
    t = text(h[0])
    assert t.startswith("2 jobs · 16,000 tok (in 14,800 / out 1,200)") and ("Claude Code" in t or "claude" in t), t
    assert "⎇ trial 1 right" in t and "⎇ trial 4 right--muse" in t and "total" in t, t
    assert "jobs tokens in out $ time" in t and "claude 1 12,000 11,800 200 $0.25" in t, t            # all the tokens, then in / out
    assert '<div class="cmpwrap">' in h[0]                                                            # scrolls inside the panel
    assert "with the questions that grew from it: 4 jobs · 16,600 tok" in t and "only the jobs dtree ran" in t, t
    assert text(h[1]).startswith("no AI job of this question yet with the questions that grew from it: 4 jobs"), text(h[1])
    assert text(h[2]).startswith("no AI job of this question yet only the jobs dtree ran"), text(h[2])


@pytest.mark.skipif(NODE is None, reason="no node to run the page's script")
def test_the_board_s_card_shows_the_tokens():
    got = run(["chipsHtml(%s)" % j({"id": 1, "trials": [], "usage": U3}), "chipsHtml(%s)" % j({"id": 2, "trials": [], "usage": None})])
    assert "🪙 600+?⏳" in text(got[0]) and "this question's AI jobs: 2 jobs · 600 + ? tok" in got[0], got[0]
    assert "🪙" not in got[1]


@pytest.mark.skipif(NODE is None, reason="no node to run the page's script")
def test_a_question_node_s_second_line_carries_its_tokens_and_its_folded_branch():
    d = {"id": 1, "status": "decided", "source": "claude", "code": 12, "usage": U1, "usage_branch": dict(U1, tokens=123456, tokens_unknown_jobs=1)}
    got = run(["nodeSub(%s, 0, false)" % j(d), "nodeSub(%s, 4, false)" % j(d), "nodeSub(%s, 4, true)" % j(d),
               "nodeSub(%s, 0, false)" % j(dict(d, usage=None, usage_branch=None)), "nodeSub(%s, 3, false)" % j(dict(d, usage=None))])
    assert got[0].endswith(" · ⟨/⟩ 12 · 🪙16k") and "Claude" in got[0], got[0]
    assert got[1].endswith(" · ⟨/⟩ 12 · ▸ 4 · 🪙Σ123.5k+?"), got[1]                     # folded: its branch, marked Σ
    assert "Claude" not in got[2] and got[2].endswith("▸ 4 · 🪙Σ123.5k+?"), got[2]       # short: who raised it goes first
    assert "🪙" not in got[3] and got[4].endswith("▸ 3 · 🪙Σ123.5k+?"), got[4]
