"""verify.py: dtree runs a trial's tests and measures itself, on its tip and on its baseline, in the sandbox — the selection, the
commands, the counts, the cache shared by a race, stale results, timeouts and sandbox failures, the page's routes and the CLI.
Every repository here is made for the test; nothing touches another project."""
import json
import os
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from conftest import ApiClient
from dtree import cli, jobs, sandbox, verify
from dtree.parse_summary import parse_summary
from dtree.project import Project
from dtree.store import DecisionError

PY = sys.executable
BWRAP, WHY = sandbox.check()
needs_bwrap = pytest.mark.skipif(not BWRAP, reason="bwrap cannot run here: %s" % WHY)

Q = {"kind": "decision", "source": "claude", "title_th": "บวก", "title_en": "Adding", "question_th": "บวกอย่างไร", "question_en": "How to add?",
     "options": [{"key": "a", "th": "ก"}, {"key": "b", "th": "ข"}, {"key": "c", "th": "ค"}]}
CALC = "def add(a, b):\n    return a + b\n"
TWO = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n"
THREE = TWO + "\n\ndef test_negative():\n    assert add(-1, 1) == 0\n"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root)] + list(args), check=True, capture_output=True, text=True).stdout


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    (r / "calc.py").write_text(CALC)
    (r / "tests" / "test_calc.py").write_text(TWO)
    (r / "score.txt").write_text("3.0\n")
    (r / ".gitignore").write_text("data/\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "calc with two tests")
    return r


def _project(root, verify_cfg=None, **extra):
    tmp = root.parent
    cfg = {"name": "t", "worktrees": str(tmp / "wt"), "logs": str(tmp / "logs"), "db": str(tmp / "d.sqlite"),
           "verify": dict({"tests": PY + " -m pytest -q {tests}", "select": {"always": ["tests/test_calc.py"]}}, **(verify_cfg or {}))}
    cfg.update(extra)
    return Project(root, cfg)


def _commit(p, branch, files, msg="trial work"):
    """Commit ``files`` ({path: text, or None to delete}) on ``branch`` from a throwaway worktree: the main checkout stays on main."""
    wt = p.root.parent / ("c-%d" % time.time_ns())
    _git(p.root, "worktree", "add", "-q", str(wt), branch)
    for path, text in files.items():
        f = wt / path
        if text is None:
            f.unlink()
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", msg)
    _git(p.root, "worktree", "remove", "--force", str(wt))
    return _git(p.root, "rev-parse", branch).strip()


def _trial(p, files, option="b", variant=None, did=None):
    if did is None:
        did = p.store.add(Q)["id"]
        p.store.answer(did, "a")
    t = p.store.try_option(did, option, variant=variant)
    _git(p.root, "branch", t["branch"], "main")
    _commit(p, t["branch"], files)
    return t


def _quiet(lines):
    return lines.append


def _left(p):
    """What verification left in its working directory (nothing: worktrees and output directories are removed)."""
    wd = verify.settings(p)["workdir"]
    return sorted(os.listdir(wd)) if wd.exists() else []


def _with_config(root, p):
    """``p``'s settings written to .dtree/config.json, for what reads them from there (the CLI, a runner in its own process)."""
    (root / ".dtree").mkdir(exist_ok=True)
    keys = ("name", "worktrees", "logs", "db", "verify", "sandbox", "test_command")
    (root / ".dtree" / "config.json").write_text(json.dumps({k: p.config[k] for k in keys}))
    return Project(root)


def _verified(p, t):
    return [e for e in p.store.get(t["decision_id"])["events"] if e["action"] == "verified" and (e.get("detail") or {}).get("trial") == t["id"]]


def _until(cond, timeout=60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _mode(path):
    return stat.S_IMODE(os.stat(str(path)).st_mode)


# ------------------------------------------------------------------------------------------------------------------- selection
def test_the_selection_follows_the_changes_and_leaves_hubs_to_the_always_set(root):
    files = {"src/pkg/__init__.py": "", "src/pkg/core.py": "X = 1\n", "src/pkg/hub.py": "H = 1\n", "src/pkg/util.py": "U = 1\n", "docs/guide.md": "g\n",
             "tests/test_core.py": "from pkg.core import X\n", "tests/test_core_more.py": "import os\n", "tests/test_util.py": "import pkg.util\n",
             "tests/test_misc.py": "from pkg import core\n", "tests/test_docs.py": "import os\n", "tests/test_smoke.py": "import os\n",
             "tests/test_a b.py": "import os\n", "tests/test_pkg.py": "import os\n"}
    files.update({"tests/test_hub%d.py" % i: "import pkg.hub\n" for i in range(3)})
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "a package")
    p = _project(root, {"select": {"always": ["tests/test_smoke.py", "tests/test_gone.py"], "hub_limit": 2,
                                   "map": [{"match": "docs/**", "tests": ["tests/test_docs.py"]}]}})
    t = _trial(p, {"src/pkg/core.py": "X = 2\n", "src/pkg/hub.py": "H = 2\n", "src/pkg/__init__.py": "# the package\n", "docs/guide.md": "g2\n",
                   "tests/test_new.py": "def test_n(): pass\n", "tests/test_a b.py": "import pkg\n", "tests/test_util.py": None})
    sel = verify.selection(p, "main", t["branch"])
    assert sel["files"] == ["tests/test_a b.py", "tests/test_core.py", "tests/test_core_more.py", "tests/test_docs.py", "tests/test_misc.py",
                            "tests/test_new.py", "tests/test_pkg.py", "tests/test_smoke.py", "tests/test_util.py"]
    r = sel["reasons"]
    assert r["tests/test_new.py"] == ["changed"] and r["tests/test_a b.py"] == ["changed"] and r["tests/test_util.py"] == ["changed"]   # (a)
    assert r["tests/test_core.py"][:2] == ["named after src/pkg/core.py", "imports pkg.core"]                          # (b) + (d)
    assert r["tests/test_core_more.py"] == ["named after src/pkg/core.py"]                                             # test_{stem}_*.py
    assert r["tests/test_pkg.py"] == ["named after src/pkg/__init__.py"]                                               # a package's own name
    assert r["tests/test_misc.py"] == ["imports pkg.core"] and r["tests/test_docs.py"] == ["map docs/** ← docs/guide.md"]   # (d), (c)
    assert r["tests/test_smoke.py"] == ["always"] and "tests/test_gone.py" not in r                                     # (e), only what exists
    assert sel["hubs"] == [{"module": "pkg", "importers": 7}, {"module": "pkg.hub", "importers": 3}]                  # hubs: left to always
    assert "tests/test_hub0.py" not in r and sel["changed"] == 7 and sel["fallback"] is None
    assert "tests/test_new.py" in sel["on_tip"] and "tests/test_new.py" not in sel["on_base"]                          # each side runs what it has
    assert "tests/test_util.py" in sel["on_base"] and "tests/test_util.py" not in sel["on_tip"]                        # deleted by the trial
    p2 = _project(root, {"select": {"always": [], "changed": False}})
    assert verify.selection(p2, "main", t["branch"])["files"] == [] and verify.selection(p2, "main", t["branch"])["fallback"] == "always"
    p3 = _project(root, {"select": {"always": ["tests/", "src/pkg/*.py", "nowhere/"], "changed": False}})        # a directory, a glob
    s3 = verify.selection(p3, "main", t["branch"])
    assert s3["files"] == ["src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/hub.py", "src/pkg/util.py", "tests"] and "tests" in s3["on_base"]


def test_the_commands_quote_the_files_add_junit_for_pytest_and_follow_the_fallback(root):
    s = verify.settings(_project(root))
    sel = {"files": ["tests/test_a b.py", "tests/test_new.py"], "on_tip": ["tests/test_a b.py", "tests/test_new.py"], "on_base": ["tests/test_a b.py"],
           "fallback": None}
    tip, base = verify._plan(s, sel, "a" * 40, "tip")[0], verify._plan(s, sel, "b" * 40, "base")[0]
    assert tip["command"] == PY + " -m pytest -q 'tests/test_a b.py' tests/test_new.py" and base["command"] == PY + " -m pytest -q 'tests/test_a b.py'"
    gone = verify._plan(s, dict(sel, on_base=[]), "b" * 40, "base")[0]
    assert gone.get("command") is None and gone["zero"] and "no selected test file exists on bbbbbbb" in gone["none"]
    empty = dict(sel, files=[], on_tip=[], on_base=[], fallback="all")
    assert verify._plan(s, empty, "a" * 40, "tip")[0]["command"] == PY + " -m pytest -q"
    assert "nothing selected" in verify._plan(s, dict(empty, fallback="skip"), "a" * 40, "tip")[0]["none"]
    plain = verify.settings(_project(root, {"tests": None}, test_command="make check"))
    assert plain["command"] == "make check" and not plain["uses_selection"] and verify._plan(plain, None, "a" * 40, "tip")[0]["command"] == "make check"
    assert verify._plan(verify.settings(_project(root, {"tests": None})), None, "a" * 40, "tip") == []           # nothing to run
    assert [verify._is_pytest(c) for c in ("python -m pytest -q", "/usr/bin/pytest x", "(cd sub && pytest)", "pytest", "make test",
                                           "/opt/pytest-venv/bin/python run.py")] == [True, True, True, True, False, False]


def test_settings_that_cannot_work_are_refused_with_what_to_fix(root):
    bad = [{"sandbox": "docker"}, {"measures": [{"name": "m", "command": "x", "pattern": "("}]}, {"timeout_s": 0},
           {"measures": [{"name": "m", "command": "x", "pattern": "a"}, {"name": "m", "command": "y", "pattern": "b"}]},
           {"measures": [{"name": "tests", "command": "x", "pattern": "a"}]}, {"select": {"fallback": "maybe"}}, {"select": {"map": [{"match": "x"}]}},
           {"measures": [{"name": "m", "command": "x", "pattern": "a", "better": "bigger"}]}, {"select": ["tests/x.py"]}, {"measures": {"m": 1}},
           {"measures": [{"name": "m", "command": "x", "pattern": "a", "timeout_s": 0}]}]
    for cfg in bad:
        with pytest.raises(DecisionError):
            verify.settings(_project(root, cfg))
    s = verify.settings(Project(root, {"name": "t"}))
    assert s["select"]["pattern"] == ["tests/test_{stem}.py", "tests/test_{stem}_*.py"] and s["select"]["hub_limit"] == 10 and s["sandbox"] == "bwrap"
    assert s["workdir"] == (root.parent / "t-worktrees" / ".verify").resolve() and s["command"] is None


# -------------------------------------------------------------------------------------------------------------- real runs
@needs_bwrap
def test_a_trial_that_adds_a_test_is_counted_against_its_baseline_in_the_sandbox(root):
    p = _project(root)
    t = _trial(p, {"tests/test_calc.py": THREE})
    lines = []
    out = verify.verify_trial(p, t["id"], by="cli", say=lines.append)
    assert out["status"] == "passed", out
    assert (out["tests"]["passed"], out["base"]["passed"], out["delta"]) == (3, 2, {"passed": 1, "failed": 0, "errors": 0})
    assert out["tests"]["framework"] == "pytest" and out["base_sha"] == _git(root, "rev-parse", "main").strip()
    tip = p.store.check(out["tests"]["check"])
    assert tip["status"] == "passed" and tip["command"] == PY + " -m pytest -q tests/test_calc.py" and tip["sha"] == out["sha"]
    log = Path(tip["log"]).read_text()
    assert "bwrap, allowlist root, no network" in log and "--junitxml=" in log and "-p no:cacheprovider" in log and "3 passed" in log
    assert any(l.startswith("🔎 verify tests on %s: 3 passed, 0 failed · base %s: 2 passed, 0 failed" % (out["sha"][:7], out["base_sha"][:7])) for l in lines), lines
    ev = [e for e in p.store.get(t["decision_id"])["events"] if e["action"] == "verified"][-1]["detail"]
    assert ev["trial"] == t["id"] and ev["status"] == "passed" and ev["tests"] == "3 passed, 0 failed" and ev["base_tests"] == "2 passed, 0 failed"
    f = verify.compare_fields(p, p.store.trial(t["id"]))["verify"]
    assert f["status"] == "passed" and f["tests"]["passed"] == 3 and f["base"]["passed"] == 2 and f["delta"]["passed"] == 1
    assert not f["stale"] and not f["running"] and not f["settings_changed"]
    assert _left(p) == [] and _git(root, "worktree", "list").count("\n") == 1                                   # nothing left behind
    row = jobs.compare(p, t["decision_id"])["rows"][0]
    assert row["verify"]["tests"]["passed"] == 3                                                               # beside the agent's own words


@needs_bwrap
def test_a_failing_trial_fails_and_a_measure_is_compared_with_its_baseline(root):
    p = _project(root, {"measures": [{"name": "score", "command": "echo score: $(cat score.txt)", "pattern": r"score: (?P<value>[0-9.]+)",
                                      "unit": "pt", "better": "higher"},
                                     {"name": "score_int", "command": "echo score: $(cat score.txt)", "pattern": r"score: ([0-9]+)", "better": "lower"}]})
    t = _trial(p, {"calc.py": "def add(a, b):\n    return a - b\n", "score.txt": "3.5\n"})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "failed" and out["tests"]["failed"] == 1 and out["tests"]["passed"] == 1 and out["base"]["status"] == "passed"
    assert out["delta"] == {"passed": -1, "failed": 1, "errors": 0}
    assert "failing: tests.test_calc::test_add" in p.store.check(out["tests"]["check"])["summary"]
    m = {x["name"]: x for x in out["measures"]}
    assert (m["score"]["value"], m["score"]["base"], m["score"]["delta"], m["score"]["delta_pct"], m["score"]["verdict"]) == (3.5, 3.0, 0.5, 16.667, "better")
    assert (m["score_int"]["value"], m["score_int"]["verdict"]) == (3.0, "same") and m["score"]["unit"] == "pt"
    a, b = p.store.check(m["score"]["check"]), p.store.check(m["score_int"]["check"])
    assert a["log"] == b["log"] and Path(a["log"]).read_text().count("$ echo score:") == 1                     # one run fed both patterns
    assert a["kind"] == "measure" and a["status"] == "passed" and a["value"] == 3.5 and a["unit"] == "pt"


def _counted(root, sleep=0):
    counter = root.parent / "counter.txt"
    cmd = "%sgit rev-parse HEAD >> %s; %s -m pytest -q {tests}" % ("sleep %s; " % sleep if sleep else "", counter, PY)
    return counter, {"tests": cmd, "sandbox": "none"}


def _runs(counter, sha):
    return counter.read_text().split().count(sha) if counter.exists() else 0


def test_a_result_is_reused_and_a_baseline_is_shared_by_the_trials_of_a_race(root):
    counter, cfg = _counted(root)
    p = _project(root, cfg)
    a = _trial(p, {"tests/test_calc.py": THREE}, variant="one")
    b = _trial(p, {"calc.py": CALC + "\n# b\n"}, variant="two", did=a["decision_id"])
    base = _git(root, "rev-parse", "main").strip()
    first = verify.verify_trial(p, a["id"], say=_quiet([]))
    assert _runs(counter, first["sha"]) == 1 and _runs(counter, base) == 1 and not first["tests"]["cached"]
    again = verify.verify_trial(p, a["id"], say=_quiet([]))                         # the same commit: nothing runs again
    assert _runs(counter, first["sha"]) == 1 and _runs(counter, base) == 1 and again["tests"]["cached"] and again["base"]["cached"]
    assert again["tests"]["passed"] == 3 and again["tests"]["check"] == first["tests"]["check"]
    other = verify.verify_trial(p, b["id"], say=_quiet([]))                         # another agent's trial from the same base
    assert _runs(counter, other["sha"]) == 1 and _runs(counter, base) == 1 and other["base"]["cached"] and other["base"]["passed"] == 2
    forced = verify.verify_trial(p, a["id"], force=True, say=_quiet([]))
    assert _runs(counter, first["sha"]) == 2 and _runs(counter, base) == 2 and forced["tests"]["check"] != first["tests"]["check"]


def test_two_trials_verified_at_once_run_their_shared_baseline_once(root):
    counter, cfg = _counted(root, sleep=1)
    p = _project(root, cfg)
    a = _trial(p, {"calc.py": CALC + "\n# a\n"}, variant="one")
    b = _trial(p, {"calc.py": CALC + "\n# b\n"}, variant="two", did=a["decision_id"])
    base = _git(root, "rev-parse", "main").strip()
    got, errors = {}, []

    def go(t):
        try:
            got[t["id"]] = verify.verify_trial(p, t["id"], say=_quiet([]))
        except Exception as exc:                                                  # reported below, not lost in the thread
            errors.append(exc)
    threads = [threading.Thread(target=go, args=(t,)) for t in (a, b)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(120)
    assert not errors and len(got) == 2
    assert _runs(counter, base) == 1 and all(s["base"]["passed"] == 2 and s["status"] == "passed" for s in got.values())
    assert sorted(bool(s["base"]["cached"] or s["base"]["shared"]) for s in got.values()) == [False, True]
    assert got[a["id"]]["base"]["check"] == got[b["id"]]["base"]["check"]


def test_a_new_commit_makes_the_result_stale_and_changed_settings_are_told(root):
    counter, cfg = _counted(root)
    p = _project(root, cfg)
    t = _trial(p, {"tests/test_calc.py": THREE})
    assert verify.compare_fields(p, p.store.trial(t["id"]))["verify"]["status"] == "none"
    verify.verify_trial(p, t["id"], say=_quiet([]))
    f = verify.compare_fields(p, p.store.trial(t["id"]))["verify"]
    assert f["status"] == "passed" and not f["stale"]
    _commit(p, t["branch"], {"calc.py": CALC + "\n# more\n"})
    assert verify.compare_fields(p, p.store.trial(t["id"]))["verify"]["stale"]
    p2 = _project(root, dict(cfg, tests=cfg["tests"] + " -x"))
    assert verify.compare_fields(p2, p2.store.trial(t["id"]))["verify"]["settings_changed"]
    assert verify.compare_fields(_project(root, dict(cfg, enabled=False)), p.store.trial(t["id"]))["verify"]["enabled"] is False


def test_a_trial_that_adds_the_only_selected_test_file_has_zero_tests_on_its_baseline(root):
    p = _project(root, {"sandbox": "none", "select": {"always": []}})
    t = _trial(p, {"tests/test_new.py": "def test_one():\n    pass\n"})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["tests"]["passed"] == 1 and out["base"]["status"] == "none" and out["base"]["passed"] == 0 and out["base"]["check"] is None
    assert out["delta"]["passed"] == 1 and out["status"] == "passed"                       # pytest never saw a missing file (exit 4)


def test_a_check_longer_than_its_time_limit_ends_timeout(root):
    p = _project(root, {"tests": "sleep 30", "timeout_s": 1, "baseline": False, "sandbox": "bwrap" if BWRAP else "none"})
    t = _trial(p, {"calc.py": CALC + "\n# t\n"})
    t0 = time.time()
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "timeout" and out["tests"]["status"] == "timeout" and out["base"] is None and time.time() - t0 < 30
    assert verify.verify_trial(p, t["id"], say=_quiet([]))["tests"]["check"] != out["tests"]["check"]   # a timeout is not reused


def test_a_stop_ends_the_check_running(root):
    p = _project(root, {"tests": "sleep 30", "sandbox": "none"})
    t = _trial(p, {"calc.py": CALC + "\n# s\n"})
    flag = threading.Event()
    threading.Timer(1.0, flag.set).start()
    out = verify.verify_trial(p, t["id"], say=_quiet([]), stopped=flag.is_set)
    assert out["status"] == "stopped" and out["tests"]["status"] == "stopped" and out["base"] is None


def test_a_sandbox_that_cannot_run_is_an_error_never_a_run_without_it(root, monkeypatch):
    counter, cfg = _counted(root)
    p = _project(root, dict(cfg, sandbox="bwrap"))
    t = _trial(p, {"calc.py": CALC + "\n# e\n"})
    monkeypatch.setattr(sandbox, "check", lambda: (False, "bwrap cannot create namespaces here (exit 1: test)"))
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "error" and "bwrap cannot create namespaces here" in out["tests"]["summary"] and not counter.exists()
    assert out["base"]["status"] == "error" and _left(p) == []
    assert "bwrap cannot create namespaces" in p.store.check(out["tests"]["check"])["summary"]


@needs_bwrap
def test_a_sandbox_setup_failure_is_an_error_quoting_bwrap(root):
    p = _project(root, {"baseline": False}, sandbox={"tmpfs": ["calc.py/inside"]})       # a tmpfs under a file: bwrap cannot mount it
    t = _trial(p, {"calc.py": CALC + "\n# f\n"})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "error" and out["tests"]["summary"].startswith("bwrap: ") and _left(p) == []


def test_copies_bring_ignored_data_in_without_its_secrets_and_never_fill_the_disk(root, monkeypatch):
    (root / "data").mkdir()
    (root / "data" / "model.txt").write_text("value: 7\n")
    (root / "data" / "write_token").write_text("s3cret\n")
    measures = [{"name": "model", "command": "cat data/model.txt", "pattern": r"value: (\d+)"},
                {"name": "files", "command": "ls data | wc -l", "pattern": r"(\d+)"}]
    p = _project(root, {"tests": None, "measures": measures, "sandbox": "none", "baseline": False},
                 sandbox={"copies": ["data", "missing"], "secrets": ["data/write_token"]})
    t = _trial(p, {"calc.py": CALC + "\n# c\n"})
    lines = []
    out = verify.verify_trial(p, t["id"], say=lines.append)
    assert {m["name"]: m["value"] for m in out["measures"]} == {"model": 7.0, "files": 1.0} and out["status"] == "passed"
    assert any("copy missing: not in the main checkout" in l for l in lines)
    monkeypatch.setattr(verify.shutil, "disk_usage", lambda path: type("U", (), {"free": 1000})())
    out = verify.verify_trial(p, t["id"], force=True, say=_quiet([]))
    assert out["status"] == "error" and "not enough space to copy data" in out["measures"][0]["summary"]


def test_a_commit_cannot_send_a_copy_outside_the_worktree_through_a_link(root):
    """The trial's commit is the agent's: ``data`` made a link to a directory elsewhere must not receive the copy of data/models,
    which dtree makes outside the sandbox."""
    (root / "data" / "models").mkdir(parents=True)
    (root / "data" / "models" / "m.txt").write_text("model\n")
    elsewhere = root.parent / "elsewhere"
    elsewhere.mkdir()
    p = _project(root, {"tests": None, "baseline": False, "sandbox": "none",
                        "measures": [{"name": "seen", "command": "ls data/models 2>/dev/null | wc -l", "pattern": r"(\d+)"}]},
                 sandbox={"copies": ["data/models"]})
    did = p.store.add(Q)["id"]
    p.store.answer(did, "a")
    t = p.store.try_option(did, "b")
    _git(root, "branch", t["branch"], "main")
    wt = root.parent / "linkwt"
    _git(root, "worktree", "add", "-q", str(wt), t["branch"])
    os.symlink(str(elsewhere), str(wt / "data"))
    _git(wt, "add", "data"); _git(wt, "commit", "-q", "-m", "data is a link now")
    _git(root, "worktree", "remove", "--force", str(wt))
    lines = []
    out = verify.verify_trial(p, t["id"], say=lines.append)
    assert os.listdir(elsewhere) == [] and any("a link or a file on its way, not copied" in l for l in lines)
    assert out["measures"][0]["value"] == 0.0


def test_the_junit_file_is_never_read_through_a_link_or_from_a_fifo(tmp_path):
    good = tmp_path / "real.xml"
    good.write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0"/></testsuites>')
    assert verify._junit_counts(good)["passed"] == 2
    os.symlink(str(good), str(tmp_path / "link.xml"))
    os.mkfifo(str(tmp_path / "fifo.xml"))
    t0 = time.time()
    assert verify._junit_counts(tmp_path / "link.xml") is None and verify._junit_counts(tmp_path / "fifo.xml") is None and time.time() - t0 < 5


SHAPES = {"subshell": "(cd . && {py} -m pytest -q {{tests}})", "pipe": "{py} -m pytest -q {{tests}} 2>&1 | tail -n 5",
          "quiet": "{py} -m pytest -q {{tests}} 2>&1 | cat > /dev/null"}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_pytest_gets_its_flags_from_the_environment_whatever_the_command_s_shape(root, shape):
    """The JUnit flags were appended to the whole command: `pytest | tail` handed them to tail (a false `failed`, cached for good,
    on a baseline for every trial from it) and a subshell became a shell syntax error. PYTEST_ADDOPTS reaches pytest wherever the
    command calls it."""
    p = _project(root, {"tests": SHAPES[shape].format(py=PY), "baseline": False, "sandbox": "bwrap" if BWRAP else "none"})
    t = _trial(p, {"tests/test_calc.py": THREE})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    row = p.store.check(out["tests"]["check"])
    assert out["status"] == "passed" and out["tests"]["passed"] == 3 and row["framework"] == "pytest", (row["summary"], row["tail"][-300:])
    log = Path(row["log"]).read_text()
    assert "# PYTEST_ADDOPTS=--junitxml=" in log and "-p no:cacheprovider\n$ %s\n" % row["command"] in log   # the command as it is
    if shape == "quiet":                                                       # nothing printed: the counts are the JUnit file's
        assert row["summary"].startswith("junit: 3 passed, 0 failed")
    again = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert again["status"] == "passed" and again["tests"]["cached"]


CRASHES = {"segv": "import os, resource, signal\n\n\ndef test_crash():\n    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
                   "    os.kill(os.getpid(), signal.SIGSEGV)\n",
           "oom": "import os, signal\n\n\ndef test_killed():\n    os.kill(os.getpid(), signal.SIGKILL)\n",
           "exit0": "import os\n\n\ndef test_gone():\n    os._exit(0)\n"}
ENDS = {"pipe": "{py} -m pytest -q {{tests}} 2>&1 | tail -n 5", "quiet": "{py} -m pytest -q {{tests}} 2>&1 | cat > /dev/null",
        "plain": "{py} -m pytest -q {{tests}}"}


@pytest.mark.parametrize("how, shape", [("segv", "pipe"), ("segv", "quiet"), ("oom", "pipe"), ("oom", "quiet"), ("exit0", "plain")])
def test_a_pytest_that_did_not_finish_is_an_error_not_a_cached_pass_also_behind_a_pipe(root, how, shape):
    """A pipe's exit status is its last command's: pytest killed (the OOM killer) or crashed (a segfault in native code) writes no
    JUnit file and no summary, `tail` or `cat` exits 0, and the run was `passed` with nothing counted, reused for good (on a
    baseline: for every trial from that base). A pytest that ends 0 without finishing (os._exit in a test) did the same with no
    pipe. pytest writes its JUnit file whenever it finishes: no file and no printed counts is `error`, which is run again."""
    p = _project(root, {"tests": ENDS[shape].format(py=PY), "baseline": False, "sandbox": "none", "select": {"always": ["tests/test_crash.py"]}})
    t = _trial(p, {"tests/test_crash.py": CRASHES[how]})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    row = p.store.check(out["tests"]["check"])
    assert out["status"] == "error" and out["tests"]["passed"] is None and row["exit_code"] == 0, (row["summary"], row["tail"][-300:])
    assert "printed no counts" in row["summary"] and "a pipe hides its exit status" in row["summary"], row["summary"]
    again = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert again["status"] == "error" and not again["tests"]["cached"] and again["tests"]["check"] != out["tests"]["check"]   # run again


def test_a_false_pass_kept_by_the_older_code_is_not_reused(root, monkeypatch):
    """A passed check is reused for good, and dtree before VERSION 3 kept such false passes (a crashed pytest behind a pipe): the
    keys it wrote are not looked at any more."""
    cmd = PY + " -m pytest -q tests/test_crash.py 2>&1 | tail -n 5"
    p = _project(root, {"tests": cmd, "baseline": False, "sandbox": "none"})
    t = _trial(p, {"tests/test_crash.py": CRASHES["oom"]})
    tip = _git(root, "rev-parse", t["branch"]).strip()
    s = verify.settings(p)
    item = verify._plan(s, None, tip, "tip")[0]
    with monkeypatch.context() as m:
        m.setattr(verify, "VERSION", 2)
        old_key = verify._key(tip, item, s, sandbox.project_settings(p))
    row = p.store.check_claim(old_key, tip, "tests", "tests", cmd, by="dtree 77ceaa0")
    p.store.check_set(row["id"], status="passed", ended_at=time.time(), exit_code=0, summary="passed")          # what it kept
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "error" and not out["tests"]["cached"] and out["tests"]["check"] != row["id"], out["tests"]


def test_a_command_with_its_own_junitxml_or_pytest_addopts_is_an_error_that_says_so(root):
    """pytest reads PYTEST_ADDOPTS before its command line: a command naming its own --junitxml, or setting PYTEST_ADDOPTS inline,
    hides dtree's flags, so no JUnit file reaches dtree, and with -qq nothing is printed either: it was `passed` with no counts and
    no delta. Now it is an error naming the cause. pytest options for every run go in sandbox.env's PYTEST_ADDOPTS, which dtree
    appends its flags to (a --junitxml there is replaced by dtree's)."""
    for cmd in (PY + " -m pytest -qq --junitxml=report.xml {tests}", PY + " -m pytest -qq --junit-xml report.xml {tests}",
                "PYTEST_ADDOPTS='-p no:randomly' " + PY + " -m pytest -qq {tests}"):
        p = _project(root, {"tests": cmd, "sandbox": "none"})
        t = _trial(p, {"tests/test_calc.py": THREE})
        out = verify.verify_trial(p, t["id"], say=_quiet([]))
        assert out["status"] == "error" and out["tests"]["passed"] is None and out["delta"] is None, (cmd, out["tests"])
        assert "the command sets its own --junitxml or PYTEST_ADDOPTS" in out["tests"]["summary"], out["tests"]["summary"]
    p = _project(root, {"tests": PY + " -m pytest -qq {tests}", "sandbox": "none"},
                 sandbox={"env": {"PYTEST_ADDOPTS": "-p no:randomly --junitxml=report.xml"}})
    t = _trial(p, {"tests/test_calc.py": THREE})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "passed" and (out["tests"]["passed"], out["base"]["passed"]) == (3, 2) and out["delta"]["passed"] == 1
    log = Path(p.store.check(out["tests"]["check"])["log"]).read_text()
    assert "# PYTEST_ADDOPTS=-p no:randomly --junitxml=report.xml --junitxml=" in log                # dtree's flags after the project's


def test_a_trial_that_removes_every_selected_test_file_fails(root, capsys):
    """An agent that breaks the code and deletes its tests used to end `none` (as if never verified) and `dtree verify` exited 0."""
    p = _with_config(root, _project(root, {"sandbox": "none"}))
    t = _trial(p, {"tests/test_calc.py": None, "calc.py": "def add(a, b):\n    return 0\n"})
    with pytest.raises(SystemExit) as exc:
        cli.main(["--project", str(root), "verify", str(t["id"])])
    printed = capsys.readouterr().out
    assert exc.value.code == 1 and "trial %d: failed" % t["id"] in printed and "removed every selected test file" in printed, printed
    ev = _verified(p, t)[-1]["detail"]
    assert ev["status"] == "failed" and ev["tests"] == "failed: the trial removed every selected test file (1 on the base)"
    assert ev["result"]["base"]["passed"] == 2 and ev["result"]["delta"]["passed"] == -2                      # the baseline still ran
    f = verify.compare_fields(p, p.store.trial(t["id"]))["verify"]
    assert f["status"] == "failed" and f["tests"]["status"] == "failed" and f["tests"]["check"] is None       # decided, nothing ran
    s = verify.settings(p)
    sel = {"files": ["tests/test_calc.py"], "on_tip": [], "on_base": ["tests/test_calc.py"], "fallback": None}
    gone = verify._plan(s, sel, "a" * 40, "tip")[0]
    assert gone.get("command") is None and gone["zero"] and "removed every selected test file" in gone["failed"]
    added = verify._plan(s, dict(sel, on_tip=["tests/test_calc.py"], on_base=[]), "b" * 40, "base")[0]      # added by the trial: none
    assert "no selected test file exists" in added["none"] and "failed" not in added


def test_the_cleanup_never_changes_a_host_file_through_a_link_the_tests_left(root, tmp_path):
    """The trial's conftest (run in the sandbox) leaves d/l -> a host file and makes d read-only, so git cannot remove the worktree;
    dtree's own cleanup, outside the sandbox, used to chmod the link's target to 0700."""
    victim = tmp_path / "victim.txt"
    victim.write_text("not the worktree's\n")
    os.chmod(str(victim), 0o640)
    conftest = "import os\nos.makedirs('d', exist_ok=True)\nos.path.lexists('d/l') or os.symlink(%r, 'd/l')\nos.chmod('d', 0o555)\n" % str(victim)
    p = _project(root, {"baseline": False, "sandbox": "bwrap" if BWRAP else "none"})
    t = _trial(p, {"tests/conftest.py": conftest, "tests/test_calc.py": THREE})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert _mode(victim) == 0o640 and victim.read_text() == "not the worktree's\n"
    assert out["status"] == "passed" and _left(p) == [] and _git(root, "worktree", "list").count("\n") == 1


def test_a_directory_the_tests_leave_unreadable_is_removed_and_the_results_kept(root):
    """A mode-000 directory with a file in it: the cleanup's retry called os.open with one argument (TypeError), so verify_trial
    raised after its checks had ended, wrote no verified event and left the checkout in workdir (RAM, in /dev/shm)."""
    conftest = "import os\nos.makedirs('d/e', exist_ok=True)\nopen('d/e/f', 'w').write('x')\nos.chmod('d/e', 0)\n"
    p = _project(root, {"baseline": False, "sandbox": "bwrap" if BWRAP else "none"})
    t = _trial(p, {"tests/conftest.py": conftest, "tests/test_calc.py": THREE})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "passed" and out["tests"]["passed"] == 3 and _verified(p, t)
    assert _left(p) == [] and _git(root, "worktree", "list").count("\n") == 1


def test_a_failed_cleanup_of_the_junit_directory_never_loses_the_result(root, monkeypatch):
    """On Python 3.9 os.walk recurses: a directory tree some 1000 levels deep that the tests leave in the JUnit output directory made
    `_rmtree(out)` raise RecursionError out of the tests check, so the check that had passed ended 'error: verification ended before
    this check ran', verify_trial raised with no verified event and the directory stayed in workdir. That cleanup is now guarded
    like the worktree's: the result is kept and the leftover is told. (3.12's os.walk does not recurse: a stand-in raises here.)"""
    real, how = verify._rmtree, {"out": "raise"}

    def rmtree(path):
        if Path(path).name.startswith("out-"):
            if how["out"] == "raise":
                raise RecursionError("maximum recursion depth exceeded")       # what os.walk raises on 3.9 for such a tree
            return False                                                         # some of it stayed
        return real(path)
    monkeypatch.setattr(verify, "_rmtree", rmtree)
    p = _project(root, {"baseline": False, "sandbox": "none"})
    t = _trial(p, {"tests/test_calc.py": THREE})
    lines = []
    out = verify.verify_trial(p, t["id"], say=lines.append)
    assert out["status"] == "passed" and out["tests"]["passed"] == 3 and _verified(p, t), out["tests"]
    assert p.store.check(out["tests"]["check"])["status"] == "passed"
    assert any(l.startswith("⚠ verify: cleaning up") and "RecursionError" in l for l in lines), lines
    assert [x for x in _left(p) if not x.startswith("out-")] == []                 # the worktree went; the out dir is told
    how["out"], lines = "partly", []
    again = verify.verify_trial(p, t["id"], force=True, say=lines.append)
    assert again["status"] == "passed" and any("could not be removed completely" in l for l in lines), lines


def _other_repo(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "config", "user.email", "t@example.invalid"); _git(other, "config", "user.name", "t")
    (other / "private.txt").write_text("another project's history\n")
    _git(other, "add", "-A"); _git(other, "commit", "-q", "-m", "x")
    return other


@needs_bwrap
def test_the_measures_get_the_mounts_of_the_commit_not_what_the_tests_left(root, tmp_path):
    """The mounts were worked out again for each command, after the tests had written in the worktree: a .git the tests rewrote to
    name another repository showed that repository's git directory to the measures, and the mount point bwrap left for a ro_bind
    looked like the commit's own file, so the measures lost the bound data."""
    other = _other_repo(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "model.txt").write_text("value: 7\n")                                    # git-ignored: shown through ro_binds
    measures = [{"name": "seen", "command": "ls %s 2>/dev/null | wc -l" % (other / ".git"), "pattern": r"(\d+)"},
                {"name": "model", "command": "cat data/model.txt", "pattern": r"value: (\d+)"}]
    p = _project(root, {"baseline": False, "measures": measures}, sandbox={"ro_binds": ["data"]})
    t = _trial(p, {"tests/conftest.py": "open('.git', 'w').write('gitdir: %s\\n')\n" % (other / ".git"), "tests/test_calc.py": THREE})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    m = {x["name"]: x for x in out["measures"]}
    assert out["tests"]["passed"] == 3 and m["seen"]["value"] == 0.0 and m["model"]["value"] == 7.0, m
    assert _git(other, "status", "--porcelain") == "" and _left(p) == [] and _git(root, "worktree", "list").count("\n") == 1


def test_a_run_killed_by_sigkill_is_an_error_and_runs_again(root):
    """The OOM killer (here the command kills its own shell, the first time only) is not the code's verdict: such a run used to end
    `failed`, reused for good — on a shared baseline, for every trial from that base."""
    flags = [root.parent / "killed-once", root.parent / "killed-once-m"]
    kill = "if [ ! -e {0} ]; then : > {0}; kill -9 $$; fi; "
    p = _project(root, {"tests": kill.format(flags[0]) + PY + " -m pytest -q {tests}", "baseline": False, "sandbox": "none",
                        "measures": [{"name": "m", "command": kill.format(flags[1]) + "echo m: 1", "pattern": r"m: (\d+)"}]})
    t = _trial(p, {"calc.py": CALC + "\n# k\n"})
    first = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert first["status"] == "error" and "SIGKILL" in first["tests"]["summary"], first["tests"]
    assert first["measures"][0]["status"] == "error" and first["measures"][0]["value"] is None and "SIGKILL" in first["measures"][0]["summary"]
    again = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert again["status"] == "passed" and not again["tests"]["cached"] and again["tests"]["passed"] == 2 and again["measures"][0]["value"] == 1.0
    for code in (137, -9):
        assert verify._classify({"state": "failed", "exit_code": code}, None, True)[0] == "error"
        assert verify._classify({"state": "failed", "exit_code": code}, {"passed": 1, "failed": 1, "total": 2}, False)[0] == "error"
    assert verify._classify({"state": "failed", "exit_code": 139}, None, False)[0] == "failed"      # its own crash is the code's


def test_a_stop_of_the_trial_running_a_shared_baseline_is_not_the_other_trial_s_result(root, monkeypatch):
    """A runs the baseline B waits for; A's job is stopped. B used to take that `stopped` row as its own baseline (no delta), though
    nobody stopped B: now B claims the check again and runs it itself."""
    base = _git(root, "rev-parse", "main").strip()
    flag = root.parent / "slow-once"                     # slow the first time only: A's run is stopped there, B's run is quick
    cmd = 'if [ "$(git rev-parse HEAD)" = %s ] && [ ! -e %s ]; then : > %s; sleep 30; fi; %s -m pytest -q {tests}' % (base, flag, flag, PY)
    p = _project(root, {"tests": cmd, "sandbox": "none"})
    a = _trial(p, {"calc.py": CALC + "\n# a\n"}, variant="one")
    b = _trial(p, {"calc.py": CALC + "\n# b\n"}, variant="two", did=a["decision_id"])
    waiting, real_wait = threading.Event(), verify._wait

    def spy(*args, **kw):                                                         # B starts waiting for A's check
        waiting.set()
        return real_wait(*args, **kw)
    monkeypatch.setattr(verify, "_wait", spy)
    stop_a, got, errors, said_b = threading.Event(), {}, [], []

    def go(name, t, say, stopped=None):
        try:
            got[name] = verify.verify_trial(p, t["id"], say=say, stopped=stopped)
        except Exception as exc:                                                  # reported below, not lost in the thread
            errors.append(exc)
    ta = threading.Thread(target=go, args=("a", a, _quiet([]), stop_a.is_set))
    ta.start()
    assert _until(lambda: any(r["sha"] == base and r["status"] == "running" for r in p.store.checks()))   # A runs the baseline
    tb = threading.Thread(target=go, args=("b", b, said_b.append))
    tb.start()
    assert waiting.wait(60)                                                       # B ran its tip and waits for A's baseline
    stop_a.set()                                                                  # the person stops A's job; nobody stopped B
    ta.join(60); tb.join(60)
    assert not errors and got["a"]["base"]["status"] == "stopped" and got["a"]["delta"] is None
    assert got["b"]["base"]["status"] == "passed" and got["b"]["base"]["passed"] == 2 and not got["b"]["base"]["shared"]
    assert got["b"]["status"] == "passed" and got["b"]["delta"] == {"passed": 0, "failed": 0, "errors": 0}
    assert any("waiting for its result" in l for l in said_b), said_b             # B's log says why it was idle


def test_an_error_of_the_shared_baseline_in_another_trial_s_run_is_run_again(root, monkeypatch):
    """A runs the baseline B waits for, and A's run is killed (the OOM killer; here the command kills its own shell, the first time
    only). B used to take that `error` as its own shared baseline (no delta), though an error is never reused: now B claims the
    check again and runs it itself."""
    base = _git(root, "rev-parse", "main").strip()
    counter, flag, release = root.parent / "counter.txt", root.parent / "killed-once", root.parent / "release"
    hold = "i=0; while [ ! -e %s ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i+1)); done" % release      # until B waits for it
    cmd = ('git rev-parse HEAD >> %s; if [ "$(git rev-parse HEAD)" = %s ] && [ ! -e %s ]; then : > %s; %s; kill -9 $$; fi; '
           '%s -m pytest -q {tests}' % (counter, base, flag, flag, hold, PY))
    p = _project(root, {"tests": cmd, "sandbox": "none"})
    a = _trial(p, {"calc.py": CALC + "\n# a\n"}, variant="one")
    b = _trial(p, {"calc.py": CALC + "\n# b\n"}, variant="two", did=a["decision_id"])
    waiting, real_wait = threading.Event(), verify._wait

    def spy(*args, **kw):                                                         # B starts waiting for A's check
        waiting.set()
        return real_wait(*args, **kw)
    monkeypatch.setattr(verify, "_wait", spy)
    got, errors = {}, []

    def go(name, t):
        try:
            got[name] = verify.verify_trial(p, t["id"], say=_quiet([]))
        except Exception as exc:                                                  # reported below, not lost in the thread
            errors.append(exc)
    ta = threading.Thread(target=go, args=("a", a))
    ta.start()
    assert _until(lambda: any(r["sha"] == base and r["status"] == "running" for r in p.store.checks()))   # A runs the baseline
    tb = threading.Thread(target=go, args=("b", b))
    tb.start()
    assert waiting.wait(60)                                                       # B ran its tip and waits for A's baseline
    release.write_text("")                                                        # A's run goes on, and is killed
    ta.join(60); tb.join(60)
    assert not errors and got["a"]["base"]["status"] == "error" and "SIGKILL" in got["a"]["base"]["summary"] and got["a"]["delta"] is None
    assert got["b"]["base"]["status"] == "passed" and got["b"]["base"]["passed"] == 2 and not got["b"]["base"]["shared"], got["b"]["base"]
    assert got["b"]["base"]["check"] != got["a"]["base"]["check"] and _runs(counter, base) == 2              # B ran it again itself
    assert got["b"]["status"] == "passed" and got["b"]["delta"] == {"passed": 0, "failed": 0, "errors": 0}


def test_two_runner_processes_share_one_baseline_run(root):
    """A real race is two processes (two job runners, or `python -m dtree.verify run` started by the page), each with its own store
    connection: the second finds the baseline running in the first, a live dtree process, waits, and takes its result."""
    base = _git(root, "rev-parse", "main").strip()
    counter, release = root.parent / "counter.txt", root.parent / "release"
    hold = "i=0; while [ ! -e %s ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i+1)); done" % release      # the baseline waits for the test
    cmd = 'git rev-parse HEAD >> %s; if [ "$(git rev-parse HEAD)" = %s ]; then %s; fi; %s -m pytest -q {tests}' % (counter, base, hold, PY)
    p = _with_config(root, _project(root, {"tests": cmd, "sandbox": "none"}))
    a = _trial(p, {"calc.py": CALC + "\n# a\n"}, variant="one")
    b = _trial(p, {"calc.py": CALC + "\n# b\n"}, variant="two", did=a["decision_id"])
    logs = p.path(p.config["logs"])
    log_of = lambda t: (logs / ("verify-%d.runner.log" % t["id"])).read_text() if (logs / ("verify-%d.runner.log" % t["id"])).exists() else ""
    pa = verify.start_background(p, a["id"], by="race-a")
    assert _until(lambda: any(r["sha"] == base and r["status"] == "running" for r in p.store.checks())), log_of(a)   # a runs the baseline
    pb = verify.start_background(p, b["id"], by="race-b")
    assert _until(lambda: "waiting for its result" in log_of(b)), log_of(b)                                         # b waits for it
    release.write_text("")
    assert _until(lambda: _verified(p, a) and _verified(p, b) and not verify._running(p, a["id"]) and not verify._running(p, b["id"])), \
        log_of(a) + log_of(b)
    ra, rb = _verified(p, a)[-1]["detail"]["result"], _verified(p, b)[-1]["detail"]["result"]
    assert _runs(counter, base) == 1 and ra["base"]["passed"] == rb["base"]["passed"] == 2 and ra["base"]["check"] == rb["base"]["check"]
    assert rb["base"]["shared"] and not ra["base"]["shared"] and not ra["base"]["cached"]
    row = p.store.check(ra["base"]["check"])
    assert row["runner_pid"] == pa and row["by"] == "race-a" and "in process %d" % pa in log_of(b) and len({pa, pb, os.getpid()}) == 3


def test_a_race_shares_measures_and_equal_base_commands_and_each_selection_runs_on_the_base(root):
    """What a race of trials from one base shares (docs/verify.md, the cache): a measure on the base runs once; the base's tests run
    once per distinct selection, since a trial's delta must compare the same test files on both sides."""
    (root / "tests" / "test_other.py").write_text("def test_o():\n    pass\n")
    (root / "other.py").write_text("O = 1\n")
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "other")
    counter, cfg = _counted(root)
    measured = root.parent / "measured.txt"
    cfg["measures"] = [{"name": "size", "command": "git rev-parse HEAD >> %s; wc -c < calc.py" % measured, "pattern": r"(\d+)"}]
    p = _project(root, cfg)
    a = _trial(p, {"calc.py": CALC + "\n# a\n"}, variant="one")                         # selects test_calc (always, named)
    b = _trial(p, {"other.py": "O = 2\n"}, variant="two", did=a["decision_id"])          # test_calc (always) and test_other (named)
    c = _trial(p, {"calc.py": CALC + "\n# c\n"}, variant="three", did=a["decision_id"])  # the same selection as a
    base = _git(root, "rev-parse", "main").strip()
    sa, sb, sc = (verify.verify_trial(p, t["id"], say=_quiet([])) for t in (a, b, c))
    assert (sa["base"]["passed"], sb["base"]["passed"], sc["base"]["passed"]) == (2, 3, 2)
    assert _runs(counter, base) == 2 and not sb["base"]["cached"] and sc["base"]["cached"] and sc["base"]["check"] == sa["base"]["check"]
    assert _runs(measured, base) == 1 and sa["measures"][0]["base_check"] == sb["measures"][0]["base_check"] == sc["measures"][0]["base_check"]


# --------------------------------------------------------------------------------------------------------------- counts
def test_junit_counts_and_how_a_run_is_classified(tmp_path):
    (tmp_path / "test_mixed.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError('setup')\n\n"
        "def test_pass(): pass\ndef test_fail(): assert 0\ndef test_error(broken): pass\n"
        "@pytest.mark.skip(reason='no')\ndef test_skip(): pass\n"
        "@pytest.mark.xfail(reason='known')\ndef test_xfail(): assert 0\n"
        "@pytest.mark.xfail(reason='known')\ndef test_xpass(): pass\n")
    subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=%s" % (tmp_path / "j.xml"), str(tmp_path / "test_mixed.py")],
                   cwd=str(tmp_path), capture_output=True)
    c = verify._junit_counts(tmp_path / "j.xml")
    assert {k: c[k] for k in ("passed", "failed", "errors", "skipped", "xfailed", "total")} == \
        {"passed": 2, "failed": 1, "errors": 1, "skipped": 1, "xfailed": 1, "total": 6}
    assert c["failing"] == ["test_mixed::test_fail", "test_mixed::test_error"]
    (tmp_path / "evil.xml").write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite tests="1"/>')
    assert verify._junit_counts(tmp_path / "evil.xml") is None and verify._junit_counts(tmp_path / "none.xml") is None
    ok, bad, none = {"passed": 3, "failed": 0, "total": 3}, {"passed": 1, "failed": 1, "total": 2}, {"passed": 0, "failed": 0, "total": 0}
    cases = [(("ok", 0), ok, True, "passed"), (("failed", 1), bad, True, "failed"), (("failed", 5), None, True, "error"),
             (("failed", 4), None, True, "error"), (("failed", 2), {"passed": 0, "failed": 0, "errors": 1, "total": 1}, True, "failed"),
             (("failed", 2), None, True, "error"), (("failed", 127), None, False, "error"), (("failed", 1), None, False, "failed"),
             (("ok", 0), None, False, "passed"), (("ok", 0), none, False, "error"), (("timeout", 130), ok, True, "timeout"),
             (("error", None), None, True, "error"), (("ok", 0), bad, False, "failed")]
    for (state, code), counts, pyt, want in cases:
        assert verify._classify({"state": state, "exit_code": code, "error": "bwrap: x"}, counts, pyt)[0] == want, (state, code, counts, pyt)
    missing = verify._classify({"state": "failed", "exit_code": 1}, None, True, "/usr/bin/python3: No module named pytest\n")
    assert missing[0] == "error" and "not installed" in missing[1]                     # the interpreter, not the code
    assert verify._classify({"state": "failed", "exit_code": 1}, bad, True, "No module named pytest")[0] == "failed"   # counted: it ran


