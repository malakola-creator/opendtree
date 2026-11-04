"""ค — from a measured trial to the base: merge readiness, merging without disturbing anyone, and bringing a trial up to date
(the user chose it on 2026-11-01).

- readiness: whether the trial's branch merges into the base cleanly (``git merge-tree``, nothing touched), the files that would
  conflict, how far the base moved since the branch left it, whether it is merged already, where the base is checked out (and so how it
  would be merged), and whether it may be merged now — only an adopted trial, or a question's own ``dec/`` branch. dtree's own worktree
  of the trial (in its shadow repository, which the project's list of worktrees does not show) is looked at too: commits there that were
  never brought back, or uncommitted changes, would not be merged;
- merge: into the base without switching anyone's checkout — by plumbing when the base is checked out nowhere, else in the clean
  worktree where it is (``gitcode.Git.merge_into``). The commits and the merge commit are recorded on the question and the trial
  (``merged`` event); ``close`` marks the question done with the trial's measurement (dtree's own when it verified that commit);
- update: the base merged into the trial's branch — by plumbing when that is clean and the branch is checked out nowhere, else in the
  trial's worktree. A conflict is left in progress there, and an ``update`` job asks an agent to resolve it with only ``git add`` and
  ``git commit``; dtree checks the result itself (``after_job``), and the trial is verified again. Until dtree accepts an update (its
  check passed: an ``updated`` event, status resolved), the trial is not merged: readiness runs the same check, and ``dtree update``
  checks an update whose job ended without it (failed, stopped, a runner that died) again. Commits that came onto the branch after the
  update started (``branch_sha`` of its event) without its merge — a job that dropped the merge and committed plainly — are held back
  the same way, until ``dtree update`` starts the update again from the branch as it is.

Interface used by the rest of dtree (kept stable):
- ``register_routes(route, project)`` · ``register_cli(sub)`` · ``run_cli(args, project) -> bool``
- ``compare_fields(project, trial) -> dict`` — added to the trial's row of ``jobs.compare``
- ``prompt_lines(project, job, trial, worktree) -> list[str]`` — extra lines for an agent's prompt (an update job's conflicts)
- ``after_job(project, job, trial, say) -> str | None`` — for an update job: why it failed (the merge is still in progress), or None

jobs, verify, agents and spend are imported inside the functions: jobs imports this module.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .gitcode import METHODS, shown
from .store import DecisionError

#: how many paths a message names and an event keeps (git keeps them all)
MAX_PATHS = 200
#: how many commit subjects a merge commit's message lists
MAX_SUBJECTS = 100
#: the base's checkout is looked at once for all the rows of one compare table: with optional locks off, ``git status`` cannot
#: refresh a stale index and hashes those files again each time (315 ms on a fresh clone of ultimateTruth, 2 ms on its own root).
#: Display only — a merge always looks again. (checkout, base commit, index mtime) → (when, why not)
_BASE_SEEN: Dict[Tuple[str, str, int], Tuple[float, str]] = {}
BASE_SEEN_S = 3.0


def method(project: Any) -> str:
    """``merge.method``: auto (plumbing when the base is checked out nowhere, else the worktree where it is), plumbing, or worktree."""
    m = str((project.config.get("merge") or {}).get("method") or "auto")
    if m not in METHODS:
        raise DecisionError("merge.method in .dtree/config.json is %s, not %r" % (" / ".join(METHODS), m))
    return m


def _trial_worktree(project: Any, branch: str) -> Path:
    """Where dtree keeps a trial's worktree: the place ``jobs._ensure_worktree`` makes it."""
    return project.path(project.config["worktrees"]) / branch.replace("/", "-")


def _same(a: Any, b: Any) -> bool:
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


def _shadow_worktree(g: Any, wt: Path, branch: str, trees: List[Dict[str, Any]]) -> Optional[Path]:
    """dtree's own worktree of a trial at ``wt`` when it is one of the trial's shadow repository (``jobs._shadow``) on its branch: the
    project's list of worktrees (``trees``) does not show those, so what a job left there is looked at here. None otherwise — a worktree
    of the project itself is in that list, and one gone detached holds nothing of the branch."""
    if not os.path.isdir(str(wt)) or any(_same(w["path"], wt) for w in trees):
        return None
    if g.worktree_dir(wt) is None or g.symbolic_head(wt) != "refs/heads/" + branch:
        return None
    return wt


def _not_brought_back(g: Any, wt: Path, branch: str, tip: Optional[str], tid: int, here: Optional[str] = None,
                      repo: Optional[Path] = None) -> Optional[Tuple[str, str, str]]:
    """(reason_code, reason, reason_th) when dtree's worktree ``wt`` of the trial holds commits of ``branch`` that the project's branch
    (at ``tip``) lacks — a runner that ended before it brought them back — else None. Behind the branch is fine: a job's start brings it
    forward. Its commits live in the trial's shadow repository, so git compares them there. ``repo`` (with ``here``, its branch's commit):
    the shadow repository itself, whose worktree is gone (deleted by hand: review F2) — its commits would be lost as well as unmerged."""
    cwd = repo if repo is not None else wt
    here = here if repo is not None else g.sha("HEAD", cwd=wt)
    if not here or not tip or here == tip:
        return None
    if repo is not None:
        where = "dtree's repository of the trial %s (its worktree %s is gone; the branch there is at %s)" % (repo, wt, here[:7])
        where_th = "repository ของ dtree สำหรับทางลองนี้ %s (worktree %s ถูกลบไปแล้ว กิ่งที่นั่นอยู่ที่ %s)" % (repo, wt, here[:7])
    else:
        where = "%s (dtree's worktree of the trial, at %s)" % (wt, here[:7])
        where_th = "%s (worktree ของ dtree สำหรับทางลองนี้ อยู่ที่ %s)" % (wt, here[:7])
    behind = g.ancestor(here, tip, cwd=cwd)
    if behind:
        return None
    ahead = g.ancestor(tip, here, cwd=cwd) if behind is False else None
    if ahead:
        n = (g.out("rev-list", "--count", "%s..%s" % (tip, here), cwd=cwd) or "").strip()
        n = n if n.isdigit() else "?"
        return ("not_brought_back", "%s has %s commit(s) in %s that were never brought into the project (the branch is at %s here): they "
                "would not be merged — dtree update %d brings them back" % (branch, n, where, tip[:7], tid),
                "%s มี %s commit ใน %s ที่ยังไม่ได้นำกลับเข้าโปรเจกต์ (ที่นี่กิ่งอยู่ที่ %s): จะไม่ถูกรวม — สั่ง dtree update %d เพื่อนำกลับมา"
                % (branch, n, where_th, tip[:7], tid))
    if behind is None or ahead is None:                              # git could not compare them: the gate stays shut
        return ("not_brought_back", "%s in %s could not be compared with the project's branch (at %s): look there before merging"
                % (branch, where, tip[:7]),
                "เทียบ %s ใน %s กับกิ่งในโปรเจกต์ (อยู่ที่ %s) ไม่ได้: ดูที่นั่นก่อนรวม" % (branch, where_th, tip[:7]))
    return ("not_brought_back", "%s in %s and in the project (at %s) have gone separate ways: bring one into the other first — what is only "
            "there would not be merged" % (branch, where, tip[:7]),
            "%s ใน %s กับในโปรเจกต์ (อยู่ที่ %s) แยกทางกันแล้ว: รวมฝั่งหนึ่งเข้าอีกฝั่งก่อน — ส่วนที่มีแค่ที่นั่นจะไม่ถูกรวม" % (branch, where_th, tip[:7]))


def _paths(paths: List[str], n: int = MAX_PATHS) -> List[str]:
    return [shown(p) for p in paths[:n]]


