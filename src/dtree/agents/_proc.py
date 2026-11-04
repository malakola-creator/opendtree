"""Running a CLI agent: find its binary, start it in its own process group, read its JSON lines, end it on a stop or the deadline."""
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
from typing import Any, Callable, Dict, Iterator, List, Optional

#: environment of a session that may have started dtree (a Claude Code or Codex shell): the agent's own session must not inherit it
PARENT_ENV = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION", "CODEX_SANDBOX", "CODEX_THREAD_ID")


def version_key(p: Path) -> List[int]:
    """The numbers in a path, in order: anthropic.claude-code-2.1.314-… sorts after 2.1.308."""
    return [int(x) for x in re.findall(r"\d+", str(p))]


def find(command: Optional[str], name: str, globs: List[str]) -> Optional[str]:
    """``command`` from config, else ``name`` on PATH (a link that resolves), else the newest match of ``globs`` under home."""
    if command:
        c = os.path.expanduser(str(command))
        return c if os.access(c, os.X_OK) else shutil.which(c)
    found = shutil.which(name)
    if found and os.path.exists(found):
        return found
    home = Path.home()
    for g in globs:
        hits = sorted(home.glob(g), key=version_key)
        for p in reversed(hits):
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    return None


def clean_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in PARENT_ENV and not k.startswith("CLAUDE_CODE_")}
    env.update(extra or {})
    return env


def stream(cmd: List[str], cwd: Any, stdin_text: str, ctx: Any, raw_log: Optional[Any] = None, env: Optional[Dict[str, str]] = None,
           end_group: bool = False) -> Iterator[Dict[str, Any]]:
    """Start ``cmd``, give it ``stdin_text``, yield each JSON line it prints. It is ended (its whole process group) when the job is
    stopped or the deadline passes; ``ctx.stop_reason`` then says which. ``end_group``: what it left running in its process group is
    killed once it has exited — before it is reaped, while its pid (the group's id) is still its own: killed after, the id could be
    another process group's by then (review of round 3)."""
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env or clean_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=raw_log if raw_log is not None else subprocess.DEVNULL, text=True, start_new_session=True)
    ctx.set_pid(proc.pid)
    ctx.stop_reason = None
    done = threading.Event()

    def watch() -> None:
        while not done.wait(1.0):
            why = "stopped" if ctx.stopped() else "timeout" if time.time() > ctx.deadline else None
            if why:
                ctx.stop_reason = why
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                return
    threading.Thread(target=watch, daemon=True).start()
    hold = end_group and hasattr(os, "waitid")
    try:
        proc.stdin.write(stdin_text)
        proc.stdin.close()
        for line in proc.stdout:
            if raw_log is not None:
                raw_log.write(line)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                yield ev
        if hold:
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)    # it has exited and is not reaped yet
        else:
            proc.wait()
    finally:
        if hold:
            try:
                os.killpg(proc.pid, signal.SIGKILL)                   # alive (an error above) or a zombie: the group is still its own
            except OSError:
                pass
        done.set()
        if hold:
            proc.wait()
        ctx.set_pid(None)
        ctx.exit_code = proc.poll()


def short(value: Any, n: int = 300) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"
