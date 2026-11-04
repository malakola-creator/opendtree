"""verify.py, the final review's findings (round 3, 2026-11-02): a waiting trial and a timeout it did not have (F3), the cache and the
interpreter's environment (F4), a pytest inside quotes (F11), a conftest.py or a test helper (F12), no project-wide `git worktree
prune` (F13), the exit code of `dtree verify` (F22), and no second verification of a trial being verified (F23).
Every repository here is made for the test; nothing touches another project."""
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from conftest import ApiClient
from dtree import cli, sandbox, verify
from test_verify import CALC, PY, Q, THREE, _git, _project, _quiet, _trial, _until, _with_config, root  # noqa: F401

HOLD = "i=0; while [ ! -e %s ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i+1)); done"      # until the test lets it go (60 s at most)


def _race(p, spy_name, monkeypatch):
    """What two trials verified in threads need: (got {name: summary}, errors, an event set when a trial starts waiting for the check
    named ``spy_name``, go(name, trial) for a thread)."""
    waiting, real_wait = threading.Event(), verify._wait

    def spy(store, row, *args, **kw):
        if row["name"] == spy_name:
            waiting.set()
        return real_wait(store, row, *args, **kw)
    monkeypatch.setattr(verify, "_wait", spy)
    got, errors = {}, []

    def go(name, t):
        try:
            got[name] = verify.verify_trial(p, t["id"], say=_quiet([]))
        except Exception as exc:                                                  # reported by the caller, not lost in the thread
            errors.append(exc)
    return got, errors, waiting, go


# ------------------------------------------------------------------------------------------------------------------------------ F3
def test_a_wait_that_runs_out_while_the_runner_works_on_is_not_the_shared_check_s_timeout(root, monkeypatch):
    """F3: a runner claims a side's checks at once and runs them in turn, so a baseline measure the race shares stays `running` while
    the trial that claimed it runs its own baseline tests. The other trial waited the measure's time limit + WAIT_GRACE_S from the
    start of its wait, gave up, and took the `timeout` it made up as the measure's baseline (no delta), though the measure passed a
    moment later. A wait now lasts as long as that runner moves on."""
    (root / "slow.py").write_text("S = 1\n")
    (root / "tests" / "test_slow.py").write_text("def test_s():\n    pass\n")
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "slow")
    base = _git(root, "rev-parse", "main").strip()
    release = root.parent / "release"
    cmd = 'case "{tests}" in *slow*) [ "$(git rev-parse HEAD)" = %s ] && { %s; };; esac; %s -m pytest -q {tests}' % (base, HOLD % release, PY)
    p = _project(root, {"tests": cmd, "sandbox": "none", "select": {"always": []},
                        "measures": [{"name": "score", "command": "echo score: 7", "pattern": r"score: (\d+)", "timeout_s": 1}]})
    monkeypatch.setattr(verify, "WAIT_GRACE_S", 1.0)                         # the old wait gave up after 1 + 1 s
    a = _trial(p, {"slow.py": "S = 2\n"}, variant="one")                     # selects tests/test_slow.py: held on the base
    b = _trial(p, {"calc.py": CALC + "\n# b\n"}, variant="two", did=a["decision_id"])
    got, errors, waiting, go = _race(p, "score", monkeypatch)
    ta = threading.Thread(target=go, args=("a", a))
    ta.start()
    assert _until(lambda: any(r["sha"] == base and r["name"] == "score" and r["status"] == "running" for r in p.store.checks()))
    tb = threading.Thread(target=go, args=("b", b))
    tb.start()
    assert waiting.wait(60)                                                   # b waits for the base measure a has claimed
    held = [r for r in p.store.checks(sha=base) if r["name"] == "tests" and r["status"] == "running"]
    assert held and held[0]["log"], held                                      # a's held check says it started (and where it logs)
    time.sleep(3.0)                                                           # longer than the old wait: 1 s limit + 1 s grace
    release.write_text("")                                                    # a's baseline tests end, then a runs the measure
    ta.join(60); tb.join(60)
    assert not errors and set(got) == {"a", "b"}, errors
    ma, mb = got["a"]["measures"][0], got["b"]["measures"][0]
    assert ma["base_status"] == "passed" and ma["base"] == 7.0
    assert (mb["base_status"], mb["base"], mb["delta"]) == ("passed", 7.0, 0.0), mb
    assert mb["base_check"] == ma["base_check"]                               # the same run: b took a's result


