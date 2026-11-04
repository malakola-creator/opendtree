

Once a trial is chosen, dtree says beforehand whether it merges into the base, which files would conflict and how far the base has
moved. It merges without switching anyone's checkout. When the base has moved on, it brings the trial up to date; if that conflicts,
an agent resolves it and dtree checks the result itself. The code is `src/dtree/merge.py` (what dtree does) on top of
`src/dtree/gitcode.py` (the git calls). Every rule below comes from probes of git 2.43 on this machine (2026-11-01).

## 1. Readiness

`dtree merge-check TRIAL`, `GET /api/decisions/trials/<tid>/merge-check`, and the `merge` field of every row of `dtree compare` /
`GET /api/decisions/<id>/compare`. It touches nothing: the preview is `git merge-tree --write-tree` on the two commits, which writes
only unreachable objects (never the index, a working tree or a ref), and no fetch is made.

It takes about 15–20 ms on a clone of ultimateTruth. The exception is a stale index (a fresh clone, files touched without changes):
then `git status` of the base's checkout hashes those files again every time, about 330 ms there, because with optional locks off
it may not refresh the index. So that one look is kept for 3 s, for all the rows of a compare table. A merge always looks again.

| field | what it is |
|---|---|
| `state` | `clean` · `conflicts` · `merged` (the branch has nothing the base lacks) · `error` (no branch, unrelated histories, …) |
| `conflicts` · `conflict_count` | the paths that would conflict (at most 200 listed), from `-z` output: Thai names, tabs and newlines come through as they are |
| `behind` · `ahead` | commits in the base since the branch left it, and the branch's own (`rev-list --left-right --count base...branch`) |
| `base_sha` · `branch_sha` · `merge_base` · `tree` | the commits compared, where the branch left the base, the merged tree |
| `base_worktree` | where the base is checked out: `[{path, how}]`, `how` = checked out · rebasing · bisecting |
| `how` | how a merge would run now: `plumbing` or `worktree` |
| `updating` | an update in progress in the trial's worktree: `{worktree, base_sha, unresolved}` |
| `can_merge` · `reason` · `reason_th` · `reason_code` | whether it can be merged now, and why not (English, Thai, a stable code) |

`reason_code`, in the order they are checked: `error` · `job_running` (a job runs on the trial) · `untrusted` (a symbolic link stands
where dtree keeps a directory in the trial's shadow repository, or a `.git` directory — a repository dtree did not make — in its
worktree: dtree runs no git there, §4 — look at it and remove or move it) ·
`updating` · `not_brought_back` (dtree's worktree of the trial holds commits the project's branch lacks — a runner that ended before
it brought them back — or the two went separate ways: they would not be merged; `dtree update TRIAL` brings them back. Also when that
worktree was deleted by hand and only the shadow repository holds them) · `markers` (an update left conflict markers on
the branch: dtree's own check failed) · `update_unchecked` (an update's merge is on the branch but dtree has not accepted it: its job
failed, was stopped, or ended before the check; or commits came onto the branch after the update started, without its merge — §3) ·
`merged` (dtree recorded its merge) · `branch_dirty` (the branch is checked out somewhere, dtree's own worktree of the trial included,
with uncommitted tracked changes: they would not be merged) · `nothing` (no commits the base lacks) · `conflicts` · `not_adopted` (only
an adopted trial is merged; the reason says what to do for its status — a measured one is adopted with `dtree end TRIAL adopted`, a
dropped one cannot be adopted, so its option is tried again on the same branch: `dtree try ID OPTION`) · `not_decided` (a question's
own branch: only when decided or done) · `base_busy` (the base is being
rebased or bisected, or checked out twice) · `method` (`merge.method` plumbing but the base is checked out) · `base_dirty` (the base's
checkout has uncommitted tracked changes or an operation in progress) · `in_the_way` (the merge would write over something git does not
track there: an untracked or ignored file at a path it adds, or a file or symbolic link where it makes a directory) · `ready`.

dtree's worktree of a trial (`<worktrees>/alt-<id>-<option>`, kept after a job that left work in it) belongs to the trial's shadow
repository, so `git worktree list` of the project does not show it; readiness looks at it by its place. One that is only behind the
branch (a person committed on the branch since) is fine: the next job's start brings it forward. When that directory is gone (deleted
by hand) but the shadow repository is not, readiness compares the shadow's branch itself, so commits only it holds still shut the gate.

`dtree merge-check TRIAL` exits 1 when the trial cannot be merged now (any `reason_code` but `ready`), so a script can rely on it:
`dtree merge-check T && dtree merge ID --trial T --close`.

