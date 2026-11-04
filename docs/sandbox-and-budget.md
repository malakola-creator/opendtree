# Sandbox and budget

What an agent's job can see, write and reach, why it is set up that way, how to widen it for a project, what agents may
spend, how the chat loop holds a job, and how many requests a chat agent may send (§10). The code is in
`src/dtree/agents/claude.py`, `src/dtree/agents/chat.py`, `src/dtree/egress.py`, `src/dtree/sandbox.py`, `src/dtree/spend.py` and,
for the requests counted, `src/dtree/store.py`.

## 1. What a Claude Code job sees, writes and reaches

A Claude Code job runs inside bubblewrap by default (`agents.<name>.sandbox: "bwrap"`). If bwrap cannot make its namespaces on
this machine, the agent shows as not available, with the reason. It never falls back to running without a sandbox on its own.

| | inside the sandbox |
|---|---|
| filesystem root | the project's `sandbox.root`: `allowlist` (default) shows /usr, /etc, /opt, /sys and a private /dev, /proc; `host` shows the whole filesystem read-only, with the known secret places under $HOME hidden |
| $HOME | an empty private tmpfs (allowlist root): other projects, `~/.ssh`, `~/.codex` and the person's other files are not there |
| `~/.claude` | a private tmpfs. Claude Code writes its session state there. The person's settings, hooks, CLAUDE.md, skills, agents and plugins are absent |
| `~/.claude/.credentials.json` | bound read-only into that tmpfs (`credentials: "rw"` binds it writable) |
| the account's MCP connectors | dropped (`--strict-mcp-config`) |
| Claude Code's own tools that act outside the job | disallowed by default (`disallowed_tools`): `WebSearch`, `WebFetch`, `RemoteTrigger`, `CronCreate`, `CronDelete`, `CronList`, `Workflow`, `SendMessage`, `ListAgents`, and `EnterWorktree` / `ExitWorktree` (§4) |
| the worktree | writable. The empty directories bwrap makes there as mount points for `sandbox.ro_binds` and `sandbox.tmpfs` are removed after the run: `project_mounts` binds only where the worktree has nothing, so one left behind would hide the shared data from the next job in a kept worktree |
| the trial's own git repository | writable, so the agent can commit. Since 2026-11-02 a worktree belongs to the trial's shadow repository `<worktrees>/.shadow/<branch>.git` (bare, borrowing the project's objects read-only); dtree brings the branch back into the project after the job, fast-forward only, every object checked (`jobs._sync_back`). Its `config` and `hooks` are read-only, and so are the worktree's `.git` file and its admin `commondir` / `gitdir` (what tells git on the host where to look) |
| the project's `.git` | read-only: HEAD, index, every branch, `config`, `hooks`, `info`. A job cannot move the base branch, switch the person's checkout or leave anything git on the host would run. (A worktree made in the project itself before the shadows is still used as it is; its job gets the project's git directory writable, with the same files guarded, as before.) |
| the main checkout | not shown under the allow-list root (only its `.git`, read-only); under the `host` root, read-only |
| the binary's own install | read-only (the VS Code extension directory, the npm package, or the file) |
| /tmp, /run, /var/tmp | private tmpfs, thrown away at the end |
| environment | cleared; then PATH, HOME, USER, LANG…, cache directories sent to /tmp, the project's `sandbox.env`, the proxy variables, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`, `DISABLE_AUTOUPDATER=1`, and the login variables of `pass_env` when the runner has them (below) |
| processes | its own pid namespace: it sees only its own processes and cannot signal the person's |
| network (`network: "proxy"`, default) | its own network namespace with only loopback; out through dtree's proxy to the allowed hosts only (§2) |

The agent is told this in a short note at the end of its prompt (what it can write, what it can see under the project's root, the
network), in step with what `jobs.prompt_for` says, so it does not spend turns finding out.

**Login variables.** `pass_env` (default `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`) are Claude Code's other ways to log in,
passed only when the runner has them. Their values never go on a command line: `/proc/<pid>/cmdline` shows bwrap's and the
shell's arguments to every local user for the whole job. They are written as `export NAME='value'` lines to a 0600 file in a
0700 directory of the job's own, bound read-only into the sandbox, read by the sandbox's shell just before it becomes Claude Code,
and deleted as soon as Claude Code answers (and in any case when the job ends). The job's log names the variables, never their
values. The usual login, `~/.claude/.credentials.json`, needs none of this.

**A stop or the deadline.** The job records bwrap's pid; bwrap leads its own process group. A stop from the page or `dtree stop`
asks the runner (`stop_requested`); the runner's watcher, on that or on the deadline, sends SIGINT to the command's process group
and, after `GRACE_S` (10 s), SIGKILL to bwrap's group. A second stop ends bwrap's group at once. The pid namespace takes every
child with it. On SIGINT Claude Code still writes a result, but after a stop it may be one of zeros (§8); the job then counts the
tokens of the API calls it saw in the stream, as it does when no result comes at all.

**How a job ends badly, and what the reason says.**

| what happened | the job's reason |
|---|---|
| the sandbox could not be set up | `the sandbox did not start: bwrap: …` (bwrap's own line); the job cost nothing ($0, 0 tokens: Claude Code never ran) |
| the dollar cap was reached | `budget: $x of $cap` |
| the login is gone or expired | `the Claude login needs a refresh (Claude Code said: …): run claude on this machine and log in, then send the job again; …` |
| the binary (or its interpreter) is not there inside the sandbox | `Claude Code ended with no result (exit 127): dtree-sandbox: 1: exec: …/claude: not found` |
| Claude Code ended without a result | `Claude Code ended with no result (exit N): <its last stderr line>` |

## 2. The one way out: dtree's egress proxy

```
claude (in the sandbox) ──HTTPS_PROXY──▶ 127.0.0.1:3128 forwarder (in the sandbox, /usr/bin/python3 -I)
        ──unix socket (its directory bound read-only into the sandbox)──▶ proxy thread in the job's runner
        ──CONNECT, allowed hosts only──▶ api.anthropic.com:443, …
```

- The proxy runs in the job's runner process, outside the sandbox, and listens on a unix socket in a per-job directory under /tmp.
  The directory is removed when the job ends.
- It tunnels `CONNECT host:port` only to the allow-list and answers 403 to everything else. A plain-http request through it is
  refused: only https (CONNECT) goes out.
- A name that resolves to a loopback, link-local or unspecified address is refused, unless the allow-list names that address or
  `localhost` itself.
- Every host asked for is counted (`allowed`, `denied`, `failed`) and logged once per job in the job's log
  (`🌐 egress allowed: …`, `⛔ egress denied: …`). The counts are the agent's `egress` output.
- The allow-list is Claude Code's own hosts (`egress.CLAUDE_HOSTS`: `api.anthropic.com` for the API and `platform.claude.com`
  for a token refresh) plus the agent's `network_allow`.
- The proxy variables are inherited by the commands Claude Code runs, so they reach the same hosts and nothing else.
  `NO_PROXY` keeps the sandbox's own loopback direct (a test server a project's tests start inside the sandbox works).
- The forwarder is started by a small `/bin/sh` wrapper whose `$0` is `dtree-sandbox`, so the shell's own errors (a binary it
  cannot run) name that and the binary, not the forwarder.

