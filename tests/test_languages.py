"""More languages than Thai and English, English first (2026-11-04): a question's words per language (``title`` as {lang: words},
``texts`` for the languages beyond en and th, options' ``texts``), edited per language, searched, dumped and loaded; the page's
languages — English and Thai built in, any other from a translation file found by the server (GET /api/languages, GET /lang/<code>.json),
the template a translator fills (``dtree lang-template``) and its check; the book in a second language; and on the page: T() through a
loaded file, the questions' words in the language shown with the English fallback, the forms' second boxes."""
import json

import pytest

from dtree import cli, languages, server, story
from dtree.project import Project
from dtree.store import DecisionError, DecisionStore
from test_page import NODE, j, run, text


# ------------------------------------------------------------------------- the store
def test_a_question_keeps_its_words_per_language_with_english_first():
    s = DecisionStore(":memory:")
    q = s.add({"title": {"en": "Two doors", "th": "ประตูสองบาน", "ja": "二つの扉"}, "question": {"en": "Left or right?", "ja": "左か右か"},
               "options": [{"key": "left", "en": "Left", "th": "ซ้าย", "texts": {"ja": "左"}}, {"key": "right", "en": "Right"}]})
    assert q["title_en"] == "Two doors" and q["title_th"] == "ประตูสองบาน" and q["question_th"] == "Left or right?"        # Thai takes the English words
    assert q["texts"] == {"ja": {"title": "二つの扉", "question": "左か右か"}}
    assert q["options"][0]["texts"] == {"ja": "左"} and q["options"][1]["texts"] == {}
    assert s.languages() == ["en", "th", "ja"]
    n = s.tree()["nodes"][0]
    assert n["texts"]["ja"]["title"] == "二つの扉" and n["options"][0]["texts"] == {"ja": "左"}
    assert [x["id"] for x in s.list("all", q="二つ")] == [q["id"]]                                                # searched in any language
    # edited per language: ja replaced, de added, th untouched; a null takes a language out
    e = s.edit(q["id"], {"texts": {"ja": {"title": "ふたつの扉"}, "de": {"title": "Zwei Türen", "question": "Links oder rechts?"}}})
    assert e["texts"] == {"ja": {"title": "ふたつの扉"}, "de": {"title": "Zwei Türen", "question": "Links oder rechts?"}} and e["title_th"] == "ประตูสองบาน"
    assert e["events"][-1]["detail"]["fields"] == ["texts"]
    e = s.edit(q["id"], {"title": {"en": "Two doors!", "fr": "Deux portes"}, "texts": {"ja": None}})
    assert e["title_en"] == "Two doors!" and e["texts"] == {"de": {"title": "Zwei Türen", "question": "Links oder rechts?"}, "fr": {"title": "Deux portes"}}
    assert s.languages() == ["en", "th", "de", "fr", "ja"]                                                          # ja: still on the option
    for bad in ({"texts": {"en": {"title": "x"}}}, {"texts": {"JA": {"title": "x"}}}, {"texts": "ja"}, {"title": {"x1": "y"}},
                {"options": [{"key": "a", "texts": {"th": "x"}}]}):
        with pytest.raises(DecisionError):
            s.edit(q["id"], bad)


def test_a_question_in_one_other_language_alone_needs_english_or_thai():
    """A title needs English or Thai words (the fields every reader of the store has); a language in texts alone is not enough."""
    s = DecisionStore(":memory:")
    with pytest.raises(DecisionError):
        s.add({"title": {"ja": "題"}, "question": {"ja": "問"}})
    ok = s.add({"title": {"en": "T", "ja": "題"}, "question": {"en": "Q", "ja": "問"}, "options": [{"key": "a", "texts": {"ja": "はい"}}]})
    assert ok["options"][0]["en"] == "はい" and ok["options"][0]["th"] == "はい"                                      # an option in ja alone: its words stand in


def test_words_per_language_go_out_and_come_back():
    s = DecisionStore(":memory:")
    s.add({"title": {"en": "T", "ja": "題"}, "question_en": "Q", "options": [{"key": "a", "en": "A", "texts": {"ja": "あ"}}]})
    d = s.dump()
    assert d["decisions"][0]["texts"] == {"ja": {"title": "題"}} and d["decisions"][0]["options"][0]["texts"] == {"ja": "あ"}
    s2 = DecisionStore(":memory:")
    s2.load(json.loads(json.dumps(d)))
    assert s2.get(1)["texts"] == {"ja": {"title": "題"}} and s2.languages() == ["en", "th", "ja"]