def test_a_shared_check_that_timed_out_in_another_trial_s_run_is_run_again(root, monkeypatch):
    """F3: a timeout is never reused (docs/verify.md), but a trial waiting for another trial's run took that run's `timeout` as its
    own baseline. Like `error` and `stopped`, the waiting trial now runs the check itself, once."""
    base = _git(root, "rev-parse", "main").strip()
    flag, release, counter = root.parent / "slow-once", root.parent / "release", root.parent / "measured.txt"
    measure = ('git rev-parse HEAD >> %s; if [ "$(git rev-parse HEAD)" = %s ] && [ ! -e %s ]; then : > %s; %s; sleep 30; fi; echo score: 7'
               % (counter, base, flag, flag, HOLD % release))
    p = _project(root, {"sandbox": "none", "measures": [{"name": "score", "command": measure, "pattern": r"score: (\d+)", "timeout_s": 6}]})
    a = _trial(p, {"calc.py": CALC + "\n# a\n"}, variant="one")
    b = _trial(p, {"calc.py": CALC + "\n# b\n"}, variant="two", did=a["decision_id"])
    got, errors, waiting, go = _race(p, "score", monkeypatch)
    ta = threading.Thread(target=go, args=("a", a))
    ta.start()
    assert _until(lambda: any(r["sha"] == base and r["name"] == "score" and r["status"] == "running" for r in p.store.checks()))
    tb = threading.Thread(target=go, args=("b", b))
    tb.start()
    assert waiting.wait(60)                                                   # b waits for a's run of the base measure
    release.write_text("")                                                    # a's run goes on into `sleep 30`: a timeout at 6 s
    ta.join(60); tb.join(60)
    assert not errors and set(got) == {"a", "b"}, errors
    ma, mb = got["a"]["measures"][0], got["b"]["measures"][0]
    assert ma["base_status"] == "timeout" and ma["base"] is None
    assert (mb["base_status"], mb["base"]) == ("passed", 7.0) and mb["base_check"] != ma["base_check"], mb   # b ran it itself
    assert counter.read_text().split().count(base) == 2


def test_a_wait_for_a_runner_that_does_not_move_ends_soon_as_an_error_not_a_timeout(root, monkeypatch):
    """F3, bounded: a live runner whose checks have not moved for the longest time limit + WAIT_GRACE_S is stuck. The wait ends then,
    saying so, as an `error` (could not verify), never as a `timeout` the check never had."""
    monkeypatch.setattr(verify, "WAIT_GRACE_S", 0.5)
    measure = {"name": "score", "command": "echo score: 7", "pattern": r"score: (\d+)", "timeout_s": 1}
    p = _project(root, {"sandbox": "none", "tests": None, "baseline": False, "timeout_s": 1, "measures": [measure]})
    t = _trial(p, {"calc.py": CALC + "\n# t\n"})
    tip = _git(root, "rev-parse", t["branch"]).strip()
    s = verify.settings(p)
    item = verify._plan(s, None, tip, "tip")[0]
    held = p.store.check_claim(verify._key(tip, item, s, sandbox.project_settings(p)), tip, "score", "measure", item["command"],
                               by="stuck", runner_pid=os.getpid())               # a live dtree runner (this one) that never moves
    assert held["claimed"]
    lines, t0 = [], time.time()
    out = verify.verify_trial(p, t["id"], say=lines.append)
    m = out["measures"][0]
    assert m["status"] == "error" and m["value"] is None and time.time() - t0 < 15, m
    assert "has not moved" in m["summary"] and str(os.getpid()) in m["summary"], m["summary"]
    assert p.store.check(held["id"])["status"] == "running"                   # never the stuck runner's row: it is not this trial's to end


# ------------------------------------------------------------------------------------------------------------------------------ F4
def _env(tmp_path, value):
    """A Python environment of its own (a venv over this interpreter, which brings pytest) with a package ``envdep`` installed."""
    env = tmp_path / "env"
    subprocess.run([PY, "-m", "venv", "--without-pip", "--system-site-packages", str(env)], check=True, capture_output=True)
    site = next((env / "lib").glob("python*/site-packages"))
    (site / "envdep.py").write_text("VALUE = %d\n" % value)
    (site / ("envdep-%d.0.dist-info" % value)).mkdir()
    return env, site


