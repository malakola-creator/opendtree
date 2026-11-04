"""The checks of round 3's fixes (2026-11-02) found a few things left, each fixed here with a test that fails on the code before:

- core: what dtree reads of git's files on the host (a FIFO a job left at the worktree's ``.git`` or an admin directory's MERGE_HEAD,
  and one the chat tools are given) never holds it; a directory made again where dtree's worktree was deleted gets no ``.git`` and
  is refused, a ``.git`` directory there is someone's and shuts the gate (never removed), and what a job that removed its ``.git``
  file committed still comes back; a follow-up whose runner cannot start keeps the measurement; ``server.allowed_hosts`` as one name;
  every trial branch within git's rules and 80 characters; what Codex left running is ended before Codex is reaped.
- agents and spend: a chat request given up is counted (at least its prompt) and marked partial; the chat loop's last words cannot
  lose its tokens; a Claude job whose sandbox never started cost nothing; tokens nobody knows show "+ ?" and "at most"; amounts
  below $0.0001; ListAgents disallowed.
- verify: a changed conftest.py or hub module with no ``always`` set runs its tests; hubs one per directory, at most 20; a wait
  outlasts the time limit while the other runner moves on; ``--background`` refused at once; a quoted interpreter path in the key.
- the page: agent names with dots in budget refusals, "at most … left" in the agent chooser, unknown tokens, <$0.0001; and the
  API's refusals carry their code.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import test_chat
import test_r3_core as r3
from dtree import cli, gitcode, jobs, merge, project, server, spend, verify
from dtree.agents import _proc
from dtree.project import Project
from dtree.store import DecisionError, DecisionStore
from test_agents import CLAUDE, Q, _exe, _trial
from test_page import NODE, NOT_BUDGET, REPORT, SPEND_REFUSALS, _table, j, run, text
from test_shadow_trust import _git, kept  # noqa: F401
from test_verify import CALC, THREE, _quiet, root  # noqa: F401
import test_verify as tv

ROOT = Path(__file__).resolve().parents[1]


def _within(fn, seconds=15.0):
    """``fn()`` in a thread: its value, or its exception; a failure when it is still running after ``seconds`` (it blocked)."""
    box = {}

    def go():
        try:
            box["out"] = fn()
        except BaseException as exc:                                 # raised again by the caller
            box["exc"] = exc
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(seconds)
    assert not th.is_alive(), "still blocked after %s s" % seconds
    if "exc" in box:
        raise box["exc"]
    return box.get("out")


def _fifo(path):
    if os.path.lexists(path):
        os.unlink(path)
    os.mkfifo(path)


# ================================================================================================================== core
def test_a_fifo_at_the_worktree_s_git_holds_nothing_and_is_put_back(kept):
    """trust() read <wt>/.git with Python before git did: a FIFO there blocked it for ever, and with it the runner after a job and
    every readiness poll (git itself refuses such a .git)."""
    p, wt = kept["p"], kept["wt"]
    good = (wt / ".git").read_text()
    _fifo(wt / ".git")
    assert "the worktree's .git put back" in _within(lambda: jobs.trust(p, wt))
    assert (wt / ".git").is_file() and (wt / ".git").read_text() == good
    _fifo(wt / ".git")
    r = _within(lambda: merge.readiness(p, p.store.trial(kept["t"]["id"])))
    assert r["can_merge"], r
    assert (wt / ".git").read_text() == good


def test_a_fifo_at_the_admin_directory_s_merge_head_holds_nothing(kept):
    p, wt = kept["p"], kept["wt"]
    admin = Path((wt / ".git").read_text().split("gitdir:", 1)[1].strip())
    _fifo(admin / "MERGE_HEAD")
    try:
        assert _within(lambda: p.git.merge_head(wt)) is None
        with pytest.raises(OSError):
            gitcode.read_file(admin / "MERGE_HEAD")
    finally:
        os.unlink(admin / "MERGE_HEAD")


def test_dtree_reads_only_small_regular_files(tmp_path):
    f = tmp_path / "f"
    f.write_text("gitdir: x\n")
    assert gitcode.read_file(f) == "gitdir: x\n"
    (tmp_path / "link").symlink_to(f)
    assert gitcode.read_file(tmp_path / "link") == "gitdir: x\n"            # git's own reads follow a link …
    with pytest.raises(OSError):
        gitcode.read_file(tmp_path / "link", follow=False)                   # … the trust of a shadow does not
    with pytest.raises(OSError):
        gitcode.read_file(tmp_path)                                          # a directory
    big = tmp_path / "big"
    big.write_bytes(b"x" * 101)
    with pytest.raises(OSError):
        gitcode.read_file(big, limit=100)
    assert gitcode.read_bytes(big, limit=101) == b"x" * 101


def test_the_chat_tools_never_wait_on_a_fifo_the_job_made(repo, models):
    """read_file / write_file / edit_file opened whatever the path named: a FIFO the job's own `run` made held the loop for ever."""
    model = models([{"calls": [("run", {"command": "mkfifo pipe && echo made"})]},
                    {"calls": [("read_file", {"path": "pipe"}), ("write_file", {"path": "pipe", "content": "x"}),
                               ("edit_file", {"path": "pipe", "old": "a", "new": "b"})]},
                    {"calls": [("finish", test_chat.FIN)]}])
    p = test_chat._project(repo, {"cheap": test_chat._chat(model)})
    job = _within(lambda: test_chat._run(p)[0], 60)
    told = model.tool_results()
    assert len(told) == 4 and told[0].rstrip().endswith("made"), told
    for result in told[1:]:
        assert result.startswith("error:") and "not a regular file" in result, told
    assert job["reason"] == "the agent did not build it: n", job["reason"]


def test_a_worktree_made_again_by_hand_gets_no_git_and_is_refused(kept):
    """The person deleted dtree's kept worktree and something made the directory again: trust wrote a .git there, turning it into a
    worktree of the shadow with every tracked file deleted (readiness: branch_dirty; the next job got it as it was)."""
    p, wt, t = kept["p"], kept["wt"], kept["t"]
    shutil.rmtree(wt)
    wt.mkdir()
    (wt / "notes.txt").write_text("mine\n")
    jobs.trust(p, wt)
    assert not os.path.lexists(wt / ".git")
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert r["reason_code"] != "branch_dirty" and r["can_merge"], r
    with pytest.raises(DecisionError, match="is not a worktree"):
        jobs._ensure_worktree(p, t["branch"])
    assert sorted(os.listdir(wt)) == ["notes.txt"]


