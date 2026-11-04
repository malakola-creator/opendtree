"""The decision store (ported from ultimateTruth's tests/test_decisions.py when dtree was split out, 2026-11-01)."""
import sqlite3
import types
from pathlib import Path

import pytest

from dtree.store import DecisionError, DecisionStore
from conftest import client_for

Q = {"kind": "decision", "source": "claude", "key": "gap_3_13", "title_th": "เดินอ้อมผนัง", "title_en": "Detour round walls",
     "question_th": "จะทำแผนที่ไหม", "question_en": "Build a map?", "context_th": "ร่างตายซ้ำ", "refs": ["docs/gaps.md#3.13"],
     "options": [{"key": "yes", "th": "ทำ", "en": "Do it", "recommended": True}, {"key": "no", "th": "ยังไม่ทำ", "en": "Not yet"}]}


def test_a_question_is_kept_answered_and_its_history_told():
    s = DecisionStore(":memory:")
    a = s.add(Q)
    assert a["status"] == "open" and a["options"][0]["recommended"] and a["refs"] == ["docs/gaps.md#3.13"]
    again = s.add(dict(Q, context_th="ตายอีก"))                           # same key while open: counted, not duplicated
    assert again["id"] == a["id"] and again["occurrences"] == 2 and s.counts()["open"] == 1
    with pytest.raises(DecisionError):
        s.answer(a["id"], "maybe")                                         # not one of the options
    with pytest.raises(DecisionError):
        s.answer(a["id"], None, "")                                        # neither an option nor a note
    d = s.answer(a["id"], "yes", "เริ่มเลย")
    assert d["status"] == "decided" and d["answer"] == "yes" and d["note"] == "เริ่มเลย" and d["decided_at"]
    assert s.list("open") == [] and [x["id"] for x in s.list("closed")] == [a["id"]]
    r = s.reopen(a["id"], "ยังไม่พอ")
    assert r["status"] == "open" and r["answer"] is None
    s.dismiss(a["id"], "ไม่ทำแล้ว")
    events = [e["action"] for e in s.get(a["id"])["events"]]
    assert events == ["raised", "seen_again", "answered", "reopened", "dismissed"]
    assert s.add(Q)["id"] != a["id"]                                       # closed: the same key raises a new question


def test_the_store_survives_a_restart(tmp_path):
    path = tmp_path / "d.sqlite"
    s = DecisionStore(path)
    did = s.add(Q)["id"]
    s.answer(did, "no", "")
    s.close()
    s2 = DecisionStore(path)
    assert s2.get(did)["answer"] == "no" and len(s2.get(did)["events"]) == 2


def test_the_questions_make_a_decision_tree_grown_on_the_branch_taken():
    s = DecisionStore(":memory:")
    a = s.add(Q)["id"]
    s.answer(a, "yes", "")
    b = s.add(dict(Q, key="b", context_th="ต่อจาก #%d ที่ตัดสินแล้ว" % a))["id"]
    c = s.add(dict(Q, key="c", refs=["#%d" % b, "docs/architecture.md#%d.61" % a]))["id"]   # a reference names #b; an anchor names nothing
    d = s.add(dict(Q, key="d", question_th="ทั้ง #%d และ (#%d) และ #%d.5" % (a, c, b)))["id"]
    t = s.tree()
    by = {n["id"]: n for n in t["nodes"]}
    assert t["roots"] == [a] and by[b]["parent"] == a and by[c]["parent"] == b and by[d]["parent"] == c and by[d]["also"] == [a]
    assert [o["chosen"] for o in by[a]["options"]] == [True, False] and not any(o["chosen"] for o in by[b]["options"])
    assert by[a]["options"][0]["recommended"] and by[a]["events"] == 2
    client = client_for(s)
    j = client.get("/api/decisions/tree").get_json()
    assert j["ok"] and j["roots"] == [a] and len(j["nodes"]) == 4 and j["counts"]["open"] == 3


