"""dtree's web server: the tree page and the JSON API, on the Python standard library only (no Flask).

Writes from another machine need the token in ``.dtree/write_token`` (header ``X-DTree-Token``), made at the first start; this
machine needs none. The server binds 127.0.0.1 by default (``server.host`` / ``--host`` to open it on the network).

A web page of another site, open in the person's browser on this machine, must not write either (review F9 / E0 of 2026-11-02: a
``text/plain`` POST needs no CORS preflight, and the browser connects from 127.0.0.1, so it could merge a trial or start paid jobs). So a
write is a JSON body (``Content-Type: application/json``, which a page of another origin cannot send without a preflight this server
never answers), with no ``Origin`` but the server's own and no ``Sec-Fetch-Site: cross-site``; and every request names this server by an
IP address, ``localhost`` or a name in ``server.allowed_hosts`` (``Host``), so a DNS name rebound to 127.0.0.1 reads nothing and writes
nothing. The page itself sends JSON from its own origin, so it works as before, on the LAN too (``http://<your-server>:5077/``).

Some routes are for this machine only (``local_only``: setting an agent's key, settings.py): the LAN connection is plain HTTP, so a key
sent from another machine would cross the network in the clear. ``Api.handle`` refuses them with 403 when the client is not this machine
(``local``: its address is loopback), and a route that declares a ``local`` parameter is told which it is (the settings page greys the
key inputs for another machine).
"""
from __future__ import annotations

import inspect
import ipaddress
import json
import re
import secrets
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse

from . import adr, jobs, languages, merge, projects, settings, spend, story, verify
from .project import Project
from .store import DecisionError

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 256 * 1024
MAX_IMPORT = 32 * 1024 * 1024                                        # POST /api/projects/import: a whole project's export
LOCAL = ("127.0.0.1", "::1", "localhost")


class Route(NamedTuple):
    """A route: ``local_only`` True, or a function (the path's groups, the server's port) → the 403 answer, for what only this machine
    may do; ``wants_local``: the function takes a ``local`` keyword (whether the client is this machine)."""
    method: str
    pattern: Any
    fn: Callable
    local_only: Union[bool, Callable]
    wants_local: bool