def _upgrade(site, old, new):
    """What `pip install -U envdep` does: the old version's files go (its compiled ones too: they are in its RECORD; a .pyc holds
    its source's mtime to the second only), the new version's come."""
    (site / "envdep.py").unlink()
    for pyc in (site / "__pycache__").glob("envdep.*.pyc"):
        pyc.unlink()
    (site / "envdep.py").write_text("VALUE = %d  # version %d.0\n" % (new, new))
    (site / ("envdep-%d.0.dist-info" % old)).rename(site / ("envdep-%d.0.dist-info" % new))


def test_a_package_upgraded_in_the_interpreter_s_environment_is_not_answered_from_the_cache(root, tmp_path):
    """F4: the key hashed the command and the sandbox settings, never what is behind them: after `pip install -U` in the same
    environment (the same interpreter path, the same command) a baseline cached before the upgrade was reused against a fresh tip, a
    false +1 passed / -1 failed with no warning. The interpreter the command names is now in the key: its binary and its
    site-packages (size and mtime), and the compare row says the earlier verification was made with another installation."""
    env, site = _env(tmp_path, 1)
    (root / "tests" / "test_env.py").write_text("import envdep\n\n\ndef test_env():\n    assert envdep.VALUE == 2\n")
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "a test of the environment")
    p = _project(root, {"tests": str(env / "bin" / "python") + " -m pytest -q {tests}", "sandbox": "none",
                        "select": {"always": ["tests/test_env.py"], "changed": False}})
    x = _trial(p, {"calc.py": CALC + "\n# x\n"}, variant="one")
    first = verify.verify_trial(p, x["id"], say=_quiet([]))
    assert (first["tests"]["failed"], first["base"]["failed"]) == (1, 1), first      # envdep 1: the test fails on both
    assert not verify.compare_fields(p, p.store.trial(x["id"]))["verify"]["settings_changed"]
    _upgrade(site, 1, 2)
    y = _trial(p, {"calc.py": CALC + "\n# y\n"}, variant="two", did=x["decision_id"])
    second = verify.verify_trial(p, y["id"], say=_quiet([]))
    assert not second["base"]["cached"] and (second["base"]["passed"], second["base"]["failed"]) == (1, 0), second["base"]   # envdep 2
    assert second["delta"] == {"passed": 0, "failed": 0, "errors": 0} and second["tests"]["passed"] == 1
    fx, fy = (verify.compare_fields(p, p.store.trial(t["id"]))["verify"] for t in (x, y))
    assert fx["settings_changed"] and fx["env_changed"] and not fy["settings_changed"] and not fy["env_changed"]
    again = verify.verify_trial(p, y["id"], say=_quiet([]))                         # nothing changed since: the cache answers
    assert again["tests"]["cached"] and again["base"]["cached"]


def test_the_environment_print_follows_a_bare_pytest_to_its_interpreter_and_site_packages(tmp_path):
    """What the key stamps: each program the command names (an absolute path, or a bare name found on PATH), the interpreter of a
    script's #! line, and a Python's site-packages; not a module named after -m, not a relative path, not an assignment."""
    env, site = _env(tmp_path, 1)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "pytest"
    script.write_text("#!%s\nprint('a console script')\n" % (env / "bin" / "python"))
    script.chmod(0o755)
    path = str(bindir) + os.pathsep + "/usr/bin:/bin"
    cmd = "bash -o pipefail -c 'PYTHONPATH=src pytest -qq {tests} 2>&1 | tail -n 3'"
    stamp = verify._environment(cmd, path)
    named = [x[0] for x in stamp]
    assert str(script) in named and str(site) in named, stamp
    assert os.path.realpath(PY) in named                                          # the interpreter the venv's python is
    assert not any(x.endswith("/src") or x.endswith("tests}") for x in named)
    assert verify._environment(cmd, path) == stamp                                # the same installation: the same print
    _upgrade(site, 1, 2)
    assert verify._environment(cmd, path) != stamp                                # pip changed site-packages
    mod = verify._environment(str(env / "bin" / "python") + " -m pytest -q tests/test_x.py", "/nowhere")
    assert str(site) in [x[0] for x in mod] and not any(os.path.basename(x[0]) == "pytest" for x in mod), mod   # -m pytest: a module
    assert verify._environment("make check", "/nowhere") == [] and verify._environment("cat score.txt", str(bindir)) == []


