"""Reading a test run's own summary: how many passed, failed, errored and were skipped, from what the runner printed.

dtree's verification (verify.py) prefers pytest's JUnit file, which is exact whatever the verbosity; this is for everything else — a
project whose tests are unittest, jest, vitest, mocha, go, cargo or TAP, a pytest run whose JUnit file was not written — and for the
summary line shown beside the counts. It was built from 74 real and synthetic runs on this machine (2026-11-01: pytest 9, unittest
3.9 and 3.12, vitest 3 and 4, mocha 10, node --test, prove; jest, go and cargo from their format strings), kept in
``tests/data/summary_vectors.json`` and checked by ``tests/test_parse_summary.py``.

``parse_summary(text, exit_code)`` returns {framework (pytest | unittest | jest | vitest | mocha | go | cargo | tap | unknown), passed,
failed, errors, skipped, xfailed, xpassed, total (int, or None when the runner does not report it), ok (exit 0 and no failures or
errors), summary_line}. Rules the runs taught:

- colour codes and carriage-return redraws are removed first (a TTY run, a progress bar);
- with several summaries (several runs, a re-run, ``pytest && npm test``) the LAST wins; go sums the packages of its last run,
  cargo the binaries after its last "Finished";
- no known summary: framework "unknown", counts None, ``ok`` from the exit code alone. ``pytest -qq`` prints no summary at all.
- ``ok`` is True for exit 0 even when total is 0 (Python 3.9's unittest, ``jest --passWithNoTests``): the caller decides whether
  "nothing ran" is a pass.
- every count is at most ``MAX_COUNT``: the text is the code under test's own output, and "10**30 passed" must not overflow the
  store (nor a 5000-digit number raise in ``int()``).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Pattern, Tuple, Union

COUNTS = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed", "total")
OUTCOMES = COUNTS[:-1]
MAX_COUNT = 10 ** 9
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[ -/]*[0-~]")  # CSI, OSC, other

Found = Optional[Tuple[int, Dict[str, Any]]]          # (index of the summary's last line, the result)


def clean_lines(text: Union[str, bytes, None]) -> List[str]:
    """The text as lines without colour codes; a carriage-return redraw keeps only its last text."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    redraws = ([p for p in line.split("\r") if p.strip()] for line in ANSI.sub("", text or "").split("\n"))
    return [parts[-1].rstrip() if parts else "" for parts in redraws]


def _int(digits: Union[str, int]) -> int:
    """A printed count (a run of digits), at most MAX_COUNT."""
    text = str(digits).lstrip("0") or "0"
    return MAX_COUNT if len(text) > 10 else min(int(text), MAX_COUNT)


def _result(framework: str, line: Optional[str], **counts: Any) -> Dict[str, Any]:
    return {"framework": framework, **dict.fromkeys(COUNTS), **counts, "summary_line": line and line.strip()}


def _last(lines: List[str], rx: Pattern[str], end: Optional[int] = None) -> Tuple[Optional[int], Optional[re.Match]]:
    """(index, match) of the last line before ``end`` that ``rx`` matches completely, or (None, None)."""
    for i in reversed(range(len(lines) if end is None else end)):
        m = rx.fullmatch(lines[i])
        if m:
            return i, m
    return None, None


# pytest: "== 1 failed, 4 passed, 2 skipped, 1 deselected, 1 xfailed, 1 xpassed, 1 warning, 1 error in 0.02s =="
PYTEST = re.compile(r"=*\s*(?=.*\b(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|no tests ran)\b)"
                    r"((?:no tests ran|\d+ \w+(?: \w+)?)(?:, \d+ \w+(?: \w+)?)*)"
                    r" in [\d.]+ ?s(?:econds)?(?: \([\d:.]+\))?\s*=*")


def _pytest(lines: List[str]) -> Found:
    i, m = _last(lines, PYTEST)
    if m is None:
        return None
    c = dict.fromkeys(OUTCOMES, 0)
    for n, word in re.findall(r"(\d+) (\w+(?: \w+)?)", m.group(1)):
        word = "errors" if word == "error" else word
        if word in c:                       # "deselected", "warnings", "subtests passed" ... are not outcomes
            c[word] += _int(n)
    return i, _result("pytest", lines[i], total=sum(c.values()), **c)


# unittest: "Ran 10 tests in 0.001s", a blank line, "OK", "OK (skipped=2)", "NO TESTS RAN" or "FAILED (failures=1, ...)"
UT_RAN = re.compile(r"Ran (\d+) tests? in [\d.]+s")
UT_END = re.compile(r"(OK|FAILED|NO TESTS RAN)(?: \((.*)\))?")
UT_WORDS = {"failures": "failed", "errors": "errors", "skipped": "skipped",
            "expected failures": "xfailed", "unexpected successes": "xpassed"}


