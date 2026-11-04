"""sandbox.py: what a sandboxed command can and cannot do, how it ends, and how its end is told apart from a setup failure."""
import os
import subprocess
import sys
import threading
import time

import pytest

from dtree import sandbox

pytestmark = pytest.mark.skipif(not sandbox.check()[0], reason="bwrap cannot run here: %s" % sandbox.check()[1])
HOME = os.path.expanduser("~")
PY = sys.executable
PY_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(PY))) if PY.startswith(HOME) else None


def _box(tmp_path, **kw):
    mounts = [("rw", str(tmp_path / "wt"))] + ([("ro-try", PY_ROOT.split("/envs/")[0], PY_ROOT.split("/envs/")[0])] if PY_ROOT else [])
    return dict({"mounts": mounts + kw.pop("mounts", [])}, **kw)


@pytest.fixture
def wt(tmp_path):
    (tmp_path / "wt").mkdir()
    return tmp_path / "wt"


def test_ok_failed_and_the_output_are_kept(tmp_path, wt):
    r = sandbox.run("echo hello; echo err >&2", wt, tmp_path / "a.log", 30, sandbox=_box(tmp_path))
    assert r["state"] == "ok" and r["exit_code"] == 0 and "hello" in r["tail"] and "err" in r["tail"]
    assert "hello" in (tmp_path / "a.log").read_text()
    r = sandbox.run("exit 3", wt, tmp_path / "b.log", 30, sandbox=_box(tmp_path))
    assert r["state"] == "failed" and r["exit_code"] == 3


def test_a_setup_failure_is_an_error_not_a_failed_command(tmp_path, wt):
    r = sandbox.run("true", wt, tmp_path / "c.log", 30, sandbox=_box(tmp_path, mounts=[("ro", str(tmp_path), "/proc/no/such/place")]))
    assert r["state"] == "error" and r["error"]


def test_only_the_worktree_is_writable_home_is_empty_and_there_is_no_network(tmp_path, wt):
    outside = tmp_path / "outside"
    outside.mkdir()
    script = ("touch inside.txt; touch %s/x 2>/dev/null && echo WROTE-OUTSIDE; ls -A %s | wc -l; "
              "ls %s/.ssh 2>/dev/null && echo SAW-SSH; "
              "%s -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\" 2>/dev/null && echo NET") % (outside, HOME, HOME, PY)
    r = sandbox.run(script, wt, tmp_path / "d.log", 60, sandbox=_box(tmp_path))
    assert (wt / "inside.txt").exists() and not (outside / "x").exists()
    assert "WROTE-OUTSIDE" not in r["tail"] and "SAW-SSH" not in r["tail"] and "NET" not in r["tail"]


def test_secrets_are_masked_and_sockets_under_run_are_gone(tmp_path, wt):
    secret = wt / "write_token"
    secret.write_text("s3cret")
    r = sandbox.run("cat write_token; echo; ls /run | wc -l", wt, tmp_path / "e.log", 30, sandbox=_box(tmp_path, mounts=[("mask", str(secret))]))
    assert "s3cret" not in r["tail"] and r["tail"].split()[-1] == "0"
    assert secret.read_text() == "s3cret"


def test_a_timeout_ends_every_child_and_returns_quickly(tmp_path, wt):
    marker = "dtree-sandbox-sleeper-%d" % os.getpid()
    t0 = time.time()
    r = sandbox.run("sh -c 'exec -a %s sleep 300' & sleep 300" % marker, wt, tmp_path / "f.log", 2, sandbox=_box(tmp_path), grace_s=2)
    assert r["state"] == "timeout" and time.time() - t0 < 30
    time.sleep(0.5)
    ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    assert marker not in ps


def test_a_stop_ends_it(tmp_path, wt):
    flag = threading.Event()
    threading.Timer(1.0, flag.set).start()
    r = sandbox.run("sleep 300", wt, tmp_path / "g.log", 120, sandbox=_box(tmp_path), stopped=flag.is_set, grace_s=2)
    assert r["state"] == "stopped"


def test_a_big_output_is_capped_with_its_head_and_tail(tmp_path, wt):
    r = sandbox.run("echo FIRST; yes line | head -n 200000; echo LAST", wt, tmp_path / "h.log", 60, sandbox=_box(tmp_path), cap_bytes=100000)
    assert r["head"].startswith("FIRST") and r["tail"].rstrip().endswith("LAST") and r["truncated"]
    assert (tmp_path / "h.log").stat().st_size < 120000


def test_without_a_sandbox_it_runs_directly(tmp_path, wt):
    r = sandbox.run("echo direct", wt, tmp_path / "i.log", 30)
    assert r["state"] == "ok" and "direct" in r["tail"]


