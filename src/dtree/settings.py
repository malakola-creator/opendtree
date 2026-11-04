"""ตั้งค่า / settings — the back end of the settings page: what a person may set from the page or the CLI, what is in effect, and
the writes to ``.dtree/config.json``. The user asked for it on 2026-11-02: "ทำหน้า setting กำหนด key, rate limit และอื่นๆได้" (a
settings page where keys, rate limits and the other settings can be set). docs/settings.md has the whole of it.

- ``view(project, local)`` — ``GET /api/settings``: the sections (jobs, budget, chat, verify, server), each a list of FIELDs (kind,
  range, choices, the default, the file's own value, the value in effect, Thai and English labels and help); every agent with its
  FIELDs, where its key comes from (KEY: never the key, nor any part of it), whether it can run here and its requests now; the presets
  an agent can be added from, and which settings each kind of agent has.
- ``update(project, changes, by, local)`` — ``POST /api/settings`` ``{set, unset, add_agent, remove_agent}``. Only the paths of the
  FIELD table; each value checked by its kind, range and choices; then the whole new config (``default_agent`` must be an agent; no
  agent's settings may come to refuse every job of it: ``spend.settings_problem``) before anything is written. The file is written
  atomically, its mode, its notes (``_…``) and every key not touched kept; the running server reloads it in place
  (``Project.reload``); ``<logs>/settings.log`` gets a line (never a key).
- ``set_key`` / ``clear_key`` — ``POST /api/settings/agents/<name>/key``, from this machine only (the page's connection from
  another machine is plain HTTP: a key would cross the network in the clear). The key goes to the agent's ``key_file`` or to
  ``~/.config/dtree/keys/<project>-<agent>.key`` (0600, its directory 0700, written atomically); a removed key's file is deleted
  only when it is dtree's own (under that directory), never another tool's.
- ``test_agent`` — ``POST /api/settings/agents/<name>/test``: whether the agent can run here and, for a chat agent, ``GET
  <base_url>/models`` with its key (10 s); the key is masked out of every text of the answer.
- CLI: ``dtree settings [--json]``, ``dtree set PATH VALUE``, ``dtree unset PATH``, ``dtree key AGENT [--remove]``,
  ``dtree test-agent AGENT``.

From another machine (a write with the token), a setting that could send a key somewhere else or weaken a sandbox — an agent's
``base_url``, ``key_env``, ``sandbox``, ``network``, ``network_allow``, ``credentials``, and ``chat.sandbox`` — is refused with 403 as
the key is: the view shows them read-only there. Before this page no write over the network could change the config at all, so the
token's holder could start sandboxed jobs, not turn the sandbox off or point an agent's key at another host.

Interface used by the rest of dtree (as spend.py / verify.py): ``register_routes(route, project)`` · ``register_cli(sub)`` ·
``run_cli(args, project) -> bool``.
"""
from __future__ import annotations

import contextlib
import copy
import datetime
import fcntl
import getpass
import http.client
import json
import math
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import agents as agents_mod
from . import egress, jobs, spend
from .project import AGENTS, CONFIG, DEFAULTS, _merge
from .store import DecisionError

#: where a key set from the page or ``dtree key`` goes when its agent names no ``key_file`` (``<project>-<agent>.key``)
KEYS_DIR = "~/.config/dtree/keys"
LOG = "settings.log"
MAX_KEY = 4096
#: how long ``test_agent`` waits for ``GET <base_url>/models``
TEST_TIMEOUT_S = 10.0
SCHEMA = Path(__file__).resolve().parent / "schema" / "config.schema.json"
_MISSING = object()
_LOCK = threading.RLock()
_HELD = threading.local()
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_CHANGE_KEYS = ("set", "unset", "add_agent", "remove_agent")


class SettingsError(DecisionError):
    """A change refused, before anything was written: ``path`` names the first bad setting (or None), ``error_th`` says it in Thai,
    ``http_status`` is the API's answer (400; 403 for what only this machine may do; 404 for an agent that is not there)."""
    code = "settings"

    def __init__(self, en: str, th: str = "", path: Optional[str] = None, http_status: int = 400, code: Optional[str] = None) -> None:
        super().__init__(en)
        self.error_th, self.path, self.http_status = th or en, path, http_status
        if code:
            self.code = code


def _cut(text: Any, n: int = 120) -> str:
    s = str(text)
    return s if len(s) <= n else s[:n - 1] + "…"


# ----------------------------------------------------------------------------------------------------------- the FIELD table
class Field:
    """One setting the page shows: its kind (int, number, string, bool, enum, list), range, choices, labels and help.

    ``above``: the minimum itself is not allowed (more than ``lo``). ``local_only``: changed from this machine only. ``warn``: {value:
    (th, en)} — the value weakens safety. ``check(value, path)`` returns the value to keep (normalised) or raises SettingsError.
    ``help_en`` is used when the config schema has no description for the setting (or ``schema`` is False). ``default`` is the
    default when ``project.DEFAULTS`` has none. ``group`` puts an agent's settings together on the page."""

    def __init__(self, key: str, kind: str, th: str, en: str, help_th: str, help_en: str = "", *, lo: Any = None, hi: Any = None,
                 above: bool = False, enum: Optional[List[Any]] = None, nullable: bool = False, readonly: bool = False,
                 restart: bool = False, local_only: bool = False, warn: Optional[Dict[Any, Tuple[str, str]]] = None,
                 check: Optional[Callable[[Any, str], Any]] = None, schema: bool = True, default: Any = None, group: str = "") -> None:
        self.key, self.kind, self.th, self.en, self.help_th, self.help_en = key, kind, th, en, help_th, help_en
        self.lo, self.hi, self.above, self.enum, self.nullable = lo, hi, above, enum, nullable
        self.readonly, self.restart, self.local_only, self.warn, self.check = readonly, restart, local_only, warn or {}, check
        self.schema, self.default, self.group = schema, default, group


def _bad(path: str, en: str, th: str) -> SettingsError:
    return SettingsError("%s %s" % (path, en), "%s %s" % (path, th), path=path)


def _plain_text(maxlen: int, spaces: bool = True, blank: bool = False) -> Callable[[Any, str], Any]:
    """Text of at most ``maxlen`` characters, one line (no control characters), without spaces when ``spaces`` is False."""
    def check(v: Any, path: str) -> Any:
        s = v.strip()
        if not s and not blank:
            raise _bad(path, "cannot be empty: unset it to use the default", "ว่างไม่ได้: ใช้ค่าเริ่มต้นด้วย ↺ (unset)")
        if len(s) > maxlen:
            raise _bad(path, "is longer than %d characters" % maxlen, "ยาวเกิน %d ตัวอักษร" % maxlen)
        if any(ord(c) < 32 or ord(c) == 127 for c in s):
            raise _bad(path, "must be one line (no control characters)", "ต้องเป็นบรรทัดเดียว (ไม่มีอักขระควบคุม)")
        if not spaces and any(c.isspace() for c in s):
            raise _bad(path, "must have no spaces", "ต้องไม่มีช่องว่าง")
        return s
    return check


def _url(v: Any, path: str) -> Any:
    s = _plain_text(500, spaces=False)(v, path)
    u = urllib.parse.urlsplit(s)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise _bad(path, "must be an http:// or https:// address, such as https://api.x.ai/v1",
                   "ต้องเป็นที่อยู่ http:// หรือ https:// เช่น https://api.x.ai/v1")
    return s.rstrip("/")


def _env_name(v: Any, path: str) -> Any:
    """The NAME of an environment variable (GEMINI_API_KEY), never the key: its value is not repeated in the message, in case a key
    was pasted here by mistake."""
    s = v.strip()
    if not re.match(r"^[A-Z_][A-Z0-9_]{0,63}$", s):
        raise _bad(path, "is the name of an environment variable (A–Z, 0–9 and _, such as GEMINI_API_KEY), not the key itself: "
                         "a key is set with the key input or dtree key", "คือชื่อตัวแปรสภาพแวดล้อม (A–Z, 0–9 และ _ เช่น GEMINI_API_KEY) "
                         "ไม่ใช่ตัว key: ตั้ง key ที่ช่อง key หรือด้วย dtree key")
    return s


def _hosts(v: Any, path: str) -> Any:
    out = []
    for h in v:
        h = h.strip().lower()
        if not re.match(r"^[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*\.?$", h) or len(h) > 253:
            raise _bad(path, "takes host names (letters, digits, - and .), such as mybox or mybox.lan",
                       "ใส่ชื่อเครื่อง (ตัวอักษร ตัวเลข - และ .) เช่น mybox หรือ mybox.lan")
        if h not in out:
            out.append(h)
    return out


def _network_allow(v: Any, path: str) -> Any:
    out = [x.strip() for x in v if x.strip()]
    try:
        egress.parse(out)
    except ValueError as exc:
        raise _bad(path, "— %s" % exc, "— %s" % exc)
    return out


def _command(v: Any, path: str) -> Any:
    s = v.strip()
    if not s:
        return None                                                  # an empty test command: test_command (verify.tests null)
    if "\x00" in s or len(s) > 4000:
        raise _bad(path, "must be a command of at most 4000 characters", "ต้องเป็นคำสั่งยาวไม่เกิน 4000 ตัวอักษร")
    return s


def _word(v: Any, path: str) -> Any:
    s = v.strip()
    if not re.match(r"^[A-Za-z][A-Za-z-]{0,39}$", s):
        raise _bad(path, "is one word, such as auto", "เป็นคำเดียว เช่น auto")
    return s


_NONE_BOX = ("คำสั่งของ AI รันบนเครื่องนี้ตรงๆ: ไม่มีกล่องทราย เข้าเน็ตและไฟล์ทั้งหมดได้",
             "the AI's commands run on this machine as they are: no sandbox, the whole network and every file")

