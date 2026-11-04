"""ก — dtree verifies an agent's work itself (the user chose it on 2026-11-01).

An agent's own report ("tests 3 passed") is a claim. After a build job (or ``dtree verify TRIAL``), dtree runs the project's tests and
measures (``verify`` in the config) itself, in a sandbox (``sandbox.py``: no network, nothing writable but a fresh detached worktree of
the commit and an output directory), on the trial's branch tip and on the commit it left the base from (the baseline). The numbers go
in the ``checks`` table, and the compare table shows them beside what the agent said, with the change against the baseline.

- **Which tests.** ``verify.tests`` with ``{tests}`` runs only the test files the trial's changes select (``selection``): the test
  files it changed, those named after the files it changed (``tests/test_{stem}.py``, ``tests/test_{stem}_*.py``), directory maps,
  the test files that import a changed module (not a hub imported by more than ``hub_limit`` test files, when the ``always`` set has a
  file to stand for it), those below a changed ``conftest.py`` or test helper (a hub too when they are more than ``hub_limit``, on
  the same condition), and the ``always`` set. Each
  side runs the selected files it has, so a test the trial adds counts as new instead of breaking the baseline's run (pytest stops the
  whole run when one named file is missing). Without ``{tests}`` the command runs as it is; null takes ``test_command``.
- **Counts.** For pytest dtree gives ``--junitxml`` through ``PYTEST_ADDOPTS`` (never by editing the command, which can be a pipe or a
  subshell): exact counts whatever ``-q`` the project uses (ultimateTruth's ``-q`` on top of ``addopts = -q`` prints no summary at
  all). Anything else is read from the printed summary (``parse_summary.py``). Counts are bounded and measures finite: what the code
  under test prints or writes must not break the store or the page's JSON.
- **Cache.** A command's result on a commit does not change while what it runs is installed the same, so a check that ended passed
  or failed is reused by its key (commit, command with the selection, pattern, the sandbox settings that change results, the
  installation of the programs the command runs — ``_environment`` —, this module's ``VERSION``): verifying again costs nothing, and
  a race of three agents from one base runs the baseline once, also when they finish at the same moment (the store claims a key in
  one transaction; the others wait for the runner that has it, as long as it moves on).
- **Never unsandboxed by surprise.** With ``sandbox: bwrap`` (the default) and bubblewrap unable to run here, every check ends
  ``error`` with the reason; running without it is ``"sandbox": "none"`` in the config, chosen by the person.

Interface used by the rest of dtree (kept stable):
- ``register_routes(route, project)`` · ``register_cli(sub)`` · ``run_cli(args, project) -> bool``
- ``after_job(project, job, trial, say) -> dict | None`` — called by the runner after a build job with commits; returns a summary for
  the job's result (only when ``auto_on``: ``verify.tests`` or ``verify.measures`` set — a bare ``test_command`` is for ``dtree
  verify`` by hand)
- ``compare_fields(project, trial) -> dict`` — added to the trial's row of ``jobs.compare``
- ``start_background(project, tid, by, force, baseline=None, auto=False) -> pid | None`` — verify a trial in a detached process
  (``auto=True`` for merge.py's call after a clean update: the rule of jobs, ``auto_on``)
- ``settings(project)`` · ``auto_on(project)`` · ``selection(project, base_sha, tip_sha)`` · ``verify_trial(project, tid, …) -> summary``
"""
from __future__ import annotations

import argparse
import ast
import bisect
import contextlib
import copy
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from . import sandbox
from .parse_summary import parse_summary
from .project import DEFAULTS as PROJECT_DEFAULTS
from .store import DecisionError

#: bump when the way a check's result is computed changes: results cached by an older dtree are not reused (2: pytest's flags through
#: PYTEST_ADDOPTS, so a pipe or a subshell no longer ends a false failed; a SIGKILL is an error. 3: a pytest run that counted nothing
#: is an error, so the false passes 2 kept for a pytest that crashed behind a pipe are not reused)
VERSION = 3
PKG_SRC = Path(__file__).resolve().parents[1]
#: the sandbox settings that can change a result, so they are part of a check's cache key (``secrets`` only hides files)
KEY_SANDBOX = ("root", "nested", "gpu", "ro_binds", "tmpfs", "copies", "env", "extra_ro", "path_tools")
FALLBACKS = ("always", "all", "skip")
WAIT_POLL_S = 0.5
WAIT_GRACE_S = 120.0
READ_CAP = 8 * 1024 * 1024               # how much of the output's end a summary or a measure's pattern is read from
JUNIT_CAP = 64 * 1024 * 1024
COPY_MARGIN = 256 * 1024 * 1024          # the free space a copy must leave behind
MAX_IMPORT_SCAN = 5000                   # test files read for the import rule; with more the rule is left out
MAX_SOURCE = 2 * 1024 * 1024             # of each test file's source
MAX_HUBS = 20                            # hubs a summary lists; how many more there were is ``hubs_more``
SUMMARY_CHARS = 2000
#: a count is a whole number from 0 to this: the code under test writes the JUnit file and prints the summary, and sqlite stores 64 bits
MAX_COUNT = 10 ** 9
COUNT_KEYS = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed", "total")
#: how a run killed by SIGKILL from outside (the OOM killer, a person) ends: -9 from Popen, 128 + 9 from a shell or bwrap
SIGKILLED = (-9, 137)
KILLED = "killed by SIGKILL (out of memory?): the run did not finish"
#: pytest writes its JUnit file whenever it finishes: a run that ended 0 with none and no printed counts did not finish
UNCOUNTED = "pytest left no JUnit file and printed no counts: it did not finish (a crash or a kill; a pipe hides its exit status)"
#: unless the command hid dtree's flags: pytest reads PYTEST_ADDOPTS first, so the command line's --junitxml wins over dtree's, and an
#: inline PYTEST_ADDOPTS=… replaces dtree's whole variable
_OWN_FLAGS = re.compile(r"--junit-?xml\b|\bPYTEST_ADDOPTS\b")
OWN_FLAGS = ("pytest left dtree no JUnit file and printed no counts: the command sets its own --junitxml or PYTEST_ADDOPTS, which hide "
             "dtree's (leave them out of the command; pytest options for every run go in sandbox.env's PYTEST_ADDOPTS)")
_GLOB_CHARS = re.compile(r"[*?\[]")
#: a bare pytest also inside quotes (`bash -o pipefail -c 'pytest …'`, which docs/verify.md recommends for a pipe) or after `=`
_PYTEST = re.compile(r"(?:^|[\s/;&|('\"`=])(?:py\.test|pytest)(?=[\s;&|)'\"`]|$)|-m\s*pytest\b")
#: `dtree verify`'s exit code when nothing was run (status none): not 0, so `dtree verify T && …` never goes on unverified; not 1
#: (did not pass) nor 2 (argparse's usage error)
EXIT_NONE = 3
#: the names of a tests directory: a helper or data file under one is test infrastructure (selection rule e)
TEST_DIRS = ("tests", "test", "testing")
#: at most this many programs of a command are stamped in its key (_environment)
MAX_PROGRAMS = 16
_WORDS = re.compile(r"[\s;|&()<>`]+")
_PYTHON_NAME = re.compile(r"(?:python|pypy)[0-9.]*\Z")
Item = Dict[str, Any]
Claimed = List[Tuple[Item, Dict[str, Any]]]