def test_a_git_directory_in_dtree_s_worktree_is_someone_s_and_shuts_the_gate(kept):
    """trust() put dtree's .git file back over whatever stood there — a .git directory too (shutil.rmtree): a person's repository
    made there would be deleted. It is now refused, and kept."""
    p, wt, t = kept["p"], kept["wt"], kept["t"]
    os.unlink(wt / ".git")
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    (wt / ".git" / "mine").write_text("keep me\n")
    with pytest.raises(DecisionError, match="a repository dtree did not make"):
        jobs.trust(p, wt)
    r = merge.readiness(p, p.store.trial(t["id"]))
    assert r["reason_code"] == "untrusted" and not r["can_merge"], r
    assert jobs._remove_worktree(p, wt) is False
    with pytest.raises(DecisionError):
        jobs._ensure_worktree(p, t["branch"])
    assert (wt / ".git" / "mine").read_text() == "keep me\n"


def test_what_a_job_that_removed_its_git_file_committed_still_comes_back(kept):
    p, wt, t = kept["p"], kept["wt"], kept["t"]
    (wt / "h.txt").write_text("more\n")
    _git(wt, "add", "h.txt")
    _git(wt, "commit", "-q", "-m", "more work")
    tip = _git(wt, "rev-parse", "HEAD").strip()
    os.unlink(wt / ".git")
    assert jobs._sync_back(p, t["branch"], wt) is None
    assert _git(kept["repo"]["path"], "rev-parse", t["branch"]).strip() == tip


def test_a_follow_up_whose_runner_cannot_start_keeps_the_trial_s_measurement(repo, monkeypatch):
    """F5/F8's last case: the runner could not be launched (start_job's OSError) after the follow-up had sent the trial back to
    trying with its results cleared — nothing put them back."""
    p = r3._project(repo)
    did, t = _trial(p)
    p.store.trial_result(t["id"], "[claude] 3 passed", "goes right")

    def no_runner(proj, jid):
        raise OSError(24, "Too many open files")
    monkeypatch.setattr(jobs, "launch", no_runner)
    out = jobs.followup(p, t["id"], "say more")
    assert out["status"] == "failed" and "the runner could not start" in out["reason"], out
    tr = p.store.trial(t["id"])
    assert (tr["status"], tr["result_tests"], tr["result_impact"]) == ("measured", "[claude] 3 passed", "goes right")

    def dropped_meanwhile(proj, jid):                                         # nothing to put back: the follow-up still answers
        p.store.trial_end(t["id"], "dropped")
        raise OSError(24, "Too many open files")
    monkeypatch.setattr(jobs, "launch", dropped_meanwhile)
    out = jobs.followup(p, t["id"], "say more again")
    assert out["status"] == "failed" and p.store.trial(t["id"])["status"] == "dropped"


def test_allowed_hosts_as_one_name_is_that_name_and_the_schema_names_it(repo):
    """A string was taken letter by letter (list("dtree.lan")): any host named "d" or "t" was let in."""
    p = r3._project(repo, server={"allowed_hosts": "dtree.lan"})
    assert server.allowed_hosts(p) == ["dtree.lan"]
    httpd, port = r3._serve(p)
    try:
        assert r3._http(port, "GET", "/api/decisions", host="dtree.lan:%d" % port)[0] == 200
        assert r3._http(port, "GET", "/api/decisions", host="d:%d" % port)[0] == 421
    finally:
        httpd.shutdown()
        httpd.server_close()
    props = json.loads((ROOT / "src" / "dtree" / "schema" / "config.schema.json").read_text())["properties"]["server"]["properties"]
    assert props["allowed_hosts"]["type"] == "array" and project.DEFAULTS["server"]["allowed_hosts"] == []


def test_every_trial_branch_fits_git_s_rules_and_80_characters():
    """The 40-hex fallback could make a name of 110 characters; a long key with a long variant one of 84; `a..b` and `x.lock` are no
    branch names git takes."""
    s = DecisionStore(":memory:")
    keys = ["a" * 40, "a" * 39 + "!", "a" * 39 + "?", "a..b", "x.lock", "y."]
    did = s.add(dict(Q, key="k", options=[{"key": k, "th": k} for k in keys]))["id"]
    branches = [s.try_option(did, k, variant="v" * 40)["branch"] for k in keys]
    assert len(set(branches)) == len(branches)
    for b in branches:
        assert gitcode.BRANCH.match(b) and len(b) - len("alt/") <= 80, b
        assert subprocess.run(["git", "check-ref-format", "refs/heads/" + b]).returncode == 0, b
    for room in range(1, 4):                                                  # the hashed names too, when the plain one is taken
        s.trial_end(s.try_option(did, keys[0], variant="w" * room)["id"], "dropped")
    with pytest.raises(DecisionError, match="variant"):
        s.try_option(did, "a..b", variant="x..y")


CHILD = """#!/bin/sh
sleep 60 > /dev/null 2>&1 &
echo $! > %s
echo '{"type": "x"}'
"""


def test_what_codex_left_running_is_ended_before_codex_is_reaped(tmp_path, monkeypatch):
    """The runner killed Codex's process group after Codex had been reaped: by then its id could be another process group's. It is
    killed while Codex is a zombie (its pid, the group's id, still its own), and what it left running ends with it."""
    marker = tmp_path / "child.pid"
    leader = tmp_path / "leader.sh"
    leader.write_text(CHILD % marker)
    leader.chmod(0o755)
    seen, real = [], os.killpg

    def spy(pgid, sig):
        seen.append((pgid, sig, os.path.exists("/proc/%d" % pgid)))     # alive or a zombie: not reaped yet
        return real(pgid, sig)
    monkeypatch.setattr(_proc.os, "killpg", spy)
    ctx = types.SimpleNamespace(set_pid=lambda pid: None, stopped=lambda: False, deadline=time.time() + 60)
    assert list(_proc.stream([str(leader)], tmp_path, "", ctx, end_group=True)) == [{"type": "x"}]
    assert [(sig, unreaped) for _, sig, unreaped in seen] == [(signal.SIGKILL, True)], seen
    child = int(marker.read_text())

    def gone():
        try:
            return Path("/proc/%d/stat" % child).read_text().split(")")[-1].split()[0] in ("Z", "X")
        except OSError:
            return True
    assert tv._until(gone, 10)
    assert ctx.exit_code == 0


