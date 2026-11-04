"""The decision store: questions waiting for a person, their options, the answer taken, the history, the options tried later and
the code of each (dtree; first built inside ultimateTruth, 2026-10-19 → 2026-11-01, split out as its own tool on 2026-11-01).

One SQLite file with these tables:

- ``decisions`` — one row per question: what kind (a problem noticed, or a decision someone has to make), who raised it
  (system / claude / user / an agent's name), the bilingual title, question and context, the options (one may be recommended), the
  references, its status (open → decided → done, or dismissed), the answer with the person's note, and — once the work is done — when
  it was finished, the test results and the effect it had. Three times are kept: ``created`` (asked), ``decided_at`` (answered),
  ``done_at`` (finished after the decision).
- ``events`` — everything that happened to a question, in order.
- ``trials`` — another option tried later on a question already decided: its git branch (``alt/<id>-<option>``), its status
  (trying → measured → adopted / switch / dropped) and its measured result. The first answer is never overwritten by a trial.
- ``jobs`` — a trial handed to an AI agent (Claude Code, Codex, or a chat model through an OpenAI-compatible API: Muse, Grok, a local
  model): it builds the option in a git worktree of the trial's branch and ends at the measurement (``jobs.py``). One job at a time.
- ``checks`` — a command dtree ran itself on one commit (``verify.py``).
- ``requests`` — each request dtree's chat loop sent to a model's API, so an agent's ``requests_per_minute`` / ``requests_per_day``
  hold across every job of it, in whatever runner process (``request_slot``; 2026-11-02, the user's settings page).

The code of each: a decision's work is done on its own branch ``dec/<id>-<option>`` and merged into the base without fast-forward;
the question keeps the branch and the commits. A question that names an earlier one ("#41") grows from it in the tree; one that names
none can be told which it follows (``follow``). The schema is the one ultimateTruth's ``data/decisions.sqlite`` has, so either opens the other's.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


KINDS = ("problem", "decision")
SOURCES = ("system", "claude", "user", "agent")
STATUSES = ("open", "decided", "done", "dismissed")
#: how a question follows an earlier one when its text names none: the same subject, or only the next one asked
FOLLOWS = ("topic", "sequence")
#: a trial of another option: being built, measured beside the answer, then the user's choice — adopted (becomes the answer),
#: switch (both kept, chosen in config) or dropped (the branch stays in git, nothing in the main line changes)
TRIAL_OPEN = ("trying", "measured")
TRIAL_ENDS = ("adopted", "switch", "dropped")
#: how a commit was kept as a decision's code: merged from its branch, named by hand, or matched afterwards from the history
CODE_HOW = ("merged", "recorded", "backfilled")
#: a trial handed to the AI (#64): running, then measured (the trial's result is in), failed (with the reason) or stopped by the user
JOB_ENDS = ("measured", "failed", "stopped")
#: what a job does: build the option (also a follow-up), or bring the trial up to date with the base (resolve a merge's conflicts)
JOB_KINDS = ("build", "update")
#: a check (verify.py): a command run on one commit in a sandbox — the project's tests or a measure — and how it ended
CHECK_STATUSES = ("running", "passed", "failed", "error", "timeout", "stopped")
#: events written for the feature modules through ``note_event`` (verify.py, merge.py, spend.py)
EXTRA_EVENTS = ("verified", "merged", "updated", "budget_refused", "rate_refused")
#: the request windows of an agent's rate limits (``request_slot``), rolling: the last minute and the last 24 hours
REQUEST_MINUTE_S = 60.0
REQUEST_DAY_S = 86400.0
#: requests older than this are of no window any more: pruned now and then (every REQUEST_PRUNE_EVERY-th request)
REQUEST_KEEP_S = 2 * 86400.0
REQUEST_PRUNE_EVERY = 100
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_BRANCH = re.compile(r"^(dec|alt)/[A-Za-z0-9_.-]{1,80}$")
DB_ENV_VAR = "DTREE_DB"
# "#41" names question 41; "architecture.md#12.61" and "#12.61" are document anchors, not questions
_MENTION = re.compile(r"(?<![A-Za-z0-9_/.#])#(\d{1,5})(?![\d]|\.\d)")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    title_th TEXT NOT NULL, title_en TEXT NOT NULL,
    question_th TEXT NOT NULL, question_en TEXT NOT NULL,
    context_th TEXT NOT NULL DEFAULT '', context_en TEXT NOT NULL DEFAULT '',
    options TEXT NOT NULL DEFAULT '[]',
    refs TEXT NOT NULL DEFAULT '[]',
    answer TEXT, note TEXT, decided_at REAL, decided_by TEXT,
    done_at REAL, done_by TEXT, result_tests TEXT, result_impact TEXT,
    follows INTEGER, follows_how TEXT, follows_why TEXT,
    branch TEXT, commits TEXT,
    superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS decisions_status ON decisions(status, updated);
CREATE INDEX IF NOT EXISTS decisions_key ON decisions(key, status);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    at REAL NOT NULL,
    action TEXT NOT NULL,
    by TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_decision ON events(decision_id, at);
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    option TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'trying',
    branch TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL,
    by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    result_tests TEXT, result_impact TEXT,
    ended_at REAL, ended_by TEXT, ended_note TEXT,
    commits TEXT,
    variant TEXT
);
CREATE INDEX IF NOT EXISTS trials_decision ON trials(decision_id, id);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id INTEGER NOT NULL REFERENCES trials(id),
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    status TEXT NOT NULL DEFAULT 'running',
    created REAL NOT NULL, updated REAL NOT NULL, ended_at REAL,
    by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    runner_pid INTEGER, agent_pid INTEGER, stop_requested INTEGER NOT NULL DEFAULT 0,
    worktree TEXT, log TEXT, reason TEXT, result TEXT, cost_usd REAL,
    agent TEXT,
    kind TEXT NOT NULL DEFAULT 'build'
);
CREATE INDEX IF NOT EXISTS jobs_trial ON jobs(trial_id, id);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    sha TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'tests',
    command TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    created REAL NOT NULL, updated REAL NOT NULL, ended_at REAL,
    by TEXT NOT NULL DEFAULT '',
    runner_pid INTEGER,
    exit_code INTEGER,
    framework TEXT, passed INTEGER, failed INTEGER, errors INTEGER, skipped INTEGER, xfailed INTEGER, xpassed INTEGER, total INTEGER,
    value REAL, unit TEXT,
    summary TEXT, tail TEXT, log TEXT, seconds REAL
);
CREATE INDEX IF NOT EXISTS checks_key ON checks(key, id);
CREATE INDEX IF NOT EXISTS checks_sha ON checks(sha, name);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    at REAL NOT NULL,
    job_id INTEGER
);
CREATE INDEX IF NOT EXISTS requests_agent ON requests(agent, at);
"""

#: limits on what a request may store (the dashboard is reachable on the local network)
MAX_TEXT = 4000
MAX_TITLE = 200
MAX_OPTIONS = 8
MAX_POINTS = 10


class DecisionError(ValueError):
    pass


def _ref_ok(branch: Any) -> bool:
    """A trial branch name dtree's checks and git both take: ``alt/<id>-…`` within 80 characters of letters, digits, ``_ . -``, with
    no ``..`` and no part ending in ``.lock`` or ``.``."""
    b = str(branch or "")
    return bool(_BRANCH.match(b)) and b.startswith("alt/") and len(b) - len("alt/") <= 80 and ".." not in b and \
        not any(part.endswith((".lock", ".")) or part.startswith(".") for part in b.split("/"))