#: the project's settings, by section
SECTIONS: List[Tuple[str, str, str, List[Field]]] = [
    ("jobs", "งาน", "Jobs", [
        Field("default_agent", "enum", "AI ที่ใช้เมื่อไม่ได้เลือก", "Default AI", "งานที่ไม่ได้เลือก AI ส่งให้ AI นี้",
              "the agent a job goes to when none is chosen"),
        Field("max_parallel_jobs", "int", "งานที่ทำพร้อมกันได้", "Jobs at once", "จำนวนงาน AI ที่ทำพร้อมกันได้มากที่สุด",
              "how many AI jobs may run at the same time", lo=1, hi=64),
        Field("timeout_s", "number", "เวลาสูงสุดของงานหนึ่ง (วินาที)", "A job's time limit (s)", "งานที่ทำนานกว่านี้ถูกหยุด", lo=1),
    ]),
    ("budget", "งบ", "Budget", [
        Field("budget.month_usd", "number", "งบต่อเดือน ทุก AI รวมกัน ($)", "Monthly budget, all agents ($)",
              "เดือนนี้ใช้ถึงแล้ว งานใหม่ไม่เริ่ม; งานที่เริ่มได้เท่าที่เหลือ; ว่าง = ไม่จำกัด", lo=0, nullable=True),
        Field("budget.job_usd", "number", "เพดานเงินต่องาน ($)", "Dollar cap per job ($)",
              "ใช้เมื่อ AI ไม่ได้ตั้งเอง (Claude Code: --max-budget-usd); ว่าง = ไม่จำกัด", lo=0, above=True, nullable=True),
        Field("budget.job_tokens", "int", "เพดานโทเคนต่องาน", "Token cap per job",
              "ใช้เมื่อ AI ไม่ได้ตั้งเอง (วงแชต, Codex); ว่าง = ไม่จำกัด", lo=1, nullable=True),
    ]),
    ("chat", "วงแชตของ dtree (Muse, Grok, Gemini, local)", "dtree's chat loop (Muse, Grok, Gemini, local)", [
        Field("chat.max_steps", "int", "คำตอบมากที่สุดต่องาน", "Most answers per job", "จำนวนครั้งที่ถามโมเดลในงานหนึ่ง",
              "the most answers of the model in one job", lo=1),
        Field("chat.max_tokens", "int", "โทเคนต่อคำตอบ", "Tokens per answer", "max_tokens ที่ขอในแต่ละคำตอบ",
              "max_tokens asked of the model for each answer", lo=1),
        Field("chat.temperature", "number", "อุณหภูมิ", "Temperature", "ความสุ่มของคำตอบ (0–2)", "the model's sampling temperature (0–2)",
              lo=0, hi=2),
        Field("chat.command_timeout_s", "int", "เวลาสูงสุดของคำสั่ง (วินาที)", "A command's time limit (s)",
              "คำสั่งหนึ่งของเครื่องมือ run", "one command of the run tool", lo=1),
        Field("chat.request_timeout_s", "number", "เวลารอคำตอบหนึ่ง (วินาที)", "Time for one answer (s)",
              "นานกว่านี้งานจบ; โมเดลช้า (เช่น Orin) ต้องใช้มากกว่า 600", lo=1),
        Field("chat.retries", "int", "ถามใหม่เมื่อ API ไม่ว่าง (ครั้ง)", "Retries when the API is busy",
              "เมื่อ API ตอบ 429 (ติดโควตา) หรือ 503 (คนใช้มาก) ถามใหม่ได้กี่ครั้ง", lo=0),
        Field("chat.retry_max_wait_s", "number", "รอได้นานสุดต่อครั้ง (วินาที)", "Longest wait for a retry (s)",
              "API ขอให้รอนานกว่านี้ (โควตาของวัน) งานจบเลย", lo=0),
        Field("chat.sandbox", "enum", "กล่องทรายของคำสั่ง", "The commands' sandbox",
              "bwrap: ไม่มีเน็ต เขียนได้แค่ worktree · none: รันบนเครื่องนี้ตรงๆ",
              "bwrap: no network, only the worktree writable · none: the commands run on this machine as they are",
              enum=["bwrap", "none"], local_only=True, warn={"none": _NONE_BOX}),
        Field("chat.context_chars", "int", "ขนาดบริบท (ตัวอักษร)", "Context size (characters)",
              "ตัดผลเก่าให้บทสนทนาพอดี (โมเดลเล็ก); ว่าง = ไม่ตัด", lo=1, nullable=True),
        Field("chat.result_chars", "int", "ผลของเครื่องมือ (ตัวอักษร)", "A tool result (characters)",
              "ผลของเครื่องมือที่ส่งให้โมเดลยาวได้เท่านี้", "how much of a tool's result the model is given", lo=1, default=12000),
    ]),
    ("verify", "dtree ตรวจเอง", "dtree's own checks", [
        Field("verify.enabled", "bool", "เปิดการตรวจ", "Verification on", "ปิด: dtree ไม่ตรวจเลย (dtree verify ถูกปฏิเสธ)"),
        Field("verify.tests", "string", "คำสั่งทดสอบ", "Test command",
              "{tests} = ไฟล์ทดสอบที่เลือกจากที่เปลี่ยน; ว่าง = test_command (ตรวจด้วยมือเท่านั้น)", nullable=True, check=_command),
        Field("verify.timeout_s", "number", "เวลาสูงสุดต่อการตรวจ (วินาที)", "Time limit per check (s)", "การตรวจหนึ่งครั้ง (ทดสอบหรือวัด)",
              lo=1),
    ]),
    ("server", "เซิร์ฟเวอร์", "Server", [
        Field("server.allowed_hosts", "list", "ชื่อเครื่องที่เปิดหน้านี้ได้", "Allowed host names",
              "ชื่อ (นอกจาก IP และ localhost) ที่ใช้เปิดหน้านี้ได้ เช่น ชื่อเครื่องใน LAN; ใช้ทันทีที่บันทึก", check=_hosts),
        Field("server.host", "string", "ที่อยู่ที่รอรับ", "Listen address",
              "อ่านตอนเริ่มเท่านั้น: แก้ในไฟล์แล้วเริ่ม dtree serve ใหม่ (127.0.0.1 = เครื่องนี้เท่านั้น)",
              "read when the server starts only: change it in the file and start dtree serve again (127.0.0.1: this machine only)",
              readonly=True, restart=True),
        Field("server.port", "int", "พอร์ต", "Port", "อ่านตอนเริ่มเท่านั้น: แก้ในไฟล์แล้วเริ่ม dtree serve ใหม่",
              "read when the server starts only: change it in the file and start dtree serve again", readonly=True, restart=True,
              lo=1, hi=65535),
    ]),
]
_TOP: Dict[str, Field] = {f.key: f for _, _, _, fields in SECTIONS for f in fields}