def test_the_argv_never_lets_the_network_in_by_default_and_hides_home():
    argv = sandbox.build("/tmp")
    assert "--unshare-net" in argv and "--unshare-pid" in argv and "--disable-userns" in argv and "--clearenv" in argv
    assert argv[argv.index(HOME) - 1] == "--tmpfs"                                      # $HOME is an empty tmpfs
    assert "--unshare-net" not in sandbox.build("/tmp", network="host") and "--disable-userns" not in sandbox.build("/tmp", nested=True)
    with pytest.raises(ValueError):
        sandbox.build("/tmp", root="nothing")


def test_the_tools_on_path_are_shown_and_their_secrets_are_not(tmp_path):
    home = tmp_path / "home"
    for d in ("miniforge3/bin", "miniforge3/lib", ".local/bin", ".local/lib", ".local/share/opencode", ".nvm/v22/bin", ".ssh", "tools"):
        (home / d).mkdir(parents=True)
    path = os.pathsep.join(str(home / d) for d in ("miniforge3/bin", ".local/bin", ".nvm/v22/bin", ".ssh", "tools", "nothing")) + ":/usr/bin"
    got = sandbox.path_tool_dirs(path, str(home))
    assert got == [str(home / "miniforge3"), str(home / ".local/bin"), str(home / ".local/lib"), str(home / ".nvm/v22"), str(home / "tools")]


def test_a_command_finds_the_python_on_path_inside_the_sandbox(tmp_path, wt):
    from dtree.project import Project
    proj = Project(tmp_path, {})
    py = subprocess.run(["sh", "-c", "command -v python3"], capture_output=True, text=True).stdout.strip()
    r = sandbox.run("command -v python3; ls %s/.ssh >/dev/null 2>&1 && echo SAW-SSH" % HOME, wt, tmp_path / "j.log", 30,
                    sandbox={"mounts": sandbox.project_mounts(proj, wt), "network": "none"})
    assert r["state"] in ("ok", "failed") and py in r["tail"] and "SAW-SSH" not in r["tail"]


def test_an_agent_commits_on_its_branch_and_cannot_touch_what_git_on_the_host_reads(tmp_path):
    """Review of 2026-11-01: with the git directory writable, a job could redirect the worktree's .git file, write a hook that git
    runs later on the host, or move the main checkout's HEAD. Only its own branch, the objects and its worktree's admin files are its."""
    from dtree.project import Project
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*a, cwd=repo):
        return subprocess.run(["git", "-C", str(cwd)] + list(a), capture_output=True, text=True, check=True).stdout
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@example.invalid"); git("config", "user.name", "t")
    (repo / "f.txt").write_text("one\n"); git("add", "f.txt"); git("commit", "-q", "-m", "first")
    import shutil as _sh
    _sh.rmtree(repo / ".git" / "hooks", ignore_errors=True)                          # no hooks dir: it must not be creatable either
    wt, other = tmp_path / "wt", tmp_path / "other"
    git("worktree", "add", "-q", "-b", "alt/1-x", str(wt)); git("worktree", "add", "-q", "-b", "alt/2-y", str(other))
    gd = repo / ".git"
    admin = sandbox.worktree_admin(wt)
    proj = Project(repo, {})
    script = "; ".join([
        "echo two > f.txt && git add f.txt && git commit -q -m mine && echo COMMITTED",
        "for p in %s; do (echo x >> \"$p\") 2>/dev/null && echo \"WROTE $p\"; done" % " ".join([
            str(wt / ".git"), admin + "/commondir", admin + "/gitdir", str(gd / "HEAD"), str(gd / "index"), str(gd / "config"),
            str(gd / "worktrees" / "other" / "HEAD")]),
        "mkdir -p %s 2>/dev/null; touch %s/post-commit 2>/dev/null && echo WROTE-HOOK" % (gd / "hooks", gd / "hooks"),
        "touch %s 2>/dev/null && [ -s %s ] && echo WROTE-ATTR; true" % (gd / "info" / "attributes", gd / "info" / "attributes"),
    ])
    r = sandbox.run(script, wt, tmp_path / "k.log", 60, sandbox={"mounts": sandbox.agent_mounts(proj, wt, gd), "network": "none"})
    assert "COMMITTED" in r["tail"], r["tail"]
    assert "WROTE" not in r["tail"].replace("WROTE-ATTR", "").replace("WROTE-HOOK", "") and "WROTE-HOOK" not in r["tail"] and "WROTE-ATTR" not in r["tail"], r["tail"]
    assert git("log", "-1", "--format=%s", "alt/1-x").strip() == "mine" and git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert not any((gd / "hooks").glob("*")) and (gd / "config").read_text().count("[core]") == 1