def test_the_cleanup_never_follows_a_link_and_empties_unreadable_directories(tmp_path):
    """What the code under test leaves in a tree dtree removes (outside the sandbox): links to host files and directories inside a
    read-only directory, a mode-000 directory with a file in it. The old retry chmodded through the links and raised TypeError."""
    outside, victim = tmp_path / "outside", tmp_path / "victim.txt"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n")
    victim.write_text("v\n")
    os.chmod(str(outside), 0o750); os.chmod(str(victim), 0o640)
    top = tmp_path / "wt"
    (top / "ro").mkdir(parents=True)
    os.symlink(str(victim), str(top / "ro" / "file-link"))
    os.symlink(str(outside), str(top / "ro" / "dir-link"))
    (top / "locked" / "inner").mkdir(parents=True)
    (top / "locked" / "inner" / "f").write_text("x")
    for d, mode in (("locked/inner", 0), ("locked", 0o555), ("ro", 0o555)):
        os.chmod(str(top / d), mode)
    assert verify._rmtree(top) and not os.path.lexists(str(top))
    assert _mode(victim) == 0o640 and _mode(outside) == 0o750 and victim.read_text() == "v\n" and (outside / "keep.txt").read_text() == "keep\n"
    link = tmp_path / "top-link"                                              # the tree itself a link: only the link goes
    os.symlink(str(outside), str(link))
    assert verify._rmtree(link) and not os.path.lexists(str(link)) and (outside / "keep.txt").exists() and _mode(outside) == 0o750