def _agent_fields() -> Dict[str, Dict[str, Field]]:
    rate = "rate"
    common = {
        "label": Field("label", "string", "ชื่อที่แสดง", "Label", "ชื่อของ AI นี้บนหน้าเว็บ", "the name the page shows",
                       check=_plain_text(100), group="model"),
        "model": Field("model", "string", "โมเดล", "Model", "ชื่อโมเดล (Claude Code, Codex: ว่าง = ค่าเริ่มต้นของโปรแกรม)",
                       "the model's name (Claude Code, Codex: null = the program's own default)", nullable=True,
                       check=_plain_text(200, spaces=False), group="model"),
        "max_budget_usd": Field("max_budget_usd", "number", "เพดานเงินต่องาน ($)", "Dollar cap per job ($)",
                                "ว่าง = budget.job_usd; Claude Code ตรวจระหว่างรอบ (--max-budget-usd)", lo=0, above=True, nullable=True,
                                group="cap"),
        "max_tokens_total": Field("max_tokens_total", "int", "เพดานโทเคนต่องาน", "Token cap per job", "ว่าง = budget.job_tokens",
                                  lo=1, nullable=True, group="cap"),
        "month_usd": Field("month_usd", "number", "เพดานเงินต่อเดือน ($)", "Dollars a month ($)",
                           "AI ที่บอกแค่โทเคน (แชต, Codex) ต้องมีราคาด้วย; 0 = ไม่ให้เริ่มงาน", lo=0, nullable=True, group="cap"),
        "month_tokens": Field("month_tokens", "int", "เพดานโทเคนต่อเดือน", "Tokens a month", "ไม่ต้องมีราคา; เริ่มนับใหม่ทุกวันที่ 1",
                              lo=0, nullable=True, group="cap"),
        "total_usd": Field("total_usd", "number", "เพดานเงินรวม (เครดิต, $)", "Dollars in all (a credit, $)",
                           "นับทุกงานทุกเดือน เช่น เครดิต $150 ของ Muse; AI ที่บอกแค่โทเคนต้องมีราคา", lo=0, nullable=True, group="cap"),
        "total_tokens": Field("total_tokens", "int", "เพดานโทเคนรวม (เครดิต)", "Tokens in all (a credit)", "นับทุกงานทุกเดือน ไม่ต้องมีราคา",
                              lo=0, nullable=True, group="cap"),
        "price_in_per_mtok": Field("price_in_per_mtok", "number", "ราคาขาเข้า ($ ต่อล้านโทเคน)", "Input price ($ per million tokens)",
                                   "ใช้ประมาณค่าใช้จ่ายของ AI ที่บอกแค่โทเคน", lo=0, nullable=True, group="price"),
        "price_out_per_mtok": Field("price_out_per_mtok", "number", "ราคาขาออก ($ ต่อล้านโทเคน)", "Output price ($ per million tokens)",
                                    "ราคาทั้งสองเป็น 0: AI นี้ไม่มีค่าใช้จ่าย", lo=0, nullable=True, group="price"),
    }
    chat = dict(common, **{
        "model": Field("model", "string", "โมเดล", "Model", "ชื่อโมเดลที่ API นี้มี (ปุ่ม ทดสอบ บอกว่ามีหรือไม่)",
                       "the model's name, one the API lists (Test says whether it does)", check=_plain_text(200, spaces=False), group="model"),
        "base_url": Field("base_url", "string", "ที่อยู่ API (base_url)", "API address (base_url)",
                          "API แบบ OpenAI เช่น https://api.x.ai/v1; แก้ได้จากเครื่องนี้เท่านั้น (key ถูกส่งไปที่นี่)", check=_url,
                          local_only=True, group="model"),
        "key_env": Field("key_env", "string", "ตัวแปรที่มี key", "Key variable (key_env)",
                         "ชื่อตัวแปรสภาพแวดล้อมที่เก็บ key (ไม่ใช่ตัว key); ถ้ามีค่า ใช้ก่อนไฟล์ key", nullable=True, check=_env_name,
                         local_only=True, group="model"),
        "requests_per_minute": Field("requests_per_minute", "int", "คำขอต่อนาที", "Requests per minute",
                                     "ทุกงานของ AI นี้รวมกัน ใน 60 วินาทีที่ผ่านมา; Gemini ฟรี: 5 ต่อนาทีต่อโมเดล; ว่าง = ไม่จำกัด",
                                     "the most requests to this agent's API in any 60 s, shared by every job of the agent (Gemini's "
                                     "free tier: 5 a minute per model); null = no limit", lo=1, nullable=True, group=rate),
        "requests_per_day": Field("requests_per_day", "int", "คำขอต่อวัน", "Requests per day",
                                  "ทุกงานของ AI นี้รวมกัน ใน 24 ชั่วโมงที่ผ่านมา; Gemini ฟรี: 20 ต่อวันต่อโมเดล; ว่าง = ไม่จำกัด",
                                  "the most requests to this agent's API in any 24 hours, shared by every job of the agent (Gemini's "
                                  "free tier: 20 a day per model); null = no limit", lo=1, nullable=True, group=rate),
        "retries": Field("retries", "int", "ถามใหม่เมื่อ API ไม่ว่าง (ครั้ง)", "Retries when the API is busy",
                         "เมื่อ API ตอบ 429 หรือ 503; ↺ = chat.retries", lo=0, group=rate),
        "retry_max_wait_s": Field("retry_max_wait_s", "number", "รอได้นานสุดต่อครั้ง (วินาที)", "Longest wait for a retry (s)",
                                  "API ขอให้รอนานกว่านี้ งานจบเลย; ↺ = chat.retry_max_wait_s", lo=0, group=rate),
        "request_timeout_s": Field("request_timeout_s", "number", "เวลารอคำตอบหนึ่ง (วินาที)", "Time for one answer (s)",
                                   "โมเดลช้า (เช่น Orin) ต้องใช้มากกว่า 600; ↺ = chat.request_timeout_s", lo=1, group=rate),
        "max_steps": Field("max_steps", "int", "คำตอบมากที่สุดต่องาน", "Most answers per job", "↺ = chat.max_steps",
                           "the most answers of the model in one job (default chat.max_steps)", lo=1),
        "max_tokens": Field("max_tokens", "int", "โทเคนต่อคำตอบ", "Tokens per answer", "↺ = chat.max_tokens",
                            "max_tokens asked of the model for each answer (default chat.max_tokens)", lo=1),
        "temperature": Field("temperature", "number", "อุณหภูมิ", "Temperature", "ความสุ่มของคำตอบ (0–2); ↺ = chat.temperature",
                             "the model's sampling temperature, 0–2 (default chat.temperature)", lo=0, hi=2),
        "context_chars": Field("context_chars", "int", "ขนาดบริบท (ตัวอักษร)", "Context size (characters)",
                               "ตัดผลเก่าให้พอดี (โมเดลเล็ก); ว่าง = ไม่ตัด; ↺ = chat.context_chars", lo=1, nullable=True),
        "result_chars": Field("result_chars", "int", "ผลของเครื่องมือ (ตัวอักษร)", "A tool result (characters)",
                              "↺ = chat.result_chars", "how much of a tool's result the model is given (default chat.result_chars)",
                              lo=1),
        "sandbox": Field("sandbox", "enum", "กล่องทรายของคำสั่ง", "The commands' sandbox",
                         "bwrap: ไม่มีเน็ต เขียนได้แค่ worktree · none: รันบนเครื่องนี้ตรงๆ; ↺ = chat.sandbox",
                         "the run tool's commands: bwrap (no network, only the worktree writable) or none (on this machine as they "
                         "are); default chat.sandbox", enum=["bwrap", "none"], local_only=True, warn={"none": _NONE_BOX}, schema=False),
    })
    claude = dict(common, **{
        "sandbox": Field("sandbox", "enum", "กล่องทราย", "Sandbox", "bwrap: Claude Code รันในกล่องทราย (ค่าเริ่มต้น) · none: รันบนเครื่องนี้ตรงๆ",
                         enum=["bwrap", "none"], local_only=True,
                         warn={"none": ("Claude Code รันบนเครื่องนี้ตรงๆ: ไม่มีกล่องทราย ใช้ ~/.claude และเน็ตทั้งหมด",
                                        "Claude Code runs on this machine as it is: no sandbox, the person's own ~/.claude and the whole network")}),
        "network": Field("network", "enum", "เครือข่าย", "Network",
                         "proxy: ออกได้แค่ host ที่อนุญาต · host: เน็ตทั้งเครื่อง รวมบริการ localhost · none: ไม่มีเน็ต",
                         enum=["proxy", "host", "none"], local_only=True,
                         warn={"host": ("งานเข้าถึงเน็ตทั้งหมดและบริการ localhost ของเครื่องนี้ (เซิร์ฟเวอร์ dtree ด้วย)",
                                        "the job reaches the whole network and this machine's localhost services (dtree's server too)")}),
        "network_allow": Field("network_allow", "list", "host ที่ให้ออกได้เพิ่ม", "More hosts allowed",
                               "เช่น pypi.org, .example.org (ทั้งโดเมน), host:8443", check=_network_allow, local_only=True),
        "credentials": Field("credentials", "enum", "ไฟล์ล็อกอินของ Claude", "Claude's login file",
                             "ro: อ่านอย่างเดียว (ค่าเริ่มต้น) · rw: เขียนได้ (งานยาวที่ต้องต่ออายุ token)", enum=["ro", "rw"],
                             local_only=True, warn={"rw": ("งานเขียนไฟล์ล็อกอินจริงของ Claude ได้", "the job can write the real Claude login file")}),
        "permission_mode": Field("permission_mode", "string", "โหมดสิทธิ์", "Permission mode", "--permission-mode ของ Claude Code (ค่าเริ่มต้น auto)",
                                 check=_word),
    })
    codex = dict(common, **{
        "sandbox": Field("sandbox", "enum", "กล่องทรายของ Codex", "Codex's sandbox",
                         "workspace-write (ค่าเริ่มต้น) · read-only · danger-full-access: ไม่มีกล่องทราย",
                         "Codex's own sandbox: workspace-write (default), read-only, or danger-full-access (none)",
                         enum=["workspace-write", "read-only", "danger-full-access"], local_only=True, schema=False,
                         warn={"danger-full-access": ("Codex รันคำสั่งบนเครื่องนี้ตรงๆ: ไม่มีกล่องทราย มีเน็ต",
                                                      "Codex runs its commands on this machine as they are: no sandbox, with the network")}),
    })
    return {"chat": chat, "claude": claude, "codex": codex}


_AGENT = _agent_fields()
_PRICED = ["month_usd", "month_tokens", "total_usd", "total_tokens", "price_in_per_mtok", "price_out_per_mtok"]
#: the settings each kind of agent has here. The per-job caps go where they act: ``max_budget_usd`` is Claude Code's own
#: (``--max-budget-usd``), ``max_tokens_total`` the chat loop's and Codex's (spend.py; docs/sandbox-and-budget.md §6)
KINDS: Dict[str, List[str]] = {
    "chat": ["label", "model", "base_url", "key_env", "requests_per_minute", "requests_per_day", "retries", "retry_max_wait_s",
             "request_timeout_s", "max_tokens_total"] + _PRICED + ["max_steps", "max_tokens", "temperature", "context_chars",
                                                                   "result_chars", "sandbox"],
    "claude": ["label", "model", "max_budget_usd"] + _PRICED + ["sandbox", "network", "network_allow", "credentials", "permission_mode"],
    "codex": ["label", "model", "max_tokens_total"] + _PRICED + ["sandbox"],
}
#: an agent of a kind dtree does not know (agents.make refuses it): only what every kind has
_ANY_KIND = ["label", "model"] + _PRICED
#: the chat loop's settings an agent may set for itself over the ``chat`` section (agents/chat.py ``_settings``)
_CHAT_LOOP = ("max_steps", "max_tokens", "temperature", "command_timeout_s", "sandbox", "context_chars", "result_chars", "request_timeout_s",
              "retries", "retry_max_wait_s")
_CHAT_CODE = {"max_steps": 80, "max_tokens": 4096, "temperature": 0.2, "command_timeout_s": 600, "sandbox": "bwrap", "context_chars": None,
              "result_chars": 12000, "request_timeout_s": 600, "retries": 8, "retry_max_wait_s": 300}
_KIND_CODE = {"claude": {"sandbox": "bwrap", "network": "proxy", "network_allow": [], "credentials": "ro", "permission_mode": "auto"},
              "codex": {"sandbox": "workspace-write"}}


def _field_of(kind: Any, key: str) -> Optional[Field]:
    keys = KINDS.get(kind, _ANY_KIND)
    if key not in keys:
        return None
    return _AGENT.get(kind, _AGENT["chat"])[key]


# ----------------------------------------------------------------------------------------------------------- reading
_SCHEMA_CACHE: Dict[str, Any] = {}


