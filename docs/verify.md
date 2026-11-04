# dtree verifies an agent's work itself

An agent ends its job with its own words: "tests 3 passed". That is a claim. dtree then runs the project's tests and measures
**itself**, in a sandbox, on two commits:

- the trial's **tip** (the branch's last commit), and
- its **baseline**: the commit the branch left the base from (`git merge-base <base> <tip>`).

It keeps the numbers in the `checks` table, writes a `verified` event on the question, and the compare table shows them beside what
the agent said, with the change against the baseline. Code: [`src/dtree/verify.py`](../src/dtree/verify.py),
[`src/dtree/parse_summary.py`](../src/dtree/parse_summary.py).

## When it runs

- After every build or update job that ends with commits, when `verify.enabled` and `verify.auto` are on (the default) **and the
  `verify` section names what to run: `verify.tests` or `verify.measures`**. The job's log shows the lines starting with 🔎, and the
  job's result keeps the summary under `verify`. The job's stop button stops it too.
- `dtree verify TRIAL` (in the terminal), `dtree verify TRIAL --background`, or `POST /api/decisions/trials/<id>/verify` (the page).
- After a clean update of the trial with the base (stream C, `merge.update_trial`, no job): in the background, by the same rule as after
  a job. `start_background(..., auto=True)` starts nothing unless `verify.auto_on`, and its run (`--auto`) leaves a bare
  `test_command` out. An update with conflicts is an update job: it is verified when it ends, like any job.

A bare `test_command` (no `verify.tests`) is **not** run after jobs: it is the command the agents are told about, often the whole
suite — ultimateTruth's takes over an hour, and verification runs it on the tip and again on the baseline, holding the job's runner
(and a `max_parallel_jobs` place) all that time. `dtree verify TRIAL` runs it by hand, and the job's log says so in one line
("· verify: not after jobs with only test_command …"). After a job with `verify.measures` and no `verify.tests`, only the measures
run. `verify.auto_on(project)` says whether jobs and clean updates are verified.

Nothing to run (no `verify.tests`, no `test_command`, no `verify.measures`): nothing happens, the summary says so, and `dtree
verify` exits 3 (nothing was run; see the exit codes below).

## What is run, and where

For each of the two commits:

1. A fresh **detached worktree** of the commit in `verify.workdir` (default `<worktrees>/.verify`). The person's git hooks do not
   run for it (`core.hooksPath=/dev/null`). It is removed afterwards, whatever happens (`git worktree remove --force`; when the
   tests left a directory read-only or unreadable, which git cannot empty, dtree gives the tree's real directories mode 0700 — never
   following a link the tests left, since this runs outside the sandbox — and removes the rest). A failure to clean up the worktree
   or pytest's JUnit output directory is told in the log ("⚠ verify: cleaning up …") and never loses the results; what could not be
   removed stays in `workdir` (on Python 3.9, whose `os.walk` recurses, a tree some 1000 levels deep that the tests leave raises
   RecursionError). Nothing is ever run in the agent's worktree or the main checkout.
   - **Never `git worktree prune`.** It is project-wide: it would drop any worktree of the project whose directory is away at that
     moment (a person's, on a disk not mounted just then), with its index and staged work. dtree touches only its own worktree's
     entry: when a directory deleted by hand (a reboot emptying `/dev/shm`) left an entry at the fresh path ("missing but already
     registered"), the worktree is added again with `-f`; when git cannot remove the tree (the tests rewrote its `.git`, or left a
     directory read-only), dtree removes the directory and then the entry, with `git worktree remove --force <its path>`. Entries
     left by a crash stay until git's own `gc` prunes them (`gc.worktreePruneExpire`, 3 months): they are a few small files each.
2. `sandbox.copies`: git-ignored paths of the main checkout copied in, only where the commit has nothing. The secrets
   (`sandbox.secrets`, `.dtree/write_token`) are never copied. A copy that would leave less than 256 MB free is refused: the checks
   end `error` with the sizes.
3. The sandbox's **mounts** are worked out once, from the fresh checkout, before anything runs. The tests write in the worktree, so
   working them out again for the measures would let the tests choose what the measures see (a `.git` file rewritten to name another
   repository would show that repository's git directory) or hide a `ro_binds` path (the mount point bwrap leaves behind looks like
   the commit's own file).
4. The **tests** command, then each **measure** (one run per distinct command: two measures with the same command share it).
5. The output goes to `<logs>/check-<id>.log`, after a header with the commit, the sandbox, what dtree added to the environment
   (`PYTEST_ADDOPTS`) and the exact command.

The tip is verified first, then the baseline, one after the other (a speed measure stays comparable).

## The sandbox

`verify.sandbox` is `bwrap` (the default) or `none`.

With `bwrap` each command runs through `sandbox.run` with `sandbox.project_mounts(project, worktree)` plus a writable output
directory for pytest's JUnit file, `network: none` and the project's `sandbox` settings (`root`, `nested`, `gpu`, `ro_binds`,
`tmpfs`, `secrets`, `extra_ro`, `env`):

- **no network**, not even the host's localhost (the dashboard, dtree's own server);
- **writable**: the verification worktree and the output directory only; the repository's git directory is read-only;
- `$HOME` is empty (allowlist root): `~/.ssh`, `~/.claude`, the other projects are not there; an interpreter named by absolute path
  in a command (`/path/to/venv/bin/python`) is shown read-only (`auto_extra_ro`);
- a timeout or a stop SIGINTs the command's process group (pytest still writes its counts), then kills the whole pid namespace.

When bubblewrap cannot run here (`sandbox.check()` fails: for example dtree started from an unconfined systemd unit on a kernel that
restricts user namespaces), **every check ends `error` with the reason**. dtree never falls back to running unsandboxed by itself:
`"sandbox": "none"` must be chosen in the config.

With `none` the commands run directly as the user, with the project's `sandbox.env` added: they can reach the network and write
anywhere the user can.

A project whose tests start a sandbox themselves needs `"sandbox": {"nested": true}` (no `--disable-userns`). dtree's own suite is
one: verified without it, its 9 bubblewrap tests skip (8 passed, 9 skipped); with it, all 17 of `test_sandbox.py` and
`test_store.py` pass (measured 2026-11-01).

## Which tests

`verify.tests` with `{tests}` runs only the test files the trial's changes select. Without `{tests}` the command runs as it is, and
`null` takes `test_command` as it is.

The changes are `git diff --name-only -z --no-renames <merge-base> <tip>` (a rename counts as both paths). The rules, in order:

| rule | selects | setting |
|---|---|---|
| (a) | a changed file that is a test file selects itself (added, changed or deleted) | `select.test_files` (default `tests/**/test_*.py`) |
| (b) | the test files named after each changed file: `{stem}` is its name without the extension (for `__init__.py` the package's name) | `select.pattern` (default `tests/test_{stem}.py` and `tests/test_{stem}_*.py`) |
| (c) | when a changed file matches a glob, that directory's own tests | `select.map` `[{"match": "src/pkg/mind/**", "tests": [...]}]` |
| (d) | the test files that import a changed module (Python's `ast`: `import`, `from … import`, relative imports, `importlib.import_module("…")`); the module name comes from the path: a leading `src/` is dropped, `/` becomes `.`, `.py` and `__init__` go. A helper module under a tests directory also goes by the names the tests import it by: pytest puts a test's own directory on `sys.path`, so `tests/helpers.py` is `helpers` (and `tests.helpers`), `tests/unit/h.py` is `unit.h` or `h` | `select.imports` (default on) |
| (e) | **test infrastructure**: a changed `conftest.py` selects the test files of its directory and below (pytest runs it for all of them; the top one's: every test); a changed helper module or data file under a tests directory (a directory named `tests`, `test` or `testing`) selects the test files of the nearest directory, from its own up to that tests directory, that has test files below it (`tests/fixtures/x.json` → everything under `tests/`) | `select.changed` |
| (f) | always | `select.always` |

- **Hubs.** A module imported by more than `select.hub_limit` (10) test files (`spec`, `runner` in ultimateTruth) would select
  nearly the whole suite: when the project has an `always` set, it is left to that set, and the summary lists it under `hubs`. So
  is a `conftest.py` or test helper (rule e) with more test files below it than `hub_limit`, listed once per directory as
  `{module: <the first changed file there>, importers: <test files below>, below: <the directory>, files: <changed files there>}`:
  ultimateTruth's `tests/conftest.py` covers 84 test files (over an hour), and its `always` set is what runs for it. A helper's
  own importers (rule d) are still selected when they are few. **With no `always` file there are no hubs** — an empty set, or
  patterns that match no file on either commit (a smoke test renamed, a base older than the file): the module's importers and the
  tests below the changed `conftest.py` run, however many they are, so a broken autouse fixture is seen, where before nothing ran
  (`none`). The summary lists at most 20 hubs (`hubs_more`: how many more).
- **Nothing selected.** `select.fallback`: `always` (only the always set; when that is empty too, nothing runs: status `none`,
  and `dtree verify` exits 3), `all` (the command with `{tests}` removed: the whole suite), `skip` (no tests check).
- **Each side runs what it has.** The selection is worked out once per trial, then each commit runs the selected files that exist
  there. A test file the trial adds is not given to the baseline's pytest (which would stop the whole run: "file or directory not
  found", exit 4); when none of the selected files exist on the baseline, its tests are "none" with zero counts, so the change shows
  as new tests. The other way round is not a pass: when the trial removed every selected test file the base has, its tests are
  **failed** ("the trial removed every selected test file") with zero counts, so the delta shows the tests that went, and
  `dtree verify` exits 1. This also flags a trial that removes a module together with its only selected tests on purpose: read
  the reason and the diff. An `always` set keeps a selection from being only the files a trial removed.
- File names are shell-quoted. Globs: `**/` any directories (also none), `*` and `?` within one name, `[...]`.
- `select.changed: false` turns rules (a)–(e) off: only `always`.

## Counts and states

- **pytest** (a command naming `pytest`, `py.test` or `-m pytest`, also inside quotes: `bash -o pipefail -c 'pytest …'`, `sh -c
  "pytest …"`): dtree gives pytest `--junitxml=<output dir>/junit.xml -p no:cacheprovider`
  through the environment, `PYTEST_ADDOPTS` (after the project's own, when `sandbox.env` sets one), and sums the leaf `<testsuite>`
  attributes: passed = tests − failures − errors − skipped; xfails (`skipped type="pytest.xfail"`) are counted apart from the skips.
  This works whatever `-q` the project uses (ultimateTruth's `-q` plus `addopts = -q` prints no counts at all). The command itself
  is never edited, so any shape counts the same: a pipe (`pytest … 2>&1 | tail -n 5`), a subshell, `cd sub && pytest`. A command
  that runs pytest more than once counts the last run that finished.
- **A pipe hides pytest's exit status**: a pipe ends with its last command's, so when pytest dies behind `| tail` (a segfault in
  native code, the OOM killer) the command still ends 0. pytest writes its JUnit file whenever it finishes, so a pytest command
  that ends 0 with no JUnit file and no printed counts is **`error`** ("pytest left no JUnit file and printed no counts: it did not
  finish"), never `passed`, and it runs again next time. The same holds for a pytest that exits 0 without finishing (`os._exit`
  in a test), pipe or not. dtree tells a finished run only by its JUnit file and its printed counts, so this is not caught when
  another pytest wrote them first: an earlier pytest run of the same command, or a pytest the tests start themselves (it inherits
  `PYTEST_ADDOPTS` and writes dtree's JUnit file — clear `PYTEST_ADDOPTS` for it, as pytester does). For such commands leave the
  pipe out, or use `bash -o pipefail -c '…'`.
- **Leave `--junitxml` and `PYTEST_ADDOPTS` out of the command.** pytest reads `PYTEST_ADDOPTS` before its command line, so a
  `--junitxml` (or `--junit-xml`) in the command replaces dtree's, and an inline `PYTEST_ADDOPTS=… pytest` replaces dtree's whole
  variable: dtree gets no JUnit file and counts only what pytest printed. With nothing printed (`-qq`, or ultimateTruth's `-q` on
  top of `addopts = -q`) the check is `error` and says why ("the command sets its own --junitxml or PYTEST_ADDOPTS, which hide
  dtree's"). pytest options meant for every run go in `sandbox.env`'s `PYTEST_ADDOPTS`: dtree appends its flags after them, so its
  `--junitxml` wins (one there of your own is replaced by dtree's).
- **anything else**, or pytest without a JUnit file: the printed summary, read by `parse_summary` from the end of the output (up to
  8 MB): pytest, unittest, jest, vitest, mocha, go, cargo, TAP. With several summaries the last wins. 74 recorded runs keep it honest
  (`tests/test_parse_summary.py`).
- Both the JUnit file and the output are written by the code under test, so every count is a whole number from 0 to 10⁹
  (`verify.MAX_COUNT`): `tests="1e400"`, `nan` or "10³⁰ passed" cannot overflow the store.

| status | means |
|---|---|
| `passed` | exit 0 and nothing failed (or, for a measure, the number was read); skips are not failures, so read the counts: a run that skipped everything is `passed` with 0 passed |
| `failed` | tests failed, or errored at collection (pytest exit 2 with the error counted: the trial's code does not import), or the command failed without a summary; the trial removed every selected test file its base has; a measure whose command failed (its value is kept when printed) |
| `error` | could not verify: the sandbox could not start (the first `bwrap: ` line is quoted), pytest exit 3/4/5 (internal error, usage, no tests), pytest exit 2 with nothing counted (interrupted before it counted), exit 126/127 (the command did not start), no tests ran, killed by SIGKILL from outside (exit 137 or −9: the OOM killer, a person — the run did not finish), a pytest command that ended 0 with no JUnit file and no printed counts (pytest did not finish: a crash or a kill behind a pipe), a measure's pattern did not match or its number is not finite (`inf`, `nan`, 400 digits), a check another runner has that did not move for the longest time limit + 120 s (that runner is stuck: "… has not moved …"; this trial's result only) |
| `timeout` | longer than `timeout_s`; partial counts are kept on the check, but not compared |
| `stopped` | the job was stopped |
| `none` | nothing to run on that side (nothing selected, or on the baseline: the trial added every selected file) |

`error`, `timeout` and `stopped` are never reused (see the cache): an OOM kill of a shared baseline is run again by the next
verification, not compared against for good.

## Measures

```json
{"name": "score", "command": "python tools/measure.py", "pattern": "\"score\": (?P<value>-?[0-9.]+)", "unit": "pt",
 "better": "higher", "timeout_s": 600, "tolerance_pct": 0}
```

- The number is group `value` of the pattern's **last** match (else its first group, else the whole match), read from the end of the
  output; the pattern is multi-line (`^` and `$` per line). No match, or `null` printed: the measure is `error`. The number must be
  finite: `inf`, `nan` or a number too big for a float (the tool is the trial's own) is `error` ("not a finite number"), since
  JSON has no Infinity and the page could not read the question. A delta too big for a float is left out the same way.
- A measure killed by SIGKILL is `error` with no value, like a timeout.
- Each measure is its own check (kind `measure`); identical commands run once and feed every pattern.
- The compare shows value, baseline, delta and delta % and, with `better`, a verdict: `better`, `worse` or `same` (within
  `tolerance_pct` of the baseline, for noisy numbers).
- A measure runs each commit's own copy of the tool: if the trial changes the tool, the two numbers may not be comparable.

## The cache

The result of a command on a commit does not change, as long as what it runs is installed the same. A check's key is a hash of:
the commit, the check's name and kind, the command after the selection, a measure's pattern, `verify.sandbox` and the sandbox
settings that change results (`root`, `nested`, `gpu`, `ro_binds`, `tmpfs`, `copies`, `env`, `extra_ro`, `path_tools`), the
**installation of the programs the command runs** (below), and the version of this code (`verify.VERSION`, so no result of an
older dtree is reused: 2 moved the pytest flags to `PYTEST_ADDOPTS` and made a SIGKILL an error; 3, also on 2026-11-02, made a
pytest run that counted nothing an error, so the false passes 2 kept for a pytest that crashed behind a pipe are not reused). The
per-run paths (worktree, output directory) are never in it.

- **The installation** (`verify._environment`, cheap: a few `stat`s, nothing run): each program the command names — an absolute
  path (`/path/to/venv/bin/python`) or a bare name found on the PATH the command gets (`sandbox.env`'s, else
  dtree's: `pytest`, `bash`, `tail`) —, the interpreter of a script's `#!` line (a bare `pytest` is a script of some Python), and
  for a Python its `site-packages` (`<prefix>/lib/pythonX.Y/site-packages` of the path as named, a venv's, and of the binary it
  resolves to; Debian's `dist-packages`; `~/.local/lib/pythonX.Y/site-packages`), each by its size and mtime. `pip install`,
  `pip install -U` and `uninstall` add and remove a package's files and its `dist-info` there, so the directory's mtime moves and
  the next verification runs again instead of comparing against a baseline of the old package (a false "+1 passed"). A module
  named after `-m`, an option, a relative path and an assignment are not programs. Each verification keeps this print in its
  `verified` event, so the compare row says `env_changed` (and `settings_changed`) when the installation changed since.

- A check that ended `passed` or `failed` is **reused**: verifying the same commit again costs nothing ("(cached)" in the log).
- A check is run once even when two runners ask for it at the same moment, also from two processes (two job runners, the page's
  background run): the store claims a key in one transaction (`check_claim`), and a runner that finds the key running in a live
  dtree process says so in its log ("⏳ … waiting for its result"), waits for it and takes its result ("(shared)"). A runner that
  died is taken over. A check that ended `stopped`, `error` or `timeout` in the runner a trial waited for is run again by that
  trial, unless it was stopped itself: the person stopped the other trial, not this one, and an error (an OOM kill, that runner's
  crash) or a timeout (a machine busy with the race) is never reused. Each waiting trial runs it at most once of its own, so an
  error or a timeout that comes back every time (a usage error on the base, a measure too slow for its limit) costs one run per
  trial that waited for it.
- **How long a trial waits.** A runner claims a side's checks at once and runs them in turn, so a check can stay `running` long
  before its turn: a baseline measure the race shares waits behind the claiming trial's own baseline tests (minutes, for tests
  that rebuild something). So a trial waits as long as that runner moves on: every check it starts or ends is a write to its rows,
  and each of its checks ends within the longest time limit of the settings (`timeout_s`, the measures' `timeout_s`). Only when
  none of that runner's checks on the commit has moved for that limit + 120 s is it stuck: the wait ends `error` ("… its runner
  (pid N) still runs but has not moved for … s"), this trial's result only, never the check's, and never a `timeout` the check
  did not have. Verify again when that runner has ended (or stop it).
- **What a race shares.** The trials of a race leave one base, so their baselines are one commit. On it, every measure runs once for
  all of them, and so does a tests command they all give the baseline. But with `{tests}` each trial's baseline runs **its own
  selection** — the selected files that exist on the base — because a trial's delta compares the same test files on its tip and on
  its base: two trials whose changes select the same files share one baseline run; one that changed `calc.py` and one that changed
  `other.py` run the base's tests once each. (Running the base once with the union of the race's selections would make every
  trial's delta count tests its own tip never ran, and the union changes whenever a trial is added.) To have the whole race share
  one baseline run of the tests, give them a fixed selection: `select.changed: false` with an `always` set, or a command without
  `{tests}`.
- `error`, `timeout` and `stopped` are not reused.
- `--force` (`{"force": true}` on the page) runs everything again.
- Not in the key: the content of copied or bound host paths (`copies`, `ro_binds`, `extra_ro`), a file edited in place inside
  `site-packages` (its directory's mtime stays), a package installed into an environment the command does not name (another
  Python's, one on `PYTHONPATH`), what a program it runs reaches by itself (a `#!/bin/sh` script that `exec`s a Python — conda's
  form for a long prefix — is stamped as itself and `/bin/sh`, not that Python's `site-packages`), and the time limits. **After changing a model or data
  file the tests read, or the environment in a way the installation print cannot see, verify with `--force`.** With `"sandbox":
  "none"` the first run after an installation can also write Python's own caches into `site-packages` (a top-level module that
  pip did not compile), which moves it once: that commit's checks run once more the next time, then come from the cache.

## Limits

- The numbers are objective about a commit, not about the tests: the trial's commit carries its own tests and its own measure
  tools. An agent that weakens a test or edits a measure's tool changes what is counted. The selection lists the test files the
  trial changed (reason `changed`), and `± diff` shows the rest: read them before trusting a better number.
- What runs on the host, outside the sandbox, is dtree's own work on the commit: the checkout (git), the copies, reading the
  JUnit file and the output, and the cleanup. A link the commit puts on the way of a copy stops that copy; a JUnit file that is a
  link or not a plain file is not read; the cleanup never follows a link nor changes the mode of anything but a directory inside
  the tree it removes; the sandbox's mounts are fixed before the code runs; a test file's source is only parsed (`ast`), never run.
- Inside the sandbox the worktree and the output directory are writable without a size limit: a run can fill `workdir`'s disk
  (or RAM, in `/dev/shm`) until its time limit.

## Reading the compare table

`jobs.compare` adds `verify` to each trial's row (from the latest `verified` event; cheap enough for the page's polling):

| field | what it is |
|---|---|
| `status` | `none` · `running` · `passed` · `failed` · `error` · `timeout` · `stopped` — the tip's tests (its measures when there are no tests). `none` is either never verified (`checked_at` null) or verified with nothing to run (`checked_at` set; `tests.summary` says why: "nothing selected (fallback always)") |
| `tests`, `base` | `{passed, failed, errors, skipped, xfailed, total, status, seconds, summary, check, cached, shared}` of the tip and of the baseline |
| `delta` | tip − baseline `{passed, failed, errors}`: +1 passed is a new passing test, +1 failed is a test the trial broke; only when both ran to the end |
| `measures` | `[{name, unit, better, value, base, delta, delta_pct, verdict, status, base_status, check, base_check}]` |
| `sha`, `base_sha`, `checked_at` | the commits verified, and when |
| `stale` | the branch moved since (a follow-up, an update): verify again |
| `settings_changed` | the tests command, selection, measures or sandbox settings changed since, or `env_changed`: the numbers may not match what would run now |
| `env_changed` | a program the commands run is installed otherwise now (a package upgraded in the interpreter's `site-packages`; see the cache); a verification made before dtree kept this print says nothing (false) |
| `running` | a verification of this trial runs now (a process with a `<logs>/verify-<trial>-<pid>.running` mark) |
| `selection` | `{files, fallback, hubs, hubs_more, changed}` when `{tests}` is used |

Read it against the agent's `tests` column: the agent's words, then what dtree saw.

## Commands and API

```bash
dtree verify TRIAL [--force] [--no-baseline] [--background]   # exit 0 passed · 1 not passed or refused · 3 nothing was run
dtree checks [--sha SHA] [--trial TRIAL]                       # the checks dtree ran (latest per commit and command)
python -m dtree.verify run --trial T --project ROOT [--force] [--no-baseline] [--auto]   # what --background, the page, an update start
```

**`dtree verify`'s exit code**, so a script can rely on it (`dtree verify T && dtree merge …` goes on only after a pass):

| code | when |
|---|---|
| 0 | the tip passed (`passed`) |
| 1 | the tip did not pass (`failed`, `error`, `timeout`, `stopped`), or the verification was refused: verification off, a setting that cannot work, the trial or its branch missing, the trial being verified already (`--force` or `--background`) |
| 2 | a usage error (argparse) |
| 3 | nothing was run (`none`: nothing selected with fallback `always` or `skip`, or nothing to verify) — not a pass |

`--background` exits 0 once the runner has started (its result comes later: `dtree checks --trial T`), and 1 when it is refused
before it starts, as the page's request is: verification off, a setting that cannot work, the trial or its branch missing, the trial
being verified already. The detached runner (`python -m dtree.verify run`) exits with the same codes as `dtree verify`.
`--background` keeps `--force` and `--no-baseline` (`start_background(..., baseline=False)`). `start_background(..., auto=True)`
(merge.py's call after a clean update) starts nothing unless `verify.auto_on(project)`, and passes `--auto`: the run verifies only
`verify.tests` and `verify.measures`, as after a job.

A trial is verified by one process at a time: while one runs (a `<logs>/verify-<trial>-<pid>.running` mark of a live process),
the page's request and `dtree verify --background` are refused, `force` or not (a forced run claims every check anew instead of
waiting, so both would run every check at the same moment); so is `dtree verify --force` in the foreground. Without `--force`
the foreground waits for the checks the other run has and takes their results.

| method | path | what |
|---|---|---|
| GET | `/api/decisions/trials/<tid>/verify` | `{verify: <the compare fields>, checks: [rows with ids]}` |
| POST | `/api/decisions/trials/<tid>/verify` `{force?}` | `{started: pid}`; refused (400) while the trial's job runs, while it is being verified (`force` or not), when verification is off, and when the trial's branch does not exist |
| GET | `/api/checks/<cid>/log?lines=N` | `{check, lines}`: the end of the check's log |

## Config

| key | default | what |
|---|---|---|
| `enabled` | `true` | false: no verification at all |
| `auto` | `true` | after every build or update job that ends with commits, and after a clean update, when `tests` or `measures` is set (a bare `test_command` only by hand: `dtree verify`) |
| `tests` | `null` | the test command; `{tests}` → the selected files; null → `test_command` as it is, for `dtree verify` only (after a job no tests run); no `--junitxml` or `PYTEST_ADDOPTS` in it (`sandbox.env`, see Counts) |
| `baseline` | `true` | also the merge-base with the base |
| `timeout_s` | `1800` | per check |
| `workdir` | `null` | where verification worktrees go: `<worktrees>/.verify`; `/dev/shm/...` keeps them in RAM |
| `sandbox` | `bwrap` | or `none` |
| `select.always` | `[]` | test files (or globs) always run |
| `select.changed` | `true` | use the trial's changes (rules a–d) |
| `select.pattern` | `["tests/test_{stem}.py", "tests/test_{stem}_*.py"]` | text or a list |
| `select.map` | `[]` | `[{"match": glob or [globs], "tests": [files or globs]}]` |
| `select.imports` | `true` | the import rule |
| `select.hub_limit` | `10` | more importers (or test files below a conftest.py or helper) than this: a hub, left to `always` when it has a file |
| `select.test_files` | `["tests/**/test_*.py"]` | what is a test file |
| `select.fallback` | `always` | `always` · `all` · `skip` |
| `measures` | `[]` | `[{name, command, pattern, unit, better, timeout_s, tolerance_pct}]` |

The `sandbox` section (`root`, `nested`, `gpu`, `ro_binds`, `tmpfs`, `secrets`, `extra_ro`, `copies`, `env`) is shared with the
agents' commands; see `src/dtree/sandbox.py`.

## ultimateTruth

Measured in a sandbox on this machine (2026-11-01, `probe-results/ut-tests.json`): ultimateTruth's tests need nothing git-ignored,
pass in a fresh detached worktree with only the worktree writable, and write only `data/write_token` (a fresh token) into it. Its
own `test_command` cannot be used as it is: it runs the whole suite (1034 tests, over an hour) and its `-q` on top of
`addopts = -q` prints no counts. The name rule alone finds a test for 38% of the source files changed in its history; names plus
imports (hubs left out) plus directory maps, 89%, with a median of 9 test files per commit. Until its config has a `verify`
section, jobs and clean updates are not verified (its `test_command` alone is only for `dtree verify` by hand).

Suggested settings:

```json
"verify": {
  "tests": "PYTHONPATH=src /path/to/venv/bin/python -m pytest -p no:cacheprovider --durations=10 {tests}",
  "workdir": "/dev/shm/dtree-verify",
  "timeout_s": 1800,
  "select": {
    "always": ["tests/test_spec.py", "tests/test_protocol.py", "tests/test_mind.py", "tests/test_present.py", "tests/test_world.py",
               "tests/test_kamma.py", "tests/test_orin_api.py", "tests/test_thought.py", "tests/test_dashboard.py", "tests/test_decisions.py",
               "tests/test_aiwork.py", "tests/test_scenarios.py"],
    "map": [{"match": "src/ultimatetruth/mind/**", "tests": ["tests/test_mind.py", "tests/test_present.py", "tests/test_stage1b.py", "tests/test_stage1c.py"]},
            {"match": "src/ultimatetruth/world/**", "tests": ["tests/test_world.py"]},
            {"match": ["src/ultimatetruth/world/vhum_world.py", "src/ultimatetruth/world/human_world.py"], "tests": ["tests/test_vhum_world.py"]},
            {"match": "src/ultimatetruth/dashboard/**", "tests": ["tests/test_dashboard.py"]},
            {"match": "docs/**", "tests": ["tests/test_docs.py"]},
            {"match": "spec/**", "tests": ["tests/test_spec.py"]}]
  },
  "measures": [
    {"name": "thorns_battery", "command": "PYTHONPATH=src /path/to/venv/bin/python tools/measure_thorns.py gate3 on --seconds 60",
     "pattern": "\"battery_change\": (-?[0-9]+(?:\\.[0-9]+)?)", "unit": "battery (0-1)", "better": "higher", "timeout_s": 600},
    {"name": "sim_speed", "command": "PYTHONPATH=src /path/to/venv/bin/python tools/measure_default_life.py 0 1 --world single --battery 0.3",
     "pattern": "\"realtime_x\": ([0-9]+(?:\\.[0-9]+)?)", "unit": "x real time", "better": "higher", "timeout_s": 600, "tolerance_pct": 10}
  ]
},
"sandbox": {"tmpfs": ["data", "build"]}
```

- **Measured through dtree** (2026-11-01, `verify_trial` on a throwaway clone in `/dev/shm`, these settings, the tip only): a docs
  change selected 13 files (the always set, and `test_docs` through the map): **318 passed in 149 s**; `thorns_battery` 0.138 in
  168 s; `sim_speed` 0.85 in 72 s: **389 s for one commit**. With the baseline it costs the same again once per base commit, then
  comes from the cache for every trial from that base. With only `tests/test_spec.py` and `tests/test_protocol.py`: the tip 18
  passed (one test added) against the baseline's 17, in 1.4 s for both; verifying again took 0.0 s (both cached).
- The always set: 315 passed in 150.5 s as one sandboxed run; without `test_scenarios` about 64 s (single-file timings, not one run).
  Keep `test_scenarios` while `runner.py` (a hub, changed in 24 of 65 commits) is reached only through it.
- `thorns_battery` takes about 170 s and is deterministic (0.138); `sim_speed` about 75 s and noisy (0.85 vs 0.80), hence
  `tolerance_pct` 10. The two measures cost about 4 minutes per commit; with the baseline cached, a race pays them once per trial.
- `workdir` in `/dev/shm`: the root disk is nearly full and a checkout is about 120 MB. `tmpfs` gives `data/` and `build/` an empty
  RAM directory in the worktree, so baseline and trial start alike. Never bind the live `data/` read-only: the tests write
  `data/write_token` and would fail with EROFS, and the real token would be inside the sandbox.
- The RTL tests (`rtl/**`, `tools/gen_rtl*.py`) rebuild Verilator models from nothing (15 minutes and more): add a map for them only
  when you accept that time.
- The 12 `test_habitat` tests need the GPU (`"sandbox": {"gpu": true}`), shared with ComfyUI and F5-TTS: off by default.
