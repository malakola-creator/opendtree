"""The page's conveniences of 2026-11-04, in the node harness of test_page.py: the side panel's home (what waits for the person, the
Setup banner when no AI can run), the status chips that filter the tree and the board, the keys (never while typing or with an overlay
open), and 🧙 Setup's steps drawn from the settings view — the presets to tick, a key box only on the server's own machine (the CLI
line elsewhere), the test lines, the default, the closing commands; and never a key in any of it."""
import json

import pytest

from test_page import NODE, j, run, text

pytestmark = pytest.mark.skipif(NODE is None, reason="no node to run the page's script")


def node(id, status, **kw):
    n = {"id": id, "kind": "decision", "status": status, "title_th": "ข้อ %d" % id, "title_en": "question %d" % id, "question_th": "ถาม", "question_en": "asked",
         "options": [{"key": "a", "th": "ก", "en": "a"}, {"key": "b", "th": "ข", "en": "b"}], "trials": [], "answer": None, "parent": None, "also": []}
    n.update(kw)
    return n


TREE = {"ok": True, "nodes": [
    node(1, "open"),
    node(2, "decided", answer="a", trials=[{"id": 7, "option": "b", "status": "trying", "ai": {"status": "running"}}]),
    node(3, "done", answer="b"),
    node(4, "decided", answer="a", trials=[{"id": 8, "option": "b", "status": "measured"}]),
    node(5, "dismissed"),
    node(6, "decided", answer="b")],
    "roots": [1, 2, 3, 4, 5, 6], "counts": {"open": 1, "decided": 3, "done": 1, "dismissed": 1}}

SECRET = "sk-THE-KEY-THAT-MUST-NEVER-SHOW-0123456789"


def view(local=True, gemini_key=False):
    """A settings view (GET /api/settings) with Claude Code in the file and Gemini, a chat agent that needs a key."""
    key = {"needs": True, "via": "file" if gemini_key else None, "env": "GEMINI_API_KEY", "env_set": False, "file_set": gemini_key,
           "file": "/home/u/.config/dtree/keys/demo-gemini.key" if gemini_key else None}
    return {"ok": True, "file": "/repo/.dtree/config.json", "exists": True, "tracked": True, "local": local, "name": "demo",
            "sections": [{"id": "jobs", "th": "งาน", "en": "Jobs", "fields": [
                {"path": "default_agent", "kind": "enum", "enum": ["claude", "gemini"], "default": "claude", "own": "gemini", "value": "gemini", "set": True}]}],
            "agents": [
                {"name": "claude", "kind": "claude", "label": "Claude Code", "preset": True, "available": True, "reason": "", "fields": [], "key": {"needs": False, "note": "claude → /login"}},
                {"name": "gemini", "kind": "chat", "label": "Gemini (Google AI)", "preset": True, "available": gemini_key, "reason": "" if gemini_key else "no key", "fields": [], "key": key}],
            "presets": [{"name": "claude", "kind": "claude", "label": "Claude Code", "in_config": True},
                        {"name": "codex", "kind": "codex", "label": "Codex", "in_config": False},
                        {"name": "gemini", "kind": "chat", "label": "Gemini (Google AI)", "in_config": True},
                        {"name": "muse", "kind": "chat", "label": "Muse Spark (Meta)", "in_config": False}],
            "kinds": {"claude": ["label"], "codex": ["label"], "chat": ["label", "model"]}}


# ------------------------------------------------------------------------- the home: what waits for the person
def test_home_lists_what_waits_by_group_and_offers_setup_when_no_ai_runs():
    setup = "state.tree = %s; state.agents = {default: null, items: []};" % j(TREE)
    (h,) = run(["homeHtml()"], setup=setup)
    t = text(h)
    assert "Waiting for you" in t
    assert "Waiting for your answer 1" in t and "#1 question 1" in t
    assert "Built and measured: choose 1" in t and "#4" in t and "⚖ 1" in t                   # a measured trial: the person chooses
    assert "An AI is building 1" in t and "#2" in t and "🤖 1" in t
    assert "Decided, to build 1" in t and "#6" in t and "✓ b" in t                               # the answer under it
    assert "#3" not in t and "#5" not in t                                                        # done and dismissed wait for nobody
    assert "No AI can run from here yet" in t and 'data-home="setup"' in h                      # the banner, with its button
    assert 'data-jump="1"' in h and "Click a node for its details" in t and "Keys:" in t
    # an AI that can run: no banner; the Setup button stays among the actions
    (h2,) = run(["homeHtml()"], setup=setup + " state.agents.items = [{name: 'claude', available: true}];")
    assert "No AI can run" not in text(h2) and h2.count('data-home="setup"') == 1