def test_counts_are_bounded_whatever_the_tests_write_or_print(root, tmp_path):
    """The JUnit file and the printed summary are the code under test's: tests="1e400" in the JUnit file, or "10**30 passed"
    printed, raised OverflowError out of verify_trial (no verified event). A count is now a whole number from 0 to 10**9."""
    most = 10 ** 9                                                              # verify.MAX_COUNT
    conftest = ("import atexit\n\ndef pytest_configure(config):\n    path = getattr(config.option, 'xmlpath', None)\n"
                "    if path:\n        atexit.register(lambda: open(path, 'w').write('<testsuites><testsuite tests=\"1e400\" failures=\"0\" "
                "errors=\"0\" skipped=\"0\"/></testsuites>'))\n")
    p = _project(root, {"baseline": False, "sandbox": "none"})
    t = _trial(p, {"tests/conftest.py": conftest, "tests/test_calc.py": THREE})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["tests"]["total"] == most and _verified(p, t)
    printed = _project(root, {"sandbox": "none", "baseline": False, "tests": "echo '%s passed in 0.10s'" % ("9" * 30)})
    t2 = _trial(printed, {"calc.py": CALC + "\n# y\n"})
    out2 = verify.verify_trial(printed, t2["id"], say=_quiet([]))
    assert out2["tests"]["passed"] == most and out2["status"] == "passed" and _verified(printed, t2)
    junit = tmp_path / "j.xml"
    junit.write_text('<testsuites><testsuite tests="1e400" failures="nan" errors="-3" skipped="12abc"/>'
                     '<testsuite tests="%s" failures="1" errors="0" skipped="0"/></testsuites>' % ("9" * 30))
    c = verify._junit_counts(junit)
    assert (c["total"], c["failed"], c["errors"], c["skipped"], c["passed"]) == (most, 1, 0, 0, most - 1)
    huge = parse_summary("%s passed, %s failed in 0.10s\n" % ("9" * 5000, "7" * 30), 1)         # int() of 5000 digits raises
    assert (huge["passed"], huge["failed"], huge["total"]) == (most, most, most) and verify.MAX_COUNT == most


