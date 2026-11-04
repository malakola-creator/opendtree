"""dtree's page (static/tree.html) without a browser: the page's own scripts run in node against a stand-in DOM, and what its
functions show for the API's answers — the spend overlay (dollars partly unknown, running jobs, the agents' caps in all), the compare
table's 🔎 cell for a trial verified with nothing selected, the ⟳ Update answer that took commits as they are, and the budget box
(only for spend's own refusals). Skipped without node. A real browser (Playwright, outside the suite) checks the same against a demo
server."""
import html as _html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import dtree

PAGE = Path(dtree.__file__).resolve().parent / "static" / "tree.html"
NODE = shutil.which("node") or shutil.which("nodejs")
pytestmark = pytest.mark.skipif(NODE is None, reason="no node to run the page's script")

# The page's scripts in a node vm. Every element is a stub that keeps what is set on it (document.getElementById gives the same one
# for the same id); fetch answers from `answers` (url regex → JSON) and never answers anything else, so the page's start-up stays
# quiet. `exprs` run after `setup`; `after` runs once the answers' promises have settled. Each value comes back as JSON.
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const job = JSON.parse(fs.readFileSync(0, 'utf8'));
const page = fs.readFileSync(job.page, 'utf8');
const scripts = Array.from(page.matchAll(/<script>([\s\S]*?)<\/script>/g), m => m[1]);
function stub(name) {
  const own = {}, memo = {};
  own.querySelector = sel => memo[sel] || (memo[sel] = stub(name + ' ' + sel));
  own.querySelectorAll = () => [];
  return new Proxy(function () {}, {
    get(t, k) {
      if (Object.prototype.hasOwnProperty.call(own, k)) return own[k];
      if (k === Symbol.toPrimitive || k === 'toString' || k === 'valueOf') return () => '';
      if (k === 'then') return undefined;
      return (own[k] = stub(name + '.' + String(k)));
    },
    set(t, k, v) { own[k] = v; return true; },
    apply() { return stub(name + '()'); },
  });
}
const elements = {}, fetched = [];
const document = stub('document');
document.getElementById = id => elements[id] || (elements[id] = stub('#' + id));
document.querySelector = sel => elements[sel] || (elements[sel] = stub(sel));
document.createElement = tag => stub('<' + tag + '>');
function fetch(url, opts) {
  fetched.push({ url: String(url), method: (opts && opts.method) || 'GET', body: opts && opts.body });
  for (const [re, answer] of job.answers || []) {
    if (new RegExp(re).test(String(url))) return Promise.resolve({ status: 200, json: () => Promise.resolve(answer) });
  }
  return new Promise(() => {});
}
const ctx = { console, document, fetch, setTimeout: () => 0, clearTimeout: () => {}, location: { search: '', hash: '' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, navigator: {}, getComputedStyle: () => ({ getPropertyValue: () => '' }),
  addEventListener() {}, prompt: () => null, confirm: () => false, alert() {}, elements, fetched };
ctx.window = ctx;
vm.createContext(ctx);
const out = { values: [], after: [], error: null };
(async () => {
  try {
    scripts.forEach((s, i) => vm.runInContext(s, ctx, { filename: 'tree.html#script' + i }));
    vm.runInContext('LANG.cur = ' + JSON.stringify(job.lang) + ';\n' + (job.setup || ''), ctx);
    for (const e of job.exprs || []) out.values.push(vm.runInContext('(' + e + ')', ctx));
    for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r));
    for (const e of job.after || []) out.after.push(vm.runInContext('(' + e + ')', ctx));
  } catch (e) { out.error = String(e && e.stack || e); }
  process.stdout.write(JSON.stringify(out));
})();
"""


def run(exprs, lang="en", setup="", answers=None, after=None):
    """The values of the page's JS expressions ``exprs`` (and of ``after``, once the fetch answers came), in language ``lang``."""
    job = {"page": str(PAGE), "lang": lang, "setup": setup, "exprs": list(exprs), "after": list(after or []),
           "answers": [[k, v] for k, v in (answers or {}).items()]}
    p = subprocess.run([NODE, "-e", HARNESS], input=json.dumps(job), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["error"] is None, out["error"]
    return (out["values"], out["after"]) if after else out["values"]


def text(h):
    """What a person reads in an HTML fragment: the tags dropped, the entities read."""
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def titles(h):
    """The tooltips (title attributes) of an HTML fragment."""
    return [_html.unescape(t) for t in re.findall(r'title="([^"]*)"', h or "")]


def j(x):
    return json.dumps(x, ensure_ascii=False)


def test_the_scripts_parse(tmp_path):
    """node --check of each inline script: a syntax error would leave the whole page blank."""
    scripts = re.findall(r"<script>([\s\S]*?)</script>", PAGE.read_text(encoding="utf-8"))
    assert scripts
    for i, s in enumerate(scripts):
        f = tmp_path / ("script%d.js" % i)
        f.write_text(s, encoding="utf-8")
        p = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr


# ------------------------------------------------------------------------- F17 / E10 / F19: the spend overlay (GET /api/spend)
def _row(**kw):
    r = {"jobs": 0, "usd": 0.0, "usd_estimated": 0.0, "tokens_in": 0, "tokens_out": 0, "tokens": 0, "seconds": 0.0, "measured": 0,
         "failed": 0, "stopped": 0, "running": 0, "no_price": 0, "cap_usd": None, "remaining_usd": None, "cap_tokens": None, "remaining_tokens": None}
    r.update(kw)
    return r


# what spend.report answers with the fields of round 3: usd_unknown_jobs per row and in the totals, the agents' caps in all
REPORT = {"month": "2026-11", "cap": 5.0, "remaining": 3.7442, "agents": {
    "claude": _row(jobs=2, usd=1.25, tokens_in=400000, tokens_out=900, tokens=400900, seconds=120.0, measured=1, stopped=1, no_price=1,
                   usd_unknown_jobs=1),
    "muse": _row(jobs=1, usd=0.0058, usd_estimated=0.0058, tokens_in=4000, tokens_out=900, tokens=4900, seconds=14.0, measured=1,
                 usd_unknown_jobs=0, cap_usd=150.0, remaining_usd=149.9942, cap_total_usd=150.0, remaining_total_usd=100.0,
                 cap_total_tokens=2000000, remaining_total_tokens=500000),
    "orin": _row(jobs=2, seconds=600.0, failed=1, running=1, usd_unknown_jobs=2)},
    "totals": _row(jobs=5, usd=1.2558, usd_estimated=0.0058, tokens_in=404000, tokens_out=1800, tokens=405800, seconds=734.0, measured=2,
                   failed=1, stopped=1, running=1, no_price=1, usd_unknown_jobs=3)}


def _table(h):
    """The spend table: its headers and its rows (cells as HTML), from spendHtml's markup."""
    t = h[h.index('<table class="cmp sp">'):]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S)
    head = [text(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", rows[0], re.S)]
    body = [re.findall(r"<td[^>]*>(.*?)</td>", r, re.S) for r in rows[1:]]
    return head, {text(r[0]).split(" ")[0]: r for r in body}


def test_spend_dollars_partly_unknown_say_plus_question_mark():
    """F17: a row with known dollars and jobs whose dollars are unknown shows "+ ?" (the total and the line by the budget bar too); a
    row with no dollar known shows "—", not $0.00 (E10); small amounts keep 4 decimals."""
    h = run(["spendHtml(%s)" % j(REPORT)])[0]
    head, rows = _table(h)
    usd = head.index("$")
    assert text(rows["claude"][usd]) == "$1.25 + ?"
    assert any("1 job" in t and "unknown" in t for t in titles(rows["claude"][usd]))
    assert text(rows["orin"][usd]) == "—"
    assert text(rows["muse"][usd]) == "~$0.0058"
    assert text(rows["total"][usd]) == "$1.26 + ? (~$0.0058 estimated)"
    top = text(h[:h.index("<table")])
    assert top.startswith("$1.26 + ? (~$0.0058 estimated) of $5.00 (budget.month_usd)")
    assert "at most $3.74 left" in top                       # what is left leaves the unknown dollars out
    th = text(run(["spendHtml(%s)" % j(REPORT)], "th")[0])
    assert "$1.25 + ?" in th and "เหลือไม่เกิน $3.74" in th


def test_spend_dash_when_no_dollar_is_known():
    """With usd_known_jobs (spend.report of round 3): "—" when no job's dollars are known and some are unknown or still running — the
    Orin's row of the real run (one failed job of unknown dollars, one running: it read $0.00); "$0.00" when nothing was spent (a job that
    never reached its agent), or when the jobs known cost $0; "+ ?" beside known dollars, $0 too."""
    rows = [_row(jobs=2, failed=1, running=1, usd_unknown_jobs=1, usd_known_jobs=0),
            _row(jobs=1, running=1, usd_unknown_jobs=0, usd_known_jobs=0),
            _row(jobs=1, failed=1, usd_unknown_jobs=0, usd_known_jobs=0),
            _row(jobs=2, measured=1, failed=1, usd_unknown_jobs=1, usd_known_jobs=1),
            _row(jobs=1, measured=1, usd=0.0058, usd_unknown_jobs=0, usd_known_jobs=1)]
    got = run(["money(%s)" % j(r) for r in rows])
    assert [text(h) for h in got] == ["—", "—", "$0.00", "$0.00 + ?", "$0.0058"]
    assert "1 job of unknown dollars" in titles(got[0])[0] and "still running" in titles(got[1])[0]


def test_spend_top_line_counts_the_jobs_of_unknown_dollars():
    top = text(run(["spendHtml(%s)" % j(REPORT)])[0].split("<table")[0])
    assert top == "$1.26 + ? (~$0.0058 estimated) of $5.00 (budget.month_usd) · at most $3.74 left · 3 jobs of unknown cost"


def test_spend_caps_in_all_use_what_the_report_says_was_spent():
    """spend.report gives spent_total_usd / spent_total_tokens: the page shows them as they are (cap − remaining only without them)."""
    rep = json.loads(j(REPORT))
    rep["agents"]["muse"].update(spent_total_usd=112.5, remaining_total_usd=None, spent_total_tokens=500000, remaining_total_tokens=None)
    caps = text(_table(run(["spendHtml(%s)" % j(rep)])[0])[1]["muse"][-1])
    assert "$112.50 / $150.00 in all (agents.muse.total_usd)" in caps and "500,000 / 2,000,000 tok in all (agents.muse.total_tokens)" in caps


def test_spend_caps_in_all_with_jobs_of_unknown_dollars():
    """spent_total_unknown_jobs: the dollars in all leave some jobs out — "+ ?" beside them and "at most" before what is left, as the CLI."""
    rep = json.loads(j(REPORT))
    rep["agents"]["muse"].update(spent_total_usd=112.5, remaining_total_usd=37.5, spent_total_unknown_jobs=2)
    caps = _table(run(["spendHtml(%s)" % j(rep)])[0])[1]["muse"][-1]
    assert "$112.50 + ? / $150.00 in all · at most $37.50 left (agents.muse.total_usd)" in text(caps)
    assert any("2 jobs of unknown dollars" in t for t in titles(caps))
    rep["agents"]["muse"]["spent_total_unknown_jobs"] = 0
    assert "$112.50 / $150.00 in all · $37.50 left (agents.muse.total_usd)" in text(_table(run(["spendHtml(%s)" % j(rep)])[0])[1]["muse"][-1])


def test_spend_without_the_new_fields_says_nothing_of_them():
    """A report without usd_unknown_jobs or the caps in all (an older server): no "+ ?", no total caps, the old "—" for tokens with no
    price still there."""
    old = json.loads(j(REPORT))
    for r in list(old["agents"].values()) + [old["totals"]]:
        for k in ("usd_unknown_jobs", "cap_total_usd", "remaining_total_usd", "cap_total_tokens", "remaining_total_tokens"):
            r.pop(k, None)
    old["agents"]["orin"]["no_price"] = 1
    h = run(["spendHtml(%s)" % j(old)])[0]
    head, rows = _table(h)
    usd = head.index("$")
    assert "+ ?" not in text(h)
    assert text(rows["claude"][usd]) == "$1.25"
    assert text(rows["orin"][usd]) == "—"
    assert "total_usd" not in h and "in all" not in text(h)
    assert "at most" not in text(h)


def test_spend_running_jobs_have_their_own_column():
    """E10: how many jobs still run is a column of its own, beside measured / failed / stopped."""
    h = run(["spendHtml(%s)" % j(REPORT)])[0]
    head, rows = _table(h)
    assert "running" in head
    k = head.index("running")
    assert text(rows["orin"][k]) == "⏳ 1"
    assert text(rows["claude"][k]) == "0"
    assert text(rows["total"][k]) == "⏳ 1"
    assert "⏳" not in text(rows["orin"][head.index("measured / failed / stopped")])


def test_spend_shows_an_agents_caps_in_all():
    """F19: agents.<name>.total_usd / total_tokens (a credit: all time, not per month) and what is left of them, in the agent's caps."""
    h = run(["spendHtml(%s)" % j(REPORT)])[0]
    head, rows = _table(h)
    caps = text(rows["muse"][-1])
    assert "$50.00 / $150.00 in all · $100.00 left (agents.muse.total_usd)" in caps
    assert "1,500,000 / 2,000,000 tok in all · 500,000 left (agents.muse.total_tokens)" in caps
    assert "$0.0058 / $150.00 · $149.99 left (agents.muse.month_usd)" in caps
    over = json.loads(j(REPORT))
    over["agents"]["muse"].update(remaining_total_usd=-2.5, remaining_total_tokens=0)
    caps = text(_table(run(["spendHtml(%s)" % j(over)])[0])[1]["muse"][-1])
    assert "$152.50 / $150.00 in all · $2.50 over" in caps and "2,000,000 / 2,000,000 tok in all · used up" in caps


def test_agent_chooser_shows_the_caps_in_all():
    """Beside an agent's name wherever one is chosen: its caps in all with what is left (from describe(), else the spend report)."""
    setup = "state.spend = %s;" % j(REPORT)
    a = {"name": "muse", "kind": "chat", "sandbox": "bwrap", "network": "none", "max_budget_usd": None, "max_tokens_total": None,
         "month_usd": 150.0, "month_tokens": None, "prices": {"in": 1.0, "out": 2.0}}
    described = dict(a, total_usd=150.0, total_tokens=2000000)
    got = run(["agentPlain(%s)" % j(a), "agentPlain(%s)" % j(described), "agentPlain(%s)" % j(dict(a, name="claude"))], setup=setup)
    for x in got[:2]:
        assert "$150.00 in all ($100.00 left)" in x and "2M tok in all (500k tok left)" in x
    assert "in all" not in got[2]                            # no caps in all anywhere for it


def test_small_amounts_keep_four_decimals():
    """E10: $0.0058 is not shown as $0.01 (the page already did this; kept)."""
    assert run(["usd(0.0058)", "usd(0.004)", "usd(0.01)", "usd(1.234)"]) == ["$0.0058", "$0.0040", "$0.01", "$1.23"]


# ------------------------------------------------------------------------- F21: verified, with nothing selected to run
SHA, BASE = "5a787f4" + "0" * 33, "fbcefb0" + "0" * 33
NONE_SIDE = {"status": "none", "passed": None, "failed": None, "errors": None, "summary": "nothing selected (fallback always)", "check": None,
             "cached": False, "shared": False}
CHECKED_NONE = {"trial": 2, "status": "measured", "branch": "alt/1-notes", "commits": 1, "verify": {
    "status": "none", "sha": SHA, "base_sha": BASE, "stale": False, "checked_at": 1793590000, "tests": NONE_SIDE, "base": NONE_SIDE,
    "delta": None, "measures": [], "running": False, "settings_changed": False, "enabled": True,
    "selection": {"files": 0, "fallback": "always", "hubs": [], "changed": 1}}}
NEVER = {"trial": 3, "status": "trying", "branch": "alt/1-x", "verify": {
    "status": "none", "sha": None, "base_sha": None, "stale": False, "checked_at": None, "tests": None, "base": None, "delta": None,
    "measures": [], "running": False, "settings_changed": False, "enabled": True, "selection": None}}


def test_verified_with_nothing_selected_is_not_not_verified_yet():
    """F21: dtree verified the trial and its changes selected no test file: the cell and its tooltip say so, in Thai and English."""
    en = run(["verifyCell(%s)" % j(CHECKED_NONE)])[0]
    assert "not verified yet" not in en and "not yet" not in text(en)
    assert text(en) == "· nothing selected to run base nothing selected to run"
    tip = titles(en)[0]
    assert tip.startswith("nothing selected to run\n") and "checked " in tip and "nothing selected (fallback always)" in tip
    th = run(["verifyCell(%s)" % j(CHECKED_NONE)], "th")[0]
    assert "ยังไม่ได้ตรวจ" not in th and "ยังไม่ตรวจ" not in th
    assert "ไม่มีอะไรถูกเลือกให้รัน" in text(th)


def test_never_verified_still_says_not_yet():
    en = run(["verifyCell(%s)" % j(NEVER)])[0]
    assert text(en) == "· not yet" and titles(en)[0].startswith("not verified yet")


def test_measures_passed_and_no_tests_selected():
    """The tests side had nothing selected while a measure ran: the tests side says so, the measure shows its value."""
    row = json.loads(j(CHECKED_NONE))
    row["verify"].update(status="passed", measures=[{"name": "score", "unit": "", "better": "higher", "value": 2.0, "base": 1.0, "delta": 1.0,
                                                     "delta_pct": 100.0, "verdict": "better", "status": "passed", "base_status": "passed",
                                                     "check": 7, "base_check": 8, "summary": None}])
    t = text(run(["verifyCell(%s)" % j(row)])[0])
    assert t.startswith("✓ nothing selected to run base nothing selected to run") and "score 2 (base 1) +100%" in t


def test_history_of_a_verification_with_nothing_selected():
    e = {"action": "verified", "by": "user", "at": 1793590000, "detail": {"trial": 2, "status": "none", "sha": SHA, "base_sha": BASE,
                                                                          "tests": "none: nothing selected (fallback always)",
                                                                          "base_tests": "none: nothing selected (fallback always)", "measures": {}}}
    x = run(["eventExtra(%s)" % j(e)])[0]
    assert "not verified yet" not in x and x.startswith("trial 2 · nothing selected to run · 5a787f4: none: nothing selected")


def test_checks_overlay_of_a_verification_with_nothing_selected():
    """Its log link opens dtree's checks of the trial: none ran, because nothing was selected — not "nothing checked yet"."""
    _, after = run(["showChecks(2, '')"], answers={r"/api/decisions/trials/2/verify$": {"ok": True, "verify": CHECKED_NONE["verify"], "checks": []}},
                   after=["elements.ovlBody.innerHTML"])
    t = text(after[0])
    assert "nothing checked for this trial yet" not in t and "nothing selected to run" in t and "nothing selected (fallback always)" in t
    _, after = run(["showChecks(3, '')"], answers={r"/api/decisions/trials/3/verify$": {"ok": True, "verify": NEVER["verify"], "checks": []}},
                   after=["elements.ovlBody.innerHTML"])
    assert text(after[0]) == "nothing checked for this trial yet"


# ------------------------------------------------------------------------- F24: ⟳ Update answered "up to date" and took commits
TAKEN = {"status": "up to date", "trial": 4, "base_sha": "b2c3d4e5f6a7", "branch_sha": "0a1b2c3d4e5f", "taken": 2, "verify": 4242,
         "note": "taken as they are by user: 2 commits on alt/4-x since the update with main 1111111 started at 2222222 (its job 7 failed), "
                 "holding main b2c3d4e already, so nothing was merged"}


def test_update_that_took_commits_says_so():
    """F24: merge._take's answer — the commits taken as they are (its note) and the verification it started (pid)."""
    en, th = run(["updateHtml(%s, 'main')" % j(TAKEN)])[0], run(["updateHtml(%s, 'main')" % j(TAKEN)], "th")[0]
    t = text(en)
    assert t.startswith("✓ the branch is up to date with main already (b2c3d4e)")
    assert "2 commits on the branch taken as they are, nothing merged · 🔎 verifying again (pid 4242)" in t
    assert TAKEN["note"] in t
    t = text(th)
    assert "รับ 2 commit ของกิ่งตามที่เป็นอยู่" in t and "(pid 4242)" in t and TAKEN["note"] in t
    one = text(run(["updateHtml(%s, 'main')" % j(dict(TAKEN, taken=1, verify=None, verify_error="DecisionError: verification is off"))])[0])
    assert "1 commit on the branch taken as they are, nothing merged" in one and "DecisionError: verification is off" in one
    assert "pid" not in one


def test_update_up_to_date_without_taking_anything():
    u = {"status": "up to date", "trial": 4, "base_sha": "b2c3d4e5f6a7", "branch_sha": "0a1b2c3d4e5f"}
    assert text(run(["updateHtml(%s, 'main')" % j(u)])[0]) == "✓ the branch is up to date with main already (b2c3d4e)"


def test_update_overlay_shows_the_taken_answer():
    """The ⟳ Update overlay's button: the answer in the overlay and in the side panel afterwards (the flash)."""
    setup = ("state.agents = {default: 'claude', items: [{name: 'claude', available: true}]};"
             "state.cmp = {rows: [{trial: 4, branch: 'alt/4-x', merge: {base: 'main', state: 'merged', reason_code: 'update_unchecked',"
             " reason: 'look at them first'}}]};")
    _, after = run(["openUpdate({id: 1, options: [], trials: []}, 4, 'claude')", "elements.upGo.onclick.call(elements.upGo)"], setup=setup,
                   answers={r"/api/decisions/trials/4/update$": {"ok": True, "update": TAKEN}},
                   after=["elements.upMsg.innerHTML", "state.flash.html"])
    for h in after:
        assert TAKEN["note"] in text(h) and "(pid 4242)" in text(h)


def test_history_of_commits_taken_as_they_are():
    """The ``updated`` event of _take (status clean, how "up to date"): taken as they are, at the tip — not "clean · merge <sha>"."""
    e = {"action": "updated", "by": "user", "at": 1793590000, "detail": {"trial": 4, "base_sha": "b2c3d4e5f6a7", "status": "clean", "conflicts": [],
                                                                         "sha": "0a1b2c3d4e5f", "how": "up to date", "note": TAKEN["note"]}}
    x = run(["eventExtra(%s)" % j(e)])[0]
    assert x.startswith("trial 4 · commits taken as they are · from main b2c3d4e · tip 0a1b2c3 · taken as they are by user")
    clean = dict(e, detail=dict(e["detail"], how="plumbing", note=None))
    assert run(["eventExtra(%s)" % j(clean)])[0] == "trial 4 · clean · from main b2c3d4e · merge 0a1b2c3"


# ------------------------------------------------------------------------- F25: the budget box only for spend's own refusals
SPEND_REFUSALS = [
    "the budget of 2026-11 is used: $0.75 of $0.30 spent (budget.month_usd); claude was not started",
    "muse's budget of 2026-11 is used: $150.00 of $150.00 spent (agents.muse.month_usd); it was not started",
    "muse's tokens of 2026-11 are used: 1,200 of 1,000 tokens (agents.muse.month_tokens); it was not started",
    "agents.muse.month_usd cannot be counted: muse reports tokens only, so its dollars come from agents.muse.price_in_per_mtok / "
    "price_out_per_mtok; set them, or cap its month in tokens with agents.muse.month_tokens",
    "agents.claude.max_budget_usd must be a number above 0, not 'lots'",
    "agents.muse.price_in_per_mtok must be a number (0 or more), not 'x'",
    "budget.month_usd must be a number (0 or more), not 'x'",
    "budget.job_tokens must be a number above 0, not 0",
    # the shape of a refusal by a cap: the setting in parentheses, then who was not started (the caps in all of round 3 too)
    "muse's total budget is used: $150.00 of $150.00 spent (agents.muse.total_usd); it was not started",
    "agents.muse.total_tokens must be a number (0 or more), not 'many'",
    # an agent whose spend settings refuse every job is not available (jobs.agent_list, F18): a send names that, then spend's words
    "agent muse cannot run here: agents.muse.month_usd cannot be counted: muse reports tokens only, so its dollars come from "
    "agents.muse.price_in_per_mtok / price_out_per_mtok; set them, or cap its month in tokens with agents.muse.month_tokens",
    "cannot run here: agents.muse.total_usd must be a number (0 or more), not 'lots'",
    "agent off cannot run here: agents.off.month_tokens is 0: no job of it starts this month",
    "agent claude cannot run here: budget.month_usd is 0: no paid job starts",
]
NOT_BUDGET = [
    "alt/4-budget-cap is checked out at /dev/shm/x/mine, not in dtree's worktree for it: update it there, or switch that checkout away",
    "alt/7-month_usd is checked out at /home/u/mine, not in dtree's worktree for it: update it there, or switch that checkout away",
    "alt/2-budget.month_usd has conflict markers left in calc.py: resolve them, then dtree update 2",
    "trial 3 is not measured yet: measure it before it is adopted or kept",
    "no agent 'budget' (agents in .dtree/config.json)",
    "agent claude cannot run here: claude (Claude Code) is not installed: the budget of it is unknown",
]


def test_only_spends_refusals_are_budget_refusals():
    got = run(["isBudget(%s)" % j(x) for x in SPEND_REFUSALS + NOT_BUDGET])
    assert got == [True] * len(SPEND_REFUSALS) + [False] * len(NOT_BUDGET)


def test_budget_box_for_a_refusal_by_the_budget_only():
    """F25: an update refused because the branch alt/4-budget-cap is checked out elsewhere is a plain failure, not "💰 Budget"."""
    for msg, budget in [(NOT_BUDGET[0], False), (SPEND_REFUSALS[0], True)]:
        h = run(["failHtml(%s)" % j({"ok": False, "error": msg})])[0]
        assert ('class="budget"' in h) == budget
        assert text(h) == ("💰 Budget: the job was not started %s See this month's spend" % msg if budget else "Failed: " + msg)


def test_an_answer_with_the_budget_code_is_a_budget_refusal():
    """An answer whose code is "budget" is a refusal by the budget whatever its words; any other code (or none) leaves it to spend's
    own message shapes, so a server that gives every error a code still shows the budget box for spend's refusals."""
    got = run(["isBudget(%s)" % j(x) for x in ({"ok": False, "error": "used up", "code": "budget"},
                                                {"ok": False, "error": SPEND_REFUSALS[0], "code": "refused"},
                                                {"ok": False, "error": NOT_BUDGET[0], "code": "refused"},
                                                {"ok": False, "error": SPEND_REFUSALS[0]})])
    assert got == [True, True, False, True]


def test_a_failure_that_names_budget_does_not_reload_as_a_refusal():
    """budgetRefused re-reads the question only for a real refusal (which wrote a budget_refused event)."""
    got = run(["budgetRefused(%s), state.flash" % j({"ok": False, "error": NOT_BUDGET[0]}),
               "budgetRefused(%s), state.flash.qid" % j({"ok": False, "error": SPEND_REFUSALS[1]})], setup="state.sel = 'd1';")
    assert got == [None, 1]


def test_race_entry_refused_by_the_budget_or_not():
    lines = run(["raceLine(%s)" % j({"agent": "claude", "ok": False, "error": NOT_BUDGET[0]}),
                 "raceLine(%s)" % j({"agent": "muse", "ok": False, "error": SPEND_REFUSALS[1]}),
                 "raceLine(%s)" % j({"agent": "claude", "ok": True, "job": {"id": 9}})])
    assert text(lines[0]) == "⚠ claude · " + NOT_BUDGET[0]
    assert 'class="budget"' in lines[1] and text(lines[1]).startswith("💰 muse · Budget: the job was not started")
    assert text(lines[2]) == "🤖 claude · started job 9"


def test_update_overlay_refusal_that_names_budget_is_not_a_budget_box():
    setup = ("state.agents = {default: 'claude', items: [{name: 'claude', available: true}]}; state.sel = 'd1';"
             "state.cmp = {rows: [{trial: 5, branch: 'alt/4-budget-cap', merge: {base: 'main', state: 'clean', behind: 1}}]};")
    _, after = run(["openUpdate({id: 1, options: [], trials: []}, 5, 'claude')", "elements.upGo.onclick.call(elements.upGo)"], setup=setup,
                   answers={r"/api/decisions/trials/5/update$": {"ok": False, "error": NOT_BUDGET[0]}},
                   after=["elements.upMsg.innerHTML", "state.flash"])
    assert text(after[0]) == "Failed: " + NOT_BUDGET[0] and after[1] is None
