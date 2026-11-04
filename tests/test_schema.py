"""docs/schema.md and src/dtree/schema/*.json against the code: every column, every event, every input field and config key is
documented, and the limits in the schemas are the ones the store enforces (the user asked for the schema on 2026-11-01)."""
import json
import re
from pathlib import Path

import pytest

from dtree import agents, project
from dtree.agents import chat
from dtree.store import DecisionError, DecisionStore

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "schema.md").read_text(encoding="utf-8")
SCHEMA = ROOT / "src" / "dtree" / "schema"


def _load(name):
    return json.loads((SCHEMA / name).read_text(encoding="utf-8"))


def _section(table):
    m = re.search(r"### `%s`.*?(?=\n### |\n## )" % table, DOC, re.S)
    assert m, "docs/schema.md has no section for %s" % table
    return m.group(0)


@pytest.mark.parametrize("table", ["decisions", "events", "trials", "jobs", "checks"])
def test_every_column_is_in_the_schema_page(table):
    s = DecisionStore(":memory:")
    cols = [r["name"] for r in s._db.execute("PRAGMA table_info(%s)" % table)]
    text = _section(table)
    missing = [c for c in cols if "`%s`" % c not in text]
    assert not missing, "docs/schema.md §%s misses %s" % (table, missing)


def test_every_event_the_store_writes_is_in_the_schema_page():
    src = (ROOT / "src" / "dtree" / "store.py").read_text(encoding="utf-8")
    actions = set(re.findall(r'_event\([^,]+, [^,]+, "([a-z_]+)"', src))
    actions = {a for a in actions if not a.endswith("_")}                       # "ai_" + status: the full names are added below
    actions |= {"trial_" + o for o in ("adopted", "switch", "dropped")} | {"ai_" + o for o in ("measured", "failed", "stopped")} | {"dismissed"}
    from dtree.store import EXTRA_EVENTS
    actions |= set(EXTRA_EVENTS)                                                 # written by verify / merge / spend through note_event
    table = re.search(r"### 3\.3 events.*?(?=\n### )", DOC, re.S).group(0)
    missing = sorted(a for a in actions if "`%s`" % a not in table)
    assert not missing, "docs/schema.md §3.3 misses %s" % missing


def test_the_question_schema_is_what_the_store_takes():
    q = _load("question.schema.json")
    props, opt = q["properties"], q["$defs"]["option"]["properties"]
    full = {"kind": "decision", "source": "agent", "key": "k" * props["key"]["maxLength"], "title_th": "ท" * props["title_th"]["maxLength"],
            "title_en": "t", "question_th": "q", "question_en": "q", "context_th": "c", "context_en": "c", "refs": ["docs/x.md"],
            "options": [{"key": "a" * opt["key"]["maxLength"], "th": "ก", "en": "a", "desc_th": "d", "desc_en": "d", "recommended": True,
                         "pros": ["p"] * opt["pros"]["maxItems"], "cons": ["c"], "texts": {"ja": "はい"}}] + [{"th": "o%d" % i} for i in range(q["properties"]["options"]["maxItems"] - 1)]}
    # every field of the schema, nothing else (title / question / context as {lang: words} and texts: the languages beyond en and th)
    assert set(full) | {"follows", "follows_how", "follows_why", "follows_option", "title", "question", "context", "texts"} == set(props)
    s = DecisionStore(":memory:")
    item = s.add(full)
    stored = item["options"][0]
    assert set(stored) == set(opt) and stored["pros"] == ["p"] * opt["pros"]["maxItems"]
    s.add(dict(full, key="k2", follows=item["id"], follows_how="sequence", follows_why="next"))
    too_long = [dict(full, key="k3", title_th="ท" * (props["title_th"]["maxLength"] + 1)),
                dict(full, key="k4", options=full["options"] + [{"th": "x"}]),
                dict(full, key="k5", options=[dict(full["options"][0], pros=["p"] * (opt["pros"]["maxItems"] + 1))]),
                dict(full, key="k6", options=[dict(full["options"][0], key="a" * (opt["key"]["maxLength"] + 1))]),
                dict(full, key="k7", kind="other"), dict(full, key="k8", source="nobody")]
    for bad in too_long:
        with pytest.raises(DecisionError):
            s.add(bad)
    assert props["kind"]["enum"] == ["decision", "problem"] and set(props["source"]["enum"]) == {"system", "claude", "user", "agent"}


def test_the_config_schema_names_every_setting():
    c = _load("config.schema.json")
    assert set(project.DEFAULTS) <= set(c["properties"]) | {"agents"}
    assert set(project.DEFAULTS["chat"]) | {"deny", "context_chars", "result_chars"} <= set(c["$defs"]["chat"]["properties"])
    agent_keys = set().union(*[set(a) for a in project.AGENTS.values()])
    assert agent_keys <= set(c["$defs"]["agent"]["properties"])
    assert set(c["$defs"]["agent"]["properties"]["kind"]["enum"]) == {"claude", "codex", "chat"}


def test_every_agent_is_held_to_the_same_result():
    r = _load("result.schema.json")
    assert agents.RESULT_SCHEMA == {k: v for k, v in r.items() if k not in agents._DOC_KEYS}
    finish = [t for t in chat._tools() if t["function"]["name"] == "finish"][0]["function"]["parameters"]
    assert finish["required"] == r["required"] and set(finish["properties"]) == set(r["properties"])
