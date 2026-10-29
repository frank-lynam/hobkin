# hobkin

An AI agent for austere environments: one stdlib-only Python file, no installs,
no open ports. Use it from the command line, or as a worker on
[hearthboard](https://github.com/frank-lynam/hearthboard). (A hobkin is a
little hob, the house spirit that does the chores while the house sleeps.)

## Get one running

Anywhere with Python 3.9+ (Linux or another POSIX system). Nothing to install:

```sh
curl -O https://raw.githubusercontent.com/frank-lynam/hobkin/main/hobkin.py
python3 hobkin.py https://openrouter.ai/api/v1 anthropic/claude-sonnet-4.5 sk-or-... --chat
```

**As a background service:** leave out `--chat`, and name it if you like. It
runs as a systemd user service where there is one (in the background where
there isn't), and keeps running after you log out. Talk to it whenever:

```sh
python3 hobkin.py https://openrouter.ai/api/v1 anthropic/claude-sonnet-4.5 sk-or-... --id helper
python3 hobkin.py chat helper
```

**As a hearthboard worker:** add the board's URL and the agent's token. The
board's Users page shows this line, ready to copy, when you add an agent:

```sh
python3 hobkin.py https://openrouter.ai/api/v1 anthropic/claude-sonnet-4.5 sk-or-... \
  --hearthboard http://board.lan:43917 at_THE_AGENTS_TOKEN
```

A worker doesn't run the board's wait loop itself: while it's idle, hobkin
waits on the board for it and wakes it with what the board says (`wake` in its
config; the heartbeat shows "waiting on the work system"). One waiter, so nothing
races for the same news. Board chat that arrives while it's busy is answered by
the front desk (see "Talking to it").

Or in two steps: set up an agent as above (`--id helper`), then paste the
board's line with `--id helper`. It joins the agent you already have; if it's
running, it restarts in place (keeping its conversation) and is told it's now a
worker. More workers on the same machine reuse the model: `python3 hobkin.py
--hearthboard http://board.lan:43917 at_ANOTHER_TOKEN`.

Installed rather than copied: `pipx install hobkin` (or `uv tool install
hobkin`), then the same commands as `hobkin ...`.

## What that does

That:
- **checks** the key works and the API offers that model (a typo gets the close
  names back; `--force` skips the check for APIs that don't list their models);
- **sets it up**: on a fresh install, as the default model in
  `~/.hobkins/_shared/config.json`, with the key in `_shared/keys/`, readable only
  by you. On a machine that's already set up it changes nothing shared: the model
  and its key go in the new agent's own folder, and your other agents and
  settings stay as they are. Leave the key out and it asks for it,
  so it stays out of your shell history; leave it out for a local server with none;
- **starts a new agent** as a systemd user service (see "Keeping agents running"),
  or in the background where there's no systemd (or with `--no-service`);
- with `--chat`, **connects you** to it. Without, it prints how.

The API style comes from the URL (`anthropic.com` is Anthropic's; anything else is
OpenAI-style: OpenRouter, vLLM, llama.cpp, Ollama's `/v1`, LiteLLM...); `--api`
overrides it. `--id NAME` names the agent instead of a made-up name like `bodkin`. Run it again with
another URL or model to start another agent on that one.

To see what an API offers before choosing: `hobkin models URL [KEY]`; `hobkin models`
lists your configured model's API, and `/models` does the same in a chat.

With `--hearthboard URL TOKEN`, it also checks the token with the board, then:
- saves the **board's own skill** (`/api/skill`) in that agent's own `skills/`, so
  it works by the board's current rules (and agents here that aren't workers
  don't mistake the board for their work system);
- puts **the token** in that agent's `secrets.env` (readable only by you; the
  model only ever sees it as an environment variable, never in its context);
- sets its **idle prompt** to the board's wait loop, so it picks up work itself;
- names the agent after its board user, unless you give `--id`.

Without `URL MODEL KEY` it uses the model you've already set up.

## Why it's built this way

- **One file, standard library only.** `hobkin.py` and Python 3.9+. Nothing to
  install, so it runs on locked-down servers, containers and old boxes alike.
- **No ports.** It listens on nothing; it only makes outgoing calls to the
  model API (and your board, if it has one).
- **Files are the interface.** Talk to it with `echo`, watch it with `tail -F`,
  and see exactly what the model sees with `cat`.
- **Many per account.** Each agent has its own id and folder, and a lock stops
  two processes running the same one.
- **With or without a work system.** Give it skills for a tracker or a queue and it
  pulls work from there; with none, its work is what you ask it in the chat, and
  between requests it waits rather than wandering the machine.

## The long way

```sh
hobkin                 # a new agent: prints its name, e.g. bodkin
hobkin bodkin          # resume that agent later (its context is kept)
```

The first run creates `~/.hobkins/_shared/config.json`. Set your model there (see
**Models**) and make sure the API key is reachable.

Then, from another terminal, chat with it:

```sh
hobkin chat bodkin
hobkin chat            # no name: the only one running, else an idle one (it says which, and who else is up)
```

While it's busy, an empty input line shows a spinner with what it's doing
(`bodkin is thinking (12s)`, `bodkin is running `pytest` (40s)`), so a slow
model doesn't look like a dead one. It disappears as soon as you type.

Its replies come under its name (`bodkin> ...`), so with several chats open
you know which hobkin is talking. (The `out` file says `agent>`, for scripts.)
When a chat opens, the agent says hello first, and it's told which folder you
opened the chat in. When you say "here" or "this folder", it knows what you
mean. It's told that folder isn't its workspace, and in safe mode it can read
there but not write.

In a terminal it's in colour, so things are easy to find when you scroll back:
its name in magenta, file names, paths and `code` in green, **bold** in
bold, links underlined, tool calls dimmed and failed commands in red (agents are
asked to put names of things in backticks for this). Piped, or with `NO_COLOR`
set, it's plain text. Its replies appear as they come, and each tool call as one line (a command, a file
read, a failed exit status), without its output; `hobkin chat ID --full` shows the
output too, as the `out` file has it. Each line you type (or paste) is sent;
`/stop` and the other commands work, and Ctrl-D leaves. Output never wipes out
what you're typing: the line is redrawn below it, with the cursor where you left
it, even mid-edit. You get the usual line
editing: ↑/↓ for earlier messages (kept in the agent's `.chat_history`),
←/→, Ctrl-R to search. Piped, it sends the
input, prints the reply and exits: `echo "status?" | hobkin chat bodkin`.

Or with plain files:

```sh
echo "Clone https://example.com/repo and get its tests passing" > ~/.hobkins/bodkin/in
tail -F ~/.hobkins/bodkin/out
```

Run it in the background however you like: `nohup hobkin bodkin &`,
tmux, or a systemd user service.

## An agent's folder

`~/.hobkins/ID/` (or `$HOBKIN_HOME/ID/`):

| File | What it is |
|---|---|
| `in` | A named pipe. `echo text > in` sends a message. A write waits until the agent is running; `hobkin send ID text` queues it instead. |
| `out` | Everything it says and does since the last compaction, with timestamps: messages, commands, output. At each compaction it's moved to `archive/` and starts afresh. `tail -F out` |
| `context.jsonl` | Exactly what the model is sent: line 1 is the system prompt, then one message per line. Rewritten after every change. |
| `MEMORY.md` | The agent's own notes. Part of its instructions on every turn, and kept when the conversation is compacted. Past 80 lines, it's told to prune. |
| `BLOOPERS.md` | Its blooper reel: its own mistakes, told for laughs, each with the lesson. Also part of its instructions every turn, so the jokes double as lessons learned. Past 30 lines, it's told to keep the best. |
| `state.json` | Model, context window (and where that number came from), status, pid, compaction count. |
| `work/` | Its working directory. `work/tools/` holds tools it made itself (see "Its tools"); in break-glass mode, `work/incident-*.md` is the incident's timeline. |
| `reminders.json` | Its reminders (when, message, repeat). Readable and editable by hand. |
| `shell.sh` | Its shell's saved state (folder, variables, functions, aliases), restored before each command. Secrets are never saved here. |
| `secrets.env` | Optional `KEY=value` lines, set as environment variables in its shell (never put in its context). |
| `config.json` | Optional per-agent settings over the shared ones. |
| `*.md` | Optional per-agent instructions; replace shared files of the same name. Chores put theirs here as `CHORE-<name>.md`. |
| `archive/` | At each compaction, the old conversation (`context-*.jsonl`, messages only) and its `out` log (`out-*.log`). Deleted after 30 days, except the newest. |
| `jobs/` | Background jobs' output and exit codes; a finished job's folder goes after 30 days. |
| `skills/` | Its own skills (a hearthboard worker's board skill goes here). |
| `.chat_history` | What you've typed in `hobkin chat` (↑ brings it back). |
| `hobkin.log` | Start-up errors, when it was started in the background without systemd. |
| `queue`, `lock` | Messages sent while it was stopped; the process lock. |

`~/.hobkins/_shared/` holds what all agents share: `config.json`, `AGENTS.md` (how
to work as a hobkin: a task routine with checklists, failure handling,
organization, memory and tidying up; created on first run, edit freely: it's never
overwritten, so new defaults reach it only if you delete it), an optional `SOUL.md`,
any other `.md` instructions, `secrets.env`, `keys/` (API keys from setup, readable
only by you) and `skills/`. `~/.hobkins/_incidents/` holds shared incidents' holds.

**The system prompt** is:

1. `SOUL.md`, then `AGENTS.md`, then the other `.md` files in name order;
2. the list of skills (see below);
3. `MEMORY.md`.

Each `.md` file is taken from the agent's folder if it has one, otherwise from
`_shared/`. The prompt is rebuilt every turn, so edits take effect on the next one.

## Skills

Skills use the same format as Claude Code and other agents: a folder holding a
`SKILL.md` with a `name` and `description` in its front matter, plus any scripts
or reference files. Install one by copying its folder into
**`~/.hobkins/_shared/skills/`**, and every agent can use it.

```
~/.hobkins/_shared/skills/release/SKILL.md
~/.hobkins/_shared/skills/release/build.sh
```

The prompt lists only each skill's name and description. When a task calls for
one, the agent opens it with the `skill` tool. That gives it the instructions,
the skill's folder as a full path, and its list of files, so it never has to
work out where scripts live. Dozens of skills cost a line each.

- An agent's own `skills/` folder can add skills, or replace a shared one with the
  same name.
- `skill_dirs` in config adds more folders at the lowest priority, e.g.
  `["~/.claude/skills"]` to reuse skills you already have.
- New skills are picked up on the next turn; no restart needed.

## Talking to it

Anything you write to `in` is a message, and the agent is told plainly what to
do with it, the way a work tracker's API would:

- **When it's free:** `[message from frank, Sat 2026-10-17 09:12: they're
  talking with you now. Answer them in your reply]`. Set `"operator": "frank"` in
  `config.json` (the name people know you by, e.g. your tracker username) and
  its system prompt says who that is; without it, `[message from operator, …]`.
- **When it's busy running a command** (a build, a test run, a tracker's wait),
  it answers straight away, while the command keeps going: hobkin asks the model
  for a short reply with the agent's own conversation so far and your message
  (the "front desk"). The answer appears in the chat, and the agent gets the whole
  exchange with its command's result, so a request you made isn't lost. Its
  request starts exactly like the agent's own, so a local server (llama.cpp,
  Ollama, vLLM) reuses its KV cache, and it's never sent alongside the agent's
  own model calls, so one server slot is enough. A message that arrives while
  the model is mid-reply is answered as soon as that reply is done.
  **A tracker's chat too:** with an `inbox` in config (`--hearthboard` sets one
  up), hobkin checks it every 15 seconds while a command runs; a message from a
  person there is answered the same way and the reply posted back to that
  conversation. Messages that only need reading (other agents', notes) are left
  for the agent. Any tracker can work this way: `inbox` is a URL that returns
  `{"messages": [{"id", "thread", "from", "text", "needs_reply"}]}` and a `reply`
  URL (`{thread}` filled in) that takes the text as `text/plain`, with headers
  whose `${NAMES}` come from `secrets.env`.
  `"front_desk": false` turns it off: then the message rides along with the
  command's result, `[… while you were working: answer it in your next reply,
  then carry on]`, with one reminder if it goes on working without a word.
- **While you're talking with it,** the idle prompt holds off for 5 minutes
  after each message, so it doesn't wander off between your messages.
- **After a quiet spell** the label says how long it's been (`; 3d 2h since you
  last worked`), so Friday's request isn't mistaken for today's.

Automatic messages are marked and timed too (`[from hobkin: idle prompt, …]`,
`[Reminder #3 …]`, finished jobs), so the agent never mistakes them for you. A
line that is just one of these is a command instead:

| Command | Effect |
|---|---|
| `/stop` | Finish cleanly and exit (Ctrl-C and SIGTERM do the same). |
| `/pause`, `/resume` | Stop and restart work; messages wait until `/resume`. |
| `/interrupt` | Kill the command it's running now (it's told it was interrupted). |
| `/compact` | Summarise and clear the conversation now. |
| `/status` | Write its status to `out`. |
| `/reminders` | Write its reminders to `out`. |
| `/veto` | Block every announced command that's waiting to run (see "Announce first"). |
| `/model [NAME]` | Alone: the model in use and the ones configured. With a name: switch to that configured model, or to any model the agent's API offers (it gets a config entry like the current one). Saved in the agent's `config.json`, so it lasts. It happens at once, even mid-call (that call is dropped and asked of the new model) or while waiting out a rate limit. |
| `/models` | List the models the agent's API offers (the one in use starred), with their context windows. |
| `/limits` | Ask the API for the context window again (it's also re-asked before every compaction and after a "too long" error, so a window that grew or shrank is noticed). |
| `/hello [FOLDER]` | Sent by `hobkin chat` as it opens (not logged): the agent says hello, and is told the folder you opened the chat in. |
| `/restart` | Once the current step is done, start over in the same process on the code on disk now (after a `git pull` or `pip install -U`), and carry on. The agent can send it itself: a repeating reminder can pull and restart. |

`hobkin chat [ID]` puts both in one terminal (see "The long way"). Commands work while the model is thinking, too: the call runs in the background. `hobkin list` shows all agents, their status and their last line of output.

## Models

`config.json` names the model to use and describes each one:

```json
{
  "model": "claude",
  "models": {
    "claude":     {"api": "anthropic", "base_url": "https://api.anthropic.com",
                   "model": "claude-sonnet-5-5", "key_env": "ANTHROPIC_API_KEY", "key_file": "~/.anthropic.key"},
    "openrouter": {"api": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "model": "anthropic/claude-sonnet-5.5", "key_file": "~/openrouter.key"},
    "local":      {"api": "openai", "base_url": "http://127.0.0.1:8080/v1", "model": "local-model"}
  }
}
```

`model` picks one of `models`. The models are exactly the ones in your config
files (the three above are only the starter file), so a model you delete is
gone. An agent's own `config.json` can add models, or change settings on a
shared one (e.g. its `requests_per_minute`), and can pick a different `model`.
If `model` names one that isn't defined, hobkin stops and lists the ones that
are; if it's left out and there's only one model, that one is used.

- `api`: `anthropic` (the Messages API) or `openai` (Chat Completions: OpenAI,
  OpenRouter, vLLM, llama.cpp, Ollama, ...).
- The key comes from the environment variable `key_env`, or else from the file
  `key_file`. It's removed from the agent's shell environment.
- `headers` (optional): extra HTTP headers.
- `thinking` (optional): `"adaptive"`, a token budget (`8000`), or the API's own
  object. Anthropic only; a budget must be smaller than the `max_tokens` sent
  (`max_output_tokens`, or 16,000 when that is unset).
- `effort` (optional): `"low"` to `"max"`. It goes to Anthropic as
  `output_config.effort`, to OpenRouter as `reasoning.effort`, and to other
  OpenAI-style APIs as `reasoning_effort`.
- `extra_body` (optional): anything else your provider takes, merged into every
  request as is.
- `prompt_cache` (optional): on OpenAI-style APIs, mark the system prompt and the
  newest message for caching. It's on by default for Claude models (e.g. on
  OpenRouter), where re-reading the conversation then costs about 5% of the
  normal input price; a test session went from about $0.023 per call to $0.001.
  On OpenRouter, each call's reported cost is added up in `state.json`
  (`spend_usd`).
- `requests_per_minute` (optional): a cap on calls to this model. It's shared by
  every agent using the same API, model and key, because that's what the
  provider counts. The agents coordinate through a small lock file in
  `_shared/` (holding a hash of the key, never the key). An agent at the cap
  waits, and still answers commands such as `/stop` while it does.

**When the provider rate-limits anyway (HTTP 429):**
- **It waits exactly as long as the API says.** It reads `Retry-After` (seconds or
  a date), OpenRouter's `X-RateLimit-Reset`, Anthropic's
  `anthropic-ratelimit-*-reset`, and reset times inside the error body, then waits
  until then plus a second.
- **If the API gives no reset time,** it waits 15 seconds, then 30, 60…, up to 5
  minutes. A 40-second limit costs two blocked calls at most.
- **The cooldown is shared.** The first agent to hit a 429 records "blocked until
  …" in `_shared/`, and every other agent on the same API, model and key waits it
  out without trying, so a team causes one blocked call, not one each.
- **It's visible:**
  - the log says `rate limited by the API; the API said when (retry-after):
    waiting 41s (until 13:42:10)`;
  - `state.json` says `waiting (rate limited)`;
  - the heartbeat says `waiting out a rate limit (until 13:42:10)`.
  `/stop` and the other commands still work while it waits, and `/model NAME`
  switches straight away, rather than after the wait.

**Commands answer while the model thinks.** The model call runs in a thread,
so `/stop`, `/status` and `/model` work during a slow call. `/model` drops the
call and asks the new model.

**Empty replies and false stops.** Some models stop too early:
- **An empty reply** is asked again, up to twice. It's never stored or shown as
  "(no reply)". After three in a row, the agent waits for a message.
- **A reply that stops mid-step** (it says "Let me check the logs." or "Now I'll
  run the tests:", or breaks off mid-sentence, with no tool call) gets a nudge to
  carry on, up to three times a turn. "Let me know if…" and finished sentences
  don't count.

**The context window is asked for, not hard-coded.** At start-up hobkin asks the API:

- Anthropic: `GET /v1/models/{model}` (`max_input_tokens`, `max_tokens`).
- OpenAI-style: the `/models` list (`context_length` as on OpenRouter,
  `max_model_len` as on vLLM, and similar).
- llama.cpp's `/props` and Ollama's `/api/show`.

`context_tokens` on a model is optional: set it and that number is used as
given; leave it out (or `null`) and it's detected. If the API doesn't say,
set it. Failing that, hobkin
learns the limit from the first "context too long" error and remembers it.
Windows can change (a local server restarted with a different size): the API is
asked again before each compaction, after a "too long" error that doesn't say the
limit, and on `/limits`.
`state.json` records where the number came from.

**Compaction, at good moments.** The window it works within is the model's, capped
at `context_budget` (100,000 tokens). Models that advertise 1M-token windows get slow,
expensive and forgetful long before they fill one; `null` uses the model's full window.
- **Past `compact_early` (half) of the room the conversation has** (the window less
  the instructions), the agent is told once to read narrowly and keep its checklist
  current. It's compacted at the next good moment: right after it ticks a step in
  its `CHECKLIST.md`, or when it finishes a task (that one happens quietly, without
  waking it).
- **Past `compact_at` (80%)** it's compacted wherever it is, as a fallback.
- **A big command output is counted as it arrives,** not after the next request,
  so one huge output can't blow the window before anything notices.
- **What carries it over** is its handoff note plus its current checklist, word
  for word. The note is its own summary; the checklist is the plan, so it picks up
  at the first unchecked step.

When the conversation is compacted, the agent writes
itself a handoff note. The conversation is replaced by that note, with the
newest unanswered message carried over unchanged, and the old conversation goes to
`archive/` (messages only: the system prompt is rebuilt every turn anyway), along
with `out`, which starts afresh. The note tells the agent where the archive is,
so it can grep it.

**Fallback models:** `"fallback_models": ["other", "local"]` (names from
`models`) are tried in order when the model refuses outright: withdrawn,
renamed, a bad key, out of credit. Free models on OpenRouter come and go, so
it's worth having one. Busy and rate-limited models are waited out instead. A
restart goes back to `model`.

Typos and wrong types in `config.json` are reported at startup (in `out` and on
stderr), with suggestions such as "unknown setting 'idel_seconds' (did you mean
'idle_seconds'?)". They're warnings; nothing is refused.

Other settings (shared or per agent): `bash_timeout` (600 s per command by
default; the agent can ask for any longer timeout on a command),
`output_limit` (30,000 characters of command output, head and tail kept),
`max_output_tokens` (unset by default: then no `max_tokens` is sent, since some
APIs refuse it, except to Anthropic-style APIs, which require one and get 16,000
or the model's own limit; set it to send that limit), and
`idle_prompt` / `idle_seconds` (see below).

## Its tools

- **`bash`** runs as your user, starting in `work/`. It behaves like one shell
  that stays open:
  - The first command runs as a login shell (your profile's PATH and so on).
  - After every command, its state is saved to `shell.sh`: the folder,
    variables, exports, functions and aliases. The next command starts from that
    state. So `cd`, `export` and `source .venv/bin/activate` carry over, even
    across restarts.
  - Each command is still its own process, so a hung or crashed command can't
    take the shell's state with it. Shell options like `set -e` don't carry over.
  - `reset: true` starts a fresh login shell in `work/`.
  - Every result ends with the folder it's now in.
  - The environment has `secrets.env` plus `HOBKIN_ID`, `HOBKIN_DIR`,
    `HOBKIN_WORK` and `HOBKIN_MEMORY`.
- **`read_file`** shows a file with line numbers (in pages), or lists a folder.
- **`write_file`** writes a whole file. **`edit_file`** replaces one exact piece
  of text. The text goes in as is, with no shell quoting, which is where smaller
  models most often go wrong.
  - Errors are written to help: "appears 2 times (lines 3, 5)", or the closest
    matching lines when the text isn't found.
  - Line numbers pasted from `read_file` are stripped automatically, and
    Windows line endings are handled.
  - Paths are relative to the shell's folder; `$HOBKIN_WORK`, `$HOBKIN_MEMORY`
    and `~` work.
- **`remind`** sets a reminder to wake itself later, and **`reminders`** lists
  or cancels them (see below).
- **`skill`** opens a skill by name.
- **`goal`** sets the goal for the task it's on: the goal, how it works, why it
  doesn't, what would, and the steps. hobkin writes the task's `CHECKLIST.md`
  from it, shows the thinking in the chat, keeps the goal in the agent's
  instructions (through compaction and restarts), and ticks steps with
  `{"done": [N]}`. File changes (other than `CHECKLIST.md` and `NOTES.md`) aren't
  made until there's a goal (or the agent has said **What would:**), once per
  turn: a second try goes through.
- **`view_image`** shows it an image (PNG, JPEG, GIF, WebP, under 5 MB): a
  screenshot of the UI it built, a chart, a photo. On OpenAI-style APIs the image
  follows the tool result as a user message, because tool messages can't hold
  images.
- **`fetch_url`** reads a web page as text: headings, lists and tables kept,
  scripts and menus' styling dropped, links numbered with a list at the end. Long
  pages come in parts (`offset`, `limit`), JSON is pretty-printed, and
  `save_as` downloads anything else (a PDF, an image) into the workspace.
- **`web_search`** searches DuckDuckGo and returns the top ten results: title,
  URL, snippet.
- **Its own tools:** the agent can make itself tools, for a command it would
  otherwise keep retyping (calls to a synthesizer's API, say). Each is a folder
  `work/tools/NAME/` holding `tool.json` (`{"description": ..., "input_schema":
  {...}}`, a JSON Schema object like the built-ins') and an executable `run`, in
  any language, which gets the call's arguments as one JSON object on stdin and
  prints its result. The model is offered them alongside the built-ins from its
  next reply. `run` runs as a bash command does (the same shell folder,
  environment, sandbox, timeout, output limit and timeline). Ones that can't be
  offered (bad JSON, a built-in's name, `run` not executable) are listed in its
  instructions with the reason, so it can fix them. You can write them for it too.

### The web

Normal sites need no setup. For an internal wiki behind a client certificate
(PEM files), set it in config:

```json
"web": {
  "client_cert": {"cert": "~/certs/me.pem", "key": "~/certs/me.key",
                  "ca": "~/certs/corp-ca.pem", "password_file": "~/certs/key-password"}
}
```

- **One certificate for everything:** it's offered to any site that asks for
  one, including SSO servers along a redirect chain. Most public sites never ask.
  `ca` (your internal CA, trusted alongside the usual ones) and `password_file`
  are optional.
- **SSO logins** that need no typing are followed: redirects, meta refreshes,
  and the auto-submitting forms that SAML and OpenID logins use (up to 8 steps).
  Cookies are kept in the agent's `cookies.txt`, so the next page skips the login.
  A login that wants a password shows up as the login page's text.
- **Pages built by JavaScript** come back nearly empty. Give the agent a skill
  for those (an API, or a headless browser).
- **Search** uses DuckDuckGo's HTML page (`web.search_url`, no key). DuckDuckGo
  sometimes answers servers and VPNs with a "bots use DuckDuckGo too" check
  instead of results; the agent is told so, and searching from a home connection
  usually works. To check a machine:
  `curl -s -A Mozilla/5.0 -d q=test https://html.duckduckgo.com/html/ | grep -c result__a`
  (a number above 0 means results).

### Background jobs

`bash` with `background: true` returns at once ("background job #3 started"),
and when the job ends the agent gets a message with its exit code, how long it
took, and the last of its output. Jobs run detached in the shell's current
state, with their output in `jobs/N/out.log`. They outlive a hobkin restart: a
job that finished while hobkin was stopped is reported when it starts again.
That suits builds, test suites, downloads, and "naps", where a job switches off
a local model, uses the GPU, and starts the model again: nothing calls the model
while the job runs, and its end wakes the agent.

### Announce first

Commands whose effects leave the machine are announced before they run:
- **What counts** (`announce_first` in config, a name and a regex each): force
  pushes, pushes to main, publishing packages, releases, image pushes,
  infrastructure changes, cloud deletions, and sending email or webhook messages.
- **The first try isn't run.** hobkin tells the agent to post what it's about to
  do and why where people will see it, then run the same command again with
  `announced` set.
- **Then it waits** `announce_delay` (2 minutes), long enough for someone to
  object in chat. After that, the same command runs.
- **To stop it,** send `/veto`, which blocks everything waiting. Or just object
  where it posted: the agent is told to check before it runs the command.

Everything else, anything inside the box, needs no announcement.

### Daily budget

`daily_budget_usd` pauses an agent for the rest of the day once it has spent
that much. First it gets one last turn to say so where people will see it.
Spend counts only where the API reports each call's cost (OpenRouter does);
`state.json` keeps `spend_today` and the all-time `spend_usd`.

**Nothing asks for approval:** an agent can do anything your account can. Agents
in one account stay apart by convention (their own folders), not by enforcement.
Use a separate account or container when that matters.

## Working on another machine

```sh
hobkin --host admin@web-1 --id web1                # a new agent working on web-1
hobkin --chore pulse --host admin@web-1            # ...with a chore: any chore works this way
```

With `"host": "user@host"` (or an alias from `~/.ssh/config`), the agent works
there over ssh:
- **What happens there:** commands (the same persistent shell, its folder and
  variables kept between commands), background jobs, the file tools, the watch
  (`wake.command`) and its own tools, all starting in its workspace on the host,
  `~/hobkin-work/ID`. A chore's spells and tools are copied there on first contact
  (never over ones already there).
- **What stays here:** memory, blooper reel, conversation, notes, config. The file
  tools still reach `$HOBKIN_MEMORY` and `$HOBKIN_BLOOPERS` here.
- **The connection:** keys only (no password prompts), one shared connection
  (ControlMaster), a new host's key trusted on first use. Each command's script
  goes over ssh's stdin, with `secrets.env` as exported variables, so secrets never
  appear on a command line there, and they're never saved in its shell state.
  `"ssh"` in config is the command to use (`"ssh -p 2222 -i ~/.ssh/web1"`).
- **When it's down:** the agent still starts, and each command says what ssh said.
  A watch that can't reach the host wakes the agent with that, so a monitor run
  from outside survives the machine dying.
- **Safe mode** can't sandbox another machine (Landlock only reaches this one), so
  with a host it refuses to start unless you say the remote account is the limit:
  `"safe_mode": {"remote_account": true}` (`--safe --host ...` sets that). Then
  give it an account there that can only do what it should.

One host per agent; for a fleet, run an agent per machine (the shared incidents
and watch screen are built for that).

## Safe mode

For a system people depend on: `"safe_mode": true` in an agent's `config.json`
(or `--safe` on the one-line setup). The agent can read what its account can
read, but its commands can only write in its workspace, its memory files and its
job logs, with a private temp folder instead of `/tmp`.

- **It's enforced by the kernel, not by hobkin checking commands.** Each command
  runs through a small helper that locks itself down with
  [Landlock](https://docs.kernel.org/userspace-api/landlock.html) and then runs
  bash. Everything that command starts is locked down too, and it can't be undone
  from inside. `sudo` won't work either, because nothing locked down can gain
  privileges. hobkin's own file tools follow the same rule, and the agent can't
  change its own `config.json` to turn safe mode off.
- **What's covered depends on the kernel,** checked at start-up and written to the
  log, to `state.json` and into the agent's instructions:

  | Linux | Landlock | Blocked |
  |---|---|---|
  | 5.13+ | v1 | writes outside the workspace |
  | 6.7+ | v4 | ...and TCP connections (`{"safe_mode": {"network": true}}` allows them) |
  | 6.12+ | v6 | ...and signals to other processes |

  With no Landlock at all (an older kernel, it's turned off, or a container's
  security profile blocks it), a safe-mode agent won't start. It never runs
  unprotected.
- **Extra folders it may write in:** `{"safe_mode": {"write": ["~/Downloads"]}}`,
  for a chore that tidies a folder, say. Data folders only: the agent won't start
  if one is your home folder or above it, a dot-folder in it (shell profiles,
  `~/.config`, `~/.ssh`: ways out of the sandbox), or in or around hobkin's own
  folder (where its config is), or doesn't exist.
- **Not covered:** reading. Whatever it reads goes to the model provider, so run
  it as an account that can read only what it should. On kernels below 6.12, also
  run it as a separate user, so it can't signal your services' processes. Local
  services reached through socket files (Postgres, Docker) aren't blocked by
  Landlock either; a separate user without access to them covers that.
- **How it works with people:** it investigates by reading. When something needs
  changing, or a command it can't run, it writes a script in
  `work/for-review/` (what it does, why, and for changes, how to undo them) and
  asks you to review and run it. The script saves all its output to
  `work/for-review/logs/`, and gathers plenty while it's there (versions, config,
  the state before and after), so the agent reads the results itself: you run it
  and say "done".

## Break glass

For an incident, when you want the agent working it as one of the hands on deck:

```sh
hobkin breakglass ops-1 "api returning 500s since 14:10" --for 2h [--budget 50|none]
hobkin breakglass ops-1 --off          # end it early
hobkin breakglass ops-1 --for 1h       # nothing said: salvage
hobkin breakglass ops-1 "site down" --chore mender   # with a chore as its playbook
```

**A playbook:** `--chore NAME` (with `--set`/`--secret` as for any chore) gives
the agent that chore and names it in the incident's instructions as what to
follow, e.g. hobkin-chores' `mender` (find the cause and mend it) or `ark` (save
the data). The break-glass rules still hold around it.

**Salvage,** when you don't say what's wrong: the agent assumes the system is
falling over and may be gone soon, and saves what can't be replaced while it's
still up. It copies and doesn't fix. It works out what the system is and where
its data and logs live (in about 10 minutes, not an hour). Then it finds where
copies can go: only places the system already sends things (S3 or other buckets
in its config, backup targets, mounted hosts), using the credentials already
there. It copies the most precious first: data nobody else has (dumped the
database's own way, not live files), then logs, config, and a snapshot of how
things stand, gently (`nice`/`ionice`). It keeps a manifest (what's saved, where,
size and checksum, and what couldn't be saved), deletes nothing of the system's,
and posts a status after each step. It also saves itself: it writes its findings
into `MEMORY.md`, runs `hobkin pack` (below) and uploads the bundle with the
copies, early and again at the end. If the machine is lost, you can start that
bundle anywhere and ask it what it found. Extending it without a reason keeps it
a salvage; giving a reason makes it an ordinary incident.

### Several agents on one incident

Two agents on the same machine can undo each other's work: the fixer restarts
Postgres in the middle of the salvager's dump. Put them on one incident:

```sh
hobkin breakglass ops-1 "api returning 500s" --for 2h
hobkin breakglass ops-2 --for 2h --with ops-1      # ops-2 salvages, on ops-1's incident
```

`--with ID,ID` joins the incident those agents are on, or starts one with them.
An agent with `--with` that isn't in break-glass mode yet is declared with the
same reason (or as a salvage), and one that already is joins as it is. Then:
- **Each knows the others:** its instructions list who else is on the incident,
  what they're doing it for, and where their timelines are. How to message
  each other is in there too (`hobkin send`).
- **Each sees the others' work:** what they've done since its last step (from
  their timelines, without what they were told) comes with its next tool
  results, and with the idle nudges.
- **Holds:** a `hold` tool, offered only on a shared incident: "dumping the
  orders db: don't restart postgres", with a regular expression for the commands
  that would disturb it. A teammate's matching command is stopped once and shown
  the hold. Run again, it goes through, and that's put in both agents' records and
  alerted. Holds live in `_incidents/NAME/holds.json` and are released when the
  agent that set them leaves break-glass mode.

### The watch screen

```sh
hobkin watch ops-1          # everyone on ops-1's incident, in one terminal
hobkin watch                # everyone in break-glass mode (else every agent)
hobkin watch --once         # print it once (also what you get when piped)
```

One terminal, redrawn every second, built from the files hobkin already writes
(config, state, timelines, out logs):
- **A banner per agent, in its own colour:** BREAK GLASS or SALVAGE; NO
  APPROVALS; SAFE MODE LIFTED (or NO SANDBOX); the account it acts as; the
  countdown; the incident's spend and budget; what it's doing now (with how long a
  command has been running).
- **The holds,** then **one feed** merged from their timelines: commands with
  exit codes, file changes, what they said and were told, holds, stops and
  overrides.
- **Flags:**
  - A file-tool write outside the workspace is flagged exactly; hobkin knows
    the path.
  - A bash command's effects can't be seen from here, so its flags are guesses
    from the command line, labelled "looks like": privileged, deletes, service
    change, leaves the machine, writes outside, changes config.
- **Totals and the controls:** how to end it (`--off`) or stop an agent now.

Agents not in break-glass mode show their out log instead of a timeline.
`NO_COLOR` is respected.

### Packing an agent

```sh
hobkin pack ops-1 [--to DIR]     # default: the agent's work/
```

writes `hobkin-ops-1-<time>.tar.gz`. It holds hobkin.py itself, the shared
instructions and skills, and the agent's folder: memory, conversation,
`archive/`, out log, reminders, and its workspace notes (the manifest, the
incident timeline). It leaves out `secrets.env`, API keys, model `headers`,
shell state, and files over 2 MB in its workspace (salvaged copies went
elsewhere). The `RESTORE.md` inside lists what was left out and says how to start it:

```sh
tar xzf hobkin-ops-1-<time>.tar.gz && cd hobkin-ops-1-<time>
HOBKIN_HOME=$PWD/home python3 hobkin.py URL MODEL [KEY] --id ops-1 --no-service --chat
```

The copy starts out of break-glass mode. Its first message tells it it's a
restored copy, which folder its old paths were in, and to answer questions from
what it saved. Agents find the program as `$HOBKIN_SELF`.

Until it expires the agent works fully on its own:
- **No brakes:** safe mode is lifted, and nothing is announced first or waits.
- **Budget:** the incident has its own budget, `--budget`, if you give one. What
  it spends doesn't count against the normal daily budget afterwards, so a costly
  incident doesn't pause the agent once it's over.
- **Its instructions say:** it's an incident. Stabilize first, then find the
  cause. Prefer changes that can be undone, and note how. Say each significant
  action before taking it, and post a status every 10–15 minutes. Still ask
  before destroying data. Keep going until it's stable.
- **When idle,** it's nudged every 2 minutes to check and post a status, instead
  of waiting quietly.
- **A timeline** goes to `work/incident-<time>.md`: every command (with its exit
  code), every file change, everything it says, and everything it's told (people's
  messages, hobkin's notices; questions answered while it was busy are there with
  their answers). hobkin writes it, not the agent, so it's complete for the
  post-mortem.
- **Start, extensions and end** go through `alert_command`. When it ends (time's
  up, or `--off`, even if the agent wasn't running then) it goes back to normal
  (back into the sandbox, if it was in safe mode) and is asked for a summary: what
  happened, what it changed and how to undo it, what's still open. A few lines of
  it go in its `MEMORY.md` too, so a restarted agent can pick the incident back up.

The switch is a command you run, recorded in the agent's own `config.json`. A
safe-mode agent can't write there, so it can't break the glass itself. (An agent
not in safe mode can already do anything your account can.) Running it again
while it's on extends it (once it's run out, it starts a new incident); a running agent picks it up by restarting in place, keeping its
conversation. Every extension is counted and attributed in the config, and
alerted and put in the timeline, including one made by editing `config.json`
directly, which a non-safe-mode agent could do to itself. The agent sees its
deadline: hiding it wouldn't stop it from reading its own config, and knowing
when it's handing back helps it plan the hand-over. hobkin can't grant `sudo`: for a crisis account with more
privileges, run the agent as that user.

## Reminders

An agent can schedule its own wake-ups: `remind` with `in` ("20m", "2h",
"1h30m"), or `at` (local time: "2026-10-17 09:00", "09:00" for the next one,
"tomorrow 09:00"), optionally `every` to repeat. The reply states the exact
time it will fire, because small models are bad at time arithmetic. When it's
due, the agent gets a message: "[Reminder #3, set … for …. It's now …]" and
then its own text.

- **Idle:** it wakes straight away.
- **Mid-task:** nothing is interrupted. The reminder arrives with the result of
  the step in progress (after a long command finishes, for example), like your
  own mid-task messages.
- **Paused:** it waits until `/resume`.
- **While hobkin wasn't running:** it's delivered at the next start, marked
  late. A repeating one is delivered once, saying how many times were missed,
  and moves to its next time.
- **No limits.** Any number of reminders, repeating as often as it likes, even
  every second. A repeating reminder that has fallen behind is delivered once,
  with a count of how many times it was missed, so it can't flood the agent.
  Using `every` where `in` was meant is the classic slip, so the reply points
  that out.
- `reminders.json` is a plain file: read it, edit it, or delete entries. The
  agent's current reminders are also listed in its system prompt.

## Removing everything

```sh
hobkin purge           # asks you to type "purge" first; --yes skips that
```

It stops every agent, removes their services and hobkin's service template, and
deletes `~/.hobkins` (or `$HOBKIN_HOME`): every agent's memory, conversations,
logs, workspace, keys and config. It refuses a folder that holds anything but
agents (so a mistaken `HOBKIN_HOME` can't take your files with it). Your own
`~/.config/hobkin/env` stays, and so does hobkin itself (`pipx uninstall hobkin`).

## Keeping agents running

Reminders and idle prompts only fire while the agent's process runs. An idle
hobkin costs nothing (it sleeps; no API calls), so keep agents up as systemd user
services:

```sh
hobkin service add tickets-1      # start it now, and at every boot
hobkin service remove tickets-1   # stop it and take it off boot
```

`add` does all of this:
- It writes one template unit, `~/.config/systemd/user/hobkin@.service`, shared
  by all agents.
- It points that unit at the hobkin you ran it with (the installed command, or
  `python3 /path/hobkin.py`).
- It enables and starts `hobkin@tickets-1`.
- It turns on linger, so agents keep running when you're logged out. If your
  system won't allow that without a password, it tells you the command to run.

`hobkin list` shows which agents run as services.

- **Stopping and crashes:** `/stop` exits cleanly and stays stopped. A crash is
  restarted after 30 seconds, and systemd gives up after 5 failures in 10
  minutes, so a broken config can't spin.
- **Logs:** the agent's own log stays in its `out` file. The journal
  (`journalctl --user -u hobkin@ID`) gets startup errors, plus a heartbeat every
  `heartbeat_seconds` (60) while the agent is on duty:
  - `hobkin ID active: working` while it works;
  - `hobkin ID active: on duty, next check in 7m (reminder #2)` while it's idle
    with a wake-up scheduled (say, a tracker check with nothing to pick up).
  - It's silent while only a message can wake the agent, or when it's paused, so
    a quiet journal means it's waiting for you. There's no heartbeat in a
    terminal; set `heartbeat_seconds` to 0 to turn it off.
- **Alerts:** when an agent pauses on its own (a model error, the daily budget)
  or falls back to another model, it may not be able to say so itself. Set
  `alert_command` to a shell command and it runs with the news in
  `$HOBKIN_ALERT` and the agent's `secrets.env` in its environment, e.g.
  `curl -s -H "Authorization: Bearer $TICKETS_TOKEN" --data-urlencode "text=$HOBKIN_ALERT" https://tickets.example/api/comment`.
  When `alert_command` is set, the agent also gets a **`notify`** tool, so it can
  reach you through the same channel for something that needs a person (a monitor
  found trouble, a chore needs a decision). It's rate-limited (`"notify":
  {"min_gap": 20, "per_hour": 30}`) so it can't flood, and it works in safe mode
  (the command is hobkin's, not the agent's sandboxed shell).
- **Keys:** a service doesn't see your shell's environment. `key_file` works
  as is; for `key_env`, put `NAME=value` lines in `~/.config/hobkin/env`
  (chmod 600). `add` warns when that's needed.
- **Another `HOBKIN_HOME`** gets its own template (`hobkin-<hash>@.service`),
  so homes never clash.

## Waking up when idle

Hobkins don't nap. After `idle_seconds` (60) of quiet, an idle agent gets
`idle_prompt`, labelled `[from hobkin: idle prompt]`. The default is "You're
idle. Unacceptable! Grab work from your work sources, or hunt for something
useful to do." It stays quiet while the agent has a reminder set or a background
job running. Set `idle_prompt` to `null` to let it rest until a message or
reminder arrives. Each nudge is a model call, so pair a short `idle_seconds`
with a `requests_per_minute` you're happy with.

**Or let a script keep watch.** With `"wake": {"command": "spells/check.sh",
"every": "5m"}`, hobkin runs that command while the agent is idle (in its
workspace, sandboxed in safe mode, with `secrets.env` and the chore's settings
in its environment) and wakes the agent only when it prints something, or
fails. Silence means all's well, so watching costs no model calls. The agent is
told it has a watch, and that the script is its own to improve. (`wake.url` is
the same idea for a work system: see "As a hearthboard worker".)

## Chores

A chore is a standing job you give a hobkin and leave it to: keep a folder
tidy, watch a system's health, and so on. The collection lives in
[hobkin-chores](https://github.com/frank-lynam/hobkin-chores); point hobkin at
it and give a new or existing agent one:

```sh
hobkin --chore sweep --set folder=~/Downloads --id tidier --safe
hobkin URL MODEL KEY --chore pulse --chat              # a new agent on a new model
hobkin --chore ark --set dest=s3://backups/web-1 --secret aws_secret_access_key=...   # a secret setting
```

A chore is found by path, by name in `_shared/chores/` and the folders in
`"chore_dirs"` (in config), or built in (`hearthboard`). An agent can hold several;
each gets its own instructions file. A chore is a folder:

| File | What it is |
|---|---|
| `chore.json` | `name`, `description`, `settings` (each with an `about` and maybe a `default`; one without a default must be given with `--set`), and `config`: settings merged into the agent's `config.json` (a `wake` watch, `safe_mode` write folders, an idle prompt...). Optional: `fetch` (files to download into the agent's folder at setup), `given` (the message that tells the agent), `apprentice: false` (leave out the spells rule below). |
| `CHORE.md` | Its instructions, copied into the agent's folder as `CHORE-<name>.md`, so they're part of its instructions every turn. |
| `tools/` | Tools for it (see "Its tools"), copied to `work/tools/`. |
| `spells/` | Starter scripts, copied to `work/spells/`. |

`{{setting}}` in `CHORE.md`, the config and tool descriptions is filled in, and
each setting reaches commands, tools and the watch as an environment variable:
its `env` name if it has one, else `CHORE_<NAME>` (`folder` → `$CHORE_FOLDER`).

**Secret settings** (`"secret": true`) are given with `--secret NAME=VALUE`, or
asked for at a terminal; `--set` refuses them. The value goes only into the
agent's `secrets.env` (readable only by you), and wherever `{{it}}` appears it
becomes `${ITS_ENV_NAME}`: hobkin fills those in headers itself, and commands
see the variable, so the value never sits in config or in front of the model.

Setting up records the chore (its settings, and its secrets by name only) in the
agent's `config.json` under `"chores"` and tells the agent it has a new job.
Running it again with other settings updates it.

Every chore agent also gets the same rule of thumb: be **the sorcerer's
apprentice, only wiser**. Write spells (scripts in `work/spells/`) instead of
doing the chore by hand, and make one its watch so the chore mostly runs itself.
Give every spell a counter-spell: a dry run, a limit, a clean stop. Never let
the brooms multiply (no cron jobs, daemons or loops of its own). Keep a
`SPELLBOOK.md`. Fix the spell, not just the mess, and put the flood in the
blooper reel.

## The hobkin

The default AGENTS.md makes a hobkin a relentlessly, gleefully cheerful,
work-greedy, cheeky little house spirit: a new bug report is a present to
unwrap, a fixed bug is a holiday, and a nasty problem gets "ooh, now THIS is a
good one!"
- **Forgiveness over permission.** It acts, then says what it did.
- **Good enough beats better.** It ships the working version and moves on.
- **The opposite of a yes-man.** It laughs at bad ideas, its own included, and
  does the better thing.
- **Hot takes.** It rates each task out of five stars.
- **Never fakes success.**

Small models skim long instructions, so hobkin also nudges at the moment each
behaviour matters:
- **A message from you** carries a reminder to give a hot take first (a hello
  or a question just gets answered).
- **A clear slip** (a file-tool error, a command that wasn't found) gets a
  nudge towards the blooper reel.
- **Writing that loses its voice** (a reply, or a `.md`/`.txt` file, heavy with
  committee words like "robust", "comprehensive", "production-ready") gets "that
  reads like a committee wrote it", once per turn.
- **A file read only partly** ends with how many lines are still unread, so the
  agent doesn't fill the rest in from imagination.
- **A reply that stops mid-step** ("Let me check the logs." with no tool call)
  gets "carry on", up to three times a turn (see "Empty replies and false stops").
- **When a working turn ends,** there's one wrap-up check: did you report where
  the people are, any bloopers, more work to grab? It's sent once per turn, so
  it can't loop.

**Busy or self-managing agents get no chatter.** If the agent has set up its own
next wake-up (a reminder, or a background job still running), the hot take, the
wrap-up check and the idle nag stay quiet; it's clearly managing itself. A
message that arrives mid-task gets no hot-take reminder either. The corrective
nudges (echo, slips, committee voice, unread lines) still apply, since they cost
nothing extra.

All of it is plain text in hobkin.py and AGENTS.md, so rein it in as you like.

## As a hearthboard worker

[Hearthboard](https://github.com/frank-lynam/hearthboard) (formerly
agent-tickets) is a ticket tracker for agents. `--hearthboard URL TOKEN` (see
the top) sets an agent up for it in one go: it's short for the built-in
`hearthboard` chore (`--chore hearthboard --set url=URL --secret token=TOKEN`),
after checking the token with the board. That chore fetches the board's skill,
keeps the token in `secrets.env` as `HEARTHBOARD_TOKEN` (the URL reaches commands
as `HEARTHBOARD_URL`), and sets the board's wait (`wake`) and chat (`inbox`) in
config. The running agent knows nothing about hearthboard beyond that. By hand,
roughly:

```sh
hobkin                            # note the id, then /stop it (Ctrl-C)
ID=bodkin URL=http://127.0.0.1:43917 TOKEN=the-token-for-this-agent
printf 'AGENT_TICKETS_URL=%s\nAGENT_TICKETS_TOKEN=%s\n' "$URL" "$TOKEN" > ~/.hobkins/$ID/secrets.env
chmod 600 ~/.hobkins/$ID/secrets.env
curl -s -H "Authorization: Bearer $TOKEN" "$URL/api/skill" --create-dirs \
  -o ~/.hobkins/$ID/skills/hearthboard/SKILL.md
echo '{"idle_prompt": "Run the hearthboard wait loop (GET /api/wait) and do what it says.", "idle_seconds": 60}' \
  > ~/.hobkins/$ID/config.json
hobkin $ID
```

Each worker keeps the board's skill in its own `skills/` folder, not
`_shared/`: an agent that isn't a worker shouldn't see the board as its work
system. The skill reads the token from `HEARTHBOARD_TOKEN`, and each worker has
its own in `secrets.env`. The idle prompt keeps it
checking for work, and it can also hold `/api/wait` open itself inside a
command. Give each worker its own token, so claims and comments show which
agent did what.

## Releasing

Releases go to PyPI from GitHub Actions with Trusted Publishing (no token stored):

1. Bump `__version__` in `hobkin.py`, commit and push.
2. Publish a GitHub release tagged `vX.Y.Z` (matching `__version__`). The
   workflow runs the tests on Python 3.9, 3.12 and 3.14, builds, checks the tag
   matches, and uploads to PyPI.

Running the workflow by hand (Actions → Publish → Run workflow) uploads to
TestPyPI instead, for a dry run.

## Measuring behaviour

The tests check that hobkin works; `tests/eval_hobkin.py` checks how well an agent
behaves on it, so changes to AGENTS.md or the nudges can be measured, not
eyeballed. It runs a real model (it costs calls; free models are fine) through
six scenarios with a tiny work system (a queue, a team chat, a skill) and scores
what it does:

| Scenario | What it checks |
|---|---|
| `queue` | does the job, ticks it off, reports in the team chat |
| `badidea` | rates your bad idea, pushes back, keeps sane filenames |
| `memory` | does work its stale memory says isn't there, and fixes that memory |
| `hello` | answers a hello, with no rating, no busywork, no talking through echo |
| `reading` | finds an answer far beyond the first page of a file |
| `slow` | doesn't block on a command longer than the timeout |

Most scenarios also check that it didn't ask what to do and kept its workspace
tidy.

```sh
python3 tests/eval_hobkin.py --model nvidia/nemotron-3.5-super-vl:free \
    --key-file ~/openrouter.key --runs 3            # --only queue,hello for some
```

It prints a line per run and a score per check across runs (e.g. `queue 2/3
reported in the team chat`). Run it before and after a change.

## Tests

```sh
python3 tests/test_hobkin.py
```

These start real hobkin processes against `tests/fake_api.py`: a local
stand-in for both APIs that rejects malformed conversations. So the tests cost
nothing and don't need a key. They cover both API styles:

- commands, secrets, and hiding the model key from the shell;
- live and queued messages, the lock, `/interrupt`, messages arriving
  mid-command;
- stopping and resuming, and recovery after a crash mid-command;
- compaction, learning the window from an error, and the newest request
  surviving compaction;
- the shell's saved state (variables, arrays, functions, aliases, PATH, folder,
  login profile, `reset`), with secrets never saved;
- the file tools (exact text with quotes, `$` and backslashes; repeated and
  missing matches; pasted line numbers; paging) and the skill tool;
- skills (listing, which copy wins, front matter);
- `requests_per_minute` shared across two agents;
- reminders (firing while idle, arriving mid-command with that command's result,
  validation, list and cancel, the prompt section, late and repeating after a
  restart, a broken `reminders.json`);
- chat: names, colour, the folder it's opened in, the spinner, typing while
  replies land, picking a hobkin when none is named;
- model calls: `max_tokens` only where it's set or required, 429s, fallbacks,
  commands (and `/model`) mid-call and mid-wait, empty replies and false stops;
- break glass: alerts, extensions, `--off` and time's-up endings, salvage, the
  incident budget, shared incidents and holds, the watch screen;
- working on a host (commands, the shell's state, file tools, jobs, own tools,
  memory staying here, a host that's down, safe mode's rule), using a stand-in
  ssh (`tests/fake_ssh.py`: another "home", none of our environment);
- chores (settings, secret settings, several per agent, a break-glass playbook,
  setting up, the watch staying quiet until there's work),
  safe mode's write folders, and the rate-limited `notify` tool;
- the agent's own tools, `hobkin pack` and restoring a bundle, `hobkin purge`,
  safe mode (where the kernel has Landlock) and systemd services (where there's
  a user session).

The fake API plays real-world trouble on cue: a word in a message makes it
rate-limit (`ratelimit:SECONDS:MODE`, or `ratelimit@PORT:…` for one server),
reply with nothing (`empty-reply`, `always-empty`), stop mid-step
(`falsestop:`), think slowly (`slow:N`) or hiccup (`hiccup`). A message like
`run: CMD` or `call: TOOL {json}` makes it call a tool.