# ------------------------------------------------------------------------------------------------------------------------- settings
def _deep(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if str(k).startswith("_"):
            continue
        out[k] = _deep(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _strings(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise DecisionError("verify.%s must be text or a list of texts" % field)
    return list(value)


def settings(project: Any) -> Dict[str, Any]:
    """``verify`` of the project's config with dtree's defaults, checked, plus what follows from it: ``command`` (the tests command:
    ``tests``, else ``test_command``, else None), ``uses_selection`` (``{tests}`` in it) and ``workdir`` as a path. A setting that
    cannot work raises DecisionError saying what to fix."""
    raw = project.config.get("verify")
    if raw is not None and not isinstance(raw, dict):
        raise DecisionError("verify must be an object in .dtree/config.json")
    s = _deep(PROJECT_DEFAULTS["verify"], raw or {})
    for k in ("enabled", "auto", "baseline"):
        s[k] = bool(s.get(k))
    tests = s.get("tests")
    if tests is not None and (not isinstance(tests, str) or not tests.strip()):
        raise DecisionError("verify.tests must be a command (or null for test_command)")
    try:
        s["timeout_s"] = float(1800 if s.get("timeout_s") is None else s["timeout_s"])
    except (TypeError, ValueError, OverflowError):
        raise DecisionError("verify.timeout_s must be a number of seconds")
    if not math.isfinite(s["timeout_s"]) or s["timeout_s"] <= 0:          # JSON's NaN or 1e999 would mean no time limit at all
        raise DecisionError("verify.timeout_s must be a finite number more than 0")
    if s.get("sandbox") not in ("bwrap", "none"):
        raise DecisionError("verify.sandbox is bwrap or none")
    if not isinstance(s.get("select") or {}, dict) or not isinstance(s.get("measures") or [], list):
        raise DecisionError("verify.select must be an object and verify.measures a list")
    sel = s["select"] = dict(s.get("select") or {})
    sel["always"] = _strings(sel.get("always"), "select.always")
    sel["pattern"] = _strings(sel.get("pattern"), "select.pattern")
    sel["test_files"] = _strings(sel.get("test_files"), "select.test_files") or ["tests/**/test_*.py"]
    sel["changed"], sel["imports"] = bool(sel.get("changed", True)), bool(sel.get("imports", True))
    try:
        sel["hub_limit"] = int(sel["hub_limit"] if sel.get("hub_limit") is not None else 10)
    except (TypeError, ValueError, OverflowError):
        raise DecisionError("verify.select.hub_limit must be a whole number")
    if sel.get("fallback") not in FALLBACKS:
        raise DecisionError("verify.select.fallback is one of %s" % ", ".join(FALLBACKS))
    maps = []
    for i, m in enumerate(sel.get("map") or []):
        if not isinstance(m, dict) or not m.get("match") or not m.get("tests"):
            raise DecisionError("verify.select.map[%d] must be {\"match\": glob, \"tests\": [...]}" % i)
        maps.append({"match": _strings(m["match"], "select.map[%d].match" % i), "tests": _strings(m["tests"], "select.map[%d].tests" % i)})
    sel["map"] = maps
    measures, seen = [], set()
    for i, m in enumerate(s.get("measures") or []):
        if not isinstance(m, dict):
            raise DecisionError("verify.measures[%d] must be an object" % i)
        name, command, pattern = (str(m.get(k) or "").strip() for k in ("name", "command", "pattern"))
        if not name or not command or not pattern:
            raise DecisionError("verify.measures[%d] needs name, command and pattern" % i)
        if name == "tests" or name in seen or len(name) > 60 or not name.isprintable():
            raise DecisionError("verify.measures[%d]: the name %r is taken, too long or not printable" % (i, name))
        seen.add(name)
        try:
            re.compile(pattern, re.M)
        except re.error as exc:
            raise DecisionError("verify.measures[%d].pattern is not a regular expression: %s" % (i, exc))
        if m.get("better") not in (None, "higher", "lower"):
            raise DecisionError("verify.measures[%d].better is higher or lower" % i)
        try:
            timeout = s["timeout_s"] if m.get("timeout_s") is None else float(m["timeout_s"])
            tolerance = abs(float(m.get("tolerance_pct") or 0))
        except (TypeError, ValueError, OverflowError):
            raise DecisionError("verify.measures[%d]: timeout_s and tolerance_pct are numbers" % i)
        if not (math.isfinite(timeout) and math.isfinite(tolerance)):
            raise DecisionError("verify.measures[%d]: timeout_s and tolerance_pct must be finite numbers" % i)
        if timeout <= 0:
            raise DecisionError("verify.measures[%d].timeout_s must be more than 0" % i)
        measures.append({"name": name, "command": command, "pattern": pattern, "unit": str(m.get("unit") or ""), "better": m.get("better"),
                         "timeout_s": timeout, "tolerance_pct": tolerance})
    s["measures"] = measures
    s["command"] = tests or project.config.get("test_command") or None
    s["uses_selection"] = tests is not None and "{tests}" in tests
    s["workdir"] = project.path(s["workdir"]) if s.get("workdir") else project.path(project.config["worktrees"]) / ".verify"
    return s


def auto_on(project: Any) -> bool:
    """Whether a build or update job that ends with commits is verified (``after_job``), and a clean update
    (``start_background(auto=True)``): ``verify.enabled`` and ``verify.auto``, and the project said what to verify — ``verify.tests``
    or ``verify.measures``. A bare ``test_command`` is the command the agents are
    told about and ``dtree verify`` runs by hand; it is often the whole suite (ultimateTruth's: over an hour), too much after every job."""
    return _auto(settings(project))


def _auto(s: Dict[str, Any]) -> bool:
    return bool(s["enabled"] and s["auto"] and (s.get("tests") is not None or s["measures"]))


def _fingerprint(project: Any, s: Dict[str, Any]) -> str:
    """What decides the results apart from the commit: when it differs from a verification's, the page says the settings changed."""
    sb = sandbox.project_settings(project)
    data = {"v": VERSION, "tests": s["command"], "select": s["select"] if s["uses_selection"] else None,
            "measures": [[m["name"], m["command"], m["pattern"]] for m in s["measures"]], "sandbox": s["sandbox"],
            "box": {k: sb.get(k) for k in KEY_SANDBOX}}
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------------------------------ git reads
def _sha(g: Any, ref: Optional[str]) -> Optional[str]:
    if not ref:
        return None
    try:
        r = g.run("rev-parse", "--verify", "--quiet", ref + "^{commit}", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and re.match(r"^[0-9a-f]{40,64}$", out) else None


def _merge_base(g: Any, a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a or not b:
        return None
    try:
        r = g.run("merge-base", a, b, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip().split("\n")[0].strip()
    return out if r.returncode == 0 and out else None


def _ls_tree(g: Any, sha: Optional[str]) -> Set[str]:
    if not sha:
        return set()
    r = g.run("ls-tree", "-r", "-z", "--name-only", sha, timeout=120)
    return {p for p in r.stdout.split("\0") if p} if r.returncode == 0 else set()


def _changed(g: Any, a: Optional[str], b: str) -> List[str]:
    if not a:
        return []
    r = g.run("diff", "--name-only", "-z", "--no-renames", a, b, timeout=120)
    return sorted({p for p in r.stdout.split("\0") if p}) if r.returncode == 0 else []


def _read_blobs(project: Any, specs: List[str]) -> Dict[str, bytes]:
    """Many ``<sha>:<path>`` at once through ``git cat-file --batch`` (one process for every test file)."""
    if not specs:
        return {}
    try:
        r = subprocess.run(["git", "-C", str(project.root), "cat-file", "--batch"], input="".join(x + "\n" for x in specs).encode("utf-8"),
                           capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out, pos, got = r.stdout, 0, {}
    for spec in specs:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        head = out[pos:nl].decode("utf-8", "replace").split(" ")
        pos = nl + 1
        if len(head) != 3 or not head[2].isdigit():
            continue                                         # "<spec> missing" / "ambiguous": nothing follows
        size = int(head[2])
        got[spec] = out[pos:pos + size]
        pos += size + 1
    return got


# ------------------------------------------------------------------------------------------------------------------------ selection
def _glob_rx(pattern: str) -> "re.Pattern[str]":
    """A path glob as a regular expression: ``**/`` any directories (also none), ``*`` and ``?`` within one name, ``[...]``."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            out.append(".*"); i += 2
        elif pattern[i] == "*":
            out.append("[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append("[^/]"); i += 1
        elif pattern[i] == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            k = pattern.find("]", j)
            if k < 0:
                out.append(re.escape("[")); i += 1
            else:
                inner, neg = pattern[i + 1:k], ""
                if inner[:1] in ("!", "^"):
                    inner, neg = inner[1:], "^"
                out.append("[" + neg + "".join("\\" + c if c in "\\[]&~|^" else c for c in inner) + "]"); i = k + 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return re.compile("".join(out) + r"\Z")


def _glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def _has(path: str, ordered: List[str], present: Set[str]) -> bool:
    """A file of the commit(s), or a directory holding one (pytest takes a directory too)."""
    if path in present:
        return True
    prefix = path.rstrip("/") + "/"
    i = bisect.bisect_left(ordered, prefix)
    return i < len(ordered) and ordered[i].startswith(prefix)


def _expand(pattern: str, ordered: List[str], present: Set[str]) -> List[str]:
    """The paths of ``ordered`` (sorted) that ``pattern`` names: just itself (a file or a directory) when it has no glob characters."""
    first = _GLOB_CHARS.search(pattern)
    if not first:
        return [pattern.rstrip("/")] if pattern.strip("/") and _has(pattern, ordered, present) else []
    rx, prefix, out = _glob_rx(pattern), pattern[:first.start()], []
    for p in ordered[bisect.bisect_left(ordered, prefix):]:
        if not p.startswith(prefix):
            break
        if rx.match(p):
            out.append(p)
    return out


def _stem(path: str) -> Optional[str]:
    p = PurePosixPath(path)
    return (p.parent.name if p.stem == "__init__" else p.stem) or None


def _module_name(path: str) -> Optional[str]:
    """``src/pkg/world/base.py`` → ``pkg.world.base``; ``pkg/__init__.py`` → ``pkg``; not Python → None."""
    if not path.endswith(".py"):
        return None
    p = path[:-3]
    parts = (p[4:] if p.startswith("src/") else p).split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts and all(x.isidentifier() for x in parts) else None


def _helper_names(path: str) -> List[str]:
    """The names a test imports a helper module under a tests directory by: pytest puts a test's own directory (the first one up
    without an ``__init__.py``) on sys.path, so ``tests/helpers.py`` is ``helpers`` to ``tests/test_x.py`` (and ``tests.helpers``
    only to a package of tests); ``tests/unit/h.py`` is ``unit.h`` or ``h``."""
    if not path.endswith(".py"):
        return []
    parts = list(PurePosixPath(path[:-3]).parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    tops = [i for i, x in enumerate(parts[:-1]) if x in TEST_DIRS]
    if not tops or parts[-1] == "conftest":
        return []
    return [".".join(parts[k:]) for k in range(tops[0] + 1, len(parts)) if all(x.isidentifier() for x in parts[k:])]


def _below(scope: str, tests_all: List[str]) -> List[str]:
    """The test files (``tests_all``, sorted) in directory ``scope`` and below it; "" is the whole repository."""
    if not scope:
        return list(tests_all)
    prefix, out = scope + "/", []
    for t in tests_all[bisect.bisect_left(tests_all, prefix):]:
        if not t.startswith(prefix):
            break
        out.append(t)
    return out


def _helper_scope(path: str, tests_all: List[str]) -> Optional[str]:
    """Where the tests are that a changed file which is not a test itself can break (rule e), or None. A ``conftest.py``: its own
    directory (pytest runs it for every test there and below; the top one's for all). A helper module or a data file under a tests
    directory (named tests, test or testing): the nearest directory with test files below it, from its own up to that tests
    directory. Elsewhere a file is the code under test, left to the other rules."""
    p = PurePosixPath(path)
    parts = list(p.parts[:-1])
    if p.name == "conftest.py":
        return "/".join(parts)
    tops = [i for i, x in enumerate(parts) if x in TEST_DIRS]
    for k in range(len(parts), tops[0], -1) if tops else ():
        d = "/".join(parts[:k])
        if _below(d, tests_all):
            return d
    return None


def _imports_of(source: str, path: str) -> Set[str]:
    """Every module a test file imports, with their parent packages (an import runs them too): ``import``, ``from … import``
    (relative ones from the file's own directory), and ``importlib.import_module("…")`` / ``pytest.importorskip("…")`` with a literal."""
    names: Set[str] = set()
    try:
        with warnings.catch_warnings():                    # "invalid escape sequence" in a test's source is not dtree's to report
            warnings.simplefilter("ignore")
            tree: Optional[ast.AST] = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):   # a file that does not parse still says what it imports
        tree = None
        for m in re.finditer(r"^\s*from\s+([\w.]+)\s+import\s+([\w, ()]+)|^\s*import\s+([\w., ]+)", source, re.M):
            if m.group(1):
                names.add(m.group(1))
                names.update(m.group(1) + "." + x.strip() for x in m.group(2).strip("() ").split(",") if x.strip())
            else:
                names.update(x.split(" as ")[0].strip() for x in m.group(3).split(",") if x.strip())
    here = PurePosixPath(path).parent.parts
    for node in ast.walk(tree) if tree is not None else ():
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                pkg = here[:len(here) - (node.level - 1)] if node.level > 1 else here
                mod = ".".join(list(pkg) + ([mod] if mod else []))
            if mod:
                names.add(mod)
                names.update(mod + "." + a.name for a in node.names if a.name != "*")
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            f = node.func
            if (f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else "") in ("import_module", "importorskip", "__import__"):
                names.add(node.args[0].value)
    out: Set[str] = set()
    for n in names:
        parts = [x for x in n.split(".") if x]
        out.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
    return out


def _select(project: Any, s: Dict[str, Any], mb: Optional[str], tip: str) -> Dict[str, Any]:
    sel, g = s["select"], project.git
    tip_files, base_files = _ls_tree(g, tip), _ls_tree(g, mb)
    present = tip_files | base_files
    ordered = sorted(present)
    test_rx = [_glob_rx(p) for p in sel["test_files"]]
    is_test = lambda f: any(rx.match(f) for rx in test_rx)
    tests_all = [f for f in ordered if is_test(f)]
    changed = _changed(g, mb, tip)
    reasons: Dict[str, List[str]] = {}
    hubs: List[Dict[str, Any]] = []

    def add(path: str, why: str) -> None:
        if _has(path, ordered, present):
            got = reasons.setdefault(path, [])
            if why not in got and len(got) < 6:
                got.append(why)

    always = list(dict.fromkeys(t for p in sel["always"] for t in _expand(p, ordered, present)))   # (f)'s files: what stands for a hub
    if sel["changed"]:
        for f in changed:                                                    # (a) a changed test file runs itself
            if is_test(f):
                add(f, "changed")
        for f in changed:                                                    # (b) the tests named after a changed file
            stem = None if is_test(f) else _stem(f)
            for p in sel["pattern"] if stem else []:
                for t in _expand(p.replace("{stem}", _glob_escape(stem)), ordered, present):
                    add(t, "named after " + f)
        for m in sel["map"]:                                                 # (c) a directory's own tests
            rxs = [_glob_rx(x) for x in m["match"]]
            hit = next((f for f in changed if any(rx.match(f) for rx in rxs)), None)
            for t in m["tests"] if hit else []:
                for x in _expand(t, ordered, present):
                    add(x, "map %s ← %s" % ("|".join(m["match"]), hit))
        mods: Dict[str, str] = {}
        for f in changed if sel["imports"] else []:                          # (d) the tests importing a changed module
            for name in [_module_name(f)] + ([] if is_test(f) else _helper_names(f)):   # a test helper also by the name tests use
                if name:
                    mods.setdefault(name, f)
        if mods and len(tests_all) <= MAX_IMPORT_SCAN:
            specs = {t: "%s:%s" % (tip if t in tip_files else mb, t) for t in tests_all if "\n" not in t}
            blobs = _read_blobs(project, list(specs.values()))
            index = {t: _imports_of(blobs.get(spec, b"")[:MAX_SOURCE].decode("utf-8", "replace"), t) for t, spec in specs.items()}
            for name in sorted(mods):
                importers = [t for t in tests_all if name in index.get(t, ())]
                if len(importers) > sel["hub_limit"] and always:              # imported nearly everywhere: left to `always` (with no
                    hubs.append({"module": name, "importers": len(importers)})  # file in it, its importers run: reviews of rounds 3, 4)
                    continue
                for t in importers:
                    add(t, "imports " + name)
        below: Dict[str, Dict[str, Any]] = {}
        for f in changed:                                                    # (e) a conftest.py, a test helper: the tests below it
            scope = None if is_test(f) else _helper_scope(f, tests_all)
            under = _below(scope, tests_all) if scope is not None else []
            if len(under) > sel["hub_limit"] and always:                     # nearly the whole suite: left to `always`, like a hub
                if scope in below:                                           # one entry per directory, however many files changed
                    below[scope]["files"] += 1
                    continue
                below[scope] = {"module": f, "importers": len(under), "below": scope, "files": 1}
                hubs.append(below[scope])
                continue
            for t in under:
                add(t, "below %s ← %s" % (scope + "/" if scope else "./", f))
    for t in always:                                                         # (f)
        add(t, "always")
    files = sorted(reasons)
    tip_sorted, base_sorted = sorted(tip_files), sorted(base_files)
    return {"files": files, "reasons": reasons, "changed": len(changed), "hubs": hubs[:MAX_HUBS], "hubs_more": max(0, len(hubs) - MAX_HUBS),
            "fallback": None if files else sel["fallback"],
            "on_tip": [f for f in files if _has(f, tip_sorted, tip_files)], "on_base": [f for f in files if _has(f, base_sorted, base_files)],
            "tip": tip, "base": mb}


def selection(project: Any, base_sha: Optional[str], tip_sha: str) -> Dict[str, Any]:
    """The test files a trial's changes select, from ``git diff --name-only <merge-base> <tip>``, with why each was chosen.

    ``base_sha``: the base (or the commit the branch left it from; the merge-base is worked out either way), None for no baseline.
    Returns {files, reasons {file: [why]}, changed (how many files changed), hubs [{module, importers}] (left to ``always``, only
    when it selects a file on either commit; a conftest.py or test helper of rule e also has ``below``, its directory, ``importers`` counting the test files
    below it, and ``files`` the changed files there: one entry per directory), hubs_more (hubs past the first MAX_HUBS, not listed),
    fallback (when nothing was selected: always | all | skip), on_tip / on_base (the files each side has), tip, base}."""
    s = settings(project)
    tip = _sha(project.git, tip_sha) or tip_sha
    base = _sha(project.git, base_sha) if base_sha else None
    return _select(project, s, _merge_base(project.git, base, tip) or base, tip)


# ------------------------------------------------------------------------------------------------------------------- what is run
def _is_pytest(command: str) -> bool:
    return bool(_PYTEST.search(command or ""))


def _plan(s: Dict[str, Any], sel: Optional[Dict[str, Any]], sha: str, side: str) -> List[Item]:
    """The checks of one side: the tests (with the selected files this side has) and every measure."""
    items: List[Item] = []
    cmd = s["command"]
    if cmd:
        item: Item = {"name": "tests", "kind": "tests", "pattern": None, "timeout": s["timeout_s"]}
        if not s["uses_selection"]:
            item["command"] = cmd
        elif not sel["files"]:
            if sel["fallback"] == "all":
                item["command"] = cmd.replace("{tests}", "").strip()
            else:
                item["none"] = "nothing selected (fallback %s)" % sel["fallback"]
        else:
            files = sel["on_tip" if side == "tip" else "on_base"]
            if files:
                item["command"] = cmd.replace("{tests}", " ".join(shlex.quote(f) for f in files))
            elif side == "tip" and sel["on_base"]:                           # the trial deleted them all: its tests are gone, not passing
                n = len(sel["on_base"])
                item.update(failed="the trial removed every selected test file (%d on the base)" % n, zero=True)
            else:                                                            # the trial added them all: zero tests here
                item.update(none="no selected test file exists on %s" % sha[:7], zero=True)
        items.append(item)
    for m in s["measures"]:
        items.append({"name": m["name"], "kind": "measure", "command": m["command"], "pattern": m["pattern"], "unit": m["unit"],
                      "better": m["better"], "timeout": m["timeout_s"], "tolerance_pct": m["tolerance_pct"]})
    return items


def _stamp(path: str) -> Optional[List[Any]]:
    """[path, size (None for a directory), mtime in ns], or None when it is not there."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [path, st.st_size if stat.S_ISREG(st.st_mode) else None, st.st_mtime_ns]


def _shebang(path: str) -> Optional[str]:
    """The interpreter a script's ``#!`` line names (``#!/usr/bin/env python3``: ``python3``, to be found on PATH), else None."""
    try:
        with open(path, "rb") as f:
            head = f.read(256)
    except OSError:
        return None
    words = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split() if head.startswith(b"#!") else []
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    return words[0] if words else None


def _site_dirs(python: str) -> List[str]:
    """Where a Python finds its packages, read from the paths alone (never run): ``<prefix>/lib/pythonX.Y/site-packages`` of the
    path as named (a venv's) and of the binary it resolves to (the installation's), Debian's ``dist-packages``, and the user's
    ``~/.local/lib/pythonX.Y/site-packages``. X.Y comes from the binary's name (``python`` resolves to ``python3.12``); only without
    one is ``lib/`` listed (a conda ``lib/`` holds thousands of files: listing it on every page poll would not be cheap)."""
    named = [m.group(0) for m in (re.match(r"(?:python|pypy)\d+\.\d+", os.path.basename(p)) for p in (python, os.path.realpath(python))) if m]
    out: List[str] = []
    for exe in dict.fromkeys((os.path.abspath(python), os.path.realpath(python))):
        prefix = os.path.dirname(os.path.dirname(exe))
        for lib in (os.path.join(prefix, "lib"), os.path.join(prefix, "local", "lib")):
            subs = list(dict.fromkeys(named))
            if not subs:
                try:
                    subs = sorted(n for n in os.listdir(lib) if _PYTHON_NAME.match(n))
                except OSError:
                    subs = []
            for sub in subs + ["python3"]:                                # python3: Debian's /usr/lib/python3/dist-packages
                for leaf in ("site-packages", "dist-packages"):
                    d = os.path.join(lib, sub, leaf)
                    if os.path.isdir(d):
                        out.append(os.path.realpath(d))
    for sub in dict.fromkeys(named or [m.group(1) for m in (re.search(r"/lib/((?:python|pypy)\d+\.\d+)/", d) for d in out) if m]):
        user = os.path.expanduser("~/.local/lib/%s/site-packages" % sub)
        if os.path.isdir(user):
            out.append(os.path.realpath(user))
    return list(dict.fromkeys(out))


def _environment(command: Optional[str], path: Optional[str]) -> List[List[Any]]:
    """A cheap print of what a command runs from outside the commit, for its cache key (a package upgraded in the same environment
    kept a result cached before the upgrade: a false delta, with no warning): each program the command names — an absolute path, or
    a bare name found on ``path`` (the PATH the command gets) —, the interpreter of a script's ``#!`` line, and a Python's
    site-packages directories, each as ``_stamp``. ``pip install`` / ``uninstall`` adds and removes a package's files and dist-info
    there, so the directory's mtime moves; a file edited in place inside it does not, nor anything else the tests read from the host
    (``extra_ro``, ``ro_binds``, ``copies``): after such a change, ``--force``. Not programs: a module named after ``-m``, a relative
    path, an option, an assignment (``PYTHONPATH=src``), a variable, the ``{tests}`` place. The words are read two ways: at every
    space and shell operator with the quotes dropped (a command inside quotes, ``bash -c 'pytest …'``), and by the shell's own rules
    (``shlex``: a quoted path with a space in it is one word, review of round 3), and a word that is itself a command (the string of
    ``bash -c '…'``) read again that way (round 4)."""
    lists = [[w.strip("'\"") for w in _WORDS.split(command or "")]]
    todo, seen_text = [command or ""], set()
    while todo and len(lists) < 1 + MAX_PROGRAMS:                          # and so a command inside quotes (bash -c '…'), the same way
        text = todo.pop(0)
        seen_text.add(text)
        try:
            lex = shlex.shlex(text, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            words = list(lex)
        except ValueError:                                                 # an unclosed quote: not read this way
            continue
        lists.append(words)
        todo += [w for w in words if any(c.isspace() for c in w) and w not in seen_text]
    programs: List[str] = []
    for words in lists:
        for i, w in enumerate(words):
            if not w or (i and words[i - 1] == "-m") or w[0] in "-{$" or "=" in w or ":" in w or w.isdigit():
                continue
            w = os.path.expanduser(w) if w.startswith("~/") else w
            if w.startswith("/"):
                exe: Optional[str] = w if os.path.isfile(w) and os.access(w, os.X_OK) else None
            else:
                exe = None if "/" in w else shutil.which(w, path=path)
            if exe and exe not in programs and len(programs) < MAX_PROGRAMS:
                programs.append(exe)
    out: List[List[Any]] = []
    seen: Set[str] = set()

    def add(p: Optional[str]) -> None:
        st = _stamp(p) if p and p not in seen else None
        if st:
            seen.add(p)
            out.append(st)
    for exe in programs:
        named = _shebang(exe)
        for t in [exe] + ([named if named.startswith("/") else shutil.which(named, path=path)] if named else []):
            if not t:
                continue
            add(os.path.realpath(t))                                           # the file that runs (a link moved elsewhere: another one)
            if _PYTHON_NAME.match(os.path.basename(t)) or _PYTHON_NAME.match(os.path.basename(os.path.realpath(t))):
                for d in _site_dirs(t):
                    add(d)
    return out


def _path_of(sb: Dict[str, Any]) -> Optional[str]:
    """The PATH a check's command gets: ``sandbox.env``'s, else dtree's own (the sandbox keeps it)."""
    return str((sb.get("env") or {}).get("PATH") or os.environ.get("PATH") or "") or None


def _env_print(project: Any, s: Dict[str, Any]) -> str:
    """``_environment`` of every command the settings name, short: kept in the ``verified`` event, so the compare row can say the
    installation changed since (``env_changed``)."""
    path = _path_of(sandbox.project_settings(project))
    data = [_environment(c, path) for c in [s["command"]] + [m["command"] for m in s["measures"]] if c]
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _key(sha: str, item: Item, s: Dict[str, Any], sb: Dict[str, Any]) -> str:
    """A check's cache key; never the per-run paths (worktree, output directory), which differ every time. ``env``: the programs
    the command runs, as installed now (``_environment``)."""
    box = {k: sb.get(k) for k in KEY_SANDBOX} if s["sandbox"] == "bwrap" else {k: sb.get(k) for k in ("env", "copies")}
    data = {"sha": sha, "name": item["name"], "kind": item["kind"], "command": item["command"], "pattern": item.get("pattern"),
            "sandbox": s["sandbox"], "box": box, "verify": VERSION, "env": _environment(item["command"], _path_of(sb))}
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _alive(pid: Any) -> bool:
    """Whether a check's runner still runs: this process, or a live dtree process (not a zombie, not a pid reused by another program)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return b"dtree" in Path("/proc/%d/cmdline" % pid).read_bytes()
    except OSError:
        return True


def _proc_start(pid: int) -> Optional[str]:
    """A process's start time (clock ticks since boot, /proc/PID/stat field 22): with the pid it names one process, not its reuse."""
    try:
        data = Path("/proc/%d/stat" % int(pid)).read_text()
    except (OSError, ValueError):
        return None
    rest = data[data.rfind(")") + 2:].split()
    return None if len(rest) < 20 or rest[0] in ("Z", "X") else rest[19]


class _Setup(Exception):
    """A side's checks could not start (worktree, copies, sandbox): every claimed check ends ``error`` with this."""


def _rmtree(path: Path) -> bool:
    """Remove a tree the code under test wrote in (the verification worktree, the JUnit directory): it can leave a directory
    read-only or unreadable, which rmtree alone cannot empty, and links to anything of the host's. So every real directory in the tree
    gets mode 0700 first, found without following a link and checked with lstat (this runs outside the sandbox: a chmod through a
    link would change the host file it names), then a plain rmtree, which unlinks links. Whether the tree is gone."""
    p = str(path)
    if os.path.islink(p):
        os.unlink(p)
        return True
    if not os.path.lexists(p):
        return True

    def writable(d: str) -> None:
        try:
            if stat.S_ISDIR(os.lstat(d).st_mode):
                os.chmod(d, 0o700)
        except OSError:
            pass
    writable(p)
    for dirpath, dirnames, _ in os.walk(p, followlinks=False):       # top-down: each directory is opened after its mode is set
        for d in dirnames:                                            # (a link to a directory is listed here, and left alone)
            writable(os.path.join(dirpath, d))
    shutil.rmtree(p, ignore_errors=True)
    return not os.path.lexists(p)


def _registered(g: Any, path: Path) -> bool:
    """Whether git has a worktree entry for ``path`` (``worktree list --porcelain -z``; whatever language git speaks)."""
    try:
        r = g.run("worktree", "list", "--porcelain", "-z", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    names = {str(path), os.path.realpath(str(path))}
    return r.returncode == 0 and any(f[len("worktree "):] in names for f in r.stdout.split("\0") if f.startswith("worktree "))


@contextlib.contextmanager
def _checkout(project: Any, sha: str, workdir: Path, say: Callable[[str], None] = lambda line: None) -> Iterator[Path]:
    """A fresh detached worktree of ``sha`` in ``workdir`` (the user's git hooks do not run for it), removed afterwards whatever
    happens. Never a ``git worktree prune``: it is project-wide, and would drop a person's worktree whose directory is away at that
    moment (a disk not mounted), with its index. Only this worktree's own entry is touched: when a directory deleted by hand (a reboot
    emptying /dev/shm) left an entry at this path, it is added again with ``-f``; when git cannot remove the tree (the tests rewrote
    its ``.git``, or left a directory read-only), dtree removes it and then the entry, by its path. A failure to clean up is told,
    never raised: the checks have ended by then and their results are kept."""
    g = project.git
    workdir.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=sha[:10] + "-", dir=str(workdir)))
    try:
        add = ["-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", "--quiet"]
        r = g.run(*add, str(path), sha, timeout=900)
        if r.returncode != 0 and _registered(g, path):                # "missing but already registered": this path, nobody else's
            r = g.run(*add, "-f", str(path), sha, timeout=900)
        if r.returncode != 0:
            raise _Setup("git worktree add failed: %s" % (r.stderr or r.stdout).strip()[:500])
        yield path
    finally:
        try:
            g.run("worktree", "remove", "--force", str(path), timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if os.path.lexists(path):                                 # git stops at a directory the tests left read-only
                if not _rmtree(path):
                    say("⚠ verify: %s could not be removed completely" % path)
                g.run("worktree", "remove", "--force", str(path), timeout=120)   # its directory gone: its entry goes, nobody else's
        except Exception as exc:                                      # never lose the checks' results to the cleanup
            say("⚠ verify: cleaning up %s failed: %s: %s" % (path, type(exc).__name__, exc))


def _tree_size(path: Path, skip: Callable[[Path], bool]) -> int:
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not skip(Path(root) / d)]
        for f in files:
            if not skip(Path(root) / f):
                try:
                    total += (Path(root) / f).lstat().st_size
                except OSError:
                    pass
    return total


def _copy_in(project: Any, sb: Dict[str, Any], wt: Path, say: Callable[[str], None]) -> None:
    """``sandbox.copies``: git-ignored paths of the main checkout copied into the verification worktree (writable there, thrown away
    with it): only where the commit has nothing, never the secrets, and not when the copy would fill the disk."""
    hidden = {str(x).strip("/") for x in list(sb.get("secrets") or []) + [".dtree/write_token"]}
    root = project.root.resolve()
    skip = lambda p: os.path.relpath(str(p), str(root)) in hidden
    for rel in sb.get("copies") or []:
        rel = str(rel).strip("/")
        if not rel or ".." in Path(rel).parts or rel in hidden:
            continue
        src, dst = root / rel, wt / rel
        if not os.path.lexists(src):
            say("· verify copy %s: not in the main checkout, skipped" % rel)
            continue
        if os.path.lexists(dst):
            say("· verify copy %s: the commit has it, not copied" % rel)
            continue
        # the commit is the agent's: a link (or a file) on the way would send this copy, made outside the sandbox, elsewhere
        on_the_way = [wt.joinpath(*Path(rel).parts[:i]) for i in range(1, len(Path(rel).parts))]
        if any(p.is_symlink() or (os.path.lexists(p) and not p.is_dir()) for p in on_the_way):
            say("· verify copy %s: the commit has a link or a file on its way, not copied" % rel)
            continue
        need, free = _tree_size(src, skip), shutil.disk_usage(str(wt)).free
        if need + COPY_MARGIN > free:
            raise _Setup("not enough space to copy %s into the verification worktree: %.1f MB needed, %.1f MB free (see verify.workdir)"
                         % (rel, need / 1e6, free / 1e6))
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(str(src), str(dst), symlinks=True, ignore=lambda d, names: [n for n in names if skip(Path(d) / n)])
        else:
            shutil.copy2(str(src), str(dst), follow_symlinks=False)


def _bound(value: Any) -> int:
    """A count as dtree keeps it: a whole number from 0 to MAX_COUNT, whatever the code under test wrote ("1e400", "nan", 10**30)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return min(max(value, 0), MAX_COUNT)
    try:
        n = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return 0 if n != n else int(min(max(n, 0.0), float(MAX_COUNT)))


def _bounded(counts: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return None if counts is None else dict(counts, **{k: _bound(counts[k]) for k in COUNT_KEYS if counts.get(k) is not None})


def _junit_counts(path: Path) -> Optional[Dict[str, Any]]:
    """Counts from pytest's JUnit file: the leaf ``<testsuite>`` attributes summed (tests, failures, errors, skipped), with xfails
    taken out of skipped. The code under test can write in that directory: a link is not followed (it could name any file of the
    host, read here outside the sandbox), and a document with a DOCTYPE or entities is not parsed."""
    try:                                                    # O_NONBLOCK: a FIFO put there does not hang the open
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > JUNIT_CAP:
            return None
        with os.fdopen(fd, "rb") as f:
            fd = -1
            data = f.read(JUNIT_CAP + 1)
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > JUNIT_CAP or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    suites = [e for e in root.iter("testsuite") if e.find("testsuite") is None]
    if not suites:
        return None

    tests, failures, errors, skipped = (_bound(sum(_bound(e.get(k)) for e in suites)) for k in ("tests", "failures", "errors", "skipped"))
    xfailed = _bound(sum(1 for e in suites for x in e.iter("skipped") if x.get("type") == "pytest.xfail"))
    failing = ["%s::%s" % (c.get("classname") or "", c.get("name") or "") for e in suites for c in e.iter("testcase")
               if c.find("failure") is not None or c.find("error") is not None]
    return {"framework": "pytest", "passed": max(0, tests - failures - errors - skipped), "failed": failures, "errors": errors,
            "skipped": max(0, skipped - xfailed), "xfailed": xfailed, "xpassed": None, "total": tests, "failing": failing[:5],
            "summary_line": None}


def _known(counts: Optional[Dict[str, Any]]) -> bool:
    return bool(counts) and any(counts.get(k) is not None for k in ("total", "passed", "failed"))


_NO_PYTEST = re.compile(r"No module named pytest\b|\bpytest: (?:command )?not found")


def _classify(r: Dict[str, Any], counts: Optional[Dict[str, Any]], pytest_cmd: bool, text: str = "", command: str = "") -> Tuple[str, str]:
    """A tests run's status from how it ended and what it counted. ``error`` is "could not verify" (the sandbox, a usage problem,
    nothing ran, a pytest that did not finish): never the code's fault, and never reused. ``failed`` is: failing tests, a crash, a
    collection error. A pipe's exit status is its last command's (``pytest | tail`` ends 0 when pytest segfaults or is killed), so an
    ``ok`` pytest run passes only with counts."""
    state, code, known = r["state"], r.get("exit_code"), _known(counts)
    if pytest_cmd and not known and state == "failed" and _NO_PYTEST.search(text[-4096:]):
        return "error", "pytest is not installed for the interpreter the command runs (inside the sandbox)"
    bad = known and ((counts.get("failed") or 0) + (counts.get("errors") or 0)) > 0
    if state == "error":
        return "error", r.get("error") or "the sandbox did not start"
    if state in ("timeout", "stopped"):
        return state, "longer than the time limit" if state == "timeout" else "stopped"
    if state == "failed" and code in SIGKILLED:                      # the OOM killer or a person: not the code's verdict, not kept
        return "error", KILLED
    if pytest_cmd and code == 5:
        return "error", "pytest found no tests to run (exit 5)"
    if pytest_cmd and code in (3, 4):
        return "error", "pytest could not run (exit %d: %s)" % (code, "internal error" if code == 3 else "usage error")
    if code in (126, 127) and not known:
        return "error", "the command could not start (exit %d)" % code
    if bad:
        return "failed", ""
    if known and counts.get("total") == 0:
        return "error", "no tests ran"
    if pytest_cmd and not known and state == "ok":
        return "error", OWN_FLAGS if _OWN_FLAGS.search(command) else UNCOUNTED
    if state == "ok":
        return "passed", ""
    if pytest_cmd and code == 2 and not known:
        return "error", "pytest was interrupted before it counted anything (exit 2)"
    return "failed", "exit %s%s" % (code, " with no failing test" if known else "")


def _counts_line(c: Dict[str, Any]) -> str:
    t = "%d passed, %d failed" % (c.get("passed") or 0, c.get("failed") or 0)
    for k in ("errors", "skipped", "xfailed", "xpassed"):
        if c.get(k):
            t += ", %d %s" % (c[k], k)
    return t


def _output(log: Path, offset: int, r: Dict[str, Any]) -> str:
    """The command's output from its log (its end, up to READ_CAP), after dtree's own header; head and tail when the log was cut."""
    if r.get("bytes", 0) <= sandbox.LOG_CAP_BYTES:
        try:
            size = log.stat().st_size
            with open(log, "rb") as f:
                f.seek(max(offset, size - READ_CAP))
                return f.read().decode("utf-8", "replace")
        except OSError:
            pass
    return (r.get("head", "") + "\n" + r.get("tail", "")) if r.get("truncated") else r.get("tail", "")


def _value(pattern: str, text: str) -> Tuple[Optional[float], str]:
    """The number a measure's pattern reads: group ``value`` (else group 1, else the whole match) of its LAST match."""
    rx, last = re.compile(pattern, re.M), None
    for last in rx.finditer(text):
        pass
    if last is None:
        return None, "the pattern did not match the output"
    raw = last.group("value") if "value" in rx.groupindex else last.group(1) if rx.groups else last.group(0)
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None, "%r is not a number" % (raw,)
    if not math.isfinite(value):                                     # inf / nan would end up as Infinity / NaN: not JSON
        return None, "%r is not a finite number" % (raw if len(str(raw)) <= 40 else str(raw)[:37] + "...",)
    return value, ""


def _failed_to_start(exc: BaseException) -> Dict[str, Any]:
    return {"state": "error", "exit_code": None, "error": "the command did not start: %s" % exc, "seconds": 0, "tail": "", "head": "", "bytes": 0}


class _Runner:
    """One side's claimed checks in one fresh worktree: the tests first, then the measures (one run for each distinct command)."""

    def __init__(self, project: Any, s: Dict[str, Any], sha: str, say: Callable[[str], None], stopped: Callable[[], bool]) -> None:
        self.p, self.s, self.sha, self.say, self.stopped = project, s, sha, say, stopped
        self.sb = sandbox.project_settings(project)
        self.logs = project.path(project.config["logs"])
        self.mounts: Optional[List[Any]] = None                            # sandbox.project_mounts of the fresh worktree (run)

    def _env(self, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        """The project's ``sandbox.env`` and dtree's own variables for this command (pytest's flags go after the project's own)."""
        env = sandbox.project_env(self.p)
        for k, v in (extra or {}).items():
            env[k] = (env[k] + " " + v) if k == "PYTEST_ADDOPTS" and env.get(k) else v
        return env

    def _box(self, wt: Path, out: Optional[Path], env: Dict[str, str]) -> Optional[Dict[str, Any]]:
        if self.s["sandbox"] == "none":
            return None
        if self.mounts is None:
            self.mounts = sandbox.project_mounts(self.p, wt)
        mounts = list(self.mounts) + ([("rw", str(out))] if out else [])
        return {"mounts": mounts, "network": "none", "root": self.sb["root"], "nested": self.sb["nested"], "env": env}

    def _run(self, command: str, wt: Path, log: Path, timeout: float, out: Optional[Path],
             extra: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, Any], int]:
        env = self._env(extra)
        box = self._box(wt, out, env)
        argv: Any = command
        if box is None and env:                                             # unsandboxed: the project's variables all the same
            argv = ["env"] + ["%s=%s" % kv for kv in env.items()] + ["/bin/sh", "-c", command]
        self.logs.mkdir(parents=True, exist_ok=True)
        where = "no sandbox" if box is None else "bwrap, %s root, no network" % box["root"]
        added = "".join("# %s=%s\n" % (k, env[k]) for k in (extra or {}))
        with open(log, "ab") as f:
            f.write(("# dtree verify · %s · %s · %s\n%s$ %s\n\n" % (self.sha, where, time.strftime("%Y-%m-%d %H:%M:%S"), added, command)).encode("utf-8"))
            offset = f.tell()
        started: Dict[str, int] = {}
        try:
            r = sandbox.run(argv, wt, log, timeout, sandbox=box, stopped=self.stopped, on_start=lambda pid: started.update(pid=pid))
        except BaseException:
            if started.get("pid"):                                          # a ^C here: the sandbox must not outlive its worktree
                sandbox.end_group(started["pid"], grace_s=0)
            raise
        return r, offset

    def tests(self, wt: Path, item: Item, row: Dict[str, Any]) -> None:
        cid, command = row["id"], item["command"]
        log, pyt = self.logs / ("check-%d.log" % cid), _is_pytest(item["command"])
        out = Path(tempfile.mkdtemp(prefix="out-%d-" % cid, dir=str(self.s["workdir"])))
        try:
            # through the environment, never appended to the command: it can be a pipe (`pytest | tail`), a subshell, `cd sub && pytest`
            extra = {"PYTEST_ADDOPTS": "--junitxml=%s -p no:cacheprovider" % shlex.quote(str(out / "junit.xml"))} if pyt else None
            self.p.store.check_set(cid, log=str(log))                       # it starts now: a runner waiting for it sees it move
            try:
                r, offset = self._run(command, wt, log, item["timeout"], out, extra)
            except OSError as exc:
                r, offset = _failed_to_start(exc), 0
            text = _output(log, offset, r)
            printed = parse_summary(text, r.get("exit_code"))
            counts = _bounded((_junit_counts(out / "junit.xml") if pyt else None) or (printed if printed["framework"] != "unknown" else None))
        finally:
            # told, never raised, like the worktree's (_checkout): the check keeps its result (os.walk recurses on Python 3.9, so a
            # tree some 1000 levels deep that the tests leave in the output directory raises RecursionError)
            try:
                if not _rmtree(out):
                    self.say("⚠ verify: %s could not be removed completely" % out)
            except Exception as exc:
                self.say("⚠ verify: cleaning up %s failed: %s: %s" % (out, type(exc).__name__, exc))
        status, why = _classify(r, counts, pyt, text, command)
        c = counts or {}
        line = printed.get("summary_line") or (("junit: " + _counts_line(c)) if _known(counts) else "")
        failing = ("failing: " + ", ".join(c["failing"])) if c.get("failing") else ""
        self.p.store.check_set(cid, status=status, ended_at=time.time(), exit_code=r.get("exit_code"), framework=c.get("framework"),
                               summary=("; ".join(x for x in (line, why, failing) if x) or status)[:SUMMARY_CHARS], tail=r.get("tail", ""),
                               log=str(log), seconds=r.get("seconds"), **{k: c.get(k) for k in COUNT_KEYS})

    def measures(self, wt: Path, group: Claimed) -> None:
        command = group[0][0]["command"]
        log = self.logs / ("check-%d.log" % group[0][1]["id"])
        for _, row in group:
            self.p.store.check_set(row["id"], log=str(log))                 # they start now (see tests)
        try:
            r, offset = self._run(command, wt, log, max(it["timeout"] for it, _ in group), None)
        except OSError as exc:
            r, offset = _failed_to_start(exc), 0
        killed = r["state"] == "failed" and r.get("exit_code") in SIGKILLED    # did not finish, like a timeout: no value, not kept
        state = "error" if killed else r["state"]
        text = _output(log, offset, r)
        for item, row in group:                                             # one run feeds every pattern of the same command
            value, why = _value(item["pattern"], text) if state in ("ok", "failed") else (None, KILLED if killed else r.get("error") or state)
            status = ("passed" if value is not None else "error") if state == "ok" else state   # failed keeps the value it printed
            said = ("%s = %g %s" % (item["name"], value, item.get("unit") or "")).strip() if value is not None else ""
            if r["state"] == "failed":
                said += ("; " if said else "") + "exit %s" % r.get("exit_code")
            self.p.store.check_set(row["id"], status=status, ended_at=time.time(), exit_code=r.get("exit_code"), value=value, unit=item.get("unit") or None,
                                   summary=("; ".join(x for x in (said, why) if x) or status)[:SUMMARY_CHARS], tail=r.get("tail", ""), log=str(log),
                                   seconds=r.get("seconds"))

    def run(self, mine: Claimed) -> None:
        """Every claimed check ends here one way or another: a check left running would make the others wait for it."""
        store = self.p.store
        try:
            if self.stopped():
                raise _Setup("stopped before it ran")
            if self.s["sandbox"] == "bwrap":
                ok, why = sandbox.check()
                if not ok:
                    raise _Setup("the sandbox cannot run here: %s (\"sandbox\": \"none\" in verify runs without it, if you accept that)" % why)
            with _checkout(self.p, self.sha, self.s["workdir"], self.say) as wt:
                _copy_in(self.p, self.sb, wt, self.say)
                # the mounts are read once, from the commit as checked out, before anything runs: the tests write in the worktree and
                # could otherwise choose what the measures' sandbox shows (a .git file naming another repository) or hide a ro_bind
                # (the mount point bwrap leaves there looks like the commit's own file)
                self.mounts = sandbox.project_mounts(self.p, wt) if self.s["sandbox"] == "bwrap" else None
                for item, row in mine:
                    if item["kind"] == "tests":
                        self.tests(wt, item, row)
                groups: Dict[str, Claimed] = {}
                for item, row in mine:
                    if item["kind"] == "measure":
                        groups.setdefault(item["command"], []).append((item, row))
                for group in groups.values():
                    if self.stopped():
                        raise _Setup("stopped before it ran")
                    self.measures(wt, group)
        except (_Setup, OSError, ValueError, ArithmeticError, subprocess.TimeoutExpired, DecisionError) as exc:
            why = str(exc) or type(exc).__name__
            for _, row in mine:
                if store.check(row["id"])["status"] == "running":
                    store.check_set(row["id"], status="stopped" if why.startswith("stopped") else "error", ended_at=time.time(), summary=why[:SUMMARY_CHARS])
        finally:
            for _, row in mine:
                if store.check(row["id"])["status"] == "running":
                    store.check_set(row["id"], status="error", ended_at=time.time(), summary="verification ended before this check ran")


def _wait(store: Any, row: Dict[str, Any], limit: float, stopped: Callable[[], bool]) -> Optional[Dict[str, Any]]:
    """Another runner has this check: its result when it ends; None when that runner is gone (the caller claims it again).

    A runner claims a side's checks at once and runs them in turn, so a check can stay ``running`` long before its turn (a baseline
    measure a race shares waits behind the claiming trial's own baseline tests): the wait lasts while that runner moves on. Each of
    its checks ends within ``limit`` (the longest time limit of the settings) of the one before, and each start and end is a write to
    its rows; when none of its rows on this commit has moved for ``limit`` + WAIT_GRACE_S, it is stuck, and the wait ends ``error``
    saying so. What the wait makes up (that, or this trial's own stop) carries ``waited``: this trial's result, never the check's,
    and never a ``timeout`` the check did not have."""
    t0 = time.time()
    while True:
        cur = store.check(row["id"])
        if cur["status"] != "running":
            return cur
        if not _alive(cur["runner_pid"]):
            return None
        if stopped():
            return dict(cur, status="stopped", waited=True, summary="stopped while waiting for check %d" % cur["id"])
        moved = max([t0] + [float(r.get("updated") or 0) for r in store.checks(sha=cur["sha"], latest=False)
                            if r.get("runner_pid") == cur["runner_pid"]])
        now = time.time()
        if now - moved > float(limit) + WAIT_GRACE_S:
            return dict(cur, status="error", waited=True, summary="waited %d s for check %d: its runner (pid %s) still runs but has not moved "
                        "for %d s (stuck?); verify again when it has ended" % (now - t0, cur["id"], cur["runner_pid"], now - moved))
        time.sleep(WAIT_POLL_S)


def _run_side(project: Any, s: Dict[str, Any], sha: str, items: List[Item], by: str, force: bool, say: Callable[[str], None],
              stopped: Callable[[], bool]) -> Dict[str, Dict[str, Any]]:
    """One commit's check results by name: reused when they ended passed or failed, waited for when another runner has them, run
    here otherwise."""
    store, sb = project.store, sandbox.project_settings(project)
    out: Dict[str, Dict[str, Any]] = {}
    decided = lambda i: bool(i.get("none") or i.get("failed"))             # known from the selection alone: nothing to run
    for item in items:
        if decided(item):
            zero = dict.fromkeys(("passed", "failed", "errors", "skipped", "total"), 0) if item.get("zero") else {}
            out[item["name"]] = dict(zero, id=None, status="failed" if item.get("failed") else "none", summary=item.get("failed") or item["none"],
                                     seconds=0)
        else:
            item["key"] = _key(sha, item, s, sb)
    pending = [i for i in items if not decided(i)]
    longest = max([float(s["timeout_s"])] + [float(m["timeout_s"]) for m in s["measures"]])   # any one check of a runner, at most
    for attempt in range(5):
        mine, waiting = [], []
        for item in pending:
            row = store.check_claim(item["key"], sha, item["name"], item["kind"], item["command"], by=by, runner_pid=os.getpid(), alive=_alive,
                                    force=force and attempt == 0)
            if row["claimed"]:
                mine.append((item, row))
            elif row["status"] == "running":
                waiting.append((item, row))
            else:
                out[item["name"]] = dict(row, cached=True)
        if mine:
            _Runner(project, s, sha, say, stopped).run(mine)
            for item, row in mine:
                out[item["name"]] = store.check(row["id"])
        pending = []
        for item, row in waiting:
            say("⏳ verify %s on %s: check %d runs in process %s, waiting for its result" % (item["name"], sha[:7], row["id"], row["runner_pid"]))
            got = _wait(store, row, longest, stopped)
            # its runner is gone, was stopped by its own job (another trial's stop, not this one's), or ended in an error or a timeout,
            # which are never reused (the OOM killer, that runner's crash, a machine busy with a race): claim it again and run it here,
            # once, as a moment later it would be. What the wait made up (this trial's stop, a stuck runner) is this trial's own.
            if got is None or (not got.get("waited") and got["status"] in ("stopped", "error", "timeout") and not stopped()):
                pending.append(item)
            else:
                out[item["name"]] = got if got.get("waited") else dict(got, shared=True)
        if not pending:
            break
    for item in pending:                                                     # their runners kept disappearing
        out[item["name"]] = {"id": None, "status": "error", "summary": "the runner of this check kept disappearing", "seconds": 0}
    return out


# ------------------------------------------------------------------------------------------------------------------------ summaries
SIDE_KEYS = ("status", "passed", "failed", "errors", "skipped", "xfailed", "xpassed", "total", "seconds", "summary", "framework")


def _side(res: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if res is None:
        return None
    d = {k: res.get(k) for k in SIDE_KEYS}
    d.update(check=res.get("id"), cached=bool(res.get("cached")), shared=bool(res.get("shared")))
    return d


def _delta(t: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    """Trial minus baseline, only when both ran to the end (partial counts of a timeout compare nothing)."""
    ok = ("passed", "failed", "none")
    if not t or not b or t["status"] not in ok or b["status"] not in ok or t.get("passed") is None or b.get("passed") is None:
        return None
    return {k: (t.get(k) or 0) - (b.get(k) or 0) for k in ("passed", "failed", "errors")}


def _measure(item: Item, t: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    v, bv = (t or {}).get("value"), (b or {}).get("value")
    d = {"name": item["name"], "unit": item.get("unit") or "", "better": item.get("better"), "value": v, "base": bv, "delta": None,
         "delta_pct": None, "verdict": None, "status": (t or {}).get("status") or "none", "base_status": b.get("status") if b else None,
         "check": (t or {}).get("id"), "base_check": b.get("id") if b else None, "summary": (t or {}).get("summary")}
    if v is not None and bv is not None:
        delta = v - bv
        pct = delta / abs(bv) * 100.0 if bv else None                    # two finite numbers can still overflow (1e308 - -1e308)
        d["delta"] = round(delta, 12) if math.isfinite(delta) else None
        d["delta_pct"] = round(pct, 3) if pct is not None and math.isfinite(pct) else None
        if item.get("better"):
            pct = abs(d["delta_pct"]) if d["delta_pct"] is not None else (0.0 if v == bv else float("inf"))
            d["verdict"] = "same" if v == bv or pct <= (item.get("tolerance_pct") or 0) else (
                "better" if (v > bv) == (item["better"] == "higher") else "worse")
    return d


def _text(side: Optional[Dict[str, Any]]) -> str:
    if not side:
        return "-"
    if side.get("passed") is not None and side["status"] != "none" and side.get("check") is not None:   # counted by a run
        t = _counts_line(side)
        return t if side["status"] in ("passed", "failed") else "%s · %s" % (side["status"], t)
    return side["status"] + ((": " + str(side["summary"])[:160]) if side.get("summary") and side["summary"] != side["status"] else "")


def _overall(tests: Optional[Dict[str, Any]], tip: Dict[str, Dict[str, Any]], measures: List[Dict[str, Any]]) -> str:
    if any(r.get("status") == "stopped" for r in tip.values()):
        return "stopped"
    if tests and tests["status"] != "none":
        return tests["status"]
    states = [m["status"] for m in measures]
    return next((st for st in ("timeout", "error", "failed", "passed") if st in states), "none")


def _mark(project: Any, tid: int, pid: int, sha: Optional[str] = None) -> Path:
    """A verification of a trial in progress, for the page: one small file per process, removed at its end."""
    p = project.path(project.config["logs"]) / ("verify-%d-%d.running" % (int(tid), int(pid)))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"trial": int(tid), "pid": int(pid), "start": _proc_start(pid), "sha": sha, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass
    return p


def _running(project: Any, tid: int) -> List[int]:
    """The live processes verifying trial ``tid``; the marks of dead ones are removed."""
    try:
        marks = list(project.path(project.config["logs"]).glob("verify-%d-*.running" % int(tid)))
    except OSError:
        return []
    pids = []
    for m in marks:
        try:
            info = json.loads(m.read_text(encoding="utf-8"))
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        start = _proc_start(pid)
        if start is not None and (info.get("start") is None or str(info["start"]) == start):
            pids.append(pid)
        elif start is None and not os.path.exists("/proc/self/stat") and _alive(pid):
            pids.append(pid)                                                  # no /proc here: by the pid alone
        else:
            try:
                m.unlink()
            except OSError:
                pass
    return pids


def verify_trial(project: Any, tid: int, by: str = "user", force: bool = False, say: Callable[[str], None] = print,
                 baseline: Optional[bool] = None, stopped: Optional[Callable[[], bool]] = None, auto: bool = False) -> Dict[str, Any]:
    """Run the trial's checks on its branch tip and on its baseline (the merge-base with the base), keep them, write the ``verified``
    event and return the summary {trial, status, sha, base_sha, branch, tests, base, delta, measures, selection, seconds, settings}.

    ``force`` runs every check again instead of reusing it; ``baseline`` None follows ``verify.baseline``; ``stopped`` (a job's stop)
    ends the check running; ``auto`` (after a job) runs only what ``verify`` names: with ``verify.tests`` null, ``test_command`` is
    left to ``dtree verify``. Refused (DecisionError) when verification is off, a setting cannot work or the branch is missing."""
    t0 = time.time()
    s = settings(project)
    if not s["enabled"]:
        raise DecisionError("verification is off: verify.enabled is false in .dtree/config.json")
    plan = dict(s, command=None, uses_selection=False) if auto and s.get("tests") is None else s
    stopped = stopped or (lambda: False)
    store, g = project.store, project.git
    t = store.trial(tid)
    tip = _sha(g, "refs/heads/" + t["branch"])
    if tip is None:
        raise DecisionError("trial %d's branch %s does not exist" % (tid, t["branch"]))
    fork = _merge_base(g, _sha(g, project.base), tip)                         # where the branch left the base: what it changed
    want_base = s["baseline"] if baseline is None else bool(baseline)
    mb = fork if want_base else None
    sel = _select(project, plan, fork, tip) if plan["uses_selection"] else None
    tip_items = _plan(plan, sel, tip, "tip")
    if not tip_items:
        return {"trial": tid, "status": "none", "sha": tip, "base_sha": None, "branch": t["branch"], "tests": None, "base": None, "delta": None,
                "measures": [], "selection": None, "seconds": 0, "summary": "nothing to verify: set verify.tests or test_command, or verify.measures"}
    marker = _mark(project, tid, os.getpid(), tip)
    try:
        cmd = plan["command"]
        what = ("%d test file%s selected" % (len(sel["files"]), "" if len(sel["files"]) == 1 else "s") if sel["files"]
                else "nothing selected, fallback %s" % sel["fallback"]) if sel else (
            "tests: %s" % (cmd if len(cmd) <= 80 else cmd[:79] + "…") if cmd else "no tests")
        say("🔎 verify trial %d (%s) at %s · baseline %s · %s%s" % (tid, t["branch"], tip[:7], mb[:7] if mb else "none" if want_base else "off",
                                                                   what, " · %d measure%s" % (len(s["measures"]), "" if len(s["measures"]) == 1 else "s")
                                                                   if s["measures"] else ""))
        tip_res = _run_side(project, plan, tip, tip_items, by, force, say, stopped)
        base_res = _run_side(project, plan, mb, _plan(plan, sel, mb, "base"), by, force, say, stopped) if mb and not stopped() else {}
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
    tests, base = _side(tip_res.get("tests")), _side(base_res.get("tests")) if mb else None
    measures = [_measure(i, tip_res.get(i["name"]), base_res.get(i["name"]) if mb else None) for i in tip_items if i["kind"] == "measure"]
    status = _overall(tests, tip_res, measures)
    summary = {"trial": tid, "status": status, "sha": tip, "base_sha": mb, "branch": t["branch"], "tests": tests, "base": base,
               "delta": _delta(tests, base), "measures": measures, "seconds": round(time.time() - t0, 1), "settings": _fingerprint(project, s),
               "selection": {"files": len(sel["files"]), "fallback": sel["fallback"], "hubs": sel["hubs"], "hubs_more": sel.get("hubs_more", 0),
                             "changed": sel["changed"]} if sel else None}
    tag = lambda side: " (cached)" if side and side.get("cached") else " (shared)" if side and side.get("shared") else ""
    nobase = " · no baseline" if want_base else " · baseline off"
    if tests:
        d = summary["delta"]
        say("🔎 verify tests on %s: %s%s%s%s · %d s" % (tip[:7], _text(tests), tag(tests), " · base %s: %s%s" % (mb[:7], _text(base), tag(base)) if mb else nobase,
                                                       " · Δ passed %+d, failed %+d" % (d["passed"], d["failed"]) if d else "", summary["seconds"]))
    for m in measures:
        said = ("%g %s" % (m["value"], m["unit"])).strip() if m["value"] is not None else m["status"]
        was = " · base %s: %s" % (mb[:7], "%g" % m["base"] if m["base"] is not None else (m["base_status"] or "-")) if mb else ""
        say("🔎 verify %s on %s: %s%s%s%s" % (m["name"], tip[:7], said, was, " · %+g (%+.1f%%)" % (m["delta"], m["delta_pct"]) if m["delta_pct"] is not None else "",
                                            " " + m["verdict"] if m["verdict"] else ""))
    store.note_event(t["decision_id"], "verified", by, {
        "trial": tid, "sha": tip, "base_sha": mb, "status": status, "tests": _text(tests) if tests else None, "base_tests": _text(base) if base else None,
        "measures": {m["name"]: [m["value"], m["base"]] for m in measures}, "result": {k: summary[k] for k in ("tests", "base", "delta", "measures", "selection")},
        "settings": summary["settings"], "env": _env_print(project, s), "seconds": summary["seconds"]})   # env: as the checks left it
    return summary


# ------------------------------------------------------------------------------------------------------------- the rest of dtree
def after_job(project: Any, job: Dict[str, Any], trial: Dict[str, Any], say: Callable[[str], None]) -> Optional[Dict[str, Any]]:
    """After a build or update job that ended with commits: verify the trial when ``auto_on`` (``verify.enabled``, ``verify.auto``, and
    ``verify.tests`` or ``verify.measures`` set). The job's stop button stops its verification too."""
    if not auto_on(project):
        s = settings(project)
        if s["enabled"] and s["auto"] and s["command"]:              # only test_command: say why nothing runs, and how to have it
            say("· verify: not after jobs with only test_command (often the whole suite): set verify.tests or verify.measures, "
                "or run dtree verify %s" % trial.get("id"))
        return None
    jid = job.get("id")
    stopped = (lambda: bool(project.store.job(jid).get("stop_requested"))) if jid is not None else None
    return verify_trial(project, int(trial["id"]), by="job %s" % jid if jid is not None else "job", say=say, stopped=stopped, auto=True)


def start_background(project: Any, tid: int, by: str = "user", force: bool = False, baseline: Optional[bool] = None,
                     auto: bool = False) -> Optional[int]:
    """Verify trial ``tid`` in a detached process (from the page, ``dtree verify --background``, after an update); returns its pid,
    or None when verification is off. Like ``jobs.launch``: ``python -m dtree.verify run``, dtree's own code first on ``PYTHONPATH``,
    the same store. ``baseline`` False: only the tip (``--no-baseline``); None follows ``verify.baseline``. ``auto`` is the rule of
    jobs, for a verification nobody asked for (merge.py's, after a clean update): None unless ``auto_on``, and the run gets
    ``--auto`` (``verify_trial(auto=True)``: only ``verify.tests`` and ``verify.measures``, a bare ``test_command`` waits for
    ``dtree verify``)."""
    try:
        s = settings(project)
        if not s["enabled"] or (auto and not _auto(s)):
            return None
    except DecisionError:
        pass                                                                 # the runner reports the setting's problem in its log
    project.store.trial(tid)
    logs = project.path(project.config["logs"])
    logs.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (str(PKG_SRC), os.environ.get("PYTHONPATH", "")) if p),
               DTREE_DB=str(project.store.path))
    cmd = [sys.executable, "-m", "dtree.verify", "run", "--trial", str(int(tid)), "--project", str(project.root), "--by", str(by)[:40]]
    cmd += (["--force"] if force else []) + (["--no-baseline"] if baseline is False else []) + (["--auto"] if auto else [])
    with open(logs / ("verify-%d.runner.log" % int(tid)), "a", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=str(PKG_SRC), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
    _mark(project, int(tid), proc.pid)
    return proc.pid


def _last_verified(project: Any, trial: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    events = project.store.get(int(trial["decision_id"])).get("events") or []
    mine = [e for e in events if e["action"] == "verified" and (e.get("detail") or {}).get("trial") == int(trial["id"])]
    return mine[-1] if mine else None


def compare_fields(project: Any, trial: Dict[str, Any]) -> Dict[str, Any]:
    """The trial's row in the compare table: what dtree measured itself, from the latest ``verified`` event (cheap: no git diff, no
    rows scanned), whether the branch moved since (``stale``), whether the settings changed since, and whether it runs now.

    {"verify": {status none|running|passed|failed|error|timeout|stopped, sha, base_sha, stale, tests {passed, failed, errors,
    skipped, total, status, seconds, …}, base {…}, delta {passed, failed, errors}, measures [{name, unit, better, value, base, delta,
    delta_pct, verdict, status}], running, settings_changed, env_changed, checked_at, selection, enabled}}

    ``env_changed``: an interpreter the commands name is installed otherwise now (a package upgraded: ``_environment``), so the
    numbers may not be what would run now; ``settings_changed`` is true then too. A verification older than this field says
    nothing (False)."""
    out: Dict[str, Any] = {"status": "none", "sha": None, "base_sha": None, "stale": False, "tests": None, "base": None, "delta": None,
                           "measures": [], "running": False, "settings_changed": False, "env_changed": False, "checked_at": None,
                           "selection": None, "enabled": True}
    try:
        s = settings(project)
        if not s["enabled"]:
            return {"verify": dict(out, enabled=False)}
        ev = _last_verified(project, trial)
        if ev:
            d = ev.get("detail") or {}
            res = d.get("result") or {}
            env_changed = bool(d.get("env")) and d["env"] != _env_print(project, s)
            out.update(status=d.get("status") or "none", sha=d.get("sha"), base_sha=d.get("base_sha"), checked_at=ev.get("at"),
                       tests=res.get("tests"), base=res.get("base"), delta=res.get("delta"), measures=res.get("measures") or [],
                       selection=res.get("selection"), settings_changed=d.get("settings") != _fingerprint(project, s) or env_changed,
                       env_changed=env_changed)
            tip_now = _sha(project.git, "refs/heads/" + str(trial.get("branch") or ""))
            out["stale"] = bool(tip_now and d.get("sha") and tip_now != d["sha"])
        if _running(project, int(trial["id"])):
            out.update(running=True, status="running")
    except (DecisionError, KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        out.update(status="error", summary="%s: %s" % (type(exc).__name__, exc))
    return {"verify": out}


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    d = {k: v for k, v in row.items() if k not in ("tail", "claimed")}
    if d.get("status") == "running":
        d["runner_alive"] = _alive(d.get("runner_pid"))
    return d


def _rows_of(project: Any, trial: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The check rows of the trial's latest verification, and those running now on its tip or baseline."""
    store, g = project.store, project.git
    rows: Dict[int, Dict[str, Any]] = {}
    res = (((_last_verified(project, trial) or {}).get("detail") or {}).get("result")) or {}
    ids = [(res.get(k) or {}).get("check") for k in ("tests", "base")] + [m.get(k) for m in res.get("measures") or [] for k in ("check", "base_check")]
    for cid in ids:
        if cid:
            try:
                rows[int(cid)] = store.check(int(cid))
            except KeyError:
                pass
    tip = _sha(g, "refs/heads/" + str(trial.get("branch") or ""))
    for sha in {tip, _merge_base(g, _sha(g, project.base), tip)} - {None}:
        for r in store.checks(sha=sha):
            if r["status"] == "running":
                rows[int(r["id"])] = r
    return [rows[k] for k in sorted(rows)]


def _log_lines(project: Any, row: Dict[str, Any], lines: int) -> List[str]:
    """The end of a check's log; only a file in the project's logs directory is read."""
    path = row.get("log")
    if not path:
        return []
    logs, p = project.path(project.config["logs"]).resolve(), Path(path).resolve()
    if logs not in p.parents or not p.is_file():
        return []
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 256 * 1024))
        text = f.read().decode("utf-8", "replace")
    return text.splitlines()[-max(1, min(int(lines), 2000)):]


_STARTING = threading.Lock()


def _not_running(project: Any, tid: int) -> None:
    """Refuse a second verification of a trial while one runs, ``force`` or not: the store would not make it wait for the first
    (``force`` claims every check anew), so both would run every check at the same time. ``force`` is for a finished result."""
    pids = _running(project, tid)
    if pids:
        raise DecisionError("trial %d is being verified already (pid %s): verify it again when that has ended" % (tid, pids[0]))


def register_routes(route: Callable, project: Any) -> None:
    @route("GET", r"/api/decisions/trials/(\d+)/verify")
    def verify_get(q, b, tid):
        t = project.store.trial(int(tid))
        return {"verify": compare_fields(project, t)["verify"], "checks": [_public(r) for r in _rows_of(project, t)]}

    @route("POST", r"/api/decisions/trials/(\d+)/verify")
    def verify_post(q, b, tid):
        tid = int(tid)
        t = project.store.trial(tid)
        if not settings(project)["enabled"]:                                 # a setting that cannot work: 400 saying what to fix
            raise DecisionError("verification is off: verify.enabled is false in .dtree/config.json")
        if _sha(project.git, "refs/heads/" + str(t.get("branch") or "")) is None:   # told now, not only in the runner's log
            raise DecisionError("trial %d's branch %s does not exist" % (tid, t.get("branch")))
        from . import jobs                                                   # jobs imports this module: only at call time
        jobs.reap(project.store)
        busy = [j for j in project.store.jobs(tid=tid) if j["status"] == "running"]
        if busy:
            raise DecisionError("trial %d is being built (job %d): it is verified when the job ends" % (tid, busy[-1]["id"]))
        with _STARTING:                                                      # two requests at once (a double click): one runner
            _not_running(project, tid)
            return {"started": start_background(project, tid, by="user", force=bool(b.get("force")))}

    @route("GET", r"/api/checks/(\d+)/log")
    def check_log(q, b, cid):
        row = project.store.check(int(cid))
        return {"check": _public(row), "lines": _log_lines(project, row, int(q["lines"]) if str(q.get("lines", "")).isdigit() else 200)}


def register_cli(sub: Any) -> None:
    p = sub.add_parser("verify", help="dtree runs the project's tests and measures itself on a trial's tip and its baseline")
    p.add_argument("trial", type=int)
    p.add_argument("--force", action="store_true", help="run every check again instead of reusing its result")
    p.add_argument("--no-baseline", action="store_true", help="only the trial's tip")
    p.add_argument("--background", action="store_true", help="in a detached process; follow it with dtree checks --trial")
    p = sub.add_parser("checks", help="the checks dtree ran (the latest per commit and command)")
    p.add_argument("--sha", default=None)
    p.add_argument("--trial", type=int, default=None)


def _say(line: str) -> None:
    print(time.strftime("%H:%M:%S ") + line, flush=True)


def _exit_code(summary: Dict[str, Any]) -> int:
    """``dtree verify``'s exit code: 0 the tip passed; EXIT_NONE (3) nothing was run (status none: nothing selected, nothing to
    verify); 1 anything else (failed, error, timeout, stopped). A refusal (verification off, a setting that cannot work, no branch,
    a trial being verified) is 1 too, and argparse's usage error 2."""
    return 0 if summary["status"] == "passed" else EXIT_NONE if summary["status"] == "none" else 1


def _check_line(r: Dict[str, Any]) -> str:
    status = r["status"] + ("?" if r["status"] == "running" and not _alive(r.get("runner_pid")) else "")
    what = ("%g %s" % (r["value"], r.get("unit") or "")).strip() if r.get("value") is not None else (
        _counts_line(r) if r.get("passed") is not None else str(r.get("summary") or ""))
    return "check %-4d %s %-12s %-7s %-8s %-40s %7s  %s" % (r["id"], r["sha"][:7], r["name"][:12], r["kind"], status, what[:40],
                                                           "-" if r.get("seconds") is None else "%.1f s" % r["seconds"], r.get("by") or "")


def run_cli(args: Any, project: Any) -> bool:
    if args.cmd == "verify":
        if args.background or args.force:                                    # without --force a second run waits for the first
            _not_running(project, args.trial)
        if args.background:                                                  # refused now as in the foreground (exit 1), not
            t = project.store.trial(args.trial)                              # only in the runner's log (review of round 3)
            if not settings(project)["enabled"]:
                raise DecisionError("verification is off: verify.enabled is false in .dtree/config.json")
            if _sha(project.git, "refs/heads/" + str(t.get("branch") or "")) is None:
                raise DecisionError("trial %d's branch %s does not exist" % (args.trial, t.get("branch")))
            pid = start_background(project, args.trial, by="cli", force=args.force, baseline=False if args.no_baseline else None)
            if not pid:                                                      # refused, like the verification in the foreground
                raise DecisionError("verification is off: verify.enabled is false in .dtree/config.json")
            print("verifying trial %d in the background (pid %s): dtree checks --trial %d" % (args.trial, pid, args.trial))
            return True
        summary = verify_trial(project, args.trial, by="cli", force=args.force, say=_say, baseline=False if args.no_baseline else None)
        print("trial %d: %s%s" % (args.trial, summary["status"], (" · " + summary["summary"]) if summary.get("summary") else ""))
        code = _exit_code(summary)
        if code:
            raise SystemExit(code)                                           # so a script can rely on it: dtree verify T && …
        return True
    if args.cmd == "checks":
        store = project.store
        if args.trial is not None:
            rows = _rows_of(project, store.trial(args.trial))
        elif args.sha:
            rows = store.checks(sha=_sha(project.git, args.sha) or args.sha)
        else:
            rows = store.checks()[-50:]
        for r in rows:
            print(_check_line(r))
        if not rows:
            print("no checks")
        return True
    return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dtree.verify", description="dtree's verification runner: a trial's tests and measures on its tip and baseline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="what dtree verify --background, the page and an update start")
    p.add_argument("--trial", type=int, required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-baseline", action="store_true")
    p.add_argument("--auto", action="store_true", help="only what verify names, as after a job: a bare test_command is left out")
    p.add_argument("--by", default="user")
    args = ap.parse_args(argv)
    from .project import Project
    proj = Project(args.project)
    try:
        summary = verify_trial(proj, args.trial, by=args.by, force=args.force, say=_say, baseline=False if args.no_baseline else None,
                               auto=args.auto)
    except (DecisionError, KeyError) as exc:
        _say("error: %s" % exc)
        return 1
    _say("trial %d: %s" % (args.trial, summary["status"]))
    return _exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())
