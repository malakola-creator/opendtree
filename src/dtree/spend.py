"""ข (cost) — what agents cost and how much they may: caps per job, per month and in all, and the spend per agent.

- per job: ``max_budget_usd`` (Claude Code's own ``--max-budget-usd``) and ``max_tokens_total`` (the chat loop and Codex stop at it),
  from the agent's settings, else ``budget.job_usd`` / ``budget.job_tokens``; null (or nothing) means no cap;
- per month, every agent together: ``budget.month_usd`` — a job is not started when this calendar month's spend (local time; reported
  + estimated) has reached it, and the question gets a ``budget_refused`` event. An agent whose two prices are 0 (a local model) is
  never refused by it;
- per month, one agent: ``agents.<name>.month_usd`` (its dollars) and ``agents.<name>.month_tokens`` (its tokens, in + out as it
  reports them: this one needs no price, for an API that reports tokens only, such as Muse's). An agent that reports tokens only (chat,
  Codex) has dollars only through its prices, so a ``month_usd`` there without prices is refused rather than never reached;
- in all, one agent: ``agents.<name>.total_usd`` / ``agents.<name>.total_tokens`` count every job of the agent in this project's
  store, whatever its month: a credit. The user's need of 2026-11-02 was Muse kept within its $150 hackathon credit, a total; a month
  cap starts again on the 1st, so a credit spent across a month's end would be allowed twice;
- every one of these caps is checked before a job (``check``), and what is left of each when the job starts is the job's cap too
  (``limits``): Claude Code gets the dollars left as ``--max-budget-usd``; the chat loop and Codex get tokens, the dollars left turned
  into tokens at the higher of the agent's two prices (an agent without prices cannot be held by dollars). Jobs that start together
  each get the same remainder, so a race can still end above a cap by what its other jobs spend;
- cost: what the agent reported (Claude Code's ``total_cost_usd``), else estimated from its tokens and the agent's
  ``price_in_per_mtok`` / ``price_out_per_mtok`` (dollars per million tokens; ``cost_estimated`` in the job's result). Tokens known
  only as a total — a count without the in/out split, or what a total counts beyond in + out (an API's reasoning tokens) — are
  priced at the higher of the two prices. Without prices a job has tokens and no dollars;
- a job's dollars are unknown when nothing reported or estimated them although its agent ran (no prices; or the agent reported no
  usage, as a Codex job stopped before its turn ended): ``report`` counts those jobs (``usd_unknown_jobs``) and ``dtree spend`` shows
  "+ ?" beside the dollars known, "-" when none are;
- ``report(project, month)``: per agent the jobs, dollars (and how much of it is estimated), tokens, time, how the jobs ended, its
  own caps with what remains of them, then the totals, ``budget.month_usd`` and what remains (``GET /api/spend``, ``dtree spend``);
- rate limits, one agent (the chat loop: dtree sends a chat model's requests itself): ``agents.<name>.requests_per_minute`` and
  ``requests_per_day`` hold the requests to its model's API in any 60 seconds and in any 24 hours (rolling windows), shared by every
  job of the agent: the store counts them (``store.request_slot``), so runners in several processes are held together. A full minute
  makes the chat loop wait; a day used up refuses a job before it starts (``RateRefused``, code "rate", and a ``rate_refused`` event)
  and ends a running one (agents/chat.py). Claude Code and Codex make their own requests, which dtree neither sees nor paces: the
  settings are refused for them. (Gemini's free tier, 2026-11-02: 5 requests a minute and 20 a day per model.)

Interface used by the rest of dtree (kept stable):
- ``register_routes(route, project)`` · ``register_cli(sub)`` · ``run_cli(args, project) -> bool``
- ``check(project, agent, by, did)`` — raise ``BudgetRefused`` (a DecisionError) when a budget does not allow another job for ``agent``
  (event on ``did``), ``RateRefused`` when its ``requests_per_day`` is used up; a setting that is not a number is a DecisionError too
- ``settings_problem(project, agent_cfg) -> str | None`` — what in the settings refuses every job of the agent (``jobs.agent_list``)
- ``limits(project, agent_cfg) -> dict`` — the job's caps, given to the agent through its Context (``ctx.limits``)
- ``job_caps(project, agent_cfg) -> dict`` — the job's own caps, without what is left of the month (each agent's ``describe()``)
- ``after_job(project, agent_cfg, out) -> dict`` — the agent's output with ``cost_usd`` estimated when only tokens are known
- ``month_caps(agent_cfg)`` / ``total_caps(agent_cfg) -> dict`` — the agent's own caps (each agent's ``describe()``)
- ``rate_caps(agent_cfg)`` — {requests_per_minute, requests_per_day}; ``requests_now(project, name, agent_cfg)`` — {requests_minute,
  requests_day, requests_wait_s}; ``rate_used(project, name, agent_cfg)`` — the day used up, or None (``jobs.agent_list``)
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .store import DecisionError

_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
#: the caps and prices of an agent, and what each must be
_CAPS = ("max_budget_usd", "max_tokens_total")
_PRICES = ("price_in_per_mtok", "price_out_per_mtok")
_MONTH_CAPS = ("month_usd", "month_tokens")
_TOTAL_CAPS = ("total_usd", "total_tokens")
_OWN_CAPS = _MONTH_CAPS + _TOTAL_CAPS
_STATUSES = ("measured", "failed", "stopped", "running")
#: the agents whose dollars are reported by the agent itself (Claude Code's total_cost_usd); the others' come from their prices
_REPORTS_DOLLARS = ("claude",)
#: an agent's rate limits: its chat loop's requests to the model's API in any 60 s and in any 24 hours, shared by every job of it
RATE = ("requests_per_minute", "requests_per_day")
#: the kinds of agent that make their own requests (dtree neither sees nor paces them): a rate limit on one is refused
_OWN_REQUESTS = {"claude": "Claude Code", "codex": "Codex"}


class BudgetRefused(DecisionError):
    """A job not started because a budget is used up (``check``). ``setting`` names the cap; ``code`` lets an API answer say it is
    the budget, not any message that happens to contain the word."""
    code = "budget"

    def __init__(self, message: str, setting: Optional[str] = None) -> None:
        super().__init__(message)
        self.setting = setting


class RateRefused(DecisionError):
    """A job not started because its agent's ``requests_per_day`` is used up (``check``): the requests of the last 24 hours, every job
    of the agent together. ``code`` "rate" lets an API answer say it is the rate limit (as "budget" does for a budget); ``setting``
    names it, ``error_th`` says it in Thai, ``wait_s`` is how long until the next request may go."""
    code = "rate"

    def __init__(self, message: str, setting: Optional[str] = None, error_th: Optional[str] = None, wait_s: Optional[float] = None) -> None:
        super().__init__(message)
        self.setting, self.error_th, self.wait_s = setting, error_th, wait_s


def _num(v: Any) -> Optional[float]:
    """A setting as a number, or None when it is not one (null, text, true/false, negative, NaN)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x >= 0 else None


