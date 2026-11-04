"""⚙ Settings on dtree's page (static/tree.html), in the node harness of test_page.py.

The overlay is built from the settings view (GET /api/settings: the sections' FIELDs, the agents' FIELDs, KEY and requests — the
contract of 2026-11-02; a fixture here, since the server's side is built beside the page). What each box shows (the file's own value,
the default as its placeholder, read-only with the restart note, warn), the paths one Save posts (only the changed ones, as {set,
unset}), an error put at its field and on top, the key routes (from the server's own machine only; the key never kept in state,
localStorage, the URL or the page), the test line, adding and removing an agent, and a view with fields missing. A real browser checks
the same outside the suite, against a stand-in server that answers this fixture."""
import copy
import html as _html
import json
import re

import pytest

from test_page import NODE, PAGE, j, run, text, titles

pytestmark = pytest.mark.skipif(NODE is None, reason="no node to run the page's script")

HOME = "/home/u"
KEYS = HOME + "/.config/dtree/keys"
SECRET = "AIzaSy-THE-SECRET-KEY-0123456789abcdef"


def F(path, kind, default=None, own=None, th="", en="", help_th="", help_en="", value=..., mn=None, mx=None, enum=None, nullable=False,
      restart=False, readonly=False, warn=None):
    """A FIELD of the view; ``value`` (in effect) is the file's own value when it has one, else the default, unless given."""
    return {"path": path, "kind": kind, "min": mn, "max": mx, "enum": enum, "nullable": nullable, "default": default, "own": own,
            "value": (own if own is not None else default) if value is ... else value, "th": th, "en": en, "help_th": help_th,
            "help_en": help_en, "restart": restart, "readonly": readonly, "warn": warn}


NAMES = ["claude", "codex", "muse", "grok", "gemini", "local", "orin"]
LONG_HELP = ("every agent together: no job starts once this calendar month's spend (local time; reported + estimated) reached it, and "
             "a job that starts gets what is left as its cap (claude: dollars; chat, codex: tokens, at the higher of the agent's prices); "
             "an agent whose two prices are 0 is never refused by it")
SECTIONS = [
    {"id": "jobs", "th": "งาน", "en": "Jobs", "fields": [
        F("default_agent", "enum", "claude", own="gemini", enum=NAMES, th="AI ที่ใช้เมื่อไม่ได้เลือก", en="The agent used when none is chosen",
          help_th="ต้องเป็นชื่อ AI ที่มีอยู่", help_en="the agent used when none is chosen"),
        F("max_parallel_jobs", "int", 3, mn=1, th="งานพร้อมกันได้สูงสุด", en="Jobs at once", help_th="จำนวนงาน AI ที่ทำพร้อมกัน",
          help_en="how many jobs run at once"),
        F("timeout_s", "number", 5400, mn=1, th="เวลาสูงสุดของงาน (วินาที)", en="A job's time limit (s)", help_en="a job's time limit")]},
    {"id": "budget", "th": "งบ", "en": "Budget", "fields": [
        F("budget.month_usd", "number", None, own=20, mn=0, nullable=True, th="งบรวมต่อเดือน ($)", en="Monthly budget of all agents ($)",
          help_th="ทุก AI รวมกัน: งานไม่เริ่มเมื่อเดือนนี้ใช้ถึง", help_en=LONG_HELP),
        F("budget.job_usd", "number", None, mn=0, nullable=True, th="เพดานดอลลาร์ต่องาน", en="Dollar cap per job",
          help_en="a job's dollar cap when its agent sets none (claude: --max-budget-usd); null = no cap"),
        F("budget.job_tokens", "int", None, mn=1, nullable=True, th="เพดาน tokens ต่องาน", en="Token cap per job",
          help_en="a job's token cap when its agent sets none (chat, codex); null = no cap")]},
    {"id": "chat", "th": "วงแชต", "en": "The chat loop", "fields": [
        F("chat.max_steps", "int", 80, mn=1, th="จำนวนรอบสูงสุด", en="Steps at most"),
        F("chat.max_tokens", "int", 4096, mn=1, th="tokens ต่อคำตอบ", en="Tokens per answer"),
        F("chat.temperature", "number", 0.2, mn=0, mx=2, th="อุณหภูมิ", en="Temperature"),
        F("chat.command_timeout_s", "int", 600, mn=1, th="เวลาต่อคำสั่ง (วินาที)", en="Time per command (s)"),
        F("chat.request_timeout_s", "number", 600, mn=1, th="รอคำตอบของโมเดล (วินาที)", en="Wait for the model (s)",
          help_en="how long one request to the model may take, in seconds (default 600)"),
        F("chat.retries", "int", 8, own=5, mn=0, th="ลองใหม่เมื่อ API ไม่ว่าง", en="Retries when the API is busy", help_th="ถามซ้ำเมื่อได้ 429/503",
          help_en="how many times an API that says it is busy (429, 503) is asked again"),
        F("chat.retry_max_wait_s", "number", 300, mn=0, th="รอนานสุดก่อนเลิก (วินาที)", en="The longest wait (s)"),
        F("chat.sandbox", "enum", "bwrap", enum=["bwrap", "none"], th="sandbox ของคำสั่ง", en="The commands' sandbox"),
        F("chat.context_chars", "int", None, mn=1, nullable=True, th="ตัดบทสนทนาเมื่อยาวเกิน (ตัวอักษร)", en="Cut the conversation at (chars)"),
        F("chat.result_chars", "int", 12000, mn=1, th="ผลของเครื่องมือยาวสุด (ตัวอักษร)", en="A tool result at most (chars)")]},
    {"id": "verify", "th": "การตรวจ", "en": "Verification", "fields": [
        F("verify.enabled", "bool", True, th="ให้ dtree ตรวจเอง", en="dtree verifies", help_en="false: no verification at all (dtree verify is refused)"),
        F("verify.tests", "string", None, own="pytest -q {tests}", nullable=True, th="คำสั่งเทสต์", en="The test command",
          help_en="the test command; {tests} is replaced by the selected test files"),
        F("verify.timeout_s", "number", 1800, mn=1, th="เวลาต่อการตรวจ (วินาที)", en="Time per check (s)")]},
    {"id": "server", "th": "เซิร์ฟเวอร์", "en": "Server", "fields": [
        F("server.allowed_hosts", "list", [], own=["vector"], th="ชื่อที่เปิดเซิร์ฟเวอร์นี้ได้", en="Names the server may be opened by",
          help_en="names (besides IP addresses and localhost) the server may be opened by"),
        F("server.host", "string", "127.0.0.1", th="ที่อยู่ที่ฟัง", en="Host", restart=True, readonly=True),
        F("server.port", "int", 5077, th="พอร์ต", en="Port", restart=True, readonly=True)]},
]

COMMON = [("label", "string", "ชื่อที่แสดง", "Label"), ("model", "string", "โมเดล", "Model"),
          ("max_budget_usd", "number", "เพดานดอลลาร์ต่องาน", "Dollar cap per job"), ("max_tokens_total", "int", "เพดาน tokens ต่องาน", "Token cap per job"),
          ("month_usd", "number", "ดอลลาร์ต่อเดือน", "Dollars per month"), ("month_tokens", "int", "tokens ต่อเดือน", "Tokens per month"),
          ("total_usd", "number", "ดอลลาร์ทั้งหมด", "Dollars in all"), ("total_tokens", "int", "tokens ทั้งหมด", "Tokens in all"),
          ("price_in_per_mtok", "number", "ราคาขาเข้า ต่อล้าน tokens", "Price in, per million tokens"),
          ("price_out_per_mtok", "number", "ราคาขาออก ต่อล้าน tokens", "Price out, per million tokens")]
