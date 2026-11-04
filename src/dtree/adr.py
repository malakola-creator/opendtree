"""Decisions as Architecture Decision Records: each question as a Markdown file in the MADR shape, kept with the code.

``dtree adr`` writes ``<adr_dir>/NNNN-<slug>.md`` for every question (``docs/decisions`` by default) and an index ``README.md``:
status (proposed / accepted / implemented / rejected / superseded by …), date, context, the options considered with their good and bad
points, the outcome with the person's note, what was done (tests, effect, commits), the options tried later with their results, and
the links (follows, superseded by). The files are made from the store each time; the store stays the source of truth.

``context()`` is the short list of the decisions in force, for an agent to read before it works (AGENTS.md / CLAUDE.md / MCP).
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .project import Project

STATUS = {"open": "proposed", "decided": "accepted", "done": "accepted, implemented", "dismissed": "rejected"}


def _day(t: Optional[float]) -> str:
    return datetime.date.fromtimestamp(t).isoformat() if t else "-"


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:60].rstrip("-") or "decision")


def filename(item: Dict[str, Any]) -> str:
    return "%04d-%s.md" % (int(item["id"]), slug(item.get("title_en") or ""))


def status_of(item: Dict[str, Any]) -> str:
    if item.get("superseded_by"):
        return "superseded by #%d" % item["superseded_by"]
    return STATUS.get(item["status"], item["status"])


def _cell(text: Any) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def usage_line(u: Dict[str, Any]) -> str:
    """What a question's AI jobs used, on one line (``spend.usage``'s row): jobs, tokens (in / out), dollars, time; "+ ?" where some
    jobs' tokens or dollars are not known, "~" when all the dollars were estimated from tokens."""
    from .spend import _usd                                          # spend imports the store; adr only needs its money format
    n = int(u.get("jobs") or 0)
    tok = "{:,} tokens".format(int(u.get("tokens") or 0)) + (" + ?" if u.get("tokens_unknown_jobs") else "")
    if u.get("tokens_in") or u.get("tokens_out"):
        tok += " (in {:,} / out {:,})".format(int(u.get("tokens_in") or 0), int(u.get("tokens_out") or 0))
    if not u.get("usd_known_jobs") and (u.get("usd_unknown_jobs") or u.get("running")):
        usd = "dollars not known"
    else:
        usd = ("~" if u.get("usd_estimated") and u["usd_estimated"] >= u.get("usd", 0) else "") + _usd(u.get("usd")) + \
            (" + ?" if u.get("usd_unknown_jobs") else "")
    secs = int(round(float(u.get("seconds") or 0)))
    return "%d job%s · %s · %s · %s" % (n, "" if n == 1 else "s", tok, usd, "%dm%02ds" % (secs // 60, secs % 60))


def render(item: Dict[str, Any], others: Optional[Dict[int, Dict[str, Any]]] = None) -> str:
    """One question as an ADR. ``others``: every question by id, to name the linked ones and their files."""
    others = others or {}
    def ref(i: int) -> str:
        o = others.get(int(i))
        return "[#%d %s](%s)" % (int(i), _cell(o["title_en"]) if o else "", filename(o)) if o else "#%d" % int(i)
    status = status_of(item)
    if item.get("superseded_by") and others.get(int(item["superseded_by"])):
        status = "superseded by " + ref(item["superseded_by"])
    L = ["# %d. %s" % (item["id"], item["title_en"]), "", "*%s*" % item["title_th"], "",
         "- Status: **%s**" % status,
         "- Asked: %s · decided: %s · done: %s" % (_day(item["created"]), _day(item.get("decided_at")), _day(item.get("done_at"))),
         "- Raised by: %s" % item.get("source", "")]
    if item.get("follows"):
        L.append("- Follows: %s (%s%s)" % (ref(item["follows"]), item.get("follows_how") or "topic", ", " + item["follows_why"] if item.get("follows_why") else ""))
    sup = [o for o in others.values() if o.get("superseded_by") == item["id"]]
    if sup:
        L.append("- Supersedes: " + ", ".join(ref(o["id"]) for o in sup))
    if item.get("branch"):
        L.append("- Branch: `%s`" % item["branch"])
    L += ["", "## Context and problem", "", item.get("question_en") or "", "", item.get("question_th") or ""]
    if item.get("context_en") or item.get("context_th"):
        L += ["", item.get("context_en") or "", "", item.get("context_th") or ""]
    L += ["", "## Considered options", ""]
    for o in item["options"]:
        mark = " ✓ chosen" if o["key"] == item.get("answer") else ""
        L.append("### `%s` — %s%s%s" % (o["key"], o.get("en") or o["th"], " ★ recommended" if o.get("recommended") else "", mark))
        L.append("")
        L.append("*%s*" % o["th"])
        if o.get("desc_en") or o.get("desc_th"):
            L += ["", o.get("desc_en") or o.get("desc_th")]
        for side, sign in (("pros", "Good"), ("cons", "Bad")):
            for x in o.get(side) or []:
                L.append("- %s, because %s" % (sign, x))
        L.append("")
    L += ["## Decision outcome", ""]
    if item.get("answer"):
        chosen = {o["key"]: o for o in item["options"]}.get(item["answer"], {})
        L.append("Chosen option: **`%s`** — %s" % (item["answer"], chosen.get("en") or chosen.get("th") or ""))
        if item.get("note"):
            L += ["", "> " + item["note"].replace("\n", "\n> ")]
    else:
        L.append("Not decided yet." if item["status"] == "open" else "No option chosen (%s)." % item["status"])
    if item.get("result_tests") or item.get("result_impact") or item.get("commits") or item.get("usage"):
        L += ["", "### Consequences (what was done)", ""]
        if item.get("result_impact"):
            L.append("- Effect: " + _cell(item["result_impact"]))
        if item.get("result_tests"):
            L.append("- Tests: " + _cell(item["result_tests"]))
        for c in (item.get("commits") or [])[:30]:
            L.append("- `%s` %s%s" % (c["sha"][:10], _cell(c.get("subject")), " (merge)" if c.get("merge") else ""))
        if item.get("usage"):
            L.append("- AI jobs run by dtree (finished): " + usage_line(item["usage"]))
    trials = item.get("trials") or []
    if trials:
        L += ["", "## Options tried later", "", "| trial | option | by | status | tests | effect | branch |", "|---|---|---|---|---|---|---|"]
        for t in trials:
            L.append("| %d | `%s` | %s | %s | %s | %s | `%s` |" % (t["id"], t["option"], t.get("variant") or "", t["status"],
                                                               _cell(t.get("result_tests")), _cell(t.get("result_impact")), t["branch"]))
    if item.get("refs"):
        L += ["", "## References", ""] + ["- %s" % r for r in item["refs"]]
    L += ["", "<!-- made by dtree from its store (question %d); edit the question, not this file -->" % item["id"], ""]
    return "\n".join(L)


def export(project: Project, out: Optional[str] = None, statuses: Optional[List[str]] = None) -> List[Path]:
    """Write every question (or those with ``statuses``) as ADR files and the index; returns the paths written."""
    store = project.store
    d = project.path(out or project.config.get("adr_dir") or "docs/decisions")
    d.mkdir(parents=True, exist_ok=True)
    from .spend import usage                                         # what each question's finished AI jobs used
    use = usage(project, ended=True)
    items = {i: dict(store.get(i), usage=use.get(i)) for i in store.ids()}
    chosen = [i for i in sorted(items.values(), key=lambda x: x["id"]) if not statuses or i["status"] in statuses]
    written = []
    for item in chosen:
        p = d / filename(item)
        text = render(item, items)
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            p.write_text(text, encoding="utf-8")
        written.append(p)
    idx = ["# Decisions — %s" % project.config["name"], "", "Made by dtree from its store; one file per question (MADR).", "",
           "| # | title | status | answer | date |", "|---|---|---|---|---|"]
    for item in chosen:
        idx.append("| %d | [%s](%s) | %s | %s | %s |" % (item["id"], _cell(item["title_en"]), filename(item), status_of(item),
                                                     "`%s`" % item["answer"] if item.get("answer") else "", _day(item.get("decided_at") or item["created"])))
    (d / "README.md").write_text("\n".join(idx) + "\n", encoding="utf-8")
    return written + [d / "README.md"]


def context(project: Project, limit: int = 200) -> str:
    """The decisions in force (decided or done, not superseded), newest first, one line each, for an agent's context."""
    store = project.store
    rows = [i for i in (store.get(x) for x in store.ids()) if i["status"] in ("decided", "done") and not i.get("superseded_by")]
    rows.sort(key=lambda i: i.get("decided_at") or i["created"], reverse=True)
    L = ["# Decisions in force — %s" % project.config["name"], "",
         "The person decided these; follow them. Ask a new question (dtree add / MCP dtree_ask) before going against one.", ""]
    for i in rows[:limit]:
        o = {x["key"]: x for x in i["options"]}.get(i.get("answer"), {})
        L.append("- #%d %s → **%s**%s%s" % (i["id"], _cell(i["title_en"]), i.get("answer") or "-", (": " + _cell(o.get("en"))) if o.get("en") else "",
                                           (" — " + _cell(i["note"])[:200]) if i.get("note") else ""))
    if len(rows) > limit:
        L.append("- … %d more (dtree list --status decided / done)" % (len(rows) - limit))
    open_n = store.counts().get("open", 0)
    if open_n:
        L += ["", "%d question(s) still open: dtree list." % open_n]
    return "\n".join(L) + "\n"
