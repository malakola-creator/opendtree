"""A story from the store (2026-11-04, the user: "a gamebook, the reader chooses"): every question is a scene, its options are the
reader's choices, and the question that follows on one option (``follows_option``, store.follow) is the scene that choice leads to.
A scene without options is an ending; a choice no scene follows yet is unwritten. The tree page reads the scenes for its reader
(``GET /api/story``); the export is a Markdown book, scenes numbered by id (``GET /api/story/export``, ``dtree book``)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .project import Project


def scenes(project: Project) -> Dict[str, Any]:
    """The scenes in id order (dismissed ones left out), each with its choices and where each leads: ``to`` the scene's id, or None
    when none is written yet. ``start``: the first scene nothing hangs on."""
    tree = project.store.tree()
    nodes = [n for n in tree["nodes"] if n.get("status") != "dismissed"]
    ids = {n["id"] for n in nodes}
    leads: Dict[tuple, int] = {}
    for n in nodes:                                                  # the earliest scene written on a choice is where it leads
        if n.get("parent") in ids and n.get("on_option") is not None:
            leads.setdefault((n["parent"], n["on_option"]), n["id"])
    out = []
    for n in nodes:
        choices = [{"key": o["key"], "th": o.get("th", ""), "en": o.get("en", ""), "texts": o.get("texts") or {}, "to": leads.get((n["id"], o["key"]))} for o in n.get("options") or []]
        out.append({"id": n["id"], "title_th": n.get("title_th") or "", "title_en": n.get("title_en") or "", "text_th": n.get("question_th") or "",
                    "text_en": n.get("question_en") or "", "texts": n.get("texts") or {}, "choices": choices, "ending": not choices,
                    "parent": n.get("parent") if n.get("parent") in ids else None, "on_option": n.get("on_option")})
    roots = [s["id"] for s in out if s["parent"] is None]
    unwritten = [{"scene": s["id"], "key": c["key"], "th": c["th"], "en": c["en"]} for s in out for c in s["choices"] if c["to"] is None]
    return {"scenes": out, "start": roots[0] if roots else None, "roots": roots, "endings": sum(1 for s in out if s["ending"]), "unwritten": unwritten}


def _both(en: str, th: str) -> str:
    """English, then Thai when it differs (English first, 2026-11-04)."""
    en, th = (en or "").strip(), (th or "").strip()
    return en if not th or th == en else (th if not en else en + " / " + th)


def _second(sc: Dict[str, Any], field: str, lang: Optional[str]) -> str:
    """A scene's words in the second language of the book: Thai from its field, any other from ``texts``; "" when none."""
    if lang == "th":
        return sc.get("title_th" if field == "title" else "text_th") or ""
    if lang and lang != "en":
        return (sc.get("texts") or {}).get(lang, {}).get("title" if field == "title" else "question") or ""
    return ""


def _choice_second(c: Dict[str, Any], lang: Optional[str]) -> str:
    if lang == "th":
        return c.get("th") or ""
    if lang and lang != "en":
        return (c.get("texts") or {}).get(lang) or ""
    return ""


def markdown(project: Project, lang: Optional[str] = "th") -> str:
    """The book in English, with a second language beside it when ``lang`` names one (Thai by default; any code in ``texts``; None or
    "en" for English alone): a title, where to start, then every scene with its text and its choices ("→ go to N"), endings marked."""
    s = scenes(project)
    name = project.config.get("name") or project.root.name
    thai = lang == "th"
    lines = ["# " + name, ""]
    if s["start"] is not None:
        lines += ["Start at scene %d" % s["start"] + (" · เริ่มที่ฉาก %d" % s["start"] if thai else ""), ""]
    for sc in s["scenes"]:
        lines.append("## %d. %s" % (sc["id"], _both(sc["title_en"], _second(sc, "title", lang))))
        lines.append("")
        text_en, text_2 = sc["text_en"].strip(), _second(sc, "text", lang).strip()
        if text_en:
            lines += [text_en, ""]
        if text_2 and text_2 != text_en:
            lines += [text_2, ""]
        if sc["ending"]:
            lines += ["**The end" + (" · จบ" if thai else "") + "**", ""]
            continue
        for c in sc["choices"]:
            where = ("→ go to %d" % c["to"] + (" · ไปที่ %d" % c["to"] if thai else "")) if c["to"] is not None else "→ (not written yet" + (" · ยังไม่ได้เขียน" if thai else "") + ")"
            lines.append("- %s %s" % (_both(c["en"], _choice_second(c, lang)), where))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def register_routes(route: Callable, project: Project) -> None:
    @route("GET", r"/api/story")
    def story_(q, b):
        return scenes(project)

    @route("GET", r"/api/story/export")
    def export_(q, b):                                               # ?lang=th (default) · ja · en (English alone)
        return {"name": project.config.get("name"), "lang": q.get("lang", "th"), "markdown": markdown(project, q.get("lang", "th"))}


def register_cli(sub: Any) -> None:
    p = sub.add_parser("book", help="the story as a Markdown book: every scene, its choices and where each leads")
    p.add_argument("--out", default=None, help="write it here (default: standard output)")
    p.add_argument("--lang", default="th", help="the second language beside English: th (default), a code in the scenes' texts, or en for English alone")


def run_cli(args: Any, project: Project) -> bool:
    if args.cmd != "book":
        return False
    text = markdown(project, args.lang)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(args.out)
    else:
        print(text, end="")
    return True