def _takes_local(fn: Callable) -> bool:
    try:
        return "local" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class Api:
    """The routes, apart from HTTP: (method, path, query, body, local) → (status, JSON)."""

    def __init__(self, project: Project, on_switch: Optional[Callable[[Project], None]] = None) -> None:
        """``on_switch``: how the server makes another project the current one (projects.py: new, open, import, save as); without
        it those routes make the project and answer, and this Api stays on its own project."""
        self.p = project
        self.s = project.store
        self.on_switch = on_switch
        r: list = []
        def route(method: str, pattern: str, local_only: Union[bool, Callable] = False):
            def deco(fn: Callable):
                r.append(Route(method, re.compile("^" + pattern + "$"), fn, local_only, _takes_local(fn)))
                return fn
            return deco
        self.routes = r
        S = self.s

        @route("GET", r"/api/project")
        def project_(q, b):
            return {"name": project.config["name"], "root": str(project.root), "base": project.base, "default_agent": project.config["default_agent"],
                    "git": project.git.available(), "mode": projects.mode_of(project)}

        @route("GET", r"/api/agents")
        def agents_(q, b):
            return {"default": project.config["default_agent"], "items": jobs.agent_list(project)}

        @route("GET", r"/api/decisions")
        def list_(q, b):
            return {"items": S.list(q.get("status", "open"), int(q.get("limit", 100)), q.get("q", "")), "counts": S.counts()}

        @route("GET", r"/api/decisions/tree")
        def tree_(q, b):
            return spend.tree_usage(project, S.tree())               # each question's AI usage, and its branch's

        @route("GET", r"/api/decisions/trials")
        def trials_(q, b):
            return {"items": S.trials(q.get("status", "open"))}

        @route("GET", r"/api/decisions/jobs")
        def jobs_(q, b):
            jobs.reap(S)
            num = lambda k: int(q[k]) if str(q.get(k, "")).isdigit() else None
            return {"items": S.jobs(tid=num("trial"), did=num("decision"))}

        @route("GET", r"/api/decisions/jobs/(\d+)/log")
        def log_(q, b, jid):
            job = S.job(int(jid))
            return {"job": job, "lines": jobs.log_tail(job, int(q["lines"]) if str(q.get("lines", "")).isdigit() else 200)}

        @route("POST", r"/api/decisions/jobs/(\d+)/stop")
        def stop_(q, b, jid):
            return {"item": jobs.stop(S, int(jid), by="user")}

        @route("GET", r"/api/decisions/(\d+)")
        def get_(q, b, did):
            return {"item": with_usage(project, S.get(int(did)))}

        @route("GET", r"/api/decisions/(\d+)/code")
        def code_(q, b, did):
            item, g = S.get(int(did)), project.git
            first = g.first_commit_time()
            out = {"git": g.available(), "branch": None, "commits": [], "trials": [],
                   "before_git": bool(first and (item.get("done_at") or item.get("decided_at") or item["created"]) < first)}
            for c in item.get("commits") or []:
                info = g.commit_info(c["sha"]) or {"sha": c["sha"], "short": c["sha"][:7], "subject": c.get("subject"), "missing": True, "files": []}
                out["commits"].append(dict(info, how=c.get("how")))
            if item.get("branch"):
                out["branch"] = g.branch_info(item["branch"], project.base)
            for t in item.get("trials") or []:
                out["trials"].append({"id": t["id"], "option": t["option"], "status": t["status"], "branch": g.branch_info(t["branch"], project.base),
                                      "commits": [dict(g.commit_info(c["sha"]) or {"sha": c["sha"], "missing": True}, how=c.get("how")) for c in t.get("commits") or []]})
            return out

        @route("POST", r"/api/decisions")
        def add_(q, b):
            return {"item": S.add(dict(b, source="user"), by="user")}

        @route("POST", r"/api/decisions/(\d+)/answer")
        def answer_(q, b, did):
            opt = b.get("option")
            if opt is not None and not isinstance(opt, str):
                raise DecisionError("option must be text")
            return {"item": S.answer(int(did), opt or None, b.get("note") or "", by="user")}

        @route("POST", r"/api/decisions/(\d+)/(dismiss|reopen|comment)")
        def act_(q, b, did, action):
            return {"item": getattr(S, action)(int(did), b.get("note") or "", by="user")}

        @route("POST", r"/api/decisions/(\d+)/done")
        def done_(q, b, did):
            return {"item": S.done(int(did), b.get("tests") or "", b.get("impact") or "", by=str(b.get("by") or "user")[:20])}

        @route("POST", r"/api/decisions/(\d+)/try")
        def try_(q, b, did):
            if not isinstance(b.get("option"), str) or not b["option"]:
                raise DecisionError("option must be text")
            return {"item": S.try_option(int(did), b["option"], b.get("note") or "", by="user")}

        @route("POST", r"/api/decisions/trials/(\d+)/measured")
        def measured_(q, b, tid):
            return {"item": S.trial_result(int(tid), b.get("tests") or "", b.get("impact") or "", by=str(b.get("by") or "user")[:20])}

        @route("POST", r"/api/decisions/trials/(\d+)/(adopted|switch|dropped)")
        def end_(q, b, tid, outcome):
            return {"item": S.trial_end(int(tid), outcome, b.get("note") or "", by="user", drop_others=bool(b.get("drop_others")))}

        @route("POST", r"/api/decisions/(\d+)/race")
        def race_(q, b, did):
            names = b.get("agents")
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise DecisionError("agents must be a list of names")
            if not isinstance(b.get("option"), str) or not b["option"]:
                raise DecisionError("option must be text")
            return {"items": jobs.race(project, int(did), b["option"], names, b.get("note") or "", by="user")}

        @route("POST", r"/api/decisions/(\d+)/supersede")
        def supersede_(q, b, did):
            new = b.get("by")
            if new is not None and not isinstance(new, int):
                raise DecisionError("by must be a question id (or null to take it back)")
            return {"item": S.supersede(int(did), new, b.get("note") or "", by="user")}

        @route("GET", r"/api/decisions/trials/(\d+)/diff")
        def diff_(q, b, tid):
            return project.git.diff(project.base, S.trial(int(tid))["branch"])

        @route("POST", r"/api/decisions/trials/(\d+)/followup")
        def followup_(q, b, tid):
            agent = b.get("agent")
            if agent is not None and not isinstance(agent, str):
                raise DecisionError("agent must be text")
            return {"item": jobs.followup(project, int(tid), b.get("feedback") or "", agent or None, by="user")}

        @route("GET", r"/api/decisions/(\d+)/adr")
        def adr_(q, b, did):
            items = {i: S.get(i) for i in S.ids()}
            item = with_usage(project, items[int(did)], ended=True)   # the ADR names what its finished AI jobs used, as `dtree adr` does
            d = project.path(project.config.get("adr_dir") or "docs/decisions")
            return {"path": str(d / adr.filename(item)), "markdown": adr.render(item, items)}

        @route("GET", r"/api/context")
        def context_(q, b):
            return {"markdown": adr.context(project)}

        @route("GET", r"/api/decisions/(\d+)/compare")
        def compare_(q, b, did):
            return jobs.compare(project, int(did))

        @route("POST", r"/api/decisions/trials/(\d+)/ai")
        def ai_(q, b, tid):
            agent = b.get("agent")
            if agent is not None and not isinstance(agent, str):
                raise DecisionError("agent must be text")
            return {"item": jobs.send(project, int(tid), agent or None, b.get("note") or "", by="user")}

        @route("POST", r"/api/decisions/(\d+)/edit")
        def edit_(q, b, did):                                        # the wording, options or references changed afterwards (a story's scene rewritten)
            return {"item": S.edit(int(did), b, by="user")}

        @route("POST", r"/api/decisions/(\d+)/follow")
        def follow_(q, b, did):                                      # which earlier question it grew from, and on which option (a scene after a choice)
            on = b.get("on")
            if on is not None and not isinstance(on, int):
                raise DecisionError("on must be a question's id or null")
            return {"item": S.follow(int(did), on, b.get("how") or "topic", b.get("why") or "", by="user", option=b.get("option"))}

        for mod in (verify, merge, spend, settings, story, languages):   # ก ข ค, ตั้งค่า, the story, the languages: each feature module adds its own routes
            mod.register_routes(route, project)
        projects.register_routes(route, project, (lambda p: self.on_switch(p)) if on_switch else None)

    def handle(self, method: str, path: str, query: Dict[str, str], body: Dict[str, Any], local: bool = True,
               port: Optional[int] = None) -> Tuple[int, Dict[str, Any]]:
        """``local``: the client is this machine (a loopback address); a local-only route is refused (403) for another. ``port``: the
        server's own, for the answer that says where to go instead."""
        allowed = False
        for rt in self.routes:
            hit = rt.pattern.match(path)
            if not hit:
                continue
            if rt.method != method:
                allowed = True
                continue
            if rt.local_only and not local:
                return 403, remote_refusal(rt.local_only, hit.groups(), port or (self.p.config.get("server") or {}).get("port") or 5077)
            try:
                kw = {"local": bool(local)} if rt.wants_local else {}
                out = dict({"ok": True}, **rt.fn(query, body, *hit.groups(), **kw))
                if method == "POST":
                    out.setdefault("counts", self.s.counts())          # every write answers with the counts (the page's chips)
                return 200, out
            except DecisionError as exc:                              # with its code when it has one (a budget refusal: "budget")
                return int(getattr(exc, "http_status", 400) or 400), refusal(exc)
            except KeyError:
                return 404, {"ok": False, "error": "not found", "error_th": "ไม่พบ"}
            except ValueError as exc:
                return 400, {"ok": False, "error": str(exc)}
            except Exception as exc:                                  # any other failure is an answer, not a dropped connection (F20)
                traceback.print_exc(file=sys.stderr)                 # the whole of it in the server's log, its last line in the answer
                return 500, {"ok": False, "error": ("%s: %s" % (type(exc).__name__, exc))[:500]}
        return (405 if allowed else 404), {"ok": False, "error": "no such route" if not allowed else "method not allowed"}