KIND_KEYS = {
    "chat": [("base_url", "string", "ที่อยู่ API", "API address"), ("key_env", "string", "ตัวแปรที่เก็บ key", "The variable holding the key"),
             ("request_timeout_s", "number", "รอคำตอบ (วินาที)", "Wait for an answer (s)"), ("retries", "int", "ลองใหม่เมื่อ API ไม่ว่าง", "Retries when busy"),
             ("retry_max_wait_s", "number", "รอนานสุด (วินาที)", "The longest wait (s)"),
             ("requests_per_minute", "int", "คำขอต่อนาที", "Requests per minute"), ("requests_per_day", "int", "คำขอต่อวัน", "Requests per day"),
             ("max_steps", "int", "รอบสูงสุด", "Steps at most"), ("max_tokens", "int", "tokens ต่อคำตอบ", "Tokens per answer"),
             ("temperature", "number", "อุณหภูมิ", "Temperature"), ("context_chars", "int", "ตัดบทสนทนาที่", "Cut the conversation at"),
             ("result_chars", "int", "ผลเครื่องมือยาวสุด", "A tool result at most"), ("sandbox", "enum", "sandbox ของคำสั่ง", "The commands' sandbox")],
    "claude": [("sandbox", "enum", "sandbox", "Sandbox"), ("network", "enum", "เครือข่าย", "Network"),
               ("network_allow", "list", "โฮสต์ที่อนุญาตเพิ่ม", "More hosts allowed"), ("credentials", "enum", "ไฟล์ล็อกอิน", "Credentials"),
               ("permission_mode", "string", "โหมดสิทธิ์", "Permission mode")],
    "codex": [("sandbox", "enum", "sandbox ของ Codex", "Codex's sandbox")]}
ENUMS = {("chat", "sandbox"): ["bwrap", "none"], ("claude", "sandbox"): ["bwrap", "none"], ("claude", "network"): ["proxy", "host", "none"],
         ("claude", "credentials"): ["ro", "rw"], ("codex", "sandbox"): ["read-only", "workspace-write", "danger-full-access"]}
MINS = {"retries": 0, "retry_max_wait_s": 0, "requests_per_minute": 1, "requests_per_day": 1, "price_in_per_mtok": 0, "price_out_per_mtok": 0,
        "request_timeout_s": 1, "max_budget_usd": 0, "month_usd": 0, "total_usd": 0, "max_tokens_total": 1, "month_tokens": 0, "total_tokens": 0}
NO_KEY = {"needs": False, "via": "none", "env": None, "env_set": False, "file": None, "file_set": False, "bytes": None, "mode": None}


def agent(name, kind, label, defaults=None, own=None, warn=None, value=None, **rest):
    """An AGENT of the view: its FIELDs by kind (``defaults``: what dtree's preset or the chat section gives, ``own``: the file's)."""
    fields = []
    for key, k, th, en in COMMON + KIND_KEYS[kind]:
        fields.append(F("agents.%s.%s" % (name, key), k, (defaults or {}).get(key), own=(own or {}).get(key), th=th, en=en,
                        help_en="" if key != "requests_per_minute" else "shared by every job of this agent; Gemini's free tier: 5 a minute and 20 a day per model",
                        value=(value or {}).get(key, ...), enum=ENUMS.get((kind, key)), mn=MINS.get(key), nullable=key not in ("label", "base_url"),
                        warn=(warn or {}).get(key)))
    a = {"name": name, "kind": kind, "label": label, "preset": True, "available": True, "reason": "", "fields": fields, "key": dict(NO_KEY),
         "requests": None}
    a.update(rest)
    return a