## 2. Merging

`dtree merge ID --trial TRIAL [--close]`, `POST /api/decisions/trials/<tid>/merge {close}` for an adopted trial;
`dtree merge ID`, `POST /api/decisions/<id>/merge` for a question's own `dec/<id>-<option>` branch (from `dtree start`). Nobody's
checkout is switched (the old `dtree merge` ran `git switch` in the main checkout). The commit merged is the one the readiness
previewed, so commits arriving on the branch meanwhile are not merged unseen.

- **The base is checked out nowhere → plumbing.** `merge-tree`, then `commit-tree -p <base> -p <branch>` with the message passed
  through `git stripspace`, then `update-ref refs/heads/<base> <new> <old>`: a compare-and-swap. When the base moved meanwhile, it
  starts over (3 tries). The commit is byte for byte the one `git merge --no-ff -m` makes (the probe and `tests/test_merge.py` compare
  the commit ids). No hook runs on this path.
- **The base is checked out in one worktree → `git merge --no-ff --no-log -m` there**, only when nothing there can be lost:
  no tracked change, no merge / rebase / cherry-pick / revert / bisect in progress, and nothing git does not track where the merge
  writes. That is an untracked or ignored file at a path the merge adds, or a file or symbolic link at a directory it makes (`cache`
  for `cache/new.txt`). git refuses an untracked file, but it overwrites an ignored one silently and replaces an ignored file or link
  by the directory. A tracked file that the merge itself turns into a directory is git's own business. The hooks of that checkout
  run. How it ends is read from the checkout, not assumed from git's exit code:
  - the merge commit is HEAD → merged (also when git was killed after it, in a `post-merge` hook: then with a `warning`). A
    `post-merge` hook that commits moves HEAD on past it: the merge commit is then taken from HEAD's first-parent line (the commit whose
    parents are the base as it was and the branch), recorded as the merge, with a `warning`;
  - the merge is left in progress (MERGE_HEAD is the branch merged: a conflict, a failing hook, a commit signature that fails, a
    stale `index.lock`) → it is aborted and the base is as it was. When the abort fails too, the reason says what is left;
  - exit 2 → git's refusal before it touched anything;
  - anything else (killed after 600 s, a hook that hangs; a write that failed, a full disk) → the reason quotes `git status` there and
    says how to put it back (`git reset --merge`, or `git merge --abort`, after removing a stale `index.lock`). It is the person's
    checkout: dtree does not reset it, and it never calls this "unchanged".
- **Every other git command** of a merge is bounded too. Plumbing writes only an unreferenced commit before `update-ref`, so a
  failure there changes nothing; an `update-ref` that did not finish is judged by where the branch is afterwards.
- **Refused** while the base is being rebased or bisected anywhere: `git worktree list` shows such a worktree as detached, so dtree
  reads git's own state files (`rebase-merge/head-name`, `rebase-apply/head-name`, `BISECT_START`), as git does before it lets a
  branch be checked out elsewhere. (`git branch -f b b` gives the same answer, but it takes the branch's lock and writes the branch
  back without an old value, so it could undo a merge landing at that moment.)
- **Recorded**: the branch's commits and the merge commit on the question and on the trial (`how: merged`), and a `merged` event
  `{trial, branch, sha, how}`. The branch is kept.
- **`close`**: the question is marked done. Its tests are dtree's own measurement when it verified the merged commit (the `verified`
  event: "dtree verify <sha>: … · base …: …" with the measures), then what the agent reported; its impact is the trial's.

The message is `Merge #<id> <branch>: <title>`, a line per commit (at most 100), and `merge_trailer` when set.

## 3. Updating a trial with the base

`dtree update TRIAL [--agent NAME]`, `POST /api/decisions/trials/<tid>/update {agent}`, for a trial being tried, measured or adopted
with no job running. The base is merged into the trial's branch (git's own message: `Merge branch '<base>' into <branch>`):

- **First, the trial's kept worktree.** The worktree a job left (`<worktrees>/alt-<id>-<option>`, in the trial's shadow repository
  `<worktrees>/.shadow/<branch>.git`) is brought to the branch's commit, forward as at a job's start (`jobs._ensure_worktree`).
  Commits made there that a runner never brought back (it ended first) are brought into the project, fast-forward only and with every
  object checked (`jobs._sync_back`). So the update, and dtree's check of it, start from all of the trial's work. When the worktree's
  directory was deleted by hand, the commits only the shadow holds are brought back the same way (refused when the two went separate
  ways), as a job's start does too: neither ever resets the shadow's branch over them (review F2, 2026-11-02).
