"""Story mode and several projects (2026-11-04): a scene hangs on the choice it follows (store.follow's option), a question's wording
is edited afterwards (an ``edited`` event), a store goes out and in whole (dump / load, ids kept); projects are registered, made,
exported, imported, saved as and renamed (projects.py) from this machine only over the API, the server switches to the one made or
opened, and the story is read as scenes and written as a Markdown book (story.py, ``dtree book``)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from dtree import cli, projects, server, story
from dtree.project import Project
from dtree.store import DecisionError, DecisionStore

SCENE1 = {"title_th": "ประตูสองบาน", "title_en": "Two doors", "question_th": "ทางเดินแยกเป็นสองประตู", "question_en": "The corridor ends at two doors.",
          "options": [{"key": "left", "th": "เปิดประตูซ้าย", "en": "Open the left door"}, {"key": "right", "th": "เปิดประตูขวา", "en": "Open the right door"}]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The registry and the projects directory under the test's own folder."""
    monkeypatch.setenv("DTREE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DTREE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.delenv("DTREE_DB", raising=False)
    return tmp_path


# ------------------------------------------------------------------------- the store
def test_a_scene_hangs_on_the_choice_it_follows_and_the_tree_says_so():
    s = DecisionStore(":memory:")
    a = s.add(SCENE1)["id"]
    left = s.add({"title_th": "ห้องสมุด", "question_th": "ชั้นหนังสือสูงจรดเพดาน", "options": [{"key": "read", "th": "อ่าน"}],
                  "follows": a, "follows_how": "sequence", "follows_why": "ทางเลือก left", "follows_option": "left"})["id"]
    right = s.add({"title_th": "สวน", "question_th": "จบ", "options": [], "follows": a, "follows_how": "sequence", "follows_why": "ทางเลือก right"})
    s.follow(right["id"], a, "sequence", "ทางเลือก right", option="right")
    nodes = {n["id"]: n for n in s.tree()["nodes"]}
    assert nodes[left]["parent"] == a and nodes[left]["on_option"] == "left"
    assert nodes[right["id"]]["parent"] == a and nodes[right["id"]]["on_option"] == "right"
    assert s.get(right["id"])["events"][-1]["detail"] == {"on": a, "how": "sequence", "why": "ทางเลือก right", "option": "right"}
    with pytest.raises(DecisionError):                                               # not one of #a's choices
        s.add({"title_th": "x", "question_th": "x", "follows": a, "follows_why": "w", "follows_option": "middle"})
    assert s.follow(left, None)["follows"] is None and s.get(left)["follows_option"] is None     # the link taken away takes the option with it
    # without an option a later question hangs on the answer in force when it was raised, as before
    s.answer(a, "left")
    later = s.add({"title_th": "y", "question_th": "y", "follows": a, "follows_why": "after the answer"})["id"]
    assert {n["id"]: n for n in s.tree()["nodes"]}[later]["on_option"] == "left"


def test_a_question_is_edited_afterwards_and_the_history_says_which_fields():
    s = DecisionStore(":memory:")
    a = s.add(SCENE1)["id"]
    out = s.edit(a, {"title_en": "Two doors, one choice", "question_th": "ทางเดินแยกเป็นสองประตู ลมเย็นพัดมาจากซ้าย",
                     "options": [{"key": "left", "th": "ซ้าย", "en": "Left"}, {"key": "right", "th": "ขวา", "en": "Right"}, {"key": "wait", "th": "รอ"}]}, by="user")
    assert out["title_en"] == "Two doors, one choice" and out["title_th"] == "ประตูสองบาน" and [o["key"] for o in out["options"]] == ["left", "right", "wait"]
    assert out["events"][-1]["action"] == "edited" and sorted(out["events"][-1]["detail"]["fields"]) == ["options", "question_th", "title_en"]
    same = s.edit(a, {"title_th": "ประตูสองบาน"})                                      # nothing changed: no event
    assert same["events"][-1]["action"] == "edited" and len(same["events"]) == len(out["events"])
    for bad in ({"answer": "left"}, {"status": "done"}, {}, {"title_th": "", "title_en": ""}, {"options": [{"key": "a", "th": "x"}] * 9}):
        with pytest.raises(DecisionError):
            s.edit(a, bad)
    with pytest.raises(KeyError):
        s.edit(999, {"title_th": "x"})
    only_en = s.edit(a, {"title_th": "", "question_th": ""})                           # one language emptied: the other's words stand in
    assert only_en["title_th"] == only_en["title_en"] == "Two doors, one choice" and only_en["question_th"] == only_en["question_en"]


def test_a_question_in_english_alone_is_enough():
    """English first (2026-11-04): a title and a question in one language; the other takes the same words."""
    s = DecisionStore(":memory:")
    q = s.add({"title_en": "Only English", "question_en": "Why not?", "options": [{"key": "a", "en": "Yes", "th": ""}]})
    assert q["title_th"] == q["title_en"] == "Only English" and q["question_th"] == "Why not?"
    th = s.add({"title_th": "ไทยอย่างเดียว", "question_th": "ทำไม"})
    assert th["title_en"] == "ไทยอย่างเดียว"
    for bad in ({"question_en": "no title"}, {"title_en": "no question"}, {"title_en": "", "title_th": "", "question_en": "x"}):
        with pytest.raises(DecisionError):
            s.add(bad)


def test_a_store_goes_out_and_in_whole_with_its_ids():
    s = DecisionStore(":memory:")
    a = s.add(SCENE1)["id"]
    b = s.add(dict(SCENE1, title_th="ต่อ", follows=a, follows_why="w", follows_option="left"))["id"]
    s.answer(a, "left", "note")
    t = s.try_option(a, "right")["id"]
    s.comment(b, "a note")
    d = s.dump()
    assert [r["id"] for r in d["decisions"]] == [a, b] and d["decisions"][0]["options"][0]["key"] == "left" and d["decisions"][1]["follows_option"] == "left"
    assert {e["action"] for e in d["events"]} >= {"raised", "answered", "trial", "commented", "followed"} and d["trials"][0]["id"] == t
    assert "jobs" not in d and "checks" not in d
    s2 = DecisionStore(":memory:")
    assert s2.load(json.loads(json.dumps(d))) == {"decisions": 2, "events": len(d["events"]), "trials": 1}
    assert s2.get(b)["title_th"] == "ต่อ" and s2.get(a)["answer"] == "left" and s2.trials("all")[0]["option"] == "right"
    assert {n["id"]: n for n in s2.tree()["nodes"]}[b]["on_option"] == "left"
    with pytest.raises(DecisionError):                                               # never over what is there
        s2.load(d)
    with pytest.raises(DecisionError):
        DecisionStore(":memory:").load({"decisions": "no"})
    d["decisions"][0]["unknown_column"] = 1                                           # a column this version does not have: dropped
    assert DecisionStore(":memory:").load(d)["decisions"] == 2


# ------------------------------------------------------------------------- projects
def test_projects_are_made_registered_exported_imported_saved_as_and_renamed(home):
    p = projects.create("นิทานผจญภัย", "story")
    assert p.root.parent == home / "projects" and p.root.name == "project" and projects.mode_of(p) == "story" and (p.root / ".git").exists()
    p2 = projects.create("Castle quest", "story")
    assert p2.root.name == "castle-quest"
    p3 = projects.create("Castle quest", "decisions")
    assert p3.root.name == "castle-quest-2" and projects.mode_of(p3) == "decisions"
    reg = projects.listing(p3)
    assert [it["path"] for it in reg["items"]][0] == str(p3.root) and reg["current"]["name"] == "Castle quest" and reg["dir"] == str(home / "projects")
    assert {it["name"] for it in reg["items"]} == {"นิทานผจญภัย", "Castle quest"} and all(it["exists"] for it in reg["items"])
    with pytest.raises(DecisionError):
        projects.create("", "story")
    with pytest.raises(DecisionError):
        projects.create("x", "poem")
    # export / import: the portable config and the store, ids kept; never the agents, server or paths
    a = p2.store.add(SCENE1)["id"]
    p2.store.add(dict(SCENE1, title_th="ซ้าย", follows=a, follows_why="w", follows_option="left"))
    projects.edit(p2, {"base": "trunk"})
    bundle = projects.export_bundle(p2)
    assert bundle["dtree_export"] == 1 and bundle["mode"] == "story" and bundle["config"]["base"] == "trunk" and "agents" not in bundle["config"]
    assert len(bundle["store"]["decisions"]) == 2
    text = json.dumps(bundle, ensure_ascii=False)
    assert "key_file" not in text and str(home) not in text
    q = projects.import_bundle(json.loads(text), "Castle quest (copy)")
    assert q.root.name == "castle-quest-copy" and projects.mode_of(q) == "story" and q.base == "trunk" and q.config["name"] == "Castle quest (copy)"
    assert {n["id"]: n for n in q.store.tree()["nodes"]}[2]["on_option"] == "left"
    with pytest.raises(DecisionError):
        projects.import_bundle({"nope": 1})
    r = projects.save_as(p2, "Castle quest v2")
    assert r.root != p2.root and r.store.counts()["open"] == 2 and r.config["name"] == "Castle quest v2"
    # edit: name and mode, the file's other keys kept
    before = json.loads((r.root / ".dtree" / "config.json").read_text(encoding="utf-8"))
    out = projects.edit(r, {"name": "Castle quest final", "mode": "decisions"})
    after = json.loads((r.root / ".dtree" / "config.json").read_text(encoding="utf-8"))
    assert out["name"] == "Castle quest final" and r.config["name"] == "Castle quest final" and projects.mode_of(r) == "decisions"
    assert {k: v for k, v in after.items() if k not in ("name", "mode")} == {k: v for k, v in before.items() if k not in ("name", "mode")}
    for bad in ({"db": "x"}, {"mode": "poem"}, {"name": ""}, {}):
        with pytest.raises(DecisionError):
            projects.edit(r, bad)
    assert [it["name"] for it in projects.listing(r)["items"]][0] == "Castle quest final"
    assert projects.forget(p3.root) and not projects.forget(p3.root)
    assert str(p3.root) not in {it["path"] for it in projects.listing(r)["items"] if not it["current"]}
    with pytest.raises(DecisionError):
        projects.open_project(home / "nowhere")
    assert projects.open_project(p3.root).root == p3.root


def test_the_api_makes_opens_and_switches_projects_from_this_machine_only(home):
    p = projects.create("Base", "decisions")
    switched = []
    api = server.Api(p, on_switch=switched.append)
    st, out = api.handle("POST", "/api/projects", {}, {"name": "Tale", "mode": "story"}, local=False, port=5077)
    assert st == 403 and out["code"] == "local_only"
    st, out = api.handle("POST", "/api/projects", {}, {"name": "Tale", "mode": "story"}, local=True)
    assert st == 200 and out["switched"] and out["project"]["mode"] == "story" and switched[0].config["name"] == "Tale"
    st, lst = api.handle("GET", "/api/projects", {}, {}, local=False)
    assert st == 200 and lst["current"]["name"] == "Base" and {it["name"] for it in lst["items"]} == {"Base", "Tale"}
    st, prj = api.handle("GET", "/api/project", {}, {}, local=False)
    assert prj["mode"] == "decisions"
    st, exp = api.handle("GET", "/api/projects/export", {}, {}, local=False)
    assert st == 200 and exp["bundle"]["name"] == "Base"
    st, imp = api.handle("POST", "/api/projects/import", {}, {"bundle": exp["bundle"], "name": "Base copy"}, local=True)
    assert st == 200 and imp["project"]["name"] == "Base copy" and switched[-1].config["name"] == "Base copy"
    st, sa = api.handle("POST", "/api/projects/save-as", {}, {"name": "Base again"}, local=True)
    assert st == 200 and sa["project"]["name"] == "Base again"
    st, ed = api.handle("POST", "/api/projects/edit", {}, {"name": "Base renamed"}, local=True)
    assert st == 200 and ed["project"]["name"] == "Base renamed" and p.config["name"] == "Base renamed"
    st, op = api.handle("POST", "/api/projects/open", {}, {"path": str(switched[0].root)}, local=True)
    assert st == 200 and op["project"]["name"] == "Tale"
    st, bad = api.handle("POST", "/api/projects/open", {}, {"path": str(home / "none")}, local=True)
    assert st == 400 and "no dtree project" in bad["error"]
    no_switch = server.Api(p)                                                        # the API alone: made, not switched
    st, out = no_switch.handle("POST", "/api/projects", {}, {"name": "Side", "mode": "story"}, local=True)
    assert st == 200 and out["switched"] is False


def test_the_server_switches_in_place_over_http(home):
    p = projects.create("First", "decisions")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(p))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    def call(path, body=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as exc:
            return dict(json.load(exc), status=exc.code)
    try:
        assert call("/api/project")["name"] == "First"
        made = call("/api/projects", {"name": "Second", "mode": "story"})
        assert made["ok"] and made["switched"]
        assert call("/api/project")["name"] == "Second" and call("/api/project")["mode"] == "story"
        sc = call("/api/decisions", SCENE1)
        assert sc["ok"] and call("/api/story")["scenes"][0]["choices"][0]["to"] is None
        assert call("/api/projects")["current"]["name"] == "Second" and call("/api/projects")["current"]["counts"]["open"] == 1
        assert call("/api/projects/open", {"path": str(p.root)})["project"]["name"] == "First"
        assert call("/api/decisions/tree")["nodes"] == []                            # the first project's own store again
        assert (p.root / ".dtree" / "write_token").exists()
    finally:
        httpd.shutdown()


# ------------------------------------------------------------------------- the story
def test_the_story_is_read_as_scenes_and_written_as_a_book(home, capsys):
    p = projects.create("Tale", "story")
    s = p.store
    a = s.add(SCENE1)["id"]
    lib = s.add({"title_th": "ห้องสมุด", "title_en": "The library", "question_th": "ชั้นหนังสือสูงจรดเพดาน", "question_en": "Shelves up to the ceiling.",
                 "options": [{"key": "read", "th": "หยิบหนังสือ", "en": "Take a book"}, {"key": "back", "th": "กลับ", "en": "Go back"}],
                 "follows": a, "follows_how": "sequence", "follows_why": "left", "follows_option": "left"})["id"]
    end = s.add({"title_th": "จบ", "title_en": "The end", "question_th": "คุณหลับไปกับหนังสือ", "question_en": "You fall asleep with the book.",
                 "options": [], "follows": lib, "follows_how": "sequence", "follows_why": "read", "follows_option": "read"})["id"]
    gone = s.add({"title_th": "ทิ้ง", "question_th": "x", "options": [], "follows": a, "follows_why": "right", "follows_option": "right"})["id"]
    s.dismiss(gone)
    st = story.scenes(p)
    assert st["start"] == a and st["roots"] == [a] and st["endings"] == 1
    by = {sc["id"]: sc for sc in st["scenes"]}
    assert gone not in by and by[a]["choices"] == [{"key": "left", "th": "เปิดประตูซ้าย", "en": "Open the left door", "texts": {}, "to": lib},
                                                   {"key": "right", "th": "เปิดประตูขวา", "en": "Open the right door", "texts": {}, "to": None}]
    assert by[lib]["parent"] == a and by[lib]["on_option"] == "left" and by[end]["ending"] and by[end]["parent"] == lib
    assert st["unwritten"] == [{"scene": a, "key": "right", "th": "เปิดประตูขวา", "en": "Open the right door"}, {"scene": lib, "key": "back", "th": "กลับ", "en": "Go back"}]
    md = story.markdown(p)
    assert md.startswith("# Tale\n\nStart at scene %d · เริ่มที่ฉาก %d\n" % (a, a))
    assert "## %d. Two doors / ประตูสองบาน\n\nThe corridor ends at two doors.\n\nทางเดินแยกเป็นสองประตู\n\n- Open the left door / เปิดประตูซ้าย → go to %d · ไปที่ %d\n- Open the right door / เปิดประตูขวา → (not written yet · ยังไม่ได้เขียน)\n" % (a, lib, lib) in md
    assert "## %d. The end / จบ\n\nYou fall asleep with the book.\n\nคุณหลับไปกับหนังสือ\n\n**The end · จบ**\n" % end in md
    assert "ทิ้ง" not in md
    api = server.Api(p)
    assert api.handle("GET", "/api/story/export", {}, {})[1]["markdown"] == md
    assert cli.main(["--project", str(p.root), "book"]) == 0 and capsys.readouterr().out == md
    out = home / "book.md"
    assert cli.main(["--project", str(p.root), "book", "--out", str(out)]) == 0 and out.read_text(encoding="utf-8") == md


def test_the_cli_makes_lists_exports_and_imports_projects(home, capsys):
    assert cli.main(["new", "Tale", "--mode", "story"]) == 0
    root = capsys.readouterr().out.strip()
    assert root == str(home / "projects" / "tale")
    p = Project(root)
    p.store.add(SCENE1)
    assert cli.main(["--project", root, "projects"]) == 0
    assert "* story     Tale" in capsys.readouterr().out
    out = home / "tale.json"
    assert cli.main(["--project", root, "export", "--out", str(out)]) == 0 and json.loads(out.read_text(encoding="utf-8"))["mode"] == "story"
    capsys.readouterr()
    assert cli.main(["--project", root, "import", str(out), "--name", "Tale 2"]) == 0
    r2 = capsys.readouterr().out.strip()
    assert Project(r2).store.counts()["open"] == 1 and Project(r2).config["name"] == "Tale 2"
    assert cli.main(["--project", root, "save-as", "Tale 3"]) == 0
    assert Project(capsys.readouterr().out.strip()).config["name"] == "Tale 3"
    api_edit = server.Api(p).handle("POST", "/api/decisions/1/edit", {}, {"title_en": "Two doors!"}, local=True)
    assert api_edit[0] == 200 and api_edit[1]["item"]["title_en"] == "Two doors!"
    st, fol = server.Api(p).handle("POST", "/api/decisions/1/follow", {}, {"on": "x"}, local=True)
    assert st == 400