def test_a_measure_is_a_finite_number_so_the_pages_stay_json(root):
    """A measure's tool is the trial's own: printing 400 digits (or inf, nan) stored Infinity in the check, the verified event and
    the job's result, and the question's page, the compare table and the verify route then sent bodies JSON.parse rejects."""
    measures = [{"name": "speed", "command": "echo speed: " + "9" * 400, "pattern": r"speed: ([0-9]+)"},
                {"name": "loose", "command": "echo loose: nan", "pattern": r"loose: (\S+)"}]
    p = _with_config(root, _project(root, {"sandbox": "none", "tests": None, "baseline": False, "measures": measures}))
    t = _trial(p, {"calc.py": CALC + "\n# x\n"})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    c = ApiClient(p)

    def strict(const):
        raise ValueError("not JSON: %s" % const)
    for url in ("/api/decisions/trials/%d/verify" % t["id"], "/api/decisions/%d" % t["decision_id"], "/api/decisions/%d/compare" % t["decision_id"]):
        json.loads(json.dumps(c.get(url).get_json(), ensure_ascii=False), parse_constant=strict)     # what a browser's JSON.parse does
    m = {x["name"]: x for x in out["measures"]}
    assert m["speed"]["value"] is None and m["speed"]["status"] == "error" and "not a finite number" in m["speed"]["summary"]
    assert m["loose"]["value"] is None and m["loose"]["status"] == "error"
    for text in ("x: inf", "x: -inf", "x: nan", "x: 1e999"):
        assert verify._value(r"x: (\S+)", text)[0] is None, text
    assert verify._value(r"x: (\S+)", "x: 2.5e3") == (2500.0, "")
    far = verify._measure({"name": "m", "better": "higher"}, {"value": 1e308, "status": "passed"}, {"value": -1e308, "status": "passed"})
    assert far["delta"] is None and far["delta_pct"] is None                    # two finite numbers whose difference overflows