def test_a_question_naming_none_is_linked_to_its_earlier_topic_so_the_tree_starts_at_one():
    """2026-11-01: the user asked for the tree to start at #1: a question whose text names no earlier one is told which it grew from."""
    s = DecisionStore(":memory:")
    a = s.add(Q)["id"]
    b = s.add(dict(Q, key="b"))["id"]
    c = s.add(dict(Q, key="c", follows=a, follows_why="same subject"))["id"]          # the link given when the question is raised
    assert s.tree()["roots"] == [a, b]
    with pytest.raises(DecisionError):
        s.follow(b, b + 1, why="x")                                                  # only an earlier question
    with pytest.raises(DecisionError):
        s.follow(b, a)                                                               # the reason is required
    with pytest.raises(DecisionError):
        s.follow(b, a, how="maybe", why="x")
    before = s.get(b)["updated"]
    s.follow(b, a, how="sequence", why="asked next, no shared subject")
    t = s.tree()
    by = {n["id"]: n for n in t["nodes"]}
    assert t["roots"] == [a] and by[b]["parent"] == a and by[b]["parent_how"] == "sequence" and by[c]["parent_how"] == "topic"
    assert by[c]["parent_why"] == "same subject" and by[a]["parent_how"] is None
    assert s.get(b)["updated"] == before and s.get(b)["events"][-1]["action"] == "followed"          # the tree link does not reorder the list
    d = s.add(dict(Q, key="d", context_th="หลัง #%d" % b))["id"]
    assert {n["id"]: n for n in s.tree()["nodes"]}[d]["parent_how"] == "named"
    s.follow(b, None)
    assert s.tree()["roots"] == [a, b]                                               # removed: b is a root again (d naming it is d's link, not b's)


def test_another_option_is_tried_on_its_own_branch_and_the_later_questions_stay_where_they_grew():
    """2026-11-01: "if another option is chosen later, can it be built, to grow the tree along a different branch" — a trial keeps the
    answer until it is adopted; the questions that grew on the old answer stay on it (and are named for review)."""
    s = DecisionStore(":memory:")
    a = s.add(Q)["id"]
    gone = s.add(dict(Q, key="gone"))["id"]
    s.dismiss(gone)
    with pytest.raises(DecisionError):
        s.try_option(gone, "no")                                                     # dismissed: nothing to try (open ones can be, see test_agents)
    s.answer(a, "yes")
    b = s.add(dict(Q, key="b", context_th="ต่อจาก #%d" % a))["id"]
    c = s.add(dict(Q, key="c", context_th="ต่อจาก #%d" % b))["id"]
    with pytest.raises(DecisionError):
        s.try_option(a, "yes")                                                       # that is the answer
    with pytest.raises(DecisionError):
        s.try_option(a, "maybe")                                                     # not an option
    t = s.try_option(a, "no", "ลองดู")
    assert t["status"] == "trying" and t["branch"] == "alt/%d-no" % a and t["review"] == [b, c]
    with pytest.raises(DecisionError):
        s.try_option(a, "no")                                                        # tried already
    with pytest.raises(DecisionError):
        s.trial_end(t["id"], "adopted")                                              # measure first
    by = {n["id"]: n for n in s.tree()["nodes"]}
    assert by[a]["answer"] == "yes" and by[a]["options"][1]["trials"][0]["status"] == "trying" and by[b]["on_option"] == "yes"
    s.trial_result(t["id"], "pytest 10 ผ่าน", "หาอาหารเร็วขึ้น")
    s.trial_end(t["id"], "adopted", "ใช้ทางนี้")
    item = s.get(a)
    assert item["answer"] == "no" and item["status"] == "decided" and item["trials"][0]["status"] == "adopted"
    assert [e["action"] for e in item["events"]][-3:] == ["trial_measured", "trial_adopted", "answered"]
    by = {n["id"]: n for n in s.tree()["nodes"]}
    yes, no = by[a]["options"]
    assert no["chosen"] and yes["was_chosen"] and not yes["chosen"]
    assert by[b]["parent"] == a and by[b]["on_option"] == "yes"                      # b grew on "yes" and stays there
    d = s.add(dict(Q, key="d", context_th="หลัง #%d" % a))["id"]
    assert {n["id"]: n for n in s.tree()["nodes"]}[d]["on_option"] == "no" and s.grown_on(a, "yes") == [b, c]
    t2 = s.try_option(a, "yes")                                                      # the old answer can be tried again, then dropped
    assert s.trial_end(t2["id"], "dropped")["status"] == "dropped" and s.get(a)["answer"] == "no"
    assert [x["id"] for x in s.trials("open")] == [] and len(s.trials("all", did=a)) == 2
    client = client_for(s)
    j = client.post("/api/decisions/%d/try" % a, json={"option": "yes", "note": "อีกครั้ง"}).get_json()
    assert j["ok"] and j["item"]["review"] == [d]
    tid = j["item"]["id"]
    assert client.post("/api/decisions/trials/%d/switch" % tid, json={}).status_code == 400          # not measured
    assert client.post("/api/decisions/trials/%d/measured" % tid, json={"tests": "t", "impact": "i"}).get_json()["item"]["status"] == "measured"
    assert client.post("/api/decisions/trials/%d/switch" % tid, json={"note": "ทั้งสอง"}).get_json()["item"]["status"] == "switch"
    assert client.get("/api/decisions/trials?status=all").get_json()["items"][-1]["status"] == "switch" and s.get(a)["answer"] == "no"