`network: "host"` shares the machine's network instead: everything is reachable, including the services on the machine's own
localhost. The dtree server and ultimateTruth's dashboard take writes from localhost without a token, so use it only when a job
must reach them. `network: "none"` gives no network at all: Claude Code cannot reach its API then, so this is for tests and
stand-ins.

## 3. Why (the probes of 2026-11-01)

Measured on this machine with Claude Code 2.1.314 and bubblewrap 0.9.0:

- **~/.claude.** Claude Code must be able to write under `~/.claude`; read-only, it stops with `EROFS`. One private tmpfs over it
  is enough for all its state. A read-only view of the real `~/.claude` still ran the person's hooks, so the tmpfs is also the
  safer choice. `~/.cache`, `~/.config` and `~/.local/state` need not be writable.
- **Credentials.** Without the credentials file in that tmpfs, Claude Code is not logged in. It answers at once, without spending
  anything: `Not logged in · Please run /login`. A valid token works read-only. Whether a token refresh needs the file writable was
  not tested, because an expiry cannot be forced safely; that is why `credentials: "rw"` exists for long jobs.
- **Connectors.** The account's claude.ai connectors load from `~/.claude.json` as MCP servers; `--strict-mcp-config` drops them.
- **The parent session.** A Claude Code session that starts dtree exports `CLAUDECODE`, `CLAUDE_PID` and `CLAUDE_CODE_*`. The
  cleared environment removes them. Inside a job, Claude Code sets its own for the commands it runs: its own session id, pid and
  socket in the sandbox's /tmp.
- **The cap.** `--max-budget-usd` is checked between turns: one turn can go past it. A run capped at $0.0001 spent $0.0021. The
  result then has the subtype `error_max_budget_usd` and Claude Code exits 1.
- **The network.** No user-mode network stack (slirp4netns, pasta) is installed here, and sharing the host's network opens
  every localhost service. Hence the proxy.
- **The processes.** A pid namespace is the only way to make a stop or a timeout take every child with it. bwrap's `--die-with-parent` is
  bound to the thread that starts it, so the job starts and waits for bwrap in the runner's main thread.
