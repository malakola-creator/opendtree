"""The settings page's back end (settings.py; the user, 2026-11-02: "ทำหน้า setting กำหนด key, rate limit และอื่นๆได้"): the view,
the writes to .dtree/config.json, the keys, the agent test, the local-only guard, and the CLI. HOME is a temporary one, the keys are
made up, and every model API is a stand-in on 127.0.0.1 (ports 5081-5089): nothing real is read, called or spent."""
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import ApiClient
from dtree import cli, server, settings
from dtree.project import AGENTS, Project

KEY = "zQ7vK2pW9xL4mN8rT3yB6cD1fG5hJ0sA"                          # made up; no 4 of its letters in a row are in any message here
ENV_KEY = "Hy3uE8wR1tQ6oP4iL9kJ2gF7dS5aZ0xC"                      # made up too, put in $GEMINI_API_KEY
SRC = Path(__file__).resolve().parents[1] / "src"


def _no_key(obj, *keys):
    """No key, nor any 6 of its characters in a row, anywhere in ``obj`` (an answer, a file's text)."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    for k in keys or (KEY, ENV_KEY):
        assert k not in text
        bits = [k[i:i + 6] for i in range(len(k) - 5)]
        assert not [b for b in bits if b in text], [b for b in bits if b in text]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for k in ("MODEL_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY", "DTREE_DB"):
        monkeypatch.delenv(k, raising=False)
    return h


def _config(repo, **extra):
    cfg = {"name": "demo", "_note": "a note: kept as it is", "base": "main", "rules": ["keep it small"],
           "db": str(repo["tmp"] / "d.sqlite"), "logs": str(repo["tmp"] / "logs"),
           "chat": {"retries": 3, "_why": "a note inside a section"},
           "agents": {"claude": {"command": str(repo["tmp"] / "no-claude")}, "codex": {"command": str(repo["tmp"] / "no-codex")},
                      "local": {"base_url": "http://127.0.0.1:9/v1"}}}
    for k, v in extra.items():
        if k == "agents":
            cfg["agents"].update(v)
        else:
            cfg[k] = v
    return cfg


def _write(repo, cfg):
    path = repo["path"] / ".dtree" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def _proj(repo, home=None, **extra):
    _write(repo, _config(repo, **extra))
    return Project(repo["path"])


def _file(p):
    return json.loads((p.root / ".dtree" / "config.json").read_text(encoding="utf-8"))


def _bytes(p):
    return (p.root / ".dtree" / "config.json").read_bytes()


def _bind(handler):
    """A server of the test's own on 127.0.0.1, on the first free port of 5081-5089."""
    for port in range(5081, 5090):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, port
    pytest.fail("no free port in 5081-5089")


def _stop(httpd):
    httpd.shutdown()
    httpd.server_close()


class Models:
    """A stand-in model API: ``GET /v1/models`` answers ``answer(handler) -> (status, body, headers)`` and records every request."""

    def __init__(self, answer):
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
                status, body, headers = answer(self)
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.httpd, self.port = _bind(H)
        self.url = "http://127.0.0.1:%d/v1" % self.port

    def close(self):
        _stop(self.httpd)