# ----------------------------------------------------------------------------------------------------------------------------- F11
def test_a_bare_pytest_inside_quotes_is_pytest_and_counted_from_its_junit_file(root):
    """F11: docs/verify.md recommends `bash -o pipefail -c '…'` for a pipe, but a bare `pytest` after a quote was not recognised:
    no --junitxml, and with -qq nothing printed, so the check `passed` with no counts and no delta."""
    for cmd in ("bash -o pipefail -c 'pytest -qq {tests} 2>&1 | tail -n 3'", 'sh -c "pytest -q {tests}"', "'pytest' -q",
                "bash -c 'cd sub && py.test'", "env X=1 pytest", "python -m pytest -q"):
        assert verify._is_pytest(cmd), cmd
    for cmd in ("cat pytest.ini", "pip install 'pytest-xdist'", "/opt/pytest-venv/bin/python run.py", "echo 'pytest-like'", "make test"):
        assert not verify._is_pytest(cmd), cmd
    path = os.path.dirname(PY) + os.pathsep + os.environ.get("PATH", "")        # this interpreter's own pytest first
    p = _project(root, {"tests": "bash -o pipefail -c 'pytest -qq {tests} 2>&1 | tail -n 3'", "sandbox": "none"},
                 sandbox={"env": {"PATH": path, "PYTHONPATH": "."}})                    # a bare pytest does not put the checkout on sys.path
    t = _trial(p, {"tests/test_calc.py": THREE})
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert (out["tests"]["passed"], out["base"]["passed"]) == (3, 2) and out["delta"]["passed"] == 1, out["tests"]
    assert out["tests"]["framework"] == "pytest" and "PYTEST_ADDOPTS=--junitxml=" in Path(p.store.check(out["tests"]["check"])["log"]).read_text()


# ----------------------------------------------------------------------------------------------------------------------------- F12
BROKEN = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef shared():\n    raise RuntimeError('a shared fixture broke')\n"


def _more_tests(root):
    (root / "tests" / "test_more.py").write_text("def test_m():\n    pass\n")
    (root / "tests" / "unit").mkdir()
    (root / "tests" / "unit" / "test_u.py").write_text("def test_u():\n    pass\n")
    (root / "tests" / "unit" / "test_v.py").write_text("def test_v():\n    pass\n")
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "more tests")


def test_a_changed_conftest_selects_the_tests_of_its_directory_and_below(root):
    """F12: conftest.py is not a test file and nobody imports it by name, so a trial that broke a shared fixture there selected
    nothing and was verified `none`. It now selects the test files of its directory and below."""
    _more_tests(root)
    p = _project(root, {"sandbox": "none", "select": {"always": []}})
    t = _trial(p, {"tests/conftest.py": BROKEN})
    sel = verify.selection(p, "main", t["branch"])
    assert sel["files"] == ["tests/test_calc.py", "tests/test_more.py", "tests/unit/test_u.py", "tests/unit/test_v.py"], sel
    assert sel["reasons"]["tests/unit/test_u.py"] == ["below tests/ ← tests/conftest.py"]
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "failed" and out["tests"]["errors"] == 5 and out["base"]["passed"] == 5, out["tests"]
    assert out["delta"] == {"passed": -5, "failed": 0, "errors": 5}
    below = _trial(p, {"tests/unit/conftest.py": BROKEN})
    assert verify.selection(p, "main", below["branch"])["files"] == ["tests/unit/test_u.py", "tests/unit/test_v.py"]
    top = _trial(p, {"conftest.py": "X = 1\n"})                                  # a conftest at the top: every test
    assert len(verify.selection(p, "main", top["branch"])["files"]) == 4