- **Clean**: by plumbing when the branch is checked out nowhere (a fast-forward when it has no commits of its own), else
  `git merge --no-ff --no-edit` in dtree's worktree for it, then brought back into the project. An `updated` event (status `clean`),
  the merge commit is recorded on the trial, and the trial is verified again in the background (`verify.start_background`). The trial's
  status does not change: an update job of a trial still being tried leaves it `trying` (jobs.run). dtree's own git in a trial's
  worktree runs as `jobs.SAFE_GIT` says: no hook and no fsmonitor, as on the plumbing path and for the agents' commits in the sandbox.
  A merge that git stops there without a conflict (say `commit.gpgSign` with a gpg that fails) is aborted and its reason is the
  error. No job is sent to "resolve" it. When the real merge does conflict after a clean preview (the base moved meanwhile), it is
  aborted and handled as a conflict.
- **Conflicts**: the update job's row is opened first (`jobs.open_job`: the agent can run here, the budget allows it, a job slot is
  free). Nothing is touched when it cannot. The row holds the trial, so a job sent meanwhile is refused instead of being given the same
  worktree. Then the merge is made in the trial's worktree and **left in progress**, and an `updated` event (status `conflicts`, the
  paths, the base commit, the `job`, and `branch_sha`: the commit the update starts from) is written. Then the job's runner starts
  (`jobs.start_job`); the agent is by default the trial's last one. Its prompt names the base and its commit and lists the conflicted
  paths. The agent must keep both intents (the option's and the base's), run the tests, and finish with only `git add <files>` and
  `git commit --no-edit --cleanup=strip`. It must never use `git merge` (nor `--abort` / `--continue`), rebase, reset, checkout,
  switch, restore or stash. When git fails or the runner cannot start, the merge is aborted (event `aborted`) and a worktree made for it
  removed while the row still holds the trial. Then the row is ended `failed` with the reason.
- **rerere**: with `rerere.enabled` and `rerere.autoupdate` in the person's git config (dtree's git reads it), git stages a resolution
  it recorded earlier and stops with exit 1 and MERGE_HEAD but no unmerged path. That is still a conflict in progress: the job is sent,
  its prompt says that git staged a recorded resolution and asks the agent to check it before committing, and the event's paths are the
  preview's, all of them (the prompt names those rerere staged). A merge that could not take `index.lock` (a stale lock, or another
  git holding it for a moment) ends the same way but writes nothing: dtree tells the two apart by what is staged (`git diff-index
  --cached HEAD`), never by whether a lock is there, and aborts the second as an error instead of sending a job on an index that holds
  nothing of the merge (its commit would have dropped the base's changes).
- **dtree checks the agent's merge itself** when the job ends (`merge.after_job`):
  - no merge is left in progress in the worktree;
  - the branch's tip is (or follows, on its first-parent line) a merge commit whose second parent is the base commit of the update.
    A fix committed after the merge commit is accepted;
  - no conflict marker is left in the files that conflicted.

  A marker counts only when it is in neither parent. `git diff --check` against the first parent alone also flags a Markdown `=======`
  underline that the base brought in; the probe saw that false positive. When all three hold, the event `updated` (status `resolved`)
  is written and the job ends measured; verification runs as after any job. Otherwise the job fails with the reason, and the
  worktree stays as it is for the person (when the agent committed, it is clean and goes; the branch holds the merge).
- **Not merged until dtree accepts it.** The same check (`merge._verdict`) gates the merge. While the trial's last `updated` event is
  `conflicts` and that merge is on the branch, readiness refuses the trial. It says `markers` with the places when markers are left.
  It says `update_unchecked` when the merge is committed but the job failed, was stopped or ended before the check (a timeout, a crash,
  a runner that died). `dtree update TRIAL` then checks it again: accepted (event `resolved` with a `note`, the trial verified again
  in the background), or refused with what is wrong — fix it on the branch, commit, and run it again. This check reads only commits,
  so it also runs while the branch is checked out in a checkout of the person's, where they fixed it. (Before this gate, a job that
  failed on its markers left them on the branch, readiness said ready, and `merge --close` put them into the base.)
