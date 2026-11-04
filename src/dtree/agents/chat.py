"""A chat model as a coding agent: dtree's own loop over any OpenAI-compatible ``/chat/completions`` API with tool calls.

For models that have no agent CLI of their own: Muse Spark on the Meta Model API, Grok on the xAI API, a local model on Ollama (or
any llama.cpp / vLLM server). The model gets six tools:

- ``list_files``, ``read_file``, ``write_file``, ``edit_file`` — on paths inside the worktree only (``.git`` is not writable);
- ``run`` — a shell command in the worktree, inside bubblewrap: the whole filesystem read-only except the worktree, the repository's
  git directory (so it can commit) and a private /tmp, with no network; commands that change branches, merge, push, or use sudo /
  ssh are refused before they run (``chat.deny``);
- ``finish`` — the structured result (built, tests, impact, note), which ends the job.

The key is read at call time from ``key_env`` or ``key_file`` and sent only in the Authorization header; it is never logged.

How the loop holds up (the final review and the real race of 2026-11-02):

- each request to the model runs in a thread of its own while the loop watches the job, so a stop or the job's deadline gives the
  request up at once — its connection is shut, so the server can stop working on it — and so does ``request_timeout_s`` (600 s by
  default, from the agent or the ``chat`` section; the Orin's llama.cpp server at ~5 tokens/s took 281 s for one answer, and its
  agent needs 1800). The reason names the setting. An answer that comes in after a stop or the deadline is not acted on;
- every way out keeps the tokens spent: an answer cut short, an answer that is not a chat completion, a failure of the loop itself
  (the job then fails saying what went wrong, and the budget still counts what it spent). A tool that fails — a number given for a
  path, an object for a file's content — tells the model ``error: …`` and the loop goes on. Arguments given as a JSON object instead
  of a string (some servers do) are taken as they are;
- ``finish``'s ``built`` must be a JSON boolean (the words ``true`` / ``false`` are taken too); anything else is sent back to the
  model to say again, and after the third such answer the work is taken as not built;
- the job's log names the sandbox the commands run in, every tool call and the head of its result (a command's exit code and last
  line); ``job-N.jsonl`` has every answer (its tool calls and usage) and every tool result, never the key;
- with ``context_chars`` (a small local model) the older tool results and the large arguments of older tool calls (a write_file's
  content) are cut to a stub, oldest first, until the conversation fits;
- a priced agent's cost so far is written to its job after every answer, so a later job's budget sees it while this one runs;
- an agent's rate limits (``requests_per_minute``, ``requests_per_day``; the user's settings page, 2026-11-02) are shared by every job
  of it: before each request — every try, a busy API's too — a slot is taken in the store (``store.request_slot``, one transaction,
  so runners in several processes are held together). A full minute is waited for (``⏳ requests_per_minute 5: the next request in
  9 s``; a stop or the deadline ends the wait), and so is a full day whose next slot comes within ``retry_max_wait_s``; a day used up
  for longer ends the job (``agents.gemini.requests_per_day (20) is used: the next request in 3 h 12 min``), with nothing in flight
  and nothing more counted. An agent without limits takes its slots too: they only count its requests.
"""
from __future__ import annotations

import errno
import http.client
import json
import os
import re
import shutil
import socket
import stat
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import sandbox, spend
from ..gitcode import read_bytes
from . import RESULT_SCHEMA, Agent, Context
from ._proc import short

DENY = [r"\bgit\s+(push|merge|rebase|switch|checkout|worktree|pull|fetch|clone)\b", r"\bgit\s+reset\s+--hard\b", r"\bgit\s+branch\s+-[dDmM]\b",
        r"\bsudo\b", r"\bssh\b", r"\bscp\b", r"\brsync\b", r"\bcurl\b", r"\bwget\b"]
MAX_RESULT = 12000
MAX_FILE = 400_000
#: the most the read_file and edit_file tools read of one file
MAX_READ = 8_000_000
#: how long one request to the model may take, unless the agent or the chat section says (request_timeout_s)
REQUEST_TIMEOUT_S = 600.0
#: how often the loop looks at the job (a stop, the deadline) while the model thinks
POLL_S = 0.5
#: an API that is busy — 429 (a rate or quota limit: Gemini's free tier takes 5 requests a minute) or 503 (overloaded) — is asked
#: again: at most this many times, after the wait it names (Retry-After, google.rpc.RetryInfo) or 5 s doubling up to 60 s
RETRIES = 8
#: a longer wait than this (a quota for the day) is not waited for: the job ends saying so
RETRY_MAX_WAIT_S = 300.0
BUSY = (429, 503)
CUT = " …[cut to fit the context]"
#: finish answers whose built is neither true nor false, before the work is taken as not built
BAD_FINISH = 3

SYSTEM = ("You are a careful software engineer working alone in a git worktree. Use the tools to look at the code, change it, run the "
          "tests and commit with git (git add / git commit through the run tool). Nobody answers questions during the work. When the "
          "option is built and committed, or you cannot build it, call finish with the honest result.")


