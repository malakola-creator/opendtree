"""parse_summary against 74 runs of real and stand-in test runners (pytest, unittest, jest, vitest, mocha, node, prove, go, cargo),
captured on 2026-11-01 for dtree's own verification: what each printed, its exit code, and the counts it must give."""
import json
import time
from pathlib import Path

import pytest

from dtree.parse_summary import COUNTS, clean_lines, parse_summary

VECTORS = json.loads((Path(__file__).resolve().parent / "data" / "summary_vectors.json").read_text(encoding="utf-8"))


def test_the_vectors_cover_every_runner_and_the_hard_cases():
    assert len(VECTORS) == 74
    assert {v["expected"]["framework"] for v in VECTORS} == {"pytest", "unittest", "jest", "vitest", "mocha", "go", "cargo", "tap", "unknown"}
    names = {v["name"] for v in VECTORS}
    assert {"pytest_mixed_qq_no_summary", "pytest_two_runs_last_wins", "composite_pytest_then_jest", "tty_pytest_mixed"} <= names


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_each_run_gives_its_counts(vector):
    got = parse_summary(vector["text"], vector["exit_code"])
    wrong = {k: (got.get(k), want) for k, want in vector["expected"].items() if got.get(k) != want}
    assert not wrong, wrong


def test_bytes_none_colour_and_redraws():
    assert parse_summary(b"5 passed in 0.10s\n", 0)["passed"] == 5
    assert parse_summary(None, 3) == dict({k: None for k in COUNTS}, framework="unknown", summary_line=None, ok=False)
    assert clean_lines("\x1b[32mok\x1b[0m\rdone\nnext") == ["done", "next"]
    assert parse_summary("collecting\r\x1b[1m3 passed, 1 failed in 0.2s\x1b[0m\n", 1)["failed"] == 1


def test_counts_printed_by_the_code_under_test_are_bounded():
    """The output is the tested code's own: a count of 30 or 5000 digits (int() of 5000 digits raised ValueError) is 10**9, and so
    is a sum of such counts; a count with leading zeros is still the number it says."""
    most, big = 10 ** 9, "9" * 5000
    assert parse_summary("%s passed in 0.10s\n" % big, 0)["passed"] == most
    ut = parse_summary("Ran %s tests in 0.001s\n\nFAILED (failures=%s)\n" % ("9" * 30, big), 1)
    assert (ut["total"], ut["failed"], ut["passed"]) == (most, most, 0)
    cargo = "test result: ok. %s passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n" % ("9" * 12)
    assert parse_summary(cargo * 5, 0)["passed"] == most                     # five binaries: the sum is bounded too
    assert parse_summary("0000000000005 passed in 0.10s\n", 0)["passed"] == 5
    import dtree.parse_summary as module
    assert module.MAX_COUNT == most


def test_a_big_log_is_read_quickly():
    one = [v for v in VECTORS if v["name"] == "pytest_mixed_default"][0]["text"]
    big = one * 2000                                                     # about 3 MB, the summary repeated: the last wins
    t0 = time.perf_counter()
    r = parse_summary(big, 1)
    assert r["passed"] == 4 and r["failed"] == 1 and time.perf_counter() - t0 < 5