def _budget(project: Any) -> Dict[str, Any]:
    return project.config.get("budget") or {}


def _usd(x: Any) -> str:
    """Dollars as the CLI and the messages show them: 4 decimals below a cent ($0.0058), else 2; below $0.0001 not as $0.0000, which
    reads as free, but as <$0.0001."""
    x = float(x or 0)
    if 0 < abs(x) < 0.00005:
        return "%s<$0.0001" % ("-" if x < 0 else "")
    return "$%.4f" % x if 0 < abs(x) < 0.01 else "$%.2f" % x


def job_caps(project: Any, agent_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """{max_budget_usd, max_tokens_total}: the job's own caps — the agent's, else the project's ``budget.job_usd`` /
    ``budget.job_tokens``; None = no cap. A value that is not a positive number counts as none (``check`` refuses it before a job)."""
    b = _budget(project)
    usd = agent_cfg.get("max_budget_usd")
    usd = _num(b.get("job_usd") if usd is None else usd)
    tok = agent_cfg.get("max_tokens_total")
    tok = _num(b.get("job_tokens") if tok is None else tok)
    return {"max_budget_usd": usd if usd else None, "max_tokens_total": int(tok) if tok else None}


def limits(project: Any, agent_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """{max_budget_usd, max_tokens_total}: the job's own caps (``job_caps``), each lowered to what is left now of ``budget.month_usd``
    and of the agent's month and total caps. Claude Code takes dollars (its token caps are checked before a job only: it has no token
    cap); the chat loop and Codex take tokens, dollars turned into tokens at the higher of the agent's prices. When a cap comes from
    what is left of one of those, ``from`` names it ({key: setting}); 0 means nothing is left (the agent then does not start), None no
    cap. Never raises (the runner calls it): a store it cannot read leaves the job's own caps."""
    caps = job_caps(project, agent_cfg)
    try:
        left = _left(project, agent_cfg)
    except Exception:                                                # the store unreadable here: check() saw it before the job
        return caps
    dollars = agent_cfg.get("kind") in _REPORTS_DOLLARS
    p = prices(agent_cfg)
    top = max(p["in"], p["out"]) if p else 0.0
    why: Dict[str, str] = {}
    for unit, setting, amount in left:
        amount = max(0.0, amount)
        if dollars:
            if unit != "usd":
                continue
            key, value = "max_budget_usd", round(amount, 6)
        elif unit == "usd":
            if not top:                                              # no prices (its dollars are unknown), or it costs nothing
                continue
            key, value = "max_tokens_total", int(math.floor(amount * 1e6 / top + 1e-6))
        else:
            key, value = "max_tokens_total", int(amount)
        if caps[key] is None or value < caps[key]:
            caps[key], why[key] = value, setting
    if why:
        caps["from"] = why
    return caps


def prices(agent_cfg: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """{in, out} dollars per million tokens, or None when the agent has no price; one price alone is used for both."""
    pin, pout = _num(agent_cfg.get("price_in_per_mtok")), _num(agent_cfg.get("price_out_per_mtok"))
    if pin is None and pout is None:
        return None
    return {"in": pin if pin is not None else pout, "out": pout if pout is not None else pin}


def free(agent_cfg: Dict[str, Any]) -> bool:
    """Both prices 0: the agent costs nothing (a local model), so the month's budget never stops it."""
    return _num(agent_cfg.get("price_in_per_mtok")) == 0 and _num(agent_cfg.get("price_out_per_mtok")) == 0


def month_caps(agent_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """{month_usd, month_tokens}: the agent's own caps for a calendar month; None = none (0 = no job this month)."""
    usd, tok = _num(agent_cfg.get("month_usd")), _num(agent_cfg.get("month_tokens"))
    return {"month_usd": usd, "month_tokens": None if tok is None else int(tok)}


def total_caps(agent_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """{total_usd, total_tokens}: the agent's own caps over every job of it in this project's store, whatever the month (a credit);
    None = none (0 = no job at all)."""
    usd, tok = _num(agent_cfg.get("total_usd")), _num(agent_cfg.get("total_tokens"))
    return {"total_usd": usd, "total_tokens": None if tok is None else int(tok)}


def _whole(v: Any) -> Optional[int]:
    """A setting as a whole number of 0 or more (an integral float too: 5.0), or None when it is not one (null, text, true/false, 2.5,
    a negative number)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float) and v == v and v >= 0 and v.is_integer():
        return int(v)
    return None


def rate_caps(agent_cfg: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """{requests_per_minute, requests_per_day}: a chat agent's rate limits (its chat loop's requests in any 60 s, in any 24 hours,
    every job of it together); None = no limit, and none for an agent that makes its own requests. A value that is not a whole number
    counts as none here: ``check`` refuses it before a job, and so a limit set on Claude Code or Codex."""
    if agent_cfg.get("kind") != "chat":
        return {k: None for k in RATE}
    return {k: _whole(agent_cfg.get(k)) for k in RATE}


def span(seconds: Any, th: bool = False) -> str:
    """A wait as the messages say it: "0.4 s", "9 s", "2 min 3 s", "3 h 12 min" (rounded up; ``th``: "9 วินาที", "3 ชม. 12 นาที")."""
    s = max(0.0, float(seconds or 0))
    sec, mins, hours = ("วินาที", "นาที", "ชม.") if th else ("s", "min", "h")
    if s == 0:
        return "0 " + sec
    tenths = int(math.ceil(s * 10))
    if tenths < 10:
        return "0.%d %s" % (tenths, sec)
    n = int(math.ceil(s))
    if n < 60:
        return "%d %s" % (n, sec)
    if n < 3600:
        m, r = divmod(n, 60)
        return "%d %s" % (m, mins) + (" %d %s" % (r, sec) if r else "")
    h, m = divmod(int(math.ceil(s / 60)), 60)
    return "%d %s" % (h, hours) + (" %d %s" % (m, mins) if m else "")


def _made_store(project: Any) -> Any:
    """The project's store, or None when it is not made yet (then no request was ever counted): the agents list does not make one."""
    if getattr(project, "_store", None) is None:
        path = os.environ.get("DTREE_DB") or project.path(project.config["db"])
        if not os.path.exists(str(path)):
            return None
    return project.store


def requests_now(project: Any, name: str, agent_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """{requests_minute, requests_day, requests_wait_s}: the requests the agent's chat loop sent to its model's API in the last minute and
    in the last 24 hours (rolling; every job of it in this project's store) and, when one of its limits is reached, how long until the
    next may go (None: one may go now). All None when they are not known: an agent that makes its own requests (Claude Code, Codex:
    dtree does not see them), or a store that cannot be read."""
    out: Dict[str, Any] = {"requests_minute": None, "requests_day": None, "requests_wait_s": None}
    if agent_cfg.get("kind") != "chat":
        return out
    rate = rate_caps(agent_cfg)
    try:
        store = _made_store(project)
        if store is None:
            return dict(out, requests_minute=0, requests_day=0)
        s = store.request_slot(name, rate["requests_per_minute"] or None, rate["requests_per_day"] or None, take=False)
    except Exception:                                                # only shown: a store that cannot be read shows nothing
        return out
    return {"requests_minute": s["minute"], "requests_day": s["day"], "requests_wait_s": None if s["ok"] else s["wait_s"]}


def rate_used(project: Any, name: str, agent_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Whether the agent's ``requests_per_day`` is used up now (the requests of the last 24 hours, every job of it together): None, or
    {setting, limit, requests, wait_s, reason, reason_th}. ``requests_per_minute`` never refuses a job: a minute's slot comes within the
    minute, and the chat loop waits for it."""
    per_day = rate_caps(agent_cfg)["requests_per_day"]
    store = _made_store(project) if agent_cfg.get("kind") == "chat" and per_day else None
    if store is None:
        return None
    s = store.request_slot(name, None, per_day, take=False)
    if s["ok"]:
        return None
    setting = "agents.%s.requests_per_day" % name
    return {"setting": setting, "limit": per_day, "requests": s["day"], "wait_s": s["wait_s"],
            "reason": "%s (%d) is used: the next request in %s" % (setting, per_day, span(s["wait_s"])),
            "reason_th": "%s ใช้คำขอครบ %d ครั้งใน 24 ชั่วโมงแล้ว (%s): ส่งคำขอถัดไปได้ในอีก %s" % (name, per_day, setting, span(s["wait_s"], th=True))}


def estimate(agent_cfg: Dict[str, Any], out: Dict[str, Any]) -> Optional[float]:
    """Dollars from the tokens an agent reported and its prices. None without prices, or when it reported no tokens at all (unknown,
    not free); 0 for an agent whose prices are both 0, whatever it reported. Tokens a total counts beyond in + out (a server that gives
    the total only, an API's reasoning tokens) are priced at the higher of the two prices."""
    p = prices(agent_cfg)
    if p is None:
        return None
    top = max(p["in"], p["out"])
    if not top:
        return 0.0
    tin, tout, total = (out.get(k) for k in ("tokens_in", "tokens_out", "tokens"))
    if tin is None and tout is None and total is None:
        return None
    tin, tout, total = int(tin or 0), int(tout or 0), int(total or 0)
    return round((tin * p["in"] + tout * p["out"] + max(0, total - tin - tout) * top) / 1e6, 6)


def after_job(project: Any, agent_cfg: Dict[str, Any], out: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(out or {})
    if out.get("cost_usd") is None:
        est = estimate(agent_cfg, out)
        if est is not None:
            out["cost_usd"], out["cost_estimated"] = est, True
    return out


def _check_settings(project: Any, name: str, cfg: Dict[str, Any]) -> None:
    """A cap or a price that is not a number is refused before a job, not ignored silently while it runs; so is a dollar cap on an
    agent whose dollars cannot be known (tokens only, no prices)."""
    for key in _CAPS + _PRICES + _OWN_CAPS:
        v = cfg.get(key)
        if v is not None and (_num(v) is None or (key in _CAPS and not _num(v))):
            raise DecisionError("agents.%s.%s must be a number%s, not %r" % (name, key, " above 0" if key in _CAPS else " (0 or more)", v))
    for key, period in (("month_usd", "month"), ("total_usd", "total")):
        if cfg.get(key) is not None and cfg.get("kind") not in _REPORTS_DOLLARS and prices(cfg) is None:
            raise DecisionError("agents.%s.%s cannot be counted: %s reports tokens only, so its dollars come from agents.%s.price_in_per_mtok "
                                "/ price_out_per_mtok; set them, or cap its %s in tokens with agents.%s.%s_tokens" % (
                                    name, key, name, name, period, name, period))
    for key in ("month_usd", "job_usd", "job_tokens"):
        v = _budget(project).get(key)
        if v is not None and (_num(v) is None or (key != "month_usd" and not _num(v))):
            raise DecisionError("budget.%s must be a number%s, not %r" % (key, " above 0" if key != "month_usd" else " (0 or more)", v))
    for key in RATE:                                                 # the chat loop's own settings
        v = cfg.get(key)
        if v is None:
            continue
        if cfg.get("kind") != "chat":
            raise DecisionError("agents.%s.%s is a setting of dtree's chat loop, which sends a chat model's requests itself; %s makes its own "
                                "requests, which dtree neither sees nor paces: remove it (the agent's own service limits it)"
                                % (name, key, _OWN_REQUESTS.get(cfg.get("kind"), name)))
        n = _whole(v)
        if n == 0:
            raise DecisionError("agents.%s.%s is 0: no request may go out, so no job of %s starts (null, or nothing, means no limit)"
                                % (name, key, name))
        if n is None:
            raise DecisionError("agents.%s.%s must be a whole number above 0 (null: no limit), not %r" % (name, key, v))


def settings_problem(project: Any, agent_cfg: Dict[str, Any]) -> Optional[str]:
    """What in the settings refuses every job of this agent, or None: a cap or price that is not a number, a dollar cap that cannot be
    counted (tokens only, no prices), a cap of 0, a rate limit that is not a whole number above 0 or that is set on an agent making its
    own requests (Claude Code, Codex). ``jobs.agent_list`` shows such an agent as not available with this reason, instead of offering it
    and refusing each job sent to it. (A cap used up this month, a day's requests used up, are not settings: ``check`` says so when a
    job is sent.)"""
    name = str(agent_cfg.get("name") or agent_cfg.get("kind") or "?")
    try:
        _check_settings(project, name, agent_cfg)
    except DecisionError as exc:
        return str(exc)
    for key in _OWN_CAPS:
        if _num(agent_cfg.get(key)) == 0:
            return "agents.%s.%s is 0: no job of %s starts" % (name, key, name)
    if _num(_budget(project).get("month_usd")) == 0 and not free(agent_cfg):
        return "budget.month_usd is 0: no job of an agent that costs something starts (%s's prices are not both 0)" % name
    return None


def _month(month: Optional[str] = None) -> Tuple[str, float, float]:
    """``YYYY-MM`` (default: now) → (the month, its first second, the next month's first second), local time."""
    if month is None:
        month = time.strftime("%Y-%m")
    m = _MONTH.match(str(month))
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise DecisionError("a month is YYYY-MM, not %r" % (month,))
    y, mo = int(m.group(1)), int(m.group(2))
    start = time.mktime((y, mo, 1, 0, 0, 0, 0, 0, -1))
    end = time.mktime((y + (mo == 12), mo % 12 + 1, 1, 0, 0, 0, 0, 0, -1))
    return "%04d-%02d" % (y, mo), start, end


def _cost(job: Dict[str, Any]) -> Tuple[Optional[float], bool]:
    """A job's dollars (the column, else its result's) and whether they are an estimate."""
    r = job.get("result") or {}
    cost = job.get("cost_usd") if job.get("cost_usd") is not None else r.get("cost_usd")
    return (None if cost is None else float(cost)), bool(r.get("cost_estimated"))


def _tokens(job: Dict[str, Any]) -> int:
    """A job's tokens: its total, else in + out (a job still running has none yet; unknown counts as none)."""
    r = job.get("result") or {}
    return int(r.get("tokens") or 0) or int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)


def _unknown(job: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """A job that ended with dollars nobody knows: none reported or estimated (no prices; no usage reported) although its agent ran —
    the runner records the worktree just before it starts the agent, and the agent's output has its tokens — or known only in part (a
    chat request given up on a stop or a time limit: ``usage_partial``, review of round 3). A job that never reached its agent cost
    nothing, and so does every job of an agent whose prices are both 0."""
    if job["status"] == "running" or free(cfg):
        return False
    r = job.get("result") or {}
    if r.get("usage_partial"):
        return True
    if _cost(job)[0] is not None:
        return False
    return bool(job.get("worktree") or any(r.get(k) is not None for k in ("tokens", "tokens_in", "tokens_out")))


def _tokens_unknown(job: Dict[str, Any]) -> bool:
    """A job whose tokens nobody knows, or knows only in part, although its agent ran (Codex with no turn completed, a chat request
    given up): month_tokens / total_tokens count it as what is known, so what is left is "at most"."""
    if job["status"] == "running":
        return False
    r = job.get("result") or {}
    return bool(r.get("usage_partial") or (job.get("worktree") and all(r.get(k) is None for k in ("tokens", "tokens_in", "tokens_out"))))


def _jobs_of(project: Any, month: Optional[str], agent: Optional[str] = None) -> List[Dict[str, Any]]:
    _, start, end = _month(month)
    return [j for j in project.store.jobs() if start <= j["created"] < end and (agent is None or j.get("agent") == agent)]


def spent(project: Any, month: Optional[str] = None, agent: Optional[str] = None) -> float:
    """This month's dollars over every job, or over ``agent``'s (a running job counts with what it has reported so far)."""
    return round(sum(_cost(j)[0] or 0.0 for j in _jobs_of(project, month, agent)), 6)


def spent_tokens(project: Any, month: Optional[str] = None, agent: Optional[str] = None) -> int:
    """This month's tokens over every job, or over ``agent``'s."""
    return sum(_tokens(j) for j in _jobs_of(project, month, agent))


def _usage(project: Any, agent: Optional[str]) -> Dict[str, Any]:
    """One pass over the store: this month's dollars over every job, and ``agent``'s dollars and tokens this month and in all."""
    _, start, end = _month()
    u = {"month_usd": 0.0, "agent_month_usd": 0.0, "agent_month_tokens": 0, "agent_total_usd": 0.0, "agent_total_tokens": 0}
    for j in project.store.jobs():
        cost = _cost(j)[0] or 0.0
        now = start <= j["created"] < end
        if now:
            u["month_usd"] += cost
        if agent is not None and j.get("agent") == agent:
            tok = _tokens(j)
            u["agent_total_usd"] += cost
            u["agent_total_tokens"] += tok
            if now:
                u["agent_month_usd"] += cost
                u["agent_month_tokens"] += tok
    for k in ("month_usd", "agent_month_usd", "agent_total_usd"):
        u[k] = round(u[k], 6)
    return u


def _left(project: Any, agent_cfg: Dict[str, Any]) -> List[Tuple[str, str, float]]:
    """What is left now of every spend cap over the agent: [(unit "usd" | "tokens", setting, amount)], below 0 when one went over."""
    name = agent_cfg.get("name")
    out: List[Tuple[str, str, float]] = []
    month = _num(_budget(project).get("month_usd"))
    own = dict(month_caps(agent_cfg), **total_caps(agent_cfg)) if name else {}
    if (month is None or free(agent_cfg)) and not any(v is not None for v in own.values()):
        return out
    u = _usage(project, name)
    if month is not None and not free(agent_cfg):
        out.append(("usd", "budget.month_usd", month - u["month_usd"]))
    for key, unit, used in (("month_usd", "usd", u["agent_month_usd"]), ("month_tokens", "tokens", u["agent_month_tokens"]),
                            ("total_usd", "usd", u["agent_total_usd"]), ("total_tokens", "tokens", u["agent_total_tokens"])):
        if own.get(key) is not None:
            out.append((unit, "agents.%s.%s" % (name, key), own[key] - used))
    return out


def _note(project: Any, did: Any, by: str, action: str, detail: Dict[str, Any]) -> None:
    if did is not None:
        try:
            project.store.note_event(int(did), action, by, detail)
        except KeyError:
            pass


def _refuse(project: Any, did: Any, by: str, detail: Dict[str, Any], message: str) -> None:
    _note(project, did, by, "budget_refused", detail)
    raise BudgetRefused(message, detail.get("setting"))


def check(project: Any, agent: str, by: str = "user", did: Any = None) -> None:
    """``did``: the question the job is for (the ``budget_refused`` or ``rate_refused`` event goes there, with the ``setting`` that
    refused it). The budgets first, then the agent's day of requests (``rate_used``: ``RateRefused``, code "rate")."""
    try:
        cfg = project.agent(agent)
    except KeyError:
        cfg = {}
    _check_settings(project, agent, cfg)
    _check_budget(project, agent, cfg, by, did)
    used = rate_used(project, agent, cfg)
    if used is not None:
        _note(project, did, by, "rate_refused", {"agent": agent, "setting": used["setting"], "requests": used["requests"], "limit": used["limit"],
                                                 "period": "day", "wait_s": used["wait_s"], "note": used["reason"]})
        raise RateRefused("%s; %s was not started" % (used["reason"], agent), used["setting"], used["reason_th"] + " — งานนี้ไม่ได้เริ่ม",
                          used["wait_s"])
    return None


def _check_budget(project: Any, agent: str, cfg: Dict[str, Any], by: str, did: Any) -> None:
    """``check``'s budgets: ``budget.month_usd`` and the agent's own month and total caps (``BudgetRefused``)."""
    month, _, _ = _month()
    cap = _num(_budget(project).get("month_usd"))
    own, tot = month_caps(cfg), total_caps(cfg)
    if (cap is None or free(cfg)) and not any(v is not None for v in list(own.values()) + list(tot.values())):
        return None                                                  # no cap over this agent: nothing to count
    u = _usage(project, agent)
    if cap is not None and not free(cfg):
        used = u["month_usd"]
        if used >= cap:
            _refuse(project, did, by, {"agent": agent, "setting": "budget.month_usd", "spent_usd": round(used, 4), "cap_usd": cap, "period": month},
                    "the budget of %s is used: %s of %s spent (budget.month_usd); %s was not started" % (month, _usd(used), _usd(cap), agent))
    if own["month_usd"] is not None:
        used = u["agent_month_usd"]
        if used >= own["month_usd"]:
            setting = "agents.%s.month_usd" % agent
            _refuse(project, did, by, {"agent": agent, "setting": setting, "spent_usd": round(used, 4), "cap_usd": own["month_usd"], "period": month},
                    "%s's budget of %s is used: %s of %s spent (%s); it was not started" % (agent, month, _usd(used), _usd(own["month_usd"]), setting))
    if own["month_tokens"] is not None:
        used_t = u["agent_month_tokens"]
        if used_t >= own["month_tokens"]:
            setting = "agents.%s.month_tokens" % agent
            _refuse(project, did, by, {"agent": agent, "setting": setting, "spent_tokens": used_t, "cap_tokens": own["month_tokens"], "period": month},
                    "%s's tokens of %s are used: %s of %s tokens (%s); it was not started" % (
                        agent, month, "{:,}".format(used_t), "{:,}".format(own["month_tokens"]), setting))
    if tot["total_usd"] is not None:
        used = u["agent_total_usd"]
        if used >= tot["total_usd"]:
            setting = "agents.%s.total_usd" % agent
            _refuse(project, did, by, {"agent": agent, "setting": setting, "spent_usd": round(used, 4), "cap_usd": tot["total_usd"], "period": "total"},
                    "%s's total is used: %s of %s spent in all (%s); it was not started" % (agent, _usd(used), _usd(tot["total_usd"]), setting))
    if tot["total_tokens"] is not None:
        used_t = u["agent_total_tokens"]
        if used_t >= tot["total_tokens"]:
            setting = "agents.%s.total_tokens" % agent
            _refuse(project, did, by, {"agent": agent, "setting": setting, "spent_tokens": used_t, "cap_tokens": tot["total_tokens"], "period": "total"},
                    "%s's total tokens are used: %s of %s tokens in all (%s); it was not started" % (
                        agent, "{:,}".format(used_t), "{:,}".format(tot["total_tokens"]), setting))
    return None


def _row() -> Dict[str, Any]:
    return {"jobs": 0, "usd": 0.0, "usd_estimated": 0.0, "tokens_in": 0, "tokens_out": 0, "tokens": 0, "seconds": 0.0,
            "measured": 0, "failed": 0, "stopped": 0, "running": 0, "no_price": 0, "usd_known_jobs": 0, "usd_unknown_jobs": 0,
            "tokens_unknown_jobs": 0}


def _add(row: Dict[str, Any], j: Dict[str, Any], cfg: Dict[str, Any], now: float) -> None:
    """Job ``j`` counted into ``row`` (``_row``): the one rule of the month's report and of each question's usage, so they agree."""
    r = j.get("result") or {}
    cost, estimated = _cost(j)
    row["jobs"] += 1
    if j["status"] in _STATUSES:
        row[j["status"]] += 1
    if cost is not None:
        row["usd"] += cost
        row["usd_known_jobs"] += 1
        if estimated:
            row["usd_estimated"] += cost
    elif r.get("tokens") or r.get("tokens_in") or r.get("tokens_out"):
        row["no_price"] += 1
    row["usd_unknown_jobs"] += _unknown(j, cfg)
    row["tokens_unknown_jobs"] += _tokens_unknown(j)
    for k in ("tokens_in", "tokens_out"):
        row[k] += int(r.get(k) or 0)
    row["tokens"] += _tokens(j)
    row["seconds"] += max(0.0, (j.get("ended_at") or now) - j["created"])


def _rounded(row: Dict[str, Any]) -> Dict[str, Any]:
    row["usd"], row["usd_estimated"], row["seconds"] = round(row["usd"], 6), round(row["usd_estimated"], 6), round(row["seconds"], 1)
    return row


def usage(project: Any, did: Optional[int] = None, ended: bool = False) -> Dict[int, Dict[str, Any]]:
    """What the AI jobs of each question used, whatever the month (the user asked 2026-11-02 for tokens per decision in the tree):
    {question id: row} with a row as ``report``'s (jobs, tokens, tokens_in, tokens_out, usd, usd_estimated, usd_known_jobs,
    usd_unknown_jobs, tokens_unknown_jobs, seconds, measured, failed, stopped, running, no_price), counted by the same rule
    (``_add``), and ``agents`` {name: row} and ``trials`` {trial id: row}. Only the jobs dtree ran (send, race, follow-up, update):
    work done outside dtree — a person, or an agent working in its own session — is not in the store. Questions without a job are
    not listed; ``did``: only that question; ``ended``: only the jobs that have ended (the ADR: a record that changes only when a
    job ends, never while one runs)."""
    now = time.time()
    cfgs = {k: v for k, v in (project.config.get("agents") or {}).items() if isinstance(v, dict)}
    out: Dict[int, Dict[str, Any]] = {}
    for j in project.store.jobs(did=did):
        if ended and j["status"] == "running":
            continue
        name = j.get("agent") or "?"
        cfg = cfgs.get(name) or {}
        q = out.setdefault(int(j["decision_id"]), dict(_row(), agents={}, trials={}))
        for row in (q, q["agents"].setdefault(name, _row()), q["trials"].setdefault(int(j["trial_id"]), _row())):
            _add(row, j, cfg, now)
    for q in out.values():
        _rounded(q)
        for row in list(q["agents"].values()) + list(q["trials"].values()):
            _rounded(row)
    return out


#: what the tree's nodes carry of a question's usage (and of its branch): the totals, not the agents' and trials' rows
BRIEF = ("jobs", "running", "tokens", "tokens_in", "tokens_out", "usd", "usd_estimated", "usd_known_jobs", "usd_unknown_jobs",
         "tokens_unknown_jobs", "seconds")


def brief(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: row.get(k, 0) for k in BRIEF}


def tree_usage(project: Any, tree: Dict[str, Any]) -> Dict[str, Any]:
    """``store.tree()`` with each question's usage on its node (``usage``, the short totals; None without a job) and, for a question
    others grew from, ``usage_branch``: the question and every question below it together (None when none of them has a job)."""
    use = usage(project)
    nodes = tree.get("nodes") or []
    ids = {int(n["id"]) for n in nodes}
    parent = {int(n["id"]): int(n["parent"]) for n in nodes if n.get("parent") is not None and int(n["parent"]) in ids}
    total = {i: brief(use[i]) if i in use else {k: 0 for k in BRIEF} for i in ids}
    kids = {i: 0 for i in ids}
    for i in sorted(ids, reverse=True):                              # a child before its parent (a parent is always an earlier
        p = parent.get(i)                                            # question): no recursion, however deep the tree (review)
        if p is not None and p < i:
            kids[p] += 1
            for k in BRIEF:
                total[p][k] += total[i][k]
    for n in nodes:
        i = int(n["id"])
        n["usage"] = brief(use[i]) if i in use else None
        b = _rounded(dict(total[i])) if kids[i] else None
        n["usage_branch"] = b if b and b["jobs"] else None
    return tree


def report(project: Any, month: Optional[str] = None) -> Dict[str, Any]:
    """{month, agents: {name: row}, totals: row, cap, remaining}; a row is {jobs, usd (reported + estimated), usd_estimated (the part
    estimated from tokens), tokens_in, tokens_out, tokens, seconds, measured, failed, stopped, running, no_price (jobs with tokens and
    no price), usd_known_jobs (jobs whose dollars are known), usd_unknown_jobs (jobs that ended with dollars nobody knows: no price, or
    no usage reported, or known only in part), tokens_unknown_jobs (jobs whose tokens are not known, or only in part)}. An agent's row
    also has its own caps and what is left of them: cap_usd / remaining_usd
    (``agents.<name>.month_usd``), cap_tokens / remaining_tokens (``agents.<name>.month_tokens``), and over every job of it whatever the
    month, spent_total_usd / spent_total_tokens / spent_total_unknown_jobs / spent_total_tokens_unknown_jobs with cap_total_usd /
    remaining_total_usd
    (``agents.<name>.total_usd``) and cap_total_tokens / remaining_total_tokens (``agents.<name>.total_tokens``); None without a cap.
    And its rate limits, requests_per_minute / requests_per_day (None: no limit), with the requests now (``requests_now``, whatever
    month the report is for): requests_minute / requests_day sent in the last minute and the last 24 hours, requests_wait_s until the
    next may go when a limit is reached (None for an agent that makes its own requests: dtree does not see them).
    An agent with a cap or a rate limit is listed even before its first job of the month. ``cap`` is ``budget.month_usd`` (None: no
    cap), ``remaining`` what is left of it (below 0 when the month went over)."""
    month, start, end = _month(month)
    now = time.time()
    cfgs = {k: v for k, v in (project.config.get("agents") or {}).items() if isinstance(v, dict)}
    agents: Dict[str, Dict[str, Any]] = {}
    total = _row()
    alltime: Dict[str, List[float]] = {}
    for j in project.store.jobs():
        name = j.get("agent") or "?"
        cost = _cost(j)[0]
        unknown, tokens_unknown = _unknown(j, cfgs.get(name) or {}), _tokens_unknown(j)
        a = alltime.setdefault(name, [0.0, 0, 0, 0])
        a[0] += cost or 0.0
        a[1] += _tokens(j)
        a[2] += unknown
        a[3] += tokens_unknown
        if not start <= j["created"] < end:
            continue
        for row in (agents.setdefault(name, _row()), total):
            _add(row, j, cfgs.get(name) or {}, now)
    for name, cfg in cfgs.items():
        if any(v is not None for v in dict(month_caps(cfg), **total_caps(cfg), **rate_caps(cfg)).values()):
            agents.setdefault(name, _row())
    for name, row in agents.items():
        cfg = cfgs.get(name) or {}
        row.update(rate_caps(cfg))
        row.update(requests_now(project, name, cfg))
        caps, tot = month_caps(cfg), total_caps(cfg)
        usd_all, tok_all, unknown_all, tok_unknown_all = alltime.get(name, [0.0, 0, 0, 0])
        row["cap_usd"], row["cap_tokens"] = caps["month_usd"], caps["month_tokens"]
        row["remaining_usd"] = None if caps["month_usd"] is None else round(caps["month_usd"] - row["usd"], 6)
        row["remaining_tokens"] = None if caps["month_tokens"] is None else caps["month_tokens"] - row["tokens"]
        row["spent_total_usd"], row["spent_total_tokens"], row["spent_total_unknown_jobs"] = round(usd_all, 6), int(tok_all), int(unknown_all)
        row["spent_total_tokens_unknown_jobs"] = int(tok_unknown_all)
        row["cap_total_usd"], row["cap_total_tokens"] = tot["total_usd"], tot["total_tokens"]
        row["remaining_total_usd"] = None if tot["total_usd"] is None else round(tot["total_usd"] - usd_all, 6)
        row["remaining_total_tokens"] = None if tot["total_tokens"] is None else tot["total_tokens"] - int(tok_all)
    for row in list(agents.values()) + [total]:
        _rounded(row)
    cap = _num(_budget(project).get("month_usd"))
    return {"month": month, "agents": dict(sorted(agents.items())), "totals": total, "cap": cap,
            "remaining": None if cap is None else round(cap - total["usd"], 6)}


# ------------------------------------------------------------------------------------------------------------------- the API, the CLI
def register_routes(route: Callable, project: Any) -> None:
    @route("GET", r"/api/spend")
    def spend_(q, b):
        return report(project, q.get("month") or None)


def register_cli(sub: Any) -> None:
    p = sub.add_parser("spend", help="what the agents spent in a month, against budget.month_usd and each agent's caps")
    p.add_argument("--month", default=None, help="YYYY-MM (default: this month)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("usage", help="what each question's AI jobs used (tokens, dollars, time), and with ID its agents and trials")
    p.add_argument("id", type=int, nargs="?", default=None)
    p.add_argument("--json", action="store_true")


def _time(s: float) -> str:
    s = int(round(s))
    return "%dh%02dm" % (s // 3600, s % 3600 // 60) if s >= 3600 else "%dm%02ds" % (s // 60, s % 60)


def _money(row: Dict[str, Any]) -> str:
    """A row's dollars: "-" when none of its jobs' dollars is known (no price, or still running), else the dollars known, "~" in front
    when all of it is estimated, "+ ?" after it when some jobs' dollars are unknown."""
    if not row.get("usd_known_jobs") and (row.get("usd_unknown_jobs") or row.get("running")):
        return "-"
    s = ("~" if row["usd_estimated"] and row["usd_estimated"] >= row["usd"] else "") + _usd(row["usd"])
    return s + " + ?" if row.get("usd_unknown_jobs") else s


def _left_usd(left: float, unknown: Any = 0) -> str:
    """What is left of a dollar cap: "at most" when some jobs' dollars are not known (they count as what is known)."""
    return ("at most " if unknown else "") + "%s left" % _usd(left) if left >= 0 else "%s over" % _usd(-left)


def _left_tokens(left: int, unknown: Any = 0) -> str:
    return ("at most " if unknown else "") + "{:,} left".format(left) if left >= 0 else "{:,} over".format(-left)


def _caps_line(name: str, row: Dict[str, Any]) -> Optional[str]:
    """``muse: $0.25 of $150.00 (agents.muse.month_usd), $149.75 left · 400 of 1,000 tokens (agents.muse.month_tokens), 600 left``"""
    parts = []
    if row.get("cap_usd") is not None:
        parts.append("%s%s of %s (agents.%s.month_usd), %s" % (_usd(row["usd"]), " + ?" if row.get("usd_unknown_jobs") else "",
                                                                _usd(row["cap_usd"]), name, _left_usd(row["remaining_usd"], row.get("usd_unknown_jobs"))))
    if row.get("cap_tokens") is not None:
        unknown = row.get("tokens_unknown_jobs")
        parts.append("%s%s of %s tokens (agents.%s.month_tokens), %s" % ("{:,}".format(row["tokens"]), " + ?" if unknown else "",
                                                                        "{:,}".format(row["cap_tokens"]), name,
                                                                        _left_tokens(row["remaining_tokens"], unknown)))
    return "%s: %s" % (name, " · ".join(parts)) if parts else None


def _total_line(name: str, row: Dict[str, Any]) -> Optional[str]:
    """``muse: $112.50 of $150.00 in all (agents.muse.total_usd), $37.50 left · …``"""
    parts = []
    if row.get("cap_total_usd") is not None:
        parts.append("%s%s of %s in all (agents.%s.total_usd), %s" % (
            _usd(row["spent_total_usd"]), " + ?" if row.get("spent_total_unknown_jobs") else "", _usd(row["cap_total_usd"]), name,
            _left_usd(row["remaining_total_usd"], row.get("spent_total_unknown_jobs"))))
    if row.get("cap_total_tokens") is not None:
        unknown = row.get("spent_total_tokens_unknown_jobs")
        parts.append("%s%s of %s tokens in all (agents.%s.total_tokens), %s" % (
            "{:,}".format(row["spent_total_tokens"]), " + ?" if unknown else "", "{:,}".format(row["cap_total_tokens"]), name,
            _left_tokens(row["remaining_total_tokens"], unknown)))
    return "%s: %s" % (name, " · ".join(parts)) if parts else None


def _requests_line(name: str, row: Dict[str, Any]) -> Optional[str]:
    """``gemini: 2 of 5 in the last minute (agents.gemini.requests_per_minute) · 12 of 20 in the last 24 h
    (agents.gemini.requests_per_day), 8 left`` — a day used up says when the next request may go."""
    parts = []

    def shown(n: Any) -> str:                                        # "?": a store that could not be read
        return "?" if n is None else "{:,}".format(n)
    if row.get("requests_per_minute") is not None:
        parts.append("%s of %s in the last minute (agents.%s.requests_per_minute)" % (shown(row.get("requests_minute")),
                                                                                     shown(row["requests_per_minute"]), name))
    if row.get("requests_per_day") is not None:
        day, cap = row.get("requests_day"), row["requests_per_day"]
        left = "%s left" % shown(None if day is None else cap - day)
        if not cap:
            left = "no request may go out"
        elif day is not None and day >= cap:
            left = "used up: the next request in %s" % span(row.get("requests_wait_s") or 0)
        parts.append("%s of %s in the last 24 h (agents.%s.requests_per_day), %s" % (shown(day), shown(cap), name, left))
    return "%s: %s" % (name, " · ".join(parts)) if parts else None


def _use_line(u: Dict[str, Any]) -> str:
    """``3 jobs · 12,345 tok (in 12,000 / out 345) · ~$0.05 · 2m10s``, with "+ ?" where some jobs' tokens or dollars are unknown."""
    n = int(u["jobs"])
    tok = "{:,} tok".format(u["tokens"]) + (" + ?" if u.get("tokens_unknown_jobs") else "")
    if u.get("tokens_in") or u.get("tokens_out"):
        tok += " (in {:,} / out {:,})".format(u["tokens_in"], u["tokens_out"])
    run = " · %d running" % u["running"] if u.get("running") else ""
    return "%d job%s · %s · %s · %s%s" % (n, "" if n == 1 else "s", tok, _money(u), _time(u["seconds"]), run)


def _usage_cli(args: Any, project: Any) -> bool:
    tree = tree_usage(project, project.store.tree())
    nodes = {int(n["id"]): n for n in tree["nodes"]}
    if args.id is None:
        rows = [n for n in tree["nodes"] if n.get("usage")]
        if args.json:
            print(json.dumps({n["id"]: {"usage": n["usage"], "usage_branch": n.get("usage_branch")} for n in rows}, ensure_ascii=False, indent=1))
            return True
        total = {k: 0 for k in BRIEF}
        for n in rows:
            print("#%-4d %s · %s" % (n["id"], n.get("title_en") or n.get("title_th") or "", _use_line(n["usage"])))
            for k in BRIEF:
                total[k] += n["usage"][k]
        if not rows:
            print("no question has AI jobs yet: only the jobs dtree runs are counted (dtree ai, race, followup, update)")
            return True
        _rounded(total)
        print("total · %d question%s · %s" % (len(rows), "" if len(rows) == 1 else "s", _use_line(total)))
        return True
    if args.id not in nodes:
        raise DecisionError("no question %d" % args.id)
    n, full = nodes[args.id], usage(project, args.id).get(args.id)
    if args.json:
        print(json.dumps({"usage": full, "usage_branch": n.get("usage_branch")}, ensure_ascii=False, indent=1))
        return True
    print("#%d %s" % (args.id, n.get("title_en") or n.get("title_th") or ""))
    if not full:
        print("  no AI job of this question: only the jobs dtree runs are counted (dtree ai, race, followup, update)")
    else:
        print("  " + _use_line(full))
        trials = {int(t["id"]): t for t in (n.get("trials") or [])}
        for name, row in sorted(full["agents"].items()):
            print("  agent %-12s %s" % (name, _use_line(row)))
        for tid, row in sorted(full["trials"].items()):
            t = trials.get(int(tid)) or {}
            print("  trial %-4s %-24s %s" % (tid, (t.get("option") or "?") + ("--" + t["variant"] if t.get("variant") else ""), _use_line(row)))
    if n.get("usage_branch"):
        print("  with the questions that grew from it: " + _use_line(n["usage_branch"]))
    return True


def run_cli(args: Any, project: Any) -> bool:
    if args.cmd == "usage":
        return _usage_cli(args, project)
    if args.cmd != "spend":
        return False
    r = report(project, args.month)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return True
    t = r["totals"]
    unknown = t["usd_unknown_jobs"]
    head = "spend %s · %s%s" % (r["month"], _usd(t["usd"]), " + ?" if unknown else "")
    if r["cap"] is not None:
        rem = r["remaining"]
        head += " of %s (budget.month_usd) · %s" % (_usd(r["cap"]), ("remaining %s%s" % ("at most " if unknown else "", _usd(rem))) if rem >= 0
                                                    else "over by %s" % _usd(-rem))
    if t["usd_estimated"]:
        head += " · %s of it estimated from tokens" % _usd(t["usd_estimated"])
    if unknown:
        head += " · %d job%s of unknown cost" % (unknown, "" if unknown == 1 else "s")
    print(head)
    print("%-12s %5s %8s %6s %7s %7s %12s %12s %12s %9s" % ("agent", "jobs", "measured", "failed", "stopped", "running", "usd", "tokens in",
                                                          "tokens out", "time"))
    for name, row in list(r["agents"].items()) + [("total", t)]:
        print("%-12s %5d %8d %6d %7d %7d %12s %12s %12s %9s" % (name[:12], row["jobs"], row["measured"], row["failed"], row["stopped"],
                                                              row["running"], _money(row),
                                                              "{:,}".format(row["tokens_in"]) if row["tokens_in"] else "-",
                                                              "{:,}".format(row["tokens_out"]) if row["tokens_out"] else "-", _time(row["seconds"])))
    for title, line in (("each agent's own month:", _caps_line), ("each agent's total (every month):", _total_line),
                        ("each agent's requests (its rate limits; every job of it, now):", _requests_line)):
        lines = [x for x in (line(name, row) for name, row in r["agents"].items()) if x]
        if lines:
            print(title)
            for x in lines:
                print("  " + x)
    return True