def _tools() -> List[Dict[str, Any]]:
    def fn(name, desc, props, req):
        return {"type": "function", "function": {"name": name, "description": desc,
                                                 "parameters": {"type": "object", "properties": props, "required": req}}}
    s, i = {"type": "string"}, {"type": "integer"}
    return [
        fn("list_files", "List files under a directory of the worktree (relative path; default the root).", {"path": s, "max": i}, []),
        fn("read_file", "Read a text file of the worktree with line numbers.", {"path": s, "offset": i, "limit": i}, ["path"]),
        fn("write_file", "Write (create or replace) a text file in the worktree.", {"path": s, "content": s}, ["path", "content"]),
        fn("edit_file", "Replace an exact, unique piece of text in a file of the worktree.", {"path": s, "old": s, "new": s}, ["path", "old", "new"]),
        fn("run", "Run a shell command in the worktree (no network; branches, merges and pushes are refused).", {"command": s, "timeout_s": i}, ["command"]),
        fn("finish", "End the work with the result.", RESULT_SCHEMA["properties"], RESULT_SCHEMA["required"]),
    ]


def _size(m: Dict[str, Any]) -> int:
    return len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or ""))


def _cut_call(call: Any) -> Any:
    """A tool call of an older answer with its large arguments cut to a stub; the arguments stay a JSON object (a server may parse
    them), with the short values (a path) kept."""
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
        return call
    f = dict(call["function"])
    args = _args(f.get("arguments"))
    if args is None:
        args = {"arguments": str(f.get("arguments"))[:120] + CUT}
    else:
        args = {k: v if len(json.dumps(v, ensure_ascii=False)) <= 200 else
                (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))[:120] + CUT for k, v in args.items()}
    f["arguments"] = json.dumps(args, ensure_ascii=False)
    return dict(call, function=f)


def _fit(messages: List[Dict[str, Any]], limit: Any) -> List[Dict[str, Any]]:
    """A model with a small context (a local server, ``context_chars``): the older tool results, and the large arguments of the older
    tool calls (a write_file's content), are cut to a stub, oldest first, until the conversation fits; the system prompt, the task and
    the latest three messages are kept whole, and so is the conversation itself (a copy is cut)."""
    if not limit:
        return messages
    out = [dict(m) for m in messages]
    sizes = [_size(m) for m in out]
    total = sum(sizes)
    for i in range(2, len(out) - 3):
        if total <= int(limit):
            break
        m = out[i]
        if m.get("role") == "tool" and len(str(m.get("content") or "")) > 200:
            m["content"] = str(m["content"])[:120] + CUT
        if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list) and len(json.dumps(m["tool_calls"])) > 400:
            m["tool_calls"] = [_cut_call(c) for c in m["tool_calls"]]
        new = _size(m)
        total += new - sizes[i]
        sizes[i] = new
    return out


def _args(raw: Any) -> Optional[Dict[str, Any]]:
    """A tool call's arguments as a dict: a JSON string (the OpenAI format), or an object as some servers give it; nothing is no
    arguments; None when they are neither."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _built(value: Any) -> Optional[bool]:
    """finish's built as a real yes or no: a JSON boolean, or the word true / false; None for anything else ("false" is not built)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _text(content: Any) -> str:
    """An answer's text: a string, or the text of its parts (some servers give a list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") if isinstance(p, dict) else str(p) for p in content)
    return "" if content is None else str(content)


def _head(name: str, result: str) -> str:
    """What the job's log shows of a tool's result: a command's exit code and its last line, else the result's first line."""
    lines = [l for l in str(result).splitlines() if l.strip()]
    if not lines:
        return "(nothing)"
    if name == "run" and lines[0].startswith("exit "):
        return short(lines[0] + ("" if len(lines) == 1 else " · " + lines[-1]), 300)
    return short(lines[0], 300)


def _what(exc: BaseException) -> str:
    text = str(exc)
    return text if isinstance(exc, RuntimeError) or text.startswith(type(exc).__name__) else "%s: %s" % (type(exc).__name__, text)


class _Ended(Exception):
    """The loop gave the model up: ``str(exc)`` is the job's error (stopped, timeout, or the request's own time limit)."""


class _Busy(RuntimeError):
    """The API said it is busy (``BUSY``): it did not run the request; ``retry_after`` is the wait it named, or None."""

    def __init__(self, message: str, status: int, retry_after: Optional[float]) -> None:
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def _retry_after(headers: Any, body: str) -> Optional[float]:
    """How long an API asks to be left alone: its ``Retry-After`` header in seconds, else the ``retryDelay`` of a google.rpc.RetryInfo in
    its error body ("13s", Gemini's); None when it names none (an HTTP date in Retry-After is not read)."""
    try:
        said = headers.get("Retry-After") if headers is not None else None
        if said is not None and str(said).strip():
            return max(0.0, float(str(said).strip()))
    except (ValueError, AttributeError):
        pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body or "")
    return float(m.group(1)) if m else None