# ------------------------------------------------------------------------------------------------------- the rest of dtree
def test_after_a_job_the_trial_is_verified_and_the_result_kept_on_the_job(root, monkeypatch):
    from test_agents import CLAUDE, _exe                                         # the Claude Code stand-in: it commits g.txt and says built
    monkeypatch.setenv("FAKE_PROMPT", str(root.parent / "prompt.txt"))
    p = _project(root, {"sandbox": "none"}, agents={"claude": {"command": _exe(root.parent / "claude", CLAUDE), "sandbox": "none"}})
    did = p.store.add(Q)["id"]
    p.store.answer(did, "a")
    t = p.store.try_option(did, "b")
    j = jobs.run(p, p.store.job_open(t["id"], agent="claude")["id"])
    assert j["status"] == "measured", j["reason"]
    v = j["result"]["verify"]
    assert v["status"] == "passed" and v["tests"]["passed"] == 2 and v["base"]["passed"] == 2 and v["delta"]["passed"] == 0
    assert "🔎 verify tests on" in "\n".join(jobs.log_tail(j))
    assert verify.after_job(_project(root, {"auto": False}), j, t, _quiet([])) is None


def test_the_routes_and_the_cli(root, monkeypatch, capsys):
    counter, cfg = _counted(root)
    p = _project(root, cfg)
    (root / ".dtree").mkdir()
    (root / ".dtree" / "config.json").write_text(json.dumps({k: p.config[k] for k in ("name", "worktrees", "logs", "db", "verify")}))
    t = _trial(p, {"tests/test_calc.py": THREE})
    bad = _trial(p, {"calc.py": "def add(a, b):\n    return 0\n"}, option="c", did=t["decision_id"])
    assert cli.main(["--project", str(root), "verify", str(t["id"])]) == 0
    out = capsys.readouterr().out
    assert "🔎 verify tests on" in out and "trial %d: passed" % t["id"] in out
    with pytest.raises(SystemExit) as exc:
        cli.main(["--project", str(root), "verify", str(bad["id"]), "--no-baseline"])
    assert exc.value.code == 1 and "trial %d: failed" % bad["id"] in capsys.readouterr().out
    assert cli.main(["--project", str(root), "checks", "--trial", str(t["id"])]) == 0
    listed = capsys.readouterr().out
    assert listed.count("check ") == 2 and "3 passed, 0 failed" in listed and "2 passed, 0 failed" in listed
    tip7 = _git(root, "rev-parse", t["branch"]).strip()[:7]
    assert cli.main(["--project", str(root), "checks", "--sha", tip7]) == 0 and capsys.readouterr().out.count("check ") == 1
    off = _project(root, dict(cfg, enabled=False))
    with pytest.raises(DecisionError):
        verify.verify_trial(off, t["id"], say=_quiet([]))
    p = Project(root)
    c = ApiClient(p)
    got = c.get("/api/decisions/trials/%d/verify" % t["id"]).get_json()
    assert got["ok"] and got["verify"]["tests"]["passed"] == 3 and len(got["checks"]) == 2 and "tail" not in got["checks"][0]
    cid = got["verify"]["tests"]["check"]
    lines = c.get("/api/checks/%d/log?lines=3" % cid).get_json()
    assert lines["ok"] and len(lines["lines"]) == 3 and lines["check"]["id"] == cid
    assert c.get("/api/checks/9999/log").status_code == 404 and c.get("/api/decisions/trials/999/verify").status_code == 404
    monkeypatch.setattr(verify, "start_background", lambda proj, tid, by="user", force=False: 4242)
    assert c.post("/api/decisions/trials/%d/verify" % t["id"], json={}).get_json()["started"] == 4242
    monkeypatch.setattr(jobs, "_alive", lambda pid, marker="": True)                 # a job's runner that lives
    j = p.store.job_open(t["id"], agent="claude")
    p.store.job_set(j["id"], runner_pid=os.getpid())
    busy = c.post("/api/decisions/trials/%d/verify" % t["id"], json={})
    assert busy.status_code == 400 and "being built" in busy.get_json()["error"]


