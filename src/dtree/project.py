"""A project dtree works on: its root (a git repository), its store, and the settings in ``.dtree/config.json``.

``dtree init`` writes the config with the defaults below; everything an agent is told about the project (its rules, how its tests
run, where its behaviour is documented) and every agent's settings live there, not in dtree's code.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .gitcode import Git
from .store import DecisionStore

CONFIG = Path(".dtree") / "config.json"

#: the agents a trial can be sent to. kind: claude (Claude Code CLI, in bubblewrap with dtree's egress proxy: agents/claude.py,
#: egress.py), codex (Codex CLI, in its own sandbox) or chat (any OpenAI-compatible chat API with tool calls, driven by dtree's own loop
#: in a sandbox). A key is read at call time from ``key_env`` or ``key_file``, never stored or shown. Caps and prices (spend.py):
#: ``max_budget_usd`` (claude), ``max_tokens_total`` (chat, codex) per job; ``month_usd`` / ``month_tokens`` per calendar month for this
#: agent alone (``month_tokens`` needs no price); ``total_usd`` / ``total_tokens`` over every month, for a credit (Muse within its
#: hackathon credit); ``price_in_per_mtok`` / ``price_out_per_mtok`` to estimate the cost of an agent that reports tokens only (the local
#: model's are 0: it costs nothing, and budget.month_usd never stops it). Claude Code's ``disallowed_tools``: the git commands dtree's rules
#: forbid, sudo / ssh, and its own tools that act outside the job through the account or the web, or move it to another worktree (the
#: names of a real session's init event, Claude Code 2.1.314); a project that sets its own list replaces this one.
CLAUDE_DISALLOWED = ["WebSearch", "WebFetch", "RemoteTrigger", "CronCreate", "CronDelete", "CronList", "Workflow", "SendMessage",
                     "ListAgents", "EnterWorktree", "ExitWorktree",
                     "Bash(git push:*)", "Bash(git merge:*)", "Bash(git rebase:*)", "Bash(git switch:*)", "Bash(git checkout:*)",
                     "Bash(git worktree:*)", "Bash(git branch -D:*)", "Bash(git branch -d:*)", "Bash(git reset --hard:*)",
                     "Bash(sudo:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)"]
AGENTS: Dict[str, Dict[str, Any]] = {
    "claude": {"kind": "claude", "label": "Claude Code", "command": None, "model": None, "permission_mode": "auto",
               "disallowed_tools": list(CLAUDE_DISALLOWED),
               "sandbox": "bwrap", "network": "proxy", "network_allow": [], "credentials": "ro"},
    "codex": {"kind": "codex", "label": "Codex", "command": None, "model": None, "sandbox": "workspace-write"},
    "muse": {"kind": "chat", "label": "Muse Spark (Meta Model API)", "base_url": "https://api.meta.ai/v1", "model": "muse-spark-1.3",
             "key_env": "MODEL_API_KEY", "key_file": None},
    "grok": {"kind": "chat", "label": "Grok (xAI API)", "base_url": "https://api.x.ai/v1", "model": "grok-4", "key_env": "XAI_API_KEY", "key_file": None},
    # Google's OpenAI-compatible endpoint (2026-11-02: gemini-2.5-flash is gone for new keys; 3.5-flash answered a tool call in 1.8 s)
    "gemini": {"kind": "chat", "label": "Gemini (Google AI)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-3.5-flash", "key_env": "GEMINI_API_KEY", "key_file": None},
    "local": {"kind": "chat", "label": "Local model (Ollama)", "base_url": "http://127.0.0.1:11434/v1", "model": "gemma4:26b", "key_env": None, "key_file": None,
              "price_in_per_mtok": 0, "price_out_per_mtok": 0},
}

#: what a project's tree is: decisions (questions a person answers, options tried on branches) or a story (every option is a scene
#: that follows; the reader chooses — a gamebook, 2026-11-04). The store is the same; the page and the exports read it differently.
MODES = ("decisions", "story")

DEFAULTS: Dict[str, Any] = {
    "name": None,
    "mode": "decisions",
    "db": ".dtree/decisions.sqlite",
    "base": "main",
    "worktrees": "../{name}-worktrees",
    "logs": ".dtree/logs",
    "rules": [],
    "test_command": None,
    "docs": None,
    "timeout_s": 5400,
    "max_parallel_jobs": 3,
    "adr_dir": "docs/decisions",
    "chat": {"max_steps": 80, "max_tokens": 4096, "command_timeout_s": 600, "sandbox": "bwrap", "temperature": 0.2, "request_timeout_s": 600,
             "retries": 8, "retry_max_wait_s": 300},
    "verify": {"enabled": True, "auto": True, "tests": None, "baseline": True, "timeout_s": 1800, "measures": [], "workdir": None,
               "sandbox": "bwrap",
               "select": {"always": [], "changed": True, "pattern": ["tests/test_{stem}.py", "tests/test_{stem}_*.py"], "map": [],
                          "imports": True, "hub_limit": 10, "test_files": ["tests/**/test_*.py"], "fallback": "always"}},
    "sandbox": {"ro_binds": [], "copies": [], "env": {}},
    "budget": {"month_usd": None, "job_usd": None, "job_tokens": None},
    "merge": {"method": "auto"},
    "default_agent": "claude",
    "agents": AGENTS,
    "server": {"host": "127.0.0.1", "port": 5077, "allowed_hosts": []},
}

GITIGNORE = "*.sqlite\n*.sqlite-*\nlogs/\nwrite_token\n"


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "agents":
            out[k] = _merge(out[k], v)
        elif k == "agents" and isinstance(v, dict):
            agents = copy.deepcopy(out.get("agents") or {})
            for name, a in v.items():
                if name.startswith("_"):
                    continue
                if a is None:
                    agents.pop(name, None)
                elif isinstance(a, dict):
                    agents[name] = _merge(agents.get(name) or {}, a)
            out["agents"] = agents
        else:
            out[k] = v
    return out


class Project:
    def __init__(self, root: Any, config: Optional[Dict[str, Any]] = None) -> None:
        self.root = Path(root).resolve()
        if config is None:
            path = self.root / CONFIG
            config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.config = _merge(DEFAULTS, config)
        self.config["name"] = self.config.get("name") or self.root.name
        self.git = Git(self.root)
        self._store: Optional[DecisionStore] = None

    @classmethod
    def find(cls, start: Any = None) -> "Project":
        """The project whose ``.dtree/config.json`` is at ``start`` or above it; else ``start`` itself (no config: the defaults)."""
        here = Path(start or os.getcwd()).resolve()
        for p in [here] + list(here.parents):
            if (p / CONFIG).exists():
                return cls(p)
        return cls(here)

    def path(self, value: str) -> Path:
        p = Path(os.path.expanduser(str(value).format(name=self.config["name"])))
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def store(self) -> DecisionStore:
        if self._store is None:
            env = os.environ.get("DTREE_DB")
            self._store = DecisionStore(env or self.path(self.config["db"]))
        return self._store

    @property
    def base(self) -> str:
        return str(self.config["base"])

    def agent(self, name: Optional[str]) -> Dict[str, Any]:
        name = name or self.config["default_agent"]
        a = (self.config.get("agents") or {}).get(name)
        if not a:
            raise KeyError(name)
        return dict(a, name=name)

    def reload(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Read ``.dtree/config.json`` again (or take ``config``) and put the merged settings into ``self.config`` in place, so the
        next request of a running server, and whoever holds this dict, uses them (settings.py, after a change is written). Each
        top-level value is swapped whole and only then are the keys the file no longer sets dropped, so a reader in another thread
        sees the old value or the new one, never a half-made one. The store and git are kept: a new ``db`` takes effect at the next
        start. A project made with a config in memory reads the file here too: the file is where settings are kept."""
        if config is None:
            path = self.root / CONFIG
            config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        new = _merge(DEFAULTS, config)
        new["name"] = new.get("name") or self.root.name
        self.config.update(new)
        for k in [k for k in self.config if k not in new]:
            self.config.pop(k, None)
        return self.config


def init(root: Any, name: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> Path:
    """Write ``.dtree/config.json`` (the project's own settings; agents and server keep dtree's defaults unless given) and a
    ``.dtree/.gitignore`` for the store, logs and token. An existing config is left as it is."""
    root = Path(root).resolve()
    d = root / ".dtree"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    path = root / CONFIG
    if not path.exists():
        cfg = {"name": name or root.name, "base": "main", "rules": [], "test_command": None, "docs": None, "default_agent": "claude",
               "_agents": "dtree's defaults: claude, codex, muse, grok, local — override one here as \"agents\": {\"local\": {\"model\": \"…\"}}"}
        cfg.update(extra or {})
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path
