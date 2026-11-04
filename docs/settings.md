# Settings

The user asked on 2026-11-02 for a settings page where keys, rate limits and the
other settings can be set. This is its back end: `src/dtree/settings.py`, the routes it adds to the server, and the commands it adds to
the CLI. The page (the "⚙ Settings" overlay of `tree.html`) is drawn from the view below; the rate limits themselves
(`requests_per_minute`, `requests_per_day`) are counted by the store and the chat loop.

Everything is kept where it was: `<project>/.dtree/config.json`, merged over dtree's defaults (`project.DEFAULTS`, `project.AGENTS`).
The page and the CLI change that file, and only the settings listed in §1; the rest (`rules`, `test_command`, the `sandbox` section,
`verify.select` and `verify.measures`, an agent's `disallowed_tools`, `extra_body`, `pass_env`, …) are still edited by hand.

## 1. What can be set

| section | settings |
|---|---|
| jobs | `default_agent` (one of the agents), `max_parallel_jobs`, `timeout_s` |
| budget | `budget.month_usd`, `budget.job_usd`, `budget.job_tokens` |
| chat | `chat.max_steps`, `chat.max_tokens`, `chat.temperature`, `chat.command_timeout_s`, `chat.request_timeout_s`, `chat.retries`, `chat.retry_max_wait_s`, `chat.sandbox`, `chat.context_chars`, `chat.result_chars` |
| verify | `verify.enabled`, `verify.tests`, `verify.timeout_s` |
| server | `server.allowed_hosts` (used at once); `server.host`, `server.port` shown read-only: the running server reads them at its start, so they are changed in the file and `dtree serve` is started again |

Each agent, by its kind (`agents.<name>.<key>`):

| kind | settings |
|---|---|
| every kind | `label`, `model`, `month_usd`, `month_tokens`, `total_usd`, `total_tokens`, `price_in_per_mtok`, `price_out_per_mtok` |
| claude | `max_budget_usd` (the job's dollar cap: `--max-budget-usd`), `sandbox`, `network`, `network_allow`, `credentials`, `permission_mode` |
| codex | `max_tokens_total` (the job's token cap), `sandbox` (Codex's own: `workspace-write`, `read-only`, `danger-full-access`) |
| chat | `base_url`, `key_env`, `requests_per_minute`, `requests_per_day`, `retries`, `retry_max_wait_s`, `request_timeout_s`, `max_tokens_total`, `max_steps`, `max_tokens`, `temperature`, `context_chars`, `result_chars`, `sandbox` |

The per-job caps go where they act (spend.py, [sandbox-and-budget.md](sandbox-and-budget.md) §6): `max_budget_usd` is Claude Code's,
`max_tokens_total` the chat loop's and Codex's; a field that does nothing for a kind is not shown for it. `key_file` is set with the key
(§4), never as a setting. A chat agent's own `retries`, `sandbox`, `max_steps` … win over the `chat` section; going back to the section's
value is "↺ default" (unset), never null: in the chat loop a null `retries` would fail the job and a null `sandbox` would mean none.

## 2. The view

```json
{"ok": true, "file": "/abs/.dtree/config.json", "exists": true, "tracked": true, "local": true, "name": "demo", "in_sync": true,
 "keys_dir": "~/.config/dtree/keys",
 "sections": [{"id": "chat", "th": "วงแชตของ dtree (Muse, Grok, Gemini, local)", "en": "dtree's chat loop (Muse, Grok, Gemini, local)",
               "fields": [FIELD, …]}, …],
 "agents": [AGENT, …],
 "presets": [{"name": "gemini", "kind": "chat", "label": "Gemini (Google AI)", "in_config": true}, …],
 "kinds": {"claude": ["label", "model", "max_budget_usd", …], "codex": […], "chat": […]}}
```

- `tracked`: git tracks the file (commit it to keep the settings); `in_sync`: the running server's settings are the file's (false when
  the file was edited by hand since: the next save reloads it); `file_error` when the file is not JSON (the page still opens with the
  values in effect, and nothing is written over it).
- **FIELD** `{"path": "chat.retries", "kind": "int", "min": 0, "max": null, "enum": null, "nullable": false, "default": 8, "own": 5,
  "set": true, "value": 5, "th", "en", "help_th", "help_en", "restart": false, "readonly": false, "warn": null}`. `kind` is int, number,
  string, bool, enum or list. `own` is what the file says (null when it does not set it; `set` tells a null written there from none);
  `value` is what is in effect; `default` what applies without the file's own value (for an agent: its preset's, else the `chat` section
  or the budget it falls back to). `help_en` is the config schema's description (`src/dtree/schema/config.schema.json`), else a line of
  settings.py's; `help_th` is settings.py's. `exclusive_min: true` where the minimum itself is refused (a dollar cap: more than 0).
  `readonly` with a `note` {th, en} for `server.host` / `server.port` (`restart: true`) and, for a client on another machine, for the
  settings only this machine may change (`local_only: true`, §5). `warn` {th, en} when the value in effect weakens safety: `sandbox:
  none` (an agent's or `chat.sandbox`), `network: host`, `credentials: rw`, Codex's `danger-full-access`. `group` (model, rate, price,
  cap) puts an agent's settings together.
- **AGENT** `{"name", "kind", "label", "model", "preset", "available", "reason", "fields": [FIELD with path "agents.<name>.<key>"],
  "key": KEY, "requests": {"minute": 0, "day": 3} | null}`. `available` / `reason` are what `GET /api/agents` says (its own check, the
  spend settings, whatever else refuses a job before it starts). `requests` is the store's count of the agent's requests in the last
  minute and day (`store.request_counts`, when the store has it), for a chat agent; null otherwise.
- **KEY** `{"needs": true, "via": "file", "env": "GEMINI_API_KEY", "env_set": false, "file": "/home/u/.config/dtree/keys/demo-gemini.key",
  "file_set": true, "bytes": 40, "mode": "600"}` — never the key, nor any part of it. `via`: where the chat loop takes it from now
  (`env` first when the variable is set, then the file). `needs`: a chat agent whose `base_url` is not this machine (Claude Code and
  Codex log in by themselves: a `note` says how). `warn` when other users can read the file (its mode is not 600); `note` when both the
  variable and the file are there (the variable wins).

## 3. Changing settings

```json
{"set": {"chat.retries": 5, "agents.gemini.requests_per_minute": 5}, "unset": ["budget.month_usd"],
 "add_agent": {"name": "gemini-pro", "from": "gemini"}, "remove_agent": "grok"}
```

Applied in the order add, set, unset, remove, all or nothing. The answer is the new view with `changed` (the paths whose value in the
file changed), or 400 `{"ok": false, "error", "error_th", "path"}` naming the first bad path (403 for a setting only this machine may
change, §5). Nothing is written when any part is refused.

1. **Paths.** Only those of §1. Any other (`rules`, `agents.gemini.key_file`, `agents.claude.requests_per_minute` …) is refused naming it.
2. **Values.** By kind (an int is a whole number; a number is finite; true / false is a JSON boolean), `min` / `max`, `enum`; text is one
   line; `base_url` is an http(s) address; `key_env` is the name of a variable (A–Z, 0–9, _), and its message never repeats the value, in
   case a key was pasted there; `network_allow` is checked as the egress proxy reads it; `allowed_hosts` are host names; null only where
   `nullable` (an emptied text box is null there).
3. **The whole new config.** `default_agent` must be an agent (removing the default agent needs a new default in the same change). No
   agent may come to have settings that refuse every job of it (`spend.settings_problem`: a cap or price that is not a number, a
   `month_usd` / `total_usd` of an agent that reports tokens only and has no prices, a cap of 0, `budget.month_usd` 0, a
   `requests_per_day` of 0): such a change is refused, saying why. A problem an agent had before the change (written by hand) does not
   block other changes, so the page can still fix it. A change under `verify` is read as dtree's own checks would read it.
4. **Agents.** `add_agent` from a preset copies the preset's settings (Claude Code's default `disallowed_tools` included: a new agent
   inherits nothing, and without them a Claude Code job could use WebSearch, RemoteTrigger or `git push`), with its label and the new
   name; a copy does not follow later changes of dtree's defaults. From `chat`: a blank OpenAI-compatible agent to fill in (`base_url`,
   `model`). A name is 1–40 letters, digits, `-` or `_`; a preset removed earlier comes back by its own name. `remove_agent` writes
   null for one of dtree's own agents (its settings in the file go too) and deletes one the file added.