def test_a_background_verification_runs_in_its_own_process(root):
    counter, cfg = _counted(root)
    p = _project(root, cfg)
    (root / ".dtree").mkdir()
    (root / ".dtree" / "config.json").write_text(json.dumps({k: p.config[k] for k in ("name", "worktrees", "logs", "db", "verify")}))
    p = Project(root)
    t = _trial(p, {"tests/test_calc.py": THREE})
    pid = verify.start_background(p, t["id"], by="test")
    assert pid and verify._running(p, t["id"]) in ([pid], [])                       # running, or already done
    for _ in range(300):
        if not verify._running(p, t["id"]) and [e for e in p.store.get(t["decision_id"])["events"] if e["action"] == "verified"]:
            break
        time.sleep(0.1)
    ev = [e for e in p.store.get(t["decision_id"])["events"] if e["action"] == "verified"]
    assert ev and ev[-1]["by"] == "test" and ev[-1]["detail"]["tests"] == "3 passed, 0 failed", (p.path(p.config["logs"]) / ("verify-%d.runner.log" % t["id"])).read_text()
    assert not list(p.path(p.config["logs"]).glob("verify-*.running"))
    assert verify.start_background(_project(root, {"enabled": False}), t["id"]) is None


def test_no_baseline_reaches_a_background_verification(root, monkeypatch, capsys):
    """`dtree verify T --no-baseline --background` dropped --no-baseline: the detached run verified the baseline after all."""
    p = _with_config(root, _project(root, {"sandbox": "none"}))
    t = _trial(p, {"tests/test_calc.py": THREE})
    seen, real = [], subprocess.Popen

    def fake_popen(cmd, **kw):                                                  # the runner is not started; dtree's own git is (the
        if cmd[:1] == ["git"]:                                                  # trial's branch is checked before the runner starts)
            return real(cmd, **kw)
        seen.append(cmd)
        return types.SimpleNamespace(pid=999999)
    with monkeypatch.context() as m:                                            # subprocess itself: undone before anything else runs
        m.setattr(verify.subprocess, "Popen", fake_popen)
        assert cli.main(["--project", str(root), "verify", str(t["id"]), "--no-baseline", "--background", "--force"]) == 0
        assert cli.main(["--project", str(root), "verify", str(t["id"]), "--background"]) == 0
    assert seen[0][-2:] == ["--force", "--no-baseline"] and "--no-baseline" not in seen[1], seen
    assert verify.main(seen[0][seen[0].index("run"):]) == 0                    # what the detached process does with it
    ev = _verified(p, t)[-1]["detail"]
    assert ev["base_sha"] is None and ev["tests"] == "3 passed, 0 failed" and ev["base_tests"] is None


