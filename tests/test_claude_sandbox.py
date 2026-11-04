"""Claude Code in bubblewrap: a stand-in for the claude binary (a script run by the system python, so the sandbox can run it) goes
through the real ClaudeAgent with the sandbox on and reports where it finds itself — what it can write, what ~/.claude is, whether the
credentials are read-only, its environment, the proxy, the network — then commits like Claude Code would. Nothing here spends quota."""
import json
import os
import signal
import socket
import subprocess
import threading
import time

import pytest

from dtree import agents, egress, jobs, sandbox, spend
from dtree.agents import claude as claude_mod
from dtree.project import Project

pytestmark = pytest.mark.skipif(not sandbox.check()[0], reason="bwrap cannot run here: %s" % sandbox.check()[1])
HOME = os.path.expanduser("~")
CREDS = os.path.join(HOME, ".claude", ".credentials.json")

FAKE = r'''#!/usr/bin/python3 -I
import errno, json, os, re, socket, subprocess, sys, time
prompt = sys.stdin.read()
marked = re.search(r"FAKECONF(\{.*?\})FAKEEND", prompt)           # in a job's note when the runner writes the prompt
conf = json.loads(marked.group(1) if marked else prompt.split("\n", 1)[0])
args = sys.argv[1:]
def ev(d):
    print(json.dumps(d), flush=True)
def tried(fn):
    try:
        fn()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
def write(path):
    with open(path, "a") as f:
        f.write("x\n")
def opened(path):
    open(path, "a").close()                  # opened for writing, nothing written
def fstype(path):
    kind = None
    for line in open("/proc/self/mountinfo"):
        left, right = line.split(" - ", 1)
        if left.split()[4] == path:
            kind = right.split()[0]          # the last mount over a path is the one seen
    return kind
def via_proxy(target):
    try:
        s = socket.create_connection(("127.0.0.1", 3128), 5)
        s.sendall(("CONNECT %s HTTP/1.1\r\n\r\n" % target).encode())
        head = s.recv(1000).decode()
        if " 200 " not in head:
            return head.split("\r\n")[0]
        s.sendall(b"hi")
        return s.recv(100).decode()
    except OSError as exc:
        return type(exc).__name__
def direct(port):
    try:
        socket.create_connection(("127.0.0.1", port), 3).close()
        return "connected"
    except OSError as exc:
        return type(exc).__name__
ev({"type": "system", "subtype": "init", "session_id": "fake-1", "model": "fake"})
mode = conf.get("mode", "probe")
if mode == "sleep":
    subprocess.Popen(["/bin/sh", "-c", "exec -a %s sleep 300" % conf["marker"]])
    time.sleep(300)
if mode == "crash":
    print("fake: something broke", file=sys.stderr)
    sys.exit(3)
if mode == "budget":
    cap = float(args[args.index("--max-budget-usd") + 1])
    ev({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "num_turns": 2, "total_cost_usd": cap + 0.05,
        "session_id": "fake-1", "errors": ["Reached maximum budget ($%s)" % cap],
        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 368, "cacheReadInputTokens": 11402, "cacheCreationInputTokens": 7062},
                       "claude-sonnet-5": {"inputTokens": 10, "outputTokens": 2}}})
    sys.exit(1)
if mode == "login":
    ev({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "total_cost_usd": 0, "session_id": "fake-1",
        "result": "Not logged in · Please run /login"})
    sys.exit(1)
if mode == "turns_then_sleep":                 # two API calls, then a long command: a stop comes before any result
    u = {"input_tokens": 3, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 50}
    ev({"type": "assistant", "message": {"id": "msg_a", "content": [{"type": "text", "text": "looking"}], "usage": u}})
    ev({"type": "assistant", "message": {"id": "msg_a", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                                         "usage": dict(u, output_tokens=55)}})          # one event per block, the same call
    ev({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "f.txt"}]}})
    ev({"type": "assistant", "message": {"id": "msg_b", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 300"}}],
                                         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1100, "output_tokens": 70}}})
    subprocess.Popen(["/bin/sh", "-c", "exec -a %s sleep 300" % conf["marker"]])
    try:
        time.sleep(300)
    except KeyboardInterrupt:                  # the runner's SIGINT: Claude Code 2.1.314 then wrote a result of zeros (2026-11-02)
        if conf.get("result_on_interrupt"):
            ev({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 3, "total_cost_usd": 0,
                "session_id": "fake-1", "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                                                  "output_tokens": 0}})
        sys.exit(130)
home = os.environ.get("HOME", "")
dot = os.path.join(home, ".claude")
creds = os.path.join(dot, ".credentials.json")
def read(path):
    try:
        return open(path).read()
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
def own_git():
    """the paths that say where git looks for this worktree's repository: its .git file, its admin dir's commondir / gitdir"""
    admin = open(".git").read().split(":", 1)[1].strip()
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True).stdout.strip()
    return [os.path.join(os.getcwd(), ".git"), os.path.join(admin, "commondir"), os.path.join(admin, "gitdir"),
            os.path.join(common, "config"), os.path.join(common, "hooks", "post-commit")]
attack = list(conf.get("attack", [])) + (own_git() if conf.get("attack_own") else [])
r = {"argv": args, "cwd": os.getcwd(), "home": home,
     "env": {k: os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                                            "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING", "DTREE_JOB", "FAKE_FROM_RUNNER")},
     "parent_env": sorted(k for k in os.environ if k.startswith("CLAUDE") and k != "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
     "dot_fs": fstype(dot), "home_fs": fstype(home),
     "dot_entries": sorted(os.listdir(dot)) if fstype(dot) == "tmpfs" else None,
     "creds_size": os.stat(creds).st_size if os.path.exists(creds) else None,
     "creds_write": tried(lambda: opened(creds)) if os.path.exists(creds) else None,
     "outside": {p: tried(lambda p=p: write(p)) for p in conf.get("outside", [])},
     "git_config": tried(lambda: opened(os.path.join(conf["git_dir"], "config"))),
     "git_hook": tried(lambda: write(os.path.join(conf["git_dir"], "hooks", "pre-commit"))),
     "binary": tried(lambda: opened(os.path.realpath(sys.argv[0]))),
     "proxy_allowed": via_proxy("127.0.0.1:%d" % conf["port"]), "proxy_denied": via_proxy("example.com:443"),
     "direct_echo": direct(conf["port"]), "direct_dtree": direct(5077),
     "key_ok": os.environ.get(conf["key_name"]) == conf["key_value"] if conf.get("key_name") else None,
     "read": {p: read(p) for p in conf.get("read", [])},
     "attack": {p: tried(lambda p=p: write(p)) for p in attack},
     "note": prompt[prompt.rfind("Sandbox:"):] if "Sandbox:" in prompt else None}
with open(os.path.join(conf.get("probe_to") or ".", "probe.json"), "w") as f:
    json.dump(r, f)
with open("g.txt", "w") as f:
    f.write("built in the sandbox\n")
c = subprocess.run("git add g.txt && git commit -q -m 'fake claude commits in the sandbox'", shell=True, capture_output=True, text=True)
ev({"type": "assistant", "message": {"id": "msg_p", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "git commit"}}],
                                     "usage": {"input_tokens": 999, "output_tokens": 999}}})     # the result's numbers win over the turns'
ev({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.0123, "session_id": "fake-1",
    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7},
    "structured_output": {"built": c.returncode == 0, "tests": "fake", "impact": "fake", "note": c.stderr.strip()}})
'''


