"""Several projects from one server and one page (2026-11-04, the user asked for new, edit, import, export and save as).

A registry of the projects this machine knows (``~/.config/dtree/projects.json``, or under ``$DTREE_HOME``): path, name, mode and
when each was last opened. The server's current project is switched to another one in place (``make_handler``'s ``switch``); a new one
is made under the projects directory (``~/dtree-projects``, or ``$DTREE_PROJECTS_DIR``) with ``dtree init`` and ``git init``; a
project is exported as one JSON file — its own config keys that travel (name, mode, base, rules, docs, test_command) and the store's
questions, history and trials (``store.dump``): never a key, an agent's settings, a job, a check or a path of this machine — imported
into a new project (``store.load``, ids kept), or saved as a copy under another name.

Making, opening, importing, saving as and renaming are done from this machine only (``local_only`` routes): they touch the file system
by path. The listing and the export are read by anyone the server serves.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .project import CONFIG, MODES, Project, init
from .store import DecisionError, _text

EXPORT_VERSION = 1
MAX_NAME = 80
#: the config keys an export carries and an import takes: the project's own words, never agents, sandbox, server or this machine's paths
PORTABLE_KEYS = ("name", "mode", "base", "rules", "docs", "test_command")
#: what ``edit`` may change: the name, what the tree is, the base branch
EDITABLE = ("name", "mode", "base")


def home() -> Path:
    return Path(os.environ.get("DTREE_HOME") or "~/.config/dtree").expanduser()


def registry_path() -> Path:
    return home() / "projects.json"


def projects_dir() -> Path:
    return Path(os.environ.get("DTREE_PROJECTS_DIR") or "~/dtree-projects").expanduser()


def mode_of(project: Project) -> str:
    m = project.config.get("mode")
    return m if m in MODES else "decisions"


def info(project: Project) -> Dict[str, Any]:
    return {"path": str(project.root), "name": project.config.get("name"), "mode": mode_of(project), "opened": time.time()}


def _read_registry() -> Dict[str, Any]:
    p = registry_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (ValueError, OSError):
        data = {}
    items = data.get("projects") if isinstance(data, dict) else None
    return {"projects": [x for x in (items or []) if isinstance(x, dict) and isinstance(x.get("path"), str)]}


def _write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    """Atomically, through a temporary file beside it, with the mode given."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register(project: Project) -> Dict[str, Any]:
    """Put the project in the registry (or move it to the front, opened now)."""
    reg = _read_registry()
    entry = info(project)
    reg["projects"] = [x for x in reg["projects"] if x.get("path") != entry["path"]] + [entry]
    _write_json(registry_path(), reg)
    return entry


def forget(path: Any) -> bool:
    """Take a project out of the registry (its files stay)."""
    reg = _read_registry()
    p = str(Path(str(path)).expanduser().resolve())
    kept = [x for x in reg["projects"] if x.get("path") != p]
    if len(kept) == len(reg["projects"]):
        return False
    _write_json(registry_path(), {"projects": kept})
    return True


def listing(project: Project) -> Dict[str, Any]:
    """The registry with the current project first (added when it is not registered yet), each with whether it still exists."""
    cur = str(project.root)
    items: List[Dict[str, Any]] = []
    for x in _read_registry()["projects"]:
        items.append({"path": x["path"], "name": x.get("name"), "mode": x.get("mode") if x.get("mode") in MODES else "decisions",
                      "opened": x.get("opened"), "exists": (Path(x["path"]) / CONFIG).exists(), "current": x["path"] == cur})
    if not any(it["current"] for it in items):
        items.append(dict(info(project), exists=True, current=True))
    items.sort(key=lambda it: (not it["current"], -float(it.get("opened") or 0)))
    return {"current": current(project), "items": items, "dir": str(projects_dir())}


def current(project: Project) -> Dict[str, Any]:
    return dict(info(project), counts=project.store.counts())