def _unittest(lines: List[str]) -> Found:
    i, m = _last(lines, UT_RAN)
    if m is None:
        return None
    total = _int(m.group(1))
    for j in range(i + 1, min(i + 4, len(lines))):
        end = UT_END.fullmatch(lines[j])
        if end:
            c = dict.fromkeys(OUTCOMES, 0)
            for word, n in re.findall(r"(\w[\w ]*)=(\d+)", end.group(2) or ""):
                if word in UT_WORDS:
                    c[UT_WORDS[word]] += _int(n)
            c["passed"] = max(0, total - sum(c.values()))     # failing subtests can outnumber the tests
            return j, _result("unittest", lines[i] + " / " + lines[j], total=total, **c)
    return i, _result("unittest", lines[i], total=total)


# jest: "Tests:       1 failed, 1 skipped, 1 todo, 4 passed, 7 total"; a suite that cannot load is an error
JEST = re.compile(r"\s*Tests:\s+((?:\d+ \w+, )*\d+ total)")
JEST_REPRINT = "Summary of all failing tests"          # over 20 suites jest prints every failure a second time


def _jest(lines: List[str]) -> Found:
    i, m = _last(lines, JEST)
    if m is None:
        return None
    c = {w: _int(n) for n, w in re.findall(r"(\d+) (\w+)", m.group(1))}
    run = lines[_last(lines, JEST, i)[0] or 0:i]
    run = run[run.index(JEST_REPRINT):] if JEST_REPRINT in run else run
    return i, _result("jest", lines[i], passed=c.get("passed", 0), failed=c.get("failed", 0),
                      errors=sum("Test suite failed to run" in line for line in run),
                      skipped=c.get("skipped", 0) + c.get("todo", 0), total=c["total"])


# vitest: "Tests  1 failed | 2 passed | 1 expected fail | 1 skipped | 1 todo (6)" or "Tests  no tests";
# a file that cannot load ("Failed Suites 1") and an unhandled error ("Errors  1 error") are errors
VITEST = re.compile(r"\s*Tests\s{2,}(?:no tests|(.*) \((\d+)\))")
VITEST_ERRORS = re.compile(r"\s*Errors\s{2,}(\d+) errors?\b.*")


def _vitest(lines: List[str]) -> Found:
    i, m = _last(lines, VITEST)
    if m is None:
        return None
    c = {w.strip(): _int(n) for n, w in re.findall(r"(\d+) ([a-z ]+)", m.group(1) or "")}
    run = lines[_last(lines, VITEST, i)[0] or 0:i]
    errors = sum(_int(n) for line in run for n in re.findall(r"Failed Suites (\d+)", line))
    errors += sum(_int(e.group(1)) for e in map(VITEST_ERRORS.fullmatch, lines[i + 1:i + 4]) if e)
    return i, _result("vitest", lines[i], passed=c.get("passed", 0), failed=c.get("failed", 0), errors=errors,
                      skipped=c.get("skipped", 0) + c.get("todo", 0), xfailed=c.get("expected fail", 0),
                      total=_int(m.group(2) or 0))


# mocha: "  3 passing (3ms)" then "  2 pending" and "  1 failing" when not zero
MOCHA = re.compile(r"\s*(\d+) (passing|pending|failing)(?: \(.*\))?")


def _mocha(lines: List[str]) -> Found:
    i, m = _last(lines, MOCHA)
    if m is None:
        return None
    j = i
    while j > 0 and MOCHA.fullmatch(lines[j - 1]):
        j -= 1
    c = {x.group(2): _int(x.group(1)) for x in map(MOCHA.fullmatch, lines[j:i + 1])}
    return i, _result("mocha", " / ".join(line.strip() for line in lines[j:i + 1]), passed=c.get("passing", 0),
                      failed=c.get("failing", 0), skipped=c.get("pending", 0), total=sum(c.values()))


# the runners' own "nothing to run" lines are a summary of zero tests
EMPTY = (("jest", re.compile(r"\s*No tests found, exiting with code \d+"), "passed failed errors skipped total"),
         ("vitest", re.compile(r"\s*No test files found, exiting with code \d+"), "passed failed errors skipped xfailed total"),
         ("mocha", re.compile(r"\s*Error: No test files found.*"), "passed failed skipped total"))


def _empty(lines: List[str]) -> Found:
    found = [(_last(lines, rx)[0], framework, keys) for framework, rx, keys in EMPTY]
    i, framework, keys = max((f for f in found if f[0] is not None), default=(None, None, None))
    return None if i is None else (i, _result(framework, lines[i], **dict.fromkeys(keys.split(), 0)))


# go test: an "ok"/"FAIL"/"?" line per package; "--- PASS/FAIL/SKIP:" per test (passes and skips only with -v)
GO_PKG = re.compile(r"(ok|FAIL|\?)\s+(\S+)\s+(?:\(cached\)|[\d.]+s|\[no test files\]|\[(?:build|setup) failed\])(?:\s.*)?")
GO_TEST = re.compile(r"\s*--- (PASS|FAIL|SKIP): .*")