def token(project: Project) -> str:
    path = project.root / ".dtree" / "write_token"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(16), encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()


def _host_name(header: str) -> Optional[str]:
    """The name in a ``Host`` header, without its port (``[::1]:5077`` → ``::1``), lower case; None when it is not one."""
    h = (header or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 and (h[end + 1:] == "" or re.match(r"^:\d+$", h[end + 1:])) else None
    name, _, port = h.partition(":")
    return name if name and (not port or port.isdigit()) and ":" not in port else None


def host_allowed(header: Optional[str], allowed: List[str]) -> bool:
    """Whether a request names this server as an IP address, ``localhost`` or one of ``server.allowed_hosts`` (no Host at all: not a
    browser, which always sends one). A DNS name rebound to this machine is none of them."""
    if header is None:
        return True
    name = _host_name(header)
    if name is None:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or name in {str(a).strip().lower() for a in allowed or []}


def with_usage(project: Project, item: Dict[str, Any], ended: bool = False) -> Dict[str, Any]:
    """A question as the store gives it, with ``usage``: what its AI jobs used (``spend.usage``: totals, per agent, per trial), or
    None when it has no job; ``ended``: its finished jobs only (the ADR)."""
    item["usage"] = spend.usage(project, int(item["id"]), ended=ended).get(int(item["id"]))
    return item


def refusal(exc: Exception) -> Dict[str, Any]:
    """The API's answer to a refusal: its message, and its ``code`` and ``setting`` when it has them (``spend.BudgetRefused``: the page
    shows a budget refusal as one by its code, not by the words in it); a settings refusal adds ``error_th`` and ``path`` (the first bad
    setting, which the page shows the error beside)."""
    out: Dict[str, Any] = {"ok": False, "error": str(exc)}
    for k in ("code", "setting", "error_th"):
        if getattr(exc, k, None):
            out[k] = getattr(exc, k)
    if isinstance(exc, settings.SettingsError):
        out["path"] = exc.path
    return out


def remote_refusal(local_only: Union[bool, Callable], groups: Tuple[str, ...], port: int) -> Dict[str, Any]:
    """The 403 of a route for this machine only, asked from another: the route's own answer, or a general one."""
    if callable(local_only):
        return dict(local_only(groups, port), ok=False)
    return {"ok": False, "code": "local_only", "error": "this is done from this machine only (http://localhost:%d)" % port,
            "error_th": "ทำได้จากเครื่องนี้เท่านั้น (http://localhost:%d)" % port}


def allowed_hosts(project: Project) -> List[str]:
    """``server.allowed_hosts``: a list of names; one name written as a string is that one name (not its letters, review of round 3)."""
    said = (project.config.get("server") or {}).get("allowed_hosts") or []
    return [said] if isinstance(said, str) else [str(a) for a in said]


def write_refusal(headers: Any) -> Optional[Tuple[int, str, str]]:
    """Why a POST is refused as a write another site's page could send (status, English, Thai), or None: its body is not JSON
    (``Content-Type``), its ``Origin`` is not this server's own, or the browser says it comes from another site (``Sec-Fetch-Site``)."""
    ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return (415, "a write is a JSON body (Content-Type: application/json), not %s" % (ctype or "no Content-Type"),
                "คำสั่งเขียนต้องส่งเป็น JSON (Content-Type: application/json)")
    origin = headers.get("Origin")
    if origin is not None and origin.strip().lower().rstrip("/") != "http://" + (headers.get("Host") or "").strip().lower():
        return 403, "a write from another site (Origin %s) is refused" % origin[:100], "คำสั่งเขียนจากเว็บอื่น (Origin %s) ถูกปฏิเสธ" % origin[:100]
    if (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return 403, "a write from another site (Sec-Fetch-Site: cross-site) is refused", "คำสั่งเขียนจากเว็บอื่นถูกปฏิเสธ"
    return None


def make_handler(project: Project):
    state: Dict[str, Any] = {}

    def switch(p: Project) -> None:                                   # another project made current (projects.py): its own routes, store and token
        state["api"], state["secret"], state["project"] = Api(p, on_switch=switch), token(p), p

    switch(project)

    class Handler(BaseHTTPRequestHandler):
        server_version = "dtree"

        def log_message(self, fmt, *args):                      # quiet: the API is polled every few seconds while a job runs
            pass

        def _send(self, status: int, data: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, obj: Dict[str, Any]) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _do(self, method: str) -> None:
            try:
                self._route(method)
            except Exception as exc:                                  # an answer, never a connection dropped without one (F20)
                traceback.print_exc(file=sys.stderr)
                try:
                    self._json(500, {"ok": False, "error": ("%s: %s" % (type(exc).__name__, exc))[:500]})
                except OSError:
                    pass

        def _route(self, method: str) -> None:
            if not host_allowed(self.headers.get("Host"), allowed_hosts(state["project"])):   # read every time: a saved change holds at once
                return self._json(421, {"ok": False, "error": "this server is not %s: open it by its address, localhost, or a name in "
                                                              "server.allowed_hosts" % (self.headers.get("Host") or "")[:100],
                                        "error_th": "เซิร์ฟเวอร์นี้ไม่ใช่ %s: เปิดด้วยที่อยู่ IP, localhost หรือชื่อใน server.allowed_hosts"
                                                    % (self.headers.get("Host") or "")[:100]})
            u = urlparse(self.path)
            if method == "GET" and u.path in ("/", "/tree.html"):
                return self._send(200, (STATIC / "tree.html").read_bytes(), "text/html; charset=utf-8")
            lang = re.match(r"^/lang/([a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)\.json$", u.path)
            if method == "GET" and lang:                                # a translation file: the page's words in that language (languages.py)
                tr = languages.read(lang.group(1))
                return self._json(404, {"ok": False, "error": "no translation file for %s" % lang.group(1)}) if tr is None else self._json(200, tr)
            if not u.path.startswith("/api/"):
                return self._json(404, {"ok": False, "error": "not found"})
            body: Dict[str, Any] = {}
            local = self.client_address[0] in LOCAL
            if method == "POST":
                if not local and self.headers.get("X-DTree-Token", "") != state["secret"]:
                    return self._json(401, {"ok": False, "error": "writes from another machine need the token (X-DTree-Token; .dtree/write_token)",
                                            "error_th": "คำสั่งเขียนจากเครื่องอื่นต้องมีรหัส (X-DTree-Token; ไฟล์ .dtree/write_token)"})
                refused = write_refusal(self.headers)
                if refused:
                    return self._json(refused[0], {"ok": False, "error": refused[1], "error_th": refused[2]})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0:
                    return self._json(400, {"ok": False, "error": "Content-Length is not a number of bytes"})
                if n > (MAX_IMPORT if u.path == "/api/projects/import" else MAX_BODY):   # an export of a whole project is bigger than a question
                    return self._json(413, {"ok": False, "error": "body too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json(400, {"ok": False, "error": "the body must be JSON"})
                if not isinstance(body, dict):
                    return self._json(400, {"ok": False, "error": "the body must be a JSON object"})
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            status, out = state["api"].handle(method, u.path, q, body, local=local, port=self.server.server_address[1])
            self._json(status, out)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

    return Handler


def serve(project: Project, host: Optional[str] = None, port: Optional[int] = None) -> None:
    cfg = project.config.get("server") or {}
    host, port = host or cfg.get("host") or "127.0.0.1", int(port or cfg.get("port") or 5077)
    httpd = ThreadingHTTPServer((host, port), make_handler(project))
    print("dtree %s · http://%s:%d/ · store %s" % (project.config["name"], host, port, project.store.path), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