def test_done_records_when_with_tests_and_effect_and_search_finds_it():
    """2026-10-19: three times (asked, answered, finished), the test results and the effect after the work; search across fields."""
    s = DecisionStore(":memory:")
    a = s.add(Q)
    with pytest.raises(DecisionError):
        s.done(a["id"], "ok", "x")                                         # not decided yet
    s.answer(a["id"], "yes", "ไปเลย")
    with pytest.raises(DecisionError):
        s.done(a["id"], "", "x")                                           # the test results are required
    d = s.done(a["id"], "pytest 41 ผ่าน, RTL 27/27", "ร่างหาอาหารข้ามห้องได้")
    assert d["status"] == "done" and d["created"] <= d["decided_at"] <= d["done_at"] and d["result_tests"].startswith("pytest")
    assert [x["id"] for x in s.list("all", q="RTL 27")] == [a["id"]] and s.list("all", q="ไม่มีคำนี้") == []
    assert [x["id"] for x in s.list("closed", q="ข้ามห้อง")] == [a["id"]] and s.counts()["done"] == 1
    assert s.get(a["id"])["events"][-1]["action"] == "done"
    client = client_for(s)
    assert client.get("/api/decisions?status=all&q=%E0%B8%82%E0%B9%89%E0%B8%B2%E0%B8%A1").get_json()["items"][0]["id"] == a["id"]   # "ข้าม"
    b = s.add(dict(Q, key="other"))
    s.answer(b["id"], "no")
    j = client.post("/api/decisions/%d/done" % b["id"], json={"tests": "t", "impact": "i"}).get_json()
    assert j["ok"] and j["item"]["done_at"] and j["counts"]["done"] == 2


def test_an_old_database_gets_the_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, kind TEXT NOT NULL, source TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL, updated REAL NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
        title_th TEXT NOT NULL, title_en TEXT NOT NULL, question_th TEXT NOT NULL, question_en TEXT NOT NULL,
        context_th TEXT NOT NULL DEFAULT '', context_en TEXT NOT NULL DEFAULT '', options TEXT NOT NULL DEFAULT '[]',
        refs TEXT NOT NULL DEFAULT '[]', answer TEXT, note TEXT, decided_at REAL, decided_by TEXT);""")
    db.commit(); db.close()
    s = DecisionStore(path)
    did = s.add(Q)["id"]; s.answer(did, "yes")
    assert s.done(did, "t", "i")["status"] == "done"


def test_a_decision_keeps_its_branch_and_commits():
    """2026-11-01: "keep the code as branches too" — a question (and a trial) keeps which commits are its work."""
    s = DecisionStore(":memory:")
    a = s.add(Q)["id"]
    s.answer(a, "yes")
    with pytest.raises(DecisionError):
        s.record_code(a, [{"sha": "not-a-sha"}])
    with pytest.raises(DecisionError):
        s.record_code(a, [], branch="main")                                          # only dec/… or alt/…
    with pytest.raises(DecisionError):
        s.record_code(a, [{"sha": "abc1234"}], how="guessed")
    before = s.get(a)["updated"]
    s.record_code(a, [], branch="dec/%d-yes" % a, why="branch started")
    s.record_code(a, [{"sha": "abc1234def", "subject": "work"}, {"sha": "abc1234", "subject": "the same commit"}], how="backfilled", why="named #1")
    item = s.get(a)
    assert item["branch"] == "dec/%d-yes" % a and [c["sha"] for c in item["commits"]] == ["abc1234def"] and item["commits"][0]["how"] == "backfilled"
    assert item["updated"] == before and item["events"][-1]["action"] == "code"
    t = s.try_option(a, "no")
    s.record_code(a, [{"sha": "fff0000"}], how="merged", trial=t["id"])
    assert s.trial(t["id"])["commits"][0]["sha"] == "fff0000" and len(s.get(a)["commits"]) == 1
    b = s.add(dict(Q, key="b"))["id"]
    with pytest.raises(DecisionError):
        s.record_code(b, [], trial=t["id"])                                          # that trial is a's
    node = {n["id"]: n for n in s.tree()["nodes"]}[a]
    assert node["code"] == 1 and node["branch"] == "dec/%d-yes" % a