def test_a_changed_test_helper_selects_the_tests_of_its_directory_and_those_importing_it_by_name(root):
    """F12: a helper beside the tests, imported by its bare name (`from helpers import two`: pytest puts the tests' directory on
    sys.path), was named `tests.helpers` by the import rule and selected nothing. A helper or data file under a tests directory now
    selects that directory's tests; when they are more than hub_limit it is a hub, and the import rule still finds its importers by
    the name they use."""
    _more_tests(root)
    (root / "tests" / "helpers.py").write_text("def two():\n    return 2\n")
    (root / "tests" / "test_h.py").write_text("from helpers import two\n\n\ndef test_two():\n    assert two() == 2\n")
    (root / "tests" / "fixtures").mkdir()
    (root / "tests" / "fixtures" / "vectors.json").write_text("[1, 2]\n")
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "a helper")
    p = _project(root, {"sandbox": "none", "select": {"always": []}})
    t = _trial(p, {"tests/helpers.py": "def two():\n    return 3\n"})
    sel = verify.selection(p, "main", t["branch"])
    assert sel["files"] == ["tests/test_calc.py", "tests/test_h.py", "tests/test_more.py", "tests/unit/test_u.py", "tests/unit/test_v.py"]
    assert sel["reasons"]["tests/test_h.py"] == ["imports helpers", "below tests/ ← tests/helpers.py"], sel["reasons"]
    data = _trial(p, {"tests/fixtures/vectors.json": "[1, 3]\n"})                     # a data file: the tests of the nearest test directory
    assert len(verify.selection(p, "main", data["branch"])["files"]) == 5
    hub = _project(root, {"sandbox": "none", "select": {"always": ["tests/test_more.py"], "hub_limit": 2}})   # hubs: with an always set
    sel = verify.selection(hub, "main", t["branch"])
    assert sel["files"] == ["tests/test_h.py", "tests/test_more.py"] and sel["reasons"]["tests/test_h.py"] == ["imports helpers"], sel
    assert {"module": "tests/helpers.py", "importers": 5, "below": "tests", "files": 1} in sel["hubs"]
    out = verify.verify_trial(hub, t["id"], say=_quiet([]))
    assert out["status"] == "failed" and out["tests"]["failed"] == 1 and out["base"]["passed"] == 2, out["tests"]
    outside = _trial(p, {"src/pkg/helpers.py": "X = 1\n"})                       # not under a tests directory: not a test helper
    assert verify.selection(p, "main", outside["branch"])["files"] == []


# ----------------------------------------------------------------------------------------------------------------------------- F13
def test_a_verification_never_prunes_another_worktree_of_the_project(root):
    """F13: every check began with a project-wide `git worktree prune`, which also dropped a person's worktree whose directory was
    away at that moment (a disk not mounted): its index with the staged work and its registration were gone."""
    p = _project(root, {"sandbox": "none", "baseline": False})
    t = _trial(p, {"tests/test_calc.py": THREE, "tests/conftest.py": "open('.git', 'w').write('gitdir: /nowhere\\n')\n"})
    mnt = root.parent / "mnt"
    feature = mnt / "feature"
    _git(root, "worktree", "add", "-q", "-b", "feature", str(feature), "main")
    (feature / "calc.py").write_text(CALC + "\n# staged, not committed\n")
    _git(feature, "add", "calc.py")
    mnt.rename(root.parent / "mnt-away")                                         # the disk it is on is away for a moment
    try:
        out = verify.verify_trial(p, t["id"], say=_quiet([]))
    finally:
        (root.parent / "mnt-away").rename(mnt)
    assert out["status"] == "passed" and out["tests"]["passed"] == 3, out["tests"]
    st = subprocess.run(["git", "-C", str(feature), "status", "--porcelain"], capture_output=True, text=True)
    assert st.returncode == 0 and "M  calc.py" in st.stdout, st.stderr
    listed = _git(root, "worktree", "list", "--porcelain")
    assert str(feature) in listed and listed.count("worktree ") == 2, listed     # the person's is kept; dtree's own entry is gone
    assert verify.settings(p)["workdir"].exists() and os.listdir(verify.settings(p)["workdir"]) == []