def test_home_in_thai_and_while_loading():
    (h,) = run(["homeHtml()"], lang="th", setup="state.tree = %s; state.agents = {default: null, items: [{available: true}]};" % j(TREE))
    assert "สิ่งที่รอคุณ" in text(h) and "รอคุณตัดสิน 1" in text(h) and "ข้อ 1" in text(h)
    (loading,) = run(["homeHtml()"], setup="state.tree = null;")
    assert text(loading) == "loading…"


def test_home_shows_six_per_group_and_says_how_many_more():
    many = dict(TREE, nodes=[node(i, "open") for i in range(1, 10)], roots=list(range(1, 10)), counts={"open": 9})
    (h,) = run(["homeHtml()"], setup="state.tree = %s; state.agents = {default: null, items: [{available: true}]};" % j(many))
    assert h.count('data-jump=') == 6 and "and 3 more" in text(h) and 'data-jump="9"' in h and 'data-jump="3"' not in h   # the latest first


# ------------------------------------------------------------------------- the status chips
def test_the_counts_are_chips_and_a_pressed_one_filters_questions_and_their_options():
    setup = "state.tree = %s;" % j(TREE)
    h, = run(["(counts(), document.getElementById('counts').innerHTML)"], setup=setup)
    assert 'data-flt="open"' in h and 'data-flt=""' in h and 'aria-pressed="true"' in h and "open 1" in text(h) and "roots 6" in text(h)
    vals = run(["matches({data: {status: 'open', title_en: 'x'}}, '')",
                "matches({type: 'q', data: {status: 'done', title_en: 'x'}}, '')",
                "matches({type: 'o', q: {status: 'open'}, data: {th: 'ก', en: 'a'}}, '')",
                "matches({type: 'o', q: {status: 'done'}, data: {th: 'ก', en: 'a'}}, '')",
                "matches({data: {status: 'open', title_en: 'alpha'}}, 'beta')",
                "(state.filter = null, matches({type: 'q', data: {status: 'done', title_en: 'x'}}, ''))"], setup=setup + " state.filter = 'open';")
    assert vals == [True, False, True, False, False, True]
    pressed, = run(["(state.filter = 'done', counts(), document.getElementById('counts').innerHTML)"], setup=setup)
    assert 'data-flt="done" aria-pressed="true"' in pressed and 'data-flt="" aria-pressed="false"' in pressed


# ------------------------------------------------------------------------- the keys
def test_shortcuts_only_outside_boxes_and_overlays():
    vals = run(["shortcutAction('/', false, false, false)", "shortcutAction('n', false, false, false)", "shortcutAction('?', false, false, false)",
                "shortcutAction('w', false, false, false)", "shortcutAction('n', true, false, false)", "shortcutAction('n', false, true, false)",
                "shortcutAction('n', false, false, true)", "shortcutAction('x', false, false, false)", "shortcutAction('Escape', false, false, false)"])
    assert vals == ["search", "new", "help", "setup", None, None, None, None, None]


# ------------------------------------------------------------------------- 🧙 setup
def test_setup_step_one_lists_the_presets_with_those_in_the_file_ticked_and_fixed():
    (h,) = run(["wzHtml('ai', %s, {pick: {muse: true}})" % j(view())])
    t = text(h)
    assert "1. Choose AIs" in t and "Keys" in t and "Test" in t and "Done" in t
    assert 'data-wzpick="claude" checked disabled' in h and "available" in t                      # in the file and running
    assert 'data-wzpick="gemini" checked disabled' in h and "in the file already" in t
    assert 'data-wzpick="codex">' in h and 'data-wzpick="muse" checked>' in h                      # not in the file: codex unticked, muse ticked by the person
    assert "its own login" in t and "needs a key: the next step" in t