- **Commits without the merge.** An agent can drop the merge (`git reset`, which the prompt forbids) and commit plainly; dtree's check
  fails the job ("no merge commit of the base"). Commits on the branch past the event's `branch_sha` without that merge are held back
  too: `update_unchecked`, "look at them (`git log --first-parent <start>..<branch>`); `dtree update TRIAL` starts the update again
  from there". That update merges the base into the branch as it is (clean, or a new job for the conflicts). When the branch holds the
  base already (say the person merged a newer base into it by hand), there is nothing to merge: the commits are taken as they are,
  with an `updated` event (status `clean`, how `up to date`, a `note` that says so), and the trial is verified again. The answer is
  `{status: "up to date", taken: <commits>, note}`. A branch put back to `branch_sha` (or rewritten without it) holds nothing of the
  update, and the gate opens. (Before, a job that took the base's side in a plain commit had its job failed, readiness said ready, and
  `merge --close` merged the commit and closed the question with the option gone.)
- **Again / abort**: `dtree update TRIAL` on a trial whose merge is still in progress sends a new job for it (another agent with
  `--agent`). `dtree update TRIAL --abort` (`POST …/update/abort`) runs `git merge --abort` there. That takes the branch back to its tip
  and keeps unrelated edits. The worktree is removed when only caches are left and no job was given it meanwhile, and the event
  `updated` (status `aborted`) is written.
- **Not here**: a trial branch checked out somewhere other than dtree's worktree for it (a person's checkout) is not updated under
  them; update it there. Only an earlier update settled from its commits alone (checked again, or taken as it is, above) is done while
  it is checked out there. A worktree at dtree's place that is not on the branch (someone went detached there) is left alone: an
  update removes only a worktree that the same call made.

## 4. Several things at once

- Every git call dtree makes runs with `GIT_OPTIONAL_LOCKS=0`. A plain `git status` rewrites the index to refresh stat data, and an
  observer polling it in an agent's worktree made 92 of the agent's 300 git calls fail on `index.lock` in the probe; with optional
  locks off, none failed. Unmerged paths are read with `ls-files -u`, which never writes the index.
- One git writer per worktree: one job per trial, and no update or merge of a trial while its job runs. An update that needs a job
  holds the trial with the job's row before it touches git (above). Different worktrees can commit at the same time (0 failures in the
  probe's stress runs, with gc and pack-refs running alongside).
- `update-ref` with the old value means two merges into the same base never lose each other: one wins and the other starts over. A
  plumbing update that loses to the same update (a double click) asks again whether the branch is up to date, so it makes no empty
  second merge.
- dtree's git calls never inherit `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` from a parent git (a hook that ran dtree), and git
  never reads dtree's stdin (`dtree mcp` speaks over it).
- `jobs._ensure_worktree` runs `git worktree prune` in the trial's shadow repository before it adds the trial's worktree. A worktree
  directory deleted by hand otherwise still holds its branch there: git reports "missing but already registered". The project's own
  repository is not pruned on these paths: a shadow's worktree is not in its list, and the list holds other tools' entries too (e.g.
  `.claude/worktrees/agent-*` in ultimateTruth). A shadow (and dtree's worktree of it) is removed only when the project's branch holds
  all its commits; otherwise it stays for the person.
- **What a job wrote in its shadow runs nothing here** (review F0, 2026-11-02). A job can write the trial's shadow repository: Codex's
  own sandbox makes the whole git directory writable (`--add-dir`), config and hooks included. dtree's own git there runs on the host,
  outside any sandbox: `git status` after the job and in every readiness poll, the merge of an update and its signing, `git merge
  --abort`, `git worktree add`. A clean or smudge filter, a merge driver, `gpg.program` with `commit.gpgSign`, `core.hooksPath`, a
  hook, a `config.worktree`, or a `.git` file / `commondir` naming a repository of the job's making would run or be followed there.
  So when dtree makes the shadow and its worktree it keeps what it made them with beside the shadow — `<worktrees>/.shadow/<branch>.pin.json`,
  which no job's sandbox can write — and before each of its git commands there (`jobs.trust`: the runner after the agent,
  `_sync_back`, `_ensure_worktree`, readiness, update, abort, `_tidy`, `_remove_worktree`) it puts back the shadow's `config`, `HEAD`
  and `objects/info/alternates` and the worktree's `.git` file and its admin `gitdir` / `commondir`, and removes `hooks`,
  `info/attributes`, `info/grafts`, `config.worktree`, `commondir` and `shallow` (of the shadow and of the admin directory). The job's
  log names what it had changed. Nothing is written or removed through a symbolic link: one where dtree keeps a directory shuts the
  merge gate (`untrusted`) until a person removes it. dtree reads these files only when they are plain files (a FIFO left at the
  worktree's `.git` held the runner after a job and every readiness poll, review of round 3: it is put back like any other content).
  A `.git` *directory* in the worktree is a repository dtree did not make — someone's, never removed — and shuts the gate too. A
  directory made again where dtree's worktree was deleted by hand, with no `.git` in it, is no worktree of the shadow: dtree writes
  no `.git` into it (the next job is refused there, as before shadows), and what only the shadow holds is still seen and brought back
  (`not_brought_back`). A worktree of the project itself standing at dtree's place (a person made it
  there after deleting dtree's; the project's own admin directory names it back, which no job can write) is left as it is. The worktree's own files (`.gitattributes` included) only name drivers, which
  now come from the person's own git config alone. A shadow made before dtree kept a pin gets one made as `_shadow` makes it (in a
  scratch repository, not read from the shadow). After a Codex job, what its commands left running in its process group is ended
  with it (killed when Codex has exited but before it is reaped — while the group's id is still its own, never another's that took
  the id over), so nothing of that group writes the shadow again after dtree put it back; a process that left the group (`setsid`)
  is a limit (§6). Codex's result file is written in dtree's logs directory — outside what its `workspace-write` sandbox lets its
  commands write (the worktree, the shadow, /tmp, $TMPDIR) — and read only as a plain file, never from a FIFO or through a link. A `.git` the job itself removed from its worktree is put back by the runner right after the job (the pin and the shadow's
  admin entry still name that worktree) — only into the directory it handed the job (the same device and inode: a job cannot
  replace it, the directory above is not its to write). A directory a person made there while the job ran gets none.