def _schema() -> Dict[str, Any]:
    if "s" not in _SCHEMA_CACHE:
        try:
            _SCHEMA_CACHE["s"] = json.loads(SCHEMA.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _SCHEMA_CACHE["s"] = {}
    return _SCHEMA_CACHE["s"]


def _schema_node(path: str) -> Dict[str, Any]:
    s = _schema()
    if path.startswith("agents."):
        return ((s.get("$defs") or {}).get("agent") or {}).get("properties", {}).get(path.rsplit(".", 1)[1]) or {}
    node: Dict[str, Any] = s
    for part in path.split("."):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            node = (s.get("$defs") or {}).get(ref[len("#/$defs/"):]) or {}
        node = (node.get("properties") or {}).get(part) or {}
    return node


def _help_en(f: Field, path: str) -> str:
    said = _schema_node(path).get("description") if f.schema else None
    return str(said) if said else f.help_en


def _dig(d: Any, parts: List[str]) -> Any:
    for p in parts:
        if not isinstance(d, dict) or p not in d:
            return _MISSING
        d = d[p]
    return d


def config_path(project: Any) -> Path:
    return Path(project.root) / CONFIG


def _read_own(project: Any) -> Tuple[Dict[str, Any], bool, Optional[int]]:
    """The file's own settings (what it says, not merged), whether it exists, and its mode. A file that is not a JSON object is
    refused: it is never written over."""
    path = config_path(project)
    try:
        st = os.stat(path)
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, False, None
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError("%s cannot be read: %s" % (path, exc), "อ่าน %s ไม่ได้: %s" % (path, exc))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SettingsError("%s is not JSON (%s): fix it by hand first" % (path, exc), "%s ไม่ใช่ JSON (%s): แก้ไฟล์ด้วยมือก่อน" % (path, exc))
    if not isinstance(data, dict):
        raise SettingsError("%s is not a JSON object: fix it by hand first" % path, "%s ไม่ใช่ JSON object: แก้ไฟล์ด้วยมือก่อน" % path)
    return data, True, stat.S_IMODE(st.st_mode)


def _merged(project: Any, own: Dict[str, Any]) -> Dict[str, Any]:
    """What a project made from ``own`` would have as its config (``Project.__init__``)."""
    cfg = _merge(DEFAULTS, own)
    cfg["name"] = cfg.get("name") or Path(project.root).name
    return cfg


class _Proposed:
    """The project with a config not written yet: what ``spend`` and ``verify`` are asked about before anything is written."""

    def __init__(self, project: Any, config: Dict[str, Any]) -> None:
        self._project, self.config, self.root = project, config, project.root

    def __getattr__(self, name: str) -> Any:                         # store, git, base … of the real project
        return getattr(self._project, name)

    def path(self, value: str) -> Path:
        p = Path(os.path.expanduser(str(value).format(name=self.config["name"])))
        return p if p.is_absolute() else (Path(self.root) / p).resolve()

    def agent(self, name: Optional[str]) -> Dict[str, Any]:
        name = name or self.config["default_agent"]
        a = (self.config.get("agents") or {}).get(name)
        if not a:
            raise KeyError(name)
        return dict(a, name=name)


def _tracked(project: Any) -> bool:
    try:
        out = project.git.out("ls-files", "--error-unmatch", "--", str(CONFIG))
    except Exception:
        return False
    return bool(out and out.strip())


# ----------------------------------------------------------------------------------------------------------- the view
def _code_default(config: Dict[str, Any], kind: Any, key: str, name: str) -> Any:
    """An agent's setting when neither its preset nor the file says: the ``chat`` section, the budget's job caps, the code's own."""
    if key == "label":
        return name
    if kind == "chat" and key in _CHAT_LOOP:
        chat = config.get("chat") if isinstance(config.get("chat"), dict) else {}
        return copy.deepcopy(chat.get(key, _CHAT_CODE.get(key)))
    if key == "max_budget_usd":
        return (config.get("budget") or {}).get("job_usd")
    if key == "max_tokens_total":
        return (config.get("budget") or {}).get("job_tokens")
    return copy.deepcopy((_KIND_CODE.get(kind) or {}).get(key))


def _agent_default(config: Dict[str, Any], name: str, kind: Any, key: str) -> Any:
    pre = AGENTS.get(name)
    if pre is not None and pre.get("kind") == kind and pre.get(key) is not None:
        return copy.deepcopy(pre[key])
    return _code_default(config, kind, key, name)


def _agent_value(config: Dict[str, Any], name: str, cfg: Dict[str, Any], key: str) -> Any:
    kind = cfg.get("kind")
    if kind == "chat" and key in _CHAT_LOOP:                         # the agent's own wins, even null (context_chars: no cut)
        return copy.deepcopy(cfg[key]) if key in cfg else _code_default(config, kind, key, name)
    v = cfg.get(key)
    return copy.deepcopy(v) if v is not None and v != "" else _code_default(config, kind, key, name)


def _field(f: Field, path: str, value: Any, default: Any, own: Any, local: bool, enum: Optional[List[Any]] = None) -> Dict[str, Any]:
    if f.kind == "list" and isinstance(value, str):                  # one name written as text is that one name (server.allowed_hosts)
        value = [value]
    warn = None
    for bad, (th, en) in f.warn.items():
        if value == bad:
            warn = {"th": th, "en": en}
    out = {"path": path, "kind": f.kind, "min": f.lo, "max": f.hi, "enum": enum if enum is not None else f.enum, "nullable": f.nullable,
           "default": default, "own": None if own is _MISSING else own, "set": own is not _MISSING, "value": value,
           "th": f.th, "en": f.en, "help_th": f.help_th, "help_en": _help_en(f, path), "restart": f.restart,
           "readonly": bool(f.readonly or (f.local_only and not local)), "warn": warn}
    if f.above:
        out["exclusive_min"] = True
    if f.local_only:
        out["local_only"] = True
    if f.group:
        out["group"] = f.group
    if f.restart:
        out["note"] = {"th": "อ่านตอนเริ่มเซิร์ฟเวอร์เท่านั้น: แก้ใน .dtree/config.json แล้วเริ่ม dtree serve ใหม่",
                       "en": "read when the server starts only: change it in .dtree/config.json and start dtree serve again"}
    elif f.local_only and not local:
        out["note"] = {"th": "แก้ได้จากเครื่องนี้เท่านั้น (localhost หรือ dtree set)", "en": "changed from this machine only (localhost, or dtree set)"}
    return out


def _local_url(base_url: Any) -> bool:
    host = urllib.parse.urlsplit(str(base_url or "")).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def _expand(path: Any) -> Path:
    return Path(os.path.expanduser(str(path)))


def key_info(cfg: Dict[str, Any], root: Any = None) -> Dict[str, Any]:
    """KEY: where an agent's key comes from — ``via`` env (``key_env`` set in this process's environment, which the chat loop reads
    first), file (``key_file`` holds one), or none — never the key, nor any part of it: only whether it is there, the file's size and
    mode. ``needs``: a chat agent with a remote ``base_url`` (Claude Code and Codex use their own login; a server on this machine needs
    none). A relative ``key_file`` is the project's (``root``): a job's runner works there."""
    kind = cfg.get("kind")
    out: Dict[str, Any] = {"needs": False, "via": "none", "env": None, "env_set": False, "file": None, "file_set": False, "bytes": None,
                           "mode": None}
    if kind != "chat":
        login = {"claude": ("Claude Code ใช้การล็อกอินของตัวเอง (claude แล้ว /login)", "Claude Code uses its own login (run claude, then /login)"),
                 "codex": ("Codex ใช้การล็อกอินของตัวเอง (codex login)", "Codex uses its own login (codex login)")}.get(kind)
        if login:
            out["note"] = {"th": login[0], "en": login[1]}
        return out
    env = cfg.get("key_env") or None
    raw = os.environ.get(env) if env else None
    out.update(needs=not _local_url(cfg.get("base_url")), env=env, env_set=bool(raw and raw.strip()))
    kf = cfg.get("key_file")
    if kf:
        p = _expand(kf)
        p = p if p.is_absolute() else Path(root or os.getcwd()) / p
        out["file"] = str(p)
        try:
            st = os.stat(p)
            out["bytes"], out["mode"] = st.st_size, "%o" % stat.S_IMODE(st.st_mode)
            if stat.S_ISREG(st.st_mode) and os.access(p, os.R_OK):
                with open(p, "rb") as fh:                            # whether it holds a key: read here, never kept or sent
                    out["file_set"] = bool(fh.read(MAX_KEY + 64).strip())
            if stat.S_IMODE(st.st_mode) & 0o077:
                out["warn"] = {"th": "ไฟล์ key นี้คนอื่นในเครื่องอ่านได้ (โหมด %s): chmod 600 หรือตั้ง key ใหม่" % out["mode"],
                               "en": "other users of this machine can read this key file (mode %s): chmod 600, or set the key again" % out["mode"]}
        except OSError:
            pass
    out["via"] = "env" if raw else "file" if out["file_set"] else "none"
    if raw and out["file_set"]:
        out["note"] = {"th": "ใช้ $%s ก่อน ไฟล์ key ใช้เมื่อไม่มีตัวแปรนี้" % env, "en": "$%s is used first; the key file when it is not set" % env}
    return out


def _requests(project: Any, name: str, kind: Any) -> Optional[Dict[str, Any]]:
    """The agent's requests in the last minute and day (the store's ``request_counts``, stream B's rate limits), or None."""
    fn = getattr(project.store, "request_counts", None)
    if kind != "chat" or not callable(fn):
        return None
    try:
        got = fn(name)
    except Exception:
        return None
    return dict(got) if isinstance(got, dict) else None


def _agent_rows(project: Any) -> Dict[str, Dict[str, Any]]:
    """Whether each agent can run here and why not, as the agents list says it (``jobs.agent_list``: its own check, the spend
    settings, and whatever else refuses a job before it starts)."""
    try:
        return {r["name"]: r for r in jobs.agent_list(project)}
    except Exception as exc:                                          # one broken agent must not hide the settings page
        return {name: {"available": False, "reason": "%s: %s" % (type(exc).__name__, exc)} for name in project.config.get("agents") or {}}


def _agent_view(project: Any, own: Dict[str, Any], name: str, cfg: Dict[str, Any], row: Optional[Dict[str, Any]], local: bool) -> Dict[str, Any]:
    config = project.config
    kind = cfg.get("kind")
    mine = own.get("agents") if isinstance(own.get("agents"), dict) else {}
    own_a = mine.get(name) if isinstance(mine.get(name), dict) else {}
    fields = []
    for key in KINDS.get(kind, _ANY_KIND):
        f = _field_of(kind, key)
        fields.append(_field(f, "agents.%s.%s" % (name, key), _agent_value(config, name, cfg, key), _agent_default(config, name, kind, key),
                             own_a.get(key, _MISSING), local))
    row = row or {}
    return {"name": name, "kind": kind, "label": cfg.get("label") or name, "preset": name in AGENTS, "model": cfg.get("model"),
            "available": bool(row.get("available")), "reason": row.get("reason") or "", "fields": fields,
            "key": key_info(cfg, project.root), "requests": _requests(project, name, kind)}


def view(project: Any, local: bool = True) -> Dict[str, Any]:
    """The settings page's view (GET /api/settings): the file, the sections, the agents, the presets and each kind's settings. The
    values in effect are the running project's; ``own`` is what the file says now. ``local``: the client is on this machine (keys,
    and the settings that only this machine may change, are read-only to another)."""
    path = config_path(project)
    file_error = None
    try:
        own, exists, _mode = _read_own(project)
    except SettingsError as exc:
        own, exists, file_error = {}, True, {"th": exc.error_th, "en": str(exc)}
    config = project.config
    names = list((config.get("agents") or {}).keys())
    sections = []
    for sid, th, en, fields in SECTIONS:
        items = []
        for f in fields:
            parts = f.key.split(".")
            default = _dig(DEFAULTS, parts)
            default = copy.deepcopy(f.default if default is _MISSING else default)
            value = _dig(config, parts)
            items.append(_field(f, f.key, default if value is _MISSING else copy.deepcopy(value), default, _dig(own, parts), local,
                                enum=names if f.key == "default_agent" else None))
        sections.append({"id": sid, "th": th, "en": en, "fields": items})
    rows = _agent_rows(project)
    out = {"file": str(path), "exists": exists, "tracked": _tracked(project), "local": bool(local), "name": config.get("name"),
           "keys_dir": KEYS_DIR, "in_sync": file_error is None and _merged(project, own) == config, "sections": sections,
           "agents": [_agent_view(project, own, n, a, rows.get(n), local) for n, a in (config.get("agents") or {}).items() if isinstance(a, dict)],
           "presets": [{"name": n, "kind": a.get("kind"), "label": a.get("label") or n, "in_config": n in names} for n, a in AGENTS.items()],
           "kinds": {k: list(v) for k, v in KINDS.items()}}
    if file_error:
        out["file_error"] = file_error
    return out


# ----------------------------------------------------------------------------------------------------------- checking a value
def _check_value(f: Field, path: str, v: Any) -> Any:
    """The value to write for ``path``, checked by its kind, range and choices; SettingsError naming the path otherwise."""
    if isinstance(v, str) and f.kind in ("string",) and f.nullable and not v.strip():
        v = None                                                      # an emptied text box: none (null)
    if v is None:
        if f.nullable:
            return None
        raise _bad(path, "cannot be null: unset it to use the default", "ว่างไม่ได้: ใช้ค่าเริ่มต้นด้วย ↺ (unset)")
    k = f.kind
    if k == "bool":
        if not isinstance(v, bool):
            raise _bad(path, "is true or false", "ต้องเป็น true หรือ false")
        return v
    if k in ("int", "number"):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            raise _bad(path, "must be a number", "ต้องเป็นตัวเลข")
        if k == "int":
            if isinstance(v, float):
                if not v.is_integer():
                    raise _bad(path, "must be a whole number", "ต้องเป็นจำนวนเต็ม")
                v = int(v)
        if f.lo is not None and (v <= f.lo if f.above else v < f.lo):
            raise _bad(path, "must be more than %s" % f.lo if f.above else "must be %s or more" % f.lo,
                       "ต้องมากกว่า %s" % f.lo if f.above else "ต้องไม่น้อยกว่า %s" % f.lo)
        if f.hi is not None and v > f.hi:
            raise _bad(path, "must be %s or less" % f.hi, "ต้องไม่เกิน %s" % f.hi)
        return v
    if k == "enum":
        if not isinstance(v, str) or v not in (f.enum or []):
            raise _bad(path, "is one of: %s" % ", ".join(map(str, f.enum or [])), "ต้องเป็นหนึ่งใน: %s" % ", ".join(map(str, f.enum or [])))
        return v
    if k == "list":
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or len(v) > 100:
            raise _bad(path, "must be a list of names (at most 100)", "ต้องเป็นรายการชื่อ (ไม่เกิน 100)")
        v = [x for x in v if x.strip()]
        return f.check(v, path) if f.check else v
    if not isinstance(v, str):
        raise _bad(path, "must be text", "ต้องเป็นข้อความ")
    return f.check(v, path) if f.check else _plain_text(2000)(v, path)


def _resolve(path: Any, merged: Dict[str, Any]) -> Tuple[List[str], Field, Optional[str]]:
    """A path of the FIELD table → (its parts in the config, its Field, the agent's name or None)."""
    if not isinstance(path, str) or not path or len(path) > 200:
        raise SettingsError("a setting's path is text, such as chat.retries", "ชื่อค่าต้องเป็นข้อความ เช่น chat.retries", path=_cut(path, 80))
    if path in _TOP:
        return path.split("."), _TOP[path], None
    if path.startswith("agents.") and "." in path[len("agents."):]:
        name, _, key = path[len("agents."):].rpartition(".")
        a = (merged.get("agents") or {}).get(name)
        if not isinstance(a, dict):
            raise SettingsError("%s: there is no agent %s" % (path, name), "%s: ไม่มี AI ชื่อ %s" % (path, name), path=path)
        kind = a.get("kind")
        f = _field_of(kind, key)
        if f is None:
            if key == "key_file":
                raise SettingsError("%s is set with the key: the page's key input, or dtree key %s" % (path, name),
                                    "%s ตั้งพร้อม key: ช่อง key บนหน้าเว็บ หรือ dtree key %s" % (path, name), path=path)
            if key == "kind":
                raise SettingsError("%s is chosen when the agent is added" % path, "%s เลือกตอนเพิ่ม AI" % path, path=path)
            raise SettingsError("%s is not a setting of a %s agent here (%s)" % (path, kind, ", ".join(KINDS.get(kind, _ANY_KIND))),
                                "%s ไม่ใช่ค่าของ AI ชนิด %s ที่หน้านี้แก้ได้" % (path, kind), path=path)
        return ["agents", name, key], f, name
    raise SettingsError("%s is not a setting the settings page changes (dtree settings lists them)" % path,
                        "%s ไม่ใช่ค่าที่หน้าตั้งค่าแก้ได้ (ดูรายการด้วย dtree settings)" % path, path=path)


def _refuse_remote(path: str) -> SettingsError:
    return SettingsError("%s is changed from this machine only (http://localhost, or dtree set on it): from another machine it could "
                         "send a key elsewhere or weaken a sandbox" % path,
                         "%s แก้ได้จากเครื่องนี้เท่านั้น (http://localhost หรือ dtree set บนเครื่องนี้)" % path, path=path,
                         http_status=403, code="local_only")


# ----------------------------------------------------------------------------------------------------------- writing
@contextlib.contextmanager
def _locked(project: Any) -> Iterator[None]:
    """One change at a time: a lock in this process and, across processes (the server and the CLI), flock on ``.dtree``. A thread
    that holds a project's lock may take it again (a key's change writes the config inside it)."""
    root = str(Path(project.root).resolve())
    held = getattr(_HELD, "roots", None)
    if held is None:
        held = _HELD.roots = {}
    if held.get(root):
        held[root] += 1
        try:
            yield
        finally:
            held[root] -= 1
        return
    with _LOCK:
        d = Path(root) / ".dtree"
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held[root] = 1
            try:
                yield
            finally:
                held.pop(root, None)
        finally:
            os.close(fd)                                              # the flock goes with it


def _atomic_write(path: Path, data: bytes, mode: Optional[int]) -> None:
    """``data`` into ``path`` through a new temporary file beside it (``O_CREAT|O_EXCL``, made 0600 and given ``mode`` before a
    byte is written; ``mode`` None: what the umask gives a new file), then fsync and ``os.replace``: a reader sees the old file or the
    new one, never a part, and a key is never in a file others can read, not even for a moment."""
    d = path.parent
    fd, tmp = -1, None
    for _ in range(20):
        tmp = d / (".%s.%s.tmp" % (path.name, secrets.token_hex(6)))
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         0o666 if mode is None else 0o600)
            break
        except FileExistsError:
            continue
    if fd < 0 or tmp is None:
        raise OSError("no temporary file could be made in %s" % d)
    try:
        try:
            if mode is not None:
                os.fchmod(fd, mode)
            view_ = memoryview(data)
            while view_:
                view_ = view_[os.write(fd, view_):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    try:                                                              # the rename itself kept across a crash
        dfd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _write_config(project: Any, data: Dict[str, Any], mode: Optional[int]) -> None:
    """The config written whole (``json.dumps(indent=1)``, as ``dtree init`` writes it), atomically, with the mode it had; a new one
    with what the umask gives (as ``dtree init``'s)."""
    path = config_path(project)
    real = Path(os.path.realpath(str(path)))                         # a linked config: the file it points to is written
    try:
        real.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(real, (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8"), mode)
    except OSError as exc:
        raise SettingsError("%s could not be written (%s): nothing changed" % (path, exc), "เขียน %s ไม่ได้ (%s): ไม่มีอะไรเปลี่ยน" % (path, exc),
                            http_status=500)


def _log(project: Any, local: bool, by: str, parts: List[str]) -> None:
    """A line in ``<logs>/settings.log``: when, from where (local or remote), by whom, and what was set (with the values) and unset —
    never a key (keys are set by their own route, which logs only the file and its size)."""
    if not parts:
        return
    try:
        d = project.path(project.config.get("logs") or ".dtree/logs")
        d.mkdir(parents=True, exist_ok=True)
        line = "%s · %s · %s · %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "local" if local else "remote",
                                        re.sub(r"\s+", " ", str(by))[:40], " · ".join(parts))
        with open(d / LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:                                                   # a log that cannot be written loses nothing else
        pass


def _shown(v: Any) -> str:
    return _cut(json.dumps(v, ensure_ascii=False), 200)


def _container(new: Dict[str, Any], parts: List[str], path: str) -> Dict[str, Any]:
    """The object in ``new`` that holds the last part of ``parts``, made where it is missing (or null)."""
    d = new
    for p in parts[:-1]:
        nxt = d.get(p)
        if nxt is None:
            nxt = d[p] = {}
        if not isinstance(nxt, dict):
            raise SettingsError("%s: the file's %s is not an object; fix it by hand first" % (path, p),
                                "%s: %s ในไฟล์ไม่ใช่ object; แก้ไฟล์ด้วยมือก่อน" % (path, p), path=path)
        d = nxt
    return d


def _drop(new: Dict[str, Any], parts: List[str]) -> bool:
    """Remove ``parts`` from ``new``; an object it leaves empty goes too (a section, a preset agent's entry), never an agent of the
    file's own. Whether anything was there."""
    chain = [new]
    for p in parts[:-1]:
        nxt = chain[-1].get(p)
        if not isinstance(nxt, dict):
            return False
        chain.append(nxt)
    if parts[-1] not in chain[-1]:
        return False
    del chain[-1][parts[-1]]
    for i in range(len(parts) - 1, 0, -1):
        holder, key = chain[i - 1], parts[i - 1]
        if chain[i] or (i == 2 and parts[0] == "agents" and key not in AGENTS):
            break
        del holder[key]
    return True


def _add_agent(new: Dict[str, Any], spec: Any, merged: Dict[str, Any]) -> Tuple[str, str]:
    """``add_agent: {name, from}`` — from a preset (its settings copied: dtree's own default for Claude Code's disallowed tools
    included, since a new agent inherits nothing) or from a kind (``chat``: an OpenAI-compatible API, to fill in). A preset removed
    with null comes back by its own name."""
    if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
        raise SettingsError("add_agent is {name, from}", "add_agent คือ {name, from}", path="add_agent")
    name, frm = spec["name"].strip(), spec.get("from")
    frm = name if frm is None and name in AGENTS else frm
    path = "agents.%s" % name
    if not _NAME.match(name):
        raise SettingsError("%s: a name is 1–40 letters, digits, - or _, starting with a letter or digit" % path,
                            "%s: ชื่อเป็นตัวอักษร ตัวเลข - หรือ _ 1–40 ตัว ขึ้นต้นด้วยตัวอักษรหรือตัวเลข" % path, path="add_agent.name")
    if name in (merged.get("agents") or {}):
        raise SettingsError("%s is there already" % path, "มี AI ชื่อ %s แล้ว" % name, path="add_agent.name")
    agents = new.get("agents")
    if agents is None:
        agents = new["agents"] = {}
    if not isinstance(agents, dict):
        raise SettingsError("the file's agents is not an object; fix it by hand first", "agents ในไฟล์ไม่ใช่ object; แก้ไฟล์ด้วยมือก่อน", path="agents")
    if name in AGENTS:                                                # one of dtree's own, removed with null: brought back as it is
        if frm != name:
            raise SettingsError("%s is the name of one of dtree's own agents: add it back from %s, or choose another name" % (name, name),
                                "%s เป็นชื่อ AI ของ dtree เอง: เพิ่มกลับจาก %s หรือใช้ชื่ออื่น" % (name, name), path="add_agent.name")
        agents.pop(name, None)
        return name, name
    if frm in AGENTS:
        body = {k: copy.deepcopy(v) for k, v in AGENTS[frm].items() if v is not None}
        body["label"] = "%s · %s" % (AGENTS[frm].get("label") or frm, name)
    elif frm == "chat":
        body = {"kind": "chat", "label": name}
    else:
        raise SettingsError("add_agent.from is one of dtree's agents (%s) or chat (an OpenAI-compatible API)" % ", ".join(AGENTS),
                            "add_agent.from ต้องเป็น AI ของ dtree (%s) หรือ chat (API แบบ OpenAI)" % ", ".join(AGENTS), path="add_agent.from")
    agents[name] = body
    return name, str(frm)


def _remove_agent(new: Dict[str, Any], name: Any, merged: Dict[str, Any]) -> str:
    """``remove_agent``: one of dtree's own is written as null (the file's settings for it go too); one of the file's own is deleted."""
    if not isinstance(name, str) or name not in (merged.get("agents") or {}):
        raise SettingsError("remove_agent: there is no agent %s" % _cut(name, 60), "remove_agent: ไม่มี AI ชื่อ %s" % _cut(name, 60),
                            path="remove_agent")
    agents = new.get("agents")
    if agents is None:
        agents = new["agents"] = {}
    if not isinstance(agents, dict):
        raise SettingsError("the file's agents is not an object; fix it by hand first", "agents ในไฟล์ไม่ใช่ object; แก้ไฟล์ด้วยมือก่อน", path="agents")
    if name in AGENTS:
        agents[name] = None
    else:
        agents.pop(name, None)
    return name


def _problem_path(problem: str, name: str, touched: List[str]) -> str:
    m = re.search(r"\b(agents\.%s\.[a-z_]+|budget\.[a-z_]+)\b" % re.escape(name), problem)
    if m:
        return m.group(1)
    mine = [p for p in touched if p.startswith("agents.%s." % name)]
    return mine[0] if mine else "agents.%s" % name


def _check_whole(project: Any, before: Dict[str, Any], after: Dict[str, Any], touched: List[str]) -> None:
    """The whole new config, before anything is written: ``default_agent`` must be an agent, and no agent may come to have settings
    that refuse every job of it (``spend.settings_problem``: a cap or price that is not a number, a dollar cap that cannot be counted
    without prices, a cap of 0 …). A problem an agent had already is left for the person to fix: it does not block other changes."""
    names = after.get("agents") or {}
    if after.get("default_agent") not in names:
        raise SettingsError("default_agent %s is not an agent (%s)" % (_cut(after.get("default_agent"), 60), ", ".join(names) or "none"),
                            "default_agent %s ไม่ใช่ AI ที่มี (%s)" % (_cut(after.get("default_agent"), 60), ", ".join(names) or "ไม่มี"),
                            path="default_agent")
    now, was = _Proposed(project, after), _Proposed(project, before)
    for name, a in names.items():
        if not isinstance(a, dict):
            continue
        try:
            problem = spend.settings_problem(now, dict(a, name=name))
        except Exception as exc:                                     # a setting spend cannot even read is a problem too
            problem = "%s: %s" % (type(exc).__name__, exc)
        if not problem:
            continue
        old = (before.get("agents") or {}).get(name)
        try:
            had = spend.settings_problem(was, dict(old, name=name)) if isinstance(old, dict) else None
        except Exception as exc:
            had = "%s: %s" % (type(exc).__name__, exc)
        if problem == had:
            continue
        raise SettingsError("not saved: %s" % problem, "ไม่ได้บันทึก: ตั้งแบบนี้งานของ %s จะไม่เริ่มเลย — %s" % (name, problem),
                            path=_problem_path(problem, name, touched))
    if any(p.startswith("verify.") for p in touched):
        from . import verify                                          # verify's own reading of its section, as a job would
        try:
            verify.settings(now)
        except DecisionError as exc:
            raise SettingsError("not saved: %s" % exc, "ไม่ได้บันทึก: %s" % exc, path=next(p for p in touched if p.startswith("verify.")))


def update(project: Any, changes: Any, by: str = "user", local: bool = True) -> Dict[str, Any]:
    """Apply ``{"set": {path: value}, "unset": [path], "add_agent": {name, from}, "remove_agent": name}`` to ``.dtree/config.json``:
    add, set, unset, remove, in that order; every path of the FIELD table only, every value checked, then the whole new config
    (``_check_whole``); nothing is written when anything is refused (SettingsError naming the first bad path). Written atomically
    (its mode, notes and every key not touched kept, ``json.dumps(indent=1)``), the project reloaded in place, a line logged.
    Answers the new view, with ``changed`` (the paths changed)."""
    if not isinstance(changes, dict):
        raise SettingsError("a change is a JSON object: {set, unset, add_agent, remove_agent}", "การแก้ต้องเป็น JSON object: {set, unset, add_agent, remove_agent}")
    other = [k for k in changes if k not in _CHANGE_KEYS]
    if other:
        raise SettingsError("%s is not part of a change (set, unset, add_agent, remove_agent)" % _cut(other[0], 60),
                            "%s ไม่ใช่ส่วนของการแก้ (set, unset, add_agent, remove_agent)" % _cut(other[0], 60), path=_cut(other[0], 60))
    sets, unsets = changes.get("set") or {}, changes.get("unset") or []
    if not isinstance(sets, dict):
        raise SettingsError("set is an object {path: value}", "set ต้องเป็น object {path: value}", path="set")
    if not isinstance(unsets, list) or not all(isinstance(p, str) for p in unsets):
        raise SettingsError("unset is a list of paths", "unset ต้องเป็นรายการชื่อค่า", path="unset")
    both = [p for p in unsets if p in sets]
    if both:
        raise SettingsError("%s is both set and unset" % both[0], "%s ถูกทั้งตั้งและล้าง" % both[0], path=both[0])
    with _locked(project):
        own, exists, mode = _read_own(project)
        new = copy.deepcopy(own)
        before = _merged(project, own)
        done: List[Tuple[List[str], str, str]] = []                  # (parts in the file, path, what the log says)
        if changes.get("add_agent") is not None:
            name, frm = _add_agent(new, changes["add_agent"], before)
            done.append((["agents", name], "agents.%s" % name, "add agents.%s from %s" % (name, frm)))
        mid = _merged(project, new)
        for path, value in sets.items():
            parts, f, _agent = _resolve(path, mid)
            if f.readonly:
                raise SettingsError("%s is read when the server starts: change it in .dtree/config.json and start dtree serve again" % path,
                                    "%s อ่านตอนเริ่มเซิร์ฟเวอร์เท่านั้น: แก้ใน .dtree/config.json แล้วเริ่ม dtree serve ใหม่" % path, path=path)
            if f.local_only and not local:
                raise _refuse_remote(path)
            if path == "default_agent":                              # an agent's name; that it is one is checked with the rest
                if not isinstance(value, str) or not value.strip():
                    raise _bad(path, "is an agent's name", "ต้องเป็นชื่อ AI")
                v = value.strip()
            else:
                v = _check_value(f, path, value)
            _container(new, parts, path)[parts[-1]] = v
            done.append((parts, path, "set %s=%s" % (path, _shown(v))))
        for path in unsets:
            parts, f, _agent = _resolve(path, mid)
            if f.readonly:
                raise SettingsError("%s is read when the server starts: change it in .dtree/config.json" % path,
                                    "%s อ่านตอนเริ่มเซิร์ฟเวอร์เท่านั้น: แก้ใน .dtree/config.json" % path, path=path)
            if f.local_only and not local:
                raise _refuse_remote(path)
            _drop(new, parts)
            done.append((parts, path, "unset %s" % path))
        if changes.get("remove_agent") is not None:
            gone = _remove_agent(new, changes["remove_agent"], mid)
            mine = [p for p in list(sets) + list(unsets) if p.startswith("agents.%s." % gone)]
            if mine:
                raise SettingsError("%s: agents.%s is removed in the same change" % (mine[0], gone), "%s: AI %s ถูกลบในการแก้เดียวกัน" % (mine[0], gone),
                                    path=mine[0])
            done.append((["agents", gone], "agents.%s" % gone, "remove agents.%s" % gone))
        after = _merged(project, new)
        _check_whole(project, before, after, [p for _parts, p, _say in done])
        done = [d for d in done if _dig(own, d[0]) != _dig(new, d[0])]   # what the file says differently now
        if new != own:
            _write_config(project, new, mode)
            _log(project, local, by, [say for _parts, _p, say in done])
            project.reload()
    out = view(project, local=local)
    out["changed"] = [p for _parts, p, _say in done]
    return out


# ----------------------------------------------------------------------------------------------------------- keys
def _chat_agent(project: Any, agent: Any) -> Tuple[str, Dict[str, Any]]:
    if not isinstance(agent, str) or not agent:
        raise SettingsError("an agent's name is needed", "ต้องระบุชื่อ AI", path="agent", http_status=400)
    try:
        cfg = project.agent(agent)
    except KeyError:
        raise SettingsError("there is no agent %s" % _cut(agent, 60), "ไม่มี AI ชื่อ %s" % _cut(agent, 60), path="agents.%s" % _cut(agent, 60),
                            http_status=404)
    if cfg.get("kind") != "chat":
        how = {"claude": ("Claude Code logs in by itself: run claude on this machine, then /login", "Claude Code ล็อกอินเอง: รัน claude บนเครื่องนี้ แล้ว /login"),
               "codex": ("Codex logs in by itself: codex login", "Codex ล็อกอินเอง: codex login")}.get(
                   cfg.get("kind"), ("only a chat agent takes a key here", "ตั้ง key ได้เฉพาะ AI ชนิด chat"))
        raise SettingsError("agents.%s: %s" % (agent, how[0]), "agents.%s: %s" % (agent, how[1]), path="agents.%s.key_file" % agent)
    return agent, cfg


def clean_key(key: Any) -> str:
    """A key as it is kept: one line of printable characters without spaces, 1–4096 of them (the spaces and line break around a
    pasted key are dropped). The messages never repeat the key, nor any part of it."""
    if not isinstance(key, str):
        raise SettingsError("a key is text", "key ต้องเป็นข้อความ", path="key")
    k = key.strip()
    why = None
    if not k:
        why = ("is empty", "ว่าง")
    elif len(k) > MAX_KEY:
        why = ("is longer than %d characters (%d)" % (MAX_KEY, len(k)), "ยาวเกิน %d ตัวอักษร (%d)" % (MAX_KEY, len(k)))
    elif any(c.isspace() for c in k):
        why = ("has a space or a line break inside: a key is one line", "มีช่องว่างหรือขึ้นบรรทัดใหม่อยู่ข้างใน: key เป็นบรรทัดเดียว")
    elif any(not ("!" <= c <= "~") for c in k):
        why = ("has a character that is not printable ASCII", "มีอักขระที่ไม่ใช่ ASCII ที่พิมพ์ได้")
    if why:
        raise SettingsError("the key %s; nothing was written" % why[0], "key %s; ไม่ได้บันทึก" % why[1], path="key")
    return k


def _keys_dir() -> Path:
    return _expand(KEYS_DIR)


def _ours(path: Path) -> bool:
    """Whether a key file is dtree's own: under ~/.config/dtree/keys, judged by where it is (its directory resolved), not where a
    link in it points. Another tool's key file (~/.config/ut-meta/meta.key) is never deleted."""
    try:
        where = Path(os.path.realpath(str(path.parent))) / path.name
        keys = Path(os.path.realpath(str(_keys_dir())))
    except OSError:
        return False
    return keys in where.parents


def _short(path: Path) -> str:
    """A path as the config keeps it: ``~/…`` under the home directory (the same file for the person on another machine too)."""
    home = Path(os.path.expanduser("~"))
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-.") or "dtree"


def _key_target(project: Any, name: str, cfg: Dict[str, Any]) -> Path:
    kf = cfg.get("key_file")
    if kf:
        p = _expand(kf)
        return p if p.is_absolute() else (Path(project.root) / p)
    return _keys_dir() / ("%s-%s.key" % (_safe(project.config.get("name") or Path(project.root).name), _safe(name)))


def _make_dir(d: Path, ours: bool) -> None:
    if ours:
        for p in (d.parent, d):                                       # ~/.config/dtree and its keys: the person's alone
            if not p.exists():
                p.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(d), 0o700)
    elif not d.exists():
        d.mkdir(mode=0o700, parents=True, exist_ok=True)


def _key_answer(project: Any, name: str, **extra: Any) -> Dict[str, Any]:
    cfg = project.agent(name)
    row = _agent_rows(project).get(name) or {}
    return dict({"agent": name, "key": key_info(cfg, project.root), "available": bool(row.get("available")),
                 "reason": row.get("reason") or ""}, **extra)


def set_key(project: Any, agent: str, key: Any, by: str = "user", local: bool = True) -> Dict[str, Any]:
    """Keep a chat agent's key in a file: its ``key_file`` when it has one, else ``~/.config/dtree/keys/<project>-<agent>.key`` (the
    directory 0700), and ``key_file`` set to it in the config. The file is 0600, written atomically (``O_CREAT|O_EXCL`` temporary file,
    ``fchmod 600``, ``os.replace``). Answers the agent's KEY and whether it can run now; never the key."""
    name, cfg = _chat_agent(project, agent)
    k = clean_key(key)
    target = _key_target(project, name, cfg)
    with _locked(project):
        try:
            _make_dir(target.parent, _ours(target) and target.parent == _keys_dir())
            if target.is_dir():
                raise SettingsError("agents.%s.key_file %s is a directory" % (name, target),
                                    "agents.%s.key_file %s เป็นไดเรกทอรี" % (name, target), path="agents.%s.key_file" % name)
            _atomic_write(target, (k + "\n").encode("ascii"), 0o600)
            size = target.stat().st_size
        except OSError as exc:                                        # the message names the file, never the key
            raise SettingsError("the key file %s could not be written (%s): nothing changed" % (target, exc.strerror or type(exc).__name__),
                                "เขียนไฟล์ key %s ไม่ได้ (%s): ไม่มีอะไรเปลี่ยน" % (target, exc.strerror or type(exc).__name__),
                                path="agents.%s.key_file" % name, http_status=500)
        finally:
            k = ""
        said = ["key set agents.%s → %s (%d bytes)" % (name, _short(target), size)]
        if not cfg.get("key_file"):
            own, _exists, mode = _read_own(project)
            new = copy.deepcopy(own)
            _container(new, ["agents", name, "key_file"], "agents.%s.key_file" % name)["key_file"] = _short(target)
            _write_config(project, new, mode)
            said.append("set agents.%s.key_file=%s" % (name, _shown(_short(target))))
            project.reload()
        _log(project, local, by, said)
    extra: Dict[str, Any] = {}
    env = cfg.get("key_env")
    if env and os.environ.get(env):
        extra["warn"] = {"th": "$%s มีค่าอยู่ในเครื่องเซิร์ฟเวอร์และใช้ก่อนไฟล์นี้: ลบตัวแปรนั้น หรือล้าง key_env ถ้าจะใช้ key นี้" % env,
                         "en": "$%s is set where the server runs and is used before this file: unset it, or clear key_env, to use this key" % env}
    return _key_answer(project, name, **extra)


def clear_key(project: Any, agent: str, by: str = "user", local: bool = True) -> Dict[str, Any]:
    """Forget a chat agent's key file: ``key_file`` unset in the config, and the file deleted only when it is dtree's own (under
    ``~/.config/dtree/keys``) and no other agent of the project names it. A key in ``key_env`` is the environment's: it stays."""
    name, cfg = _chat_agent(project, agent)
    if not cfg.get("key_file"):
        return _key_answer(project, name, deleted=False, file=None)
    target = _key_target(project, name, cfg)
    deleted = False
    with _locked(project):
        others = [n for n, a in (project.config.get("agents") or {}).items()
                  if n != name and isinstance(a, dict) and a.get("key_file") and _same(_key_target(project, n, a), target)]
        own, _exists, mode = _read_own(project)
        new = copy.deepcopy(own)
        _drop(new, ["agents", name, "key_file"])
        if ((_merged(project, new).get("agents") or {}).get(name) or {}).get("key_file"):   # still named (a preset's own): null
            _container(new, ["agents", name, "key_file"], "agents.%s.key_file" % name)["key_file"] = None
        if new != own:
            _write_config(project, new, mode)
        project.reload()
        ours = _ours(target)
        if ours and not others:
            try:
                os.unlink(str(target))
                deleted = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise SettingsError("key_file is unset, but %s could not be deleted (%s)" % (target, exc.strerror),
                                    "ล้าง key_file แล้ว แต่ลบ %s ไม่ได้ (%s)" % (target, exc.strerror), path="agents.%s.key_file" % name,
                                    http_status=500)
        why = "deleted" if deleted else "kept (not dtree's own file)" if not ours else "kept (%s use it too)" % ", ".join(others) \
            if others else "already gone"
        _log(project, local, by, ["key removed agents.%s · %s %s" % (name, _short(target), why)])
    return _key_answer(project, name, deleted=deleted, file=str(target))


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except OSError:
        return str(a) == str(b)


# ----------------------------------------------------------------------------------------------------------- test an agent
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is the API's answer, never followed: the key is not sent anywhere else (as the chat loop does)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib's hook
        return None


def mask(text: Any, secret: Optional[str]) -> Any:
    """``text`` with ``secret`` masked out: the whole of it, and any run of 4 or more of its characters (an API that echoes a key's
    end, "…abcd", shows a part of it). Strings inside lists and dicts too."""
    if isinstance(text, dict):
        return {k: mask(v, secret) for k, v in text.items()}
    if isinstance(text, list):
        return [mask(v, secret) for v in text]
    if not isinstance(text, str) or not secret or not text:
        return text
    out = text.replace(secret, "•••")
    n = 4
    if len(secret) < n:
        return out
    grams = {secret[i:i + n] for i in range(len(secret) - n + 1)}
    cover = [False] * len(out)
    for i in range(len(out) - n + 1):
        if out[i:i + n] in grams:
            for j in range(i, i + n):
                cover[j] = True
    if not any(cover):
        return out
    pieces, i = [], 0
    while i < len(out):
        if cover[i]:
            while i < len(out) and cover[i]:
                i += 1
            pieces.append("•••")
        else:
            pieces.append(out[i])
            i += 1
    return "".join(pieces)


def _api_message(body: bytes, fallback: str) -> str:
    text = body.decode("utf-8", "replace")
    msg: Any = None
    try:
        j = json.loads(text)
    except ValueError:
        j = None
    if isinstance(j, list) and j and isinstance(j[0], dict):          # Google answers a list of one error at times
        j = j[0]
    if isinstance(j, dict):
        e = j.get("error")
        msg = (e.get("message") or e.get("status") or e.get("code")) if isinstance(e, dict) else e
        msg = msg or j.get("message") or j.get("detail")
    return _cut(str(msg or text.strip() or fallback), 300)


def _models(base_url: Any, key: Optional[str], model: Any) -> Dict[str, Any]:
    """``GET <base_url>/models`` with the key (Authorization: Bearer), at most ``TEST_TIMEOUT_S``: {status, models, url, ms,
    model_listed} or {status, error}. No redirect is followed."""
    url = str(base_url or "").rstrip("/") + "/models"
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return {"status": None, "error": "base_url is not an http(s) address", "url": url}
    headers = {"Accept": "application/json", "User-Agent": "dtree-settings-test"}
    if key:
        headers["Authorization"] = "Bearer " + key
    opener = urllib.request.build_opener(_NoRedirect)
    t0 = time.time()
    try:
        with opener.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=TEST_TIMEOUT_S) as r:
            status, body = r.status, r.read(4_000_000)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(20000) or b""
        except (OSError, http.client.HTTPException):
            body = b""
        out = {"status": exc.code, "error": _api_message(body, str(exc.reason)), "url": url, "ms": int((time.time() - t0) * 1000)}
        if 300 <= exc.code < 400:
            out["error"] = "the API answered %d (a redirect, not followed: is base_url right?) %s" % (exc.code, out["error"])
        return out
    except (urllib.error.URLError, OSError, socket.timeout, http.client.HTTPException, ValueError) as exc:
        why = getattr(exc, "reason", None) or exc
        return {"status": None, "error": "no answer: %s" % _cut(why, 200), "url": url, "ms": int((time.time() - t0) * 1000)}
    ms = int((time.time() - t0) * 1000)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return {"status": status, "models": None, "error": "the answer is not JSON: is base_url the API's address?", "url": url, "ms": ms}
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) and isinstance(data, dict):
        items = data.get("models")
    if not isinstance(items, list):
        return {"status": status, "models": None, "error": "the answer has no list of models", "url": url, "ms": ms}
    ids = {str(m.get("id") or m.get("name") or "") for m in items if isinstance(m, dict)}
    listed = None
    if model and ids:
        listed = str(model) in ids or ("models/" + str(model)) in ids
    return {"status": status, "models": len(items), "url": url, "ms": ms, "model_listed": listed}