def test_a_background_run_with_auto_follows_the_rule_of_jobs(root, monkeypatch):
    """After a clean update merge.py verifies the trial in the background: start_background ran a bare test_command (the whole suite,
    on the tip and on the baseline) after every clean update, though jobs never run it, and verify.auto false did not stop it. With
    auto=True (merge.py's call) nothing starts unless auto_on, and the detached run gets --auto: as after a job, only what verify
    names runs (a project with only measures: only its measures)."""
    counter = root.parent / "counter.txt"
    bare = "git rev-parse HEAD >> %s; %s -m pytest -q" % (counter, PY)
    p = _with_config(root, _project(root, {"tests": None, "sandbox": "none"}, test_command=bare))
    t = _trial(p, {"calc.py": CALC + "\n# u\n"})
    seen = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen.append(cmd)
            self.pid = 999999
    with monkeypatch.context() as m:                                            # subprocess itself: undone before anything else runs
        m.setattr(verify.subprocess, "Popen", FakePopen)
        assert verify.start_background(p, t["id"], by="update", auto=True) is None and seen == []        # only test_command: nothing
        assert verify.start_background(_project(root, {"auto": False}), t["id"], by="update", auto=True) is None and seen == []
        assert verify.start_background(p, t["id"], by="cli") == 999999 and "--auto" not in seen[-1]      # by hand: test_command runs
        measures = [{"name": "n", "command": "echo n: 1", "pattern": r"n: (\d+)"}]
        only_measures = _with_config(root, _project(root, {"tests": None, "sandbox": "none", "measures": measures}, test_command=bare))
        assert verify.start_background(only_measures, t["id"], by="update", auto=True) == 999999 and seen[-1][-1] == "--auto"
    assert verify.main(seen[-1][seen[-1].index("run"):]) == 0                  # what the detached process does with it
    ev = _verified(p, t)[-1]
    assert ev["by"] == "update" and ev["detail"]["tests"] is None and ev["detail"]["measures"] == {"n": [1.0, 1.0]}, ev["detail"]
    assert not counter.exists()                                                 # test_command never ran


