"""Bubblewrap: one way to run something with only what it needs, shared by the chat loop's ``run`` tool, verification (a project's
tests and measures, verify.py) and Claude Code jobs (agents/claude.py).

The rules come from experiments on this machine (2026-11-01, bubblewrap 0.9.0, git 2.43):

- **root** ``allowlist`` (default): an empty root with /usr (and the merged-/usr links), /etc, /opt, /sys, /dev, /proc read-only, a
  private /tmp, /run and /var/tmp, and an empty $HOME — so ~/.ssh, ~/.claude, other projects and the sockets under $HOME and /run
  (docker, D-Bus, the ssh agent: reachable even without a network) are not there. Whatever else is needed is given by ``mounts``.
  ``host``: the whole filesystem read-only, /run and the known secret places under $HOME hidden.
- **namespaces**: always a new pid namespace (a timeout or a stop takes every child with it, and nothing inside can signal the user's
  processes), ipc, uts, cgroup; no network unless ``network="host"``; ``--disable-userns`` unless the project's commands nest
  sandboxes themselves; ``--die-with-parent`` (bound to the thread that starts it: start and wait in the same thread) and
  ``--new-session``.
- **environment**: cleared, then a short list plus caches sent to /tmp and the caller's additions.
- **status**: ``--json-status-fd`` tells a bwrap setup failure (no exit-code record) from a command that failed (exit 1 alone cannot).
- **output**: read by a thread into a capped log file, keeping its head and its last 8 KB (an unread pipe deadlocks; communicate()
  holds it all in memory).
- **stop**: SIGINT to the command's process group (pytest still writes its summary and JUnit file), then SIGKILL to bwrap.

``mounts`` is an ordered list applied after the root: ``("rw", path)``, ``("ro", src, dst)``, ``("ro-try", src, dst)``,
``("tmpfs", path)``, ``("tmpfs-size", path, bytes)``, ``("mask", path)`` (a file becomes /dev/null, a directory an empty tmpfs; only
when it exists, since a missing target fails the setup), ``("dev", path)`` (a device, for a GPU).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .gitcode import read_file

BWRAP = shutil.which("bwrap") or "/usr/bin/bwrap"
TAIL_BYTES = 8192
HEAD_BYTES = 4096
LOG_CAP_BYTES = 50 * 1024 * 1024
TMP_SIZE = 8 * 1024 ** 3

#: caches a run would write under $HOME, sent to the sandbox's private /tmp
CACHE_ENV = {"XDG_CACHE_HOME": "/tmp/.cache", "MPLCONFIGDIR": "/tmp/.mpl", "NUMBA_CACHE_DIR": "/tmp/.numba", "PYTHONDONTWRITEBYTECODE": "1",
             "TMPDIR": "/tmp", "TERM": "dumb"}
#: kept from the caller's environment when it is cleared
KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ")
#: places under $HOME a ``host`` root hides (when they exist): keys, agent configs, cloud credentials
HOME_SECRETS = (".ssh", ".gnupg", ".claude", ".claude.json", ".codex", ".config/ut-meta", ".aws", ".azure", ".kube", ".docker", ".netrc",
                ".git-credentials", ".config/gh", ".pypirc", ".npmrc", ".password-store", ".local/share/keyrings")
GPU_DEVICES = ("/dev/dri", "/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-modeset", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools")

Mount = Tuple[Any, ...]


def available() -> bool:
    return os.access(BWRAP, os.X_OK)


_CHECKED: Dict[str, Tuple[bool, str]] = {}


def check() -> Tuple[bool, str]:
    """Whether bwrap can make the namespaces here. It cannot when, for example, dtree runs from an 'unconfined' AppArmor context (a
    systemd unit) on a kernel that restricts unprivileged user namespaces; the remedies (an AppArmor profile for bwrap, or the sysctl)
    need root and are the user's choice. Cached for the process."""
    if "r" in _CHECKED:
        return _CHECKED["r"]
    if not available():
        res = (False, "bubblewrap (bwrap) is not installed")
    else:
        try:
            r = subprocess.run([BWRAP, "--unshare-user", "--unshare-pid", "--unshare-net", "--ro-bind", "/", "/", "/bin/true"],
                               capture_output=True, text=True, timeout=20)
            res = (True, BWRAP) if r.returncode == 0 else (False, "bwrap cannot create namespaces here (exit %d: %s)" % (
                r.returncode, (r.stderr or "").strip()[:300] or "an AppArmor user-namespace restriction?"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            res = (False, "bwrap did not run: %s" % exc)
    _CHECKED["r"] = res
    return res


def _root_allowlist(home: str, tmp_size: Optional[int]) -> List[str]:
    out = ["--ro-bind", "/usr", "/usr"]
    for d in ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32"):
        if os.path.islink(d):
            out += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            out += ["--ro-bind", d, d]
    out += ["--ro-bind", "/etc", "/etc", "--ro-bind-try", "/opt", "/opt", "--ro-bind", "/sys", "/sys", "--dev", "/dev", "--proc", "/proc"]
    out += (["--size", str(tmp_size)] if tmp_size else []) + ["--tmpfs", "/tmp"]
    out += ["--tmpfs", "/run", "--tmpfs", "/var/tmp", "--ro-bind-try", "/var/cache/fontconfig", "/var/cache/fontconfig", "--tmpfs", home,
            "--ro-bind-try", os.path.join(home, ".gitconfig"), os.path.join(home, ".gitconfig")]
    return out


def _root_host(home: str, tmp_size: Optional[int]) -> List[str]:
    out = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"] + (["--size", str(tmp_size)] if tmp_size else []) + ["--tmpfs", "/tmp", "--tmpfs", "/run"]
    for rel in HOME_SECRETS:
        out += _mask(os.path.join(home, rel))
    return out


def _mask(path: str) -> List[str]:
    if os.path.isdir(path):
        return ["--tmpfs", path]
    if os.path.lexists(path):
        return ["--ro-bind", "/dev/null", path]
    return []


def build(cwd: Union[str, Path], mounts: Iterable[Mount] = (), network: str = "none", root: str = "allowlist", nested: bool = False,
          env: Optional[Dict[str, str]] = None, status_fd: Optional[int] = None, home: Optional[str] = None,
          tmp_size: Optional[int] = TMP_SIZE) -> List[str]:
    """The bwrap argv prefix (the command follows it). See the module's docstring for ``root``, ``network``, ``mounts``."""
    if root not in ("allowlist", "host"):
        raise ValueError("root is allowlist or host")
    if network not in ("none", "host"):
        raise ValueError("network is none or host")
    home = home or os.path.expanduser("~")
    out = [BWRAP, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
    if network == "none":
        out.append("--unshare-net")
    if not nested:
        out.append("--disable-userns")
    out += ["--die-with-parent", "--new-session"]
    if status_fd is not None:
        out += ["--json-status-fd", str(status_fd)]
    out += _root_allowlist(home, tmp_size) if root == "allowlist" else _root_host(home, tmp_size)
    for m in mounts:
        kind = m[0]
        if kind == "rw":
            p = str(Path(m[1]).resolve())
            if os.path.exists(p):
                out += ["--bind", p, p]
        elif kind in ("ro", "ro-try"):
            src, dst = str(Path(m[1]).resolve()), str(m[2] if len(m) > 2 else m[1])
            if os.path.lexists(src):
                out += ["--ro-bind" if kind == "ro" else "--ro-bind-try", src, dst]
        elif kind == "tmpfs":
            out += ["--tmpfs", str(m[1])]
        elif kind == "tmpfs-size":
            out += ["--size", str(int(m[2])), "--tmpfs", str(m[1])]
        elif kind == "mask":
            out += _mask(str(m[1]))
        elif kind == "dev":
            if os.path.exists(str(m[1])):
                out += ["--dev-bind", str(m[1]), str(m[1])]
        elif kind == "empty-ro":                                     # an empty read-only directory (bwrap makes the mount point)
            out += ["--tmpfs", str(m[1]), "--remount-ro", str(m[1])]
        else:
            raise ValueError("unknown mount %r" % (kind,))
    out.append("--clearenv")
    merged: Dict[str, str] = {k: os.environ[k] for k in KEEP_ENV if os.environ.get(k)}
    merged["HOME"] = home
    merged.update(CACHE_ENV)
    merged.update({str(k): str(v) for k, v in (env or {}).items()})
    for k, v in merged.items():
        out += ["--setenv", k, v]
    out += ["--chdir", str(Path(cwd).resolve())]
    return out


def gpu_mounts() -> List[Mount]:
    """The GPU's device nodes, for a project that opts in (rendering tests); off by default."""
    return [("dev", d) for d in GPU_DEVICES if os.path.exists(d)]


# ------------------------------------------------------------------------------------------------------------------------------ running
class _Capture(threading.Thread):
    """Copies a pipe into a log file up to a cap, keeping the head and the last bytes in memory."""

    def __init__(self, pipe: Any, log: Union[str, Path], cap: int) -> None:
        super().__init__(daemon=True)
        self.pipe, self.log, self.cap = pipe, str(log), cap
        self.head, self.tail_buf, self.size = bytearray(), bytearray(), 0

    def run(self) -> None:
        with open(self.log, "ab") as f:
            while True:
                chunk = self.pipe.read1(65536) if hasattr(self.pipe, "read1") else self.pipe.read(65536)
                if not chunk:
                    break
                if self.size < self.cap:
                    f.write(chunk[: self.cap - self.size])
                    if self.size + len(chunk) > self.cap:
                        f.write(b"\n...[the rest is not kept: more than %d bytes]\n" % self.cap)
                    f.flush()
                if len(self.head) < HEAD_BYTES:
                    self.head += chunk[: HEAD_BYTES - len(self.head)]
                self.tail_buf = (self.tail_buf + chunk)[-TAIL_BYTES:]
                self.size += len(chunk)


class _Status(threading.Thread):
    """Reads bwrap's --json-status-fd: {"child-pid": N}, then {"exit-code": N} only if the command ran."""

    def __init__(self, fd: int) -> None:
        super().__init__(daemon=True)
        self.fd, self.child_pid, self.exit_code = fd, None, None

    def run(self) -> None:
        with os.fdopen(self.fd, "rb") as f:
            for line in f:
                try:
                    rec = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if "child-pid" in rec:
                    self.child_pid = int(rec["child-pid"])
                if "exit-code" in rec:
                    self.exit_code = int(rec["exit-code"])


def run(command: Union[str, Sequence[str]], cwd: Union[str, Path], log: Union[str, Path], timeout_s: float,
        sandbox: Optional[Dict[str, Any]] = None, stopped: Callable[[], bool] = lambda: False,
        on_start: Callable[[int], None] = lambda pid: None, cap_bytes: int = LOG_CAP_BYTES, grace_s: float = 15.0) -> Dict[str, Any]:
    """Run ``command`` (a shell string, or an argv list) in ``cwd``, its output (stdout and stderr) to ``log``.

    ``sandbox``: the keyword arguments of ``build`` (``mounts``, ``network``, ``root``, ``nested``, ``env`` …); None runs it directly
    (with ``env`` taken from nothing but this process). Start and wait happen in the calling thread.

    Returns {state: ok | failed | timeout | stopped | error, exit_code, error, seconds, head, tail, truncated, bytes}: ``ok`` exit 0,
    ``failed`` another exit code, ``error`` the sandbox could not be set up (``error`` quotes bwrap)."""
    cmd = ["/bin/sh", "-c", command] if isinstance(command, str) else list(command)
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    r_fd = w_fd = None
    if sandbox is not None:
        r_fd, w_fd = os.pipe()
        argv = build(cwd, status_fd=w_fd, **sandbox) + cmd
        env = {"PATH": "/usr/bin:/bin"}
    else:
        argv, env = cmd, None
    t0 = time.time()
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, pass_fds=(w_fd,) if w_fd is not None else ())
    if w_fd is not None:
        os.close(w_fd)
    status = _Status(r_fd) if r_fd is not None else None
    if status:
        status.start()
    cap = _Capture(proc.stdout, log, cap_bytes)
    cap.start()
    on_start(proc.pid)
    why = None
    while proc.poll() is None:
        if time.time() - t0 > timeout_s:
            why = "timeout"
        elif stopped():
            why = "stopped"
        if why:
            interrupt(proc, status.child_pid if status else None, grace_s)
            break
        time.sleep(0.25)
    code = proc.wait()
    cap.join(timeout=10)
    if status:
        status.join(timeout=5)
    head, tail = bytes(cap.head).decode("utf-8", "replace"), bytes(cap.tail_buf).decode("utf-8", "replace")
    out: Dict[str, Any] = {"seconds": round(time.time() - t0, 1), "head": head, "tail": tail, "truncated": cap.size > len(cap.tail_buf),
                           "bytes": cap.size, "error": None}
    if status is not None and status.exit_code is None and why is None:
        first = next((l for l in (head + "\n" + tail).splitlines() if l.startswith("bwrap: ")), "")
        out.update(state="error", exit_code=code, error=first or "the sandbox did not start (exit %s)" % code)
    else:
        exit_code = status.exit_code if status is not None and status.exit_code is not None else code
        out.update(state=why or ("ok" if exit_code == 0 else "failed"), exit_code=exit_code)
    return out


def interrupt(proc: "subprocess.Popen[bytes]", child_pid: Optional[int], grace_s: float = 15.0) -> None:
    """SIGINT the command's process group (in a sandbox: the group bwrap's --new-session made, whose host id is the child pid), let it
    write what it can, then SIGKILL — bwrap's group, which takes the pid namespace and everything in it."""
    for target in ([child_pid] if child_pid else []) + [proc.pid]:
        try:
            os.killpg(target, signal.SIGINT)
            break
        except OSError:
            continue
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    end_group(proc.pid, grace_s=0)


def end_group(pid: int, grace_s: float = 5.0) -> None:
    """SIGTERM the process group (SIGKILL straight away when ``grace_s`` is 0), then SIGKILL what is left after ``grace_s``."""
    try:
        os.killpg(pid, signal.SIGTERM if grace_s else signal.SIGKILL)
    except OSError:
        return
    t0 = time.time()
    while time.time() - t0 < grace_s:
        try:
            os.killpg(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def tail(path: Union[str, Path], size: int = TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ------------------------------------------------------------------------------------------------------------------- the project's settings
def project_settings(project: Any) -> Dict[str, Any]:
    """``sandbox`` of the project's config with dtree's defaults."""
    s = dict(project.config.get("sandbox") or {})
    s.setdefault("root", "allowlist")
    s.setdefault("nested", False)
    s.setdefault("gpu", False)
    s.setdefault("path_tools", True)
    for k in ("ro_binds", "tmpfs", "secrets", "extra_ro", "copies"):
        s.setdefault(k, [])
    s.setdefault("env", {})
    return s


def project_mounts(project: Any, worktree: Union[str, Path], writable: bool = True) -> List[Mount]:
    """The mounts a project's sandboxed command gets beyond the root:

    - ``extra_ro``: absolute paths shown read-only (an interpreter's installation under $HOME, a toolchain), those the project's
      commands name (``auto_extra_ro``) and, with ``path_tools`` (default on), the installations behind PATH (``path_tool_dirs``);
    - the repository's git directory, read-only (git works in the worktree; a run never writes refs) — callers that must commit add it
      read-write themselves;
    - ``ro_binds``: git-ignored paths of the main checkout shown read-only at the same place in the worktree, only where the worktree has
      nothing (never over the commit's own files);
    - ``tmpfs``: paths in the worktree given an empty writable RAM directory (what tests write and nobody keeps: data/, build/);
    - ``secrets``: files or directories of the main checkout hidden even where the main checkout is visible (plus .dtree/write_token);
    - the worktree itself, writable when ``writable``;
    - the GPU when ``gpu`` is on."""
    s = project_settings(project)
    wt = Path(worktree).resolve()
    out: List[Mount] = []
    shown = [os.path.expanduser(str(p)) for p in list(s["extra_ro"]) + auto_extra_ro(project)]
    if s.get("path_tools", True) and s["root"] == "allowlist":
        shown += [d for d in path_tool_dirs() if d not in shown]
    home = os.path.expanduser("~")
    for p in shown:
        out.append(("ro-try", p, p))
    for rel in HOME_SECRETS:                                         # a secret inside something shown stays hidden
        secret = os.path.join(home, rel)
        if any(secret == p or secret.startswith(p.rstrip("/") + "/") for p in shown):
            out.append(("mask", secret))
    git_dirs = []
    for cwd in ([wt] if wt.exists() else []) + [project.root]:        # the worktree's (a trial's shadow) and the project's (its objects)
        d = project.git.run("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).stdout.strip()
        if d and d not in git_dirs:
            git_dirs.append(d)
    for d in git_dirs:
        out.append(("ro", d, d))
    if writable:
        out.append(("rw", str(wt)))
    else:
        out.append(("ro", str(wt), str(wt)))
    for rel in s["ro_binds"]:
        rel = str(rel).strip("/")
        if rel and ".." not in Path(rel).parts and (project.root / rel).exists() and not os.path.lexists(wt / rel):
            out.append(("ro", str(project.root / rel), str(wt / rel)))
    for rel in s["tmpfs"]:
        rel = str(rel).strip("/")
        if rel and ".." not in Path(rel).parts:
            out.append(("tmpfs", str(wt / rel)))
    for rel in list(s["secrets"]) + [".dtree/write_token"]:
        for base in (project.root, wt):
            out.append(("mask", str(base / str(rel).strip("/"))))
    if s["gpu"]:
        out += gpu_mounts()
    return out


def project_binds(project: Any, worktree: Union[str, Path]) -> List[Tuple[str, str]]:
    """``ro_binds`` as (source, destination) pairs (what the chat loop's commands see of the main checkout)."""
    return [(m[1], m[2]) for m in project_mounts(project, worktree) if m[0] == "ro" and m[2].startswith(str(Path(worktree).resolve()) + os.sep)]


def project_env(project: Any) -> Dict[str, str]:
    """``sandbox.env`` of the project (added after dtree's own variables)."""
    return {str(k): str(v) for k, v in (project_settings(project).get("env") or {}).items()}


def cleanup_mount_points(worktree: Union[str, Path], before: Iterable[str]) -> List[str]:
    """Remove the empty directories and 0-byte files bwrap made as mount points in a worktree that had nothing there before."""
    removed = []
    wt = Path(worktree)
    for rel in before:
        p = wt / rel
        try:
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir(); removed.append(rel)
            elif p.is_file() and p.stat().st_size == 0:
                p.unlink(); removed.append(rel)
        except OSError:
            pass
    return removed


#: what git reads from a repository's own directory and could be told to run: a hook, a ``core.fsmonitor`` / ``core.hooksPath`` /
#: filter / diff command in a config file, a filter named in attributes
_GIT_RUNS = ("config", "config.worktree", "hooks", "info/attributes")


def worktree_admin(worktree: Union[str, Path]) -> Optional[str]:
    """The admin directory of a linked worktree (``<common git dir>/worktrees/<name>``), read from its ``.git`` file; None for a main
    checkout (whose ``.git`` is the directory itself)."""
    gitfile = Path(worktree) / ".git"
    try:
        if gitfile.is_file() and not gitfile.is_symlink():
            line = read_file(gitfile, follow=False).strip()
            if line.startswith("gitdir:"):
                admin = Path(line[len("gitdir:"):].strip())
                admin = admin if admin.is_absolute() else (Path(worktree) / admin)
                return str(admin.resolve())
    except OSError:
        pass
    return None


def agent_mounts(project: Any, worktree: Union[str, Path], git_dir: Union[str, Path]) -> List[Mount]:
    """What an agent's commands get: the project's mounts with a writable worktree, and the repository's git directory writable so it
    can commit — the objects, its branch's ref and the admin files of its own worktree. Read-only, because git on the host would act on
    them later, outside any sandbox:

    - what git runs: ``config``, ``config.worktree``, ``hooks``, ``info/attributes`` — of the repository, of the worktree's admin
      directory and of every submodule (``modules/*``); a missing ``hooks`` is covered by an empty read-only directory, so it cannot
      be made; a missing file can be, but does nothing while ``config`` is read-only, and jobs.run removes it;
    - where git looks: the worktree's ``.git`` file and its admin ``commondir`` / ``gitdir`` (else a job could point the host's later
      git commands at a repository of its own making);
    - what is not its own: the main checkout's ``HEAD``, ``index`` and ``logs/HEAD``, ``packed-refs``, every other worktree's admin
      directory, and where its objects come from (``objects/info/alternates``; a missing ``info`` cannot be made either).

    An agent's repository is the trial's shadow (jobs._shadow): what is left writable there — its own branch, its objects, the loose refs
    beside it — never reaches the project except through jobs._sync_back (that branch only, fast-forward, objects checked). The
    project's own git directory is read-only (project_mounts)."""
    gd = str(Path(git_dir).resolve())
    wt = Path(worktree).resolve()
    out = project_mounts(project, wt, writable=True) + [("rw", gd)]
    admin = worktree_admin(wt)
    linked = admin is not None and admin.startswith(gd + os.sep)

    def guard(base: str) -> None:
        for rel in _GIT_RUNS:
            p = os.path.join(base, rel)
            if os.path.lexists(p):
                out.append(("ro", p, p))
            elif rel == "hooks":                 # a hook needs no config to run: the directory cannot be made either
                out.append(("empty-ro", p))
            # a missing config file is left alone (a mount point would stay behind as an empty file): what a job creates there does
            # nothing while the repository's config stays read-only (config.worktree needs extensions.worktreeConfig in it)
    guard(gd)
    alternates = os.path.join(gd, "objects", "info", "alternates")
    if os.path.lexists(alternates):
        out.append(("ro", alternates, alternates))
    if not os.path.lexists(os.path.join(gd, "info")):
        out.append(("empty-ro", os.path.join(gd, "info")))
    modules = os.path.join(gd, "modules")
    if os.path.isdir(modules):
        for dirpath, dirnames, filenames in os.walk(modules):
            if "HEAD" in filenames and ("objects" in dirnames or "config" in filenames):
                guard(dirpath)
                dirnames[:] = [d for d in dirnames if d == "modules"]
    if linked:
        out.append(("ro", str(wt / ".git"), str(wt / ".git")))
        for rel in ("commondir", "gitdir"):
            p = os.path.join(admin, rel)
            if os.path.lexists(p):
                out.append(("ro", p, p))
        guard(admin)
        # -try: these can vanish between now and bwrap's setup (another job removing its worktree, a gc packing refs)
        for rel in ("HEAD", "index", "logs/HEAD", "packed-refs"):
            p = os.path.join(gd, rel)
            if os.path.lexists(p):
                out.append(("ro-try", p, p))
        others = os.path.join(gd, "worktrees")
        if os.path.isdir(others):
            for name in sorted(os.listdir(others)):
                p = os.path.join(others, name)
                if os.path.realpath(p) != admin and os.path.isdir(p):
                    out.append(("ro-try", p, p))
    return out


def auto_extra_ro(project: Any) -> List[str]:
    """Directories under $HOME the project's commands name by absolute path (an interpreter in ~/miniforge3, a tool in ~/.local), so an
    allowlist root can show them read-only: the first directory under $HOME of each such path, for the test command, the verify
    command and the measures."""
    home = os.path.expanduser("~").rstrip("/") + "/"
    cfg = project.config
    texts = [cfg.get("test_command") or "", (cfg.get("verify") or {}).get("tests") or ""]
    texts += [str(m.get("command") or "") for m in (cfg.get("verify") or {}).get("measures") or [] if isinstance(m, dict)]
    out: List[str] = []
    for text in texts:
        for path in re.findall(r"(?<![\w.~-])(/[^\s'\";|&<>()]+)", text):
            if path.startswith(home):
                top = home + path[len(home):].split("/")[0]
                if top not in out and os.path.isdir(top) and os.path.basename(top) not in (".ssh", ".gnupg", ".claude", ".codex", ".config"):
                    out.append(top)
    return out


#: never shown even when they sit on PATH or inside a tool's directory
_NEVER = (".ssh", ".gnupg", ".claude", ".codex", ".config", ".aws", ".azure", ".kube", ".docker", ".password-store", ".mozilla", ".pki")


def path_tool_dirs(path: Optional[str] = None, home: Optional[str] = None) -> List[str]:
    """The installations behind the PATH entries under $HOME (an allowlist root hides $HOME, and with it ~/miniforge3's python and its
    pytest, ~/.nvm's node, ~/.deno …): for ``<prefix>/bin`` the whole prefix (a conda or nvm install needs its lib/), except ~/.local,
    of which only bin/ and lib/ are shown (~/.local/share holds other programs' tokens); an entry not named bin is shown as it is.
    Nothing under the places that hold keys or agent configs."""
    home = (home or os.path.expanduser("~")).rstrip("/")
    out: List[str] = []
    for p in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep):
        p = os.path.normpath(p) if p else ""
        if not p.startswith(home + "/") or not os.path.isdir(p):
            continue
        first = p[len(home) + 1:].split("/")[0]
        if first in _NEVER:
            continue
        dirs = [p]
        if os.path.basename(p) == "bin":
            prefix = os.path.dirname(p)
            if prefix == home:
                dirs = [p]
            elif prefix == os.path.join(home, ".local"):
                dirs = [p, os.path.join(prefix, "lib")]
            else:
                dirs = [prefix]
        for d in dirs:
            if os.path.isdir(d) and d not in out and not any(d.startswith(o + "/") for o in out):
                out = [o for o in out if not o.startswith(d + "/")] + [d]
    return out


def mount_points(project: Any, worktree: Union[str, Path]) -> List[str]:
    """What bwrap will make in the worktree as mount points: the ``ro_binds`` and ``tmpfs`` targets it lacks now, with their missing
    parents, deepest first. They outlive the sandbox as empty directories, and ``project_mounts`` binds only where the worktree has
    nothing, so a caller removes those still empty afterwards (``cleanup_mount_points``), else the next run would lose the bind."""
    s = project_settings(project)
    wt = Path(worktree)
    out: List[str] = []
    for rel in list(s.get("ro_binds") or []) + list(s.get("tmpfs") or []):
        parts = Path(str(rel).strip("/")).parts
        if not parts or ".." in parts:
            continue
        for k in range(1, len(parts) + 1):
            sub = str(Path(*parts[:k]))
            if sub not in out and not os.path.lexists(wt / sub):
                out.append(sub)
    return sorted(out, key=lambda r: -len(Path(r).parts))
