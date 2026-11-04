"""Story mode and the projects overlay on the page (2026-11-04), in the node harness of test_page.py: the scenes index (where each
choice leads, dismissed scenes left out), a scene's side panel (each choice → its scene or + write it), the reader (choices as
buttons, unwritten ones disabled, endings), the story's home, a new scene posted after one choice (follows + follows_option), the
mode applied to the page (no board, scene words), and the projects overlay drawn from GET /api/projects on the server's machine and
from another."""
import json

import pytest

from test_page import NODE, j, run, text

pytestmark = pytest.mark.skipif(NODE is None, reason="no node to run the page's script")


def scene(id, title, text_, options, parent=None, on=None, status="open"):
    return {"id": id, "kind": "decision", "status": status, "title_th": title, "title_en": title + " (en)", "question_th": text_, "question_en": text_ + " (en)",
            "options": [{"key": k, "th": th, "en": en} for k, th, en in options], "trials": [], "answer": None, "parent": parent, "on_option": on, "also": []}


TREE = {"ok": True, "nodes": [
    scene(1, "Two doors", "A corridor ends at two doors.", [("left", "ซ้าย", "Open the left door"), ("right", "ขวา", "Open the right door")]),
    scene(2, "The library", "Shelves to the ceiling.", [("read", "อ่าน", "Take a book"), ("back", "กลับ", "Go back")], parent=1, on="left"),
    scene(3, "Asleep", "You fall asleep with the book.", [], parent=2, on="read"),
    scene(4, "Dropped", "x", [], parent=1, on="right", status="dismissed")],
    "roots": [1], "counts": {"open": 3, "decided": 0, "done": 0, "dismissed": 1}}
STORY = "state.tree = %s; state.project = {name: 'Tale', mode: 'story', root: '/r'}; state.agents = {default: null, items: []};" % j(TREE)


def test_the_scenes_index_says_where_each_choice_leads():
    (ix,) = run(["(function () { var i = storyIndex(); return {start: i.start, roots: i.roots, leads: i.leads, ids: i.nodes.map(function (n) { return n.id; })}; })()"], setup=STORY)
    assert ix == {"start": 1, "roots": [1], "leads": {"1:left": 2, "2:read": 3}, "ids": [1, 2, 3]}          # the dismissed scene leads nowhere and is not led to


def test_a_scenes_panel_shows_its_choices_and_what_follows_each():
    h, end = run(["sceneHtml(storyIndex().by[1])", "sceneHtml(storyIndex().by[3])"], setup=STORY)
    t = text(h)
    assert "#1 Two doors (en)" in t and "an opening scene" in t and "A corridor ends at two doors. (en)" in t
    assert "Open the left door" in t and 'data-id="2"' in h and "#2 The library (en)" in t                   # the choice leads to #2
    assert "Open the right door" in t and "no scene yet" in t and 'data-after="right"' in h                 # the dropped scene does not count
    assert 'data-story="read"' in h and 'data-story="edit"' in h and 'data-act="dismiss"' in h
    assert "An ending" in text(end) and "after" in text(end) and 'data-id="2"' in end and "choice read" in text(end)


def test_the_reader_offers_the_choices_as_buttons_and_marks_the_unwritten_and_the_end():
    first, last = run(["readerHtml(1, [1])", "readerHtml(3, [1, 2, 3])"], setup=STORY)
    assert 'data-go="2"' in first and "Open the left door" in text(first)
    assert "disabled" in first and "not written yet" in text(first) and 'data-write="right"' in first
    assert 'data-rd="back"' not in first and 'data-rd="restart"' in first
    assert "The end" in text(last) and "the way so far: #1 → #2 → #3" in text(last) and 'data-rd="back"' in last and "data-go" not in last


def test_the_story_home_counts_and_lists_the_choices_waiting_for_a_scene():
    (h,) = run(["homeHtml()"], setup=STORY)
    t = text(h)
    assert "Tale" in t and "scenes 3 · endings 1 · choices without a scene 2" in t
    assert "#1 Two doors (en) → Open the right door" in t and "#2 The library (en) → Go back" in t
    assert 'data-home="read"' in h and 'data-home="book"' in h and 'data-home="projects"' in h and 'data-home="new"' in h
    (empty,) = run(["homeHtml()"], setup=STORY + " state.tree = {nodes: [], roots: [], counts: {}};")
    assert "No scene yet" in text(empty) and 'data-home="read" disabled' in empty