def test_a_job_verifies_only_what_verify_names_and_a_bare_test_command_waits_for_dtree_verify(root, monkeypatch):
    """ultimateTruth's test_command is its whole suite (over an hour): run on the tip and the baseline after every job, it held each
    job's runner (and a max_parallel_jobs place) for hours. After a job only verify.tests and verify.measures run; test_command is
    what `dtree verify` runs by hand."""
    from test_agents import CLAUDE, _exe                                         # the Claude Code stand-in: it commits g.txt and says built
    monkeypatch.setenv("FAKE_PROMPT", str(root.parent / "prompt.txt"))
    counter = root.parent / "counter.txt"
    bare = "git rev-parse HEAD >> %s; %s -m pytest -q" % (counter, PY)
    agents = {"claude": {"command": _exe(root.parent / "claude", CLAUDE), "sandbox": "none"}}
    p = _project(root, {"tests": None, "sandbox": "none"}, test_command=bare, agents=agents)
    did = p.store.add(Q)["id"]
    p.store.answer(did, "a")
    t = p.store.try_option(did, "b")
    j = jobs.run(p, p.store.job_open(t["id"], agent="claude")["id"])
    assert j["status"] == "measured" and j["result"]["verify"] is None and not counter.exists() and not _verified(p, t)
    assert any("verify: not after jobs with only test_command" in l for l in jobs.log_tail(j))     # the job's log says why
    by_hand = verify.verify_trial(p, t["id"], say=_quiet([]))                   # dtree verify: test_command as it is
    assert by_hand["status"] == "passed" and by_hand["tests"]["passed"] == 2 and _runs(counter, by_hand["sha"]) == 1
    m = _project(root, {"tests": None, "sandbox": "none", "measures": [{"name": "n", "command": "echo n: 1", "pattern": r"n: (\d+)"}]},
                 test_command=bare)
    after = verify.after_job(m, {"id": None}, p.store.trial(t["id"]), _quiet([]))   # measures only: test_command still waits
    assert after["measures"][0]["value"] == 1.0 and after["tests"] is None and _runs(counter, by_hand["sha"]) == 1
    assert not verify.auto_on(p) and verify.auto_on(m) and verify.auto_on(_project(root)) and not verify.auto_on(_project(root, {"auto": False}))


def test_settings_refuse_numbers_that_are_not_finite(root):
    """JSON's NaN, or 1e999 (inf), as a time limit meant no time limit at all; an infinite hub_limit raised OverflowError."""
    for cfg in ({"timeout_s": float("inf")}, {"timeout_s": float("nan")}, {"select": {"hub_limit": float("inf")}},
                {"measures": [{"name": "m", "command": "x", "pattern": "a", "timeout_s": float("nan")}]},
                {"measures": [{"name": "m", "command": "x", "pattern": "a", "tolerance_pct": float("inf")}]}):
        with pytest.raises(DecisionError):
            verify.settings(_project(root, cfg))