class Echo:
    """A TCP server on the host's loopback standing for an allowed host."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            c.sendall(b"ECHO:" + c.recv(100))
            c.close()


@pytest.fixture
def setup(repo):
    """A project whose claude is the stand-in, a worktree of a trial branch, an echo server, and a way to run the agent."""
    fake = repo["tmp"] / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    echo = Echo()
    wt = repo["tmp"] / "wt" / "alt-1-x"
    repo["git"]("worktree", "add", "-q", "-b", "alt/1-x", str(wt))
    git_dir = str(repo["path"] / ".git")
    (repo["tmp"] / "outside").mkdir()

    def make(_project=None, **agent):
        cfg = {"name": "t", "worktrees": str(repo["tmp"] / "wt"), "logs": str(repo["tmp"] / "logs"), "db": str(repo["tmp"] / "d.sqlite"),
               "agents": {"claude": dict({"command": str(fake), "network_allow": ["127.0.0.1:%d" % echo.port]}, **agent)}}
        cfg.update(_project or {})
        p = Project(repo["path"], cfg)
        return p, agents.make(p.agent("claude"), p)

    def run(agent, conf=None, stopped=lambda: False, deadline=None, limits=None, pids=None):
        lines = []
        conf = dict({"port": echo.port, "git_dir": git_dir}, **(conf or {}))
        ctx = agents.Context(wt, git_dir, lines.append, (pids.append if pids is not None else lambda pid: None), stopped,
                             deadline or time.time() + 120)
        ctx.job, ctx.limits = 7, limits if limits is not None else spend.limits(agent.project, agent.cfg)
        out = agent.run(json.dumps(conf) + "\nthe task", ctx)
        return out, lines

    yield {"repo": repo, "wt": wt, "git_dir": git_dir, "echo": echo, "make": make, "run": run, "fake": str(fake)}
    echo.srv.close()


def _probe(wt):
    return json.loads((wt / "probe.json").read_text())


def test_claude_runs_in_the_sandbox_writes_only_its_worktree_and_reaches_only_the_allowed_hosts(setup, monkeypatch):
    repo, wt = setup["repo"], setup["wt"]
    escape = os.path.join(HOME, "dtree-escape-probe-%d" % os.getpid())
    main_file = str(repo["path"] / "f.txt")
    outside = str(repo["tmp"] / "outside" / "x")
    monkeypatch.setenv("FAKE_FROM_RUNNER", "must-not-pass")
    p, agent = setup["make"]()
    assert agent.available() == (True, setup["fake"])
    out, lines = setup["run"](agent, {"outside": [escape, main_file, outside, "/usr/dtree-probe"]})
    assert "error" not in out, out
    r = _probe(wt)
    # where it runs: ~/.claude is a private tmpfs with only the credentials in it, read-only; nothing of the parent session's environment
    assert r["cwd"] == str(wt) and r["dot_fs"] == "tmpfs" and r["home_fs"] == "tmpfs"
    if os.path.isfile(CREDS):
        assert r["dot_entries"] == [".credentials.json"] and r["creds_size"] == os.stat(CREDS).st_size and r["creds_write"] == "EROFS"
    assert r["parent_env"] == [] and r["env"]["FAKE_FROM_RUNNER"] is None and r["env"]["DTREE_JOB"] == "7"
    assert r["env"]["HTTPS_PROXY"] == r["env"]["HTTP_PROXY"] == "http://127.0.0.1:%d" % egress.PORT and "127.0.0.1" in r["env"]["NO_PROXY"]
    assert r["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == r["env"]["DISABLE_TELEMETRY"] == r["env"]["DISABLE_ERROR_REPORTING"] == "1"
    # what it can write: the worktree and the commit; not the git config or hooks, the binary, /usr; and nothing outside reaches the host
    assert r["git_config"] == "EROFS" and r["git_hook"] == "EROFS" and r["binary"] == "EROFS" and r["outside"]["/usr/dtree-probe"] == "EROFS"
    assert not os.path.exists(escape) and (repo["path"] / "f.txt").read_text() == "one\n" and not os.path.exists(outside)
    assert not os.path.exists(os.path.join(setup["git_dir"], "hooks", "pre-commit")) and "x\n" not in open(os.path.join(setup["git_dir"], "config")).read()
    assert repo["git"]("log", "-1", "--format=%s", "alt/1-x").strip() == "fake claude commits in the sandbox" and out["built"] is True
    # the network: the allowed host through the proxy, nothing else, and no direct way to the host's loopback (the dtree server)
    assert r["proxy_allowed"] == "ECHO:hi" and r["proxy_denied"] == "HTTP/1.1 403 Forbidden"
    assert r["direct_echo"] == "ConnectionRefusedError" and r["direct_dtree"] == "ConnectionRefusedError"
    assert out["egress"]["allowed"] == {"127.0.0.1:%d" % setup["echo"].port: 1} and out["egress"]["denied"] == {"example.com:443": 1}
    assert any(l.startswith("⛔ egress denied: example.com:443") for l in lines) and any(l.startswith("🔒 sandbox: bwrap · root allowlist · network proxy") for l in lines)
    # the command: no account connectors, the caps and the result's numbers
    assert "--strict-mcp-config" in r["argv"] and "--max-budget-usd" not in r["argv"] and r["argv"][-1] == "Bash(rsync:*)"
    assert out["cost_usd"] == 0.0123 and out["tokens_in"] == 115 and out["tokens_out"] == 7 and out["tokens"] == 122 and out["session"] == "fake-1"


def test_the_proxy_directory_is_gone_after_the_job(setup, monkeypatch):
    made = []
    real = egress.Proxy.start

    def start(self):
        made.append(real(self))
        return made[-1]
    monkeypatch.setattr(egress.Proxy, "start", start)
    p, agent = setup["make"]()
    setup["run"](agent)
    assert made and not os.path.exists(made[0].dir)


def test_without_the_proxy_there_is_no_network_and_with_host_the_host_s_loopback_is_open(setup):
    _, agent = setup["make"](network="none")
    out, lines = setup["run"](agent)
    r = _probe(setup["wt"])
    assert r["env"]["HTTPS_PROXY"] is None and r["proxy_allowed"] == "ConnectionRefusedError" and r["direct_echo"] == "ConnectionRefusedError"
    assert "egress" not in out and out["built"] is True
    _, agent = setup["make"](network="host")
    out, _ = setup["run"](agent)
    r = _probe(setup["wt"])
    assert r["env"]["HTTPS_PROXY"] is None and r["direct_echo"] == "connected" and r["dot_fs"] == "tmpfs"      # still sandboxed, but on the host's network


def test_a_stop_or_the_deadline_ends_the_sandbox_and_every_process_in_it(setup):
    _, agent = setup["make"]()
    for how in ("flag", "page", "deadline"):
        marker = "dtree-claude-sleeper-%d-%s" % (os.getpid(), how)
        flag, pids = threading.Event(), []

        def later():
            time.sleep(1.5)
            flag.set()
            if how == "page":                                                 # what jobs.stop does: SIGTERM to the recorded group (bwrap)
                os.killpg(pids[0], signal.SIGTERM)
        if how != "deadline":
            threading.Thread(target=later, daemon=True).start()
        t0 = time.time()
        out, _ = setup["run"](agent, {"mode": "sleep", "marker": marker}, stopped=flag.is_set, pids=pids,
                              deadline=time.time() + (2 if how == "deadline" else 120))
        assert out["error"] == ("timeout" if how == "deadline" else "stopped") and time.time() - t0 < 30, (how, out)
        time.sleep(0.5)
        assert marker not in subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout, how
        assert pids and pids[-1] is None and pids[0] > 0                         # bwrap was the recorded process, then cleared


def test_the_budget_cap_goes_to_claude_and_its_end_fails_the_job_with_the_numbers(setup):
    _, agent = setup["make"](max_budget_usd=0.25)
    assert agent.describe()["max_budget_usd"] == 0.25
    out, _ = setup["run"](agent, {"mode": "budget"})
    assert out["error"] == "budget: $0.30 of $0.25" and out["cost_usd"] == pytest.approx(0.30)
    assert (out["tokens_in"], out["tokens_out"]) == (2 + 11402 + 7062 + 10, 370)          # modelUsage: usage is zeros when the budget ends it


def test_a_lost_login_and_a_crash_are_told_apart(setup):
    _, agent = setup["make"]()
    out, _ = setup["run"](agent, {"mode": "login"})
    assert out["error"].startswith("the Claude login needs a refresh") and "Not logged in" in out["error"] and "credentials \"rw\"" in out["error"]
    out, _ = setup["run"](agent, {"mode": "crash"})
    assert out["error"] == "Claude Code ended with no result (exit 3): fake: something broke"


def test_a_sandbox_that_cannot_be_set_up_is_an_error_quoting_bwrap(setup, monkeypatch):
    _, agent = setup["make"]()
    real = claude_mod.ClaudeAgent._mounts
    monkeypatch.setattr(claude_mod.ClaudeAgent, "_mounts", lambda self, *a: real(self, *a) + [("ro", str(setup["repo"]["tmp"]), "/proc/no/such/place")])
    out, _ = setup["run"](agent)
    assert out["error"].startswith("the sandbox did not start: bwrap: "), out


def test_credentials_rw_binds_the_real_file_writable_and_the_rest_stays(setup):
    if not os.path.isfile(CREDS):
        pytest.skip("no Claude login on this machine")
    _, agent = setup["make"](credentials="rw")
    ctx = agents.Context(setup["wt"], setup["git_dir"], lambda l: None, lambda pid: None, lambda: False, time.time() + 60)
    mounts = agent._mounts(os.path.realpath(setup["fake"]), ctx, None)
    argv = sandbox.build(setup["wt"], mounts=mounts)
    real = os.path.realpath(CREDS)
    assert ["--bind", real, real] == argv[argv.index(real) - 1:argv.index(real) + 2] and "--ro-bind" != argv[argv.index(real) - 1]
    _, ro = setup["make"]()
    argv = sandbox.build(setup["wt"], mounts=ro._mounts(os.path.realpath(setup["fake"]), ctx, None))
    assert argv[argv.index(real) - 1] == "--ro-bind"


def test_availability_says_why_and_never_falls_back_to_no_sandbox(setup, monkeypatch):
    _, agent = setup["make"]()
    monkeypatch.setattr(sandbox, "check", lambda: (False, "bwrap cannot create namespaces here (exit 1: an AppArmor user-namespace restriction?)"))
    ok, why = agent.available()
    assert not ok and "AppArmor" in why and "sandbox \"none\"" in why
    _, none = setup["make"](sandbox="none")
    assert none.available()[0] and none.describe()["sandbox"] == "none" and none.describe()["network"] == "host"
    monkeypatch.undo()
    for bad, word in ((dict(network="wide"), "network"), (dict(credentials="yes"), "credentials"), (dict(network_allow=["a b"]), "network_allow"),
                      (dict(sandbox="docker"), "sandbox"), (dict(pass_env=["NOT A NAME"]), "pass_env")):
        ok, why = setup["make"](**bad)[1].available()
        assert not ok and word in why, (bad, why)


def test_describe_and_the_agents_list_show_the_sandbox_network_and_caps(setup):
    p, agent = setup["make"](max_budget_usd=2)
    d = agent.describe()
    assert d["sandbox"] == "bwrap" and d["network"] == "proxy" and d["credentials"] == "ro" and d["max_budget_usd"] == 2
    assert d["network_allow"][:2] == list(egress.CLAUDE_HOSTS) and d["network_allow"][2] == "127.0.0.1:%d" % setup["echo"].port
    rows = {a["name"]: a for a in jobs.agent_list(p)}
    assert rows["claude"]["sandbox"] == "bwrap" and rows["claude"]["network"] == "proxy"
    assert rows["codex"]["sandbox"] == "codex workspace-write" and rows["codex"]["network"] == "none"
    assert rows["local"]["prices"] == {"in": 0.0, "out": 0.0} and rows["muse"]["sandbox"] == "bwrap" and rows["muse"]["max_tokens_total"] is None


def test_login_variables_reach_claude_through_a_private_file_never_a_command_line(setup, monkeypatch):
    """Review of 2026-11-01: ``--setenv NAME VALUE`` put ANTHROPIC_API_KEY on bwrap's command line, which /proc/<pid>/cmdline shows every
    local user for the whole job. The value goes through a 0600 file in a 0700 directory, bound read-only and read by the sandbox's shell;
    only the names are logged, and the file is gone after the job."""
    secret = "dummy-%d 'quoted' \"dq\" $HOME `id` \\ end\nsecond line" % os.getpid()
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    started, files = [], []
    real = claude_mod.subprocess.Popen

    def popen(argv, *a, **k):
        argv = list(argv) if isinstance(argv, (list, tuple)) else [argv]
        started.append(argv)
        for x in map(str, argv):                                              # the private file as it is when the sandbox starts
            d = os.path.dirname(x)
            if os.path.basename(d).startswith("dtree-job-") and os.path.isfile(x):
                files.append((x, os.stat(x).st_mode & 0o777, os.stat(d).st_mode & 0o777))
        return real(argv, *a, **k)
    monkeypatch.setattr(claude_mod.subprocess, "Popen", popen)
    for network in ("proxy", "none"):                                           # with and without the forwarder's wrapper
        del started[:], files[:]
        _, agent = setup["make"](network=network)
        out, lines = setup["run"](agent, {"key_name": "ANTHROPIC_API_KEY", "key_value": secret})
        assert "error" not in out, (network, out)
        assert _probe(setup["wt"])["key_ok"] is True, network                   # Claude Code has it, every character intact
        assert any(str(a[0]).endswith("bwrap") for a in started)
        assert not any("dummy-%d" % os.getpid() in str(x) for a in started for x in a), network   # on no command line at all
        assert len(files) == 1 and files[0][1:] == (0o600, 0o700), files
        assert not os.path.exists(files[0][0]) and not os.path.exists(os.path.dirname(files[0][0]))
        assert any("ANTHROPIC_API_KEY" in l for l in lines if l.startswith("🔒")) and not any("dummy-" in l for l in lines)


def test_a_stopped_job_still_counts_the_tokens_of_its_turns(setup):
    """Review of 2026-11-01: tokens (and so an estimated cost) came only from Claude Code's result, which a hard stop prevents, so the
    month's budget counted such a job as free. Each assistant event carries its API call's usage (one event per content block, the same
    call repeated), so the calls are counted once each."""
    p, agent = setup["make"](price_in_per_mtok=1.0, price_out_per_mtok=5.0)
    marker = "dtree-claude-turns-%d" % os.getpid()
    flag = threading.Event()
    threading.Timer(2.0, flag.set).start()
    out, lines = setup["run"](agent, {"mode": "turns_then_sleep", "marker": marker}, stopped=flag.is_set)
    assert out["error"] == "stopped" and out["cost_usd"] is None, out
    assert (out["tokens_in"], out["tokens_out"], out["tokens"]) == (1103 + 1102, 55 + 70, 2330)
    priced = spend.after_job(p, agent.cfg, out)
    assert priced["cost_estimated"] is True and priced["cost_usd"] == pytest.approx((2205 * 1.0 + 125 * 5.0) / 1e6)
    assert any("2,330 tokens" in l for l in lines), lines
    time.sleep(0.5)
    assert marker not in subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout


def test_a_result_of_zeros_after_a_stop_does_not_make_the_job_free(setup):
    """A real stop of 2026-11-02 (Claude Code 2.1.314, haiku): on the runner's SIGINT Claude Code wrote a result with total_cost_usd 0
    and all-zero usage after an API call of 18,678 input tokens. The calls' tokens count, and the dollars are unknown (an estimate
    from the agent's prices when it has them), not a reported $0."""
    p, agent = setup["make"](price_in_per_mtok=1.0, price_out_per_mtok=5.0)
    costs = []
    flag = threading.Event()
    threading.Timer(2.0, flag.set).start()
    ctx_cost = {"marker": "dtree-claude-zeros-%d" % os.getpid(), "result_on_interrupt": True, "mode": "turns_then_sleep"}
    lines = []
    ctx = agents.Context(setup["wt"], setup["git_dir"], lines.append, lambda pid: None, flag.is_set, time.time() + 120, costs.append)
    ctx.job, ctx.limits = 7, spend.limits(p, agent.cfg)
    out = agent.run(json.dumps(dict({"port": setup["echo"].port, "git_dir": setup["git_dir"]}, **ctx_cost)) + "\nthe task", ctx)
    assert out["error"] == "stopped" and out["turns"] == 3 and (out["tokens_in"], out["tokens_out"]) == (2205, 125), out
    assert out["cost_usd"] is None and costs == [], (out, costs)                  # unknown, not $0 reported
    priced = spend.after_job(p, agent.cfg, out)
    assert priced["cost_estimated"] is True and priced["cost_usd"] == pytest.approx((2205 * 1.0 + 125 * 5.0) / 1e6)
    assert any("its result had none" in l for l in lines), lines


