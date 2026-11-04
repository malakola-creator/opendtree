"""More languages than Thai and English (2026-11-04), English first.

Two things are in a language. **The page's own words** (buttons, labels, help): English is written in the page with Thai beside it,
and any other language comes from a translation file — ``{"_name": "日本語", "<the English words>": "<the same in that language>", …}``
— in the package's ``static/lang/<code>.json`` or the person's own ``~/.config/dtree/lang/<code>.json`` (``$DTREE_HOME``; the
person's file wins). What a file does not translate stays English. ``dtree lang-template`` writes a file of every English string the
page shows, for a translator to fill in. **The questions' words** (title, question, context, options): English and Thai have their
own fields, every other language goes in ``texts`` (store.py); the page shows the language chosen and falls back to English.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .store import LANG_CODE

STATIC = Path(__file__).resolve().parent / "static"
BUILTIN = [{"code": "en", "name": "English"}, {"code": "th", "name": "ไทย"}]
_T_CALL = re.compile(r"T\('((?:[^'\\]|\\.)*)', *'((?:[^'\\]|\\.)*)'\)")      # T('ไทย', 'English') in the page's script, both literal
_SPAN = re.compile(r'<span class="en(?: lbl)?">([^<]*)</span>')               # the header's and the form's English spans


def dirs() -> List[Path]:
    home = Path(os.environ.get("DTREE_HOME") or "~/.config/dtree").expanduser()
    return [STATIC / "lang", home / "lang"]


def files() -> Dict[str, Path]:
    """The translation files by code: the package's, then the person's own (which win)."""
    out: Dict[str, Path] = {}
    for d in dirs():
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                if LANG_CODE.match(p.stem) and p.stem not in ("en", "th"):
                    out[p.stem] = p
    return out


def read(code: str) -> Optional[Dict[str, str]]:
    """One language's translations (``_name`` among them), or None when there is no such file or it is not JSON."""
    p = files().get(str(code))
    if p is None:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else None


def ui_languages() -> List[Dict[str, Any]]:
    """English and Thai (built in), then every translation file: code, its own name, how many strings it translates."""
    out: List[Dict[str, Any]] = [dict(b, builtin=True) for b in BUILTIN]
    for code in files():
        tr = read(code) or {}
        out.append({"code": code, "name": tr.get("_name") or code, "builtin": False,
                    "strings": sum(1 for k, v in tr.items() if not k.startswith("_") and v)})
    return out


def english_strings() -> List[str]:
    """Every English string the page shows, in the order it appears: the second argument of each literal ``T('…', '…')`` and the
    English spans of the markup. A string built at run time (``'choice ' + key``) is not here: it stays English in any language."""
    page = (STATIC / "tree.html").read_text(encoding="utf-8")
    seen, out = set(), []
    for _, en in _T_CALL.findall(page):
        en = en.replace("\\'", "'").replace("\\n", "\n")
        if en and en not in seen:
            seen.add(en)
            out.append(en)
    for en in _SPAN.findall(page):
        en = en.strip()
        if en and en not in seen:
            seen.add(en)
            out.append(en)
    return out


def template(code: Optional[str] = None) -> Dict[str, str]:
    """A translation file to fill in: ``_name`` and every English string → "" (or what the language's file already has)."""
    have = (read(code) or {}) if code else {}
    out = {"_name": have.get("_name", "")}
    for s in english_strings():
        out[s] = have.get(s, "")
    return out


def check(code: str) -> Dict[str, Any]:
    """How complete a language's file is: translated, missing (English strings it lacks) and unused (keys the page no longer shows)."""
    tr = read(code)
    if tr is None:
        return {"code": code, "exists": False}
    strings = english_strings()
    translated = [s for s in strings if tr.get(s)]
    return {"code": code, "exists": True, "name": tr.get("_name") or code, "translated": len(translated), "total": len(strings),
            "missing": [s for s in strings if not tr.get(s)], "unused": sorted(k for k in tr if not k.startswith("_") and k not in strings)}


def register_routes(route: Callable, project: Any) -> None:
    @route("GET", r"/api/languages")
    def languages_(q, b):
        return {"ui": ui_languages(), "content": project.store.languages()}


def register_cli(sub: Any) -> None:
    sub.add_parser("languages", help="the languages the page can show (English, Thai, and every translation file) and the ones the questions are written in")
    p = sub.add_parser("lang-template", help="a translation file to fill in: every English string the page shows → \"\" (or the file's own, with --code)")
    p.add_argument("--code", default=None, help="start from that language's file, keeping what it has")
    p.add_argument("--out", default=None, help="write it here (default: standard output)")
    p = sub.add_parser("lang-check", help="how complete a translation file is: translated, missing, unused")
    p.add_argument("code")


def run_cli(args: Any, project: Any) -> bool:
    if args.cmd == "languages":
        for lang in ui_languages():
            print("%-8s %-20s %s" % (lang["code"], lang["name"], "built in" if lang.get("builtin") else "%s strings · %s" % (lang.get("strings", 0), files().get(lang["code"]))))
        print("content:", ", ".join(project.store.languages()))
    elif args.cmd == "lang-template":
        text = json.dumps(template(args.code), ensure_ascii=False, indent=1) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(args.out)
        else:
            print(text, end="")
    elif args.cmd == "lang-check":
        c = check(args.code)
        if not c["exists"]:
            print("no translation file for %s (looked in %s)" % (args.code, ", ".join(str(d) for d in dirs())))
            return True
        print("%s (%s): %d of %d translated · %d missing · %d unused" % (c["code"], c["name"], c["translated"], c["total"], len(c["missing"]), len(c["unused"])))
        for s in c["missing"][:50]:
            print("  missing:", s)
    else:
        return False
    return True