## 5. Config

```json
"merge": {"method": "auto"}
```

| `method` | what happens |
|---|---|
| `auto` (default) | plumbing when the base is checked out nowhere, else `git merge` in the clean worktree where it is |
| `plumbing` | only when the base is checked out nowhere (refused otherwise: moving the branch would leave that checkout stale) |
| `worktree` | always a real `git merge` so the hooks run; in a temporary worktree under `worktrees` when the base is checked out nowhere |

`merge_trailer` (top level) is added to merge commits.

**ultimateTruth.** `main` is checked out in its root, so a merge there runs `git merge --no-ff` in the root. It runs only when the root
has no uncommitted tracked change and no operation in progress, and nothing git does not track stands where the merge writes: its
`data/`, `build/` and `res/` patterns are directory-only (a file or link there is untracked, so git refuses it), but `*.log` and `*.pyc`
match files, which git would overwrite or replace silently. The merge changes files in the root while its services (ports 5000,
5077–5079) may be running from it, as the old `dtree merge` did. Updating its trials happens in their own worktrees and never touches
the root.

## 6. Limits

- `git merge-tree --write-tree` needs git ≥ 2.38 (tested: 2.43).
- Untested by the probe:
  - SHA-256 repositories (64-hex ids are accepted);
  - submodule, symlink and mode-only conflicts;
  - `commit.gpgSign` with a working gpg (one that fails is tested: git stops the merge without a conflict, and dtree aborts it),
    `merge.log` and `merge.conflictStyle` (dtree passes `--no-log`);
  - non-local filesystems.
- The preview uses the `.gitattributes` merge drivers of the worktree where the merge would run. Neither dtree nor ultimateTruth
  tracks a `.gitattributes`.
- Between reading where the base is checked out and moving it, someone could check the base out. git has no lock for that.
- An update job's row has no runner until its merge is in progress, and `jobs.reap` ends such a row after 120 s. If git takes longer
  there (a huge checkout), the row no longer holds the trial. The update then aborts its merge, or, when another job took the trial
  meanwhile, leaves the merge in progress and says so.
- A clean update in a kept worktree has no job row to hold the trial (it takes about 0.1 s); a job sent in that moment can meet git
  there (an `index.lock`).
- dtree deletes a trial worktree's ignored files with it (`worktree remove --force` after a job that left only caches). A kept
  worktree is brought forward (`git merge --ff-only`) only when nothing git does not track is in the way; otherwise it is refused.
- `jobs.trust` puts the shadow back just before dtree's git runs there; a process of the job's that escaped its process group (a
  `setsid`) and still writes the shadow could race it. bwrap jobs (Claude Code, the chat loop) cannot leave one: bwrap ends them all.
- `branch_dirty` sees tracked files only, in dtree's worktree as in a person's checkout: a new file a job left there without `git add`
  is not seen by readiness (the job's log names it, and the worktree is kept).
- The gate follows the trial's last `updated` event. An update aborted with `dtree update TRIAL --abort` (event `aborted`) opens it,
  also when its job had committed plainly before it started the merge again (two of the prompt's rules broken); the abort's answer says
  where the branch is.
