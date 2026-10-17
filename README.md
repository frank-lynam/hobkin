# hobkin

A small unattended agent: one process per agent, run through plain files. A
hobkin is a little hob, the house spirit that does the chores while the house
sleeps.

- **One file, standard library only.** `hobkin.py`, Python 3.12+. Nothing to install.
- **No ports.** It listens on nothing; it only makes outgoing calls to the model API.
- **Files are the interface.** Talk to it with `echo`, watch it with `tail -f`,
  and see exactly what the model sees with `cat`.
- **Many per account.** Each agent has its own id and folder, and a lock stops two
  processes running the same one.

## Install

```sh
pipx install hobkin        # or: uv tool install hobkin
```

That gives you the `hobkin` command. Python 3.9 or newer on Linux (or another
POSIX system); no dependencies. You can also skip installing and run the file
directly: `python3 hobkin.py`.

## Quick start

```sh
hobkin                 # a new agent: prints its id, e.g. 7f3a91c2
hobkin 7f3a91c2        # resume that agent later (its context is kept)
```

The first run creates `~/.hobkins/_shared/config.json`. Set your model there (see
**Models**) and make sure the API key is reachable.

Then, from another terminal, chat with it:

```sh
hobkin chat 7f3a91c2
```

Its output appears as it comes, and each line you type (or paste) is sent;
`/stop` and the other commands work, and Ctrl-D leaves. Piped, it sends the
input, prints the reply and exits: `echo "status?" | hobkin chat 7f3a91c2`.

Or with plain files:

```sh
echo "Clone https://example.com/repo and get its tests passing" > ~/.hobkins/7f3a91c2/in
tail -f ~/.hobkins/7f3a91c2/out
```

Run it in the background however you like: `nohup hobkin 7f3a91c2 &`,
tmux, or a systemd user service.

## An agent's folder

`~/.hobkins/ID/` (or `$HOBKIN_HOME/ID/`):

| File | What it is |
|---|---|
| `in` | A named pipe. `echo text > in` sends a message. A write waits until the agent is running; `hobkin send ID text` queues it instead. |
| `out` | Everything it says and does, with timestamps: messages, commands, output. `tail -f out` |
| `context.jsonl` | Exactly what the model is sent: line 1 is the system prompt, then one message per line. Rewritten after every change. |
| `MEMORY.md` | The agent's own notes. Part of its instructions on every turn, and kept when the conversation is compacted. |
| `state.json` | Model, context window (and where that number came from), status, pid, compaction count. |
| `work/` | Its working directory. |
| `shell.sh` | Its shell's saved state (folder, variables, functions, aliases), restored before each command. Secrets are never saved here. |
| `secrets.env` | Optional `KEY=value` lines, set as environment variables in its shell (never put in its context). |
| `config.json` | Optional per-agent settings over the shared ones. |
| `*.md` | Optional per-agent instructions; replace shared files of the same name. |
| `archive/` | Old contexts, kept when it compacts. |
| `queue`, `lock` | Messages sent while it was stopped; the process lock. |

`~/.hobkins/_shared/` holds what all agents share: `config.json`, `AGENTS.md` (how
to work as a hobkin: a task routine with checklists, failure handling,
organization, memory and tidying up; created on first run, edit freely. Once
edited it's never touched; an untouched copy of an older default is upgraded), an optional `SOUL.md`,
any other `.md` instructions, and `secrets.env`.

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
~/.hobkins/_shared/skills/agent-tickets/SKILL.md
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

Anything you write to `in` is a message. If it's busy running a command, the
message is added to the result of that command, marked "[message from
operator]". A line that is just one of these is a command instead:

| Command | Effect |
|---|---|
| `/stop` | Finish cleanly and exit (Ctrl-C and SIGTERM do the same). |
| `/pause`, `/resume` | Stop and restart work; messages wait until `/resume`. |
| `/interrupt` | Kill the command it's running now (it's told it was interrupted). |
| `/compact` | Summarise and clear the conversation now. |
| `/status` | Write its status to `out`. |

`hobkin chat ID` puts both in one terminal (see Quick start). `hobkin list` shows all agents, their status and their last line of output.

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
- `requests_per_minute` (optional): a cap on calls to this model. It's shared by
  every agent using the same API, model and key, because that's what the
  provider counts. The agents coordinate through a small lock file in
  `_shared/` (holding a hash of the key, never the key). An agent at the cap
  waits, and still answers commands such as `/stop` while it does.

**The context window is asked for, not hard-coded.** At start-up hobkin asks the API:

- Anthropic: `GET /v1/models/{model}` (`max_input_tokens`, `max_tokens`).
- OpenAI-style: the `/models` list (`context_length` as on OpenRouter,
  `max_model_len` as on vLLM, and similar).
- llama.cpp's `/props` and Ollama's `/api/show`.

`context_tokens` on a model is optional: set it and that number is used as
given; leave it out (or `null`) and it's detected. If the API doesn't say,
set it. Failing that, hobkin
learns the limit from the first "context too long" error and remembers it.
`state.json` records where the number came from.

When the conversation passes `compact_at` (80%) of the window, the agent writes
itself a handoff note. The conversation is replaced by that note, with the
newest unanswered message carried over unchanged, and the old context goes to
`archive/`.

Other settings (shared or per agent): `bash_timeout` (600 s per command),
`output_limit` (30,000 characters of command output, head and tail kept),
`max_output_tokens` (16,000, capped at what the API reports), and
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
- **`skill`** opens a skill by name.

**Nothing asks for approval:** an agent can do anything your account can. Agents
in one account stay apart by convention (their own folders), not by enforcement.
Use a separate account or container when that matters.

## Waking up when idle

When it has nothing to do, it waits for a message. With `idle_prompt` set, it
also gets that message after `idle_seconds` of quiet. For example,
`"Check for new work."` every 300 seconds.

## As an agent-tickets worker

Nothing in hobkin knows about agent-tickets; you give it the instructions and
the token:

```sh
hobkin                            # note the id, then /stop it (Ctrl-C)
ID=7f3a91c2 URL=http://127.0.0.1:43917 TOKEN=the-token-for-this-agent
printf 'AGENT_TICKETS_URL=%s\nAGENT_TICKETS_TOKEN=%s\n' "$URL" "$TOKEN" > ~/.hobkins/$ID/secrets.env
chmod 600 ~/.hobkins/$ID/secrets.env
curl -s -H "Authorization: Bearer $TOKEN" "$URL/api/skill" --create-dirs \
  -o ~/.hobkins/_shared/skills/agent-tickets/SKILL.md
echo '{"idle_prompt": "Run the agent-tickets wait loop (GET /api/wait) and do what it says.", "idle_seconds": 60}' \
  > ~/.hobkins/$ID/config.json
hobkin $ID
```

The agent-tickets skill is shared: it reads the token from
`AGENT_TICKETS_TOKEN`, so one copy serves every worker, each with its own
`secrets.env`. The idle prompt keeps it
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
- `requests_per_minute` shared across two agents.