5. **Writing.** The file is read again under a lock (a thread lock and `flock` on `.dtree`, so the server and `dtree set` never lose each
   other's change), changed, written as `json.dumps(…, ensure_ascii=False, indent=1) + "\n"` (dtree init's format: a file in it keeps
   every untouched line byte for byte) through a temporary file beside it (`O_CREAT|O_EXCL`, the file's mode, fsync, `os.replace`), its
   notes (`_…` keys) and every key not touched kept. A section or a preset's entry left empty by an unset goes; an agent the file added
   is never emptied away. A linked config is written where the link points.
6. **In effect.** The server's own config is reloaded in place (`Project.reload`): the next request and every job started after it use
   the new settings, `server.allowed_hosts` at once. A running job keeps the settings it started with.
7. **The log.** `<logs>/settings.log` gets a line per change: `2026-11-02 14:03:05 · local · user · set chat.retries=5 · set
   agents.gemini.requests_per_minute=5 · unset budget.month_usd` (`remote` for a client on another machine; `cli` for `dtree set`). Never
   a key.

## 4. Keys / key: `POST /api/settings/agents/<name>/key`

`{"key": "…"}` sets a chat agent's key; `{"remove": true}` forgets it. The answer is `{"agent", "key": KEY, "available", "reason"}`
(and `deleted`, `file` for a removal; `warn` when `$key_env` is set where the server runs, since the chat loop reads it first).

- **From this machine only.** A client that is not this machine (its address is not loopback) gets 403: `a key is set on the server's own machine only (http://localhost:<port>), or with dtree key <agent>` — the LAN connection is plain HTTP, and the key would cross it in the clear. The
  view tells the page (`local: false`), which greys the key inputs.
- **The key.** One line of printable characters without spaces, 1–4096 of them (the spaces and line break around a pasted key are
  dropped). A refusal never repeats it.
- **Where.** The agent's `key_file` when it has one; else `~/.config/dtree/keys/<project>-<agent>.key` (the directory 0700, and
  `~/.config/dtree` when dtree makes it), and `key_file` set to that path (`~/…`) in the config. The file is 0600 from its first byte:
  a temporary file made with `O_CREAT|O_EXCL` and mode 0600, fsync, `os.replace`. The log names the file and its size.