def test_agent(project: Any, agent: str) -> Dict[str, Any]:
    """Whether the agent can run here now (as the agents list says) and, for a chat agent, whether its API answers ``GET /models``
    with its key: ``{ok, available, reason, check}``, ``check`` = ``{status: 200, models: 60}`` or ``{status: 401, error: …}``, the
    key masked out of every text. Claude Code and Codex: whether they can run only (``check`` null)."""
    try:
        cfg = project.agent(agent)
    except KeyError:
        raise SettingsError("there is no agent %s" % _cut(agent, 60), "ไม่มี AI ชื่อ %s" % _cut(agent, 60), path="agents.%s" % _cut(agent, 60),
                            http_status=404)
    row = _agent_rows(project).get(agent) or {}
    out: Dict[str, Any] = {"agent": agent, "available": bool(row.get("available")), "reason": row.get("reason") or "", "check": None}
    key = None
    if cfg.get("kind") == "chat":
        try:
            a = agents_mod.make(cfg, project)
            key = a._key() if callable(getattr(a, "_key", None)) else None   # the key a job would send, read as the chat loop reads it
        except Exception:
            key = None
        out["check"] = _models(cfg.get("base_url"), key, cfg.get("model"))
        out["check"]["key"] = "sent" if key else "none"
    c = out["check"] or {}
    ok = out["available"] and (not c or (c.get("status") == 200 and c.get("models") is not None))
    out["ok"] = bool(ok)
    if not ok:
        if c and c.get("status") is None:
            en, th = "no answer from the API (%s)" % c.get("error"), "API ไม่ตอบ (%s)" % c.get("error")
        elif c and (c.get("status") != 200 or c.get("error")):
            en = "the API answered %s: %s" % (c.get("status"), c.get("error") or "?")
            th = "API ตอบ %s: %s" % (c.get("status"), c.get("error") or "?")
        else:
            en = out["reason"] or "not available here"
            th = "ใช้ที่นี่ไม่ได้: %s" % en
        out["error"], out["error_th"] = "agents.%s: %s" % (agent, en), "agents.%s: %s" % (agent, th)
    return mask(out, key)