def test_the_mount_points_of_ro_binds_and_tmpfs_leave_nothing_in_the_worktree(setup):
    """Review of 2026-11-01: bwrap makes an empty directory in the worktree for each bind it puts there, and project_mounts binds only where
    the worktree has nothing, so in a kept worktree the next job saw an empty data/ instead of the shared data."""
    repo, wt = setup["repo"], setup["wt"]
    (repo["path"] / "data" / "models").mkdir(parents=True)
    (repo["path"] / "data" / "models" / "model.bin").write_text("weights\n")
    _, agent = setup["make"](_project={"sandbox": {"ro_binds": ["data/models"], "tmpfs": ["build/cache"]}})
    for n in (1, 2):
        setup["run"](agent, {"read": ["data/models/model.bin"]})
        assert _probe(wt)["read"]["data/models/model.bin"] == "weights\n", n
        assert not (wt / "data").exists() and not (wt / "build").exists(), (n, sorted(os.listdir(wt)))


def test_a_binary_the_sandbox_cannot_start_is_named_in_the_reason_not_the_forwarder(setup):
    """Review of 2026-11-01: the shell's $0 was the forwarder's path, so this read '…/fwd.py: 1: exec: …/claude: not found'."""
    bad = setup["repo"]["tmp"] / "claude-elsewhere"
    bad.write_text("#!/nonexistent/interpreter\n")
    bad.chmod(0o755)
    _, agent = setup["make"](command=str(bad))
    out, _ = setup["run"](agent)
    assert "fwd.py" not in out["error"] and "dtree-sandbox" in out["error"] and os.path.realpath(str(bad)) in out["error"], out["error"]