def _limit(value: Any, name: str) -> Optional[int]:
    """A rate limit for ``request_slot``: None (no limit) or a whole number above 0."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DecisionError("%s is a whole number above 0, or None (no limit), not %r" % (name, value))
    return int(value)


def _id(value: Any) -> int:
    """A row id; one SQLite cannot hold (a 20-digit number from a URL or a form) is no row: KeyError, as for an id that is not there."""
    n = int(value)
    if not 0 < n < 2 ** 63:
        raise KeyError(value)
    return n


def default_db_path(root: Any = ".") -> Path:
    """``$DTREE_DB`` if set, else ``.dtree/decisions.sqlite`` under the project root."""
    env = os.environ.get(DB_ENV_VAR)
    return Path(env) if env else Path(root) / ".dtree" / "decisions.sqlite"


def _text(value: Any, limit: int, field: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise DecisionError("%s must be text" % field)
    value = value.strip()
    if required and not value:
        raise DecisionError("%s is required" % field)
    if len(value) > limit:
        raise DecisionError("%s is longer than %d characters" % (field, limit))
    return value


def _refs(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DecisionError("refs must be a list of text")
    return raw


#: a language code: ISO 639 letters, with a region or script when needed (ja, pt-BR, zh-Hant); en and th have columns of their own
LANG_CODE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
MAX_LANGS = 20


def _texts(raw: Any, field: str, limit: int = MAX_TEXT) -> Dict[str, Any]:
    """Words in languages beyond en and th: ``{"ja": "…"}`` for an option, ``{"ja": {"title": …, "question": …, "context": …}}`` for a
    question. Codes checked, empty ones dropped, en and th refused here (they have columns)."""
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_LANGS:
        raise DecisionError("%s must be an object of at most %d languages" % (field, MAX_LANGS))
    out: Dict[str, Any] = {}
    for code, v in raw.items():
        code = str(code).strip()
        if code in ("en", "th") or not LANG_CODE.match(code):
            raise DecisionError("%s: %r is not a language code (en and th have their own fields)" % (field, code))
        if isinstance(v, dict):
            words = {k: _text(v.get(k), MAX_TITLE if k == "title" else limit, "%s.%s.%s" % (field, code, k)) for k in ("title", "question", "context") if v.get(k) is not None}
            words = {k: w for k, w in words.items() if w}
            if words:
                out[code] = words
        else:
            w = _text(v, limit, "%s.%s" % (field, code))
            if w:
                out[code] = w
    return out


def _split_langs(entry: Dict[str, Any]) -> Dict[str, Any]:
    """A question given with ``title`` / ``question`` / ``context`` as ``{"en": …, "th": …, "ja": …}`` (multi-language, 2026-11-04):
    en and th go to their fields, the rest into ``texts``. The flat fields (``title_en`` …) still work, and win when both are given."""
    out = dict(entry)
    texts = _texts(entry.get("texts"), "texts")
    for k in ("title", "question", "context"):
        d = entry.get(k)
        if not isinstance(d, dict):
            continue
        for code, v in d.items():
            code = str(code).strip()
            if code in ("en", "th"):
                out.setdefault("%s_%s" % (k, code), v)
            else:
                w = _text(v, MAX_TITLE if k == "title" else MAX_TEXT, "%s.%s" % (k, code))
                if not LANG_CODE.match(code):
                    raise DecisionError("%s: %r is not a language code" % (k, code))
                if w:
                    texts.setdefault(code, {})[k] = w
    out["texts"] = texts
    return out


def _options(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_OPTIONS:
        raise DecisionError("options must be a list of at most %d" % MAX_OPTIONS)
    out, seen = [], set()
    for i, o in enumerate(raw):
        if not isinstance(o, dict):
            raise DecisionError("option %d must be an object" % i)
        key = _text(o.get("key") or "o%d" % (i + 1), 40, "option key", True)
        if key in seen:
            raise DecisionError("option key %r is repeated" % key)
        seen.add(key)
        th, en = _text(o.get("th"), MAX_TITLE, "option th"), _text(o.get("en"), MAX_TITLE, "option en")
        texts = _texts(o.get("texts"), "option %d texts" % i, MAX_TITLE)           # its words in other languages: {"ja": "…"}
        if not (th or en or texts):                                  # its words in one language at least (English first, 2026-11-04)
            raise DecisionError("option %d needs its words (en, th or texts)" % i)
        en, th = en or th or next(iter(texts.values())), th or en or next(iter(texts.values()))
        item = {"key": key, "th": th, "en": en, "texts": texts,
                "desc_th": _text(o.get("desc_th"), MAX_TEXT, "option desc_th"), "desc_en": _text(o.get("desc_en"), MAX_TEXT, "option desc_en"),
                "recommended": bool(o.get("recommended"))}
        for side in ("pros", "cons"):                                # an ADR's "considered options": good and bad of each
            got = o.get(side)
            if got is None:
                continue
            if not isinstance(got, list) or len(got) > MAX_POINTS:
                raise DecisionError("option %s must be a list of at most %d" % (side, MAX_POINTS))
            item[side] = [_text(x, MAX_TITLE, "option %s" % side, True) for x in got]
        out.append(item)
    return out


class DecisionStore:
    """The questions and their history. One connection guarded by a lock: the dashboard's threads and the simulation share it."""

    def __init__(self, path: Any = None) -> None:
        self.path = str(path) if path is not None else str(default_db_path())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            if "commits" not in {r["name"] for r in self._db.execute("PRAGMA table_info(trials)")}:      # a trials table made before the code was kept
                self._db.execute("ALTER TABLE trials ADD COLUMN commits TEXT")
            if "variant" not in {r["name"] for r in self._db.execute("PRAGMA table_info(trials)")}:      # before races (2026-11-01)
                self._db.execute("ALTER TABLE trials ADD COLUMN variant TEXT")
            jcols = {r["name"] for r in self._db.execute("PRAGMA table_info(jobs)")}
            if "agent" not in jcols:                                                                       # a jobs table made by ultimateTruth
                self._db.execute("ALTER TABLE jobs ADD COLUMN agent TEXT")
            if "kind" not in jcols:                                                                        # before update jobs (2026-11-01)
                self._db.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'build'")
            have = {r["name"] for r in self._db.execute("PRAGMA table_info(decisions)")}
            for col in ("done_at REAL", "done_by TEXT", "result_tests TEXT", "result_impact TEXT",     # a database made before 2026-10-19
                        "follows INTEGER", "follows_how TEXT", "follows_why TEXT",                     # before 2026-11-01
                        "branch TEXT", "commits TEXT", "superseded_by INTEGER",
                        "follows_option TEXT",                                                         # before story mode (2026-11-04)
                        "texts TEXT"):                                                                 # before more languages than en and th (2026-11-04)
                if col.split()[0] not in have:
                    self._db.execute("ALTER TABLE decisions ADD COLUMN " + col)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextlib.contextmanager
    def _writing(self) -> Iterator[None]:
        """The lock for a write, and the write rolled back when it does not finish: a refusal after an INSERT (a question whose follows
        is refused, review F10) must not stay pending on the shared connection, where every read sees it and the next write commits it."""
        with self._lock:
            try:
                yield
            except BaseException:
                self._db.rollback()
                raise

    # ------------------------------------------------------------------ writes
    def add(self, entry: Dict[str, Any], by: Optional[str] = None) -> Dict[str, Any]:
        """Raise a question. With a ``key`` already open: counted again instead of duplicated. Returns the stored question."""
        if not isinstance(entry, dict):
            raise DecisionError("a question must be an object")
        entry = _split_langs(entry)                                  # title / question / context as {lang: words} → the fields and ``texts``
        kind = entry.get("kind", "decision")
        source = entry.get("source", "user")
        if kind not in KINDS:
            raise DecisionError("kind must be one of %s" % ", ".join(KINDS))
        if source not in SOURCES:
            raise DecisionError("source must be one of %s" % ", ".join(SOURCES))
        key = _text(entry.get("key"), 80, "key") or None
        # a title and a question in at least one language (English first, 2026-11-04); the other language takes the same words
        title_th, title_en = _text(entry.get("title_th"), MAX_TITLE, "title_th"), _text(entry.get("title_en"), MAX_TITLE, "title_en")
        if not (title_th or title_en):
            raise DecisionError("title_en (or title_th) is required")
        question_th, question_en = _text(entry.get("question_th"), MAX_TEXT, "question_th"), _text(entry.get("question_en"), MAX_TEXT, "question_en")
        if not (question_th or question_en):
            raise DecisionError("question_en (or question_th) is required")
        row = {
            "key": key, "kind": kind, "source": source,
            "title_th": title_th or title_en, "title_en": title_en or title_th,
            "question_th": question_th or question_en, "question_en": question_en or question_th,
            "context_th": _text(entry.get("context_th"), MAX_TEXT, "context_th"), "context_en": _text(entry.get("context_en"), MAX_TEXT, "context_en"),
            "options": json.dumps(_options(entry.get("options")), ensure_ascii=False),
            "refs": json.dumps([_text(r, 200, "ref", True) for r in _refs(entry.get("refs"))][:12], ensure_ascii=False),
            "texts": json.dumps(entry.get("texts") or {}, ensure_ascii=False),
        }
        now = time.time()
        who = by or source
        with self._writing():                                        # the question, its raised event and its follows link, or none of them
            if key:
                old = self._db.execute("SELECT id FROM decisions WHERE key = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (key,)).fetchone()
                if old is not None:
                    self._db.execute("UPDATE decisions SET occurrences = occurrences + 1, updated = ?, context_th = ?, context_en = ? WHERE id = ?",
                                     (now, row["context_th"], row["context_en"], old["id"]))
                    self._event(old["id"], now, "seen_again", who, {"context_th": row["context_th"]})
                    self._db.commit()
                    return self._get(old["id"])
            cur = self._db.execute(
                "INSERT INTO decisions (key, kind, source, status, created, updated, title_th, title_en, question_th, question_en, context_th, context_en, options, refs, texts)"
                " VALUES (:key, :kind, :source, 'open', :now, :now, :title_th, :title_en, :question_th, :question_en, :context_th, :context_en, :options, :refs, :texts)",
                dict(row, now=now))
            did = cur.lastrowid
            self._event(did, now, "raised", who, {})
            if entry.get("follows") is not None:
                self._follow(did, entry.get("follows"), entry.get("follows_how", "topic"), entry.get("follows_why", ""), who, now,
                             entry.get("follows_option"))
            self._db.commit()
            return self._get(did)

    def follow(self, did: int, on: Optional[int], how: str = "topic", why: str = "", by: str = "claude", option: Optional[str] = None) -> Dict[str, Any]:
        """Say which earlier question this one grew from, for the tree, when its text names none (or names the wrong one).
        ``how``: topic (the same subject) or sequence (no shared subject, only asked next). ``on=None`` removes the link.
        ``option``: the key of the earlier question's option this one hangs on — a story's scene after one choice (2026-11-04);
        without it the tree hangs it on the answer in force when it was raised."""
        with self._writing():
            self._get(did)
            now = time.time()
            self._follow(did, on, how, why, by, now, option)             # the tree only: the question's own "updated" stays
            self._db.commit()
            return self._get(did)

    def _follow(self, did: int, on: Any, how: str, why: str, by: str, now: float, option: Any = None) -> None:
        why = _text(why, MAX_TEXT, "why")
        option = _text(option, 40, "follows_option") or None
        if on is not None:
            on = int(on)
            if how not in FOLLOWS:
                raise DecisionError("how must be one of %s" % ", ".join(FOLLOWS))
            if not 0 < on < did:
                raise DecisionError("question %d can only follow an earlier question" % did)
            parent = self._get(on)
            if not why:
                raise DecisionError("say why question %d follows #%d" % (did, on))
            if option is not None and option not in {o.get("key") for o in parent["options"] if isinstance(o, dict)}:
                raise DecisionError("question %d has no option %r" % (on, option))
        else:
            option = None
        self._db.execute("UPDATE decisions SET follows = ?, follows_how = ?, follows_why = ?, follows_option = ? WHERE id = ?",
                         (on, how if on is not None else None, why if on is not None else None, option, did))
        self._event(did, now, "followed", by, {"on": on, "how": how, "why": why, "option": option})

    # the fields a person may change afterwards (2026-11-04: a story's scenes are rewritten; a question's wording too); the answer,
    # the status and the links are changed by their own actions, never here
    _EDITABLE = ("title_th", "title_en", "question_th", "question_en", "context_th", "context_en", "options", "refs", "texts")

    def edit(self, did: int, fields: Dict[str, Any], by: str = "user") -> Dict[str, Any]:
        """Change a question's wording, options or references. Only the fields given change; an option keeps its key (a later
        question hangs on it); ``texts`` (or ``title`` / ``question`` / ``context`` as {lang: words}) replaces the words in the
        languages given and keeps the others. Returns the question; the history gets ``edited`` with the fields that changed."""
        if not isinstance(fields, dict):
            raise DecisionError("the edit must be an object")
        raw_texts = fields.get("texts") if isinstance(fields.get("texts"), dict) else {}   # as given: a null there takes a language out
        if any(isinstance(fields.get(k), dict) for k in ("title", "question", "context")):
            fields = _split_langs(fields)
            fields = {k: v for k, v in fields.items() if k not in ("title", "question", "context")}
        bad = [k for k in fields if k not in self._EDITABLE]
        if bad:
            raise DecisionError("cannot edit %s (only %s)" % (", ".join(sorted(bad)), ", ".join(self._EDITABLE)))
        new: Dict[str, Any] = {}
        texts_given: Optional[Dict[str, Any]] = None
        for k, v in fields.items():
            if k == "options":
                new[k] = json.dumps(_options(v), ensure_ascii=False)
            elif k == "refs":
                new[k] = json.dumps([_text(r, 200, "ref", True) for r in _refs(v)][:12], ensure_ascii=False)
            elif k == "texts":
                texts_given = _texts(v, "texts")
            else:
                new[k] = _text(v, MAX_TITLE if k.startswith("title") else MAX_TEXT, k)
        if not new and texts_given is None:
            raise DecisionError("nothing to edit")
        with self._writing():
            old = self._db.execute("SELECT * FROM decisions WHERE id = ?", (_id(did),)).fetchone()
            if old is None:
                raise KeyError(did)
            if texts_given is not None:                              # per language: given words replace, null or empty takes the language out
                merged = json.loads(old["texts"] or "{}") if "texts" in old.keys() else {}
                for code, raw in raw_texts.items():
                    code = str(code).strip()
                    if code in texts_given:
                        merged[code] = texts_given[code]
                    elif raw in (None, {}, ""):
                        merged.pop(code, None)
                for code, words in texts_given.items():              # languages that came through title / question / context as {lang: words}
                    merged[code] = dict(merged.get(code) or {}, **words)
                new["texts"] = json.dumps(merged, ensure_ascii=False)
            for th, en, what in (("title_th", "title_en", "title"), ("question_th", "question_en", "question")):   # one language at least; the other takes its words
                a, b = new.get(th, old[th]) or "", new.get(en, old[en]) or ""
                if not (a or b):
                    raise DecisionError("a %s is needed in at least one language" % what)
                if th in new and not new[th]:
                    new[th] = b
                if en in new and not new[en]:
                    new[en] = a
            changed = [k for k, v in new.items() if (old[k] or ("[]" if k in ("options", "refs") else "{}" if k == "texts" else "")) != v]
            if changed:
                now = time.time()
                self._db.execute("UPDATE decisions SET " + ", ".join("%s = :%s" % (k, k) for k in changed) + ", updated = :now WHERE id = :id",
                                 dict({k: new[k] for k in changed}, now=now, id=int(did)))
                self._event(int(did), now, "edited", by, {"fields": changed})
                self._db.commit()
            return self._get(int(did), events=True)

    # ------------------------------------------------------------------ a whole store out and in (export, import, save as — 2026-11-04)
    _PORTABLE = ("decisions", "events", "trials")

    def dump(self) -> Dict[str, Any]:
        """Every question with its history and its trials, as rows (``options``, ``refs``, ``commits`` as JSON values). Jobs, checks and
        requests stay: they belong to this machine's runs."""
        with self._lock:
            out: Dict[str, Any] = {}
            for table in self._PORTABLE:
                rows = [dict(r) for r in self._db.execute("SELECT * FROM %s ORDER BY id" % table)]
                for r in rows:
                    for k in ("options", "refs", "commits", "detail", "texts"):
                        if k in r:
                            r[k] = json.loads(r[k] or ("{}" if k in ("detail", "texts") else "[]"))
                out[table] = rows
            return out

    def load(self, data: Dict[str, Any]) -> Dict[str, int]:
        """Put a ``dump`` into this empty store, ids kept (a later question hangs on an earlier one by id). Columns the dump has and this
        store lacks are dropped; the ones it lacks take the table's defaults. Refused when the store already has questions."""
        if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
            raise DecisionError("the data must be a dump with a decisions list")
        counts: Dict[str, int] = {}
        with self._writing():
            if self._db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]:
                raise DecisionError("the store is not empty: import into a new project")
            for table in self._PORTABLE:
                cols = [r["name"] for r in self._db.execute("PRAGMA table_info(%s)" % table)]
                rows = data.get(table) or []
                if not isinstance(rows, list):
                    raise DecisionError("%s must be a list" % table)
                n = 0
                for row in rows:
                    if not isinstance(row, dict):
                        raise DecisionError("every %s row must be an object" % table)
                    keep = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in row.items() if k in cols}
                    if not keep:
                        continue
                    self._db.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(keep), ", ".join("?" * len(keep))), list(keep.values()))
                    n += 1
                counts[table] = n
            self._db.commit()
        return counts

    def answer(self, did: int, option: Optional[str], note: str = "", by: str = "user") -> Dict[str, Any]:
        """Decide: one of the options (or none, with a note saying what to do instead)."""
        note = _text(note, MAX_TEXT, "note")
        with self._writing():
            item = self._get(did)
            if item["status"] != "open":
                raise DecisionError("question %d is already %s" % (did, item["status"]))
            keys = [o["key"] for o in item["options"]]
            if option is not None and option not in keys:
                raise DecisionError("option %r is not one of %s" % (option, ", ".join(keys)))
            if option is None and not note:
                raise DecisionError("choose an option or write what to do instead")
            now = time.time()
            self._db.execute("UPDATE decisions SET status = 'decided', answer = ?, note = ?, decided_at = ?, decided_by = ?, updated = ? WHERE id = ?",
                             (option, note, now, by, now, did))
            self._event(did, now, "answered", by, {"option": option, "note": note})
            self._db.commit()
            return self._get(did)

    def done(self, did: int, tests: str, impact: str, by: str = "claude") -> Dict[str, Any]:
        """The work a decision asked for is finished: when, the test results, and what it changed."""
        tests = _text(tests, MAX_TEXT, "tests", True)
        impact = _text(impact, MAX_TEXT, "impact", True)
        with self._writing():
            item = self._get(did)
            if item["status"] not in ("decided", "done"):
                raise DecisionError("question %d is %s: only a decided question can be marked done" % (did, item["status"]))
            now = time.time()
            self._db.execute("UPDATE decisions SET status = 'done', done_at = ?, done_by = ?, result_tests = ?, result_impact = ?, updated = ? WHERE id = ?",
                             (now, by, tests, impact, now, did))
            self._event(did, now, "done", by, {"tests": tests, "impact": impact})
            self._db.commit()
            return self._get(did)

    def dismiss(self, did: int, note: str = "", by: str = "user") -> Dict[str, Any]:
        return self._close(did, "dismissed", note, by)

    def reopen(self, did: int, note: str = "", by: str = "user") -> Dict[str, Any]:
        note = _text(note, MAX_TEXT, "note")
        with self._writing():
            item = self._get(did)
            if item["status"] == "open":
                raise DecisionError("question %d is open already" % did)
            now = time.time()
            self._db.execute("UPDATE decisions SET status = 'open', answer = NULL, note = NULL, decided_at = NULL, decided_by = NULL,"
                             " done_at = NULL, done_by = NULL, result_tests = NULL, result_impact = NULL, updated = ? WHERE id = ?", (now, did))
            self._event(did, now, "reopened", by, {"note": note, "was": item["status"], "answer": item["answer"], "old_note": item["note"]})
            self._db.commit()
            return self._get(did)

    def supersede(self, did: int, by_id: Optional[int], note: str = "", by: str = "user") -> Dict[str, Any]:
        """An ADR's "superseded": question ``did`` (decided or done) is replaced by the later question ``by_id``; None takes it back. The
        old answer and its history stay; the tree and the ADR say which replaced it."""
        note = _text(note, MAX_TEXT, "note")
        with self._writing():
            item = self._get(did)
            now = time.time()
            if by_id is None:
                self._db.execute("UPDATE decisions SET superseded_by = NULL, updated = ? WHERE id = ?", (now, did))
                self._event(did, now, "unsuperseded", by, {"was": item.get("superseded_by"), "note": note})
            else:
                new = self._get(int(by_id))
                if item["status"] not in ("decided", "done"):
                    raise DecisionError("question %d is %s: only a decided question is superseded" % (did, item["status"]))
                if int(new["id"]) <= int(did):
                    raise DecisionError("question %d is not later than %d" % (new["id"], did))
                self._db.execute("UPDATE decisions SET superseded_by = ?, updated = ? WHERE id = ?", (int(by_id), now, did))
                self._event(did, now, "superseded", by, {"by_question": int(by_id), "note": note})
                self._event(int(by_id), now, "supersedes", by, {"question": did, "note": note})
            self._db.commit()
            return self._get(did)

    def trial_followup(self, tid: int, feedback: str, by: str = "user") -> Dict[str, Any]:
        """Vibe Kanban's follow-up: a trial (being tried, or measured) goes on with the person's feedback on the same branch. A measured
        one goes back to trying; its last result stays in the event."""
        feedback = _text(feedback, MAX_TEXT, "feedback", True)
        with self._writing():
            self._follow_up(self._trial(tid), feedback, by, time.time())
            self._db.commit()
            return self._trial(tid)

    def followup_job(self, tid: int, feedback: str, by: str = "user", agent: str = "claude", max_running: int = 1) -> Dict[str, Any]:
        """``trial_followup`` and the job that goes on with it (``job_open``, a build job with the feedback as its note) in one
        transaction: when the job cannot be opened — a job runs on the trial, every slot is taken — the trial keeps its status and its
        measured result (review F5/F8: it went back to trying, results cleared, and the job was refused after). Returns the job; a job
        that never reaches its agent puts the measurement back (``followup_undo``)."""
        feedback = _text(feedback, MAX_TEXT, "feedback", True)
        with self._writing():
            now = time.time()
            self._follow_up(self._trial(tid), feedback, by, now)
            jid = self._open_job(self._trial(tid), feedback, by, agent, max_running, "build", now)
            self._db.commit()
            return self._job(jid)

    def followup_was(self, jid: int) -> Optional[Dict[str, Any]]:
        """The trial as it was before the follow-up that opened job ``jid`` ({status, tests, impact}, from its ``trial_followup`` event,
        written in the same transaction and at the same moment as the job's row), or None for a job no follow-up opened."""
        with self._lock:
            return self._followup_was(self._job(jid))

    def _followup_was(self, j: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for row in self._db.execute("SELECT detail FROM events WHERE decision_id = ? AND action = 'trial_followup' AND at = ? ORDER BY id DESC",
                                    (j["decision_id"], j["created"])):
            d = json.loads(row["detail"] or "{}")
            if d.get("trial") == j["trial_id"]:
                return d.get("was")
        return None

    def followup_undo(self, jid: int, by: str = "dtree") -> bool:
        """A follow-up whose job ``jid`` never reached its agent changed nothing: its trial is measured again with the result it had
        before (``followup_was``) — in one transaction, and only while the trial is still as the follow-up left it (trying, no
        result): a measurement recorded meanwhile, a trial ended meanwhile, stay as they are (reviews of round 4). True when it was
        put back."""
        with self._writing():
            self._db.execute("BEGIN IMMEDIATE")
            j = self._job(jid)
            was = self._followup_was(j) or {}
            t = self._trial(j["trial_id"])
            if was.get("status") != "measured" or not was.get("tests") or not was.get("impact") or \
                    t["status"] != "trying" or t["result_tests"] is not None or t["result_impact"] is not None:
                self._db.rollback()
                return False
            now = time.time()
            self._db.execute("UPDATE trials SET status = 'measured', result_tests = ?, result_impact = ?, updated = ? WHERE id = ?",
                             (was["tests"], was["impact"], now, t["id"]))
            self._db.execute("UPDATE decisions SET updated = ? WHERE id = ?", (now, t["decision_id"]))
            self._event(t["decision_id"], now, "trial_measured", by, {"trial": t["id"], "option": t["option"], "tests": was["tests"],
                                                                      "impact": was["impact"]})
            self._db.commit()
            return True

    def _follow_up(self, t: Dict[str, Any], feedback: str, by: str, now: float) -> None:
        if t["status"] not in TRIAL_OPEN:
            raise DecisionError("trial %d is %s: it has ended" % (t["id"], t["status"]))
        self._db.execute("UPDATE trials SET status = 'trying', result_tests = NULL, result_impact = NULL, updated = ? WHERE id = ?", (now, t["id"]))
        self._event(t["decision_id"], now, "trial_followup", by, {"trial": t["id"], "option": t["option"], "note": feedback,
                                                                   "was": {"status": t["status"], "tests": t["result_tests"], "impact": t["result_impact"]}})

    def comment(self, did: int, note: str, by: str = "user") -> Dict[str, Any]:
        note = _text(note, MAX_TEXT, "note", True)
        with self._writing():
            self._get(did)
            now = time.time()
            self._db.execute("UPDATE decisions SET updated = ? WHERE id = ?", (now, did))
            self._event(did, now, "commented", by, {"note": note})
            self._db.commit()
            return self._get(did)

    def _close(self, did: int, status: str, note: str, by: str) -> Dict[str, Any]:
        note = _text(note, MAX_TEXT, "note")
        with self._writing():
            item = self._get(did)
            if item["status"] != "open":
                raise DecisionError("question %d is already %s" % (did, item["status"]))
            now = time.time()
            self._db.execute("UPDATE decisions SET status = ?, note = ?, decided_at = ?, decided_by = ?, updated = ? WHERE id = ?", (status, note, now, by, now, did))
            self._event(did, now, status, by, {"note": note})
            self._db.commit()
            return self._get(did)

    # ------------------------------------------------------------------ trials
    def try_option(self, did: int, option: str, note: str = "", by: str = "user", variant: Optional[str] = None) -> Dict[str, Any]:
        """Try an option on its own branch: another option of a decided question (the answer stays as it is), or an option of an open
        question (built before choosing; adopting the trial answers it). ``variant`` (an agent's name in a race) lets several trials of
        the same option be open at once, each on ``alt/<id>-<option>--<variant>``. Returns the trial and ``review``: the later questions
        that grew on the answer taken now, which a switch to this option would have to look at again."""
        note = _text(note, MAX_TEXT, "note")
        variant = _text(variant, 40, "variant") or None
        if variant is not None and (not re.match(r"^[A-Za-z0-9_.-]+$", variant) or ".." in variant or variant.endswith((".", ".lock"))):
            raise DecisionError("a variant is letters, digits, _ . - only (no .. and not ending in . or .lock: git takes no such branch)")
        with self._writing():
            self._db.execute("BEGIN IMMEDIATE")                       # another process trying an option meanwhile waits: one branch each
            item = self._get(did)
            if item["status"] not in ("open", "decided", "done"):
                raise DecisionError("question %d is %s: a dismissed question is not tried" % (did, item["status"]))
            keys = [o["key"] for o in item["options"]]
            if option not in keys:
                raise DecisionError("option %r is not one of %s" % (option, ", ".join(keys)))
            if option == item["answer"] and item["status"] != "open":
                raise DecisionError("option %r is the answer already" % option)
            if self._db.execute("SELECT 1 FROM trials WHERE decision_id = ? AND option = ? AND IFNULL(variant, '') = ? AND status IN ('trying', 'measured')",
                                (did, option, variant or "")).fetchone():
                raise DecisionError("option %r of question %d is being tried already%s" % (option, did, " by %s" % variant if variant else ""))
            now = time.time()
            branch = self._branch_for(int(did), option, variant)
            tid = self._db.execute("INSERT INTO trials (decision_id, option, status, branch, created, updated, by, note, variant) VALUES (?, ?, 'trying', ?, ?, ?, ?, ?, ?)",
                                   (did, option, branch, now, now, by, note, variant)).lastrowid
            self._db.execute("UPDATE decisions SET updated = ? WHERE id = ?", (now, did))
            self._event(did, now, "trial", by, {"trial": tid, "option": option, "branch": branch, "note": note, "variant": variant})
            self._db.commit()
            trial = self._trial(tid)
        trial["review"] = self.grown_on(did, item["answer"])
        return trial

    def _branch_for(self, did: int, option: str, variant: Optional[str]) -> str:
        """``alt/<id>-<option>[--<variant>]``, the option's key made fit for git (what git does not take becomes ``-``). When a trial of
        another option of the question has that name already — ``ซ้าย`` and ``ขวา`` both make ``option``, so do ``a/b`` and ``a-b``, and
        option ``a--x`` is option ``a`` of variant ``x`` — a short hash of the key goes after it: two options never share a branch, and
        so never each other's commits (review F1). The same option of the same variant tried again keeps its branch, with its earlier
        work, as before."""
        held = "SELECT 1 FROM trials WHERE decision_id = ? AND branch = ? AND (option != ? OR IFNULL(variant, '') != ?) LIMIT 1"
        prev = self._db.execute("SELECT branch FROM trials WHERE decision_id = ? AND option = ? AND IFNULL(variant, '') = ? ORDER BY id DESC LIMIT 1",
                                (did, option, variant or "")).fetchone()
        if prev and _ref_ok(prev["branch"]) and not self._db.execute(held, (did, prev["branch"], option, variant or "")).fetchone():
            return prev["branch"]                                    # tried again: its branch, with its earlier work, whatever the rule now
        stem = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9_.-]+", "-", option)).strip("-.") or "option"   # git takes no ".." in a name
        tail = "--" + variant if variant else ""
        digest = hashlib.sha1(option.encode("utf-8")).hexdigest()
        room = 80 - len("%d-" % did) - len(tail)                     # every name within gitcode.BRANCH's 80 after "alt/" (review r3)

        def cut(n: int) -> str:
            name = stem[:max(1, room - n)].rstrip("-.") or "o"
            return name[:-len(".lock")] + "-lock" if not tail and name.endswith(".lock") else name   # nor ".lock" at its end
        for name in [cut(0)] + ["%s-%s" % (cut(n + 1), digest[:n]) for n in (6, 10) if room - n - 1 >= 1] + [digest[:min(40, room)]]:
            branch = "alt/%d-%s%s" % (did, name, tail)
            if not self._db.execute(held, (did, branch, option, variant or "")).fetchone():
                return branch
        raise DecisionError("no branch name is free for option %r of question %d" % (option, did))

    def trial_result(self, tid: int, tests: str, impact: str, by: str = "claude") -> Dict[str, Any]:
        """The trial is built and measured: the same tests and scenarios as the answer, so the two can be compared."""
        tests = _text(tests, MAX_TEXT, "tests", True)
        impact = _text(impact, MAX_TEXT, "impact", True)
        with self._writing():
            t = self._trial(tid)
            if t["status"] not in TRIAL_OPEN:
                raise DecisionError("trial %d is %s already" % (tid, t["status"]))
            now = time.time()
            self._db.execute("UPDATE trials SET status = 'measured', result_tests = ?, result_impact = ?, updated = ? WHERE id = ?", (tests, impact, now, tid))
            self._db.execute("UPDATE decisions SET updated = ? WHERE id = ?", (now, t["decision_id"]))
            self._event(t["decision_id"], now, "trial_measured", by, {"trial": tid, "option": t["option"], "tests": tests, "impact": impact})
            self._db.commit()
            return self._trial(tid)

    def trial_end(self, tid: int, outcome: str, note: str = "", by: str = "user", drop_others: bool = False) -> Dict[str, Any]:
        """The user's choice after the measurement. ``adopted``: the option becomes the answer and the question is decided again (the
        branch is to be merged, then ``done``); ``switch``: both are kept, chosen in config (decided again: the switch is to be made);
        ``dropped``: nothing changes (allowed before the measurement too). The earlier answer stays in the events. ``drop_others``
        with adopted: the question's other open trials (the rest of a race) are dropped with it."""
        note = _text(note, MAX_TEXT, "note")
        if outcome not in TRIAL_ENDS:
            raise DecisionError("outcome must be one of %s" % ", ".join(TRIAL_ENDS))
        with self._writing():
            t = self._trial(tid)
            if t["status"] not in TRIAL_OPEN:
                raise DecisionError("trial %d is %s already" % (tid, t["status"]))
            if outcome != "dropped" and t["status"] != "measured":
                raise DecisionError("trial %d is not measured yet: measure it before it is adopted or kept" % tid)
            did, now = t["decision_id"], time.time()
            item = self._get(did)
            if outcome == "switch" and item["status"] == "open":
                raise DecisionError("question %d is open: there is no answer to keep beside this one — adopt it or drop it" % did)
            self._db.execute("UPDATE trials SET status = ?, ended_at = ?, ended_by = ?, ended_note = ?, updated = ? WHERE id = ?", (outcome, now, by, note, now, tid))
            detail = {"trial": tid, "option": t["option"], "branch": t["branch"], "note": note, "was": item["answer"]}
            if outcome == "dropped":
                detail["stopped"] = self._stop_jobs(tid, now)
            self._event(did, now, "trial_" + outcome, by, detail)
            if outcome == "adopted":
                self._db.execute("UPDATE decisions SET status = 'decided', answer = ?, note = ?, decided_at = ?, decided_by = ?, done_at = NULL, done_by = NULL,"
                                 " result_tests = NULL, result_impact = NULL, updated = ? WHERE id = ?",
                                 (t["option"], note or item["note"], now, by, now, did))
                self._event(did, now, "answered", by, {"option": t["option"], "note": note, "trial": tid, "was": item["answer"]})
                if drop_others:
                    for o in self._db.execute("SELECT id, option, branch FROM trials WHERE decision_id = ? AND id != ? AND status IN ('trying', 'measured')",
                                              (did, tid)).fetchall():
                        self._db.execute("UPDATE trials SET status = 'dropped', ended_at = ?, ended_by = ?, ended_note = ?, updated = ? WHERE id = ?",
                                         (now, by, "dropped with the adoption of trial %d" % tid, now, o["id"]))
                        self._event(did, now, "trial_dropped", by, {"trial": o["id"], "option": o["option"], "branch": o["branch"], "note": "with trial %d" % tid,
                                                                    "stopped": self._stop_jobs(o["id"], now)})
            elif outcome == "switch":
                self._db.execute("UPDATE decisions SET status = 'decided', done_at = NULL, updated = ? WHERE id = ?", (now, did))
            else:
                self._db.execute("UPDATE decisions SET updated = ? WHERE id = ?", (now, did))
            self._db.commit()
            return self._trial(tid)

    def _stop_jobs(self, tid: int, now: float) -> List[int]:
        """A dropped trial's running jobs are asked to stop (their runner watches ``stop_requested`` and ends them ``stopped``: E2 of the
        real run — adopting the quickest of a race left the others running and spending). Their ids."""
        ids = [int(r["id"]) for r in self._db.execute("SELECT id FROM jobs WHERE trial_id = ? AND status = 'running'", (tid,))]
        for jid in ids:
            self._db.execute("UPDATE jobs SET stop_requested = 1, updated = ? WHERE id = ?", (now, jid))
        return ids

    def trials(self, status: str = "all", did: Optional[int] = None) -> List[Dict[str, Any]]:
        """``status``: open (trying or measured), one status, or all; oldest first."""
        where, args = [], []
        if status == "open":
            where.append("status IN ('trying', 'measured')")
        elif status in TRIAL_OPEN + TRIAL_ENDS:
            where.append("status = ?"); args.append(status)
        elif status != "all":
            raise DecisionError("status must be open, all or one of %s" % ", ".join(TRIAL_OPEN + TRIAL_ENDS))
        if did is not None:
            where.append("decision_id = ?"); args.append(int(did))
        with self._lock:
            return [dict(r, commits=json.loads(r["commits"] or "[]"))
                    for r in self._db.execute("SELECT * FROM trials" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id", args)]

    def trial(self, tid: int) -> Dict[str, Any]:
        with self._lock:
            return self._trial(tid)

    def _trial(self, tid: int) -> Dict[str, Any]:
        row = self._db.execute("SELECT * FROM trials WHERE id = ?", (_id(tid),)).fetchone()
        if row is None:
            raise KeyError(tid)
        return dict(row, commits=json.loads(row["commits"] or "[]"))

    def grown_on(self, did: int, option: Optional[str]) -> List[int]:
        """The questions that grew, directly or further on, from ``option`` of question ``did`` (from the question itself when None)."""
        nodes = {n["id"]: n for n in self.tree()["nodes"]}
        out, todo = [], [n for n in nodes.values() if n["parent"] == did and n["on_option"] == option]
        while todo:
            n = todo.pop(0)
            out.append(n["id"])
            todo.extend(k for k in nodes.values() if k["parent"] == n["id"])
        return sorted(out)

    # -------------------------------------------------------------------- the AI's jobs (#64)
    _JOB_SET = ("runner_pid", "agent_pid", "worktree", "log", "stop_requested", "cost_usd")

    def job_open(self, tid: int, note: str = "", by: str = "user", agent: str = "claude", max_running: int = 1, kind: str = "build") -> Dict[str, Any]:
        """Hand a trial to an agent. At most ``max_running`` jobs at once (a race runs several), one per trial. A build job needs a trial
        being tried; an update job (bring the branch up to date with the base) also takes a measured or adopted one."""
        note = _text(note, MAX_TEXT, "note")
        with self._writing():
            jid = self._open_job(self._trial(tid), note, by, agent, max_running, kind, time.time())
            self._db.commit()
            return self._job(jid)

    def _open_job(self, t: Dict[str, Any], note: str, by: str, agent: str, max_running: int, kind: str, now: float) -> int:
        tid = t["id"]
        if kind not in JOB_KINDS:
            raise DecisionError("a job is %s" % " or ".join(JOB_KINDS))
        allowed = ("trying",) if kind == "build" else ("trying", "measured", "adopted")
        if t["status"] not in allowed:
            raise DecisionError("trial %d is %s: a %s job needs a trial that is %s" % (tid, t["status"], kind, " or ".join(allowed)))
        mine = self._db.execute("SELECT id FROM jobs WHERE status = 'running' AND trial_id = ?", (tid,)).fetchone()
        if mine:
            raise DecisionError("trial %d is being built already (job %d)" % (tid, mine["id"]))
        running = self._db.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'running'").fetchone()["n"]
        if running >= max(1, int(max_running)):
            raise DecisionError("%d jobs are running: at most %d at once (max_parallel_jobs)" % (running, max_running))
        jid = self._db.execute("INSERT INTO jobs (trial_id, decision_id, status, created, updated, by, note, agent, kind) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                               (tid, t["decision_id"], now, now, by, note, agent, kind)).lastrowid
        self._event(t["decision_id"], now, "ai_sent", by, {"job": jid, "trial": tid, "option": t["option"], "branch": t["branch"], "note": note, "agent": agent,
                                                           "kind": kind})
        return jid

    def job_set(self, jid: int, **fields: Any) -> Dict[str, Any]:
        """The runner's bookkeeping: its pid and the agent's, the worktree, the log, a stop asked for, the cost so far."""
        bad = set(fields) - set(self._JOB_SET)
        if bad:
            raise DecisionError("a job has no field %s" % ", ".join(sorted(bad)))
        with self._writing():
            self._job(jid)
            if fields:
                self._db.execute("UPDATE jobs SET %s, updated = ? WHERE id = ?" % ", ".join(k + " = ?" for k in fields),
                                 list(fields.values()) + [time.time(), int(jid)])
                self._db.commit()
            return self._job(jid)

    def job_end(self, jid: int, status: str, reason: str = "", result: Optional[Dict[str, Any]] = None, by: str = "ai") -> Dict[str, Any]:
        if status not in JOB_ENDS:
            raise DecisionError("a job ends %s" % " / ".join(JOB_ENDS))
        reason = str(reason or "")[:MAX_TEXT]
        with self._writing():
            j = self._job(jid)
            if j["status"] != "running":
                return j
            now = time.time()
            self._db.execute("UPDATE jobs SET status = ?, reason = ?, result = ?, ended_at = ?, updated = ? WHERE id = ?",
                             (status, reason, json.dumps(result or {}, ensure_ascii=False), now, now, int(jid)))
            self._event(j["decision_id"], now, "ai_" + status, by, {"job": int(jid), "trial": j["trial_id"], "note": reason})
            self._db.commit()
            return self._job(jid)

    def jobs(self, tid: Optional[int] = None, did: Optional[int] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        where, args = [], []
        for col, val in (("trial_id", tid), ("decision_id", did), ("status", status)):
            if val is not None:
                where.append(col + " = ?"); args.append(val)
        with self._lock:
            return [self._job_row(r) for r in self._db.execute("SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id", args)]

    def job(self, jid: int) -> Dict[str, Any]:
        with self._lock:
            return self._job(jid)

    def _job(self, jid: int) -> Dict[str, Any]:
        row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (_id(jid),)).fetchone()
        if row is None:
            raise KeyError(jid)
        return self._job_row(row)

    @staticmethod
    def _job_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row, result=json.loads(row["result"] or "{}"), stop_requested=bool(row["stop_requested"]))

    # -------------------------------------------------------------------- checks (verify.py)
    _CHECK_SET = ("status", "ended_at", "runner_pid", "exit_code", "framework", "passed", "failed", "errors", "skipped", "xfailed", "xpassed",
                  "total", "value", "unit", "summary", "tail", "log", "seconds")

    def check_claim(self, key: str, sha: str, name: str, kind: str, command: str, by: str = "", runner_pid: Optional[int] = None,
                    alive: Any = None, force: bool = False) -> Dict[str, Any]:
        """Start a check, or find it done. The latest check with this ``key`` (commit + command + sandbox) is returned with ``claimed``
        False when it ended passed / failed (the result of a command on a commit does not change; ``force`` runs it again) or is running
        in a live process (``alive(pid)``); otherwise a new running row is made and returned with ``claimed`` True. One transaction
        (BEGIN IMMEDIATE), so two runners asking for the same baseline at once run it once."""
        if kind not in ("tests", "measure"):
            raise DecisionError("a check is tests or measure")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM checks WHERE key = ? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
                if row is not None and not force:
                    if row["status"] in ("passed", "failed"):
                        self._db.commit()
                        return dict(row, claimed=False)
                    if row["status"] == "running" and alive is not None and alive(row["runner_pid"]):
                        self._db.commit()
                        return dict(row, claimed=False)
                    if row["status"] == "running":                       # its runner is gone: it never ends by itself
                        now = time.time()
                        self._db.execute("UPDATE checks SET status = 'error', summary = 'the runner is gone', ended_at = ?, updated = ? WHERE id = ?",
                                         (now, now, row["id"]))
                now = time.time()
                cid = self._db.execute("INSERT INTO checks (key, sha, name, kind, command, status, created, updated, by, runner_pid)"
                                       " VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)", (key, sha, name, kind, command, now, now, by, runner_pid)).lastrowid
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
            return dict(self._db.execute("SELECT * FROM checks WHERE id = ?", (cid,)).fetchone(), claimed=True)

    def check_set(self, cid: int, **fields: Any) -> Dict[str, Any]:
        bad = set(fields) - set(self._CHECK_SET)
        if bad:
            raise DecisionError("a check has no field %s" % ", ".join(sorted(bad)))
        if "status" in fields and fields["status"] not in CHECK_STATUSES:
            raise DecisionError("a check's status is one of %s" % ", ".join(CHECK_STATUSES))
        with self._writing():
            if self._db.execute("SELECT 1 FROM checks WHERE id = ?", (int(cid),)).fetchone() is None:
                raise KeyError(cid)
            if fields:
                self._db.execute("UPDATE checks SET %s, updated = ? WHERE id = ?" % ", ".join(k + " = ?" for k in fields), list(fields.values()) + [time.time(), int(cid)])
                self._db.commit()
            return dict(self._db.execute("SELECT * FROM checks WHERE id = ?", (int(cid),)).fetchone())

    def check(self, cid: int) -> Dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT * FROM checks WHERE id = ?", (int(cid),)).fetchone()
            if row is None:
                raise KeyError(cid)
            return dict(row)

    def checks(self, key: Optional[str] = None, sha: Optional[str] = None, latest: bool = True) -> List[Dict[str, Any]]:
        """Checks by ``key`` or by commit; ``latest``: only the newest per key."""
        where, args = [], []
        for col, val in (("key", key), ("sha", sha)):
            if val is not None:
                where.append(col + " = ?"); args.append(val)
        with self._lock:
            rows = [dict(r) for r in self._db.execute("SELECT * FROM checks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id", args)]
        if latest:
            last: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                last[r["key"]] = r
            rows = sorted(last.values(), key=lambda r: r["id"])
        return rows

    # -------------------------------------------------------------------- the chat loop's requests (an agent's rate limits)
    def request_slot(self, agent: str, per_minute: Optional[int] = None, per_day: Optional[int] = None, job: Optional[int] = None,
                     take: bool = True) -> Dict[str, Any]:
        """A slot for one request of ``agent`` to its model's API under its rate limits (``requests_per_minute`` / ``requests_per_day``;
        None = no limit). The agent's requests of the last minute and of the last 24 hours (rolling windows) are counted; when neither
        limit is reached a row is inserted — the slot is taken — and ``ok`` is True. One ``BEGIN IMMEDIATE`` transaction: the runner
        processes of several jobs of the agent share the store, and between them they never take more than the limits.

        Returns {ok, wait_s, why, minute, day}. ``why``: "day" when ``per_day`` is reached (a chat job then ends: its day is used up),
        else "minute" when ``per_minute`` is, None when ok. ``wait_s``: how long until a slot is free — until the oldest request of the
        full window has left it (of both windows when both are full; when a limit was lowered below the count, until as many have left
        as needed), 0 when ok. ``minute`` / ``day``: the requests in the two windows now, the slot just taken included. ``take`` False
        only answers (nothing is taken). Rows older than two days are of no window: they are pruned now and then."""
        agent = _text(agent, 80, "agent", True)
        per_minute, per_day = _limit(per_minute, "per_minute"), _limit(per_day, "per_day")
        if not take:
            with self._lock:
                return self._request_state(agent, per_minute, per_day, time.time())
        with self._writing():
            self._db.execute("BEGIN IMMEDIATE")                      # another runner taking a slot meanwhile waits for this one
            now = time.time()                                        # after the lock: the rows go in the order they were taken
            state = self._request_state(agent, per_minute, per_day, now)
            if not state["ok"]:
                self._db.rollback()
                return state
            rid = self._db.execute("INSERT INTO requests (agent, at, job_id) VALUES (?, ?, ?)",
                                   (agent, now, None if job is None else int(job))).lastrowid
            if rid % REQUEST_PRUNE_EVERY == 0:
                self._db.execute("DELETE FROM requests WHERE at < ?", (now - REQUEST_KEEP_S,))
            self._db.commit()
            state.update(minute=state["minute"] + 1, day=state["day"] + 1)
            return state

    def request_counts(self, agent: str) -> Dict[str, int]:
        """{minute, day}: the requests ``agent``'s chat loop sent in the last minute and in the last 24 hours (rolling)."""
        with self._lock:
            state = self._request_state(str(agent), None, None, time.time())
        return {"minute": state["minute"], "day": state["day"]}

    def _request_state(self, agent: str, per_minute: Optional[int], per_day: Optional[int], now: float) -> Dict[str, Any]:
        row = self._db.execute("SELECT COUNT(*) AS day, IFNULL(SUM(at > ?), 0) AS minute FROM requests WHERE agent = ? AND at > ?",
                               (now - REQUEST_MINUTE_S, agent, now - REQUEST_DAY_S)).fetchone()
        minute, day = int(row["minute"]), int(row["day"])
        full = []
        for why, count, limit, window in (("day", day, per_day, REQUEST_DAY_S), ("minute", minute, per_minute, REQUEST_MINUTE_S)):
            if limit is not None and count >= limit:                 # below the limit once count - limit + 1 of them have left:
                at = self._db.execute("SELECT at FROM requests WHERE agent = ? AND at > ? ORDER BY at, id LIMIT 1 OFFSET ?",
                                      (agent, now - window, count - limit)).fetchone()["at"]     # the (count - limit + 1)-th oldest
                full.append((why, max(0.0, float(at) + window - now)))
        if not full:
            return {"ok": True, "wait_s": 0.0, "why": None, "minute": minute, "day": day}
        wait = math.ceil(max(w for _, w in full) * 1000) / 1000              # up to the millisecond: a wait never ends too early
        return {"ok": False, "wait_s": wait, "why": full[0][0], "minute": minute, "day": day}

    def note_event(self, did: int, action: str, by: str, detail: Dict[str, Any]) -> None:
        """An event from a feature module (``EXTRA_EVENTS``): verified, merged, updated, budget_refused, rate_refused."""
        if action not in EXTRA_EVENTS:
            raise DecisionError("unknown event %r" % action)
        with self._writing():
            self._get(did)
            now = time.time()
            self._event(did, now, action, by, detail)
            self._db.commit()

    # -------------------------------------------------------------------- code
    def record_code(self, did: int, commits: List[Dict[str, Any]], branch: Optional[str] = None, how: str = "recorded", why: str = "",
                    trial: Optional[int] = None, by: str = "claude") -> Dict[str, Any]:
        """Keep which commits are this decision's work (or its trial's), and its branch. ``how``: merged (by ``decisions.py merge``),
        recorded (named by hand), backfilled (matched afterwards from the history made before branches — say why). A commit already
        kept is not added twice. Does not change the question's ``updated``."""
        if how not in CODE_HOW:
            raise DecisionError("how must be one of %s" % ", ".join(CODE_HOW))
        why = _text(why, MAX_TEXT, "why")
        if branch is not None and not _BRANCH.match(branch):
            raise DecisionError("a branch is dec/<id>-<option> or alt/<id>-<option>")
        clean = []
        for c in commits or []:
            sha = str((c or {}).get("sha", "")).lower()
            if not _SHA.match(sha):
                raise DecisionError("%r is not a commit sha" % sha)
            clean.append({"sha": sha, "subject": _text(str(c.get("subject") or "")[:MAX_TITLE], MAX_TITLE, "subject"), "how": how, "merge": bool(c.get("merge"))})
        with self._writing():
            item = self._get(did)
            table, key = ("trials", trial) if trial is not None else ("decisions", did)
            if trial is not None and self._trial(trial)["decision_id"] != did:
                raise DecisionError("trial %d is not on question %d" % (trial, did))
            row = self._db.execute("SELECT commits FROM %s WHERE id = ?" % table, (key,)).fetchone()
            kept = json.loads(row["commits"] or "[]")
            have = {c["sha"][:7] for c in kept}
            added = []
            for c in clean:                                                  # a commit named twice (short and long) is kept once
                if c["sha"][:7] not in have:
                    have.add(c["sha"][:7]); added.append(c)
            kept.extend(added)
            now = time.time()
            self._db.execute("UPDATE %s SET commits = ? WHERE id = ?" % table, (json.dumps(kept, ensure_ascii=False), key))
            if branch is not None and trial is None:
                self._db.execute("UPDATE decisions SET branch = ? WHERE id = ?", (branch, did))
            self._event(did, now, "code", by, {"branch": branch, "commits": [c["sha"][:10] for c in added], "how": how, "why": why, "trial": trial})
            self._db.commit()
            return self._get(did, events=True) if trial is None else self._trial(trial)

    def _event(self, did: int, at: float, action: str, by: str, detail: Dict[str, Any]) -> None:
        self._db.execute("INSERT INTO events (decision_id, at, action, by, detail) VALUES (?, ?, ?, ?, ?)",
                         (did, at, action, by, json.dumps(detail, ensure_ascii=False)))

    # ------------------------------------------------------------------- reads
    def get(self, did: int) -> Dict[str, Any]:
        with self._lock:
            return self._get(did, events=True)

    def _get(self, did: int, events: bool = False) -> Dict[str, Any]:
        row = self._db.execute("SELECT * FROM decisions WHERE id = ?", (_id(did),)).fetchone()
        if row is None:
            raise KeyError(did)
        item = self._row(row)
        if events:
            item["events"] = [{"at": e["at"], "action": e["action"], "by": e["by"], "detail": json.loads(e["detail"] or "{}")}
                              for e in self._db.execute("SELECT * FROM events WHERE decision_id = ? ORDER BY at, id", (int(did),))]
            item["trials"] = [dict(t, commits=json.loads(t["commits"] or "[]")) for t in self._db.execute("SELECT * FROM trials WHERE decision_id = ? ORDER BY id", (int(did),))]
            item["jobs"] = [self._job_row(j) for j in self._db.execute("SELECT * FROM jobs WHERE decision_id = ? ORDER BY id", (int(did),))]
        return item

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["options"] = json.loads(d["options"] or "[]")
        d["refs"] = json.loads(d["refs"] or "[]")
        d["commits"] = json.loads(d.get("commits") or "[]")
        d["texts"] = json.loads(d.get("texts") or "{}")                # words in languages beyond en and th: {"ja": {"title": …}}
        return d

    _SEARCHED = ("title_th", "title_en", "question_th", "question_en", "context_th", "context_en", "note", "answer", "options", "refs",
                 "result_tests", "result_impact", "key", "texts")

    def list(self, status: str = "open", limit: int = 100, q: str = "") -> List[Dict[str, Any]]:
        """``status``: open, decided, done, dismissed, closed (everything not open) or all; newest change first.
        ``q``: words that must all appear somewhere in the question, its answer, note, options, references or results."""
        limit = max(1, min(500, int(limit)))
        where, args = [], []
        if status == "closed":
            where.append("status != 'open'")
        elif status in STATUSES:
            where.append("status = ?"); args.append(status)
        elif status != "all":
            raise DecisionError("status must be one of open, decided, done, dismissed, closed, all")
        for word in _text(q, 200, "q").split():
            like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(" + " OR ".join("IFNULL(%s, '') LIKE ? ESCAPE '\\'" % c for c in self._SEARCHED) + ")")
            args.extend([like] * len(self._SEARCHED))
        sql = "SELECT * FROM decisions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated DESC, id DESC LIMIT ?"
        with self._lock:
            return [self._row(r) for r in self._db.execute(sql, args + [limit])]

    # the fields a question may name another question in ("#41", "(#18)", a reference "#51"); a document anchor (gaps.md#3.13) is not one
    _MENTIONED_IN = ("title_th", "title_en", "question_th", "question_en", "context_th", "context_en", "note", "result_tests", "result_impact", "texts")

    def tree(self) -> Dict[str, Any]:
        """Every question as a decision tree (the user asked for it on 2026-11-01): a question whose text names earlier questions ("#41")
        grows from the latest of them — on that question's chosen option, the branch it followed from; the other questions it names are
        cross links. A question hangs on the option its parent had taken when it was raised (``on_option``; the answer may have changed
        since: a trial adopted, a reopen), the options carry ``was_chosen`` and their ``trials``. A question that names none can be told which one it grew from (``follow``: the same topic, or only the next asked),
        and that link wins over the names. The options are the branches, the answer marks the one taken. Read-only, built at every read."""
        with self._lock:
            rows = [self._row(r) for r in self._db.execute("SELECT * FROM decisions ORDER BY id")]
            seen = {int(r["decision_id"]): int(r["n"]) for r in self._db.execute("SELECT decision_id, COUNT(*) AS n FROM events GROUP BY decision_id")}
            # each question's answers through time (answered sets it, reopened clears it): a later question grew on the answer
            # in force when it was raised, and stays on that branch when the answer changes afterwards
            answers: Dict[int, List[Any]] = {}
            for e in self._db.execute("SELECT decision_id, at, action, detail FROM events WHERE action IN ('answered', 'reopened') ORDER BY at, id"):
                opt = json.loads(e["detail"] or "{}").get("option") if e["action"] == "answered" else None
                answers.setdefault(int(e["decision_id"]), []).append((float(e["at"]), opt))
            trials: Dict[int, List[Dict[str, Any]]] = {}
            ai = {int(j["trial_id"]): {"job": int(j["id"]), "status": j["status"], "agent": j["agent"], "kind": j["kind"] or "build"}
                  for j in self._db.execute("SELECT id, trial_id, status, agent, kind FROM jobs ORDER BY id")}
            for t in self._db.execute("SELECT id, decision_id, option, status, branch, variant FROM trials ORDER BY id"):
                trials.setdefault(int(t["decision_id"]), []).append({k: t[k] for k in ("id", "option", "status", "branch", "variant")} | {"ai": ai.get(int(t["id"]))})
        ids = {int(r["id"]) for r in rows}
        named: Dict[int, set] = {}
        for r in rows:
            text = " ".join(str(r.get(f) or "") for f in self._MENTIONED_IN)
            text += " " + " ".join("%s %s" % (o.get("th", ""), o.get("en", "")) for o in r["options"] if isinstance(o, dict))
            text += " " + " ".join(str(x) for x in r["refs"])
            named[int(r["id"])] = {int(m) for m in _MENTION.findall(text)} & ids - {int(r["id"])}
        links: Dict[int, set] = {i: set() for i in ids}                # i ← earlier question j: i names j, or j names i afterwards
        for i, ns in named.items():
            for j in ns:
                a, b = sorted((i, j))
                links[b].add(a)
        def answer_at(did: int, at: float) -> Optional[str]:
            opt = None
            for when, o in answers.get(did, []):
                if when > at:
                    break
                opt = o
            return opt

        created = {int(r["id"]): float(r["created"]) for r in rows}
        supersedes: Dict[int, List[int]] = {}
        for r in rows:
            if r.get("superseded_by"):
                supersedes.setdefault(int(r["superseded_by"]), []).append(int(r["id"]))
        nodes = []
        for r in rows:
            i = int(r["id"])
            parent = max(links[i]) if links[i] else None
            how, why = ("named", "") if parent is not None else (None, "")
            if r.get("follows") in ids and int(r["follows"]) < i:
                parent, how, why = int(r["follows"]), r.get("follows_how") or "topic", r.get("follows_why") or ""
            taken_before = {o for _, o in answers.get(i, []) if o is not None} - {r.get("answer")}
            mine = trials.get(i, [])
            nodes.append({k: r.get(k) for k in ("id", "key", "kind", "source", "status", "title_th", "title_en", "question_th", "question_en",
                                                  "answer", "created", "decided_at", "done_at", "texts")}
                         | {"options": [{"key": o.get("key"), "th": o.get("th", ""), "en": o.get("en", ""), "texts": o.get("texts") or {}, "recommended": bool(o.get("recommended")),
                                         "pros": o.get("pros") or [], "cons": o.get("cons") or [],
                                         "chosen": r.get("answer") is not None and o.get("key") == r.get("answer"),
                                         "was_chosen": o.get("key") in taken_before,
                                         "trials": [t for t in mine if t["option"] == o.get("key")]} for o in r["options"] if isinstance(o, dict)],
                            "trials": mine, "branch": r.get("branch"), "code": len(r["commits"]),
                            "superseded_by": r.get("superseded_by"), "supersedes": supersedes.get(i, []),
                            "parent": parent,
                            # the option it hangs on: the one it was told (a story's scene after one choice), else the parent's answer when it was raised
                            "on_option": (r.get("follows_option") if r.get("follows_option") and parent == r.get("follows") else answer_at(parent, created[i]))
                                         if parent is not None else None,
                            "parent_how": how, "parent_why": why,
                            "also": sorted(links[i] - {parent}) if parent is not None else [], "events": seen.get(i, 0)})
        return {"nodes": nodes, "roots": [n["id"] for n in nodes if n["parent"] is None], "counts": self.counts()}

    def languages(self) -> List[str]:
        """The languages the questions are written in: en and th (their own fields), then every code found in ``texts``, sorted."""
        with self._lock:
            codes: set = set()
            for r in self._db.execute("SELECT texts, options FROM decisions"):
                codes |= set(json.loads(r["texts"] or "{}"))
                for o in json.loads(r["options"] or "[]"):
                    if isinstance(o, dict):
                        codes |= set(o.get("texts") or {})
        return ["en", "th"] + sorted(c for c in codes if c not in ("en", "th"))

    def ids(self) -> List[int]:
        """Every question's id, oldest first (``list`` stops at 500)."""
        with self._lock:
            return [int(r["id"]) for r in self._db.execute("SELECT id FROM decisions ORDER BY id")]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            out = {s: 0 for s in STATUSES}
            for r in self._db.execute("SELECT status, COUNT(*) AS n FROM decisions GROUP BY status"):
                out[r["status"]] = int(r["n"])
            return out