# ----------------------------------------------------------------------------------------------------------- the routes
def _remote_key(groups: Tuple[str, ...], port: Optional[int]) -> Dict[str, Any]:
    agent = groups[0] if groups else "AGENT"
    where = "http://localhost:%s" % (port or 5077)
    return {"ok": False, "code": "local_only", "path": "agents.%s.key_file" % agent,
            "error": "a key is set from this machine only (%s), or with: dtree key %s — the page's connection from another machine is "
                     "plain HTTP, and the key would cross the network in the clear" % (where, agent),
            "error_th": "ตั้ง key ได้จากเครื่องนี้เท่านั้น (%s) หรือ dtree key %s" % (where, agent)}


def register_routes(route: Callable, project: Any) -> None:
    @route("GET", r"/api/settings")
    def settings_get(q, b, local=True):
        return view(project, local=local)

    @route("POST", r"/api/settings")
    def settings_post(q, b, local=True):
        return update(project, b, by="user", local=local)

    @route("POST", r"/api/settings/agents/([^/]+)/key", local_only=_remote_key)
    def key_post(q, b, name, local=True):
        if b.get("remove") is True:
            return clear_key(project, name, by="user", local=local)
        if "key" not in b:
            raise SettingsError("the body is {\"key\": \"…\"} or {\"remove\": true}", "ส่ง {\"key\": \"…\"} หรือ {\"remove\": true}", path="key")
        return set_key(project, name, b.get("key"), by="user", local=local)

    @route("POST", r"/api/settings/agents/([^/]+)/test")
    def test_post(q, b, name):
        return test_agent(project, name)