# ================================================================================================================== agents and spend
def test_a_request_given_up_is_counted_at_least_as_its_prompt_and_marked_partial(repo, models, capsys):
    """E1 gave a request up on a stop and counted none of it, and the job's dollars read as exact: a credit's total under-counted."""
    model = models([{"calls": [("list_files", {})], "usage": {"prompt_tokens": 40000, "completion_tokens": 500, "total_tokens": 40500}},
                    {"delay": 30, "content": "never"}])
    p = test_chat._project(repo, {"cheap": test_chat._chat(model, price_in_per_mtok=3.0, price_out_per_mtok=15.0, total_tokens=1000000)})
    did, t = test_chat._trial(p)
    jid = p.store.job_open(t["id"], agent="cheap")["id"]

    def stop_while_it_thinks():
        tv._until(lambda: len(model.requests) >= 2, 30)
        time.sleep(0.5)
        p.store.job_set(jid, stop_requested=1)
    threading.Thread(target=stop_while_it_thinks, daemon=True).start()
    job = jobs.run(p, jid)
    r = job["result"]
    assert job["status"] == "stopped" and r["usage_partial"] is True, (job["status"], r)
    assert (r["tokens"], r["tokens_in"], r["tokens_out"]) == (80500, 80000, 500), r       # 40,500 answered + the 40,000 it carried
    assert "counted as at least 40,000 prompt tokens" in "\n".join(jobs.log_tail(job))
    row = spend.report(p)["agents"]["cheap"]
    assert row["usd_unknown_jobs"] == 1 and row["usd_known_jobs"] == 1 and row["tokens_unknown_jobs"] == 1
    assert row["spent_total_unknown_jobs"] == 1 and row["spent_total_tokens_unknown_jobs"] == 1
    spend.run_cli(types.SimpleNamespace(cmd="spend", month=None, json=False), p)
    out = capsys.readouterr().out
    assert "80,500 + ? of 1,000,000 tokens in all (agents.cheap.total_tokens), at most 919,500 left" in out, out
    assert " + ?" in r3_line(out, "cheap")


def r3_line(out, name):
    return [l for l in out.splitlines() if l.split()[:1] == [name]][0]


def test_the_chat_loop_keeps_its_tokens_when_even_its_log_cannot_be_written(repo, models, monkeypatch):
    model = models([{"calls": [("list_files", {})]}, {"calls": [("finish", test_chat.FIN)]}])
    p = test_chat._project(repo, {"cheap": test_chat._chat(model)})
    a = jobs.agents.make(p.agent("cheap"), p)

    def full(line):                                                           # the disk is full when the loop fails
        if line.startswith("⚠ the chat loop failed"):
            raise OSError(28, "No space left on device")
    monkeypatch.setattr(test_chat.chat_mod, "_fit", lambda messages, limit: 1 / 0 if len(model.requests) >= 1 else messages)
    ctx = test_chat._ctx(repo)
    ctx.say = full
    out = a.run("the task", ctx)
    assert out["tokens"] == 10 and "ZeroDivisionError" in out["error"], out


def test_a_claude_job_whose_sandbox_never_started_cost_nothing(repo, monkeypatch):
    """bwrap failing left the job with no tokens and no dollars, counted as a job of unknown cost ("+ ?", "remaining at most")."""
    from test_claude_sandbox import FAKE
    from dtree.agents import claude as claude_mod
    fake = repo["tmp"] / "claude-fake"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    p = r3._project(repo, agents={"claude": {"command": str(fake)}})
    real = claude_mod.ClaudeAgent._mounts
    monkeypatch.setattr(claude_mod.ClaudeAgent, "_mounts", lambda self, *a: real(self, *a) + [("ro", str(repo["tmp"]), "/proc/no/such/place")])
    did, t = _trial(p)
    job = jobs.run(p, p.store.job_open(t["id"], agent="claude")["id"])
    r = job["result"]
    assert job["status"] == "failed" and "the sandbox did not start" in job["reason"], job["reason"]
    assert (r["cost_usd"], r["tokens"], r["tokens_in"], r["tokens_out"]) == (0.0, 0, 0, 0), r
    row = spend.report(p)["agents"]["claude"]
    assert row["usd_unknown_jobs"] == 0 and row["tokens_unknown_jobs"] == 0, row


def test_tokens_nobody_knows_show_plus_question_mark_and_at_most(repo, capsys):
    """A Codex job with no usage counted as 0 against month_tokens / total_tokens, and `dtree spend` said "1,000 left"."""
    import test_spend_r3 as s3
    fake = repo["tmp"] / "codex"
    fake.write_text(s3.CODEX_NO_USAGE % sys.executable)
    fake.chmod(0o755)
    p = s3._project(repo, agents={"codex": {"command": str(fake), "month_tokens": 1000, "total_tokens": 5000}})
    did, t = s3._trial(p)
    jobs.run(p, p.store.job_open(t["id"], agent="codex")["id"])
    row = spend.report(p)["agents"]["codex"]
    assert row["tokens_unknown_jobs"] == 1 and row["spent_total_tokens_unknown_jobs"] == 1
    spend.run_cli(types.SimpleNamespace(cmd="spend", month=None, json=False), p)
    out = capsys.readouterr().out
    assert "codex: 0 + ? of 1,000 tokens (agents.codex.month_tokens), at most 1,000 left" in out, out
    assert "codex: 0 + ? of 5,000 tokens in all (agents.codex.total_tokens), at most 5,000 left" in out, out


def test_amounts_below_a_hundredth_of_a_cent_do_not_read_as_free():
    assert [spend._usd(x) for x in (0.000012, 0.00005, 0.0058, 0, 1.234)] == ["<$0.0001", "$0.0001", "$0.0058", "$0.00", "$1.23"]


def test_claude_code_s_list_of_the_account_s_sessions_is_disallowed_by_default():
    assert "ListAgents" in project.CLAUDE_DISALLOWED and "ListAgents" in project.AGENTS["claude"]["disallowed_tools"]
    assert "`ListAgents`" in (ROOT / "docs" / "sandbox-and-budget.md").read_text(encoding="utf-8")


# ================================================================================================================== verify
BROKEN_FIXTURE = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef broken():\n    raise RuntimeError('broken fixture')\n"