- **Startup.** bwrap works here because the callers (VS Code's snap context) are allowed to create user namespaces. From an
  unconfined context, such as a systemd unit, on this kernel's AppArmor settings, it fails at once; `sandbox.check()` reports that.

## 4. Configuration

The agent's keys, in `.dtree/config.json` under `"agents": {"claude": {…}}`:

| key | default | what it is |
|---|---|---|
| `sandbox` | `"bwrap"` | `"none"` runs Claude Code without a sandbox, as before (the person's own `~/.claude`, the whole network) |
| `network` | `"proxy"` | `"proxy"`, `"host"` or `"none"` (§2) |
| `network_allow` | `[]` | more hosts for the proxy, added to Claude Code's own: `"pypi.org"`, `".example.org"` (the domain and every name under it), `"host:8443"` (a port; 443 by default), `"192.0.2.5:8080"`, `"[::1]:8080"` |
| `credentials` | `"ro"` | `"rw"`: the real credentials file writable (a long job may refresh its token) |
| `pass_env` | `["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]` | variables passed from the runner into the sandbox when they are set, through a private file (§1) |
| `rw_paths` | `[]` | more paths the job may write (absolute, `~` allowed; only those that exist) |
| `disallowed_tools` | below | the tools Claude Code may not use in a job |
| `max_budget_usd` | `budget.job_usd` | the job's dollar cap (§6) |
| `month_usd`, `month_tokens` | none | this agent's own caps for a calendar month (§6; every kind of agent has them) |
| `total_usd`, `total_tokens` | none | this agent's own caps over every month: a credit (§6; every kind of agent has them) |

**`disallowed_tools`.** By default: the git commands that change branches, merge or push (`Bash(git push:*)`, `Bash(git merge:*)`,
`Bash(git rebase:*)`, `Bash(git switch:*)`, `Bash(git checkout:*)`, `Bash(git worktree:*)`, `Bash(git branch -D:*)`,
`Bash(git branch -d:*)`, `Bash(git reset --hard:*)`), `Bash(sudo:*)`, `Bash(ssh:*)`, `Bash(scp:*)`, `Bash(rsync:*)`, and Claude
Code's own tools that act outside the job through the account or the web: `WebSearch` (reads the web through Anthropic's servers),
`WebFetch`, `RemoteTrigger` (cloud routines on the account), `CronCreate`, `CronDelete`, `CronList`, `Workflow`, `SendMessage`
(messages to the account's other sessions) and `ListAgents` (lists those sessions: a job has no need to see them). They all go
through `api.anthropic.com`, which the proxy has to let through for the
API itself, so this list is what keeps them out of a job: a routine or a message made there would live outside dtree's sandbox and
budget. `EnterWorktree` / `ExitWorktree` go too: like `git worktree` they would move the work off the trial's branch. The names
are those of a real session's init event (Claude Code 2.1.314, 2026-11-02); a job's `job-N.jsonl` keeps its session's init event,
whose `tools` list shows what that session had. A project that sets its own `disallowed_tools` replaces the whole list, so copy the default and add to it.

What every sandboxed command of the project sees (Claude Code jobs, the chat loop's `run` tool, verification) is under
`"sandbox"`, documented in `sandbox.py`: `root`, `nested`, `gpu`, `ro_binds`, `tmpfs`, `secrets`, `extra_ro`, `copies`, `env`.

## 5. Widening it for a project

- **Installing packages.** Add the index hosts: `"network_allow": ["pypi.org", "files.pythonhosted.org"]`. For npm,
  `"registry.npmjs.org"`; for GitHub, `"github.com"` and `".githubusercontent.com"`.
- **An interpreter or a toolchain under $HOME** (a conda env, `~/.local`): `"sandbox": {"extra_ro": ["~/miniforge3"]}`. A
  directory the project's commands name by its absolute path is added by itself (`sandbox.auto_extra_ro`).
- **Data the tests read from the main checkout** (git-ignored files): `"sandbox": {"ro_binds": ["data/models"]}` shows them
  read-only at the same place in the worktree. The main checkout itself is not shown under the allow-list root.
- **Directories the tests write and nobody keeps:** `"sandbox": {"tmpfs": ["build", "data"]}`.
- **A model server on the local network:** add its address and port: `"network_allow": ["192.168.1.20:11434"]`.
- **The GPU:** `"sandbox": {"gpu": true}`.
- **Tests that start their own sandboxes or user namespaces** (dtree's own tests do): `"sandbox": {"nested": true}`.
- **A job that must reach the machine's own services:** `"network": "host"`. Prefer a single `network_allow` entry for the one
  service it needs, for example `"127.0.0.1:11434"` for a local model server.
- **A `command` that is a wrapper script** must use an interpreter the sandbox shows (`/usr/bin/…`), or the interpreter's install
  goes into `sandbox.extra_ro`.

## 6. The budget (spend.py)

**Per job.** Two caps, from the agent's own setting or else the project's `budget`:

| cap | agent key | project default | who applies it |
|---|---|---|---|
| dollars | `max_budget_usd` | `budget.job_usd` | Claude Code (`--max-budget-usd`, checked between turns) |
| tokens, in + out | `max_tokens_total` | `budget.job_tokens` | the chat loop: it stops before asking the model again. Codex: its process group is stopped when a turn's usage reaches the cap |

A job that reaches its cap fails with `budget: $x of $cap` or `budget: N tokens of cap M`. `null` means no cap. A cap or price
that is not a number is refused before the job starts, rather than ignored while it runs. `codex exec` reports usage only at the
end of a turn (one turn per job), so a Codex job usually meets its cap when its work is already done.

**What is left caps the job too.** When a job starts, each of its caps is lowered to what is left then of `budget.month_usd` and
of the agent's own `month_usd`, `month_tokens`, `total_usd` and `total_tokens` (`spend.limits`; the review of 2026-11-02 found a
job sent with 5 of a month's 25 tokens left running to 120). Claude Code gets the dollars left as `--max-budget-usd`; its token
caps are checked before a job only, since Claude Code has no token cap. The chat loop and Codex get tokens: the dollars left are
turned into tokens at the higher of the agent's two prices, so the cap is reached no later than the dollars would be; an agent
without prices is not held by dollar caps at all (its dollars are unknown). The reason then says where the cap came from:
`budget: 10 tokens of cap 5 (what was left of agents.muse.month_tokens)`. When nothing is left (two jobs of a race passed the
check together and the first took the month past its cap), the cap is 0 and the agent does not start: `budget: nothing was left
of budget.month_usd; Claude Code was not started`. Jobs that start together each get the same remainder, so a race can still end
above a cap by what its other jobs spend. A chat agent with prices writes its cost so far to its job after every answer, so a job
started later counts it.

**Cost.** Claude Code reports what a job cost (`total_cost_usd`). For an agent that reports only tokens (chat, Codex), the cost is
estimated from `price_in_per_mtok` / `price_out_per_mtok` (dollars per million tokens). The result is then marked
`cost_estimated`. One price alone is used for both directions. Tokens known only as a total are priced at the higher of the two
prices: a count without the in/out split, and what an API's total counts beyond in + out (xAI's reasoning tokens are only in
`total_tokens`, so a Grok answer of 1,000 in, 100 out and 5,100 in all is priced 1,000 × in + 4,100 × out). Without prices, a job
has tokens and no dollars. The local model's prices are 0: it costs nothing.

Tokens: Claude Code's from the result's `modelUsage` (every model a session used; `usage` is all zeros when the budget ends a
session), input counting the cached context; the chat loop's from each answer's `prompt_tokens` / `completion_tokens` (and
`total_tokens`); Codex's from `turn.completed`. A Claude Code job with no result, or with a result of zeros (a stop, §8), counts
the tokens of the API calls it showed in its stream: each call once (Claude Code prints one event per content block, the same call
repeated), its input in full and its output only as far as the printed blocks went, so the output is a lower bound. Its dollars
are then unknown, not $0: estimated from the agent's prices when it has them, else the job counts as tokens without a price. A
Codex job that ended before any turn completed (stopped, timed out, its turn failed) reported no usage at all: its tokens are
unknown (`null`), not 0, and so are its dollars, so it is never counted as free.

**Per month, every agent together.** `budget.month_usd`: a job is not started once this calendar month's spend (local time,
reported + estimated) has reached it. An agent whose two prices are 0 is never refused by it.

**Per month, one agent.** In the agent's own settings:

| key | counts | notes |
|---|---|---|
| `month_usd` | this agent's dollars in the month (reported + estimated) | an agent that reports tokens only (chat, Codex) has dollars only through `price_in_per_mtok` / `price_out_per_mtok`: without them a `month_usd` would never be reached, so a job is refused with that reason instead |
| `month_tokens` | this agent's tokens in the month (in + out as the agent reports them; Claude Code's input counts its cached context) | needs no price: for an API that reports tokens only, such as Muse's |

`0` means no job of that agent this month; `null` (or nothing) means no cap. A month cap starts again on the 1st.

**In all, one agent: a credit.** The user's need of 2026-11-02 was Muse kept within its $150 hackathon credit, which is a total,
not a month: a month cap would let a credit spent across a month's end be spent once per month. `total_usd` and `total_tokens`
count every job of the agent in this project's store, whatever its month, and are checked like the month caps:

| key | counts | notes |
|---|---|---|
| `total_usd` | this agent's dollars over every job of it (reported + estimated) | needs the agent's prices when it reports tokens only (chat, Codex), as `month_usd` does |
| `total_tokens` | this agent's tokens over every job of it | needs no price |

For Muse within its credit, give it its prices and cap the dollars:

```
"agents": {"muse": {"total_usd": 150, "price_in_per_mtok": IN, "price_out_per_mtok": OUT}}
```

where `IN` and `OUT` are the Meta Model API's own prices in dollars per million tokens (dtree does not know them; do not put 0
there: both prices 0 make an agent free, and its jobs then count $0). Without its prices, cap its tokens instead:
`"agents": {"muse": {"total_tokens": N}}`, with N worked out from the credit and the prices (approximate, since input and output
are priced differently). A total counts this project's store only: an account used by several projects needs each project's
share, and jobs from before the credit count too (raise the cap by what they spent).