# ----------------------------------------------------------------------------------------------------------- the CLI
def register_cli(sub: Any) -> None:
    p = sub.add_parser("settings", help="every setting the settings page shows: in effect, in the file, the default; agents and keys")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("set", help="set one setting in .dtree/config.json, checked as the page checks it: dtree set chat.retries 5")
    p.add_argument("path", help="chat.retries, budget.month_usd, agents.gemini.requests_per_minute, … (dtree settings lists them)")
    p.add_argument("value", help="read as JSON when it is (5, null, true, [\"a\"]), else as text")
    p = sub.add_parser("unset", help="take a setting out of .dtree/config.json: its default is used again")
    p.add_argument("path")
    p = sub.add_parser("key", help="set a chat agent's key (read without echo, or one line of stdin), kept in a 0600 file; never printed")
    p.add_argument("agent")
    p.add_argument("--remove", action="store_true", help="forget the key file (deleted only when it is dtree's own)")
    p = sub.add_parser("test-agent", help="whether an agent can run here, and for a chat agent whether GET /models answers with its key")
    p.add_argument("agent")


def _shown_value(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def field_line(f: Dict[str, Any], width: int = 36) -> str:
    """One FIELD for the terminal: the path, the value in effect, then what the file says and the default, read-only, ⚠."""
    tags = []
    if f.get("set"):
        tags.append("in the file" + ("" if f.get("own") == f.get("value") else ": %s (not in effect yet)" % _shown_value(f.get("own"))))
        if f.get("default") != f.get("own"):
            tags.append("default %s" % _shown_value(f.get("default")))
    if f.get("readonly"):
        tags.append("read-only" + (": read at start (restart dtree serve)" if f.get("restart") else ""))
    if f.get("warn"):
        tags.append("⚠ " + f["warn"]["en"])
    return "  %-*s %s%s" % (width, f["path"], _shown_value(f.get("value")), ("   · " + " · ".join(tags)) if tags else "")


def key_line(k: Dict[str, Any]) -> str:
    """KEY for the terminal, without the key: where it comes from, the file's mode and size."""
    if k.get("note") and not k.get("needs") and k.get("via") == "none" and not k.get("file"):
        return k["note"]["en"]
    parts = []
    if k.get("via") == "env":
        parts.append("key from $%s" % k.get("env"))
    elif k.get("via") == "file":
        parts.append("key in %s (mode %s, %s bytes)" % (k.get("file"), k.get("mode"), k.get("bytes")))
    else:
        parts.append("no key" + (" (one is needed)" if k.get("needs") else " needed (a server on this machine)"))
        if k.get("env"):
            parts.append("$%s is not set" % k["env"])
        if k.get("file"):
            parts.append("%s holds none" % k["file"] if k.get("bytes") is not None else "%s is not there" % k["file"])
    if k.get("warn"):
        parts.append("⚠ " + k["warn"]["en"])
    return " · ".join(parts)


def _cli_value(project: Any, path: str, raw: str) -> Any:
    """``dtree set``'s VALUE: JSON when it parses (5, null, true, ["a", "b"]), else the text. A text setting keeps the text as typed
    (``dtree set agents.x.model 3`` is "3"); a list takes "a,b" too."""
    try:
        v = json.loads(raw)
    except ValueError:
        v = raw
    try:
        _parts, f, _agent = _resolve(path, project.config)
    except SettingsError:
        return v                                                      # update() says what is wrong with the path
    if f.kind in ("string", "enum") and v is not None and not isinstance(v, str):
        return raw
    if f.kind == "list" and isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return v


def _find(out: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    for s in out.get("sections") or []:
        for f in s["fields"]:
            if f["path"] == path:
                return f
    for a in out.get("agents") or []:
        for f in a["fields"]:
            if f["path"] == path:
                return f
    return None


def _print_view(v: Dict[str, Any]) -> None:
    head = "settings of %s · %s" % (v.get("name"), v["file"])
    head += " · " + ("tracked by git: commit it to keep the settings" if v.get("tracked") else "not tracked by git") if v.get("exists") else \
        " · not there yet (dtree set writes it)"
    print(head)
    if v.get("file_error"):
        print("⚠ " + v["file_error"]["en"])
    elif not v.get("in_sync"):
        print("⚠ the file changed since this process read it: the values below are this process's")
    for s in v["sections"]:
        print("[%s] %s / %s" % (s["id"], s["th"], s["en"]))
        for f in s["fields"]:
            print(field_line(f))
    print("[agents] AI")
    for a in v["agents"]:
        print("  %s · %s · %s · %s" % (a["name"], a["kind"], a["label"], "available" if a["available"] else "not available: " + a["reason"]))
        print("    " + key_line(a["key"]))
        if a.get("requests"):
            print("    requests: this minute %s · today %s" % (a["requests"].get("minute"), a["requests"].get("day")))
        for f in a["fields"]:
            print("  " + field_line(f, 40))
    print("presets for dtree settings' add: %s · or chat (an OpenAI-compatible API)" % ", ".join(p["name"] for p in v["presets"]))


def run_cli(args: Any, project: Any) -> bool:
    cmd = args.cmd
    if cmd == "settings":
        v = view(project, local=True)
        if args.json:
            print(json.dumps(v, ensure_ascii=False, indent=1))
        else:
            _print_view(v)
        return True
    if cmd in ("set", "unset"):
        change = {"set": {args.path: _cli_value(project, args.path, args.value)}} if cmd == "set" else {"unset": [args.path]}
        out = update(project, change, by="cli", local=True)
        f = _find(out, args.path)
        print((field_line(f, 0).strip() if f else args.path) + ("" if out["changed"] else "   (no change)"))
        return True
    if cmd == "key":
        if args.remove:
            out = clear_key(project, args.agent, by="cli")
        else:
            if sys.stdin is not None and sys.stdin.isatty():
                key = getpass.getpass("key for agents.%s (not shown): " % args.agent)
            else:
                key = (sys.stdin.readline() if sys.stdin is not None else "")
            out = set_key(project, args.agent, key, by="cli")
            key = None
        line = "agents.%s: %s · %s" % (args.agent, key_line(out["key"]), "available" if out["available"] else "not available: " + out["reason"])
        if out.get("warn"):
            line += "\n⚠ " + out["warn"]["en"]
        print(line)
        return True
    if cmd == "test-agent":
        out = test_agent(project, args.agent)
        line = "%s: %s" % (args.agent, "available" if out["available"] else "not available: " + out["reason"])
        c = out.get("check")
        if c:
            line += " · GET %s → %s" % (c.get("url"), c.get("status") or "no answer")
            if c.get("models") is not None:
                line += ", %d model%s" % (c["models"], "" if c["models"] == 1 else "s")
                if c.get("model_listed") is False:
                    line += " (its model %s is not among them)" % project.agent(args.agent).get("model")
            if c.get("error"):
                line += ": " + c["error"]
            line += " (key %s)" % c.get("key")
        print(line)
        if not out["ok"]:
            raise SystemExit(1)                                       # dtree test-agent X && … in a script
        return True
    return False