def _many_tests(root, n=11):
    for i in range(n):
        (root / "tests" / ("test_n%02d.py" % i)).write_text("def test_n():\n    pass\n")
    (root / "tests" / "conftest.py").write_text("import pytest\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "more tests")


def test_a_changed_conftest_with_many_tests_below_and_no_always_set_runs_them(root):
    """Rule (e) left a conftest.py with more than hub_limit test files below it to the always set even when there was none: a broken
    autouse fixture was verified `none`."""
    _many_tests(root)
    p = tv._project(root, {"sandbox": "none", "select": {"always": []}})
    t = tv._trial(p, {"tests/conftest.py": BROKEN_FIXTURE})
    sel = verify.selection(p, "main", t["branch"])
    assert len(sel["files"]) == 12 and sel["hubs"] == [], sel                 # the 11 and tests/test_calc.py
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "failed" and out["tests"]["errors"] == 13, out["tests"]   # every test (13) errors on the fixture
    assert out["base"]["passed"] == 13


def test_a_hub_module_with_no_always_set_runs_its_importers(root):
    for i in range(3):
        (root / "tests" / ("test_i%d.py" % i)).write_text("from calc import add\n\n\ndef test_i():\n    assert add(1, 1) == 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "importers")
    p = tv._project(root, {"sandbox": "none", "select": {"always": [], "hub_limit": 2}})
    t = tv._trial(p, {"calc.py": "def add(a, b):\n    return a - b\n"})
    sel = verify.selection(p, "main", t["branch"])
    assert sel["hubs"] == [] and len(sel["files"]) == 4, sel
    with_always = tv._project(root, {"sandbox": "none", "select": {"always": ["tests/test_i0.py"], "hub_limit": 2}})
    assert verify.selection(with_always, "main", t["branch"])["hubs"] == [{"module": "calc", "importers": 4}]


def test_hubs_are_one_per_directory_and_at_most_twenty(root):
    """Rule (e) listed a hub per changed file: 300 data files made 300 entries in every verified event and compare row."""
    _many_tests(root)
    (root / "tests" / "fixtures").mkdir()
    for i in range(25):
        (root / ("m%02d.py" % i)).write_text("X = %d\n" % i)
    imports = "".join("import m%02d\n" % i for i in range(25))
    for name in ("test_a.py", "test_b.py"):
        (root / "tests" / name).write_text(imports + "\n\ndef test_x():\n    pass\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "modules")
    p = tv._project(root, {"sandbox": "none", "select": {"always": ["tests/test_calc.py"]}})
    data = tv._trial(p, {"tests/fixtures/f%03d.json" % i: "[%d]\n" % i for i in range(300)})
    sel = verify.selection(p, "main", data["branch"])
    assert sel["hubs"] == [{"module": "tests/fixtures/f000.json", "importers": 14, "below": "tests", "files": 300}], sel["hubs"]
    hub = tv._project(root, {"sandbox": "none", "select": {"always": ["tests/test_calc.py"], "hub_limit": 1}})
    mods = tv._trial(hub, {"m%02d.py" % i: "X = -%d\n" % i for i in range(25)}, variant="mods", did=data["decision_id"])
    sel = verify.selection(hub, "main", mods["branch"])
    assert len(sel["hubs"]) == 20 and sel["hubs_more"] == 5, sel["hubs"]


def test_a_wait_outlasts_the_time_limit_while_the_other_runner_moves_on(root, monkeypatch):
    """F3's movement rule, which no test held: trial A's shared base measure sits behind A's own base tests (about 6 s) and runs
    6.5 s itself, with time limits of 8 s and a grace of 0.5 s. B waits while A's rows move, and takes A's result at about 12.5 s;
    without the rule B gave up at limit + grace (8.5 s) from the start of its wait, with an error."""
    (root / "slow.py").write_text("S = 1\n")
    (root / "tests" / "test_slow.py").write_text("def test_s():\n    pass\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "slow")
    base = _git(root, "rev-parse", "main").strip()
    on_base = '[ "$(git rev-parse HEAD)" = %s ]' % base
    cmd = 'case "{tests}" in *slow*) %s && sleep 5.5;; esac; %s -m pytest -q {tests}' % (on_base, tv.PY)
    measure = {"name": "m0", "command": "%s && sleep 6.5; echo m0: 1" % on_base, "pattern": r"m0: (\d+)", "timeout_s": 8}
    p = tv._project(root, {"tests": cmd, "sandbox": "none", "timeout_s": 8, "select": {"always": []}, "measures": [measure]})
    monkeypatch.setattr(verify, "WAIT_GRACE_S", 0.5)
    a = tv._trial(p, {"slow.py": "S = 2\n"}, variant="one")
    b = tv._trial(p, {"calc.py": CALC + "\n# b\n"}, variant="two", did=a["decision_id"])
    got, errors = {}, []

    def go(name, t):
        try:
            got[name] = verify.verify_trial(p, t["id"], say=_quiet([]))
        except Exception as exc:
            errors.append(exc)
    ta = threading.Thread(target=go, args=("a", a))
    ta.start()
    assert tv._until(lambda: any(r["sha"] == base and r["name"] == "m0" and r["status"] == "running" for r in p.store.checks()))
    t0 = time.time()
    tb = threading.Thread(target=go, args=("b", b))
    tb.start()
    ta.join(60)
    tb.join(60)
    assert not errors and set(got) == {"a", "b"}, errors
    mb = got["b"]["measures"][0]
    assert (mb["base_status"], mb["base"]) == ("passed", 1.0), mb
    assert mb["base_check"] == got["a"]["measures"][0]["base_check"] and time.time() - t0 > 9.5


def test_background_verification_is_refused_at_once_like_the_foreground(root, capsys):
    """`dtree verify T --background` said "verifying in the background" (exit 0) for a setting that cannot work and for a trial
    whose branch is missing; the error was only in the runner's log."""
    bad = tv._with_config(root, tv._project(root, {"sandbox": "docker"}))
    t = tv._trial(bad, {"tests/test_calc.py": THREE})
    assert cli.main(["--project", str(root), "verify", str(t["id"]), "--background"]) == 1
    assert "sandbox" in capsys.readouterr().err
    tv._with_config(root, tv._project(root, {"sandbox": "none"}))
    _git(root, "branch", "-D", t["branch"])
    assert cli.main(["--project", str(root), "verify", str(t["id"]), "--background"]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not list((root.parent / "logs").glob("verify-*.runner.log"))      # no runner was started


def test_a_quoted_interpreter_path_with_a_space_is_in_the_cache_key(tmp_path):
    exe = tmp_path / "my env" / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    got = [x[0] for x in verify._environment('"%s" -m pytest -q {tests}' % exe, "/usr/bin:/bin")]
    assert str(exe) in got, got


def test_a_temporary_merge_worktree_leaves_no_entry_when_its_removal_fails(tmp_path, monkeypatch):
    """gitcode said "still registered; git worktree prune clears it", but nothing of dtree's prunes the project's worktrees now."""
    r = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True)
    g = gitcode.Git(r)
    tmp = tmp_path / "scratch" / "merge-main-1"
    assert g.run("worktree", "add", "--quiet", str(tmp), "HEAD").returncode == 0
    real, calls = g.run, []

    def failing_once(*args, **kw):
        if args[:2] == ("worktree", "remove") and not calls:
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "error: failed to delete")
        return real(*args, **kw)
    monkeypatch.setattr(g, "run", failing_once)
    g._remove_temporary(tmp)
    assert not tmp.exists() and str(tmp) not in real("worktree", "list").stdout


# ================================================================================================================== the page, the API
DOTTED = [
    "qwen2.5's budget of 2026-11 is used: $0.0058 of $0.0010 spent (agents.qwen2.5.month_usd); it was not started",
    "gpt-4.1's total is used: $150.00 of $150.00 spent (agents.gpt-4.1.total_usd); it was not started",
    "agents.qwen2.5.month_usd cannot be counted: qwen2.5 reports tokens only, so its dollars come from its prices",
    "agent llama3.1 cannot run here: agents.llama3.1.month_tokens is 0: no job of it starts this month",
]


@pytest.mark.skipif(NODE is None, reason="no node to run the page's script")
def test_a_budget_refusal_for_an_agent_with_dots_in_its_name_is_one():
    got = run(["isBudget(%s)" % j(x) for x in DOTTED + SPEND_REFUSALS + NOT_BUDGET])
    assert got == [True] * (len(DOTTED) + len(SPEND_REFUSALS)) + [False] * len(NOT_BUDGET)


@pytest.mark.skipif(NODE is None, reason="no node to run the page's script")
def test_the_agent_chooser_says_at_most_when_some_cost_is_unknown():
    sp = {"month": "2026-11", "agents": {
        "claude": {"remaining_usd": 4.0, "usd_unknown_jobs": 1, "cap_total_usd": 150.0, "remaining_total_usd": 148.74, "spent_total_unknown_jobs": 1},
        "muse": {"remaining_tokens": 600, "tokens_unknown_jobs": 1, "cap_total_tokens": 5000, "remaining_total_tokens": 4000,
                 "spent_total_tokens_unknown_jobs": 2},
        "known": {"remaining_usd": 4.0, "usd_unknown_jobs": 0}}}
    claude = {"name": "claude", "kind": "claude", "sandbox": "bwrap", "network": "proxy", "max_budget_usd": None, "max_tokens_total": None,
              "month_usd": 5.0, "month_tokens": None, "total_usd": 150.0}
    muse = {"name": "muse", "kind": "chat", "sandbox": "bwrap", "network": "none", "max_budget_usd": None, "max_tokens_total": None,
            "month_usd": None, "month_tokens": 1000, "total_tokens": 5000}
    got = run(["agentPlain(%s)" % j(claude), "agentPlain(%s)" % j(muse), "agentPlain(%s)" % j(dict(claude, name="known", total_usd=None))],
              setup="state.spend = %s;" % j(sp))
    assert "$5.00/month (at most $4.00 left)" in got[0] and "$150.00 in all (at most $148.74 left)" in got[0], got[0]
    assert "1k tok/month (at most 600 tok left)" in got[1] and "5k tok in all (at most 4k tok left)" in got[1], got[1]
    assert "$5.00/month ($4.00 left)" in got[2], got[2]


@pytest.mark.skipif(NODE is None, reason="no node to run the page's script")
def test_the_spend_overlay_shows_tokens_nobody_knows_and_amounts_below_a_hundredth_of_a_cent():
    report = json.loads(json.dumps(REPORT))
    report["agents"]["muse"].update(cap_tokens=1000, tokens=400, remaining_tokens=600, tokens_unknown_jobs=1,
                                    spent_total_tokens=1500000, spent_total_tokens_unknown_jobs=1)
    caps = text(_table(run(["spendHtml(%s)" % j(report)])[0])[1]["muse"][-1])
    assert "400 + ? / 1,000 tok · at most 600 left" in caps, caps
    assert "1,500,000 + ? / 2,000,000 tok in all · at most 500,000 left" in caps, caps
    assert run(["usd(0.00001)", "usd(0.00005)", "usd(-0.00001)"]) == ["<$0.0001", "$0.0001", "−<$0.0001"]


def test_a_budget_refusal_answers_with_its_code_and_a_race_entry_too(repo, runner):
    """The page tells a budget refusal by its code first, but neither the API's answer nor a race's entry carried it."""
    p = r3._project(repo, budget={"month_usd": 0.5})
    did, t = _trial(p)
    done = p.store.job_open(t["id"], agent="claude")
    p.store.job_set(done["id"], cost_usd=0.6)
    p.store.job_end(done["id"], "measured", "built", {"cost_usd": 0.6})
    code, answer = server.Api(p).handle("POST", "/api/decisions/trials/%d/ai" % t["id"], {}, {"agent": "claude"})
    assert code == 400 and answer["code"] == "budget" and answer["setting"] == "budget.month_usd", answer
    entry = jobs.race(p, did, "right", ["claude"])[0]
    assert not entry["ok"] and entry["code"] == "budget" and entry["setting"] == "budget.month_usd", entry
    code, answer = server.Api(p).handle("POST", "/api/decisions/trials/%d/ai" % t["id"], {}, {"agent": "nobody"})
    assert code == 400 and "code" not in answer, answer                      # a refusal without a code answers as before




# ================================================================================================================== the review of these fixes
CODEX_R4 = r'''#!%(python)s
import json, os, subprocess, sys
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
last = args[args.index("-o") + 1]
sys.stdin.read()
mode = os.environ["FAKE_MODE"]
open(os.environ["FAKE_SEEN"], "w").write(last)
def ev(d):
    print(json.dumps(d), flush=True)
ev({"type": "thread.started", "thread_id": "t4"})
open("g.txt", "w").write("right\n")
subprocess.run(["git", "add", "g.txt"], check=True)
subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "codex builds right"], check=True)
if mode == "child":
    subprocess.Popen(["sleep", "%(marker)s"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)   # in Codex's own group
if mode == "slow":                                                          # the person replaces the worktree meanwhile
    import time
    open(os.environ["FAKE_SEEN"] + ".committed", "w").write("")
    time.sleep(3)
ev({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
if mode == "fifo":
    os.mkfifo(last)
else:
    open(last, "w").write(json.dumps({"built": True, "tests": "1 passed", "impact": "right", "note": ""}))
if mode == "unlink-git":
    os.unlink(".git")
'''


def _codex(repo, monkeypatch, mode):
    marker = "301.%06d" % (os.getpid() % 1000000)                         # `sleep 301.<pid>`: a child ps can tell apart
    exe = repo["tmp"] / "codex"
    exe.write_text(CODEX_R4 % {"python": sys.executable, "marker": marker})
    exe.chmod(0o755)
    seen = repo["tmp"] / "seen.txt"
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setenv("FAKE_SEEN", str(seen))
    p = r3._project(repo, agents={"codex": {"command": str(exe)}}, default_agent="codex")
    did, t = _trial(p)
    return p, t, seen, marker


def test_a_fifo_at_codex_s_result_file_does_not_hold_the_runner(repo, monkeypatch):
    """The runner read Codex's -o file with a plain open(): a FIFO there (in /tmp, which Codex's commands may write) held it for ever,
    the job running with no way to stop it. The file is now in the logs directory, and read only as a plain file."""
    p, t, seen, _ = _codex(repo, monkeypatch, "fifo")
    job = _within(lambda: jobs.run(p, p.store.job_open(t["id"], agent="codex")["id"]), 60)
    assert job["status"] == "failed", job
    assert Path(seen.read_text()).resolve().parent.parent == p.path(p.config["logs"]).resolve()   # <logs>/dtree-codex-*/last.json


def test_a_codex_job_that_removed_its_git_file_leaves_a_worktree_dtree_can_use(repo, monkeypatch):
    """After the fix that writes no .git into a directory made again by hand, a .git the job itself removed was not put back either:
    the worktree stayed, not a worktree any more, and every later job of the trial was refused until a person removed it."""
    p, t, _, _ = _codex(repo, monkeypatch, "unlink-git")
    job = jobs.run(p, p.store.job_open(t["id"], agent="codex")["id"])
    assert job["status"] == "measured", job["reason"]
    assert "the worktree's .git put back" in "\n".join(jobs.log_tail(job))
    assert "codex builds right" in _git(repo["path"], "log", "-1", "--format=%s", t["branch"])
    wt = jobs._ensure_worktree(p, t["branch"])                               # the next job gets a worktree of its own
    assert (wt / "g.txt").read_text() == "right\n"


def test_what_codex_left_in_its_group_ends_with_the_job(repo, monkeypatch):
    """jobs.run's own kill is gone: codex.py's end_group is what ends Codex's leftovers, and only a test through the agent holds it."""
    p, t, _, marker = _codex(repo, monkeypatch, "child")
    job = jobs.run(p, p.store.job_open(t["id"], agent="codex")["id"])
    assert job["status"] == "measured", job["reason"]
    left = lambda: "sleep " + marker in subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    try:
        assert tv._until(lambda: not left(), 10)
    finally:                                                                  # never left behind, whatever the test found
        subprocess.run(["pkill", "-x", "-f", "sleep " + marker])


def test_a_follow_up_whose_job_ends_before_its_agent_keeps_the_measurement(repo, runner):
    """The undo covered only a runner that could not start; a runner that ended the job before its agent ran (no agent, no worktree)
    left the trial trying, its result cleared, though nothing had run."""
    p = r3._project(repo, agents={"claude": {"command": _exe(repo["tmp"] / "claude", CLAUDE), "sandbox": "none"},
                                  "helper": {"kind": "claude", "command": _exe(repo["tmp"] / "claude", CLAUDE), "sandbox": "none"}})
    did, t = _trial(p)
    p.store.trial_result(t["id"], "[claude] 3 passed", "goes right")
    wt = jobs._ensure_worktree(p, t["branch"])
    shutil.rmtree(wt)
    wt.mkdir()
    (wt / "mine.txt").write_text("mine\n")                                    # made again by hand: the runner refuses it
    out = jobs.run(p, jobs.followup(p, t["id"], "say more")["id"])
    assert out["status"] == "failed" and "is not a worktree" in out["reason"] and "measured again" in out["reason"], out["reason"]

    def measured():
        tr = p.store.trial(t["id"])
        return (tr["status"], tr["result_tests"], tr["result_impact"]) == ("measured", "[claude] 3 passed", "goes right")
    assert measured()
    shutil.rmtree(wt)
    job = jobs.followup(p, t["id"], "say more", agent="helper")
    del p.config["agents"]["helper"]                                          # the agent is gone from the config before its runner
    out = jobs.run(p, job["id"])
    assert out["status"] == "failed" and out["reason"].startswith("no agent") and "measured again" in out["reason"], out["reason"]
    assert measured()
    assert p.store.followup_was(job["id"])["status"] == "measured"
    build = p.store.job_open(p.store.try_option(did, "right", variant="other")["id"], agent="claude")
    assert p.store.followup_was(build["id"]) is None                          # a job no follow-up opened: nothing to put back


def test_trial_branches_keep_lock_names_git_takes_and_fix_the_ones_it_does_not():
    """`.lock` was rewritten before the strip (`x.lock.` stayed `alt/N-x.lock`, which git refuses) and also when a variant follows
    (`alt/N-file.lock--claude` is a name git takes: a race trial tried again lost its branch); the 40-hex name could pass 80."""
    s = DecisionStore(":memory:")
    plain = ["x.lock", "x.lock.", "x.lock!", "pkg.lock/"]
    did = s.add(dict(Q, key="plain", options=[{"key": k, "th": k} for k in plain]))["id"]
    for k in plain:
        b = s.try_option(did, k)["branch"]
        assert subprocess.run(["git", "check-ref-format", "refs/heads/" + b]).returncode == 0 and not b.endswith(".lock"), b
    did = s.add(dict(Q, key="race", options=[{"key": "file.lock", "th": "f"}, {"key": "b", "th": "b"}]))["id"]
    assert s.try_option(did, "file.lock", variant="claude")["branch"] == "alt/%d-file.lock--claude" % did   # as before the fix
    a = "a" * 20                                                              # its plain, 6-hex and 10-hex names taken first
    digest = __import__("hashlib").sha1(a.encode()).hexdigest()
    keys = [a + "!", "%s-%s" % (a, digest[:6]), "%s-%s" % (a, digest[:10]), a]
    did = s.add(dict(Q, key="hashed", options=[{"key": k, "th": k} for k in keys]))["id"]
    got = [s.try_option(did, k, variant="v" * 40)["branch"] for k in keys]
    room = 80 - len("%d-" % did) - len("--" + "v" * 40)
    assert len(set(got)) == 4 and got[3] == "alt/%d-%s--%s" % (did, digest[:room], "v" * 40), got   # the digest alone, cut to fit
    for b in got:
        assert gitcode.BRANCH.match(b) and len(b) - len("alt/") <= 80, b
        assert subprocess.run(["git", "check-ref-format", "refs/heads/" + b]).returncode == 0, b


class _Answers:
    """A stand-in chat API answering ``script`` in order: (status, body[, headers]) per request; ``None`` closes the connection
    unanswered."""

    def __init__(self, script):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.script, self.requests = list(script), []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data": []}')

            def do_POST(self):
                me.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                step = me.script.pop(0) if me.script else (200, {"choices": [{"message": {"role": "assistant", "content": "done"}}]})
                if step is None:
                    self.close_connection = True
                    return
                data = json.dumps(step[1]).encode()
                self.send_response(step[0])
                for k, v in (step[2] if len(step) > 2 else {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _calls(*calls, usage=(40000, 500)):
    msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                                                             for i, (n, a) in enumerate(calls)]}
    out = {"choices": [{"message": msg}]}
    if usage is not None:
        out["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}
    return out


def _chat_run(repo, url, lines=None, **cfg):
    p = test_chat._project(repo, {"cheap": dict({"kind": "chat", "base_url": url, "model": "m", "price_in_per_mtok": 3.0,
                                                 "price_out_per_mtok": 15.0}, **cfg)})
    a = jobs.agents.make(p.agent("cheap"), p)
    return a, test_chat._ctx(repo, lines)


@pytest.mark.parametrize("second, partial", [(None, True), ((502, {"error": "bad gateway"}), True), ((400, {"error": "bad request"}), False)])
def test_a_request_that_went_out_and_came_back_without_an_answer_is_counted(repo, second, partial):
    """An answer cut short, a dropped connection or a 5xx went to 'could not be asked' and counted nothing of a request the server had
    (a non-streaming 200 means the answer was generated, so billed); a 4xx refuses it, and nothing was spent (a 429 is the API busy:
    asked again, test_gemini.py)."""
    api = _Answers([(200, _calls(("list_files", {})))] + [second])
    try:
        a, ctx = _chat_run(repo, api.url)
        out = a.run("the task", ctx)
    finally:
        api.close()
    assert out["error"].startswith("the model could not be asked"), out
    if partial:
        assert (out["tokens"], out["tokens_in"], out["usage_partial"]) == (80500, 80000, True), out   # 40,500 + the 40,000 it carried
    else:
        assert (out["tokens"], out["tokens_in"]) == (40500, 40000) and not out.get("usage_partial"), out


def test_a_request_never_sent_is_not_counted(repo):
    """The bound was added whenever a request was given up, also when its connection was never made (a SYN dropped, a TLS handshake
    that hangs): tokens the server never saw. Here the next connect hangs on a full accept queue until request_timeout_s gives it up."""
    import socket
    api = _Answers([(200, _calls(("list_files", {})))])
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(0)
    fillers = []
    for _ in range(4):                                                       # its queue full: a new connect is never answered
        c = socket.socket()
        c.setblocking(False)
        try:
            c.connect(held.getsockname())
        except (BlockingIOError, OSError):
            pass
        fillers.append(c)
    lines = []
    try:
        a, ctx = _chat_run(repo, api.url, lines, request_timeout_s=1)

        def say(line):                                                        # after the first answer: the address that never answers
            lines.append(line)
            if line.startswith("🔧"):
                a.cfg["base_url"] = "http://127.0.0.1:%d/v1" % held.getsockname()[1]
        ctx.say = say
        out = a.run("the task", ctx)
    finally:
        api.close()
        for c in fillers + [held]:
            c.close()
    assert out["error"].startswith("the model did not answer") and (out["tokens"], out["tokens_in"]) == (40500, 40000), out
    assert not out.get("usage_partial") and not any("counted as at least" in l for l in lines), lines


def test_a_given_up_request_whose_context_was_cut_adds_no_tokens_it_did_not_carry(repo, models):
    """With context_chars the given-up request carried less than the conversation (older parts cut), and the reasoning in an answer is
    never sent back: asked + answered was no lower bound."""
    model = models([{"calls": [("write_file", {"path": "big.txt", "content": "x" * 6000})], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}},
                    {"calls": [("list_files", {})], "usage": {"prompt_tokens": 2000, "completion_tokens": 10, "total_tokens": 2010}},
                    {"calls": [("list_files", {})], "usage": {"prompt_tokens": 2100, "completion_tokens": 10, "total_tokens": 2110}},
                    {"delay": 30, "content": "never"}])
    lines = []
    a, ctx = _chat_run(repo, model.url, lines, context_chars=3000, request_timeout_s=1)
    out = a.run("the task", ctx)
    assert out["usage_partial"] is True and out["tokens"] == 4230, out
    assert any("counted as at least 0 prompt tokens (its context was cut (context_chars))" in l for l in lines), lines


def test_the_bound_of_a_request_given_up_is_kept_when_its_log_line_fails(repo, models):
    model = models([{"calls": [("list_files", {})], "usage": {"prompt_tokens": 40000, "completion_tokens": 500, "total_tokens": 40500}},
                    {"delay": 30, "content": "never"}])
    a, ctx = _chat_run(repo, model.url, request_timeout_s=1)

    def full(line):
        if line.startswith("⚠ the model did not answer"):
            raise OSError(28, "No space left on device")
    ctx.say = full
    out = a.run("the task", ctx)
    assert out["error"].startswith("the model did not answer") and (out["tokens"], out["usage_partial"]) == (80500, True), out


def test_answers_with_no_usage_are_tokens_nobody_knows(repo):
    api = _Answers([(200, _calls(("finish", test_chat.FIN), usage=None))])
    try:
        p = test_chat._project(repo, {"cheap": {"kind": "chat", "base_url": api.url, "model": "m", "price_in_per_mtok": 3.0,
                                                 "price_out_per_mtok": 15.0, "month_tokens": 1000}})
        did, t = test_chat._trial(p)
        job = jobs.run(p, p.store.job_open(t["id"], agent="cheap")["id"])
    finally:
        api.close()
    assert job["result"]["usage_partial"] is True, job["result"]
    row = spend.report(p)["agents"]["cheap"]
    assert row["tokens_unknown_jobs"] == 1 and row["usd_unknown_jobs"] == 1, row


def test_an_always_set_that_selects_no_file_makes_no_hub(root):
    _many_tests(root)
    p = tv._project(root, {"sandbox": "none", "select": {"always": ["tests/test_smoke.py"]}})       # renamed away, say
    t = tv._trial(p, {"tests/conftest.py": BROKEN_FIXTURE})
    sel = verify.selection(p, "main", t["branch"])
    assert sel["hubs"] == [] and len(sel["files"]) == 12, sel
    assert verify.verify_trial(p, t["id"], say=_quiet([]))["status"] == "failed"


def test_a_quoted_interpreter_path_inside_bash_c_is_in_the_cache_key(tmp_path):
    exe = tmp_path / "my env" / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    for command in ("bash -o pipefail -c '\"%s\" -m pytest -q {tests} | tail -n 5'" % exe, "sh -c '\"%s\" -m pytest -q {tests}'" % exe):
        assert str(exe) in [x[0] for x in verify._environment(command, "/usr/bin:/bin")], command


def test_the_docs_say_what_hubs_and_background_exit_codes_are_now():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "`dtree verify TRIAL --background` (exit 0 the runner started, 1 refused" in readme
    schema = json.loads((ROOT / "src" / "dtree" / "schema" / "config.schema.json").read_text())
    hub = json.dumps(schema, ensure_ascii=False)
    assert "left to always when always selects a file" in hub
    assert "a process that left the group (`setsid`)" in (ROOT / "docs" / "merge.md").read_text(encoding="utf-8")


# ================================================================================================================== the second review
@pytest.mark.parametrize("code", [307, 302])
def test_a_redirect_is_the_api_s_answer_never_followed_and_never_counted(repo, code):
    """went_out took a redirect for a request the server may have spent (a 307 / 308 POST is not followed by urllib: HTTPError), and
    urllib followed a 301 / 302 / 303 as a GET elsewhere — with the key in its Authorization header."""
    target = _Answers([])
    api = _Answers([(200, _calls(("list_files", {}))), (code, {"moved": True}, {"Location": target.url + "/chat/completions"})])
    try:
        a, ctx = _chat_run(repo, api.url, key_env="FAKE_R4_KEY")
        os.environ["FAKE_R4_KEY"] = "sk-test-not-real"
        out = a.run("the task", ctx)
    finally:
        os.environ.pop("FAKE_R4_KEY", None)
        api.close()
        target.close()
    assert out["error"] == "the model could not be asked: the API answered %d: {\"moved\": true}" % code, out
    assert (out["tokens"], out["tokens_in"]) == (40500, 40000) and not out.get("usage_partial"), out
    assert target.requests == []                                              # never sent there, nor its key


def test_the_log_says_why_a_given_up_request_adds_nothing(repo, models):
    """With nothing answered yet (or an answer with no prompt size) the bound is 0 too, and the log blamed context_chars."""
    model = models([{"delay": 30, "content": "never"}])
    lines = []
    a, ctx = _chat_run(repo, model.url, lines, request_timeout_s=1)
    out = a.run("the task", ctx)
    assert out["usage_partial"] is True and out["tokens"] == 0, out
    assert any("counted as at least 0 prompt tokens (no earlier answer gave a prompt size)" in l for l in lines), lines


def test_a_directory_a_person_made_while_the_job_ran_gets_no_git(repo, monkeypatch):
    """restore_missing put a .git into any directory at the worktree's place: one a person made there while a long job ran became
    the next job's worktree, their files untracked and the branch's files deleted. The runner now puts .git back only into the
    directory it handed the job (a job cannot replace that one: the directory above is not its to write)."""
    p, t, seen, _ = _codex(repo, monkeypatch, "slow")
    wt = p.path(p.config["worktrees"]) / t["branch"].replace("/", "-")

    def replace_it():
        assert tv._until(lambda: os.path.exists(str(seen) + ".committed"), 30)
        shutil.rmtree(wt)
        wt.mkdir()
        (wt / "notes.txt").write_text("mine\n")
    th = threading.Thread(target=replace_it)
    th.start()
    job = jobs.run(p, p.store.job_open(t["id"], agent="codex")["id"])
    th.join(30)
    assert job["status"] == "measured", job["reason"]
    assert not os.path.lexists(wt / ".git") and sorted(os.listdir(wt)) == ["notes.txt"]
    assert "codex builds right" in _git(repo["path"], "log", "-1", "--format=%s", t["branch"])   # the work came back all the same
    with pytest.raises(DecisionError, match="is not a worktree"):
        jobs._ensure_worktree(p, t["branch"])


def test_a_follow_up_whose_runner_dies_before_its_agent_keeps_the_measurement(repo, runner):
    """reap ended such a job 'the runner is gone' and put nothing back (a runner that dies at its start: a config left broken)."""
    p = r3._project(repo)
    did, t = _trial(p)
    p.store.trial_result(t["id"], "[claude] 3 passed", "goes right")
    job = jobs.followup(p, t["id"], "say more")
    dead = subprocess.Popen(["true"])
    dead.wait()
    p.store.job_set(job["id"], runner_pid=dead.pid)
    assert jobs.reap(p.store) == [job["id"]]
    out = p.store.job(job["id"])
    assert out["status"] == "failed" and out["reason"].startswith("the runner is gone") and "measured again" in out["reason"], out
    tr = p.store.trial(t["id"])
    assert (tr["status"], tr["result_tests"], tr["result_impact"]) == ("measured", "[claude] 3 passed", "goes right")


def test_a_measurement_recorded_while_the_runner_starts_is_kept(repo, runner, monkeypatch):
    """The restore wrote the result from before the follow-up over one recorded while the runner was getting its worktree."""
    p = r3._project(repo)
    did, t = _trial(p)
    p.store.trial_result(t["id"], "[claude] 3 passed", "goes right")
    job = jobs.followup(p, t["id"], "say more")

    def slow_then_refused(project, branch):
        project.store.trial_result(t["id"], "[person] 5 passed", "measured by hand")
        raise DecisionError("git worktree add failed: fatal: something")
    monkeypatch.setattr(jobs, "_ensure_worktree", slow_then_refused)
    out = jobs.run(p, job["id"])
    assert out["status"] == "failed" and "measured again" not in out["reason"], out["reason"]
    tr = p.store.trial(t["id"])
    assert (tr["status"], tr["result_tests"]) == ("measured", "[person] 5 passed")


def test_only_the_follow_up_s_own_job_puts_its_measurement_back(repo, runner):
    """followup_was matched the trial's follow-up by the job's own time; a match on the trial's latest follow-up would put a stale
    measurement back when a later plain job of the trial is refused before its agent."""
    p = r3._project(repo)
    did, t = _trial(p)
    p.store.trial_result(t["id"], "[claude] 3 passed", "goes right")
    follow = jobs.followup(p, t["id"], "say more")
    p.store.job_set(follow["id"], worktree="/nonexistent/wt", agent_pid=12345)   # its agent ran, and failed: the trial stays trying
    p.store.job_end(follow["id"], "failed", "the agent failed")
    plain = p.store.job_open(t["id"], agent="claude")
    assert p.store.followup_was(plain["id"]) is None and p.store.followup_was(follow["id"])["status"] == "measured"
    wt = p.path(p.config["worktrees"]) / t["branch"].replace("/", "-")
    wt.mkdir(parents=True)                                                    # a directory made by hand: the runner refuses it
    out = jobs.run(p, plain["id"])
    assert out["status"] == "failed" and "measured again" not in out["reason"], out["reason"]
    assert p.store.trial(t["id"])["status"] == "trying"


def test_a_hub_module_whose_always_set_selects_no_file_runs_its_importers(root):
    """The other half of 'always selects no file': rule (d), a module imported by more than hub_limit test files."""
    (root / "shared.py").write_text("V = 1\n")
    for i in range(12):
        (root / "tests" / ("test_s%02d.py" % i)).write_text("import shared\n\n\ndef test_v():\n    assert shared.V == 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "shared")
    p = tv._project(root, {"sandbox": "none", "select": {"always": ["tests/test_smoke.py"]}})
    t = tv._trial(p, {"shared.py": "V = 2\n"})
    sel = verify.selection(p, "main", t["branch"])
    assert sel["hubs"] == [] and len(sel["files"]) == 12, sel
    assert verify.verify_trial(p, t["id"], say=_quiet([]))["status"] == "failed"


def test_an_option_tried_again_keeps_the_branch_it_had_when_git_still_takes_it():
    """The name rule changed twice this round (`.lock`, the hashed cut): a trial made before it, tried again, got a new branch from
    the base and left its earlier work. The latest trial's branch of the same option and variant is now taken again."""
    s = DecisionStore(":memory:")
    did = s.add(dict(Q, key="again", options=[{"key": "pin; uv.lock", "th": "a"}, {"key": "x.lock", "th": "b"}]))["id"]
    t = s.try_option(did, "pin; uv.lock")
    with s._lock:                                                             # as an older dtree named it
        s._db.execute("UPDATE trials SET branch = ? WHERE id = ?", ("alt/%d-pin-uv.lock-8b57cd" % did, t["id"]))
        s._db.commit()
    s.trial_end(t["id"], "dropped")
    assert s.try_option(did, "pin; uv.lock")["branch"] == "alt/%d-pin-uv.lock-8b57cd" % did
    bad = s.try_option(did, "x.lock")
    with s._lock:                                                             # a name git refuses is not taken again
        s._db.execute("UPDATE trials SET branch = ? WHERE id = ?", ("alt/%d-x.lock" % did, bad["id"]))
        s._db.commit()
    s.trial_end(bad["id"], "dropped")
    assert s.try_option(did, "x.lock")["branch"] == "alt/%d-x-lock" % did


runner = r3.runner
models = test_chat.models