- **Removing.** `key_file` is unset in the config. The file is deleted only when it is dtree's own (in `~/.config/dtree/keys`, judged by
  where it is, not where a link there points) and no other agent of the project names it. Another tool's key file, such as
  `~/.config/ut-meta/meta.key`, is never deleted; a key in an environment variable stays the environment's.
- **Claude Code and Codex** log in by themselves (`claude` then `/login`; `codex login`): their key route says so.

## 5. From another machine

A write from another machine needs the token (`.dtree/write_token`), as every write does. Besides the key itself, these settings are
changed from this machine only (403, `code: "local_only"`; read-only in the view for another machine): an agent's `base_url` and `key_env`
(the key is sent to `base_url`, and `key_env` chooses which variable's value is sent: from elsewhere, they could send a key to another
host), and what weakens a sandbox — an agent's `sandbox`, `network`, `network_allow`, `credentials`, and `chat.sandbox`. Before this page,
no write over the network could change the config at all: the token's holder could start sandboxed jobs, not turn the sandbox off.

## 6. Testing an agent

`{"ok", "available", "reason", "check"}`: whether the agent can run here (as the agents list says) and, for a chat agent, `GET
<base_url>/models` with its key (10 s; no redirect followed, so the key is never sent anywhere else): `check` = `{"status": 200,
"models": 60, "model_listed": true, "url", "ms", "key": "sent"}` or `{"status": 401, "error": "…"}`, `{"status": null, "error": "no
answer: …"}`. `model_listed` says whether the agent's `model` is among the models (Gemini lists `models/<name>`). Every text of the answer
has the key masked out: the whole of it and any 4 or more of its characters in a row (some APIs echo a key's end). `ok` is false, with
`error` / `error_th`, when the agent cannot run or the API did not list its models. Claude Code and Codex: `available()` only.

## 7. The CLI

```bash
dtree settings [--json]                    # every setting: in effect, what the file says, the default; agents, keys (never shown)
dtree set chat.retries 5                   # VALUE as JSON when it parses (5, null, true, ["a"]), else text; checked as the page checks
dtree set agents.gemini.requests_per_day 20
dtree set server.allowed_hosts mybox,mybox.lan     # a list takes a comma list too
dtree unset budget.month_usd               # back to the default
dtree key gemini                           # asked without echo on a terminal; else one line of stdin: pass show gemini | dtree key gemini
dtree key gemini --remove
dtree test-agent gemini                    # exit 1 when the agent cannot run or its API does not answer GET /models
```

The CLI runs on this machine, so §5 does not limit it; a text setting keeps the text typed (`dtree set agents.x.model 3` is "3").

## 8. Limits and open points

- `chat.temperature` 0 and `chat.retry_max_wait_s` 0 (and an agent's own) are accepted here, but the chat loop reads them with
  `or` (`float(s.get("temperature") or 0.2)`), so 0 acts as 0.2 and 300 s (agents/chat.py).
- A cap of 0 (`month_usd: 0`, `budget.month_usd: 0` …), which the schema allows as "no job", is refused here, as every setting that
  refuses every job of an agent is; set it in the file by hand if that is what is meant, or remove the agent.
- `server.host` and `server.port` need a restart of `dtree serve`; the page shows them read-only.
- A job already running keeps its settings; the agents' availability in the view is checked on every view (Codex runs `codex login
  status`), so a save answers as fast as `GET /api/agents` does.