# ------------------------------------------------------------------------- the server and the files
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DTREE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DTREE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.delenv("DTREE_DB", raising=False)
    d = tmp_path / "home" / "lang"
    d.mkdir(parents=True)
    (d / "ja.json").write_text(json.dumps({"_name": "日本語", "Settings": "設定", "New question": "新しい質問", "Tree": "ツリー"}, ensure_ascii=False), encoding="utf-8")
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    (d / "xx-TOOLONGREGION.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_the_languages_the_page_can_show_and_the_translation_files(home):
    p = Project(home / "proj", {"db": str(home / "d.sqlite")})
    p.store.add({"title_en": "T", "question_en": "Q", "texts": {"de": {"title": "T"}}})
    api = server.Api(p)
    st, out = api.handle("GET", "/api/languages", {}, {})
    assert st == 200 and [l["code"] for l in out["ui"]] == ["en", "th", "bad", "ja"] and out["content"] == ["en", "th", "de"]
    ja = [l for l in out["ui"] if l["code"] == "ja"][0]
    assert ja["name"] == "日本語" and ja["strings"] == 3 and not ja["builtin"]
    assert [l for l in out["ui"] if l["code"] == "bad"][0]["name"] == "bad"                                       # not JSON: listed by its code, empty
    assert languages.read("ja")["Settings"] == "設定" and languages.read("bad") is None and languages.read("nope") is None
    # over HTTP: the file by its code, nothing else under /lang/
    import threading, urllib.request, urllib.error
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(p))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        assert json.load(urllib.request.urlopen(base + "/lang/ja.json"))["Tree"] == "ツリー"
        for path in ("/lang/nope.json", "/lang/../lang/ja.json", "/lang/ja.txt", "/lang/JA.json"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(base + path)
    finally:
        httpd.shutdown()


def test_the_template_names_every_english_string_and_the_check_counts(home, capsys):
    strings = languages.english_strings()
    assert "Settings" in strings and "New question" in strings and "Click a node for its details" in strings and "Waiting for you" in strings
    assert all(s for s in strings) and len(strings) == len(set(strings))                 # some end in a space on purpose ('asked '): kept as the page uses them
    t = languages.template()
    assert t["_name"] == "" and all(t[s] == "" for s in strings)
    t2 = languages.template("ja")
    assert t2["_name"] == "日本語" and t2["Settings"] == "設定" and t2["Click a node for its details"] == ""
    c = languages.check("ja")
    assert c["exists"] and c["translated"] == 3 and c["total"] == len(strings) and "Click a node for its details" in c["missing"] and c["unused"] == []
    assert languages.check("nope") == {"code": "nope", "exists": False}
    p = Project(home / "proj", {"db": str(home / "d.sqlite")})
    out = home / "xx.json"
    assert cli.main(["--project", str(home / "proj"), "lang-template", "--out", str(out)]) == 0 and json.loads(out.read_text(encoding="utf-8"))["Settings"] == ""
    capsys.readouterr()
    assert cli.main(["--project", str(home / "proj"), "lang-check", "ja"]) == 0 and "3 of" in capsys.readouterr().out
    assert cli.main(["--project", str(home / "proj"), "languages"]) == 0
    printed = capsys.readouterr().out
    assert "ja       日本語" in printed and "content: en, th" in printed


def test_the_book_takes_a_second_language(home):
    p = Project(home / "proj", {"db": str(home / "d.sqlite"), "mode": "story", "name": "Tale"})
    p.store.add({"title": {"en": "Two doors", "th": "ประตูสองบาน", "ja": "二つの扉"}, "question": {"en": "Left or right?", "ja": "左か右か"},
                 "options": [{"key": "left", "en": "Left", "th": "ซ้าย", "texts": {"ja": "左"}}]})
    th = story.markdown(p)
    assert "## 1. Two doors / ประตูสองบาน" in th and "Start at scene 1 · เริ่มที่ฉาก 1" in th and "- Left / ซ้าย → (not written yet · ยังไม่ได้เขียน)" in th
    ja = story.markdown(p, "ja")
    assert "## 1. Two doors / 二つの扉\n\nLeft or right?\n\n左か右か\n" in ja and "- Left / 左 → (not written yet)" in ja and "เริ่ม" not in ja
    en = story.markdown(p, "en")
    assert "## 1. Two doors\n\nLeft or right?\n\n- Left → (not written yet)" in en
    assert server.Api(p).handle("GET", "/api/story/export", {"lang": "ja"}, {})[1]["markdown"] == ja


# ------------------------------------------------------------------------- the page
pytestmark_page = pytest.mark.skipif(NODE is None, reason="no node to run the page's script")


@pytestmark_page
def test_the_page_shows_a_loaded_language_and_falls_back_to_english():
    setup = "LANG.list = [{code: 'en', name: 'English'}, {code: 'th', name: 'ไทย'}, {code: 'ja', name: '日本語'}]; LANG.tr.ja = {'Settings': '設定', 'Waiting for you': 'あなたを待つもの'}; LANG.cur = 'ja';"
    q = {"title_en": "Two doors", "title_th": "ประตูสองบาน", "question_en": "Left or right?", "question_th": "ซ้ายหรือขวา", "texts": {"ja": {"title": "二つの扉"}},
         "options": [{"key": "left", "en": "Left", "th": "ซ้าย", "texts": {"ja": "左"}}, {"key": "right", "en": "Right", "th": "ขวา", "texts": {}}]}
    vals = run(["T('ตั้งค่า', 'Settings')", "T('ไม่มี', 'none')", "CT(Q, 'title')", "CT(Q, 'question')", "CO(Q.options[0])", "CO(Q.options[1])",
                "secondLang()", "langName('ja')", "langSelectHtml()",
                "(LANG.cur = 'th', [CT(Q, 'title'), CO(Q.options[0]), secondLang()])", "(LANG.cur = 'en', [CT(Q, 'title'), CO(Q.options[0]), secondLang(), T('ตั้งค่า', 'Settings')])"],
               setup=setup + " var Q = %s;" % j(q))
    assert vals[:8] == ["設定", "none", "二つの扉", "Left or right?", "左", "Right", "ja", "日本語"]      # translated · English where the file has none · the words shown, else English
    assert '<option value="ja" selected>日本語</option>' in vals[8] and vals[8].startswith('<option value="en">English</option>')
    assert vals[9] == ["ประตูสองบาน", "ซ้าย", "th"] and vals[10] == ["Two doors", "Left", "th", "Settings"]


@pytestmark_page
def test_the_forms_second_boxes_go_to_thai_or_to_the_language_shown():
    vals = run(["optionsOut(parseOptions('left: Left | ซ้าย\\nright: Right'), 'th')", "optionsOut(parseOptions('left: Left | 左\\nright: Right'), 'ja')",
                "wordsOut({title_en: 'T'}, 'th', 'ท', 'ถ')", "wordsOut({title_en: 'T'}, 'ja', '題', '')", "wordsOut({title_en: 'T'}, 'ja', '', '')"])
    assert vals[0] == [{"key": "left", "en": "Left", "recommended": False, "th": "ซ้าย"}, {"key": "right", "en": "Right", "recommended": False, "th": "Right"}]
    assert vals[1] == [{"key": "left", "en": "Left", "recommended": False, "texts": {"ja": "左"}}, {"key": "right", "en": "Right", "recommended": False}]
    assert vals[2] == {"title_en": "T", "title_th": "ท", "question_th": "ถ"} and vals[3] == {"title_en": "T", "texts": {"ja": {"title": "題", "question": ""}}} and vals[4] == {"title_en": "T"}
    # the new-question form in Japanese: the second boxes carry ja into texts
    setup = ("LANG.list = [{code: 'en', name: 'English'}, {code: 'th', name: 'ไทย'}, {code: 'ja', name: '日本語'}]; LANG.tr.ja = {}; LANG.cur = 'ja';"
             " state.project = {name: 'd', mode: 'decisions'}; ['nTitleTh', 'nTitleEn', 'nQTh', 'nQEn', 'nOpts'].forEach(function (id) { document.getElementById(id).value = ''; });"
             " document.getElementById('nTitleEn').value = 'Two doors'; document.getElementById('nTitleTh').value = '二つの扉'; document.getElementById('nQEn').value = 'Left or right?';"
             " document.getElementById('nOpts').value = 'left: Left | 左';")
    _, fetched = run(["(applyMode(), document.getElementById('nSend').onclick(), document.getElementById('nTitleTh').placeholder)"], setup=setup,
                     after=["fetched.filter(function (f) { return f.method === 'POST'; }).map(function (f) { return f.body; })"])
    body = json.loads(fetched[0][0])
    assert body["title_en"] == "Two doors" and body["texts"] == {"ja": {"title": "二つの扉", "question": ""}} and "title_th" not in body
    assert body["options"] == [{"key": "left", "en": "Left", "recommended": False, "texts": {"ja": "左"}}]
    (ph,) = run(["(applyMode(), document.getElementById('nTitleTh').placeholder)"], setup=setup)
    assert ph == "title (日本語, optional)"