def slug(name: str) -> str:
    """A directory name from a project's name: letters, digits, - and _ kept; anything else (Thai, spaces) becomes -; empty → project."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(name or "")).strip("-").lower()
    return s[:60] or "project"


def _new_root(name: str, where: Optional[str]) -> Path:
    base = Path(where).expanduser().resolve() if where else projects_dir()
    root, i = base / slug(name), 2
    while root.exists():
        root = base / ("%s-%d" % (slug(name), i))
        i += 1
    return root


def create(name: str, mode: str = "decisions", where: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> Project:
    """A new project: a directory under the projects directory (or ``where``), ``dtree init`` with the mode, ``git init`` when git is
    here (a story needs no git; a decisions project's trials do), registered and returned."""
    name = _text(name, MAX_NAME, "name", True)
    if mode not in MODES:
        raise DecisionError("mode must be one of %s" % ", ".join(MODES))
    root = _new_root(name, where)
    root.mkdir(parents=True)
    init(root, name, dict({"mode": mode}, **(extra or {})))
    if shutil.which("git"):
        try:
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(root), capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    p = Project(root)
    register(p)
    return p


def open_project(path: Any) -> Project:
    """An existing project by its path (``.dtree/config.json`` there), registered."""
    root = Path(str(path or "")).expanduser().resolve()
    if not str(path or "").strip() or not (root / CONFIG).exists():
        raise DecisionError("no dtree project at %s (.dtree/config.json)" % (root if str(path or "").strip() else "(no path)"))
    p = Project(root)
    register(p)
    return p


def _own_config(project: Project) -> Dict[str, Any]:
    path = project.root / CONFIG
    try:
        d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (ValueError, OSError):
        d = {}
    return d if isinstance(d, dict) else {}


def export_bundle(project: Project) -> Dict[str, Any]:
    """The project as one JSON value: version, when, name, mode, its portable config keys and the store's dump."""
    own = _own_config(project)
    return {"dtree_export": EXPORT_VERSION, "exported": time.time(), "name": project.config.get("name"), "mode": mode_of(project),
            "config": {k: own[k] for k in PORTABLE_KEYS if k in own}, "store": project.store.dump()}


def import_bundle(bundle: Any, name: Optional[str] = None, where: Optional[str] = None) -> Project:
    """A new project from an export: its portable config, then the store's rows with their ids. Never into an existing project."""
    if not isinstance(bundle, dict) or bundle.get("dtree_export") != EXPORT_VERSION or not isinstance(bundle.get("store"), dict):
        raise DecisionError("not a dtree export (dtree_export %d with a store)" % EXPORT_VERSION)
    cfg = bundle.get("config") if isinstance(bundle.get("config"), dict) else {}
    mode = bundle.get("mode") if bundle.get("mode") in MODES else (cfg.get("mode") if cfg.get("mode") in MODES else "decisions")
    extra = {k: cfg[k] for k in PORTABLE_KEYS if k in cfg and k not in ("name", "mode")}
    p = create(name or bundle.get("name") or cfg.get("name") or "imported", mode, where, extra)
    p.store.load(bundle["store"])
    return p


def save_as(project: Project, name: str, where: Optional[str] = None) -> Project:
    """A copy of the project under another name: its export imported as a new project."""
    return import_bundle(export_bundle(project), name, where)


def edit(project: Project, changes: Dict[str, Any]) -> Dict[str, Any]:
    """Change the project's name, mode or base branch in ``.dtree/config.json`` (the file's other keys kept), in effect at once."""
    if not isinstance(changes, dict):
        raise DecisionError("the edit must be an object")
    bad = [k for k in changes if k not in EDITABLE]
    if bad:
        raise DecisionError("cannot edit %s (only %s)" % (", ".join(sorted(bad)), ", ".join(EDITABLE)))
    own = _own_config(project)
    for k, v in changes.items():
        if k == "mode":
            if v not in MODES:
                raise DecisionError("mode must be one of %s" % ", ".join(MODES))
            own[k] = v
        else:
            own[k] = _text(v, MAX_NAME, k, True)
    if not changes:
        raise DecisionError("nothing to edit")
    path = project.root / CONFIG
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    _write_json(path, own, mode)
    project.reload()
    register(project)
    return current(project)


# ---------------------------------------------------------------------------- the routes and the commands
def register_routes(route: Callable, project: Project, switch: Optional[Callable[[Project], None]] = None) -> None:
    """``switch``: the server's way to make another project the current one (``make_handler``); without it (the API used directly)
    the new project is made and answered, and the server stays on this one (``switched: false``)."""
    def go(p: Project) -> Dict[str, Any]:
        if switch is not None:
            switch(p)
        return {"project": current(p), "switched": switch is not None}

    @route("GET", r"/api/projects")
    def list_(q, b):
        return listing(project)

    @route("POST", r"/api/projects", local_only=True)
    def new_(q, b):
        return go(create(b.get("name") or "", b.get("mode") or "decisions"))

    @route("POST", r"/api/projects/open", local_only=True)
    def open_(q, b):
        return go(open_project(b.get("path") or ""))

    @route("GET", r"/api/projects/export")
    def export_(q, b):
        return {"bundle": export_bundle(project)}

    @route("POST", r"/api/projects/import", local_only=True)
    def import_(q, b):
        return go(import_bundle(b.get("bundle"), b.get("name") or None))

    @route("POST", r"/api/projects/save-as", local_only=True)
    def save_as_(q, b):
        return go(save_as(project, b.get("name") or ""))

    @route("POST", r"/api/projects/edit", local_only=True)
    def edit_(q, b):
        return {"project": edit(project, b)}

    @route("POST", r"/api/projects/forget", local_only=True)
    def forget_(q, b):
        return {"forgotten": forget(b.get("path") or "")}


def register_cli(sub: Any) -> None:
    sub.add_parser("projects", help="the projects this machine knows (the registry), the current one first")
    p = sub.add_parser("new", help="a new project under the projects directory (dtree init + git init), registered")
    p.add_argument("name")
    p.add_argument("--mode", choices=list(MODES), default="decisions")
    p.add_argument("--dir", default=None, help="where to make it (default: ~/dtree-projects or $DTREE_PROJECTS_DIR)")
    p = sub.add_parser("export", help="this project as one JSON file: its own config, the questions, their history and trials")
    p.add_argument("--out", default=None, help="write it here (default: standard output)")
    p = sub.add_parser("import", help="a new project from an export file")
    p.add_argument("file")
    p.add_argument("--name", default=None)
    p.add_argument("--dir", default=None)
    p = sub.add_parser("save-as", help="a copy of this project under a new name")
    p.add_argument("name")
    p.add_argument("--dir", default=None)


def run_cli(args: Any, project: Project) -> bool:
    if args.cmd == "projects":
        for it in listing(project)["items"]:
            print("%s %-9s %-30s %s%s" % ("*" if it["current"] else " ", it["mode"], (it.get("name") or "")[:30], it["path"], "" if it["exists"] else "  (missing)"))
    elif args.cmd == "new":
        print(create(args.name, args.mode, args.dir).root)
    elif args.cmd == "export":
        text = json.dumps(export_bundle(project), ensure_ascii=False, indent=1) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(args.out)
        else:
            print(text, end="")
    elif args.cmd == "import":
        print(import_bundle(json.loads(Path(args.file).read_text(encoding="utf-8")), args.name, args.dir).root)
    elif args.cmd == "save-as":
        print(save_as(project, args.name, args.dir).root)
    else:
        return False
    return True