def test_a_path_registered_but_missing_is_added_again_with_force(root, monkeypatch):
    """F13: without the prune, a worktree entry whose directory went (a reboot emptying /dev/shm) still blocks a new worktree at that
    path ("missing but already registered"): dtree adds its own path again with -f."""
    p = _project(root, {"sandbox": "none", "baseline": False})
    t = _trial(p, {"tests/test_calc.py": THREE})
    wd = verify.settings(p)["workdir"]
    wd.mkdir(parents=True)
    stale = wd / "stale"
    _git(root, "worktree", "add", "-q", "--detach", str(stale), "main")
    shutil.rmtree(str(stale))                                                     # still registered, its directory gone
    real = tempfile.mkdtemp

    def mkdtemp(prefix=None, dir=None, **kw):                                    # the fresh name happens to be the stale one
        if prefix and not prefix.startswith("out-") and not os.path.exists(str(stale)):
            os.mkdir(str(stale))
            return str(stale)
        return real(prefix=prefix, dir=dir, **kw)
    monkeypatch.setattr(verify.tempfile, "mkdtemp", mkdtemp)
    out = verify.verify_trial(p, t["id"], say=_quiet([]))
    assert out["status"] == "passed" and out["tests"]["passed"] == 3, out["tests"]
    assert _git(root, "worktree", "list").count("\n") == 1 and not stale.exists()


# ----------------------------------------------------------------------------------------------------------------------------- F22
def test_dtree_verify_exits_3_when_nothing_was_verified_and_1_when_it_is_off(root, capsys):
    """F22: `dtree verify T && dtree merge …` merged a trial nothing had verified: status none exited 0, though the docs promised
    "exit 1 when the tip did not pass". Exit codes now: 0 passed, 1 not passed or refused, 3 nothing run; `--background` with
    verification off is refused (1) like the foreground."""
    p = _with_config(root, _project(root, {"sandbox": "none", "select": {"always": []}}))
    t = _trial(p, {"NOTES.md": "notes\n"})                                       # selects nothing: fallback always, an empty set
    with pytest.raises(SystemExit) as exc:
        cli.main(["--project", str(root), "verify", str(t["id"])])
    printed = capsys.readouterr().out
    assert exc.value.code == 3 and "trial %d: none" % t["id"] in printed, printed
    run = ["run", "--trial", str(t["id"]), "--project", str(root)]
    assert verify.main(run) == 3                                                  # the detached runner says the same
    passed = _trial(p, {"tests/test_calc.py": THREE})
    assert cli.main(["--project", str(root), "verify", str(passed["id"])]) == 0
    _with_config(root, _project(root, {"sandbox": "none", "enabled": False}))
    assert cli.main(["--project", str(root), "verify", str(t["id"]), "--background"]) == 1
    assert "verification is off" in capsys.readouterr().err


# ----------------------------------------------------------------------------------------------------------------------------- F23
def test_a_trial_being_verified_is_not_verified_again_force_or_not(root, monkeypatch, capsys):
    """F23: POST …/verify {force: true} while a verification ran started a second runner: every check ran twice at the same time.
    It is refused like the request without force, from the page and from `dtree verify --background`; a trial with no branch is
    refused at once (the runner failed alone, its reason only in its log)."""
    p = _with_config(root, _project(root, {"sandbox": "none"}))
    t = _trial(p, {"tests/test_calc.py": THREE})
    started = []
    monkeypatch.setattr(verify, "start_background", lambda proj, tid, by="user", force=False, **kw: started.append((tid, force)) or 999999)
    c = ApiClient(p)
    mark = verify._mark(p, t["id"], os.getpid(), "f" * 40)                       # a verification of the trial runs (this process)
    for body in ({}, {"force": True}):
        r = c.post("/api/decisions/trials/%d/verify" % t["id"], json=body)
        assert r.status_code == 400 and "being verified already" in r.get_json()["error"], (body, r.get_json())
    assert cli.main(["--project", str(root), "verify", str(t["id"]), "--background", "--force"]) == 1
    assert "being verified already" in capsys.readouterr().err and started == []
    mark.unlink()
    assert c.post("/api/decisions/trials/%d/verify" % t["id"], json={"force": True}).get_json()["started"] == 999999 and len(started) == 1
    nob = p.store.try_option(p.store.add(Q)["id"], "b")                          # no branch made for it
    r = c.post("/api/decisions/trials/%d/verify" % nob["id"], json={})
    assert r.status_code == 400 and "does not exist" in r.get_json()["error"] and len(started) == 1, r.get_json()