Every one of these caps is checked before a job, and what is left of it caps the job (above). A refusal says what was spent
against which cap, and the question gets a `budget_refused` event naming the setting: `{agent, setting: "budget.month_usd" |
"agents.<name>.month_usd" | "agents.<name>.total_usd", spent_usd, cap_usd, period}` or `{agent, setting:
"agents.<name>.month_tokens" | "agents.<name>.total_tokens", spent_tokens, cap_tokens, period}`; `period` is the month
(`YYYY-MM`), or `total` for the totals. The refusal is a `spend.BudgetRefused`, a DecisionError whose `code` is `budget` and whose
`setting` names the cap, so an API can tell a budget refusal from any other message.

**Settings that refuse every job.** `spend.settings_problem(project, agent_cfg)` says what in an agent's settings would refuse
every job sent to it (a cap or price that is not a number, a `month_usd` / `total_usd` that cannot be counted without prices, a
cap of 0, `budget.month_usd` 0 for an agent that costs something, a rate limit of 0 or one that is not a whole number, a rate limit
on Claude Code or Codex: §10), or None; the agents list uses it to show such an agent as not available with that reason, instead of
offering it and refusing each job.

**The report.** `GET /api/spend?month=YYYY-MM` and `dtree spend [--month YYYY-MM] [--json]`:

```json
{"month": "2026-11", "cap": 50.0, "remaining": 48.75,
 "agents": {"claude": {"jobs": 3, "measured": 2, "failed": 1, "stopped": 0, "running": 0, "usd": 1.25, "usd_estimated": 0.0,
                       "tokens_in": 470000, "tokens_out": 6100, "tokens": 476100, "seconds": 312.4, "no_price": 0,
                       "usd_known_jobs": 3, "usd_unknown_jobs": 0, "tokens_unknown_jobs": 0,
                       "cap_usd": null, "remaining_usd": null, "cap_tokens": null, "remaining_tokens": null,
                       "spent_total_usd": 1.25, "spent_total_tokens": 476100, "spent_total_unknown_jobs": 0,
                       "spent_total_tokens_unknown_jobs": 0,
                       "cap_total_usd": null, "remaining_total_usd": null, "cap_total_tokens": null, "remaining_total_tokens": null},
            "muse": {"jobs": 2, "usd": 0.4, "tokens": 180000, "…": "…", "cap_total_usd": 150.0, "spent_total_usd": 12.9,
                     "remaining_total_usd": 137.1},
            "gemini": {"jobs": 0, "…": "…", "requests_per_minute": 5, "requests_per_day": 20, "requests_minute": 2,
                       "requests_day": 12, "requests_wait_s": null}},
 "totals": {"jobs": 5, "usd": 1.65, "usd_unknown_jobs": 0, "…": "…"}}
```