def test_a_new_scene_after_a_choice_is_posted_with_the_link():
    setup = STORY + " setAfter({id: 1, key: 'right', label: 'Open the right door'});" + \
        " ['nTitleTh', 'nTitleEn', 'nQTh', 'nQEn', 'nOpts'].forEach(function (id) { document.getElementById(id).value = ''; });" + \
        " document.getElementById('nTitleTh').value = 'สวน'; document.getElementById('nQTh').value = 'ดอกไม้บาน'; document.getElementById('nOpts').value = 'smell: Smell | ดม';"
    (after,), fetched = run(["(document.getElementById('nSend').onclick(), state.newAfter && state.newAfter.key)"], setup=setup,
                            after=["fetched.filter(function (f) { return f.method === 'POST'; }).map(function (f) { return f.url + ' ' + f.body; })"])
    assert after == "right"                                                                     # cleared only once the server answered
    body = json.loads(fetched[0][0].split(" ", 1)[1])
    assert fetched[0][0].startswith("/api/decisions ") and body["follows"] == 1 and body["follows_option"] == "right" and body["follows_how"] == "sequence"
    assert body["title_th"] == "สวน" and body["options"] == [{"key": "smell", "th": "ดม", "en": "Smell", "recommended": False}]
    (cleared,) = run(["(setAfter({id: 1, key: 'left', label: 'x'}), setAfter(null), state.newAfter)"], setup=STORY)
    assert cleared is None


def test_the_mode_changes_the_pages_words_and_hides_the_board():
    vals = run(["(applyMode(), [document.getElementById('vBoard').hidden, document.getElementById('chkWrap').hidden, document.getElementById('newQ').innerHTML, document.getElementById('nOpts').placeholder])"],
               setup=STORY + " state.mode = 'board';")
    assert vals[0][0] is True and vals[0][1] is True and "New scene" in vals[0][2] and "an ending" in vals[0][3]
    (mode,) = run(["(applyMode(), state.mode)"], setup=STORY + " state.mode = 'board';")
    assert mode == "tree"
    back = run(["(applyMode(), [document.getElementById('vBoard').hidden, document.getElementById('newQ').innerHTML])"],
               setup="state.project = {name: 'd', mode: 'decisions'};")
    assert back[0][0] is False and "New question" in back[0][1]
    assert run(["shortcutAction('p', false, false, false)"]) == ["projects"]


LISTING = {"ok": True, "dir": "/home/u/dtree-projects",
           "current": {"path": "/r/tale", "name": "Tale", "mode": "story", "counts": {"open": 3, "decided": 0, "done": 0, "dismissed": 1}},
           "items": [{"path": "/r/tale", "name": "Tale", "mode": "story", "exists": True, "current": True},
                     {"path": "/r/work", "name": "Work", "mode": "decisions", "exists": True, "current": False},
                     {"path": "/r/gone", "name": "Gone", "mode": "story", "exists": False, "current": False}]}


def test_the_projects_overlay_lists_opens_makes_edits_imports_exports_and_saves_as():
    local, remote = run(["projectsHtml(L, true)", "projectsHtml(L, false)"], setup="var L = %s;" % j(LISTING))
    t = text(local)
    assert "Tale · story · /r/tale · all 4" in t and "Known projects" in t and "open now" in t
    assert 'data-popen="/r/work">' in local and 'data-popen="/r/gone" disabled' in local and "missing" in t
    assert 'id="pNew"' in local and '<option value="story" selected>' in local and 'id="pEditName" value="Tale"' in local
    assert 'id="pExport"' in local and 'id="pBook"' in local and 'id="pFile"' in local and 'id="pSave"' in local and "/home/u/dtree-projects" in t
    r = text(remote)
    assert "server's own machine only" in r and 'id="pNew" disabled' in remote and 'id="pSave" disabled' in remote and 'id="pExport">' in remote
    assert 'data-popen="/r/work" disabled' in remote
    (dec,) = run(["projectsHtml(L, true)"], setup="var L = %s;" % j(dict(LISTING, current=dict(LISTING["current"], mode="decisions"))))
    assert 'id="pBook"' not in dec and '<option value="decisions" selected>' in dec