def test_the_sandbox_note_in_the_prompt_says_what_the_job_can_see(setup):
    """Review of 2026-11-01: the note did not say that the main checkout is not visible, while the prompt told the agent to read git-ignored
    data from it. The note now says what each root shows, as jobs.prompt_for does."""
    _, agent = setup["make"]()
    setup["run"](agent)
    note = _probe(setup["wt"])["note"]
    assert "main checkout is not visible" in note and "sandbox.ro_binds" in note and "home directory" in note, note
    _, host = setup["make"](_project={"sandbox": {"root": "host"}})
    setup["run"](host)
    note = _probe(setup["wt"])["note"]
    assert "main checkout is not visible" not in note and "the rest of the filesystem" in note and "read-only" in note, note


Q = {"kind": "decision", "source": "claude", "key": "way", "title_th": "ทางเดิน", "title_en": "The way", "question_th": "ทางไหน", "question_en": "Which way?",
     "options": [{"key": "left", "th": "ซ้าย", "en": "Left", "recommended": True}, {"key": "right", "th": "ขวา", "en": "Right"}]}


def test_through_the_runner_claude_works_in_its_shadow_and_cannot_touch_the_project_s_git(setup):
    """The review's write probe (2026-11-01) against the shadow repositories, through the real runner: the job's worktree belongs to the
    trial's own repository; the project's .git (HEAD, index, the base branch, config, hooks, info, packed-refs) is read-only, and so are
    what tells git where to look (the worktree's .git file, its admin commondir / gitdir) and the shadow's config and hooks. The commit
    comes back through jobs._sync_back, and the proxy's counts are kept in the job's result."""
    repo = setup["repo"]
    gd = repo["path"] / ".git"
    out_dir = repo["tmp"] / "probe-out"
    out_dir.mkdir()
    before = {n: (gd / n).read_bytes() for n in ("HEAD", "config", "refs/heads/main")}
    p, _ = setup["make"](rw_paths=[str(out_dir)], _project={"verify": {"enabled": False}})
    did = p.store.add(Q)["id"]
    p.store.answer(did, "left")
    t = p.store.try_option(did, "right")
    attack = [str(gd / x) for x in ("HEAD", "index", "config", "refs/heads/main", "refs/heads/evil", "packed-refs", "hooks/post-commit",
                                    "info/attributes", "description")]
    conf = {"port": setup["echo"].port, "git_dir": str(gd), "probe_to": str(out_dir), "attack": attack, "attack_own": True}
    job = jobs.run(p, p.store.job_open(t["id"], "FAKECONF%sFAKEEND" % json.dumps(conf), agent="claude")["id"])
    r = json.loads((out_dir / "probe.json").read_text())
    assert job["status"] == "measured", job["reason"]
    assert len(r["attack"]) == len(attack) + 5 and all(v != "ok" for v in r["attack"].values()), r["attack"]
    assert repo["git"]("log", "-1", "--format=%s", t["branch"]).strip() == "fake claude commits in the sandbox"
    assert {n: (gd / n).read_bytes() for n in before} == before
    assert not any((gd / x).exists() for x in ("refs/heads/evil", "packed-refs", "hooks/post-commit", "info/attributes"))
    assert job["result"]["egress"] == {"allowed": {"127.0.0.1:%d" % setup["echo"].port: 1}, "denied": {"example.com:443": 1}, "failed": {}}
    assert job["result"]["cost_usd"] == 0.0123 and job["result"]["tokens"] == 122


def test_the_binary_s_install_is_what_the_sandbox_shows(tmp_path):
    ext = tmp_path / ".vscode" / "extensions" / "anthropic.claude-code-2.1.314-linux-x64" / "resources" / "native-binary"
    ext.mkdir(parents=True)
    (ext / "claude").write_text("")
    link = tmp_path / "claude-link"
    link.symlink_to(ext / "claude")
    assert claude_mod.install_dir(str(link)) == str(tmp_path / ".vscode" / "extensions" / "anthropic.claude-code-2.1.314-linux-x64")
    npm = tmp_path / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
    npm.mkdir(parents=True)
    (npm / "cli.js").write_text("")
    assert claude_mod.install_dir(str(npm / "cli.js")) == str(npm)
    (tmp_path / "solo").write_text("")
    assert claude_mod.install_dir(str(tmp_path / "solo")) == str(tmp_path / "solo")