AGENTS = [
    agent("claude", "claude", "Claude Code", {"label": "Claude Code", "sandbox": "bwrap", "network": "proxy", "network_allow": [], "credentials": "ro",
                                              "permission_mode": "auto"},
          own={"sandbox": "none"}, warn={"sandbox": {"th": "ไม่มี sandbox: Claude Code เข้าถึงได้ทุกอย่างที่คุณเข้าถึงได้",
                                                      "en": "no sandbox: Claude Code reaches whatever you can"}}),
    agent("codex", "codex", "Codex", {"label": "Codex", "sandbox": "workspace-write"}, available=False,
          reason="codex is not installed: npm i -g @openai/codex, then codex login"),
    agent("muse", "chat", "Muse Spark (Meta Model API)", {"label": "Muse Spark (Meta Model API)", "base_url": "https://api.meta.ai/v1",
                                                         "model": "muse-spark-1.3", "key_env": "MODEL_API_KEY", "retries": 8, "sandbox": "bwrap"},
          own={"total_usd": 150, "price_in_per_mtok": 1.0, "price_out_per_mtok": 3.0}, value={"retries": 5}, available=False,
          reason="no key: set $MODEL_API_KEY or agents.muse.key_file",
          key={"needs": True, "via": "none", "env": "MODEL_API_KEY", "env_set": False, "file": None, "file_set": False, "bytes": None, "mode": None}),
    agent("grok", "chat", "Grok (xAI API)", {"label": "Grok (xAI API)", "base_url": "https://api.x.ai/v1", "model": "grok-4", "key_env": "XAI_API_KEY"},
          key={"needs": True, "via": "env", "env": "XAI_API_KEY", "env_set": True, "file": None, "file_set": False, "bytes": None, "mode": None}),
    agent("gemini", "chat", "Gemini (Google AI)", {"label": "Gemini (Google AI)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                                                  "model": "gemini-3.5-flash", "key_env": "GEMINI_API_KEY", "retries": 8},
          own={"requests_per_minute": 5, "requests_per_day": 20}, value={"retries": 5},
          key={"needs": True, "via": "file", "env": "GEMINI_API_KEY", "env_set": False, "file": KEYS + "/dtree-gemini.key", "file_set": True,
               "bytes": 39, "mode": "600"},
          requests={"minute": 2, "day": 3}),
    agent("local", "chat", "Local model (Ollama)", {"label": "Local model (Ollama)", "base_url": "http://127.0.0.1:11434/v1", "model": "gemma4:26b",
                                                   "price_in_per_mtok": 0, "price_out_per_mtok": 0},
          available=False, reason="the local server at http://127.0.0.1:11434/v1 does not answer (Connection refused)"),
    agent("orin", "chat", "Muse Glimmer (the Orin)", {}, own={"label": "Muse Glimmer (the Orin)", "base_url": "http://192.0.2.4:8095/v1",
                                                            "model": "muse-glimmer-30b-q5", "request_timeout_s": 1800, "context_chars": 16000},
          preset=False, key={"needs": True, "via": "file", "env": None, "env_set": False, "file": HOME + "/.config/ut-meta/orin.key", "file_set": True,
                             "bytes": 33, "mode": "644"},
          requests={"minute": 0, "day": 1}),
]
VIEW = {"ok": True, "file": "/srv/proj/.dtree/config.json", "exists": True, "tracked": True, "local": True, "sections": SECTIONS,
        "agents": AGENTS, "presets": [{"name": a["name"], "kind": a["kind"], "label": a["label"]} for a in AGENTS if a["preset"]],
        "kinds": {k: [x[0] for x in COMMON + v] for k, v in KIND_KEYS.items()}}
AGENTS_ANSWER = {"ok": True, "default": "gemini", "items": [{"name": "gemini", "label": "Gemini (Google AI)", "kind": "chat", "available": True, "reason": ""}]}
KEY_SET = {"needs": True, "via": "file", "env": "MODEL_API_KEY", "env_set": False, "file": KEYS + "/proj-muse.key", "file_set": True, "bytes": len(SECRET),
           "mode": "600"}


def view(**kw):
    v = copy.deepcopy(VIEW)
    v.update(kw)
    return v


def applied(v, set_=None, unset=()):
    """The view after a save, as the server would answer it: each path's own value (and what is in effect) changed."""
    v = copy.deepcopy(v)
    for part in v["sections"] + v["agents"]:
        for f in part["fields"]:
            if f["path"] in (set_ or {}):
                f["own"] = f["value"] = set_[f["path"]]
            elif f["path"] in unset:
                f["own"], f["value"] = None, f["default"]
    return v


def opened(v=None, cards=()):
    """The page as showSettings leaves it once GET /api/settings answered with ``v`` (agent cards ``cards`` open)."""
    return ("openOverlay('⚙', ''); state.st.seq = state.ovlSeq;" + "".join("state.st.open[%s] = true;" % j(n) for n in cards) +
            "stRender(%s, null);" % j(v or VIEW))


def field(path, v=None):
    for part in (v or VIEW)["sections"] + (v or VIEW)["agents"]:
        for f in part["fields"]:
            if f["path"] == path:
                return f
    raise KeyError(path)


def posts(fetched, url=r"/api/settings$"):
    return [dict(x, body=json.loads(x["body"])) for x in fetched if x["method"] == "POST" and re.search(url, x["url"])]


def attrs(h, ident):
    """The attributes of the element whose id is ``ident`` in an HTML fragment (an attribute without a value: True), and its tag."""
    m = re.search(r'<(\w+)\b([^>]*\bid="%s"[^>]*)>' % re.escape(ident), h)
    assert m, ident
    out = {"tag": m.group(1)}
    for a in re.finditer(r'([\w-]+)(="([^"]*)")?', m.group(2)):
        out[a.group(1)] = _html.unescape(a.group(3)) if a.group(2) else True
    return out


# ------------------------------------------------------------------------- the button, the legend, the overlay
def test_settings_button_beside_spend():
    page = PAGE.read_text(encoding="utf-8")
    head = page[page.index("<header>"):page.index("</header>")]
    spend, sets = head.index('id="spendBtn"'), head.index('id="setBtn"')
    assert spend < sets and "</button>" not in head[head.index("</button>", spend) + 9:sets]       # right after 💰
    btn = head[sets:head.index("</button>", sets)]
    assert "⚙" in btn and '<span class="th">ตั้งค่า</span>' in btn and '<span class="en">Settings</span>' in btn
    assert re.search(r'title="[^"]*key[^"]*·[^"]*rate limits', btn)


def test_legend_and_note_tell_of_settings():
    for lang, word in (("th", "ตั้งค่า"), ("en", "settings")):
        legend, note = run(["(legend(), elements.legend.innerHTML)", "elements.note.innerHTML"], lang)
        assert "⚙ " + word in text(legend)
        assert "⚙" in note and ("ตั้งค่า" if lang == "th" else "Settings") in note


def test_overlay_from_the_view():
    """The ⚙ button reads GET /api/settings and draws the sections as the view gives them, then the AI, then ＋ Add AI."""
    vals, after = run(["showSettings()"],
                      answers={r"/api/settings$": VIEW}, after=["elements.ovlBody.innerHTML", "elements.ovlTitle.textContent", "JSON.stringify(fetched)"])
    h, title, fetched = after[0], after[1], json.loads(after[2])
    assert title.startswith("⚙ Settings")
    assert [x["url"] for x in fetched if "settings" in x["url"]] == ["/api/settings"]
    order = [h.index('id="sts-%s"' % s["id"]) for s in SECTIONS] + [h.index('id="sts-ai"'), h.index('id="sts-add"')]
    assert order == sorted(order)
    for part in SECTIONS + AGENTS:
        for f in part["fields"]:
            assert 'id="str-%s"' % f["path"] in h, f["path"]
    for a in AGENTS:
        assert 'id="stc-%s"' % a["name"] in h
    t = text(h)
    assert "/srv/proj/.dtree/config.json" in t and "tracked by git: commit it to keep the settings" in t
    th = text(run(["elements.ovlBody.innerHTML"], "th", setup=opened())[0])
    assert "git ติดตามไฟล์นี้: commit เพื่อเก็บค่า" in th and "เพิ่ม AI" in th and "วงแชต" in th


def test_the_file_line():
    h = run(["settingsHtml(%s)" % j(view(exists=False, tracked=False))])[0]
    top = text(h[:h.index('class="sttoc"')])
    assert "/srv/proj/.dtree/config.json not there yet: the first Save makes it" in top and "tracked by git" not in top


# ------------------------------------------------------------------------- a field
def test_inputs_by_kind():
    """number with min/max (int: step 1), text, checkbox, select for enum (the default first), a comma list for list."""
    got = run(["stField(%s, 's:x')" % j(field(p)) for p in ("chat.retries", "chat.temperature", "verify.tests", "verify.enabled", "chat.sandbox",
                                                            "server.allowed_hosts", "default_agent")], setup="state.st.view = %s;" % j(VIEW))
    retries, temp, tests, enabled, sandbox, hosts, default = got
    a = attrs(retries, "stf-chat.retries")
    assert a["tag"] == "input" and a["type"] == "number" and a["step"] == "1" and a["min"] == "0" and a["value"] == "5"
    assert a["placeholder"] == "default: 8" and a["data-stg"] == "s:x"
    a = attrs(temp, "stf-chat.temperature")
    assert a["type"] == "number" and a["step"] == "any" and a["min"] == "0" and a["max"] == "2" and a["value"] == "" and a["placeholder"] == "default: 0.2"
    a = attrs(tests, "stf-verify.tests")
    assert a["type"] == "text" and a["value"] == "pytest -q {tests}" and a["placeholder"] == "default: none"
    a = attrs(enabled, "stf-verify.enabled")
    assert a["type"] == "checkbox" and a.get("checked") is True and "default: on" in text(enabled)
    a = attrs(sandbox, "stf-chat.sandbox")
    assert a["tag"] == "select"
    assert re.findall(r'<option value="([^"]*)"', sandbox) == ["", "bwrap", "none"] and "— default: bwrap —" in text(sandbox)
    assert "selected" not in sandbox                                            # not in the file: the default line is the one shown
    a = attrs(hosts, "stf-server.allowed_hosts")
    assert a["type"] == "text" and a["value"] == "vector" and a["placeholder"] == "default: (empty) · comma separated"
    assert re.search(r'<option value="gemini" selected>gemini — Gemini \(Google AI\)</option>', default)    # an agent's label beside its name


def test_default_as_placeholder_and_reset_only_for_own_values():
    own, deflt = run(["stField(%s, 's:chat')" % j(field("chat.retries")), "stField(%s, 's:chat')" % j(field("chat.max_steps"))])
    assert 'data-streset="chat.retries"' in own and "↺ default" in text(own)
    assert "streset" not in deflt and attrs(deflt, "stf-chat.max_steps")["value"] == ""
    assert attrs(deflt, "stf-chat.max_steps")["placeholder"] == "default: 80"
    th = run(["stField(%s, 's:chat')" % j(field("chat.retries"))], "th")[0]
    assert attrs(th, "stf-chat.retries")["placeholder"] == "ค่าเริ่มต้น: 8" and "↺ ใช้ค่าเริ่มต้น" in text(th)
    assert "ลองใหม่เมื่อ API ไม่ว่าง" in text(th) and "ถามซ้ำเมื่อได้ 429/503" in text(th) and "chat.retries" in text(th)


def test_read_only_fields_with_their_restart_note():
    port, host, hosts = run(["stField(%s, 's:server')" % j(field(p)) for p in ("server.port", "server.host", "server.allowed_hosts")])
    for h, val in ((port, "5077"), (host, "127.0.0.1")):
        assert "<input" not in h and "<select" not in h and "streset" not in h
        assert val in text(h) and "read only when the server starts" in text(h) and "dtree serve --host / --port" in text(h)
    assert "<input" in hosts and "server starts" not in text(hosts)            # allowed_hosts is read again on every request
    th = run(["stField(%s, 's:server')" % j(field("server.port"))], "th")[0]
    assert "อ่านตอนเริ่ม server เท่านั้น" in text(th)
    raw = run(["stRead(%s)" % j([field("server.port")])], setup="document.getElementById('stf-server.port').value = '6000';")[0]
    assert raw == {}                                                             # a read-only field is never read, so never posted


def test_warn_in_a_warning_colour():
    f = field("agents.claude.sandbox")
    en, th = run(["stField(%s, 'a:claude')" % j(f)])[0], run(["stField(%s, 'a:claude')" % j(f)], "th")[0]
    assert '<div class="stw">⚠ no sandbox: Claude Code reaches whatever you can</div>' in en
    assert "⚠ ไม่มี sandbox: Claude Code เข้าถึงได้ทุกอย่างที่คุณเข้าถึงได้" in text(th)
    assert "stw" not in run(["stField(%s, 'a:claude')" % j(field("agents.claude.network"))])[0]


def test_what_is_in_effect_when_it_is_not_what_the_box_shows():
    """gemini's retries are not in the file and its preset says 8, but chat.retries (5) is in effect: the page says so."""
    h = run(["stField(%s, 'a:gemini')" % j(field("agents.gemini.retries"))])[0]
    assert "in effect: 5" in text(h) and attrs(h, "stf-agents.gemini.retries")["placeholder"] == "default: 8"
    assert "in effect" not in text(run(["stField(%s, 's:chat')" % j(field("chat.retries"))])[0])


def test_a_long_help_line_opens_on_a_click():
    h = run(["stField(%s, 's:budget')" % j(field("budget.month_usd"))])[0]
    assert "<details><summary>" in h and text(h).count("an agent whose two prices are 0 is never refused by it") == 1
    assert "ทุก AI รวมกัน" in text(run(["stField(%s, 's:budget')" % j(field("budget.month_usd"))], "th")[0])


# ------------------------------------------------------------------------- an agent's card
def _card(h, name):
    """One agent's card in the overlay: from its <details> to the next card (or the ＋ form)."""
    i = h.index('id="stc-%s"' % name)
    ends = [k for k in (h.find('<details class="stcard"', i + 1), h.find('id="sts-add"', i)) if k > 0]
    return h[i:min(ends)]


def test_agent_cards_say_whether_each_can_run_and_why():
    h = run(["elements.ovlBody.innerHTML"], setup=opened(cards=["codex"]))[0]
    assert 'id="sta-claude">✓' in h and 'id="sta-codex">✗' in h
    codex = _card(h, "codex")
    assert "cannot run here now: codex is not installed: npm i -g @openai/codex, then codex login" in text(codex)
    assert "<details class=\"stcard\" data-stcard=\"codex\" id=\"stc-codex\" open>" in h and 'id="stc-gemini">' in h     # open only those opened
    assert "built in" in text(_card(h, "gemini")) and "added in the file" in text(_card(h, "orin"))
    gem = text(_card(h, "gemini"))
    assert "🔑 file" in gem and "⏱ 5/min · 20/day" in gem and "this minute 2 / 5 · today 3 / 20" in gem


def test_agent_fields_in_groups_with_the_requests_now():
    a = [x for x in AGENTS if x["name"] == "gemini"][0]
    en, th = run(["stGroups(%s)" % j(a)])[0], run(["stGroups(%s)" % j(a)], "th")[0]
    heads = re.findall(r"<h4>(.*?)</h4>", en)
    assert heads == ["Name, model and address", "Rate limits", "Prices", "Caps", "The rest"]
    groups = {text(h): re.findall(r'id="str-agents\.gemini\.([a-z_]+)"', body) for h, body in re.findall(r"<h4>(.*?)</h4>(.*?)(?=<h4>|$)", en, re.S)}
    assert groups["Name, model and address"] == ["label", "model", "base_url", "key_env"]
    assert groups["Rate limits"] == ["requests_per_minute", "requests_per_day", "retries", "retry_max_wait_s", "request_timeout_s"]
    assert groups["Prices"] == ["price_in_per_mtok", "price_out_per_mtok"]
    assert groups["Caps"] == ["max_budget_usd", "max_tokens_total", "month_usd", "month_tokens", "total_usd", "total_tokens"]
    assert groups["The rest"] == ["max_steps", "max_tokens", "temperature", "context_chars", "result_chars", "sandbox"]
    rate = en[en.index("<h4>Rate limits</h4>"):en.index("<h4>Prices</h4>")]
    assert text(rate).startswith("Rate limits this minute 2 / 5 · today 3 / 20")
    assert "นาทีนี้ 2 / 5 · วันนี้ 3 / 20" in text(th) and "อัตราการเรียก" in text(th) and "เพดานค่าใช้จ่าย" in text(th)
    assert "shared by every job of this agent" in text(rate)                    # the help line of B's setting, from the view


def test_claude_and_codex_use_their_own_login():
    h = run(["elements.ovlBody.innerHTML"], setup=opened())[0]
    for name, how in (("claude", "claude → /login"), ("codex", "codex login")):
        c = _card(h, name)
        assert 'type="password"' not in c and "uses its own login, not a key kept by dtree (%s)" % how in text(c)


# ------------------------------------------------------------------------- the key row (KEY: never the key itself)
def _agent(name, **kw):
    a = copy.deepcopy([x for x in AGENTS if x["name"] == name][0])
    a.update(kw)
    return a


def test_key_states():
    lines = run(["stKeyLines(%s)" % j(a) for a in (
        _agent("gemini"), _agent("grok"), _agent("muse"), _agent("orin"), _agent("local"),
        _agent("grok", key=dict(_agent("gemini")["key"], via="env", env_set=True)),
        _agent("gemini", key=dict(_agent("gemini")["key"], file_set=False, bytes=None, mode=None, via="none", needs=True)),
        _agent("gemini", key=None))])
    plain = [[t for t, _ in x] for x in lines]
    assert plain[0] == ["🔑 the key comes from the file", "$GEMINI_API_KEY: not set", "file %s/dtree-gemini.key: 39 bytes · mode 600" % KEYS]
    assert plain[1] == ["🔑 the key comes from $XAI_API_KEY (the server's environment)", "$XAI_API_KEY: set"]
    assert plain[2] == ["✗ no key yet: set one here, or $MODEL_API_KEY before the server starts", "$MODEL_API_KEY: not set"]
    assert lines[2][0][1] == "vbad"
    assert plain[3][1] == "file %s/.config/ut-meta/orin.key: 33 bytes · mode 644" % HOME
    assert plain[3][2].startswith("⚠ other users of this machine can read it (mode 644): chmod 600") and lines[3][2][1] == "vwarn"
    assert plain[4] == ["no key needed (a server on this machine) — set one if the server asks for it"]
    assert plain[5][0] == "🔑 the key comes from $GEMINI_API_KEY (the server's environment) — before the file"
    assert plain[6][-1] == "file %s/dtree-gemini.key: not there" % KEYS and lines[6][-1][1] == "vwarn"
    assert plain[7] == ["where its key comes from is not known (the answer does not say)"]
    th = run(["stKeyLines(%s)" % j(_agent("muse"))], "th")[0]
    assert th[0][0] == "✗ ยังไม่มี key: ตั้งที่นี่ หรือตั้ง $MODEL_API_KEY ก่อนเริ่ม server"


def test_key_row_on_this_machine():
    h = run(["stKeyRow(%s, %s)" % (j(_agent("gemini")), j(VIEW))], setup="state.project = {name: 'proj'};")[0]
    box = attrs(h, "stk-gemini")
    assert box["type"] == "password" and box["autocomplete"] == "off" and "disabled" not in box and "value" not in box
    assert re.search(r'<button data-stkey="gemini">Set key</button>', h) and re.search(r'<button data-stkeyrm="gemini" title="[^"]*">Remove</button>', h)
    assert "written to %s/dtree-gemini.key (mode 600), and the box is emptied at once" % KEYS in text(h)
    muse = run(["stKeyRow(%s, %s)" % (j(_agent("muse")), j(VIEW))], setup="state.project = {name: 'proj'};")[0]
    assert re.search(r'data-stkeyrm="muse" disabled', muse)                    # no key_file: nothing to remove
    assert "written to ~/.config/dtree/keys/proj-muse.key (mode 600)" in text(muse)
    th = run(["stKeyRow(%s, %s)" % (j(_agent("gemini")), j(VIEW))], "th")[0]
    assert "ตั้ง key" in text(th) and ">ลบ<" in th and "วาง key ที่นี่" in th


def test_keys_greyed_when_the_page_is_not_on_the_servers_machine():
    remote = view(local=False)
    h = run(["stKeyRow(%s, %s)" % (j(_agent("gemini")), j(remote))])[0]
    assert attrs(h, "stk-gemini").get("disabled") is True
    assert re.search(r'data-stkey="gemini" disabled', h) and re.search(r'data-stkeyrm="gemini" disabled', h)
    assert ("🔒 a key is set on the server's own machine only (http://localhost:5077), or with dtree key gemini — the LAN connection is plain "
            "HTTP, and a key would cross it in the clear") in text(h)
    th = run(["stKeyRow(%s, %s)" % (j(_agent("gemini")), j(remote))], "th")[0]
    assert "ตั้ง key ได้จากเครื่องที่รัน server เท่านั้น (http://localhost:5077) หรือ dtree key gemini" in text(th)
    top = run(["settingsHtml(%s)" % j(remote)])[0]
    assert "this page is open from another machine" in text(top[:top.index('class="sttoc"')])
    # even if the box were filled and the button clicked, nothing is sent: the key stays off the LAN, and the box is emptied
    vals, after = run(["stSetKey('gemini')", "elements['stk-gemini'].value"], setup=opened(remote) + "document.getElementById('stk-gemini').value = %s;" % j(SECRET),
                      answers={r"/key$": {"ok": True}}, after=["JSON.stringify(fetched)", "elements['stkm-gemini'].textContent"])
    assert vals[1] == "" and SECRET not in after[0] and "/key" not in after[0]
    assert "on the server's own machine only" in after[1]


def test_a_key_is_never_kept():
    """The key goes in one POST to …/agents/gemini/key and nowhere else: its box is emptied as it is read; not in state, not in
    localStorage, not in the URL, not in the page; the answer (KEY, never the key) is drawn."""
    setup = (opened(cards=["gemini"]) + "var stored = []; localStorage.setItem = function (k, v) { stored.push(k + '=' + v); };"
             "document.getElementById('stk-gemini').value = ' %s ';" % SECRET)
    answer = {"ok": True, "key": dict(_agent("gemini")["key"], bytes=len(SECRET)), "available": True, "reason": ""}
    vals, after = run(["stSetKey('gemini')", "elements['stk-gemini'].value"], setup=setup,
                      answers={r"/api/settings/agents/gemini/key$": answer, r"/api/agents$": AGENTS_ANSWER},
                      after=["JSON.stringify(state)", "elements.ovlBody.innerHTML", "elements['stk-gemini'].value", "JSON.stringify(fetched)",
                             "stored.join('\\n')", "location.hash + '|' + location.search", "elements['stkm-gemini'].textContent",
                             "JSON.stringify(Object.keys(elements).map(function (k) { var e = elements[k]; return [k, String(e.value), String(e.textContent), String(e.innerHTML), String(e.title)]; }))"])
    assert vals[1] == ""                                                            # emptied at once, before the answer
    state_, page, box, fetched, stored, url, msg, every = after
    assert SECRET not in state_ and SECRET not in page and box == "" and SECRET not in stored and url == "|"
    assert SECRET not in every                                                      # not in any element of the page
    sent = [x for x in json.loads(fetched) if SECRET in (x.get("body") or "")]
    assert [(x["url"], x["method"], json.loads(x["body"])) for x in sent] == [("/api/settings/agents/gemini/key", "POST", {"key": SECRET})]
    assert msg.startswith("✓ the key is set")
    assert "/api/agents" in [x["url"] for x in json.loads(fetched)]                # the agents are read again: the key may make one available


def test_a_refused_key_is_not_kept_either():
    setup = opened() + "document.getElementById('stk-muse').value = %s;" % j(SECRET)
    refused = {"ok": False, "error": "a key is set from this machine only (http://localhost:5077), or with dtree key muse",
               "error_th": "ตั้ง key ได้จากเครื่องนี้เท่านั้น (http://localhost:5077) หรือ dtree key muse"}
    for lang, want in (("th", refused["error_th"]), ("en", refused["error"])):
        _, after = run(["stSetKey('muse')"], lang, setup=setup, answers={r"/key$": refused},
                       after=["elements['stkm-muse'].textContent", "elements['stk-muse'].value", "JSON.stringify(state)"])
        assert after[0] == "✗ " + want and after[1] == "" and SECRET not in after[2]


def test_a_key_with_whitespace_inside_is_not_sent():
    _, after = run(["stSetKey('muse')"], setup=opened() + "document.getElementById('stk-muse').value = 'two words';", answers={r"/key$": {"ok": True}},
                   after=["JSON.stringify(fetched)", "elements['stkm-muse'].textContent", "elements['stk-muse'].value"])
    assert "/key" not in after[0] and "no whitespace inside" in after[1] and after[2] == ""
    _, after = run(["stSetKey('muse')"], setup=opened(), answers={r"/key$": {"ok": True}}, after=["JSON.stringify(fetched)", "elements['stkm-muse'].textContent"])
    assert "/key" not in after[0] and after[1] == "paste the key into the box first"


ASKED = "var asked = []; window.confirm = function (q) { asked.push(q); return %s; };"


def test_remove_a_key_asks_first_and_says_whose_file_is_kept():
    _, after = run(["stRemoveKey('orin')"], setup=opened() + ASKED % "false", answers={r"/key$": {"ok": True}}, after=["JSON.stringify(fetched)", "asked[0]"])
    assert "/key" not in after[0]
    assert "%s/.config/ut-meta/orin.key is kept (another tool's file" % HOME in after[1]
    answer = {"ok": True, "key": dict(_agent("gemini")["key"], via="none", file=None, file_set=False, bytes=None, mode=None), "available": False,
              "reason": "no key: set $GEMINI_API_KEY or agents.gemini.key_file"}
    _, after = run(["stRemoveKey('gemini')"], setup=opened() + ASKED % "true", answers={r"/key$": answer, r"/api/agents$": AGENTS_ANSWER},
                   after=["JSON.stringify(fetched)", "asked[0]", "elements['stkm-gemini'].textContent", "elements.ovlBody.innerHTML"])
    sent = posts(json.loads(after[0]), r"/agents/gemini/key$")
    assert [x["body"] for x in sent] == [{"remove": True}] and "%s/dtree-gemini.key is deleted" % KEYS in after[1]
    assert after[2] == "✓ the key is taken out · it still cannot run: no key: set $GEMINI_API_KEY or agents.gemini.key_file"
    assert 'id="sta-gemini">✗' in after[3]                                       # the card shows the answer


def test_a_key_answer_without_the_key_reads_the_view_again():
    """A server that answers the key route without KEY: the view is read again, so the card says where the key comes from now."""
    now = copy.deepcopy(VIEW)
    for a in now["agents"]:
        if a["name"] == "muse":
            a.update(key=KEY_SET, available=True, reason="")
    _, after = run(["stSetKey('muse')"], setup=opened(cards=["muse"]) + "document.getElementById('stk-muse').value = %s;" % j(SECRET),
                   answers={r"/agents/muse/key$": {"ok": True}, r"/api/settings$": now, r"/api/agents$": AGENTS_ANSWER},
                   after=["JSON.stringify(fetched)", "elements.ovlBody.innerHTML", "elements['stkm-muse'].textContent", "JSON.stringify(state)"])
    assert [x["url"] for x in json.loads(after[0]) if x["url"] == "/api/settings"] == ["/api/settings"]
    assert "%s/proj-muse.key: %d bytes · mode 600" % (KEYS, len(SECRET)) in text(after[1]) and after[2] == "✓ the key is set"
    assert SECRET not in after[1] and SECRET not in after[3]


# ------------------------------------------------------------------------- the changed paths: {set, unset}
def test_the_changed_paths_diff():
    fs = [field(p) for p in ("chat.retries", "chat.max_steps", "chat.temperature", "chat.sandbox", "budget.month_usd", "verify.enabled",
                             "server.allowed_hosts", "verify.tests", "chat.context_chars", "default_agent", "server.port")]
    cases = [
        ({}, {"set": {}, "unset": [], "errors": {}, "n": 0}),                                              # nothing read: nothing changed
        ({"chat.retries": "5", "chat.max_steps": "", "chat.temperature": "", "chat.sandbox": "", "budget.month_usd": "20",
          "verify.enabled": {"checked": True, "unset": False}, "server.allowed_hosts": "vector", "verify.tests": "pytest -q {tests}",
          "chat.context_chars": "", "default_agent": "gemini"}, {"set": {}, "unset": [], "errors": {}, "n": 0}),   # as they were drawn
        ({"chat.retries": " 5.0 ", "budget.month_usd": "20.00", "server.allowed_hosts": " vector ,"}, {"set": {}, "unset": [], "errors": {}, "n": 0}),
        ({"chat.retries": "3", "chat.max_steps": "100", "chat.temperature": "0.5", "chat.sandbox": "none", "server.allowed_hosts": "vector, 192.0.2.24",
          "verify.tests": "pytest -x {tests}", "default_agent": "claude", "chat.context_chars": "16000"},
         {"set": {"chat.retries": 3, "chat.max_steps": 100, "chat.temperature": 0.5, "chat.sandbox": "none", "server.allowed_hosts": ["vector", "192.0.2.24"],
                  "verify.tests": "pytest -x {tests}", "default_agent": "claude", "chat.context_chars": 16000}, "unset": [], "errors": {}, "n": 8}),
        ({"chat.retries": "", "budget.month_usd": " ", "server.allowed_hosts": "", "verify.tests": "", "default_agent": ""},
         {"set": {}, "unset": ["chat.retries", "budget.month_usd", "server.allowed_hosts", "verify.tests", "default_agent"], "errors": {}, "n": 5}),
        ({"verify.enabled": {"checked": False, "unset": False}}, {"set": {"verify.enabled": False}, "unset": [], "errors": {}, "n": 1}),
        ({"verify.enabled": {"checked": True, "unset": True}}, {"set": {}, "unset": [], "errors": {}, "n": 0}),   # ↺ of a value not in the file
        ({"chat.retries": "x", "chat.max_steps": "2.5", "chat.temperature": "3", "chat.context_chars": "0", "chat.sandbox": "docker",
          "budget.month_usd": {"bad": True}, "server.port": "1"},
         {"set": {}, "unset": [], "n": 6, "errors": {"chat.retries": ["ต้องเป็นตัวเลข", "must be a number"],
                                                    "chat.max_steps": ["ต้องเป็นจำนวนเต็ม", "must be a whole number"],
                                                    "chat.temperature": ["ต้องไม่เกิน 2", "must be at most 2"],
                                                    "chat.context_chars": ["ต้องไม่น้อยกว่า 1", "must be at least 1"],
                                                    "chat.sandbox": ["ต้องเป็นหนึ่งใน bwrap, none", "must be one of bwrap, none"],
                                                    "budget.month_usd": ["ต้องเป็นตัวเลข", "must be a number"]}}),
    ]
    got = run(["stDiff(%s, %s)" % (j(fs), j(raw)) for raw, _ in cases])
    assert got == [want for _, want in cases]
    own_bool = dict(field("verify.enabled"), own=False, value=False)
    got = run(["stDiff(%s, %s)" % (j([own_bool]), j(raw)) for raw in ({"verify.enabled": {"checked": False, "unset": False}},
                                                                       {"verify.enabled": {"checked": True, "unset": True}},
                                                                       {"verify.enabled": {"checked": True, "unset": False}})])
    assert [(g["set"], g["unset"]) for g in got] == [({}, []), ({}, ["verify.enabled"]), ({"verify.enabled": True}, [])]


def test_reset_of_a_value_drawn_as_an_empty_box():
    """An own [] (or "") in the file is drawn as an empty box, so an empty box is no change; ↺ marks the box, and Save unsets it."""
    empty = dict(field("agents.claude.network_allow"), own=[], value=[])
    got = run(["stDiff(%s, %s)" % (j([empty]), j({empty["path"]: raw})) for raw in ("", {"unset": True}, "pypi.org")])
    assert [(g["set"], g["unset"]) for g in got] == [({}, []), ({}, [empty["path"]]), ({empty["path"]: ["pypi.org"]}, [])]
    assert run(["stDiff(%s, %s)" % (j([field("agents.claude.network_allow")]), j({empty["path"]: {"unset": True}}))])[0]["n"] == 0  # nothing to unset
    v = copy.deepcopy(VIEW)
    for f in [x for x in v["agents"] if x["name"] == "claude"][0]["fields"]:
        if f["path"] == empty["path"]:
            f.update(own=[], value=[])
    attrs_ = ATTRS.replace("stf-verify.enabled", "stf-" + empty["path"])
    vals = run(["(stReset(%s), stDiff(stFields('a:claude'), stRead(stFields('a:claude'))).unset)" % j(empty["path"]),
                "elements['stb-a:claude'].textContent"], setup=opened(v) + attrs_)
    assert vals == [[empty["path"]], "Save (1)"]


def test_a_bad_value_in_the_file_does_not_block_its_part():
    """A file edited by hand with a value its kind cannot read ("docker" for an enum, "lots" for a number): its box shows it as it is,
    left alone it is no change (the rest of the part still saves), and only a box changed to something unreadable is refused."""
    sandbox = dict(field("chat.sandbox"), own="docker", value="docker")
    timeout = dict(field("chat.request_timeout_s"), own="lots", value="lots")
    h = run(["stField(%s, 's:chat')" % j(sandbox), "stField(%s, 's:chat')" % j(timeout)])
    assert re.search(r'<option value="docker" selected>docker</option>', h[0])
    assert attrs(h[1], "stf-chat.request_timeout_s")["type"] == "text" and attrs(h[1], "stf-chat.request_timeout_s")["value"] == "lots"
    fs = [sandbox, timeout, field("chat.retries")]
    got = run(["stDiff(%s, %s)" % (j(fs), j(raw)) for raw in ({"chat.sandbox": "docker", "chat.request_timeout_s": "lots", "chat.retries": "6"},
                                                              {"chat.sandbox": "none", "chat.request_timeout_s": "900"},
                                                              {"chat.request_timeout_s": "more"})])
    assert got[0] == {"set": {"chat.retries": 6}, "unset": [], "errors": {}, "n": 1}
    assert got[1]["set"] == {"chat.sandbox": "none", "chat.request_timeout_s": 900}
    assert got[2]["errors"] == {"chat.request_timeout_s": ["ต้องเป็นตัวเลข", "must be a number"]}


def test_a_field_of_a_kind_the_page_does_not_know_reads_json_or_text():
    f = F("x.y", "dict", None, th="x", en="x")
    got = run(["stDiff(%s, %s)" % (j([f]), j({"x.y": s})) for s in ('{"a": 1}', "plain words", "5")])
    assert [g["set"]["x.y"] for g in got] == [{"a": 1}, "plain words", 5]


def test_save_posts_only_the_changed_paths():
    setup = opened() + ("elements['stf-chat.retries'].value = '3';"              # 5 in the file → 3
                        "elements['stf-chat.max_steps'].value = '100';"          # not in the file → written
                        "elements['stf-chat.temperature'].value = '';"           # not in the file, left empty → nothing
                        "elements['stf-chat.sandbox'].value = '';"               # the default line kept → nothing
                        "elements['stf-budget.month_usd'].value = '';")          # another part's box: not this Save's
    new = applied(VIEW, {"chat.retries": 3, "chat.max_steps": 100})
    _, after = run(["stSave('s:chat')"], setup=setup, answers={r"/api/settings$": new, r"/api/agents$": AGENTS_ANSWER},
                   after=["JSON.stringify(fetched)", "elements['stm-s:chat'].textContent", "elements.stMsg.textContent", "elements.ovlBody.innerHTML"])
    fetched = json.loads(after[0])
    assert [x["body"] for x in posts(fetched)] == [{"set": {"chat.retries": 3, "chat.max_steps": 100}, "unset": []}]
    assert after[1] == after[2] == "✓ saved: chat.max_steps = 100 · chat.retries = 3"
    assert attrs(after[3], "stf-chat.max_steps")["value"] == "100"                # the answer (the new view) is drawn
    urls = [x["url"] for x in fetched]
    i = urls.index("/api/settings")
    assert "/api/agents" in urls[i:] and "/api/spend" in urls[i:] and "/api/decisions/tree" in urls[i:]   # the page's agents, spend, tree


def test_unset_bool_and_an_agents_save():
    setup = opened(cards=["gemini"]) + ("elements['stf-budget.month_usd'].value = '';"
                                        "elements['stf-verify.enabled'].checked = false;"
                                        "elements['stf-agents.gemini.requests_per_minute'].value = '10';"
                                        "elements['stf-agents.gemini.requests_per_day'].value = '';"
                                        "elements['stf-agents.gemini.model'].value = 'gemini-3.5-pro';")
    _, after = run(["stSave('s:budget')", "stSave('s:verify')", "stSave('a:gemini')"], setup=setup,
                   answers={r"/api/settings$": VIEW}, after=["JSON.stringify(fetched)"])
    assert [x["body"] for x in posts(json.loads(after[0]))] == [
        {"set": {}, "unset": ["budget.month_usd"]}, {"set": {"verify.enabled": False}, "unset": []},
        {"set": {"agents.gemini.model": "gemini-3.5-pro", "agents.gemini.requests_per_minute": 10}, "unset": ["agents.gemini.requests_per_day"]}]


ATTRS = ("elements['stf-verify.enabled'].getAttribute = function (k) { return k in this.own_ ? this.own_[k] : null; };"
         "elements['stf-verify.enabled'].own_ = {};"
         "elements['stf-verify.enabled'].setAttribute = function (k, v) { this.own_[k] = String(v); };"
         "elements['stf-verify.enabled'].removeAttribute = function (k) { delete this.own_[k]; };")


def test_reset_button_then_save_unsets():
    vals = run(["(stReset('chat.retries'), elements['stf-chat.retries'].value)", "stDiff(stFields('s:chat'), stRead(stFields('s:chat'))).unset",
                "elements['stb-s:chat'].textContent"], setup=opened())
    assert vals == ["", ["chat.retries"], "Save (1)"]
    own = applied(VIEW, {"verify.enabled": False})                                  # a bool the file sets: ↺ shows the default, then unsets
    vals = run(["(stReset('verify.enabled'), elements['stf-verify.enabled'].checked)", "stDiff(stFields('s:verify'), stRead(stFields('s:verify')))"],
               setup=opened(own) + ATTRS + "elements['stf-verify.enabled'].checked = false;")
    assert vals[0] is True and vals[1]["unset"] == ["verify.enabled"] and vals[1]["set"] == {}


def test_nothing_changed_posts_nothing():
    _, after = run(["stSave('s:chat')"], setup=opened(), answers={r"/api/settings$": VIEW}, after=["JSON.stringify(fetched)", "elements['stm-s:chat'].textContent"])
    assert posts(json.loads(after[0])) == [] and after[1] == "nothing changed"


def test_a_box_that_cannot_be_read_is_not_posted_and_says_so_at_its_field():
    setup = opened() + "elements['stf-chat.retries'].value = 'many'; elements['stf-chat.max_steps'].value = '90';"
    for lang, word in (("en", "must be a number"), ("th", "ต้องเป็นตัวเลข")):
        _, after = run(["stSave('s:chat')"], lang, setup=setup, answers={r"/api/settings$": VIEW},
                       after=["JSON.stringify(fetched)", "elements['ste-chat.retries'].textContent", "elements['stm-s:chat'].textContent"])
        assert posts(json.loads(after[0])) == [] and after[1] == "✗ " + word
        assert after[2] == ("✗ fix the fields in red first (nothing was saved)" if lang == "en" else "✗ แก้ช่องที่แดงก่อน (ยังไม่ได้บันทึก)")


def test_a_refusal_is_shown_at_its_field_and_on_top():
    setup = opened() + "elements['stf-agents.gemini.requests_per_day'].value = '7';"
    refused = {"ok": False, "path": "agents.gemini.requests_per_day", "error": "agents.gemini.requests_per_day must be an integer of 1 or more, or null",
               "error_th": "agents.gemini.requests_per_day ต้องเป็นจำนวนเต็มตั้งแต่ 1 ขึ้นไป หรือ null"}
    for lang, want in (("en", refused["error"]), ("th", refused["error_th"])):
        _, after = run(["stSave('a:gemini')"], lang, setup=setup, answers={r"/api/settings$": refused},
                       after=["elements['ste-agents.gemini.requests_per_day'].textContent", "elements.stMsg.textContent",
                              "elements['stm-a:gemini'].textContent", "state.st.open.gemini === true && elements['stc-gemini'].open === true"])
        assert after[0] == "✗ " + want and after[1] == "✗ " + want
        assert after[2] == ("✗ see the field in red" if lang == "en" else "✗ ดูช่องที่แดง")
        assert after[3] is True                                                    # its card, closed, is opened to show it


def test_a_refusal_naming_a_field_of_another_part_or_none():
    """remove_agent refused because default_agent names it: the error is put at default_agent (in the Jobs part) and on top; an error
    with no field the page has is put by the Save that was clicked."""
    refused = {"ok": False, "path": "default_agent", "error": "default_agent is gemini: choose another before removing it",
               "error_th": "default_agent เป็น gemini: เลือกตัวอื่นก่อนลบ"}
    _, after = run(["stRemoveAgent('gemini')"], setup=opened() + ASKED % "true", answers={r"/api/settings$": refused},
                   after=["elements['ste-default_agent'].textContent", "elements.stMsg.textContent", "JSON.stringify(fetched)"])
    assert after[0] == after[1] == "✗ " + refused["error"]
    assert [x["body"] for x in posts(json.loads(after[2]))] == [{"remove_agent": "gemini"}]
    nowhere = {"ok": False, "error": "the config file is not valid JSON: line 3", "path": "nothing.here"}
    _, after = run(["stSave('s:chat')"], setup=opened() + "elements['stf-chat.retries'].value = '4';", answers={r"/api/settings$": nowhere},
                   after=["elements['stm-s:chat'].textContent", "elements.stMsg.textContent"])
    assert after == ["✗ the config file is not valid JSON: line 3"] * 2


def test_drafts_of_the_other_parts():
    setup = opened() + ("elements['stf-chat.retries'].value = '4'; elements['stf-budget.job_usd'].value = '0.5';"
                        "elements['stf-agents.grok.model'].value = 'grok-5'; elements['stf-verify.enabled'].checked = false;")
    got = run(["stDrafts('s:chat')", "stDrafts(null)", "stDirty()"], setup=setup)
    assert got[0] == {"budget.job_usd": "0.5", "agents.grok.model": "grok-5", "verify.enabled": {"checked": False, "unset": False}}
    assert got[1] == dict(got[0], **{"chat.retries": "4"}) and got[2] is True
    assert run(["stDirty()"], setup=opened())[0] is False
    # drawn again, the boxes typed are put back by path (the page's own elements are new then; here the stub shows what was put)
    put = run(["(stRender(%s, {'budget.job_usd': '0.5', 'verify.enabled': {checked: false, unset: true}, 'gone.path': 'x'}),"
               " [elements['stf-budget.job_usd'].value, elements['stf-verify.enabled'].checked, elements['stf-verify.enabled'].own_['data-unset']])" % j(VIEW)],
              setup=opened() + ATTRS)[0]
    assert put == ["0.5", False, "1"]


def test_closing_with_unsaved_settings_asks():
    asked = "var asked = 0; window.confirm = function () { asked++; return %s; };"
    typed = "elements['stf-chat.retries'].value = '4';"
    assert run(["(closeOverlay(), elements.ovl.hidden)", "asked"], setup=opened() + asked % "false" + typed) == [False, 1]
    assert run(["(closeOverlay(), elements.ovl.hidden)", "asked"], setup=opened() + asked % "true" + typed) == [True, 1]
    assert run(["(closeOverlay(), elements.ovl.hidden)", "asked"], setup=opened() + asked % "false") == [True, 0]     # nothing typed: no question
    assert run(["(closeOverlay(), elements.ovl.hidden)", "asked"],                                                    # another overlay since: none
               setup=opened() + asked % "false" + typed + "openOverlay('± diff', '');") == [True, 0]


# ------------------------------------------------------------------------- test, remove, add
def test_the_test_line():
    lines = run(["stTestLine(%s)" % j(x) for x in (
        {"ok": True, "available": True, "reason": "", "check": {"status": 200, "models": 60}},
        {"ok": True, "available": True, "reason": "", "check": {"status": 401, "error": "API key not valid: AIza…(masked)"}},
        {"ok": True, "available": False, "reason": "codex is not installed", "check": None},
        {"ok": True, "available": True, "reason": "claude 2.1.314 · bwrap"},
        {"ok": True, "available": False, "reason": "no key", "check": {"error": "connection refused"}},
        {"ok": False, "error": "no agent 'x'", "error_th": "ไม่มี AI ชื่อ x"})])
    assert lines == [["✓ available · GET /models 200 · 60 models", "vgood"],
                     ["✓ available · GET /models 401: API key not valid: AIza…(masked)", "vbad"],
                     ["✗ cannot run · codex is not installed", "vbad"], ["✓ available · claude 2.1.314 · bwrap", "vgood"],
                     ["✗ cannot run · no key · GET /models: connection refused", "vbad"], ["✗ no agent 'x'", "vbad"]]
    assert run(["stTestLine(%s)" % j({"ok": True, "available": True, "check": {"status": 200, "models": 3}})], "th")[0][0] == "✓ ใช้ได้ · GET /models 200 · 3 โมเดล"


def test_test_posts_and_shows_its_answer_on_one_line():
    answer = {"ok": True, "available": False, "reason": "agents.gemini: the API answered 401", "check": {"status": 401, "error": "API key not valid"}}
    _, after = run(["stTest('gemini')"], setup=opened(), answers={r"/agents/gemini/test$": answer},
                   after=["JSON.stringify(fetched)", "elements['stt-gemini'].textContent", "elements['stt-gemini'].className", "elements['sta-gemini'].textContent"])
    sent = posts(json.loads(after[0]), r"/agents/gemini/test$")
    assert len(sent) == 1 and sent[0]["body"] == {}
    assert after[1] == "✗ cannot run · agents.gemini: the API answered 401 · GET /models 401: API key not valid" and "\n" not in after[1]
    assert after[2] == "stmsg vbad" and after[3] == "✗"


def test_remove_an_agent_asks_first():
    _, after = run(["stRemoveAgent('grok')"], setup=opened() + ASKED % "false", answers={r"/api/settings$": VIEW}, after=["JSON.stringify(fetched)", "asked[0]"])
    assert posts(json.loads(after[0])) == [] and 'the file gets "grok": null' in after[1]
    gone = view(agents=[a for a in AGENTS if a["name"] != "grok"])
    _, after = run(["stRemoveAgent('grok')"], setup=opened() + ASKED % "true", answers={r"/api/settings$": gone, r"/api/agents$": AGENTS_ANSWER},
                   after=["JSON.stringify(fetched)", "elements.ovlBody.innerHTML", "elements.stMsg.textContent"])
    assert [x["body"] for x in posts(json.loads(after[0]))] == [{"remove_agent": "grok"}]
    assert 'id="stc-grok"' not in after[1] and after[2] == "✓ grok is removed"
    th = run(["stRemoveAgent('orin')", "asked[0]"], "th", setup=opened() + ASKED % "false")[1]
    assert th.startswith("ลบ Muse Glimmer (the Orin) (orin) ออกจากรายชื่อ AI?") and "null" not in th     # not a preset: nothing written as null


def test_add_an_agent_from_a_preset_or_an_openai_compatible_one():
    form = run(["stAddForm(%s)" % j(VIEW)])[0]
    assert re.findall(r'<option value="([^"]*)"', form) == ["claude", "codex", "muse", "grok", "gemini", "local", "chat"]
    assert "OpenAI-compatible (chat)" in text(form) and attrs(form, "stAddName")["placeholder"] == "e.g. claude2"
    added = view(agents=AGENTS + [agent("gemini2", "chat", "Gemini (Google AI)", {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
                                        key=dict(NO_KEY, needs=True, env="GEMINI_API_KEY"))])
    _, after = run(["stAddAgent()"], setup=opened() + "elements.stAddFrom.value = 'gemini'; elements.stAddName.value = '';",
                   answers={r"/api/settings$": added, r"/api/agents$": AGENTS_ANSWER},
                   after=["JSON.stringify(fetched)", "elements.ovlBody.innerHTML", "elements.stMsg.textContent"])
    assert [x["body"] for x in posts(json.loads(after[0]))] == [{"add_agent": {"name": "gemini2", "from": "gemini"}}]
    assert '<details class="stcard" data-stcard="gemini2" id="stc-gemini2" open>' in after[1] and after[2] == "✓ gemini2 is added (from gemini)"
    _, after = run(["stAddAgent()"], setup=opened() + "elements.stAddFrom.value = 'chat'; elements.stAddName.value = ' llama ';",
                   answers={r"/api/settings$": {"ok": False, "error": "agents.llama: base_url and model are needed", "path": "add_agent"}},
                   after=["JSON.stringify(fetched)", "elements['stm-add'].textContent", "elements.stMsg.textContent"])
    assert [x["body"] for x in posts(json.loads(after[0]))] == [{"add_agent": {"name": "llama", "from": "chat"}}]
    assert after[1] == after[2] == "✗ agents.llama: base_url and model are needed"
    assert run(["stFree('chat', %s)" % j(VIEW), "stFree('gemini', %s)" % j(added)]) == ["custom", "gemini3"]


# ------------------------------------------------------------------------- what the view may lack
def test_a_view_with_fields_missing_still_draws():
    """An older or smaller view: no requests, no key, FIELDs without kind / default / labels, agents without fields, no presets."""
    small = {"ok": True, "sections": [{"id": "jobs", "fields": [{"path": "timeout_s"}, {"path": "max_parallel_jobs", "kind": "int", "own": 4}, None]}],
             "agents": [{"name": "solo"}, {"name": "gemini", "kind": "chat", "fields": [{"path": "agents.gemini.requests_per_minute", "kind": "int"}]}, {}]}
    vals, after = run(["showSettings()"], answers={r"/api/settings$": small}, after=["elements.ovlBody.innerHTML", "JSON.stringify(stDiff(stFields('s:jobs'), {'timeout_s': '9'}))"])
    h = after[0]
    assert 'id="str-timeout_s"' in h and "timeout_s" in text(h) and 'id="stc-solo"' in h and 'id="stc-gemini"' in h
    assert "this minute" not in text(h) and "where its key comes from is not known" in text(h)
    assert json.loads(after[1])["set"] == {"timeout_s": 9}
    assert "OpenAI-compatible" in text(h)                                         # ＋ still works without presets
    assert run(["settingsHtml({})", "settingsHtml(null)"])[0]                     # nothing at all: the frame alone, no error


def test_an_error_answer_to_the_view():
    _, after = run(["showSettings()"], answers={r"/api/settings$": {"ok": False, "error": "no such route"}}, after=["elements.ovlBody.textContent"])
    assert after[0] == "Could not read: no such route"


def test_an_agent_that_cannot_run_links_to_its_settings():
    setup = "state.agents = {default: 'claude', items: [{name: 'claude', available: true}, {name: 'muse', label: 'Muse', available: false, reason: 'no key'}]};"
    h = run(["agentBelow('x')"], setup=setup)[0]
    assert 'data-stopen="muse"' in h and "⚙ Settings" in text(h)
    _, after = run(["showSettings('muse')"], answers={r"/api/settings$": VIEW}, after=["elements.ovlBody.innerHTML"])
    assert '<details class="stcard" data-stcard="muse" id="stc-muse" open>' in after[0]