def test_setup_keys_step_has_a_box_only_on_the_servers_machine_and_never_a_key():
    local, = run(["wzHtml('keys', %s, {})" % j(view(local=True))])
    assert 'type="password" data-wzkey="gemini"' in local and 'data-wzsetkey="gemini"' in local and "$GEMINI_API_KEY" in text(local)
    assert "logs in by itself: claude" in text(local)
    remote, = run(["wzHtml('keys', %s, {})" % j(view(local=False))])
    assert "data-wzkey" not in remote and "dtree key gemini" in text(remote) and "server's own machine only" in text(remote)
    done, = run(["wzHtml('keys', %s, {})" % j(view(gemini_key=True))])
    assert "data-wzkey" not in done and "key set (file)" in text(done)
    none, = run(["wzHtml('keys', %s, {})" % j(dict(view(), agents=[view()["agents"][0]]))])
    assert "No chat AI" in text(none)
    # the key typed into the box is sent to the agent's own route only, and the box is emptied before anything else happens
    (sent,), after = run(["(state.wz = {view: %s, seq: state.ovlSeq, tested: {}}, elements['box'] = {value: %s}, document.querySelector = function (s) { return s.indexOf('wzkey') >= 0 ? elements['box'] : elements[s] || (elements[s] = {}); }, wzSetKey('gemini'), elements['box'].value)"
                          % (j(view()), json.dumps(SECRET))], after=["fetched.filter(function (f) { return f.method === 'POST'; }).map(function (f) { return f.url + ' ' + f.body; })"])
    assert sent == ""
    assert after == [["/api/settings/agents/gemini/key " + json.dumps({"key": SECRET}, separators=(",", ":"))]]


def test_setup_test_step_shows_each_agents_line_and_the_default():
    (h,) = run(["wzHtml('test', %s, {tested: {gemini: ['✓ available · GET /models 200 · 60 models', 'vgood']}})" % j(view())])
    t = text(h)
    assert 'data-wztestall="1"' in h and 'data-wztest="claude"' in h and 'data-wztest="gemini"' in h
    assert "✓ available · GET /models 200 · 60 models" in t and 'id="wzt-gemini"' in h
    assert "✓ available" in t                                                                      # claude: as the view says, before any test
    assert 'data-wzdefault="1"' in h and '<option value="gemini" selected>' in h                   # the jobs section's default_agent
    untested, = run(["wzHtml('test', %s, {})" % j(view())])
    assert "✗ no key" in text(untested)


def test_setup_done_step_sums_up_and_gives_the_commands():
    (h,) = run(["wzHtml('done', %s, {})" % j(view(gemini_key=True))], setup="state.project = {name: 'demo', root: '/repo'};")
    t = text(h)
    assert "2 can run: Claude Code, Gemini (Google AI)" in t
    assert "claude mcp add dtree -- dtree --project /repo mcp" in t and "dtree ai TRIAL --agent gemini" in t
    assert 'data-wzclose="new"' in h and 'data-wzclose="1"' in h
    v = view()
    for a in v["agents"]:
        a["available"] = False
    (none,) = run(["wzHtml('done', %s, {})" % j(v)])
    assert "No AI can run yet" in text(none)


def test_setup_never_shows_a_key_and_the_page_opens_it_from_the_url():
    v = view(gemini_key=True)
    v["agents"][1]["key"]["secret_should_not_be_here"] = SECRET                                    # even a view that carried one would not be drawn
    pages = run(["wzHtml('ai', V, {})", "wzHtml('keys', V, {})", "wzHtml('test', V, {})", "wzHtml('done', V, {})"], setup="var V = %s;" % j(v))
    assert all(SECRET not in p for p in pages)
    (opened,), after = run(["(location.search = '?setup=1', state.wz)"], after=["state.wz && state.wz.step", "fetched.some(function (f) { return f.url === '/api/settings'; })"])
    # the start-up Promise chain has no answers here (fetch never resolves), so showSetup is not reached: the flag is read at that point
    assert opened is None and after[0] is None and after[1] is False
    (step,), after2 = run(["(showSetup(), state.wz.step)"], after=["fetched.filter(function (f) { return f.url === '/api/settings'; }).length"])
    assert step == "ai" and after2 == [1]


def test_setup_next_from_step_one_adds_the_ticked_presets_one_request_each():
    setup = "state.wz = {step: 'ai', view: %s, pick: {muse: true, codex: true, claude: true}, tested: {}, seq: state.ovlSeq};" % j(view())
    vals, after = run(["(wzNav('next'), state.wz.step)"], setup=setup,
                      answers={r"/api/settings$": dict(view(), changed=["agents.muse"])},
                      after=["fetched.filter(function (f) { return f.method === 'POST'; }).map(function (f) { return f.body; })", "state.wz.step"])
    assert vals == ["ai"]                                                                          # not before the adds answer
    compact = lambda d: json.dumps(d, separators=(",", ":"))
    assert sorted(after[0]) == sorted([compact({"add_agent": {"name": "codex", "from": "codex"}}), compact({"add_agent": {"name": "muse", "from": "muse"}})])
    assert after[1] == "keys"