def _pause(seconds: float, ctx: Optional[Context]) -> Optional[str]:
    """Wait ``seconds``, looking at the job every POLL_S: "stopped" / "timeout" as soon as either comes, else None."""
    end = time.time() + max(0.0, seconds)
    while True:
        why = _halt(ctx) if ctx is not None else None
        if why or time.time() >= end:
            return why
        time.sleep(min(POLL_S, max(0.0, end - time.time())))


class _Keeping:
    """A urllib handler mix-in that hands each connection's socket to ``keep`` as soon as it is connected (``_Call.give_up`` shuts it)."""
    keep: Callable[[Any], None] = staticmethod(lambda sock: None)

    def do_open(self, http_class, req, **kw):                        # the one place urllib makes its connection
        keep = self.keep

        def make(*a, **k):
            conn = http_class(*a, **k)
            connect = conn.connect

            def connected():
                connect()
                keep(conn.sock)
            conn.connect = connected
            return conn
        return super().do_open(make, req, **kw)


class _KeepHTTP(_Keeping, urllib.request.HTTPHandler):
    pass


class _KeepHTTPS(_Keeping, urllib.request.HTTPSHandler):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A chat API answers its POST itself: a 3xx is its answer (a base_url to fix), never followed — urllib would send the request
    again elsewhere, as a GET for a 301/302/303, with its key (it keeps the Authorization header), and a model there spends nothing on
    it (review of round 4)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Call:
    """One request to the model, in a thread of its own so the loop can give it up: on a stop, at the job's deadline or after
    request_timeout_s. Giving it up shuts the request's connection, so the server can stop working on it."""

    def __init__(self, req: urllib.request.Request, timeout: float) -> None:
        self.done = threading.Event()
        self.answer: Any = None
        self.error: Optional[BaseException] = None
        self.status: Optional[int] = None
        self.retry_after: Optional[float] = None
        self._socks: List[Any] = []
        self._lock = threading.Lock()
        self._given_up = False
        threading.Thread(target=self._work, args=(req, timeout), name="dtree-chat-request", daemon=True).start()

    def _keep(self, sock: Any) -> None:
        with self._lock:
            self._socks.append(sock)
            late = self._given_up
        if late:
            self._shut(sock)

    def _work(self, req: urllib.request.Request, timeout: float) -> None:
        try:
            handlers = [_KeepHTTP(), _KeepHTTPS()]
            for h in handlers:
                h.keep = self._keep
            handlers.append(_NoRedirect())
            with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
                self.status = getattr(r, "status", None)
                body = r.read()
            try:
                self.answer = json.loads(body)
            except ValueError:
                raise RuntimeError("the API's answer is not JSON: %s" % short(body.decode("utf-8", "replace"), 200))
        except urllib.error.HTTPError as exc:
            self.status = exc.code
            try:
                said = exc.read().decode("utf-8", "replace")
            except Exception:                                        # an error page cut short: its status is enough
                said = ""
            self.retry_after = _retry_after(getattr(exc, "headers", None), said)
            self.error = RuntimeError("the API answered %s: %s" % (exc.code, short(said, 400)))
        except BaseException as exc:                                 # handed to the loop as it is (it decides what it means)
            self.error = exc
        finally:
            self.done.set()

    def went_out(self) -> bool:
        """Whether the server may have spent (and billed) this request though no answer was taken: its connection was made (urllib
        sends the request at once) and no 3xx, 4xx or 503 turned it away. A connection never made, a redirect, a refusal, an API too
        busy to run it: nothing was spent."""
        with self._lock:
            made = bool(self._socks)
        return made and not (self.status is not None and (300 <= self.status < 500 or self.status == 503))

    def give_up(self) -> None:
        with self._lock:
            self._given_up = True
            socks = list(self._socks)
        for s in socks:
            self._shut(s)

    @staticmethod
    def _shut(sock: Any) -> None:
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)            # wakes the thread's read; it closes the socket itself
        except (OSError, TypeError, ValueError):
            pass


def _marked(exc: BaseException, call: _Call, whole: bool) -> BaseException:
    """``exc`` (a request given up, cut short or failed) with what the loop's accounting needs: ``dtree_went_out`` — the server may
    have spent it (``_Call.went_out``) — and ``dtree_whole`` — it carried the whole conversation, nothing cut by context_chars, so it
    held all of the previous request's prompt."""
    try:
        exc.dtree_went_out = call.went_out()                         # type: ignore[attr-defined]
        exc.dtree_whole = whole                                       # type: ignore[attr-defined]
    except Exception:                                                # an exception that takes no attributes: counted as not sent
        pass
    return exc


