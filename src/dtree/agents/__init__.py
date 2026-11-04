"""The agents a trial can be sent to. Each takes the job's prompt, works in the trial's worktree, says what it does line by line, and
ends with the structured result (built, tests, impact, note).

- ``claude`` — Claude Code, headless (``claude -p --output-format stream-json --json-schema``).
- ``codex``  — Codex CLI (``codex exec --json --output-schema``), in its own workspace-write sandbox.
- ``chat``   — any OpenAI-compatible chat API with tool calls (Muse Spark on the Meta Model API, Grok on xAI, a local model on Ollama…),
  driven by dtree's own loop: read / write / edit files in the worktree and run commands in a bubblewrap sandbox without network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

#: the agent's last word, the same for every agent: schema/result.schema.json without its document keys ($schema, $id, title,
#: description), which Codex's structured output refuses
_DOC_KEYS = ("$schema", "$id", "title", "description")
RESULT_SCHEMA = {k: v for k, v in json.loads((Path(__file__).resolve().parents[1] / "schema" / "result.schema.json").read_text(encoding="utf-8")).items()
                 if k not in _DOC_KEYS}


class Context:
    """What the runner gives an agent: where to work, how to log, how to report its process (for a stop), and whether to stop."""

    def __init__(self, worktree: Any, git_dir: Any, say: Callable[[str], None], set_pid: Callable[[Optional[int]], None],
                 stopped: Callable[[], bool], deadline: float, set_cost: Callable[[float], None] = lambda c: None) -> None:
        self.worktree, self.git_dir, self.say, self.set_pid, self.stopped, self.deadline, self.set_cost = worktree, git_dir, say, set_pid, stopped, deadline, set_cost


class Agent:
    kind = ""

    def __init__(self, cfg: Dict[str, Any], project: Any) -> None:
        self.cfg = cfg
        self.project = project
        self.name = cfg.get("name") or self.kind

    def available(self) -> Tuple[bool, str]:
        raise NotImplementedError

    def run(self, prompt: str, ctx: Context) -> Dict[str, Any]:
        """Returns {built, tests, impact, note} plus what is known: session, turns, cost_usd, tokens; ``error`` when it ended badly."""
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        """What the page shows beside the agent's name: its sandbox, network and caps."""
        return {}


def make(cfg: Dict[str, Any], project: Any) -> Agent:
    kind = cfg.get("kind")
    if kind == "claude":
        from .claude import ClaudeAgent
        return ClaudeAgent(cfg, project)
    if kind == "codex":
        from .codex import CodexAgent
        return CodexAgent(cfg, project)
    if kind == "chat":
        from .chat import ChatAgent
        return ChatAgent(cfg, project)
    raise ValueError("agent %r has an unknown kind %r (claude, codex, chat)" % (cfg.get("name"), kind))
