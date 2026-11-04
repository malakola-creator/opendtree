"""Claude Code, headless: ``claude -p --output-format stream-json --verbose --permission-mode <mode> --json-schema <result>``, inside
bubblewrap unless the agent's ``sandbox`` is ``none``.

Nobody answers permission prompts during a job, so the default mode is ``auto`` (Claude Code's classifier blocks risky actions);
``disallowed_tools`` forbids again the git commands that change branches, merge or push, and sudo / ssh, and Claude Code's own tools
that act outside the job through the account or the web (WebSearch, WebFetch, RemoteTrigger, CronCreate / CronDelete / CronList,
Workflow, SendMessage: names from a real session's init event, Claude Code 2.1.314) or move it to another worktree (EnterWorktree,
ExitWorktree). They reach api.anthropic.com, which the proxy must let through, so only this list keeps them out.

The sandbox (``sandbox: bwrap``, the default; when bwrap cannot make namespaces here the agent is not available — it never runs
unsandboxed by itself). From the probes of 2026-11-01 (Claude Code 2.1.314, bubblewrap 0.9.0):

- the project's sandbox root (``sandbox.root``: an allow-list root by default, with an empty $HOME), the worktree and the git
  repository it belongs to (the trial's own shadow repository, jobs._shadow) writable so it can commit; the project's .git read-only;
  what git on the host would act on later read-only (``sandbox.agent_mounts``); and the binary's own install read-only. The mount
  points bwrap makes in the worktree for ``sandbox.ro_binds`` / ``tmpfs`` are removed after the run (``_mount_points``): an empty
  directory left there would take the place of the next job's bind;
- ``~/.claude`` a private tmpfs: Claude Code writes its session state there, and the person's settings, hooks, CLAUDE.md, skills,
  agents and plugins are absent. A read-only view of the real one still ran the person's hooks. ``--strict-mcp-config`` drops the
  account's connectors;
- ``~/.claude/.credentials.json`` bound read-only into it: without it Claude Code is not logged in. ``credentials: rw`` binds it
  writable, for jobs long enough to need a token refresh (untested: an expiry cannot be forced safely);
- the network (``network``): ``proxy`` (default) — a network namespace of its own with only loopback, and the hosts of
  ``egress.CLAUDE_HOSTS`` + ``network_allow`` through dtree's CONNECT proxy (egress.py); ``host`` — the host's network, its localhost
  services included; ``none``;
- the environment cleared (the parent session's CLAUDE_* variables go with it), then the proxy, ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC``,
  ``DISABLE_TELEMETRY``, ``DISABLE_ERROR_REPORTING`` and the project's ``sandbox.env`` (bwrap's ``--setenv``). The login variables of
  ``pass_env`` never go on a command line, which /proc shows every local user: they are written to a 0600 file in a 0700 directory of
  the job, bound read-only, read by the sandbox's shell before it starts Claude Code, and deleted as soon as Claude Code answers;
- the process the job records is bwrap, leader of its own process group. The runner's watcher, on a stop (jobs.stop asks it) or the
  deadline, sends SIGINT to the command's group — Claude Code then still writes its result, cost included — and SIGKILL to bwrap's
  group after ``GRACE_S`` (the pid namespace takes every child with it); a second stop from the page ends bwrap's group at once.

The job's dollar cap goes to ``--max-budget-usd``: its own (``max_budget_usd`` / ``budget.job_usd``), lowered to what is left of
``budget.month_usd`` and of the agent's ``month_usd`` / ``total_usd`` (``spend.limits``; with nothing left the job does not start).
Claude Code checks it between turns, so one turn can go past it; the result then has the subtype ``error_max_budget_usd`` and the job
fails with ``budget: $x of $cap``. Tokens come from the result; a job that ends with no result (a hard stop, a crash) still counts the
tokens of its API calls, read from the assistant events (``_count_call``: the input in full, the output at least), so the budget does
not take it for free.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import egress, sandbox, spend
from . import RESULT_SCHEMA, Agent, Context
from ._proc import clean_env, find, short, stream

GLOBS = [".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude", ".local/share/claude/versions/*"]
#: Claude Code's other ways to log in, passed into the sandbox when the runner has them (``pass_env`` replaces the list)
PASS_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
#: nothing but the work goes out: no telemetry, error reports or update checks
QUIET_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1"}
#: what an error result says when the login is the problem ("Not logged in · Please run /login", an expired token, a 401)
LOGIN = re.compile(r"not logged in|please run /login|oauth token (has )?expired|authentication_error|invalid (x-)?api[ -]?key|\b401\b", re.I)
GRACE_S = 10.0
_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: the sandbox's shell reads the job's private variables, then becomes Claude Code ($1 = the file; a missing file ends the shell)
ENV_SCRIPT = '. "$1" || exit 98; shift; exec "$@"'
#: what an API call's usage counts (input with the cached context, as the result's modelUsage does)
_USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def describe(ev: Dict[str, Any]) -> List[str]:
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init":
        return ["▶ session %s · model %s" % (ev.get("session_id", "?"), ev.get("model", "?"))]
    out = []
    if kind == "assistant":
        for c in (ev.get("message") or {}).get("content") or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append("💬 " + short(c["text"], 2000))
            elif c.get("type") == "tool_use":
                inp = c.get("input") or {}
                out.append("🔧 %s %s" % (c.get("name"), short(inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") or inp)))
    elif kind == "user":
        for c in (ev.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                out.append("⚠ " + short(c.get("content")))
    elif kind == "result":
        out.append("■ %s · %s turns · $%.2f" % (ev.get("subtype"), ev.get("num_turns"), float(ev.get("total_cost_usd") or 0)))
    return out


def _usd(x: Any) -> str:
    x = float(x or 0)
    return "$%.4f" % x if 0 < x < 0.01 else "$%.2f" % x


def _tokens(final: Dict[str, Any]) -> Dict[str, int]:
    """{tokens_in, tokens_out, tokens} of the whole session from the result: ``modelUsage`` (every model it used, and still filled
    when the budget ended it, where ``usage`` is all zeros), else ``usage``. Input counts the cached context too."""
    per_model = [m for m in (final.get("modelUsage") or {}).values() if isinstance(m, dict)]
    if per_model:
        tin = sum(int(m.get(k) or 0) for m in per_model for k in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
        tout = sum(int(m.get("outputTokens") or 0) for m in per_model)
    elif final.get("usage"):
        u = final["usage"]
        tin = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        tout = int(u.get("output_tokens") or 0)
    else:
        return {}
    return {"tokens_in": tin, "tokens_out": tout, "tokens": tin + tout}


def _count_call(calls: Dict[str, Dict[str, int]], ev: Dict[str, Any]) -> None:
    """Keep an assistant event's usage under its API call (``message.id``). Claude Code prints one event per content block, each with
    the call's usage so far, so a call is counted once: the largest of each number seen for it. Its input is complete when it is
    printed; its output only as far as the printed blocks went (a real run of 2026-11-02: input 77,003 = the result's, output 70 of
    the result's 1,108), so for a job without a result the output is a lower bound."""
    m = ev.get("message") or {}
    u = m.get("usage")
    if not isinstance(u, dict):
        return
    row = calls.setdefault(str(m.get("id") or "event-%d" % len(calls)), {})
    for k in _USAGE:
        try:
            row[k] = max(row.get(k, 0), int(u.get(k) or 0))
        except (TypeError, ValueError):
            pass


def _call_tokens(calls: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    """{tokens_in, tokens_out, tokens} over the API calls ``_count_call`` kept: what a job that wrote no result still spent."""
    tin = sum(r.get(k, 0) for r in calls.values() for k in _USAGE[:3])
    tout = sum(r.get("output_tokens", 0) for r in calls.values())
    return {"tokens_in": tin, "tokens_out": tout, "tokens": tin + tout}


def _env_file(values: Dict[str, str]) -> Tuple[str, str]:
    """(a private directory, the file in it): ``values`` as ``export NAME='value'`` lines, the file 0600 in a 0700 directory of its own
    (bound read-only into the sandbox, read by ``ENV_SCRIPT``), so a login variable's value is on no command line."""
    d = tempfile.mkdtemp(prefix="dtree-job-")
    path = os.path.join(d, "env")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for k, v in values.items():
                f.write("export %s=%s\n" % (k, shlex.quote(v)))
    except BaseException:                                            # a full disk: nothing half-written stays behind
        shutil.rmtree(d, ignore_errors=True)
        raise
    return d, path


_mount_points = sandbox.mount_points                                  # shared with the chat loop's run tool


def install_dir(binary: str) -> str:
    """What the sandbox shows of the binary: its VS Code extension (``…/extensions/anthropic.claude-code-X``), the npm package
    (``…/node_modules/@anthropic-ai/claude-code``), else the file itself."""
    real = os.path.realpath(binary)
    parts = Path(real).parts
    for i, p in enumerate(parts[:-2]):
        if p == "extensions":
            return str(Path(*parts[:i + 2]))
        if p == "node_modules" and parts[i + 1] == "@anthropic-ai" and i + 2 < len(parts) - 1:
            return str(Path(*parts[:i + 3]))
    return real


class ClaudeAgent(Agent):
    kind = "claude"

    # ------------------------------------------------------------------------------------------------------------ settings
    def binary(self):
        return find(self.cfg.get("command"), "claude", GLOBS)

    def _mode(self) -> str:
        return str(self.cfg.get("sandbox") or "bwrap")

    def _network(self) -> str:
        return str(self.cfg.get("network") or "proxy")

    def _credentials(self) -> str:
        return str(self.cfg.get("credentials") or "ro")

    def _allow(self) -> List[str]:
        """The hosts the proxy lets through: Claude Code's own and the agent's ``network_allow``."""
        out: List[str] = []
        for h in list(egress.CLAUDE_HOSTS) + [str(x) for x in self.cfg.get("network_allow") or []]:
            if h not in out:
                out.append(h)
        return out

    def _pass_env(self) -> List[str]:
        names = self.cfg.get("pass_env")
        return [str(n) for n in (PASS_ENV if names is None else names)]

    def _secrets(self) -> Dict[str, str]:
        """The variables of ``pass_env`` the runner has, with their values (they go through a private file: ``_env_file``)."""
        return {k: os.environ[k] for k in self._pass_env() if _VAR.match(k) and os.environ.get(k)}

    def available(self) -> Tuple[bool, str]:
        b = self.binary()
        if not b:
            return False, "Claude Code is not found (agents.%s.command)" % self.name
        mode = self._mode()
        if mode not in ("bwrap", "none"):
            return False, "agents.%s.sandbox is bwrap or none, not %r" % (self.name, mode)
        if mode == "none":
            return True, b
        ok, why = sandbox.check()
        if not ok:
            return False, "%s; Claude Code runs only in the sandbox here (agents.%s.sandbox \"none\" runs it without one)" % (why, self.name)
        net, cred = self._network(), self._credentials()
        if net not in ("proxy", "host", "none"):
            return False, "agents.%s.network is proxy, host or none, not %r" % (self.name, net)
        if cred not in ("ro", "rw"):
            return False, "agents.%s.credentials is ro or rw, not %r" % (self.name, cred)
        bad = [n for n in self._pass_env() if not _VAR.match(n)]
        if bad:
            return False, "agents.%s.pass_env: %r is not a variable name" % (self.name, bad[0])
        if net == "proxy":
            if not os.access("/usr/bin/python3", os.X_OK):
                return False, "network proxy needs /usr/bin/python3 for the forwarder inside the sandbox (or agents.%s.network host / none)" % self.name
            try:
                egress.parse(self._allow())
            except ValueError as exc:
                return False, "agents.%s.%s" % (self.name, exc)
        creds = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
        if not os.path.isfile(creds) and not any(os.environ.get(k) for k in self._pass_env()):
            return False, "no Claude login for the sandbox: %s is missing (run claude here and log in)" % creds
        return True, b

    def describe(self) -> Dict[str, Any]:
        boxed = self._mode() == "bwrap"
        lim = spend.job_caps(self.project, self.cfg)
        out: Dict[str, Any] = {"sandbox": self._mode(), "network": self._network() if boxed else "host",
                               "credentials": self._credentials() if boxed else None, "max_budget_usd": lim["max_budget_usd"],
                               "max_tokens_total": None, "prices": spend.prices(self.cfg)}
        out.update(spend.month_caps(self.cfg))
        out.update(spend.total_caps(self.cfg))
        if boxed and out["network"] == "proxy":
            out["network_allow"] = self._allow()
        return out

    # ------------------------------------------------------------------------------------------------------------ the run
    def _command(self, binary: str, boxed: bool, cap: Optional[float]) -> List[str]:
        cmd = [binary, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", str(self.cfg.get("permission_mode") or "auto"),
               "--json-schema", json.dumps(RESULT_SCHEMA)]
        if self.cfg.get("model"):
            cmd += ["--model", str(self.cfg["model"])]
        if boxed:
            cmd.append("--strict-mcp-config")                         # no connectors from the account (~/.claude.json)
        if cap is not None:
            cmd += ["--max-budget-usd", "%g" % cap]
        if self.cfg.get("disallowed_tools"):                          # last: the flag takes every argument after it
            cmd += ["--disallowedTools"] + [str(x) for x in self.cfg["disallowed_tools"]]
        return cmd

    def _mounts(self, binary: str, ctx: Context, proxy: Optional[egress.Proxy], private: Optional[str] = None) -> List[sandbox.Mount]:
        home = os.path.expanduser("~")
        dot = os.path.join(home, ".claude")
        creds = os.path.join(dot, ".credentials.json")
        inst = install_dir(binary)
        out: List[sandbox.Mount] = [("ro-try", inst, inst)]             # first: the worktree's own binds come after it
        out += sandbox.agent_mounts(self.project, ctx.worktree, ctx.git_dir)
        out.append(("tmpfs", dot))
        if os.path.isfile(creds):
            out.append(("rw", creds) if self._credentials() == "rw" else ("ro", creds, creds))
        if proxy is not None:
            out.append(("ro", proxy.dir, proxy.dir))
        if private is not None:                                        # the login variables' file (_env_file)
            out.append(("ro", private, private))
        for p in self.cfg.get("rw_paths") or []:
            out.append(("rw", os.path.expanduser(str(p))))
        return out

    def _env(self, ctx: Context, proxy: Optional[egress.Proxy]) -> Dict[str, str]:
        """What goes on bwrap's command line (``--setenv``): nothing secret; the login variables take ``_env_file``."""
        env = sandbox.project_env(self.project)
        env.update(QUIET_ENV)
        env["DTREE_JOB"] = str(getattr(ctx, "job", ""))
        if proxy is not None:
            env.update(egress.proxy_env())
        return env

    def _note(self, network: str, root: str = "allowlist") -> str:
        """What the agent is told about its sandbox, so it does not spend turns finding out (in step with jobs.prompt_for)."""
        net = {"proxy": "The network reaches only %s (https, through dtree's proxy); every other host and the machine's localhost "
                        "services are refused, so do not try to download or install from elsewhere." % ", ".join(self._allow()),
               "host": "The network is the machine's own, its localhost services included: never write to them.",
               "none": "There is no network."}[network]
        more = [os.path.expanduser(str(p)) for p in self.cfg.get("rw_paths") or []]
        write = ("You can write only the working directory%s and the git repository it belongs to (to commit; its config and hooks are "
                 "read-only)" % "".join(", " + p for p in more))
        if root == "host":
            see = ("; the rest of the filesystem, the main checkout included, is read-only, and the person's keys and agent settings are "
                   "hidden. /tmp is private and thrown away at the end. ")
        else:
            see = (". The main checkout is not visible here (only its .git, read-only); git-ignored files the project shares "
                   "(sandbox.ro_binds) appear read-only at the same place in the working directory. /tmp and your home directory are "
                   "private and thrown away at the end (none of the person's files are in them). ")
        return "Sandbox: this job runs in bubblewrap. " + write + see + net

    def run(self, prompt: str, ctx: Context) -> Dict[str, Any]:
        limits = getattr(ctx, "limits", None) or spend.limits(self.project, self.cfg)
        cap = limits.get("max_budget_usd")
        left_of = (limits.get("from") or {}).get("max_budget_usd")   # the setting whose remainder the cap is, if one is
        if cap is not None and cap <= 0:                             # nothing left of a month's or a total's cap (a race)
            why = "budget: nothing was left of %s; Claude Code was not started" % (left_of or "its dollar cap")
            ctx.say("■ " + why)
            return {"error": why, "cost_usd": 0.0, "tokens": 0, "tokens_in": 0, "tokens_out": 0}
        binary = os.path.realpath(self.binary() or "claude")
        boxed = self._mode() == "bwrap"
        network = self._network()
        cmd = self._command(binary, boxed, cap)
        final: Dict[str, Any] = {}
        calls: Dict[str, Dict[str, int]] = {}                         # each API call's usage: what counts when no result arrives
        session = None
        proxy: Optional[egress.Proxy] = None
        events: Any = None
        private: Optional[str] = None
        made: List[str] = []
        out: Dict[str, Any] = {}
        ctx.box_error = None
        try:
            if boxed:
                secrets = self._secrets()
                if secrets:                                          # innermost: the forwarder never has them
                    private, path = _env_file(secrets)
                    cmd = ["/bin/sh", "-c", ENV_SCRIPT, egress.SHELL_NAME, path] + cmd
                if network == "proxy":
                    proxy = egress.Proxy(self._allow(), ctx.say).start()
                    cmd = egress.wrap(cmd, proxy)
                ps = sandbox.project_settings(self.project)
                made = _mount_points(self.project, ctx.worktree)
                box = {"mounts": self._mounts(binary, ctx, proxy, private), "network": "host" if network == "host" else "none",
                       "root": ps["root"], "nested": ps["nested"], "env": self._env(ctx, proxy)}
                ctx.say("🔒 sandbox: bwrap · root %s · network %s%s%s" % (
                    ps["root"], network, (" → " + ", ".join(self._allow())) if proxy else "",
                    (" · login variables %s (a private file)" % ", ".join(sorted(secrets))) if secrets else ""))
                events = _boxed_stream(cmd, box, ctx.worktree, prompt + "\n\n" + self._note(network, ps["root"]), ctx, getattr(ctx, "raw_log", None))
            else:
                events = stream(cmd, ctx.worktree, prompt, ctx, getattr(ctx, "raw_log", None), clean_env({"DTREE_JOB": str(getattr(ctx, "job", ""))}))
            for ev in events:
                if private is not None:                              # Claude Code answers: the shell has read the file
                    shutil.rmtree(private, ignore_errors=True)
                    private = None
                for line in describe(ev):
                    ctx.say(line)
                kind = ev.get("type")
                if kind == "system" and ev.get("subtype") == "init":
                    session = ev.get("session_id")
                elif kind == "assistant":
                    _count_call(calls, ev)
                elif kind == "result":
                    final = ev
                    if float(ev.get("total_cost_usd") or 0) > 0:        # a $0 after real calls is a report cut short (below)
                        ctx.set_cost(float(ev["total_cost_usd"]))
        finally:
            if events is not None:
                events.close()                                       # a failure above still ends the sandbox
            if private is not None:
                shutil.rmtree(private, ignore_errors=True)
            if made:
                sandbox.cleanup_mount_points(ctx.worktree, made)
            if proxy is not None:
                out["egress"] = proxy.summary()
                proxy.stop()
        out.update(final.get("structured_output") or {})
        out.update(session=final.get("session_id") or session, turns=final.get("num_turns") if final else (len(calls) or None),
                   cost_usd=final.get("total_cost_usd"))
        tokens = _tokens(final)
        if not tokens.get("tokens") and calls:
            tokens = _call_tokens(calls)
            ctx.say("■ %s tokens counted from Claude Code's %d API calls (%s; their input in full, their output at least)" % (
                "{:,}".format(tokens["tokens"]), len(calls), "its result had none" if final else "it wrote no result"))
            if tokens["tokens"] and not float(final.get("total_cost_usd") or 0) > 0:
                out["cost_usd"] = None                               # unknown, not free: spend.after_job estimates it from prices
        out.update(tokens)
        if getattr(ctx, "box_error", None) and not final and not calls:   # bwrap never started it: nothing was spent, nothing unknown
            out.update(cost_usd=0.0, tokens=0, tokens_in=0, tokens_out=0)
        why = self._failure(final, ctx, cap, left_of)
        if why:
            out["error"] = why
        return out

    def _failure(self, final: Dict[str, Any], ctx: Context, cap: Optional[float], left_of: Optional[str] = None) -> Optional[str]:
        if getattr(ctx, "stop_reason", None):
            return ctx.stop_reason
        if getattr(ctx, "box_error", None):
            return "the sandbox did not start: %s" % ctx.box_error
        if final.get("subtype") == "error_max_budget_usd":
            return "budget: %s of %s%s" % (_usd(final.get("total_cost_usd")), _usd(cap) if cap is not None else "the cap",
                                           " (what was left of %s)" % left_of if left_of else "")
        said = " ".join(str(x) for x in [final.get("result") or ""] + list(final.get("errors") or [])).strip()
        if final.get("is_error") and (LOGIN.search(said) or final.get("api_error_status") == 401):
            return ("the Claude login needs a refresh (Claude Code said: %s): run claude on this machine and log in, then send the job "
                    "again; agents.%s.credentials \"rw\" lets a long job refresh its token itself" % (short(said, 160), self.name))
        if not final or final.get("is_error"):
            tail = (getattr(ctx, "stderr_tail", "") or "").strip().splitlines()
            more = (": " + short(said, 300)) if said else (": " + short(tail[-1], 300)) if tail else ""
            return "Claude Code ended with %s%s" % (final.get("subtype") or "no result (exit %s)" % getattr(ctx, "exit_code", "?"), more)
        return None


def _boxed_stream(cmd: List[str], box: Dict[str, Any], cwd: Any, stdin_text: str, ctx: Any, raw_log: Optional[Any]) -> Iterator[Dict[str, Any]]:
    """``stream`` in bubblewrap: start ``cmd`` in the sandbox ``box`` (``sandbox.build`` keywords), give it ``stdin_text``, yield its
    JSON lines. bwrap's status fd tells a sandbox that never started (``ctx.box_error``, quoting bwrap) from a command that ended; a stop
    or the deadline sends SIGINT to the command's group, then SIGKILL to bwrap's (``ctx.stop_reason``)."""
    r_fd, w_fd = os.pipe()
    try:
        argv = sandbox.build(cwd, status_fd=w_fd, **box) + list(cmd)
        proc = subprocess.Popen(argv, cwd=str(cwd), env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding="utf-8", errors="replace", start_new_session=True, pass_fds=(w_fd,))
    except BaseException:
        os.close(r_fd)
        os.close(w_fd)
        raise
    os.close(w_fd)
    status = sandbox._Status(r_fd)
    status.start()
    ctx.set_pid(proc.pid)
    ctx.stop_reason, ctx.box_error, ctx.stderr_tail = None, None, ""
    lock = threading.Lock()
    err: List[str] = []
    done = threading.Event()

    def read_err() -> None:
        for line in proc.stderr:
            with lock:
                if raw_log is not None:
                    raw_log.write(line)
                err.append(line)
                del err[:-40]

    def watch() -> None:
        while not done.wait(1.0):
            why = "stopped" if ctx.stopped() else "timeout" if time.time() > ctx.deadline else None
            if why:
                ctx.stop_reason = why
                sandbox.interrupt(proc, status.child_pid, GRACE_S)
                return

    errs = threading.Thread(target=read_err, daemon=True)
    errs.start()
    threading.Thread(target=watch, daemon=True).start()
    try:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except OSError:                                              # the sandbox never started: nobody reads
            pass
        for line in proc.stdout:
            if raw_log is not None:
                with lock:
                    raw_log.write(line)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                yield ev
        proc.wait()
    finally:
        done.set()
        if proc.poll() is None:                                      # the reader gave up early: end it all
            sandbox.interrupt(proc, status.child_pid, 2.0)
        ctx.set_pid(None)
        errs.join(timeout=5)
        status.join(timeout=5)
        code = proc.poll()
        ctx.exit_code = status.exit_code if status.exit_code is not None else code
        ctx.stderr_tail = "".join(err[-5:])
        if ctx.stop_reason is None and ctx.stopped():                # a stop from the page ended bwrap's group (jobs.stop)
            ctx.stop_reason = "stopped"
        if status.exit_code is None and ctx.stop_reason is None:
            first = next((l.strip() for l in err if l.startswith("bwrap: ")), "")
            ctx.box_error = first or ("bwrap ended by signal %d" % -code if code is not None and code < 0 else
                                      "the sandbox did not start (exit %s)" % code)