def _go(lines: List[str]) -> Found:
    pkgs: Dict[str, Tuple[str, List[str]]] = {}
    tags: List[str] = []
    last = None
    for i, line in enumerate(lines):
        t = GO_TEST.fullmatch(line)
        if t or line.startswith("=== RUN"):
            tags.append(t.group(1) if t else "RUN")
            continue
        m = GO_PKG.fullmatch(line)
        if m:
            pkgs = {} if m.group(2) in pkgs else pkgs          # the same package again: a new run began
            pkgs[m.group(2)], tags, last = (m.group(1), tags), [], i
    if last is None:
        return None
    every = [t for _, ts in pkgs.values() for t in ts]
    # a failed package without a failed test is an error: build failure, panic outside a test, timeout
    c = {"failed": every.count("FAIL"), "errors": sum(st == "FAIL" and "FAIL" not in ts for st, ts in pkgs.values())}
    if "RUN" in every or "PASS" in every:                      # -v lists every test, so passes and skips are known
        c.update(passed=every.count("PASS"), skipped=every.count("SKIP"))
        c["total"] = c["passed"] + c["failed"] + c["skipped"]
    return last, _result("go", lines[last], **c)


# cargo test: "test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s" per binary
CARGO = re.compile(r"\s*test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; \d+ measured.*")
CARGO_RUN = re.compile(r"\s*Finished\b.*target\(s\) in .*")


def _cargo(lines: List[str]) -> Found:
    found: List[Tuple[int, List[int]]] = []
    for i, line in enumerate(lines):
        if CARGO_RUN.fullmatch(line):
            found = []                                          # a new `cargo test` run
        m = CARGO.fullmatch(line)
        if m:
            found.append((i, [_int(n) for n in m.groups()]))
    if not found:
        return None
    (last, _), (passed, failed, ignored) = found[-1], (sum(n[k] for _, n in found) for k in range(3))
    return last, _result("cargo", lines[last], passed=passed, failed=failed, skipped=ignored, total=passed + failed + ignored)


# TAP: "1..N" plan, "ok 1 - x", "not ok 2 - y", "# SKIP" / "# TODO" directives, "Bail out!"; top level only.
# prove (Perl's TAP harness) without -v prints only "Files=2, Tests=7, ..." and a per-file "Failed: N" report.
TAP = re.compile(r"(not )?ok (\d+)\b[^#]*(?:#\s*((?i:skip|todo)))?.*")
TAP_PLAN = re.compile(r"1\.\.(\d+)(?:\s*#.*)?")
PROVE = re.compile(r"Files=\d+, Tests=(\d+),.*")


def _tap(lines: List[str]) -> Found:
    start = next((i for i in reversed(range(len(lines))) if lines[i].startswith("TAP version")), 0)
    c: Dict[str, int] = dict.fromkeys(OUTCOMES, 0)
    plan: Optional[int] = None
    plan_line = prove = last = None
    for i in range(start, len(lines)):
        line, m, p = lines[i], TAP.fullmatch(lines[i]), TAP_PLAN.fullmatch(lines[i])
        if m:
            d = (m.group(3) or "").lower()
            c["skipped" if d == "skip" else ("xfailed" if m.group(1) else "xpassed") if d == "todo"
              else "failed" if m.group(1) else "passed"] += 1
        elif p:
            plan, plan_line = (plan or 0) + _int(p.group(1)), line
        elif line.startswith("Bail out!"):
            c["errors"] += 1
        elif PROVE.fullmatch(line):
            prove = i
        last = i if m or p or line.startswith("Bail out!") else last
    seen = sum(c.values()) - c["errors"]
    if prove is not None and not seen:                          # prove without -v: only the harness summary
        report = lines[start:prove]
        return prove, _result("tap", lines[prove], total=_int(PROVE.fullmatch(lines[prove]).group(1)),
                              failed=sum(_int(n) for x in report for n in re.findall(r"Tests: \d+ Failed: (\d+)\)", x)),
                              errors=sum(x.lstrip().startswith("Parse errors:") for x in report))
    if last is None or (plan is None and not lines[start].startswith("TAP version")):
        return None                                             # "ok 1" lines without a plan or a TAP header are not taken for TAP
    c["errors"] += max(0, (plan or 0) - seen)                   # planned tests that never reported
    c["total"] = max(plan or 0, seen)
    at = last if prove is None else prove
    return at, _result("tap", lines[prove] if prove is not None else plan_line or lines[last], **c)


FINDERS = (_pytest, _unittest, _jest, _vitest, _mocha, _empty, _go, _cargo, _tap)


def parse_summary(text: Union[str, bytes, None], exit_code: Optional[int]) -> Dict[str, Any]:
    """Give stdout and stderr together, in order: unittest, jest and Perl's Test::More print their summary on stderr."""
    lines = clean_lines(text)
    found = [r for r in (find(lines) for find in FINDERS) if r]
    result = max(found, key=lambda r: r[0])[1] if found else _result("unknown", None)
    for k in COUNTS:                                            # sums of bounded counts (go's packages, cargo's binaries) too
        if isinstance(result.get(k), int):
            result[k] = min(result[k], MAX_COUNT)
    result["ok"] = exit_code == 0 and not result["failed"] and not result["errors"]
    return result