def _last_update(store: Any, trial: Dict[str, Any], status: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The detail of the trial's latest ``updated`` event (with ``status``), or None."""
    for e in reversed(store.get(trial["decision_id"])["events"]):
        d = e.get("detail") or {}
        if e["action"] == "updated" and d.get("trial") == trial["id"] and (status is None or d.get("status") == status):
            return d
    return None


def _files(n: int) -> str:
    return "%d file%s" % (n, "" if n == 1 else "s")


def _commits(n: int) -> str:
    return "%d commit%s" % (n, "" if n == 1 else "s")


def _running(store: Any, tid: int) -> List[Dict[str, Any]]:
    return [j for j in store.jobs(tid=tid) if j["status"] == "running"]


_SHADOW_SEEN: Dict[Tuple[str, str, int], Tuple[float, List[str]]] = {}


def _shadow_changes(g: Any, wt: Path, fresh: bool) -> List[str]:
    """``Git.changes`` of dtree's shadow worktree of a trial, kept like ``_base_ready`` for the compare table's every row and poll (a
    fresh worktree's index cannot be refreshed without a lock, so git hashes its files each time); ``fresh`` for a merge."""
    d = g.worktree_dir(wt)
    try:
        stamp = (d / "index").stat().st_mtime_ns if d is not None else 0
    except OSError:
        stamp = 0
    key, now = (str(wt), g.sha("HEAD", cwd=wt) or "", stamp), time.time()
    hit = _SHADOW_SEEN.get(key)
    if not fresh and hit is not None and now - hit[0] < BASE_SEEN_S:
        return hit[1]
    changed = g.changes(wt)
    if len(_SHADOW_SEEN) > 64:
        _SHADOW_SEEN.clear()
    _SHADOW_SEEN[key] = (now, changed)
    return changed


def _base_ready(g: Any, wt: Path, base: str, base_sha: str) -> str:
    """``Git.ready_worktree`` of the base's checkout, kept for ``BASE_SEEN_S`` while its index and the base commit are unchanged."""
    d = g.worktree_dir(wt)
    try:
        stamp = (d / "index").stat().st_mtime_ns if d is not None else 0
    except OSError:
        stamp = 0
    key, now = (str(wt), base_sha or "", stamp), time.time()
    hit = _BASE_SEEN.get(key)
    if hit is not None and now - hit[0] < BASE_SEEN_S:
        return hit[1]
    why = g.ready_worktree(wt, base)
    if len(_BASE_SEEN) > 64:
        _BASE_SEEN.clear()
    _BASE_SEEN[key] = (now, why)
    return why


# ------------------------------------------------------------------------------------------------------------- readiness
def readiness(project: Any, trial: Dict[str, Any], fresh: bool = False) -> Dict[str, Any]:
    """Whether ``trial`` can be merged into the base now, and why not: the preview (``Git.merge_preview``: state clean / conflicts /
    merged / error, the conflicting paths, behind and ahead), where the base is checked out (``base_worktree``) and so ``how`` it would
    be merged, an update in progress in the trial's worktree (``updating``), ``can_merge``, and ``reason`` / ``reason_th`` with a
    stable ``reason_code``. Only an adopted trial is merged. Read-only and fast (no fetch): the compare table asks it for every row."""
    return _readiness(project, trial["branch"], trial=trial, fresh=fresh)


def _readiness(project: Any, branch: str, trial: Optional[Dict[str, Any]] = None, question: Optional[Dict[str, Any]] = None,
               fresh: bool = False) -> Dict[str, Any]:
    g, base = project.git, project.base
    out: Dict[str, Any] = {"branch": branch, "base": base, "state": "error", "conflicts": [], "conflict_count": 0, "base_worktree": [],
                           "how": None, "updating": None, "can_merge": False, "reason_code": "", "reason": "", "reason_th": ""}

    def no(code: str, en: str, th: str) -> Dict[str, Any]:
        out.update(can_merge=False, reason_code=code, reason=en, reason_th=th)
        return out

    if not g.available():
        out["error"] = "no git repository at %s" % project.root
        return no("error", out["error"], "ไม่มี git repository ที่ %s" % project.root)
    how = method(project)
    trees = g.worktrees()
    bases = [w for w in g.checked_out(base, trees) if not w["prunable"]]
    one = bases[0] if len(bases) == 1 and bases[0]["how"] == "checked out" and os.path.isdir(bases[0]["path"]) else None
    out.update(g.merge_preview(base, branch, cwd=one["path"] if one else None))
    out["conflict_count"] = len(out["conflicts"])
    out["conflicts"] = _paths(out["conflicts"])
    out["base_worktree"] = [{"path": w["path"], "how": w["how"]} for w in bases]
    out["how"] = "worktree" if bases or how == "worktree" else "plumbing"
    n, b7 = out["conflict_count"], (out.get("base_sha") or "")[:7]
    if out["state"] == "error":
        return no("error", out["error"], "ตรวจไม่ได้: %s" % out["error"])
    shadow: Optional[Path] = None
    if trial is not None:
        busy = _running(project.store, trial["id"])
        if busy:
            return no("job_running", "job %d is running on trial %d: wait for it or stop it" % (busy[-1]["id"], trial["id"]),
                      "งาน %d กำลังทำทางลอง %d อยู่: รอให้เสร็จหรือหยุดก่อน" % (busy[-1]["id"], trial["id"]))
        wt = _trial_worktree(project, branch)
        from . import jobs
        try:
            jobs.trust(project, wt)                                  # nothing a job left in the trial's shadow runs in these polls (F0)
        except DecisionError as exc:
            return no("untrusted", "dtree runs no git in the trial's worktree %s: %s" % (wt, exc),
                      "dtree ไม่สั่ง git ใน worktree ของทางลองนี้ %s: %s" % (wt, exc))
        head = g.merge_head(wt)
        if head:
            left = g.unmerged(wt)
            out["updating"] = {"worktree": str(wt), "base_sha": head, "unresolved": _paths(left)}
            return no("updating", "a merge of %s into %s is in progress in %s (%s not resolved): finish it (dtree update %d sends a job) "
                      "or abort it (dtree update %d --abort)" % (head[:7], branch, wt, _files(len(left)), trial["id"], trial["id"]),
                      "การรวม %s เข้า %s ค้างอยู่ใน %s (ยังไม่แก้ %d ไฟล์): ให้งานแก้ให้เสร็จ หรือยกเลิกการอัปเดต" % (head[:7], branch, wt, len(left)))
        shadow = _shadow_worktree(g, wt, branch, trees)
        repo = jobs._shadow_path(project, branch)
        behind = None
        if shadow is not None:
            behind = _not_brought_back(g, shadow, branch, out.get("branch_sha"), trial["id"])
        elif repo.is_dir() and g.worktree_dir(wt) is None:          # its worktree deleted by hand: the shadow still holds the commits
            behind = _not_brought_back(g, wt, branch, out.get("branch_sha"), trial["id"], here=g.sha("refs/heads/" + branch, cwd=repo), repo=repo)
        if behind is not None:
            code, en, th = behind
            held = [w["path"] for w in g.checked_out(branch, trees) if w["how"] == "checked out" and not w["prunable"]]
            if held:                                                 # dtree update refuses while the branch is checked out there
                en += " — first switch %s away from %s" % (held[0], branch)
                th += " — สลับ %s ออกจาก %s ก่อน" % (held[0], branch)
            return no(code, en, th)
        gate = _unaccepted(project, trial, out.get("branch_sha"))
        if gate is not None:
            return no(*gate)
    kept = (trial if trial is not None else question or {}).get("commits") or []
    merged = [c for c in kept if c.get("how") == "merged" and c.get("merge")]
    if out["state"] == "merged" and merged:
        return no("merged", "%s is merged into %s already (%s)" % (branch, base, merged[-1]["sha"][:7]),
                  "%s รวมเข้า %s แล้ว (%s)" % (branch, base, merged[-1]["sha"][:7]))
    places = [w["path"] for w in g.checked_out(branch, trees) if w["how"] == "checked out" and not w["prunable"]]
    for place in places + ([str(shadow)] if shadow is not None else []):
        changed = _shadow_changes(g, Path(place), fresh) if shadow is not None and place == str(shadow) else g.changes(place)
        if changed:
            return no("branch_dirty", "%s has uncommitted changes in %s (%s): commit or discard them first — they would not be merged"
                      % (branch, place, ", ".join(_paths(changed, 5))),
                      "%s มีงานที่ยังไม่ commit ใน %s (%s): commit หรือทิ้งก่อน ไม่เช่นนั้นจะไม่ถูกรวม" % (branch, place, ", ".join(_paths(changed, 5))))
    if out["state"] == "merged":
        return no("nothing", "%s has no commits that %s lacks (merged already, or nothing committed yet)" % (branch, base),
                  "%s ไม่มี commit ที่ %s ยังไม่มี (รวมแล้ว หรือยังไม่มีอะไร commit)" % (branch, base))
    if out["state"] == "conflicts":
        hint = (" (dtree update %d)" % trial["id"]) if trial is not None and trial["status"] in ("trying", "measured", "adopted") else ""
        return no("conflicts", "%s conflict with %s %s: update the branch with %s first%s" % (_files(n), base, b7, base, hint),
                  "ขัดกับ %s %s อยู่ %d ไฟล์: อัปเดตกิ่งจาก %s ก่อน" % (base, b7, n, base))
    if trial is not None and trial["status"] != "adopted":
        return no("not_adopted", *_not_adopted(trial))
    if question is not None and question["status"] not in ("decided", "done"):
        return no("not_decided", "question %d is %s: only a decided question's branch is merged" % (question["id"], question["status"]),
                  "คำถาม %d ยังเป็น %s: รวมได้เฉพาะกิ่งของคำถามที่ตัดสินแล้ว" % (question["id"], question["status"]))
    busy_base = [w for w in bases if w["how"] != "checked out"]
    if busy_base or len(bases) > 1:
        w = (busy_base or bases)[0]
        return no("base_busy", "%s is %s in %s: finish that first" % (base, w["how"] if busy_base else "checked out %d times" % len(bases), w["path"]),
                  "%s ไม่ว่าง (%s ที่ %s): ทำให้เสร็จก่อน" % (base, w["how"], w["path"]))
    if bases and how == "plumbing":
        return no("method", "merge.method is plumbing but %s is checked out at %s (moving the branch would leave that checkout stale)" % (base, bases[0]["path"]),
                  "merge.method เป็น plumbing แต่ %s ถูก checkout อยู่ที่ %s" % (base, bases[0]["path"]))
    if bases:
        wt = Path(bases[0]["path"])
        why = _base_ready(g, wt, base, out["base_sha"])
        if why:
            return no("base_dirty", why, "ที่ checkout ของ %s (%s) ยังไม่พร้อม: %s" % (base, wt, why))
        way = g.in_the_way(wt, out["base_sha"], out["tree"])
        if way is None or way:
            listed = ", ".join(_paths(way or [], 10)) or "?"
            return no("in_the_way", "the merge would overwrite what git does not track in %s: %s (move it away first)" % (wt, listed),
                      "การรวมจะเขียนทับสิ่งที่ git ไม่ได้ติดตามใน %s: %s (ย้ายออกก่อน)" % (wt, listed))
        out.update(can_merge=True, reason_code="ready", reason="ready: git merge --no-ff in %s, %s's clean checkout" % (wt, base),
                   reason_th="พร้อมรวม: git merge --no-ff ใน %s (ที่ checkout ของ %s ซึ่งสะอาด)" % (wt, base))
    elif how == "worktree":
        out.update(can_merge=True, reason_code="ready", reason="ready: git merge --no-ff in a temporary worktree of %s (merge.method worktree)" % base,
                   reason_th="พร้อมรวม: git merge --no-ff ใน worktree ชั่วคราวของ %s" % base)
    else:
        out.update(can_merge=True, reason_code="ready", reason="ready: by plumbing — %s is checked out nowhere, so no checkout is touched" % base,
                   reason_th="พร้อมรวม: แบบ plumbing — ไม่มีที่ใด checkout %s อยู่ จึงไม่แตะไฟล์ของใคร" % base)
    return out


def _not_adopted(t: Dict[str, Any]) -> Tuple[str, str]:
    """Why a trial that is not adopted is not merged, and what to do, by its status: a dropped trial cannot be adopted (``dtree end``
    refuses it: E9 of the real run), so its option is tried again, on the same branch."""
    tid, st = t["id"], t["status"]
    if st == "measured":
        return ("trial %d is measured: only an adopted trial is merged (adopt it first: dtree end %d adopted)" % (tid, tid),
                "ทางลอง %d วัดผลแล้ว: รวมได้เฉพาะทางที่เลือกแล้ว (เลือกก่อน: dtree end %d adopted)" % (tid, tid))
    if st == "trying":
        return ("trial %d is trying: only an adopted trial is merged (once it is measured: dtree end %d adopted)" % (tid, tid),
                "ทางลอง %d ยังอยู่ระหว่างลอง: รวมได้เฉพาะทางที่เลือกแล้ว (เมื่อวัดผลแล้ว: dtree end %d adopted)" % (tid, tid))
    if st == "dropped":
        return ("trial %d is dropped: only an adopted trial is merged, and a dropped one is not adopted — to take its option after all, try it "
                "again (dtree try %d %s, on the same branch %s), measure it and adopt that trial" % (tid, t["decision_id"], t["option"], t["branch"]),
                "ทางลอง %d ถูกทิ้งแล้ว: รวมได้เฉพาะทางที่เลือก และทางที่ทิ้งแล้วเลือกไม่ได้ — ถ้าจะเอาทางนี้ ให้ลองใหม่ (dtree try %d %s บนกิ่งเดิม %s) "
                "วัดผล แล้วเลือกทางลองนั้น" % (tid, t["decision_id"], t["option"], t["branch"]))
    return ("trial %d is %s: only an adopted trial is merged (a switch keeps both options; it is chosen in the config)" % (tid, st),
            "ทางลอง %d เป็น %s: รวมได้เฉพาะทางที่เลือกแล้ว" % (tid, st))


def compare_fields(project: Any, trial: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return {"merge": readiness(project, trial)}
    except Exception as exc:                                         # one row's git trouble must not take the compare table down
        return {"merge": {"state": "error", "can_merge": False, "reason_code": "error", "error": "%s: %s" % (type(exc).__name__, exc),
                          "reason": "%s: %s" % (type(exc).__name__, exc), "reason_th": "ตรวจไม่ได้"}}


# ------------------------------------------------------------------------------------------------------------- merging
def _merge(project: Any, d: Dict[str, Any], branch: str, r: Dict[str, Any], by: str, trial: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge ``branch`` at the commit readiness ``r`` previewed; record the commits on the question (and the trial) and the event."""
    g, s = project.git, project.store
    commits = g.commits_between(r["base_sha"], r["branch_sha"])
    subject = "Merge #%d %s: %s" % (d["id"], branch, d["title_en"][:100])
    lines = ["- " + c["subject"] for c in commits[:MAX_SUBJECTS]] + (["- … and %d more" % (len(commits) - MAX_SUBJECTS)] if len(commits) > MAX_SUBJECTS else [])
    trailer = project.config.get("merge_trailer")
    msg = subject + "\n\n" + "\n".join(lines) + (("\n\n" + trailer) if trailer else "")
    res = g.merge_into(project.base, branch, msg, method=method(project), scratch=project.path(project.config["worktrees"]), at=r["branch_sha"])
    if not res["ok"]:
        raise DecisionError("%s was not merged into %s: %s" % (branch, project.base, res["error"]))
    kept = commits + [{"sha": res["sha"], "subject": subject, "merge": True}]
    why = "merged %s into %s (%s)" % (branch, project.base, res["how"])
    s.record_code(d["id"], kept, how="merged", why=why, by=by)
    if trial is not None:
        s.record_code(d["id"], kept, how="merged", why=why, trial=trial["id"], by=by)
    s.note_event(d["id"], "merged", by, {"trial": trial["id"] if trial is not None else None, "branch": branch, "sha": res["sha"], "how": res["how"]})
    return dict(res, branch=branch, commits=len(commits), subject=subject)


def _measured(d: Dict[str, Any], t: Dict[str, Any], sha: str) -> Tuple[str, str]:
    """What a merged trial was measured with, for ``done``: dtree's own verification of the commit merged when it ran, then what the
    agent reported; and the trial's impact."""
    tests = []
    for e in reversed(d.get("events") or []):
        v = e.get("detail") or {}
        if e["action"] == "verified" and v.get("trial") == t["id"] and v.get("sha") == sha and v.get("tests"):
            line = "dtree verify %s: %s" % (sha[:7], v["tests"])
            if v.get("base_tests"):
                line += " · base %s: %s" % (str(v.get("base_sha") or "")[:7], v["base_tests"])
            for name, pair in (v.get("measures") or {}).items():
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    line += " · %s %s (base %s)" % (name, pair[0], pair[1])
            tests.append(line)
            break
    if t.get("result_tests"):
        tests.append(str(t["result_tests"]))
    return ("\n".join(tests) or "-")[:4000], (str(t.get("result_impact") or "") or "merged %s" % t["branch"])[:4000]


def merge_trial(project: Any, tid: int, by: str = "user", close: bool = False) -> Dict[str, Any]:
    """Merge adopted trial ``tid`` into the base (``readiness`` says when it can). The trial's commits and the merge commit are
    recorded on the question and on the trial (how=merged) with a ``merged`` event. ``close``: the question is then marked done with
    the trial's measurement and impact; a refusal there is returned (``close_error``), since the merge itself is made."""
    from . import jobs
    s = project.store
    jobs.reap(s)
    t = s.trial(tid)
    r = readiness(project, t, fresh=True)                            # nothing looks at the trial's worktree again before the merge
    if not r["can_merge"]:
        raise DecisionError(r["reason"])
    d = s.get(t["decision_id"])
    res = _merge(project, d, t["branch"], r, by, trial=t)
    out: Dict[str, Any] = {"item": s.trial(tid), "merge": res}
    if close:
        tests, impact = _measured(s.get(d["id"]), t, res["branch_sha"])
        try:
            out["question"] = s.done(d["id"], tests, impact, by=by)
            out["closed"] = True
        except DecisionError as exc:
            out.update(closed=False, close_error=str(exc))
    return out


def merge_decision(project: Any, did: int, by: str = "user") -> Dict[str, Any]:
    """Merge question ``did``'s own branch (``dec/<id>-<option>``, from ``dtree start``) into the base, the same way as a trial."""
    from . import jobs
    s = project.store
    jobs.reap(s)
    d = s.get(did)
    if not d.get("branch"):
        raise DecisionError("question %d has no branch: start it first (dtree start %d)" % (did, did))
    r = _readiness(project, d["branch"], question=d)
    if not r["can_merge"]:
        raise DecisionError(r["reason"])
    res = _merge(project, d, d["branch"], r, by)
    return {"item": s.get(did), "merge": res}


# ------------------------------------------------------------------------------------------------------------- updating
def _update_agent(project: Any, t: Dict[str, Any], agent: Optional[str]) -> str:
    """The agent that resolves an update's conflicts: ``agent``, else the trial's last one. ``jobs.open_job`` / ``jobs.send`` check it
    (it can run here, the budget allows it, a job slot is free) before git is touched."""
    last = project.store.jobs(tid=t["id"])
    return agent or (last[-1].get("agent") if last else None) or t.get("variant") or project.config["default_agent"]


def _tidy(project: Any, wt: Path) -> bool:
    """Remove a trial's worktree when it holds nothing but caches (as the runner does after a job), and never one holding commits the
    project lacks (``jobs._remove_worktree``); True when removed."""
    from . import jobs
    g = project.git
    try:
        jobs.trust(project, wt)
    except DecisionError:
        return False
    if g.worktree_dir(wt) is None or g.in_progress(wt):
        return False
    if jobs._left_over(g.run(*jobs.SAFE_GIT, "status", "--porcelain", cwd=wt).stdout):
        return False
    return jobs._remove_worktree(project, wt)


def _note(project: Any) -> str:
    return "update: resolve the conflicts with %s" % project.base


def update_trial(project: Any, tid: int, agent: Optional[str] = None, by: str = "user") -> Dict[str, Any]:
    """Bring trial ``tid``'s branch up to date with the base: the base merged into it (``Git.update_branch``).

    - clean: by plumbing when the branch is checked out nowhere (a fast-forward when it has no commits of its own), else in the trial's
      worktree; an ``updated`` event (status clean), the merge commit recorded on the trial, and the trial verified again in the
      background (``verify.start_background``);
    - conflicts: an ``update`` job for ``agent`` (the trial's last agent by default) is opened first — its row holds the trial, so no
      other job is given the worktree while git works in it — then the merge is left in progress in the trial's worktree (made when
      missing), an ``updated`` event (status conflicts: the paths, the base commit, the job) is written, and the job's runner starts;
    - a merge from an earlier update still in progress there (its job failed or was stopped): only the job is sent again;
    - an earlier update dtree has not accepted (its merge is committed on the branch, but its job ended before dtree's check, or the
      check failed): checked again, as at a job's end — accepted (status resolved, verified again), or refused with what is wrong;
    - commits on the branch since an earlier update started, without its merge (its job broke the rules, or a person went on by hand):
      the update starts again from the branch as it is; when the branch holds the base already, there is nothing to merge, and the
      commits are taken as they are (``_take``: an ``updated`` event that says so, and the trial verified again).

    A worktree kept from a job is first brought to the branch's commit, and commits made there that were never brought back (a runner
    that ended first) are brought back, so the update starts from all of the trial's work. Refused while a job runs on the trial, for
    a trial that ended other than adopted, and when the branch is checked out somewhere other than dtree's own worktree for it
    (someone's checkout is not changed under them) — unless the earlier update is settled from its commits alone, as above (a person
    fixed it in that checkout). A worktree is removed only when this call made it."""
    from . import jobs
    s, g, base = project.store, project.git, project.base
    t = s.trial(tid)
    if t["status"] not in ("trying", "measured", "adopted"):
        raise DecisionError("trial %d is %s: only a trial being tried, measured or adopted is updated" % (tid, t["status"]))
    jobs.reap(s)
    busy = _running(s, tid)
    if busy:
        raise DecisionError("job %d is running on trial %d: wait for it or stop it first" % (busy[-1]["id"], tid))
    branch, did = t["branch"], t["decision_id"]
    ours, theirs = g.sha("refs/heads/" + branch), g.sha(base)
    if not ours or not theirs:
        raise DecisionError("no branch %s" % (branch if not ours else base))
    wt = _trial_worktree(project, branch)
    jobs.trust(project, wt)                                          # nothing a job left in the trial's shadow runs in this update (F0)
    where = [w for w in g.checked_out(branch) if not w["prunable"]]
    elsewhere = [w for w in where if not _same(w["path"], wt)]
    ev = _last_update(s, t)
    if elsewhere:
        settled = _settle(project, t, ev, ours, theirs, by)          # reads only commits: that checkout is not touched
        if settled is not None:
            return settled
        raise DecisionError("%s is checked out at %s, not in dtree's worktree for it: update it there, or switch that checkout away"
                            % (branch, elsewhere[0]["path"]))
    # dtree's own worktree for the trial: in the project's list, or one of the trial's shadow repository (jobs._shadow)
    mine = bool(where) or (wt.exists() and g.worktree_dir(wt) is not None and g.symbolic_head(wt) == "refs/heads/" + branch)
    head = g.merge_head(wt) if mine else None
    if head:
        return _resend(project, t, wt, head, agent, by)
    if mine:
        brought = _bring_up(project, t, wt, by)
    elif g.worktree_dir(wt) is None and jobs._shadow_path(project, branch).is_dir():
        brought = _bring_back(project, t, by)                        # its worktree was deleted by hand; its shadow still holds them (F2)
    else:
        brought = ours
    if brought != ours:                                              # commits a runner never brought back: dtree's check comes first
        gate = _unaccepted(project, t, brought)
        if gate is not None and gate[0] == "update_unchecked":
            raise DecisionError("%s (its commits were brought back from %s just now)" % (gate[1], wt if mine else jobs._shadow_path(project, branch)))
    ours = brought
    settled = _settle(project, t, ev, ours, theirs, by)
    if settled is not None:
        return settled
    if g.ancestor(theirs, ours):
        return {"status": "up to date", "trial": tid, "base_sha": theirs, "branch_sha": ours}
    preview = g.merge_tree(ours, theirs, cwd=str(wt) if mine else None)
    if preview["state"] == "error":
        raise DecisionError("%s cannot be updated: %s" % (branch, preview["error"]))
    if preview["state"] == "clean":
        out = _update_clean(project, t, wt if mine else None, by)
        if out is not None:
            return out
    return _update_with_job(project, t, agent, by)


def _bring_up(project: Any, t: Dict[str, Any], wt: Path, by: str) -> str:
    """A worktree of the trial kept from a job and the project's branch, brought to one commit before an update: the worktree forward to
    the branch when the branch moved (as a job's start does: ``jobs._ensure_worktree``), and commits made there that were never brought
    back — a runner that ended before it could — brought into the project (``jobs._sync_back``: a fast-forward, every object checked),
    so the update starts from all of the trial's work and dtree's own check sees what came back. Returns the branch's commit."""
    from . import jobs
    g, branch = project.git, t["branch"]
    before = g.sha("refs/heads/" + branch)
    jobs._ensure_worktree(project, branch)
    ours = g.sha("refs/heads/" + branch)
    here = g.sha("HEAD", cwd=wt)
    if here and ours and here != ours:
        back = jobs._sync_back(project, branch, wt)
        if back:
            raise DecisionError(back)
        ours = g.sha("refs/heads/" + branch)
    if ours != before:
        _record_brought(project, t, wt, by)
    return ours


def _bring_back(project: Any, t: Dict[str, Any], by: str) -> Optional[str]:
    """A trial whose worktree was deleted by hand while its shadow repository still holds commits the project's branch lacks (review F2:
    the next job's start reset them away): they are brought into the project first (``jobs._bring_back_shadow``; refused when the two
    went separate ways) and recorded on the trial. Returns the branch's commit."""
    from . import jobs
    g, branch = project.git, t["branch"]
    before = g.sha("refs/heads/" + branch)
    shadow = jobs._shadow_path(project, branch)
    ours = jobs._bring_back_shadow(project, branch, shadow, before)
    if ours != before:
        _record_brought(project, t, shadow, by)
    return ours


def _record_brought(project: Any, t: Dict[str, Any], where: Path, by: str) -> None:
    commits = project.git.commits_between(project.base, t["branch"])
    if commits:
        project.store.record_code(t["decision_id"], commits, how="recorded", why="brought back from %s by an update" % where, trial=t["id"], by=by)


def _verify_again(project: Any, out: Dict[str, Any], tid: int, by: str) -> Dict[str, Any]:
    from . import verify
    try:
        out["verify"] = verify.start_background(project, tid, by, auto=True)   # the rule of jobs: only what verify names
    except Exception as exc:                                         # the update is made; verification failing to start is only reported
        out["verify_error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def _updated(project: Any, t: Dict[str, Any], res: Dict[str, Any], wt: Optional[Path], by: str) -> Dict[str, Any]:
    """A clean update made (``res`` of ``Git.update_branch``, in worktree ``wt`` or by plumbing when None): brought into the project
    from the trial's shadow, the merge commit recorded on the trial, an ``updated`` event (status clean), and the trial verified again."""
    from . import jobs
    s, g, base, branch = project.store, project.git, project.base, t["branch"]
    if wt is not None:
        back = jobs._sync_back(project, branch, wt)                  # made in the trial's shadow: into the project
        if back:
            raise DecisionError(back)
        res["sha"] = g.sha("refs/heads/" + branch) or res["sha"]
    if res["how"] != "fast-forward":
        s.record_code(t["decision_id"], [{"sha": res["sha"], "subject": "Merge branch '%s' into %s" % (base, branch), "merge": True}],
                      how="recorded", why="updated with %s" % base, trial=t["id"], by=by)
    s.note_event(t["decision_id"], "updated", by, {"trial": t["id"], "base_sha": res["base_sha"], "status": "clean", "conflicts": [], "sha": res["sha"],
                                                   "how": res["how"]})
    out = {"status": "clean", "trial": t["id"], "base_sha": res["base_sha"], "sha": res["sha"], "how": res["how"], "verify": None}
    return _verify_again(project, out, t["id"], by)


def _update_clean(project: Any, t: Dict[str, Any], wt: Optional[Path], by: str) -> Optional[Dict[str, Any]]:
    """An update that the preview found clean: by plumbing (``wt`` None) or in the trial's kept worktree, with no job (and no hook: dtree's
    own git there runs as ``jobs.SAFE_GIT``). None when the real merge conflicts after all (the base or the branch moved since the
    preview): nothing is left changed, and it goes the way of a conflict."""
    from . import jobs
    g, branch = project.git, t["branch"]
    res = g.update_branch(branch, project.base, wt, opts=jobs.SAFE_GIT)
    if res["state"] == "conflicts":
        if res["in_progress"]:
            undo = g.abort_merge(wt, jobs.SAFE_GIT)
            if undo:
                raise DecisionError("%s: the merge conflicted although its preview was clean, and aborting it failed: %s" % (branch, undo))
        return None
    if res["state"] == "error":
        raise DecisionError("%s was not updated: %s" % (branch, res["error"]))
    if res["state"] == "up to date":
        return {"status": "up to date", "trial": t["id"], "base_sha": res["base_sha"], "branch_sha": res["branch_sha"]}
    return _updated(project, t, res, wt, by)


def _update_with_job(project: Any, t: Dict[str, Any], agent: Optional[str], by: str) -> Dict[str, Any]:
    """A conflicting update. Its job's row comes first (``jobs.open_job``, which checks that the agent can run here, the budget and a
    slot): it holds the trial, so no other job is given the worktree while git works there. Then the merge is left in progress in the
    trial's worktree, an ``updated`` event (status conflicts) names it and the job, and the job's runner starts (``jobs.start_job``).
    When git fails or the runner cannot start, the merge is undone — and a worktree made for it removed — while the row still holds the
    trial; then the row is ended."""
    from . import jobs
    s, g, base, branch, did, tid = project.store, project.git, project.base, t["branch"], t["decision_id"], t["id"]
    job = jobs.open_job(project, tid, _update_agent(project, t, agent), _note(project), by=by, kind="update")
    jid = job["id"]
    wt = _trial_worktree(project, branch)
    made = not os.path.lexists(str(wt))

    def drop() -> None:                                              # a worktree made for this update: it holds nothing of anyone's
        if made and g.worktree_dir(wt) is not None:
            jobs._remove_worktree(project, wt)
    try:
        wt = jobs._ensure_worktree(project, branch)
        res = g.update_branch(branch, base, wt, opts=jobs.SAFE_GIT)
    except (DecisionError, OSError, subprocess.TimeoutExpired) as exc:
        res = {"state": "error", "error": str(exc), "in_progress": False}
    if res["state"] == "clean":                                      # the base moved since the preview, and merges cleanly now
        try:
            out = _updated(project, t, res, wt, by)
        finally:
            s.job_end(jid, "failed", "not needed: %s merged into %s without a conflict" % (base, branch))
        drop()                                                       # its merge is in the project now
        return out
    if res["state"] != "conflicts" or not res["in_progress"]:
        drop()
        if res["state"] == "up to date":
            s.job_end(jid, "failed", "not needed: %s is up to date with %s" % (branch, base))
            return {"status": "up to date", "trial": tid, "base_sha": res.get("base_sha"), "branch_sha": res.get("branch_sha")}
        why = res.get("error") or "%s moved meanwhile: try again" % base
        s.job_end(jid, "failed", "the update did not start: %s" % why)
        raise DecisionError("%s was not updated: %s" % (branch, why))
    conflicts = _paths(res["conflicts"])
    # branch_sha: the commit the update starts from — commits past it without the merge are the update's too, until dtree accepts it
    s.note_event(did, "updated", by, {"trial": tid, "base_sha": res["base_sha"], "status": "conflicts", "conflicts": conflicts, "how": "worktree",
                                      "job": jid, "branch_sha": res["branch_sha"]})
    undone: List[str] = []

    def undo(why: str) -> None:                                      # no job will resolve it: back to where it was
        failed = g.abort_merge(wt, jobs.SAFE_GIT)
        if failed and not made:
            undone.append(failed)
            return
        s.note_event(did, "updated", by, {"trial": tid, "base_sha": res["base_sha"], "status": "aborted", "conflicts": conflicts, "note": why})
        drop()
    now = s.job(jid)
    if now["status"] != "running":                                   # ended meanwhile (reap's grace for a row with no runner is 120 s)
        others = [j for j in _running(s, tid) if j["id"] != jid]
        if others:
            raise DecisionError("update job %d was ended before its runner started (%s), and job %d holds trial %d now: the merge is left "
                                "in progress in %s" % (jid, now.get("reason"), others[-1]["id"], tid, wt))
        undo("its job %d was ended before its runner started: %s" % (jid, now.get("reason")))
        raise DecisionError("update job %d was ended before its runner started (%s); %s" % (
            jid, now.get("reason"), undone[0] if undone else "the merge was aborted and %s is unchanged" % branch))
    job = jobs.start_job(project, job, undo=lambda: undo("the job's runner could not start"))
    if job["status"] != "running":
        raise DecisionError("update job %d could not start (%s); %s" % (
            jid, job.get("reason"), undone[0] if undone else "the merge was aborted and %s is unchanged" % branch))
    return {"status": "conflicts", "trial": tid, "base_sha": res["base_sha"], "conflicts": conflicts, "worktree": str(wt), "job": job}


def _check_again(project: Any, t: Dict[str, Any], ev: Dict[str, Any], tip: str, by: str) -> Dict[str, Any]:
    """The trial's last update is committed on its branch but dtree has not accepted it: its job failed, was stopped, or ended before the
    check (a runner that died). The check at a job's end (``_verdict``), now: accepted — an ``updated`` event, status resolved, and the
    trial verified again — or refused with what is wrong."""
    s = project.store
    v = _verdict(project, t, ev, tip)
    if v["state"] != "ok":
        raise DecisionError(_refusal(project, t, v))
    job = _update_job(s, t, ev)
    s.note_event(t["decision_id"], "updated", by, {"trial": t["id"], "base_sha": v["base_sha"], "status": "resolved", "conflicts": list(ev.get("conflicts") or []),
                                                   "sha": v["merge"], "job": job["id"] if job else None,
                                                   "note": "checked again by %s (%s)" % (by, "its job %d %s" % (job["id"], job["status"]) if job else "no job checked it")})
    return _verify_again(project, {"status": "resolved", "trial": t["id"], "base_sha": v["base_sha"], "sha": v["merge"], "verify": None}, t["id"], by)


def _settle(project: Any, t: Dict[str, Any], ev: Optional[Dict[str, Any]], ours: str, theirs: str, by: str) -> Optional[Dict[str, Any]]:
    """An earlier update dtree has not accepted (``ev``: the trial's last ``updated`` event, status conflicts), settled from the branch's
    commits alone — no checkout is touched, so this also runs while the branch is checked out in a person's checkout: its merge committed
    on the branch is checked again (``_check_again``); commits past where it started, without that merge, on a branch that holds the base
    already are taken as they are (``_take``). None when neither applies: the update goes on as usual."""
    if ev is None or ev.get("status") != "conflicts" or not ev.get("base_sha"):
        return None
    g = project.git
    if g.merge_of(ours, str(ev["base_sha"])):
        return _check_again(project, t, ev, ours, by)
    since = _moved_on(project, ev, ours)
    if since and g.ancestor(theirs, ours):
        return _take(project, t, ev, ours, theirs, since, by)
    return None


def _take(project: Any, t: Dict[str, Any], ev: Dict[str, Any], tip: str, theirs: str, since: int, by: str) -> Dict[str, Any]:
    """``dtree update`` on a branch that has moved on since its last update started, without that update's merge, and holds the base
    already (a person merged a newer base into it, say): there is nothing to merge, and answering only "up to date" would keep the merge
    gate shut for good. The commits are taken as they are — the person was told to look at them first — with an ``updated`` event
    (status clean, how "up to date") that says so, and the trial is verified again."""
    s, g, base, branch = project.store, project.git, project.base, t["branch"]
    since_sha = str(ev.get("branch_sha") or "")
    merges = g.out("rev-list", "--first-parent", "--merges", "--parents", *(["%s..%s" % (since_sha, tip)] if since_sha else ["-n", "100", tip])) or ""
    for line in merges.splitlines():                                 # newest first: the merge that brought the base in, checked like any
        parts = line.split()
        if len(parts) >= 3 and g.ancestor(str(ev["base_sha"]), parts[2]):
            marks = g.leftover_markers(parts[0], tip, list(ev.get("conflicts") or []))
            if marks:
                raise DecisionError(_refusal(project, t, {"state": "markers", "marks": marks, "base_sha": str(ev["base_sha"])}))
            break
    job = _update_job(s, t, ev)
    note = "taken as they are by %s: %s on %s since the update with %s %s started at %s (%s), holding %s %s already, so nothing was merged" % (
        by, _commits(since), branch, base, str(ev["base_sha"])[:7], str(ev.get("branch_sha") or "")[:7],
        "its job %d %s" % (job["id"], job["status"]) if job else "no job checked it", base, theirs[:7])
    s.note_event(t["decision_id"], "updated", by, {"trial": t["id"], "base_sha": theirs, "status": "clean", "conflicts": [], "sha": tip, "how": "up to date",
                                                   "note": note})
    out = {"status": "up to date", "trial": t["id"], "base_sha": theirs, "branch_sha": tip, "taken": since, "note": note, "verify": None}
    return _verify_again(project, out, t["id"], by)


def _resend(project: Any, t: Dict[str, Any], wt: Path, head: str, agent: Optional[str], by: str) -> Dict[str, Any]:
    """A merge from an earlier update is still in progress in the trial's worktree: send a job to finish it. Its ``updated`` event
    (status conflicts) stays the one the job is checked against; the job's own ``ai_sent`` records the sending. The merge stays as it is
    when the job cannot start."""
    from . import jobs
    s, g = project.store, project.git
    ev = _last_update(s, t, "conflicts")
    if ev is None or ev.get("base_sha") != head:
        raise DecisionError("a merge of %s that dtree did not start is in progress in %s: finish or abort it there" % (head[:10], wt))
    job = jobs.send(project, t["id"], _update_agent(project, t, agent), _note(project), by=by, kind="update")
    return {"status": "conflicts", "trial": t["id"], "base_sha": head, "conflicts": _paths(g.unmerged(wt)) or list(ev.get("conflicts") or []),
            "worktree": str(wt), "job": job, "resumed": True}


def abort_update(project: Any, tid: int, by: str = "user") -> Dict[str, Any]:
    """Abort a trial's update whose merge is in progress (``git merge --abort`` in its worktree: back to the branch's tip, unrelated
    edits kept); the worktree is removed when nothing but caches is left. Refused while a job runs on the trial."""
    from . import jobs
    s, g = project.store, project.git
    t = s.trial(tid)
    jobs.reap(s)
    busy = _running(s, tid)
    if busy:
        raise DecisionError("job %d is running on trial %d: stop it first (dtree stop %d)" % (busy[-1]["id"], tid, busy[-1]["id"]))
    wt = _trial_worktree(project, t["branch"])
    jobs.trust(project, wt)                                          # git merge --abort there runs nothing a job left (F0)
    head = g.merge_head(wt)
    if not head:
        raise DecisionError("no update of trial %d is in progress (no merge in %s)" % (tid, wt))
    ev = _last_update(s, t, "conflicts") or {}
    left = g.unmerged(wt)
    why = g.abort_merge(wt, jobs.SAFE_GIT)
    if why:
        raise DecisionError(why)
    s.note_event(t["decision_id"], "updated", by, {"trial": tid, "base_sha": head, "status": "aborted", "conflicts": ev.get("conflicts") or _paths(left)})
    removed = False if _running(s, tid) else _tidy(project, wt)       # a job sent meanwhile was given this worktree: it stays
    return {"status": "aborted", "trial": tid, "base_sha": head, "branch_sha": g.sha("refs/heads/" + t["branch"]), "worktree": str(wt),
            "removed": removed}


# ------------------------------------------------------------------------------------------------------------- the update job
def prompt_lines(project: Any, job: Dict[str, Any], trial: Dict[str, Any], worktree: Any) -> List[str]:
    """For an update job: the base and its commit, the conflicted paths (from the worktree, else the event), and how to finish."""
    if (job.get("kind") or "build") != "update":
        return []
    g, base, branch = project.git, project.base, trial["branch"]
    ev = _last_update(project.store, trial, "conflicts") or {}
    merging = g.merge_head(worktree)
    head = merging or str(ev.get("base_sha") or "?")
    left = _paths(g.unmerged(worktree))
    files = list(dict.fromkeys(left + list(ev.get("conflicts") or []))) if merging else (left or list(ev.get("conflicts") or []))
    resolved = [f for f in files if f not in left] if merging else []   # staged by git rerere (a recorded resolution), or an earlier job
    staged = bool(merging) and not left
    lines = ["", "UPDATE — this job brings %s up to date with %s; it builds nothing new." % (branch, base),
             "A merge of %s (%s) into %s is in progress in this worktree; it stopped on conflicts in %d file%s:" % (base, head[:10], branch, len(files),
                                                                                                                "" if len(files) == 1 else "s")]
    lines += ["  " + f for f in files[:100]] + (["  … and %d more (git status lists them all)" % (len(files) - 100)] if len(files) > 100 else [])
    if resolved:
        lines += [("None of them is unmerged any more" if staged else "Of these, %s %s already staged" % (", ".join(resolved[:20]),
                   "is" if len(resolved) == 1 else "are")) + ": git rerere staged a resolution it had recorded (or an earlier job did).",
                  "Do not take that on trust: check each file against both intents before you commit."]
    lines += [
        "- Resolve each conflict so that both intents stay: what this branch does for its option, and what %s changed meanwhile. Never" % base,
        "  drop one side to make it merge; where both cannot stay, keep the option's behaviour and say in the note what was given up.",
        ("  git show HEAD:<path> is this branch's side, git show MERGE_HEAD:<path> is %s's, git diff --cached HEAD what the merge brings." % base
         if staged else "  git show :2:<path> is this branch's side, git show :3:<path> is %s's, git show :1:<path> their common ancestor." % base),
        "- Then run the tests.",
        "- Finish the merge with exactly: git add <the resolved files>, then git commit --no-edit --cleanup=strip (that commit completes it).",
        "- Never run git merge (nor --abort or --continue), git rebase, git reset, git checkout, git switch, git restore or git stash here.",
        "- dtree checks afterwards: no merge left in progress, the merge commit on %s, and no conflict marker left in these files." % branch,
        "- The result: built true only when the merge is committed; tests with their counts after the merge; impact: what this option",
        ("  changes against the answer taken now (as before the update), then one line on how the conflicts were resolved."
         if project.store.get(trial["decision_id"]).get("answer") else
         "  changes in the project (as before the update), then one line on how the conflicts were resolved."),
    ]
    if trial.get("result_tests") or trial.get("result_impact"):
        lines.append("  The trial's last result: tests %s · impact %s" % (trial.get("result_tests") or "-", trial.get("result_impact") or "-"))
    return lines


def after_job(project: Any, job: Dict[str, Any], trial: Dict[str, Any], say: Callable[[str], None]) -> Optional[str]:
    """For an update job, dtree checks the agent's merge itself: none is left in progress in the worktree; the branch's tip is (or
    follows on its first-parent line) a merge commit whose second parent is the base commit of the update; no conflict marker is left in
    the files that conflicted. None when all hold (an ``updated`` event, status resolved), else the reason — the worktree is left as
    it is for the person."""
    if (job.get("kind") or "build") != "update":
        return None
    try:
        return _check_update(project, job, trial, say)
    except Exception as exc:                                         # the runner calls this unguarded: a crash would leave the job running
        return "the update could not be checked: %s: %s" % (type(exc).__name__, exc)


def _verdict(project: Any, t: Dict[str, Any], ev: Dict[str, Any], tip: Optional[str], wt: Optional[Path] = None) -> Dict[str, Any]:
    """dtree's own check of an update — ``ev``, the trial's ``updated`` event of status conflicts — as of ``tip`` of its branch: the one
    check behind the update job's end (``after_job``), the merge gate (``readiness``) and ``dtree update`` checking again. {state:
    in_progress (a merge is still in progress in ``wt``) · not_committed (no merge commit of that base commit on the branch's first-parent
    line) · markers (conflict markers, in neither parent, left in the files that conflicted) · ok; base_sha, merge, marks, paths,
    unresolved}."""
    g = project.git
    theirs = str(ev.get("base_sha") or "")
    out: Dict[str, Any] = {"state": "not_committed", "base_sha": theirs, "merge": None, "marks": [], "paths": [], "unresolved": []}
    if wt is not None and g.merge_head(wt):
        out.update(state="in_progress", unresolved=g.unmerged(wt))
        return out
    merge = g.merge_of(tip, theirs) if tip and theirs else None
    if not merge:
        return out
    parents = (g.out("rev-list", "--parents", "-n", "1", merge) or "").split()[1:3]
    paths = set(ev.get("conflicts") or [])
    if len(parents) == 2:
        paths |= set(g.merge_tree(parents[0], parents[1])["conflicts"])
    marks = g.leftover_markers(merge, tip, sorted(paths))
    out.update(state="markers" if marks else "ok", merge=merge, marks=marks, paths=sorted(paths))
    return out


def _refusal(project: Any, t: Dict[str, Any], v: Dict[str, Any], wt: Any = None) -> str:
    """Why a ``_verdict`` that is not ok refuses the update, in English."""
    base, branch, b7 = project.base, t["branch"], v["base_sha"][:7]
    if v["state"] == "in_progress":
        left = v["unresolved"]
        return "the merge of %s %s is not finished in %s: %s" % (base, b7, wt, ("not resolved yet: " + ", ".join(_paths(left, 10)))
                                                                 if left else "resolved but not committed (git commit --no-edit)")
    if v["state"] == "not_committed":
        return "%s has no merge commit of %s %s: the merge was not committed" % (branch, base, b7)
    marks = v["marks"]
    return "conflict markers are left in %s: fix them on %s and commit, then dtree update %d checks it again" % (
        ", ".join(_paths(marks, 10)) + (" …" if len(marks) > 10 else ""), branch, t["id"])


def _update_job(store: Any, t: Dict[str, Any], ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The job of update event ``ev``: the one it names, else the trial's last update job."""
    if ev.get("job"):
        try:
            return store.job(int(ev["job"]))
        except (KeyError, TypeError, ValueError):
            pass
    ups = [j for j in store.jobs(tid=t["id"]) if (j.get("kind") or "build") == "update"]
    return ups[-1] if ups else None


def _moved_on(project: Any, ev: Dict[str, Any], tip: Optional[str]) -> int:
    """How many commits ``tip`` has, on its first-parent line, past the commit the update of ``ev`` started from (its ``branch_sha``).
    0 when the branch is still there, when it no longer holds that commit (put back, rewritten: nothing the update made is on it), and
    for an event written before ``branch_sha`` was kept."""
    g, start = project.git, str(ev.get("branch_sha") or "")
    if not start or not tip or tip == start or g.ancestor(start, tip) is False:
        return 0
    n = (g.out("rev-list", "--count", "--first-parent", "%s..%s" % (start, tip)) or "").strip()
    return int(n) if n.isdigit() and int(n) > 0 else 1                # git could not count them: at least one, the gate stays shut


def _unaccepted(project: Any, t: Dict[str, Any], tip: Optional[str]) -> Optional[Tuple[str, str, str]]:
    """The merge gate's view of updates: (reason_code, reason, reason_th) when the trial's branch holds an update dtree has not accepted
    — its last ``updated`` event is a conflict whose merge commit is on the branch, but dtree's check never passed (the job failed, was
    stopped, or ended before the check), or commits came onto the branch after the update started without that merge (the job dropped
    the merge and committed plainly) — else None. Conflict markers left are named (``markers``); otherwise ``update_unchecked``:
    ``dtree update`` checks it again, or starts the update again from the branch as it is. Readiness asks this only when no job runs on
    the trial, so the update's job has ended."""
    s, base, tid = project.store, project.base, t["id"]
    ev = _last_update(s, t)
    if ev is None or ev.get("status") != "conflicts" or not ev.get("base_sha") or not tip:
        return None
    b7 = str(ev["base_sha"])[:7]
    try:
        v = _verdict(project, t, ev, tip)
        since = _moved_on(project, ev, tip) if v["state"] == "not_committed" else 0
    except Exception as exc:                                         # the gate stays shut; the row still renders
        return ("update_unchecked", "the update with %s %s could not be checked (%s: %s): dtree update %d checks it again" % (base, b7, type(exc).__name__, exc, tid),
                "ตรวจการอัปเดตจาก %s %s ไม่ได้: สั่ง dtree update %d เพื่อตรวจอีกครั้ง" % (base, b7, tid))
    if v["state"] == "not_committed":
        if not since:
            return None                                              # it never landed on the branch, nor did anything since it started
        job = _update_job(s, t, ev)
        how = ("update job %d %s%s" % (job["id"], job["status"], (": " + str(job["reason"])[:160]) if job.get("reason") else "")
               if job else "no update job checked it")
        start, them = str(ev["branch_sha"])[:7], "it" if since == 1 else "them"
        return ("update_unchecked", "%s has %s since the update with %s %s started at %s, without that merge (%s): dtree has not checked %s — "
                "look at %s (git log --first-parent %s..%s); dtree update %d starts the update again from there"
                % (t["branch"], _commits(since), base, b7, start, how, them, them, start, t["branch"], tid),
                "%s มี %d commit หลังจุดที่เริ่มอัปเดตจาก %s %s (%s) โดยไม่มีการรวมนั้น (%s): dtree ยังไม่ได้ตรวจ — ดูก่อน "
                "(git log --first-parent %s..%s) แล้วสั่ง dtree update %d เพื่อเริ่มอัปเดตใหม่จากตรงนั้น"
                % (t["branch"], since, base, b7, start, how, start, t["branch"], tid))
    if v["state"] == "markers":
        listed = ", ".join(_paths(v["marks"], 10)) + (" …" if len(v["marks"]) > 10 else "")
        return ("markers", "the update with %s %s left conflict markers in %s on %s: fix them there and commit, then dtree update %d checks "
                "it again" % (base, b7, listed, t["branch"], tid),
                "การอัปเดตจาก %s %s ทิ้งเครื่องหมาย conflict ไว้ใน %s บน %s: แก้แล้ว commit จากนั้นสั่ง dtree update %d เพื่อตรวจอีกครั้ง"
                % (base, b7, listed, t["branch"], tid))
    job = _update_job(s, t, ev)
    how = ("update job %d %s%s" % (job["id"], job["status"], (": " + str(job["reason"])[:160]) if job.get("reason") else "")
           if job else "no update job checked it")
    return ("update_unchecked", "the update with %s %s is committed on %s (%s) but dtree has not accepted it (%s): dtree update %d checks it"
            % (base, b7, t["branch"], v["merge"][:7], how, tid),
            "การอัปเดตจาก %s %s ถูก commit บน %s แล้ว แต่ dtree ยังไม่ได้ตรวจรับ (%s): สั่ง dtree update %d เพื่อตรวจ" % (base, b7, t["branch"], how, tid))


def _check_update(project: Any, job: Dict[str, Any], trial: Dict[str, Any], say: Callable[[str], None]) -> Optional[str]:
    s, base = project.store, project.base
    t = s.trial(trial["id"])
    ev = _last_update(s, t, "conflicts")
    if ev is None or not ev.get("base_sha"):
        return "no update of trial %d is recorded (an updated event with its conflicts): nothing to check" % t["id"]
    theirs, branch = str(ev["base_sha"]), t["branch"]
    wt = Path(job.get("worktree") or _trial_worktree(project, branch))
    v = _verdict(project, t, ev, project.git.sha("refs/heads/" + branch), wt)
    if v["state"] != "ok":
        return _refusal(project, t, v, wt)
    s.note_event(t["decision_id"], "updated", job.get("agent") or "ai", {"trial": t["id"], "base_sha": theirs, "status": "resolved",
                                                                       "conflicts": list(ev.get("conflicts") or []), "sha": v["merge"], "job": job["id"]})
    say("✓ update: %s %s is merged into %s (%s); no conflict marker left in the %s that conflicted" % (base, theirs[:7], branch, v["merge"][:7],
                                                                                                       _files(len(v["paths"]))))
    return None


# ------------------------------------------------------------------------------------------------------------- API and CLI
def register_routes(route: Callable, project: Any) -> None:
    S = project.store

    @route("GET", r"/api/decisions/trials/(\d+)/merge-check")
    def merge_check_(q, b, tid):
        return {"merge": readiness(project, S.trial(int(tid)))}

    @route("POST", r"/api/decisions/trials/(\d+)/merge")
    def merge_trial_(q, b, tid):
        close = b.get("close", False)
        if not isinstance(close, bool):
            raise DecisionError("close must be true or false")
        return merge_trial(project, int(tid), by="user", close=close)

    @route("POST", r"/api/decisions/(\d+)/merge")
    def merge_decision_(q, b, did):
        return merge_decision(project, int(did), by="user")

    @route("POST", r"/api/decisions/trials/(\d+)/update")
    def update_(q, b, tid):
        agent = b.get("agent")
        if agent is not None and not isinstance(agent, str):
            raise DecisionError("agent must be text")
        return {"update": update_trial(project, int(tid), agent or None, by="user")}

    @route("POST", r"/api/decisions/trials/(\d+)/update/abort")
    def abort_(q, b, tid):
        return {"update": abort_update(project, int(tid), by="user")}


def register_cli(sub: Any) -> None:
    p = sub.add_parser("merge-check", help="whether a trial merges into the base now, and why not (exit 1 when it cannot)")
    p.add_argument("trial", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("update", help="bring a trial's branch up to date with the base (an agent resolves conflicts)")
    p.add_argument("trial", type=int)
    p.add_argument("--agent", default=None)
    p.add_argument("--abort", action="store_true", help="abort an update whose merge is in progress")
    merge_cmd = sub.choices.get("merge")                             # dtree merge ID [--trial T] is cli.py's; --close is added here
    if merge_cmd is not None:
        merge_cmd.add_argument("--close", action="store_true", help="with --trial: mark the question done with the trial's measurement")


def check_lines(r: Dict[str, Any]) -> List[str]:
    """A readiness for the terminal."""
    head = "%s → %s: %s" % (r["branch"], r["base"], r["state"])
    if r.get("behind") is not None:
        head += " · %d behind, %d ahead" % (r["behind"], r["ahead"])
    if r.get("merge_base"):
        head += " · from %s" % r["merge_base"][:7]
    lines = [head] + ["  conflict: " + p for p in r.get("conflicts") or []]
    if r.get("conflict_count", 0) > len(r.get("conflicts") or []):
        lines.append("  … %d more" % (r["conflict_count"] - len(r["conflicts"])))
    for w in r.get("base_worktree") or []:
        lines.append("  %s is %s at %s" % (r["base"], w["how"], w["path"]))
    lines.append("can merge: %s — %s" % ("yes" if r["can_merge"] else "no", r["reason"]))
    return lines


def _update_line(u: Dict[str, Any], tid: int, base: str) -> str:
    if u["status"] == "up to date":
        line = "trial %d is up to date with %s (%s)" % (tid, base, (u.get("base_sha") or "")[:7])
        if u.get("taken"):                                           # an earlier update's commits taken as they are (_take)
            line += "; %s%s" % (u["note"], ("; verifying in the background (pid %s)" % u["verify"]) if u.get("verify") else "")
        return line
    if u["status"] == "clean":
        verifying = ("; verifying in the background (pid %s)" % u["verify"]) if u.get("verify") else ""
        return "trial %d updated with %s %s: clean, %s %s%s" % (tid, base, u["base_sha"][:7], u["how"], u["sha"][:7], verifying)
    if u["status"] == "resolved":
        verifying = ("; verifying in the background (pid %s)" % u["verify"]) if u.get("verify") else ""
        return "trial %d: the update with %s %s is checked and accepted (merge %s)%s" % (tid, base, u["base_sha"][:7], u["sha"][:7], verifying)
    if u["status"] == "aborted":
        return "the update of trial %d is aborted: the branch is back at %s%s" % (tid, (u.get("branch_sha") or "")[:7], " (worktree removed)" if u.get("removed") else "")
    j = u["job"]
    return "trial %d: %s conflict with %s %s — job %d (%s, %s) resolves them in %s%s" % (
        tid, _files(len(u["conflicts"])), base, u["base_sha"][:7], j["id"], j.get("agent"), j["status"], u["worktree"], " (sent again)" if u.get("resumed") else "")


def run_cli(args: Any, project: Any) -> bool:
    cmd = getattr(args, "cmd", None)
    if cmd == "merge-check":
        r = readiness(project, project.store.trial(args.trial))
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            print("\n".join(check_lines(r)))
        if not r["can_merge"]:                                       # so a script can rely on it: dtree merge-check T && dtree merge …
            raise SystemExit(1)
        return True
    if cmd == "update":
        u = abort_update(project, args.trial, by="cli") if args.abort else update_trial(project, args.trial, args.agent, by="cli")
        print(_update_line(u, args.trial, project.base))
        if u["status"] == "conflicts":
            for p in u["conflicts"][:20]:
                print("  conflict: " + p)
        return True
    if cmd == "merge" and getattr(args, "close", False):
        from .cli import merge as cli_merge                           # cli.py's own merge(), which takes close
        print(cli_merge(project, args.id, args.trial, close=True))
        return True
    return False