def _http(port, method, path, body=None, source=None, token=None):
    """A request to the dtree server under test, from ``source`` (127.0.0.2 is not one of server.LOCAL: another machine to it)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30, source_address=(source, 0) if source else None)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-DTree-Token"] = token
    conn.request(method, path, json.dumps(body).encode("utf-8") if body is not None else None, headers)
    r = conn.getresponse()
    out = r.status, json.loads(r.read() or b"{}")
    conn.close()
    return out


# ----------------------------------------------------------------------------------------------------------- the view
def test_the_view_has_every_part_of_the_contract(repo, home):
    p = _proj(repo, home)
    v = settings.view(p)
    assert v["file"] == str(repo["path"] / ".dtree" / "config.json") and v["exists"] is True and v["local"] is True
    assert v["tracked"] is False                                                     # not committed yet
    assert [s["id"] for s in v["sections"]] == ["jobs", "budget", "chat", "verify", "server"]
    paths = {s["id"]: [f["path"] for f in s["fields"]] for s in v["sections"]}
    assert paths["jobs"] == ["default_agent", "max_parallel_jobs", "timeout_s"]
    assert paths["budget"] == ["budget.month_usd", "budget.job_usd", "budget.job_tokens"]
    assert paths["chat"] == ["chat.max_steps", "chat.max_tokens", "chat.temperature", "chat.command_timeout_s", "chat.request_timeout_s",
                             "chat.retries", "chat.retry_max_wait_s", "chat.sandbox", "chat.context_chars", "chat.result_chars"]
    assert paths["verify"] == ["verify.enabled", "verify.tests", "verify.timeout_s"]
    assert paths["server"] == ["server.allowed_hosts", "server.host", "server.port"]
    contract = {"path", "kind", "min", "max", "enum", "nullable", "default", "own", "value", "th", "en", "help_th", "help_en", "restart",
                "readonly", "warn"}
    every = [f for s in v["sections"] for f in s["fields"]] + [f for a in v["agents"] for f in a["fields"]]
    for f in every:
        assert contract <= set(f), (f["path"], contract - set(f))
        assert f["kind"] in ("int", "number", "string", "bool", "enum", "list"), f
        assert f["th"] and f["en"] and f["help_th"] and f["help_en"], f["path"]       # every text in Thai and English
    for s in v["sections"]:
        assert s["th"] and s["en"]
    f = {x["path"]: x for x in every}
    r = f["chat.retries"]
    assert (r["kind"], r["min"], r["max"], r["nullable"], r["default"], r["own"], r["value"]) == ("int", 0, None, False, 8, 3, 3)
    schema = json.loads((SRC / "dtree" / "schema" / "config.schema.json").read_text(encoding="utf-8"))
    assert r["help_en"] == schema["$defs"]["chat"]["properties"]["retries"]["description"]       # help_en is the schema's
    assert f["budget.month_usd"]["nullable"] and f["budget.month_usd"]["own"] is None and f["budget.month_usd"]["set"] is False
    assert f["chat.result_chars"]["default"] == 12000 and f["chat.context_chars"]["value"] is None
    for k in ("server.host", "server.port"):
        assert f[k]["readonly"] and f[k]["restart"] and f[k]["note"]["th"] and f[k]["note"]["en"]
    assert f["server.allowed_hosts"]["readonly"] is False and f["server.allowed_hosts"]["kind"] == "list"
    names = [a["name"] for a in v["agents"]]
    assert names == list(p.config["agents"]) and f["default_agent"]["enum"] == names and f["default_agent"]["value"] == "claude"
    assert set(v["kinds"]) == {"claude", "codex", "chat"}
    assert {"requests_per_minute", "requests_per_day", "base_url", "key_env", "retries"} <= set(v["kinds"]["chat"])
    assert "max_budget_usd" in v["kinds"]["claude"] and "max_tokens_total" not in v["kinds"]["claude"]   # each cap where it acts
    assert "max_tokens_total" in v["kinds"]["codex"] and "key_file" not in v["kinds"]["chat"]
    assert [x["name"] for x in v["presets"]] == list(AGENTS) and {"name", "kind", "label"} <= set(v["presets"][0])
    g = [a for a in v["agents"] if a["name"] == "gemini"][0]
    assert {"name", "kind", "label", "preset", "available", "reason", "fields", "key", "requests"} <= set(g)
    assert g["preset"] and g["kind"] == "chat" and not g["available"] and "no key" in g["reason"]
    assert [x["path"].rsplit(".", 1)[1] for x in g["fields"]] == v["kinds"]["chat"]
    gf = {x["path"]: x for x in g["fields"]}
    assert gf["agents.gemini.model"]["value"] == AGENTS["gemini"]["model"] == gf["agents.gemini.model"]["default"]
    rpm = gf["agents.gemini.requests_per_minute"]
    assert (rpm["kind"], rpm["min"], rpm["nullable"], rpm["value"]) == ("int", 1, True, None)  # stream B's: listed before its schema
    assert gf["agents.gemini.retries"]["default"] == 3 and gf["agents.gemini.retries"]["value"] == 3   # the chat section's, in effect
    assert g["key"] == {"needs": True, "via": "none", "env": "GEMINI_API_KEY", "env_set": False, "file": None, "file_set": False,
                        "bytes": None, "mode": None}
    c = [a for a in v["agents"] if a["name"] == "claude"][0]
    assert c["key"]["needs"] is False and c["key"]["note"]["en"] and c["requests"] is None
    loc = [a for a in v["agents"] if a["name"] == "local"][0]
    assert loc["key"]["needs"] is False                                              # a keyless server on this machine
    repo["git"]("add", ".dtree/config.json")
    repo["git"]("commit", "-q", "-m", "settings")
    assert settings.view(p)["tracked"] is True


def test_the_view_never_carries_a_key(repo, home, monkeypatch):
    kf = home / "muse.key"
    kf.write_text(KEY + "\n")
    os.chmod(kf, 0o644)
    monkeypatch.setenv("GEMINI_API_KEY", ENV_KEY)
    p = _proj(repo, home, agents={"muse": {"key_file": str(kf)}})
    v = settings.view(p)
    _no_key(v)
    a = {x["name"]: x for x in v["agents"]}
    assert a["gemini"]["key"]["via"] == "env" and a["gemini"]["key"]["env_set"] is True and a["gemini"]["available"]
    m = a["muse"]["key"]
    assert (m["via"], m["file"], m["file_set"], m["bytes"], m["mode"]) == ("file", str(kf), True, len(KEY) + 1, "644")
    assert m["warn"]["th"] and "chmod 600" in m["warn"]["en"]                        # others can read it: said
    r = ApiClient(p).get("/api/settings")
    out = r.get_json()
    assert r.status_code == 200 and out["ok"] and out["local"] is True
    _no_key(out)
    kf.write_text("   \n")
    assert {a["name"]: a for a in settings.view(p)["agents"]}["muse"]["key"]["file_set"] is False   # a blank file holds no key


def test_requests_come_from_the_store_when_it_counts_them(repo, home, monkeypatch):
    p = _proj(repo, home)
    store = type(p.store)
    monkeypatch.setattr(store, "request_counts", None, raising=False)               # a store without stream B's table
    assert {a["name"]: a["requests"] for a in settings.view(p)["agents"]}["gemini"] is None
    monkeypatch.setattr(store, "request_counts", lambda self, name: {"minute": 1, "day": 4 if name == "gemini" else 0}, raising=False)
    got = {a["name"]: a["requests"] for a in settings.view(p)["agents"]}
    assert got["gemini"] == {"minute": 1, "day": 4} and got["muse"] == {"minute": 1, "day": 0} and got["claude"] is None


def test_one_host_name_written_as_text_is_a_list_of_one(repo, home):
    p = _proj(repo, home, server={"allowed_hosts": "mybox"})                           # by hand: the server reads it as one name
    f = {x["path"]: x for s in settings.view(p)["sections"] for x in s["fields"]}
    assert f["server.allowed_hosts"]["value"] == ["mybox"] and f["server.allowed_hosts"]["own"] == "mybox"
    settings.update(p, {"set": {"server.allowed_hosts": ["mybox", "mybox.lan"]}})
    assert _file(p)["server"]["allowed_hosts"] == ["mybox", "mybox.lan"] and server.allowed_hosts(p) == ["mybox", "mybox.lan"]


def test_settings_that_weaken_safety_carry_a_warning(repo, home):
    p = _proj(repo, home, agents={"claude": {"command": str(repo["tmp"] / "no-claude"), "sandbox": "none", "network": "host"}},
              chat={"sandbox": "none"})
    v = settings.view(p)
    f = {x["path"]: x for a in v["agents"] for x in a["fields"]}
    f.update({x["path"]: x for s in v["sections"] for x in s["fields"]})
    for path in ("agents.claude.sandbox", "agents.claude.network", "chat.sandbox", "agents.muse.sandbox"):
        assert f[path]["warn"] and f[path]["warn"]["th"] and f[path]["warn"]["en"], path
    assert f["agents.claude.credentials"]["warn"] is None and f["agents.codex.sandbox"]["warn"] is None
    assert f["agents.muse.sandbox"]["own"] is None and f["agents.muse.sandbox"]["value"] == "none"   # the chat section's, in effect


# ----------------------------------------------------------------------------------------------------------- update
BAD = [
    ({"set": {"chat.retries": "five"}}, "chat.retries"),
    ({"set": {"chat.retries": -1}}, "chat.retries"),
    ({"set": {"chat.retries": None}}, "chat.retries"),                                # null would crash the chat loop: unset it
    ({"set": {"chat.retries": True}}, "chat.retries"),
    ({"set": {"chat.temperature": 2.5}}, "chat.temperature"),
    ({"set": {"chat.request_timeout_s": float("nan")}}, "chat.request_timeout_s"),
    ({"set": {"max_parallel_jobs": 1.5}}, "max_parallel_jobs"),
    ({"set": {"chat.sandbox": "docker"}}, "chat.sandbox"),
    ({"set": {"chat.sandbox": None}}, "chat.sandbox"),                                # null would mean no sandbox at all
    ({"set": {"verify.enabled": "yes"}}, "verify.enabled"),
    ({"set": {"budget.job_usd": 0}}, "budget.job_usd"),
    ({"set": {"server.port": 5099}}, "server.port"),
    ({"unset": ["server.host"]}, "server.host"),
    ({"set": {"server.allowed_hosts": ["my box"]}}, "server.allowed_hosts"),
    ({"set": {"rules": ["x"]}}, "rules"),
    ({"set": {"agents.gemini.key_file": "/somewhere/k"}}, "agents.gemini.key_file"),
    ({"set": {"agents.gemini.kind": "claude"}}, "agents.gemini.kind"),
    ({"set": {"agents.nobody.model": "m"}}, "agents.nobody.model"),
    ({"set": {"agents.claude.requests_per_minute": 5}}, "agents.claude.requests_per_minute"),   # a chat-loop setting
    ({"set": {"agents.gemini.requests_per_minute": 0}}, "agents.gemini.requests_per_minute"),
    ({"set": {"agents.gemini.requests_per_day": 2.5}}, "agents.gemini.requests_per_day"),
    ({"set": {"agents.gemini.base_url": "ftp://example.invalid/v1"}}, "agents.gemini.base_url"),
    ({"set": {"agents.gemini.model": "two words"}}, "agents.gemini.model"),
    ({"set": {"agents.gemini.model": None}}, "agents.gemini.model"),
    ({"set": {"agents.claude.network_allow": ["not a host!"]}}, "agents.claude.network_allow"),
    ({"set": {"agents.claude.network": "everywhere"}}, "agents.claude.network"),
    ({"set": {"agents.codex.sandbox": "none"}}, "agents.codex.sandbox"),
    ({"set": {"agents.local.label": "two\nlines"}}, "agents.local.label"),
    ({"set": {"default_agent": "nobody"}}, "default_agent"),
    ({"set": {"default_agent": 3}}, "default_agent"),
    ({"set": {"chat.retries": 5}, "unset": ["chat.retries"]}, "chat.retries"),
    ({"sets": {"chat.retries": 5}}, "sets"),
    ({"remove_agent": "nobody"}, "remove_agent"),
    ({"add_agent": {"name": "two words", "from": "chat"}}, "add_agent.name"),
    ({"add_agent": {"name": "gemini", "from": "chat"}}, "add_agent.name"),
    ({"add_agent": {"name": "g2", "from": "nothing"}}, "add_agent.from"),
]


@pytest.mark.parametrize("change, path", BAD, ids=[b[1] + "-%d" % i for i, b in enumerate(BAD)])
def test_a_bad_change_names_its_path_and_writes_nothing(repo, home, change, path):
    p = _proj(repo, home)
    before, stat0 = _bytes(p), os.stat(p.root / ".dtree" / "config.json")
    with pytest.raises(settings.SettingsError) as err:
        settings.update(p, change)
    assert err.value.path == path and str(err.value) and err.value.error_th
    r = ApiClient(p).post("/api/settings", json=change)
    out = r.get_json()
    assert r.status_code == 400 and out["ok"] is False and out["path"] == path and out["error"] and out["error_th"], out
    assert _bytes(p) == before and os.stat(p.root / ".dtree" / "config.json").st_ino == stat0.st_ino
    assert not (repo["tmp"] / "logs" / "settings.log").exists()


def test_a_value_that_might_be_a_key_is_not_repeated(repo, home):
    p = _proj(repo, home)
    for bad in (KEY, "AIzaSy-" + KEY):
        with pytest.raises(settings.SettingsError) as err:
            settings.update(p, {"set": {"agents.gemini.key_env": bad}})
        _no_key(str(err.value) + err.value.error_th)
        assert err.value.path == "agents.gemini.key_env" and "name of an environment variable" in str(err.value)


def test_a_change_keeps_the_notes_and_every_untouched_byte(repo, home):
    p = _proj(repo, home)
    path = p.root / ".dtree" / "config.json"
    os.chmod(path, 0o640)
    old_text, old_ino = path.read_text(encoding="utf-8"), path.stat().st_ino
    out = settings.update(p, {"set": {"chat.retries": 5}})
    assert out["changed"] == ["chat.retries"] and [s["id"] for s in out["sections"]][0] == "jobs"     # the answer is the new view
    new_text = path.read_text(encoding="utf-8")
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    assert len(old_lines) == len(new_lines) and [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b] == \
        [old_lines.index('  "retries": 3,')]                                        # only that line changed
    assert '"retries": 5,' in new_text and '"_note": "a note: kept as it is"' in new_text and '"_why": "a note inside a section"' in new_text
    expected = _config(repo)
    expected["chat"]["retries"] = 5
    assert new_text == json.dumps(expected, ensure_ascii=False, indent=1) + "\n"     # dtree init's format
    assert stat.S_IMODE(path.stat().st_mode) == 0o640 and path.stat().st_ino != old_ino     # its mode kept; a new file put in place
    assert [x.name for x in path.parent.iterdir() if x.name.endswith(".tmp")] == []
    settings.update(p, {"set": {"agents.gemini.requests_per_minute": 5, "agents.gemini.requests_per_day": 20},
                        "unset": ["chat.retries", "budget.month_usd"]})
    f = _file(p)
    assert "chat" in f and f["chat"] == {"_why": "a note inside a section"}           # the note keeps its section
    assert f["agents"]["gemini"] == {"requests_per_minute": 5, "requests_per_day": 20} and f["_note"] == "a note: kept as it is"
    settings.update(p, {"unset": ["agents.gemini.requests_per_minute", "agents.gemini.requests_per_day"]})
    assert "gemini" not in _file(p)["agents"]                                         # an emptied preset entry goes


def test_the_write_is_atomic_and_a_failed_one_leaves_the_file(repo, home, monkeypatch):
    p = _proj(repo, home)
    before = _bytes(p)

    def broken(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(settings.os, "replace", broken)
    with pytest.raises(settings.SettingsError) as err:
        settings.update(p, {"set": {"chat.retries": 5}})
    assert "No space left" in str(err.value) and err.value.http_status == 500
    assert _bytes(p) == before and p.config["chat"]["retries"] == 3
    assert [x.name for x in (p.root / ".dtree").iterdir() if x.name.endswith(".tmp")] == []    # the temporary file removed


def test_the_running_project_is_reloaded_in_place(repo, home):
    p = _proj(repo, home, merge_trailer="Co-Authored-By: x <x@example.invalid>")
    cfg = p.config
    settings.update(p, {"set": {"agents.gemini.requests_per_minute": 5, "max_parallel_jobs": 5, "chat.retries": 6}})
    assert p.config is cfg and cfg["max_parallel_jobs"] == 5 and cfg["chat"]["retries"] == 6
    assert p.agent("gemini")["requests_per_minute"] == 5 and p.agent("gemini")["model"] == AGENTS["gemini"]["model"]
    assert Project(repo["path"]).agent("gemini")["requests_per_minute"] == 5          # a new job runner reads the same file
    raw = _file(p)
    del raw["merge_trailer"]
    _write(repo, raw)
    assert p.reload() is cfg and "merge_trailer" not in cfg and cfg["name"] == "demo"
    raw.pop("name")
    _write(repo, raw)
    p.reload()
    assert cfg["name"] == repo["path"].name


def test_an_agent_setting_that_refuses_every_job_is_refused_before_writing(repo, home):
    p = _proj(repo, home)
    before = _bytes(p)
    for change, path in [({"agents.muse.month_usd": 5}, "agents.muse.month_usd"),       # dollars of a tokens-only API, no prices
                         ({"agents.muse.month_tokens": 0}, "agents.muse.month_tokens"),
                         ({"budget.month_usd": 0}, "budget.month_usd")]:
        with pytest.raises(settings.SettingsError) as err:
            settings.update(p, {"set": change})
        assert err.value.path == path and "not saved" in str(err.value) and err.value.error_th.startswith("ไม่ได้บันทึก"), err.value
        assert _bytes(p) == before
    settings.update(p, {"set": {"agents.muse.month_usd": 5, "agents.muse.price_in_per_mtok": 1, "agents.muse.price_out_per_mtok": 4}})
    assert p.agent("muse")["month_usd"] == 5
    raw = _file(p)
    raw["agents"]["grok"] = {"total_usd": 9}                                         # a problem written by hand, before this page
    _write(repo, raw)
    p.reload()
    settings.update(p, {"set": {"agents.grok.label": "Grok, capped"}})              # not this change's doing: other changes go on
    assert p.agent("grok")["label"] == "Grok, capped"
    settings.update(p, {"unset": ["agents.grok.total_usd"]})
    assert "total_usd" not in p.agent("grok")


def test_default_agent_must_be_an_agent(repo, home):
    p = _proj(repo, home)
    with pytest.raises(settings.SettingsError) as err:
        settings.update(p, {"remove_agent": "claude"})                               # the default
    assert err.value.path == "default_agent"
    out = settings.update(p, {"remove_agent": "claude", "set": {"default_agent": "codex"}})
    f = _file(p)
    assert f["agents"]["claude"] is None and f["default_agent"] == "codex" and "claude" not in [a["name"] for a in out["agents"]]
    assert set(out["changed"]) == {"default_agent", "agents.claude"}
    with pytest.raises(settings.SettingsError) as err:
        settings.update(p, {"unset": ["default_agent"]})                             # back to claude, which is gone
    assert err.value.path == "default_agent"


def test_remove_and_add_agents(repo, home):
    p = _proj(repo, home)
    settings.update(p, {"remove_agent": "grok"})
    assert _file(p)["agents"]["grok"] is None and "grok" not in p.config["agents"]   # a preset removed: null
    v = settings.view(p)
    assert {x["name"]: x["in_config"] for x in v["presets"]}["grok"] is False
    settings.update(p, {"add_agent": {"name": "grok"}})                             # brought back as dtree has it
    assert "grok" not in _file(p)["agents"] and p.agent("grok")["base_url"] == AGENTS["grok"]["base_url"]
    out = settings.update(p, {"add_agent": {"name": "gemini-pro", "from": "gemini"}, "set": {"agents.gemini-pro.model": "gemini-3.5-pro",
                                                                                          "agents.gemini-pro.requests_per_day": 20}})
    g = _file(p)["agents"]["gemini-pro"]
    assert g == {"kind": "chat", "label": "Gemini (Google AI) · gemini-pro", "base_url": AGENTS["gemini"]["base_url"], "model": "gemini-3.5-pro",
                 "key_env": "GEMINI_API_KEY", "requests_per_day": 20}
    assert "gemini-pro" in [a["name"] for a in out["agents"]] and p.agent("gemini-pro")["kind"] == "chat"
    settings.update(p, {"add_agent": {"name": "haiku", "from": "claude"}, "set": {"agents.haiku.model": "haiku"}})
    h = _file(p)["agents"]["haiku"]
    assert h["kind"] == "claude" and h["disallowed_tools"] == AGENTS["claude"]["disallowed_tools"] and h["sandbox"] == "bwrap"   # its guards
    settings.update(p, {"add_agent": {"name": "orin", "from": "chat"},
                        "set": {"agents.orin.base_url": "http://192.0.2.4:8095/v1", "agents.orin.model": "glimmer", "agents.orin.request_timeout_s": 1800}})
    assert _file(p)["agents"]["orin"] == {"kind": "chat", "label": "orin", "base_url": "http://192.0.2.4:8095/v1", "model": "glimmer",
                                          "request_timeout_s": 1800}
    settings.update(p, {"unset": ["agents.orin.request_timeout_s"]})
    assert _file(p)["agents"]["orin"]["kind"] == "chat"                              # a file's own agent is never emptied away
    settings.update(p, {"remove_agent": "orin"})
    assert "orin" not in _file(p)["agents"] and "orin" not in p.config["agents"]      # one of the file's own: deleted
    with pytest.raises(settings.SettingsError) as err:
        settings.update(p, {"remove_agent": "haiku", "set": {"agents.haiku.model": "x"}})
    assert err.value.path == "agents.haiku.model"


def test_changes_at_once_lose_nothing(repo, home, monkeypatch):
    """Two changes at once, each reading the file slowly: without the lock the second write would carry the first one's old file."""
    p = _proj(repo, home)
    p.store                                                                          # the store there already, as in real use
    read = settings._read_own

    def slow(project):
        got = read(project)
        threading.Event().wait(0.3)
        return got
    monkeypatch.setattr(settings, "_read_own", slow)
    errors = []

    def work(path, value):
        try:
            settings.update(Project(repo["path"]), {"set": {path: value}}, by="t")
        except Exception as exc:                                                      # pragma: no cover - shown below
            errors.append(exc)
    threads = [threading.Thread(target=work, args=("chat.retries", 7)), threading.Thread(target=work, args=("max_parallel_jobs", 9))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    f = _file(p)
    assert f["chat"]["retries"] == 7 and f["max_parallel_jobs"] == 9
    assert len((repo["tmp"] / "logs" / "settings.log").read_text().splitlines()) == 2


def test_another_process_waits_for_a_change_in_progress(repo, home):
    """The server and dtree set are two processes: the lock is flock on .dtree, so the CLI's change waits for the server's."""
    p = _proj(repo, home)
    p.store
    env = dict(os.environ, PYTHONPATH=str(SRC))
    with settings._locked(p):
        proc = subprocess.Popen([sys.executable, "-m", "dtree.cli", "--project", str(repo["path"]), "set", "chat.max_steps", "50"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Event().wait(1.5)                                                  # long enough to start, import and write
        assert proc.poll() is None and "max_steps" not in _file(p)["chat"]            # it waits
        settings.update(p, {"set": {"chat.retries": 7}})                              # this process's change, inside the lock
    assert proc.wait(60) == 0, proc.stderr.read()
    f = _file(p)
    assert f["chat"]["max_steps"] == 50 and f["chat"]["retries"] == 7                # both kept


def test_settings_log_says_what_changed_and_where_from(repo, home):
    p = _proj(repo, home)
    settings.update(p, {"set": {"agents.gemini.label": "ราศีเมถุน", "budget.month_usd": 20}}, by="user", local=True)
    settings.update(p, {"unset": ["budget.month_usd"], "set": {"chat.retries": 4}}, by="user", local=False)
    lines = (repo["tmp"] / "logs" / "settings.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert " · local · user · " in lines[0] and 'set agents.gemini.label="ราศีเมถุน"' in lines[0] and "set budget.month_usd=20" in lines[0]
    assert " · remote · user · " in lines[1] and "set chat.retries=4" in lines[1] and "unset budget.month_usd" in lines[1]
    assert lines[0][:4].isdigit() and lines[0][10] == " "                             # the time first


def test_a_broken_file_is_shown_and_never_written_over(repo, home):
    p = _proj(repo, home)
    path = p.root / ".dtree" / "config.json"
    path.write_text('{"name": "demo",', encoding="utf-8")
    v = settings.view(p)
    assert v["file_error"]["en"].endswith("fix it by hand first") and v["sections"]   # the page still opens, with the values in effect
    with pytest.raises(settings.SettingsError):
        settings.update(p, {"set": {"chat.retries": 5}})
    assert path.read_text() == '{"name": "demo",'


def test_the_view_for_another_machine_greys_what_only_this_one_may_change(repo, home):
    p = _proj(repo, home)
    v = settings.view(p, local=False)
    f = {x["path"]: x for a in v["agents"] for x in a["fields"]}
    f.update({x["path"]: x for s in v["sections"] for x in s["fields"]})
    assert v["local"] is False
    for path in ("agents.gemini.base_url", "agents.gemini.key_env", "agents.gemini.sandbox", "chat.sandbox", "agents.claude.network",
                 "agents.claude.network_allow", "agents.claude.credentials", "agents.claude.sandbox", "agents.codex.sandbox"):
        assert f[path]["readonly"] and f[path]["local_only"] and f[path]["note"]["th"], path
    assert not f["chat.retries"]["readonly"] and not f["agents.gemini.requests_per_minute"]["readonly"]
    for change in ({"set": {"agents.gemini.base_url": "https://evil.example.invalid/v1"}}, {"set": {"agents.claude.sandbox": "none"}},
                   {"unset": ["agents.gemini.key_env"]}, {"set": {"chat.sandbox": "none"}}):
        with pytest.raises(settings.SettingsError) as err:
            settings.update(p, change, local=False)
        assert err.value.http_status == 403 and err.value.code == "local_only"
    settings.update(p, {"set": {"chat.retries": 4}}, local=False)                   # the rest is fine with the token
    assert p.config["chat"]["retries"] == 4
    here = {x["path"]: x for a in settings.view(p, local=True)["agents"] for x in a["fields"]}
    assert here["agents.gemini.base_url"]["readonly"] is False and "note" not in here["agents.gemini.base_url"]


# ----------------------------------------------------------------------------------------------------------- keys
def test_a_key_goes_to_a_0600_file_under_the_keys_directory(repo, home):
    p = _proj(repo, home)
    out = settings.set_key(p, "gemini", "  " + KEY + "\n")                         # what a paste brings around it is dropped
    _no_key(out)
    kf = home / ".config" / "dtree" / "keys" / "demo-gemini.key"
    assert kf.read_text() == KEY + "\n" and stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert stat.S_IMODE(kf.parent.stat().st_mode) == 0o700 and stat.S_IMODE(kf.parent.parent.stat().st_mode) == 0o700
    assert _file(p)["agents"]["gemini"]["key_file"] == "~/.config/dtree/keys/demo-gemini.key"
    assert out["key"] == {"needs": True, "via": "file", "env": "GEMINI_API_KEY", "env_set": False, "file": str(kf), "file_set": True,
                          "bytes": len(KEY) + 1, "mode": "600"}
    assert out["available"] is True and out["agent"] == "gemini"
    from dtree import agents
    assert agents.make(p.agent("gemini"), p)._key() == KEY                           # what a job reads
    again = settings.set_key(p, "gemini", ENV_KEY)                                   # again: the same file, the config untouched
    assert kf.read_text() == ENV_KEY + "\n" and again["key"]["via"] == "file"
    log = (repo["tmp"] / "logs" / "settings.log").read_text()
    assert "key set agents.gemini → ~/.config/dtree/keys/demo-gemini.key (%d bytes)" % (len(KEY) + 1) in log
    _no_key(log)
    _no_key(settings.view(p))
    assert [x.name for x in kf.parent.iterdir()] == ["demo-gemini.key"]               # no temporary file left


def test_a_key_goes_where_the_agent_s_key_file_points(repo, home):
    other = home / "elsewhere" / "muse.key"
    p = _proj(repo, home, agents={"muse": {"key_file": str(other)}})
    before = _bytes(p)
    out = settings.set_key(p, "muse", KEY)
    assert other.read_text() == KEY + "\n" and stat.S_IMODE(other.stat().st_mode) == 0o600 and _bytes(p) == before
    assert out["key"]["file"] == str(other) and out["key"]["via"] == "file"
    assert not (home / ".config" / "dtree").exists()


def test_removing_a_key_deletes_only_dtree_s_own_file(repo, home):
    meta = home / ".config" / "ut-meta" / "meta.key"                                # another tool's key file (a stand-in here)
    meta.parent.mkdir(parents=True)
    meta.write_text(ENV_KEY + "\n")
    os.chmod(meta, 0o600)
    p = _proj(repo, home, agents={"muse": {"key_file": "~/.config/ut-meta/meta.key"}})
    out = settings.clear_key(p, "muse")
    assert meta.read_text() == ENV_KEY + "\n" and out["deleted"] is False             # never deleted
    assert "key_file" not in _file(p)["agents"].get("muse", {}) and p.agent("muse").get("key_file") is None and out["key"]["via"] == "none"
    settings.set_key(p, "gemini", KEY)
    kf = home / ".config" / "dtree" / "keys" / "demo-gemini.key"
    settings.update(p, {"add_agent": {"name": "gemini-pro", "from": "gemini"}})
    raw = _file(p)
    raw["agents"]["gemini-pro"]["key_file"] = "~/.config/dtree/keys/demo-gemini.key"    # two agents, one key (set by hand)
    _write(repo, raw)
    p.reload()
    assert settings.clear_key(p, "gemini-pro")["deleted"] is False and kf.exists()     # gemini still uses it
    out = settings.clear_key(p, "gemini")
    assert out["deleted"] is True and not kf.exists() and "gemini" not in _file(p)["agents"]
    assert settings.clear_key(p, "gemini")["deleted"] is False                        # nothing to remove: fine
    linked = home / ".config" / "dtree" / "keys" / "demo-grok.key"                  # dtree's name, linked to another tool's file
    os.symlink(str(meta), str(linked))
    raw = _file(p)
    raw["agents"]["grok"] = {"key_file": str(linked)}
    _write(repo, raw)
    p.reload()
    assert settings.clear_key(p, "grok")["deleted"] is True and meta.read_text() == ENV_KEY + "\n"   # the link goes, not its target
    log = (repo["tmp"] / "logs" / "settings.log").read_text()
    assert "key removed agents.muse · ~/.config/ut-meta/meta.key kept (not dtree's own file)" in log
    _no_key(log)


@pytest.mark.parametrize("bad", ["", "   ", KEY[:10] + " " + KEY[10:], KEY[:10] + "\n" + KEY[10:], "k" * 4097, "ключ" + KEY, 42])
def test_a_key_is_one_line_of_printable_characters(repo, home, bad):
    p = _proj(repo, home)
    before = _bytes(p)
    with pytest.raises(settings.SettingsError) as err:
        settings.set_key(p, "gemini", bad)
    assert err.value.path == "key" and err.value.error_th
    if isinstance(bad, str) and bad.strip():
        _no_key(str(err.value) + err.value.error_th, KEY)
    assert _bytes(p) == before and not (home / ".config" / "dtree").exists()


def test_claude_and_codex_keep_their_own_logins(repo, home):
    p = _proj(repo, home)
    for name, words in (("claude", "/login"), ("codex", "codex login")):
        with pytest.raises(settings.SettingsError) as err:
            settings.set_key(p, name, KEY)
        assert words in str(err.value) and err.value.http_status == 400
    with pytest.raises(settings.SettingsError) as err:
        settings.set_key(p, "nobody", KEY)
    assert err.value.http_status == 404
    assert ApiClient(p).post("/api/settings/agents/nobody/key", json={"key": KEY}).status_code == 404


def test_the_key_route_is_refused_from_another_machine(repo, home):
    p = _proj(repo, home)
    api = server.Api(p)
    code, out = api.handle("POST", "/api/settings/agents/gemini/key", {}, {"key": KEY}, local=False, port=5083)
    assert code == 403 and out["ok"] is False and out["code"] == "local_only"
    assert out["error_th"] == "ตั้ง key ได้จากเครื่องนี้เท่านั้น (http://localhost:5083) หรือ dtree key gemini"
    assert "plain HTTP" in out["error"] and not (home / ".config" / "dtree").exists()
    _no_key(out)
    code, out = api.handle("POST", "/api/settings/agents/gemini/key", {}, {"key": KEY})          # this machine
    assert code == 200 and out["key"]["via"] == "file"
    _no_key(out)
    code, out = api.handle("POST", "/api/settings/agents/gemini/key", {}, {"remove": True}, local=False)
    assert code == 403 and (home / ".config" / "dtree" / "keys" / "demo-gemini.key").exists()
    code, out = api.handle("POST", "/api/settings/agents/gemini/key", {}, {})
    assert code == 400 and out["path"] == "key"


def test_over_http_another_machine_cannot_set_a_key_or_point_it_elsewhere(repo, home):
    """Real HTTP: a client from 127.0.0.2 is another machine to the server (not one of server.LOCAL), so it needs the token, and even
    with the token cannot set a key, change base_url, or weaken a sandbox; this machine can. allowed_hosts holds at once."""
    p = _proj(repo, home)
    httpd, port = _bind(server.make_handler(p))
    token = (repo["path"] / ".dtree" / "write_token").read_text().strip()
    try:
        status, out = _http(port, "GET", "/api/settings", source="127.0.0.2")
        assert status == 200 and out["local"] is False
        status, out = _http(port, "POST", "/api/settings/agents/gemini/key", {"key": KEY}, source="127.0.0.2")
        assert status == 401                                                            # no token: as every write from elsewhere
        status, out = _http(port, "POST", "/api/settings/agents/gemini/key", {"key": KEY}, source="127.0.0.2", token=token)
        assert status == 403 and out["error_th"] == "ตั้ง key ได้จากเครื่องนี้เท่านั้น (http://localhost:%d) หรือ dtree key gemini" % port
        assert not (home / ".config" / "dtree").exists()
        status, out = _http(port, "POST", "/api/settings", {"set": {"agents.gemini.base_url": "https://elsewhere.invalid/v1"}},
                            source="127.0.0.2", token=token)
        assert status == 403 and out["path"] == "agents.gemini.base_url" and out["code"] == "local_only"
        status, out = _http(port, "POST", "/api/settings", {"set": {"chat.retries": 4}}, source="127.0.0.2", token=token)
        assert status == 200 and out["changed"] == ["chat.retries"] and out["local"] is False
        status, out = _http(port, "POST", "/api/settings/agents/gemini/key", {"key": KEY})
        assert status == 200 and out["key"]["via"] == "file" and out["available"] is True
        _no_key(out)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("GET", "/api/project", headers={"Host": "mybox:%d" % port})
        assert conn.getresponse().status == 421
        conn.close()
        status, out = _http(port, "POST", "/api/settings", {"set": {"server.allowed_hosts": ["MyBox"]}})
        assert status == 200 and p.config["server"]["allowed_hosts"] == ["mybox"]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("GET", "/api/project", headers={"Host": "mybox:%d" % port})
        assert conn.getresponse().status == 200                                         # without a restart
        conn.close()
    finally:
        _stop(httpd)
    log = (repo["tmp"] / "logs" / "settings.log").read_text()
    assert " · remote · user · set chat.retries=4" in log
    _no_key(log)


# ----------------------------------------------------------------------------------------------------------- test an agent
def test_test_agent_asks_the_api_for_its_models_with_the_key(repo, home):
    api = Models(lambda h: (200, {"object": "list", "data": [{"id": "m-small"}, {"id": "models/m-big"}, {"id": "m-x"}]}, None))
    try:
        p = _proj(repo, home, agents={"k1": {"kind": "chat", "base_url": api.url, "model": "m-big", "key_file": str(home / "k1.key")}})
        settings.set_key(p, "k1", KEY)
        out = settings.test_agent(p, "k1")
        assert out["ok"] is True and out["available"] is True and out["reason"] == "" and "error" not in out
        assert out["check"]["status"] == 200 and out["check"]["models"] == 3 and out["check"]["model_listed"] is True
        assert out["check"]["url"] == api.url + "/models" and out["check"]["key"] == "sent"
        assert {"path": "/v1/models", "auth": "Bearer " + KEY} in api.seen              # the key went to the API, in the header
        _no_key(out)
        r = ApiClient(p).post("/api/settings/agents/k1/test")
        assert r.status_code == 200 and r.get_json()["ok"] and r.get_json()["check"]["models"] == 3
    finally:
        api.close()


def test_a_refused_key_is_masked_out_of_the_answer(repo, home):
    said = {"n": 0}

    def answer(h):
        if "Bearer" not in str(h.headers.get("Authorization")):
            return 200, {"data": []}, None                                            # available() asks without a key
        said["n"] += 1                                                                # the whole key, then only its end, as some APIs do
        text = ("Incorrect API key provided: %s." % KEY) if said["n"] % 2 else "Incorrect API key provided: sk-…%s" % KEY[-6:]
        return 401, {"error": {"message": text, "type": "invalid_request_error"}}, None
    api = Models(answer)
    try:
        p = _proj(repo, home, agents={"k1": {"kind": "chat", "base_url": api.url, "model": "m", "key_file": str(home / "k1.key")}})
        settings.set_key(p, "k1", KEY)
        for _ in range(3):
            out = settings.test_agent(p, "k1")
            assert out["ok"] is False and out["check"]["status"] == 401 and "Incorrect API key provided" in out["check"]["error"]
            assert "•••" in out["check"]["error"] and out["error"].startswith("agents.k1: the API answered 401") and out["error_th"]
            _no_key(out)
        assert said["n"] == 3
    finally:
        api.close()


def test_test_agent_says_why_when_there_is_no_answer_or_a_redirect(repo, home):
    elsewhere = Models(lambda h: (200, {"data": [{"id": "stolen"}]}, None))
    api = Models(lambda h: (307, {"moved": True}, {"Location": elsewhere.url + "/models"}))
    try:
        p = _proj(repo, home, agents={"k1": {"kind": "chat", "base_url": api.url, "model": "m", "key_file": str(home / "k1.key")},
                                      "k2": {"kind": "chat", "base_url": "http://127.0.0.1:9/v1", "model": "m"}})
        settings.set_key(p, "k1", KEY)
        out = settings.test_agent(p, "k1")
        assert out["check"]["status"] == 307 and "not followed" in out["check"]["error"] and not out["ok"] and out["check"]["key"] == "sent"
        assert [s for s in elsewhere.seen if s["auth"]] == []                           # never followed: the key went nowhere else
        _no_key(out)
        out = settings.test_agent(p, "k2")
        assert out["check"]["status"] is None and out["check"]["error"].startswith("no answer") and out["ok"] is False
        assert out["available"] is False and out["error"].startswith("agents.k2: no answer from the API") and out["error_th"]
        c = settings.test_agent(p, "claude")
        assert c["check"] is None and c["ok"] is False and "not found" in c["reason"]
        with pytest.raises(settings.SettingsError) as err:
            settings.test_agent(p, "nobody")
        assert err.value.http_status == 404
    finally:
        api.close()
        elsewhere.close()


# ----------------------------------------------------------------------------------------------------------- the CLI
def test_the_cli_shows_sets_and_unsets(repo, home, capsys):
    p = _proj(repo, home)
    root = str(repo["path"])
    assert cli.main(["--project", root, "settings", "--json"]) == 0
    v = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in v["sections"]] == ["jobs", "budget", "chat", "verify", "server"] and v["local"] is True
    assert cli.main(["--project", root, "settings"]) == 0
    text = capsys.readouterr().out
    assert "[chat] วงแชตของ dtree" in text and "chat.retries" in text and "in the file" in text and "agents.gemini.requests_per_minute" in text
    assert cli.main(["--project", root, "set", "chat.retries", "5"]) == 0
    assert "chat.retries 5" in capsys.readouterr().out and _file(p)["chat"]["retries"] == 5
    assert cli.main(["--project", root, "set", "agents.gemini.model", "3"]) == 0 and _file(p)["agents"]["gemini"]["model"] == "3"
    assert cli.main(["--project", root, "set", "server.allowed_hosts", "mybox, other"]) == 0
    assert _file(p)["server"]["allowed_hosts"] == ["mybox", "other"]
    assert cli.main(["--project", root, "set", "budget.month_usd", "null"]) == 0 and _file(p)["budget"]["month_usd"] is None
    assert cli.main(["--project", root, "set", "verify.tests", "pytest -q {tests}"]) == 0 and _file(p)["verify"]["tests"] == "pytest -q {tests}"
    capsys.readouterr()
    assert cli.main(["--project", root, "set", "chat.retries", "-1"]) == 1
    assert "error: chat.retries must be 0 or more" in capsys.readouterr().err
    assert cli.main(["--project", root, "set", "server.port", "5099"]) == 1
    assert cli.main(["--project", root, "unset", "chat.retries"]) == 0
    assert "retries" not in _file(p)["chat"] and "chat.retries 8" in capsys.readouterr().out
    assert cli.main(["--project", root, "unset", "chat.retries"]) == 0 and "(no change)" in capsys.readouterr().out


def test_the_cli_takes_a_key_without_printing_it(repo, home, capsys, monkeypatch):
    p = _proj(repo, home)
    root = str(repo["path"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    assert cli.main(["--project", root, "key", "gemini"]) == 0
    out = capsys.readouterr()
    _no_key(out.out + out.err)
    kf = home / ".config" / "dtree" / "keys" / "demo-gemini.key"
    assert kf.read_text() == KEY + "\n" and "key in %s (mode 600" % kf in out.out and "available" in out.out
    monkeypatch.setattr(sys, "stdin", io.StringIO("two words\n"))
    assert cli.main(["--project", root, "key", "gemini"]) == 1 and kf.read_text() == KEY + "\n"
    assert cli.main(["--project", root, "key", "gemini", "--remove"]) == 0 and not kf.exists()
    assert "no key (one is needed)" in capsys.readouterr().out
    asked = []

    class Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", Tty(""))
    monkeypatch.setattr(settings.getpass, "getpass", lambda prompt: asked.append(prompt) or KEY)
    assert cli.main(["--project", root, "key", "gemini"]) == 0 and kf.read_text() == KEY + "\n"
    assert asked == ["key for agents.gemini (not shown): "]                         # asked without echo on a terminal
    _no_key(capsys.readouterr().out)
    log = (repo["tmp"] / "logs" / "settings.log").read_text()
    assert " · local · cli · key set agents.gemini → ~/.config/dtree/keys/demo-gemini.key" in log and " · local · cli · key removed" in log
    _no_key(log)


def test_the_cli_tests_an_agent(repo, home, capsys):
    api = Models(lambda h: (200, {"data": [{"id": "m"}]}, None))
    try:
        p = _proj(repo, home, agents={"k1": {"kind": "chat", "base_url": api.url, "model": "m"}})
        root = str(repo["path"])
        assert cli.main(["--project", root, "test-agent", "k1"]) == 0
        assert "k1: available · GET %s/models → 200, 1 model (key none)" % api.url in capsys.readouterr().out
        with pytest.raises(SystemExit) as end:
            cli.main(["--project", root, "test-agent", "local"])
        assert end.value.code == 1 and "no answer" in capsys.readouterr().out
        assert p.agent("k1")["model"] == "m"
    finally:
        api.close()