`usd` is reported + estimated; `usd_estimated` is the part estimated from tokens; `no_price` counts jobs with tokens and no price;
`usd_known_jobs` the jobs whose dollars are known; `usd_unknown_jobs` the jobs that ended with dollars nobody knows although their
agent ran (no price, or no usage reported; a job that never reached its agent cost nothing, and so does a free agent's), or knows
only in part (a chat request given up: `usage_partial`, §9); `tokens_unknown_jobs` the jobs whose tokens are not known, or only in
part (Codex with no turn completed, a request given up) — the token caps count them as what is known, so what is left of a token cap
is then "at most". An agent's row has its month caps (`cap_usd` / `remaining_usd`, `cap_tokens` / `remaining_tokens`) and, over every
month, `spent_total_usd`, `spent_total_tokens`, `spent_total_unknown_jobs`, `spent_total_tokens_unknown_jobs` with `cap_total_usd` /
`remaining_total_usd` and `cap_total_tokens` / `remaining_total_tokens` (whatever month the report is for, the totals count every job up to now). `remaining` (of
`budget.month_usd`) and the agents' `remaining_*` go below 0 when a cap went over. An agent's row also has its rate limits
(`requests_per_minute`, `requests_per_day`: null without one) and its requests now, whatever the month (§10): `requests_minute`,
`requests_day` and `requests_wait_s` (until the next request may go when a limit is reached, else null; all null for Claude Code
and Codex, whose requests dtree does not see). An agent with a cap or a rate limit is listed even before its first job of the month.
Each agent in `GET /api/agents` also shows its `sandbox`, `network`, `max_budget_usd`, `max_tokens_total` (the job's own caps),
`prices`, `month_usd`, `month_tokens`, `total_usd` and `total_tokens` (and a chat agent its `request_timeout_s` and the five rate
fields above).

`dtree spend` prints the month's line, a table with a `running` column, then a line per capped agent (a throwaway store's,
2026-11-02: Claude's second job was stopped with no dollars reported and no prices; the Orin has no price; Muse's $12.50 job was
in October):

```
spend 2026-11 · $1.26 + ? of $5.00 (budget.month_usd) · remaining at most $3.74 · $0.0058 of it estimated from tokens · 2 jobs of unknown cost
agent         jobs measured failed stopped running          usd    tokens in   tokens out      time
claude           2        1      0       1       0    $1.25 + ?      418,677          920     2m00s
muse             1        1      0       0       0     ~$0.0058        5,000          400     0m17s
orin             2        0      1       0       1            -            -            -    10m01s
total            5        2      1       1       1    $1.26 + ?      423,677        1,320    12m18s
each agent's own month:
  muse: 5,400 of 1,000,000 tokens (agents.muse.month_tokens), 994,600 left
each agent's total (every month):
  muse: $12.51 of $150.00 in all (agents.muse.total_usd), $137.49 left
```

**Per question.** `spend.usage` counts each question's jobs by the same rule (`_add`), whatever the month: the tree's nodes carry it
(`usage`, and `usage_branch` with the questions that grew from it), `GET /api/decisions/<id>` gives it by agent and trial,
`dtree usage [ID]` prints it, and the page shows it as 🪙 (the fields: [schema.md](schema.md) §5).

The dollars show `-` when none of a row's jobs has known dollars (an agent without a price, or only a running job), `+ ?` after
the dollars known when some jobs' are unknown (and `remaining at most`, `at most … left`), `~` in front when all of it is
estimated, 4 decimals below a cent, and `<$0.0001` for less than that (not `$0.0000`, which reads as free). A capped agent's
tokens show `+ ?` and `at most … left` the same way when some of its jobs' tokens are unknown.

## 7. Measured for real (2026-11-01, Claude Code 2.1.314, haiku)

Throwaway repositories in /dev/shm, each job sent through dtree's own runner:

| run | result | cost |
|---|---|---|
| a job asked to try what the sandbox should refuse, then commit | measured: the commit landed on its branch. From inside: 127.0.0.1:5077 (the dtree server) and :5000 (the dashboard) refused the connection; `example.com` got 403 from the proxy; `api.anthropic.com` answered (401, no key) through the tunnel; `/usr/local` read-only; `git config --local` and a new hook in `.git/hooks` refused; a file written in $HOME stayed in the private tmpfs. On the host: `.git/config`, `.git/hooks`, the person's Claude settings and the main checkout unchanged | $0.0080 |
| `max_budget_usd: 0.001` | failed: `budget: $0.0017 of $0.0010`, after one turn | $0.0017 |
| a stop from the page while a command ran | stopped 1 s after the stop; nothing of the sandbox's process tree left | not reported |
| `timeout_s: 25` | failed: `longer than timeout_s (25 s)`; on SIGINT Claude Code still wrote its result | $0.0017 |
| `sandbox.root: "host"` | measured; `~/.claude.json` was the empty mask and Claude Code ran with it | $0.0026 |

Hosts seen by the proxy over all the runs: `api.anthropic.com:443` (allowed) and `example.com:443` (denied: the probe's own
request). Claude Code asked for nothing else; `platform.claude.com` was not needed, since no token refresh happened.

Again on 2026-11-02, after the shadow repositories, the graceful stop and the fixes of the review, each job sent with `dtree ai`
(the CLI, then dtree's runner process), sandboxed, network proxy, in a throwaway repository in /dev/shm:

| run | result | cost |
|---|---|---|
| add `mul(a, b)`, try `curl https://pypi.org/simple/`, commit | measured, 5 turns, 78,111 tokens: Claude Code committed `calc: mul` in the trial's shadow repository and dtree brought it into the project's `alt/1-yes`; the worktree and the shadow were removed afterwards; the main checkout, its HEAD and `main` unchanged. The proxy: `api.anthropic.com:443` allowed (3 connections), `pypi.org:443` denied (the job's own request) | $0.0051 |
| `dtree stop` right after its first command (a second stop at once found it stopped already) | stopped about 1 s later. On the runner's SIGINT Claude Code wrote a result of zeros (`error_during_execution`, $0, no usage) after an API call of 18,677 input tokens; the job kept 18,697 tokens counted from its stream and no dollars (unknown: the agent has no prices), and `dtree spend` showed the agent's `month_tokens` line | not reported by Claude Code |

From that run's stream: the API calls' input added up to the result's exactly (77,003), their output to 70 of the result's 1,108,
which is why the output counted from a stream is a lower bound.

## 8. Limits and open points

- **Token refresh.** Not tested: a token's expiry cannot be forced safely. A read-only credentials file serves a valid token;
  for jobs longer than a token's life, use `credentials: "rw"`, or log in again before sending them.
- **What a stopped Claude Code job cost.** A stop now goes through the runner (SIGINT first), and Claude Code writes a result,
  but on 2026-11-02 that result was all zeros: `total_cost_usd` 0 and no usage after a real API call. The job then counts the
  tokens of its calls from the stream (input in full, output at least) and leaves its dollars unknown. So `budget.month_usd` and an
  agent's `month_usd` see a stopped Claude Code job only through an estimate from prices; give the claude agent
  `price_in_per_mtok` / `price_out_per_mtok` (a rough estimate: the cached input is priced at the full input price), or cap it in
  tokens (`month_tokens`), if stops are frequent.
- **Codex** reports its tokens at the end of a turn, so its token cap is mostly seen when the work is done, and a Codex job
  stopped before its turn ended has unknown tokens and dollars (not 0): the budget cannot count it. Codex is stopped with SIGTERM;
  whether a SIGINT would make it report the turn so far was not tried.
- **The month's and the totals' caps** count what is known when a job starts, and the job gets what is left. Claude Code reports
  its cost only at the end, and jobs started together each get the same remainder, so jobs running in parallel can still take a
  cap past its end by what the others spend. Claude Code's token caps (`month_tokens`, `total_tokens`) are checked before a job
  only: it has no token cap of its own.
- **What a job can still write in its shadow repository** (sandbox.agent_mounts, reported 2026-11-02): `objects/info/alternates`,
  a new `info/attributes`, `config.worktree`, its own refs. Git on the host reads attributes in the worktree (`git status`, an
  update's merge), but a filter, diff or merge driver needs a config the job cannot write (none is set globally on this machine);
  `config.worktree` is read only with `extensions.worktreeConfig`, which the read-only config does not set.
- **Claude Code's own Bash sandbox setting** would need nested user namespaces. Inside dtree's sandbox they are disabled unless
  the project sets `sandbox.nested`.
- **The main checkout** is not shown to the job under the allow-list root. Files a job needs from it go in `sandbox.ro_binds`.

## 9. The chat loop (agents/chat.py)

Muse, Grok, a local model and the Orin are driven by dtree's own loop over their `/chat/completions` API. The model's API is
called from the runner, outside the sandbox; the `run` tool's commands run inside it (§1's filesystem, no network). From the
final review and the real race of 2026-11-02:

- **A stop and the time limit hold while the model thinks.** Each request runs in a thread of its own while the loop looks at
  the job every half second. A stop from the page or `dtree stop`, or the job's `timeout_s`, gives the request up at once: its
  connection is shut, so the server can stop working on it, and an answer that comes in after that is not acted on (none of its
  tool calls run). Before, a stop waited for the model's answer, up to `request_timeout_s`, and then ran the write it brought.
- **`request_timeout_s`** (default 600 s; the agent's own, else `chat.request_timeout_s`) is how long one request may take. A
  slow local model needs more: the Orin's llama.cpp server, shared and at ~5 tokens/s, took 281 s for one 1024-token answer, and
  a job failed at 600 s before it was given 1800. When it is reached the job fails with `the model did not answer within 600 s
  (agents.orin.request_timeout_s; a slow model needs more)`. For the Orin: `"agents": {"orin": {"kind": "chat", "base_url":
  "http://192.0.2.4:8095/v1", "model": "…", "max_tokens": 1024, "context_chars": 16000, "result_chars": 3000,
  "request_timeout_s": 1800}}`.
- **Every way out keeps the tokens spent.** An answer cut short, an answer that is not a chat completion, an API error, a failure
  of the loop itself (even when its log cannot be written): the job fails saying what went wrong, with the tokens of every answer
  it had counted, so the budget and the report count them. A request that went out and brought no answer back — given up on a stop
  or a time limit, or cut short (an answer cut off, a dropped connection, a 5xx) — has no usage, but the server may have spent (and
  billed) it: it is counted as the previous request's prompt, which it carried whole (nothing more when `context_chars` cut the
  conversation, and never the previous answer, whose reasoning tokens are not sent back), and the job is marked `usage_partial`, so
  its dollars and tokens show `+ ?` and what is left of a cap is "at most". A request never sent (no connection was made) or turned
  away (a 3xx or a 4xx) adds nothing. A redirect is never followed: it is the API's answer (`the API answered 307: …`, a `base_url`
  to fix), so the request and its key are never sent anywhere else. An answer that carries no usage at all marks the job
  `usage_partial` too: its tokens are not known, not 0. A tool that fails — a number given for a path, an object given for a file's content — answers the model
  `error: …` and the loop goes on. Arguments given as a JSON object instead of a string (some servers do) are taken as they are.
- **A busy API waits, it does not fail the job.** A 429 (a rate or quota limit — Gemini's free tier takes 5 requests a minute: a
  real job of 2026-11-02 was refused at its 7th request) or a 503 (overloaded: "high demand") ran nothing, so it is not counted,
  and the same request is asked again after the wait the API names (`Retry-After`, or Gemini's `retryDelay`), else 5 s doubling
  up to 60 s, at most `retries` times (8), within the job's `timeout_s`; the log says `⏳ the API is busy (429): asked again in
  13 s (1 of 8)`. A stop ends a wait at once. A wait longer than `retry_max_wait_s` (300 s: a quota for the day) is not waited
  for: the job ends with the API's answer and why. Each try takes a slot of the agent's rate limits (§10): the limits keep dtree
  within the provider's own, and a 429 that still comes (the key used elsewhere too) is waited for here.
- **`finish`.** `built` must be a JSON boolean; the words `true` / `false` are taken too, in any case (`"false"` is not built).
  Anything else is sent back to the model (`error: built must be true or false …`), and after the third such answer the work is
  taken as not built.
- **The log.** The job's log names the sandbox the commands run in (`🔒 sandbox: bwrap · root allowlist · network none …`, or
  `⚠ sandbox: none …`), every tool call (`🔧 run git commit -q -m …`) and the head of its result (`↳ exit 1 · nothing to commit,
  working tree clean`). `job-N.jsonl` has a line per answer (`answer`: its text, tool calls and usage), per tool result
  (`tool_result`), and the `start` and `end` of the loop (`error` with its traceback when the loop itself failed); never the key.
- **A small context** (`context_chars`): the older tool results and the large arguments of older tool calls (a `write_file`'s
  content) are cut to a stub, oldest first, until the conversation fits; the system prompt, the task and the latest three messages
  are kept whole. The cut arguments stay a JSON object with their short values (the path), since a server may parse them.
- **The cost so far.** A chat agent with prices writes its estimated cost to its job after every answer, so the page shows it and
  a job started later counts it against the month.

## 10. Rate limits: requests_per_minute, requests_per_day

A provider limits how often its API may be asked: Gemini's free tier (2026-11-02) takes 5 requests a minute and 20 a day per model,
and a real job of that day was refused at its 7th request. The busy retries of §9 wait out a 429 once it comes; a rate limit keeps
dtree from sending the request at all. Two settings of a chat agent, in its own settings (the user asked on 2026-11-02 for a
settings page where keys and rate limits are set):

| key | default | what it is |
|---|---|---|
| `requests_per_minute` | none | at most this many requests to the model's API in any 60 seconds |
| `requests_per_day` | none | at most this many in any 24 hours |

Both are whole numbers above 0, or `null` (or nothing) for no limit, and both are **shared by every job of the agent**: a race, a
follow-up and an update job of it count together, whatever runner process they run in. For Gemini's free tier:

```json
"agents": {"gemini": {"requests_per_minute": 5, "requests_per_day": 20}}
```

**How they hold.** Every request dtree's chat loop sends is a row of the store's `requests` table ([schema.md](schema.md)). Before
each request — every try, a busy API's retries too — the loop takes a slot (`store.request_slot`): the agent's requests of the last
60 seconds and of the last 24 hours are counted and, when neither limit is reached, a row is added, in one `BEGIN IMMEDIATE`
transaction, so runners in several processes never take more than the limits between them (six processes asking for 20 slots
each at the same moment got exactly 7 of a limit of 7; without the one transaction 8 to 10 got through). The windows are rolling,
not a calendar day: never more than N in any 24 hours holds whatever day the provider counts by.

- **A full minute waits.** The request waits until the oldest of the minute has left the window, and the job's log says so:
  `⏳ requests_per_minute 5: the next request in 9 s`. A stop or the job's deadline ends the wait at once (nothing in flight,
  nothing counted). A full minute never refuses a job before it starts: its slot comes within the minute.
- **A day used up refuses a job.** `jobs.open_job` (a send, `dtree ai`, the page's "🤖"), `followup` and `race` refuse it before
  anything is touched, as a budget refusal is: a `spend.RateRefused`, a DecisionError whose code is `rate` and whose `setting` is
  `agents.<name>.requests_per_day` (the API's answer and a race's entry carry both; the refusal and the race entry carry an
  `error_th` too), and the question gets a `rate_refused` event. The refusal says:
  `agents.gemini.requests_per_day (20) is used: the next request in 3 h 12 min; gemini was not started`. The agents list
  (`GET /api/agents`, `dtree agents`) shows such an agent as not available with that reason (and `reason_th`), until a slot comes
  again.
- **A day used up ends a running job.** A job whose request finds the day used up ends there with nothing in flight and nothing
  more counted (the answers it had keep their tokens):
  `agents.gemini.requests_per_day (20) is used: the next request in 3 h 12 min`. When the day's next slot comes within
  `retry_max_wait_s` (300 s) and before the job's deadline — the oldest request of the day is about to leave the window — the job
  waits for it instead, as for a minute (`⏳ requests_per_day 20: …`). Two jobs sent at once with one request of the day left
  both start; the one whose first request comes second then ends at once, having sent nothing.
- **Who counts.** Every chat agent's requests are counted, with limits or without (an agent without limits is only shown its
  counts). The count is per agent name and per store: two agents on one key and one model each count their own (give them limits
  that add up to the provider's), and so does each project using the key.
- **Not for Claude Code or Codex.** They make their own requests, which dtree neither sees nor paces: a rate limit set on one is
  a setting problem (`agents.claude.requests_per_day is a setting of dtree's chat loop, which sends a chat model's requests
  itself; Claude Code makes its own requests, …`), and the agent is not available until it is removed. So is a limit of 0 or one
  that is not a whole number above 0 (§6, settings that refuse every job).
- **Where it shows.** Each chat agent in `GET /api/agents`, and its row of `GET /api/spend`, carry `requests_per_minute`,
  `requests_per_day`, `requests_minute`, `requests_day` and `requests_wait_s` (§6); `store.request_counts(agent)` gives the
  settings page the same counts (`{minute, day}`); `dtree spend` prints a line for each agent with a limit.

A throwaway store of 2026-11-02 (a stand-in model API on 127.0.0.1; `gemini` with 2 a minute and 4 a day, and two requests of an
earlier job 57 s and 55 s old), one job sent through dtree's runner:

```
10:43:29 ⏳ requests_per_minute 2: the next request in 3 s
10:43:32 🔧 list_files {}
10:43:32 ⏳ requests_per_minute 2: the next request in 2 s
10:43:34 🔧 list_files {}
10:43:34 ⚠ agents.gemini.requests_per_day (4) is used: the next request in 23 h 59 min
10:43:34 ■ job failed: agents.gemini.requests_per_day (4) is used: the next request in 23 h 59 min
```

The stand-in received the job's two requests 3.0 s and 5.0 s after it started, 60 s after each of the earlier two: each waited for
one of those to leave the minute. A second send was refused with code `rate`, and `dtree spend` printed:

```
each agent's requests (its rate limits; every job of it, now):
  gemini: 2 of 2 in the last minute (agents.gemini.requests_per_minute) · 4 of 4 in the last 24 h (agents.gemini.requests_per_day), used up: the next request in 23 h 59 min
```
