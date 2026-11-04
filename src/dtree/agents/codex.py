"""Codex CLI: ``codex exec --json -s workspace-write -C <worktree> --add-dir <git dir> --output-schema <result> -o <last message> -``.

Codex's own sandbox lets it write only in the worktree and the repository's git directory (so it can commit) and gives it no
network. It needs a Codex login on this machine (``codex login``); ``available`` asks ``codex login status``.

Tokens: each ``turn.completed`` carries the turn's usage (input / output); the job's token cap (``max_tokens_total`` or
``budget.job_tokens``, lowered to what is left of the month's and the agent's caps: ``spend.limits``) stops Codex's process group when
the sum reaches it. ``codex exec`` reports usage only at the end of a turn (one turn per job), so the cap is mostly seen when the work
is done: the job then fails with ``budget: N tokens of cap M``. A job that ends before any turn completed (stopped, timed out, its turn
failed) reported no usage: its tokens are unknown (None), not 0, so its dollars are unknown too rather than free."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .. import spend
from ..gitcode import read_file
from . import RESULT_SCHEMA, Agent, Context
from ._proc import clean_env, find, short, stream

GLOBS = [".vscode/extensions/openai.chatgpt-*/bin/linux-x86_64/codex", ".vscode/extensions/openai.chatgpt-*/bin/*/codex"]
#: the most dtree reads of Codex's last message (``-o``)
RESULT_MAX = 4 * 1024 * 1024


def describe(ev: Dict[str, Any]) -> List[str]:
    kind = ev.get("type")
    if kind == "thread.started":
        return ["▶ thread %s" % ev.get("thread_id", "?")]
    if kind in ("turn.failed", "error"):
        return ["⚠ " + short(ev.get("error") or ev.get("message") or ev)]
    if kind == "turn.completed":
        u = ev.get("usage") or {}
        return ["■ turn · %s in / %s out tokens" % (u.get("input_tokens", "?"), u.get("output_tokens", "?"))]
    if kind != "item.completed":
        return []
    it = ev.get("item") or {}
    t = it.get("type") or it.get("item_type")
    if t == "agent_message" and (it.get("text") or "").strip():
        return ["💬 " + short(it["text"], 2000)]
    if t == "command_execution":
        return ["🔧 %s%s" % (short(it.get("command")), "" if it.get("exit_code") in (0, None) else "  → exit %s" % it.get("exit_code"))]
    if t == "file_change":
        return ["✎ " + ", ".join("%s %s" % (c.get("kind", ""), c.get("path", "")) for c in it.get("changes") or [])]
    if t == "error":
        return ["⚠ " + short(it.get("message"))]
    return []


class CodexAgent(Agent):
    kind = "codex"

    def binary(self):
        return find(self.cfg.get("command"), "codex", GLOBS)

    def available(self) -> Tuple[bool, str]:
        b = self.binary()
        if not b:
            return False, "Codex is not found (agents.%s.command)" % self.name
        try:
            r = subprocess.run([b, "login", "status"], capture_output=True, text=True, timeout=20, env=clean_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, "codex login status failed: %s" % exc
        text = (r.stdout + r.stderr).strip()
        if r.returncode != 0 or "not logged in" in text.lower():
            return False, "Codex is not logged in on this machine (run: codex login)"
        return True, b

    def describe(self) -> Dict[str, Any]:
        mode = str(self.cfg.get("sandbox") or "workspace-write")
        return dict({"sandbox": "codex " + mode, "network": "host" if mode == "danger-full-access" else "none", "max_budget_usd": None,
                     "max_tokens_total": spend.job_caps(self.project, self.cfg)["max_tokens_total"], "prices": spend.prices(self.cfg)},
                    **spend.month_caps(self.cfg), **spend.total_caps(self.cfg))

    def _private(self) -> Optional[str]:
        """Where Codex's schema and last message go: the project's logs directory, which Codex's sandbox cannot write (it writes the
        worktree, the shadow, /tmp and $TMPDIR), so its commands cannot put a FIFO or a link where dtree reads the result (review of
        round 4). None (the system's temporary directory) when it cannot be made."""
        try:
            logs = self.project.path(self.project.config["logs"])
            logs.mkdir(parents=True, exist_ok=True)
            return str(logs)
        except (OSError, KeyError, TypeError, AttributeError):
            return None

    def run(self, prompt: str, ctx: Context) -> Dict[str, Any]:
        limits = getattr(ctx, "limits", None) or spend.limits(self.project, self.cfg)
        cap = limits.get("max_tokens_total")
        left_of = (limits.get("from") or {}).get("max_tokens_total")
        if cap is not None and cap <= 0:                             # nothing left of a month's or a total's cap (a race)
            why = "budget: nothing was left of %s; Codex was not started" % (left_of or "its token cap")
            ctx.say("■ " + why)
            return {"error": why, "tokens": 0, "tokens_in": 0, "tokens_out": 0}
        with tempfile.TemporaryDirectory(prefix="dtree-codex-", dir=self._private()) as tmp:
            schema, last = os.path.join(tmp, "schema.json"), os.path.join(tmp, "last.json")
            with open(schema, "w", encoding="utf-8") as f:
                json.dump(RESULT_SCHEMA, f)
            cmd = [self.binary(), "exec", "--json", "-s", str(self.cfg.get("sandbox") or "workspace-write"), "-C", str(ctx.worktree),
                   "--add-dir", str(ctx.git_dir), "--output-schema", schema, "-o", last]
            if self.cfg.get("model"):
                cmd += ["-m", str(self.cfg["model"])]
            cmd.append("-")
            thread, failed, over, tin, tout, seen = None, None, None, 0, 0, False
            pids: List[int] = []
            set_pid = ctx.set_pid

            def remember(pid: Optional[int]) -> None:                # the group to stop at the cap
                if pid:
                    pids.append(pid)
                set_pid(pid)
            ctx.set_pid = remember
            try:
                for ev in stream(cmd, ctx.worktree, prompt, ctx, getattr(ctx, "raw_log", None), end_group=True):
                    for line in describe(ev):
                        ctx.say(line)
                    if ev.get("type") == "thread.started":
                        thread = ev.get("thread_id")
                    elif ev.get("type") == "turn.completed":
                        u = ev.get("usage") or {}
                        seen = True
                        tin += int(u.get("input_tokens") or 0)
                        tout += int(u.get("output_tokens") or 0)
                        if cap is not None and tin + tout >= cap and over is None:
                            over = "budget: %d tokens of cap %d%s" % (tin + tout, cap, " (what was left of %s)" % left_of if left_of else "")
                            ctx.say("■ %s: Codex is stopped" % over)
                            for pid in pids[-1:]:
                                try:
                                    os.killpg(pid, signal.SIGTERM)
                                except OSError:
                                    pass
                    elif ev.get("type") in ("turn.failed", "error"):
                        failed = short(ev.get("error") or ev.get("message") or ev)
            finally:
                ctx.set_pid = set_pid
            try:                                                     # never a FIFO's open, which would wait for ever
                out = json.loads(read_file(last, follow=False, limit=RESULT_MAX) or "{}")
            except (OSError, ValueError):
                out = {}
        out = out if isinstance(out, dict) else {}
        if seen:
            out.update(session=thread, tokens=tin + tout, tokens_in=tin, tokens_out=tout)
        else:                                                        # unknown, not free: no turn completed, so no usage came
            out.update(session=thread, tokens=None, tokens_in=None, tokens_out=None)
            ctx.say("■ Codex reported no usage (no turn completed): its tokens, and so its dollars, are unknown")
        if getattr(ctx, "stop_reason", None):
            out["error"] = ctx.stop_reason
        elif over:
            out["error"] = over
        elif failed and not out.get("built"):
            out["error"] = "Codex failed: " + failed
        elif "built" not in out:
            out["error"] = "Codex ended without the result (exit %s)" % getattr(ctx, "exit_code", "?")
        return out