def _halt(ctx: Context) -> Optional[str]:
    """"stopped" (asked from the page or dtree stop), "timeout" (the job's deadline), or None. A store that cannot be read for a moment
    (the stop is a column of the job) is not a stop: the next look tells."""
    try:
        if ctx.stopped():
            return "stopped"
    except Exception:
        pass
    return "timeout" if time.time() > ctx.deadline else None


def _timed_out(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError))



def _write(p: Path, text: str) -> None:
    """``text`` into the worktree file ``p`` for write_file / edit_file — only ever a regular file: opening a FIFO the job's own
    commands made there to write would wait for a reader for ever (O_NONBLOCK: refused at once instead), and nothing is cut before the
    file is known to be one."""
    if not isinstance(text, str):
        raise TypeError("the content must be a string, not %s" % type(text).__name__)
    if os.path.lexists(str(p)) and not stat.S_ISREG(os.lstat(str(p)).st_mode):   # said plainly (a FIFO with no reader: ENXIO)
        raise OSError(errno.EINVAL, "not a regular file", str(p))
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(p))
        os.ftruncate(fd, 0)
        view = memoryview(text.encode("utf-8"))
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)

class ChatAgent(Agent):
    kind = "chat"

    def _key(self) -> Optional[str]:
        env = self.cfg.get("key_env")
        if env and os.environ.get(env):
            return os.environ[env].strip()
        path = self.cfg.get("key_file")
        if path:
            try:
                return Path(os.path.expanduser(str(path))).read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
        return None

    def _local(self) -> bool:
        host = urllib.parse.urlparse(str(self.cfg.get("base_url") or "")).hostname or ""
        return host in ("127.0.0.1", "localhost", "::1")

    def _settings(self) -> Dict[str, Any]:
        return dict(self.project.config.get("chat") or {}, **{k: v for k, v in self.cfg.items()
                                                               if k in ("max_steps", "max_tokens", "temperature", "command_timeout_s", "sandbox",
                                                                        "context_chars", "result_chars", "request_timeout_s", "retries",
                                                                        "retry_max_wait_s")})

    def available(self) -> Tuple[bool, str]:
        if not self.cfg.get("base_url") or not self.cfg.get("model"):
            return False, "agents.%s needs base_url and model" % self.name
        if (self.cfg.get("key_env") or self.cfg.get("key_file")) and not self._key():
            return False, "no key: set $%s%s" % (self.cfg.get("key_env") or "?", " or agents.%s.key_file" % self.name if not self.cfg.get("key_file") else " / " + str(self.cfg["key_file"]))
        if self._settings().get("sandbox", "bwrap") == "bwrap" and not shutil.which("bwrap"):
            return False, "bubblewrap (bwrap) is not installed; set chat.sandbox to \"none\" only if you accept unsandboxed commands"
        if self._local():
            try:
                urllib.request.urlopen(str(self.cfg["base_url"]).rstrip("/") + "/models", timeout=3).read(1)
            except (OSError, urllib.error.URLError) as exc:
                return False, "the local server at %s does not answer (%s)" % (self.cfg["base_url"], getattr(exc, "reason", exc))
        return True, "%s · %s" % (self.cfg["base_url"], self.cfg["model"])

    def describe(self) -> Dict[str, Any]:
        box = str(self._settings().get("sandbox", "bwrap"))
        return dict({"sandbox": box, "network": "none" if box == "bwrap" else "host", "max_budget_usd": None,
                     "max_tokens_total": spend.job_caps(self.project, self.cfg)["max_tokens_total"], "prices": spend.prices(self.cfg),
                     "request_timeout_s": self._request_timeout(self._settings())},
                    **spend.month_caps(self.cfg), **spend.total_caps(self.cfg), **spend.rate_caps(self.cfg),
                    **spend.requests_now(self.project, self.name, self.cfg))

    def _sandbox_line(self, s: Dict[str, Any]) -> str:
        if s.get("sandbox", "bwrap") == "bwrap":
            return ("🔒 sandbox: bwrap · root %s · network none (the run tool's commands; the model is asked from outside the sandbox)"
                    % sandbox.project_settings(self.project)["root"])
        return "⚠ sandbox: none (chat.sandbox): the run tool's commands run on this machine as they are, with its network"

    # ----------------------------------------------------------------------------------------------------------- the model
    @staticmethod
    def _request_timeout(s: Dict[str, Any]) -> float:
        try:
            return float(s.get("request_timeout_s") or REQUEST_TIMEOUT_S)
        except (TypeError, ValueError):
            return REQUEST_TIMEOUT_S

    def _too_slow(self, limit: float) -> str:
        where = "agents.%s.request_timeout_s" % self.name if self.cfg.get("request_timeout_s") is not None else "chat.request_timeout_s"
        return "the model did not answer within %g s (%s; a slow model needs more)" % (limit, where)

    def _ask(self, messages: List[Dict[str, Any]], s: Dict[str, Any], ctx: Optional[Context] = None) -> Dict[str, Any]:
        """One answer of the model. It is waited for in steps, so a stop or the job's deadline (``_Ended``) and request_timeout_s give
        it up at once."""
        fitted = _fit(messages, s.get("context_chars"))
        body = {"model": self.cfg["model"], "messages": fitted, "tools": _tools(), "tool_choice": "auto",
                "max_tokens": int(s.get("max_tokens") or 4096), "temperature": float(s.get("temperature") or 0.2)}
        body.update(self.cfg.get("extra_body") or {})                  # a server's own fields (llama.cpp: chat_template_kwargs, …)
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(str(self.cfg["base_url"]).rstrip("/") + "/chat/completions", json.dumps(body).encode("utf-8"), headers)
        limit = self._request_timeout(s)
        call = _Call(req, limit)
        give_up = time.time() + limit
        while not call.done.wait(POLL_S):
            why = _halt(ctx) if ctx is not None else None
            if why is None and time.time() > give_up:
                why = self._too_slow(limit)
            if why:
                call.give_up()
                raise _marked(_Ended(why), call, fitted == messages)
        if call.error is not None:
            if _timed_out(call.error):
                raise _marked(_Ended(self._too_slow(limit)), call, fitted == messages)
            if call.status in BUSY:
                raise _Busy(str(call.error), call.status, call.retry_after)
            raise _marked(call.error, call, fitted == messages)
        return call.answer

    def _ask_patiently(self, messages: List[Dict[str, Any]], s: Dict[str, Any], ctx: Optional[Context], tell: Callable[[str], None]) -> Dict[str, Any]:
        """``_ask``, asked again while the API is busy (``BUSY``) — after the wait it names, else 5 s doubling up to 60 s, at most
        ``retries`` times, within the job's time — so a rate limit (Gemini's free tier: 5 requests a minute) slows a job down instead
        of ending it. A stop or the deadline ends a wait at once (``_Ended``, nothing in flight: nothing counted). Busy for longer than
        ``retry_max_wait_s`` (a quota for the day), or still busy after every try: the API's own answer ends the job, saying why.
        Every try first takes a slot of the agent's rate limits (``_slot``), so the limits are met before the API has to refuse."""
        tries = 0
        retries = max(0, int(s.get("retries", RETRIES)))
        cap = float(s.get("retry_max_wait_s") or RETRY_MAX_WAIT_S)
        while True:
            self._slot(ctx, cap, tell)                               # every try takes a slot: a 429 counts against a limit too
            try:
                return self._ask(messages, s, ctx)
            except _Busy as exc:
                tries += 1
                wait = exc.retry_after if exc.retry_after is not None else min(60.0, 5.0 * 2 ** (tries - 1))
                deadline = ctx.deadline if ctx is not None else float("inf")
                if tries > retries or wait > cap or time.time() + wait > deadline:
                    why = ("still busy after %d tries (chat.retries %d)" % (tries, retries) if tries > retries else
                           "it asks to wait %d s, longer than retry_max_wait_s (%d): a quota for the day?" % (wait, cap) if wait > cap else
                           "its wait of %d s goes past the job's timeout_s" % wait)
                    raise RuntimeError("%s — %s" % (exc, why))
                tell("⏳ the API is busy (%d): asked again in %s s (%d of %d)" % (exc.status, ("%.0f" % wait) if wait >= 1 else "%.1f" % wait, tries, retries))
                why = _pause(wait, ctx)
                if why:
                    raise _Ended(why)

    def _slot(self, ctx: Optional[Context], cap: float, tell: Callable[[str], None]) -> None:
        """Before each request to the model: a slot under the agent's rate limits, taken in the store (``store.request_slot``), so every
        job of the agent, in whatever runner process, is held to them together. A full minute is waited for (a stop or the deadline
        ends the wait: ``_Ended``), and so is a full day whose next slot comes within ``cap`` (retry_max_wait_s) and before the job's
        deadline; a day used up for longer ends the job (``_Ended`` with the setting and when the next request may go: nothing in
        flight, nothing counted). An agent without limits takes its slot too, which only counts its requests: a store that cannot be
        written holds none of its requests up. With a limit, a store that cannot be counted in three tries ends the job."""
        rate = spend.rate_caps(self.cfg)
        per_minute, per_day = rate["requests_per_minute"], rate["requests_per_day"]
        for key, limit in (("requests_per_day", per_day), ("requests_per_minute", per_minute)):
            if limit == 0:                                           # spend.check refuses it before a job: a config changed since
                raise _Ended("agents.%s.%s is 0: no request may go out (null means no limit)" % (self.name, key))
        job = getattr(ctx, "job", None)
        failed = 0
        while True:
            try:
                got = self.project.store.request_slot(self.name, per_minute, per_day, job=job)
            except Exception as exc:                                 # the store locked or unreadable for longer than its timeout
                if not (per_minute or per_day):
                    return
                failed += 1
                if failed >= 3:
                    raise _Ended("the requests of agents.%s could not be counted (%s), so its rate limits cannot hold: no request was sent"
                                 % (self.name, _what(exc)))
                why = _pause(1.0, ctx)
                if why:
                    raise _Ended(why)
                continue
            if got["ok"]:
                return
            wait = float(got["wait_s"])
            deadline = ctx.deadline if ctx is not None else float("inf")
            if got["why"] == "day" and (wait > cap or time.time() + wait > deadline):
                raise _Ended("agents.%s.requests_per_day (%d) is used: the next request in %s" % (self.name, per_day, spend.span(wait)))
            key, limit = ("requests_per_day", per_day) if got["why"] == "day" else ("requests_per_minute", per_minute)
            tell("⏳ %s %d: the next request in %s" % (key, limit, spend.span(wait)))
            why = _pause(wait, ctx)
            if why:
                raise _Ended(why)

    # ----------------------------------------------------------------------------------------------------------- the tools
    def _inside(self, ctx: Context, path: str, write: bool = False) -> Path:
        root = Path(ctx.worktree).resolve()
        p = (root / (path or ".")).resolve()
        if p != root and root not in p.parents:
            raise ValueError("%s is outside the worktree" % path)
        if write and (p.name == ".git" or (root / ".git") in p.parents or p == root / ".git"):
            raise ValueError("the git directory is not written by hand; commit with the run tool")
        return p

    def _run(self, ctx: Context, command: str, timeout_s: Optional[int], s: Dict[str, Any]) -> str:
        for pat in list(s.get("deny") or DENY):
            if re.search(pat, command):
                return "refused: this command is not allowed during a job (%s)" % pat
        wt = str(Path(ctx.worktree).resolve())
        limit = min(int(timeout_s or s.get("command_timeout_s") or 600), int(s.get("command_timeout_s") or 600), max(1, int(ctx.deadline - time.time())))
        box = None
        if s.get("sandbox", "bwrap") == "bwrap":
            ps = sandbox.project_settings(self.project)
            box = {"mounts": sandbox.agent_mounts(self.project, wt, ctx.git_dir), "network": "none", "root": ps["root"], "nested": ps["nested"],
                   "env": sandbox.project_env(self.project)}
        fd, log = tempfile.mkstemp(prefix="dtree-run-", suffix=".log")
        os.close(fd)
        made = sandbox.mount_points(self.project, wt) if box is not None else []
        try:
            r = sandbox.run(command, wt, log, limit, sandbox=box, stopped=ctx.stopped, on_start=ctx.set_pid)
        finally:
            ctx.set_pid(None)
            sandbox.cleanup_mount_points(wt, made)                   # else the next command would lose the ro_binds
            try:
                os.unlink(log)
            except OSError:
                pass
        text = r["tail"] if r["bytes"] <= sandbox.TAIL_BYTES else r["head"] + "\n…[cut]…\n" + r["tail"]
        code = {"timeout": "timeout after %d s" % limit, "stopped": "stopped", "error": "sandbox error: %s" % r["error"]}.get(r["state"], r["exit_code"])
        return "exit %s\n%s" % (code, text)

    def _tool(self, ctx: Context, name: str, a: Dict[str, Any], s: Dict[str, Any]) -> str:
        if name == "list_files":
            root = self._inside(ctx, a.get("path") or ".")
            out, cap = [], int(a.get("max") or 400)
            for dirpath, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".pytest_cache"))
                for f in sorted(files):
                    out.append(os.path.relpath(os.path.join(dirpath, f), ctx.worktree))
                    if len(out) >= cap:
                        return "\n".join(out) + "\n…(more)"
            return "\n".join(out)
        if name == "read_file":
            p = self._inside(ctx, a["path"])
            lines = read_bytes(p, limit=MAX_READ).decode("utf-8", errors="replace").splitlines()
            off, lim = max(1, int(a.get("offset") or 1)), min(2000, int(a.get("limit") or 400))
            return "\n".join("%6d\t%s" % (i, l) for i, l in enumerate(lines[off - 1:off - 1 + lim], off)) + ("" if off - 1 + lim >= len(lines) else "\n…(%d lines in all)" % len(lines))
        if name == "write_file":
            p = self._inside(ctx, a["path"], write=True)
            if len(a.get("content") or "") > MAX_FILE:
                return "refused: larger than %d characters" % MAX_FILE
            p.parent.mkdir(parents=True, exist_ok=True)
            _write(p, a.get("content") or "")
            return "written %s" % a["path"]
        if name == "edit_file":
            p = self._inside(ctx, a["path"], write=True)
            text = read_bytes(p, limit=MAX_READ).decode("utf-8")
            n = text.count(a.get("old") or "\0")
            if n != 1:
                return "not changed: the old text is found %d times (it must be exactly once)" % n
            _write(p, text.replace(a["old"], a.get("new") or "", 1))
            return "edited %s" % a["path"]
        if name == "run":
            return self._run(ctx, str(a.get("command") or ""), a.get("timeout_s"), s)
        return "no such tool: %s" % name

    # ----------------------------------------------------------------------------------------------------------- the loop
    def run(self, prompt: str, ctx: Context) -> Dict[str, Any]:
        s = self._settings()
        messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        raw = getattr(ctx, "raw_log", None)
        limits = getattr(ctx, "limits", None) or spend.limits(self.project, self.cfg)
        cap = limits.get("max_tokens_total")
        left_of = (limits.get("from") or {}).get("max_tokens_total")
        priced = spend.prices(self.cfg) is not None
        n = {"tokens": 0, "in": 0, "out": 0, "step": 0, "idle": 0, "bad_finish": 0, "asked": 0, "partial": False}

        def record(kind: str, **data: Any) -> None:                  # job-N.jsonl: what the model answered and what the tools gave it
            if raw is None:
                return
            try:
                raw.write(json.dumps(dict({"type": kind, "time": round(time.time(), 3)}, **data), ensure_ascii=False, default=str) + "\n")
                raw.flush()
            except (OSError, ValueError, TypeError):
                pass

        def end(out: Dict[str, Any]) -> Dict[str, Any]:              # every way out says what was spent
            out.update(tokens=n["tokens"], tokens_in=n["in"], tokens_out=n["out"])
            if n["partial"]:                                         # a request given up: what it cost is not all known
                out["usage_partial"] = True
            record("end", **{k: out.get(k) for k in ("error", "built", "turns", "tokens", "tokens_in", "tokens_out", "usage_partial")})
            return out

        def over() -> str:
            return "budget: %d tokens of cap %d%s" % (n["tokens"], cap, " (what was left of %s)" % left_of if left_of else "")

        def tell(line: str) -> None:                                 # a log line that cannot be written loses nothing else
            try:
                ctx.say(line)
            except Exception:
                pass

        def lost(exc: BaseException, how: str) -> None:              # a request that went out and brought no answer back (review r4)
            if not getattr(exc, "dtree_went_out", False):            # never sent, or refused (4xx): nothing was spent
                return
            whole = getattr(exc, "dtree_whole", False)
            low = n["asked"] if whole else 0                         # it held the previous prompt whole, unless cut
            n["partial"] = True
            n["in"], n["tokens"] = n["in"] + low, n["tokens"] + low
            tell("■ the request %s is counted as at least %s prompt tokens (%s); what the model spent on it is not known" % (
                how, "{:,}".format(low), "the previous request's prompt, which it carried whole" if low else
                "no earlier answer gave a prompt size" if whole else "its context was cut (context_chars)"))

        ctx.say("▶ %s · %s" % (self.cfg.get("base_url"), self.cfg.get("model")))
        ctx.say(self._sandbox_line(s))
        record("start", agent=self.name, base_url=self.cfg.get("base_url"), model=self.cfg.get("model"), sandbox=s.get("sandbox", "bwrap"),
               max_tokens_total=cap, request_timeout_s=self._request_timeout(s))
        try:
            for step in range(int(s.get("max_steps") or 80)):
                n["step"] = step
                halt = _halt(ctx)
                if halt:
                    return end({"error": halt, "turns": step})
                if cap is not None and n["tokens"] >= cap:            # nothing left to ask with (a cap of 0: what was left of a month)
                    ctx.say("■ " + over())
                    return end({"error": over(), "turns": step})
                t0 = time.time()
                try:
                    resp = self._ask_patiently(messages, s, ctx, tell)
                except _Ended as exc:
                    lost(exc, "given up")                            # the server may have spent (and billed) it
                    if str(exc) not in ("stopped", "timeout"):
                        tell("⚠ " + str(exc))
                    return end({"error": str(exc), "turns": step})
                except (RuntimeError, OSError, ValueError, http.client.HTTPException) as exc:
                    lost(exc, "cut short")                           # an answer cut short, a dropped connection, a 5xx
                    tell("⚠ the model could not be asked: %s" % _what(exc))
                    return end({"error": "the model could not be asked: %s" % _what(exc), "turns": step})
                u = resp.get("usage") if isinstance(resp, dict) else None
                u = u if isinstance(u, dict) else {}
                asked, answered = _int(u.get("prompt_tokens")), _int(u.get("completion_tokens"))
                n["in"], n["out"] = n["in"] + asked, n["out"] + answered
                n["asked"] = asked                                   # what the next request carries again (its answer may not:
                n["tokens"] += _int(u.get("total_tokens")) or asked + answered   # reasoning tokens are never sent back)
                if not any(isinstance(u.get(k), (int, float)) and not isinstance(u.get(k), bool)
                           for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
                    if not n["partial"]:                             # unknown, not free (review r4)
                        tell("■ the API gave no usage for an answer: the job's tokens and dollars are not all known")
                    n["partial"] = True
                if priced:                                           # the cost so far, on the job: a later job's budget sees it
                    so_far = spend.estimate(self.cfg, {"tokens": n["tokens"], "tokens_in": n["in"], "tokens_out": n["out"]})
                    try:
                        if so_far is not None:
                            ctx.set_cost(so_far)
                    except Exception:                                # only a help to the budget: never the end of the job
                        pass
                choices = resp.get("choices") if isinstance(resp, dict) else None
                if not isinstance(resp, dict) or not isinstance(choices, (list, type(None))) or \
                        (choices and not isinstance(choices[0], dict)):
                    record("answer", step=step + 1, seconds=round(time.time() - t0, 1), usage=u, body=short(resp, 2000))
                    return end({"error": "the API answered something that is not a chat completion: %s" % short(resp, 200), "turns": step + 1})
                if not choices and resp.get("error"):
                    record("answer", step=step + 1, seconds=round(time.time() - t0, 1), usage=u, body=short(resp, 2000))
                    return end({"error": "the API answered an error: %s" % short(resp["error"], 300), "turns": step + 1})
                msg = (choices or [{}])[0].get("message")
                msg = msg if isinstance(msg, dict) else {}
                text = _text(msg.get("content"))
                calls = [c for c in (msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []) if isinstance(c, dict)]
                for c in calls:                                      # the OpenAI form when it goes back: arguments as a JSON string
                    f = c.get("function")
                    if isinstance(f, dict) and isinstance(f.get("arguments"), dict):
                        c["function"] = dict(f, arguments=json.dumps(f["arguments"], ensure_ascii=False))
                record("answer", step=step + 1, seconds=round(time.time() - t0, 1), usage=u, message={"content": text, "tool_calls": calls})
                if text.strip():
                    ctx.say("💬 " + short(text, 2000))
                if cap is not None and n["tokens"] >= cap and not any((x.get("function") or {}).get("name") == "finish" for x in calls):
                    ctx.say("■ " + over())                         # spent: no more asking (a finish in hand is still taken)
                    return end({"error": over(), "turns": step + 1})
                messages.append({"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})})
                if not calls:
                    n["idle"] += 1
                    if n["idle"] >= 3:
                        return end({"error": "the model did not use the tools (does it support tool calls?)", "turns": step + 1})
                    messages.append({"role": "user", "content": "Use the tools to do the work, and end with the finish tool."})
                    continue
                n["idle"] = 0
                for c in calls:
                    halt = _halt(ctx)                                # an answer that came after a stop is not acted on
                    if halt:
                        return end({"error": halt, "turns": step + 1})
                    f = c.get("function") if isinstance(c.get("function"), dict) else {}
                    name = str(f.get("name") or "")
                    args = _args(f.get("arguments"))
                    if args is None:
                        result = "error: the arguments are not a JSON object"
                    elif name == "finish":
                        built = _built(args.get("built"))
                        if built is None:
                            n["bad_finish"] += 1
                            said = short(json.dumps(args.get("built"), ensure_ascii=False, default=str), 60)
                            if n["bad_finish"] < BAD_FINISH:
                                result = "error: built must be true or false (a JSON boolean), not %s: call finish again" % said
                            else:
                                built = False
                                ctx.say("⚠ finish gave built %s, %d times not true or false: taken as not built" % (said, BAD_FINISH))
                        if built is not None:
                            ctx.say("■ finish · %d steps · %d tokens" % (step + 1, n["tokens"]))
                            return end({"built": built, "tests": str(args.get("tests") or ""), "impact": str(args.get("impact") or ""),
                                        "note": str(args.get("note") or ""), "turns": step + 1})
                    else:
                        ctx.say("🔧 %s %s" % (name, short(args.get("command") or args.get("path") or args)))
                        try:
                            result = self._tool(ctx, name, args, s)
                        except (OSError, ValueError, KeyError, UnicodeError) as exc:
                            result = "error: %s" % exc
                        except Exception as exc:                     # a wrong type from the model (a number for a path): tell it
                            result = "error: %s: %s" % (type(exc).__name__, exc)
                        ctx.say("  ↳ " + _head(name, result))
                    result = str(result)[:int(s.get("result_chars") or MAX_RESULT)]
                    record("tool_result", step=step + 1, id=c.get("id"), name=name, content=result)
                    messages.append({"role": "tool", "tool_call_id": c.get("id") or name, "content": result})
            return end({"error": "max_steps (%s) reached without finish" % s.get("max_steps"), "turns": s.get("max_steps")})
        except Exception as exc:                                     # whatever ends the loop, the tokens it spent are kept
            what = "%s: %s" % (type(exc).__name__, exc)
            try:                                                     # even when the log cannot be written (a full disk)
                ctx.say("⚠ the chat loop failed: " + what)
            except Exception:
                pass
            record("error", error=what, traceback=traceback.format_exc()[-4000:])
            return end({"error": "the chat loop failed: " + what, "turns": n["step"]})
