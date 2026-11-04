"""The decision store as MCP tools over stdio (``dtree mcp``), so any agent that speaks MCP — Claude Code, Codex, others — raises the
questions it needs a person to decide, reads the answers, and records the finished work, the same way the CLI does.

Register it: ``claude mcp add dtree -- dtree --project /path/to/repo mcp`` · Codex: ``[mcp_servers.dtree] command = "dtree"
args = ["--project", "/path/to/repo", "mcp"]`` in ~/.codex/config.toml. The person answers on the tree page; agents never answer.
Newline-delimited JSON-RPC 2.0 (MCP's stdio transport); only the tools capability.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional, TextIO

from . import __version__
from .project import Project
from .store import DecisionError

PROTOCOL = "2025-06-18"

_S, _I = {"type": "string"}, {"type": "integer"}
OPTION = {"type": "object", "properties": {"key": _S, "th": _S, "en": _S, "desc_th": _S, "desc_en": _S, "recommended": {"type": "boolean"},
                                          "pros": {"type": "array", "items": _S}, "cons": {"type": "array", "items": _S}}, "required": ["key", "th"]}


def _tool(name: str, desc: str, props: Dict[str, Any], req: List[str]) -> Dict[str, Any]:
    return {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": props, "required": req}}


TOOLS = [
    _tool("dtree_ask", "Raise a question for the person to decide (bilingual Thai/English title and question, options with one recommended). "
          "Write \"#N\" in the text when it follows from question N. Returns the question with its id; the person answers on the tree page.",
          {"title_th": _S, "title_en": _S, "question_th": _S, "question_en": _S, "context_th": _S, "context_en": _S,
           "options": {"type": "array", "items": OPTION}, "refs": {"type": "array", "items": _S}, "key": _S, "kind": {"type": "string", "enum": ["decision", "problem"]},
           "follows": _I, "follows_how": {"type": "string", "enum": ["topic", "sequence"]}, "follows_why": _S, "follows_option": _S},
          ["options"]),                                            # a title and a question in English (title_en, question_en) or Thai; the other language takes the same words
    _tool("dtree_context", "The decisions in force (decided, not superseded), one line each: read this before working, and ask before going "
          "against one.", {}, []),
    _tool("dtree_list", "List questions by status (open, decided, done, dismissed, closed, all); decided ones are waiting for their work.",
          {"status": _S, "q": _S, "limit": _I}, []),
    _tool("dtree_get", "One question with its options, answer, note, history, trials and jobs, and what its AI jobs used (tokens, dollars).", {"id": _I}, ["id"]),
    _tool("dtree_done", "The decided work is finished: the tests run with their counts and what changed.", {"id": _I, "tests": _S, "impact": _S}, ["id", "tests", "impact"]),
    _tool("dtree_comment", "Add a note to a question's history.", {"id": _I, "note": _S}, ["id", "note"]),
    _tool("dtree_trials", "Trials of other options (status open = being tried or measured).", {"status": _S}, []),
    _tool("dtree_measured", "A trial is built and measured with the same tests as the answer.", {"trial": _I, "tests": _S, "impact": _S}, ["trial", "tests", "impact"]),
    _tool("dtree_code", "Record which commits are a question's (or a trial's) work.", {"id": _I, "shas": {"type": "array", "items": _S}, "why": _S, "trial": _I}, ["id", "shas", "why"]),
]


class Server:
    def __init__(self, project: Project, by: str = "agent") -> None:
        self.p, self.by = project, by

    def call(self, name: str, a: Dict[str, Any]) -> Any:
        s = self.p.store
        if name == "dtree_ask":
            return s.add(dict(a, source="agent"), by=self.by)
        if name == "dtree_context":
            from .adr import context
            return context(self.p)
        if name == "dtree_list":
            return s.list(a.get("status") or "open", int(a.get("limit") or 100), a.get("q") or "")
        if name == "dtree_get":
            from . import spend
            item = s.get(int(a["id"]))
            item["usage"] = spend.usage(self.p, int(item["id"])).get(int(item["id"]))
            return item
        if name == "dtree_done":
            return s.done(int(a["id"]), a.get("tests") or "", a.get("impact") or "", by=self.by)
        if name == "dtree_comment":
            return s.comment(int(a["id"]), a.get("note") or "", by=self.by)
        if name == "dtree_trials":
            return s.trials(a.get("status") or "open")
        if name == "dtree_measured":
            return s.trial_result(int(a["trial"]), a.get("tests") or "", a.get("impact") or "", by=self.by)
        if name == "dtree_code":
            found = []
            for sha in a.get("shas") or []:
                info = self.p.git.commit_info(str(sha).lower())
                if info is None:
                    raise DecisionError("no commit %s in this repository" % sha)
                found.append({"sha": info["sha"], "subject": info["subject"], "merge": info["merge"]})
            return s.record_code(int(a["id"]), found, how="recorded", why=a.get("why") or "", trial=a.get("trial"), by=self.by)
        raise KeyError(name)

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:                                                     # a notification (initialized, cancelled): no answer
            return None
        def ok(result):
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        if method == "initialize":
            return ok({"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "dtree", "version": __version__},
                       "instructions": "dtree: raise questions for the person with dtree_ask, read answers with dtree_list status=decided, "
                                       "close finished work with dtree_done. Project %s." % self.p.config["name"]})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            try:
                out = self.call(name, args)
                return ok({"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}], "isError": False})
            except KeyError as exc:
                return ok({"content": [{"type": "text", "text": "not found: %s" % exc}], "isError": True})
            except (DecisionError, ValueError, TypeError) as exc:
                return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}


def serve_stdio(project: Project, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    server = Server(project)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out: Optional[Dict[str, Any]] = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = server.handle(msg) if isinstance(msg, dict) else {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()
