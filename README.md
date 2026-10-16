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

## Quick start

```sh
python3 hobkin.py              # a new agent: prints its id, e.g. 7f3a91c2
python3 hobkin.py 7f3a91c2     # resume that agent later (its context is kept)
```

The first run creates `~/.hobkins/_shared/config.json`. Set your model there (see
**Models**) and make sure the API key is reachable.

Then, from another terminal:

```sh
echo "Clone https://example.com/repo and get its tests passing" > ~/.hobkins/7f3a91c2/in
tail -f ~/.hobkins/7f3a91c2/out
```

Run it in the background however you like: `nohup python3 hobkin.py 7f3a91c2 &`,
tmux, or a systemd user service.

## An agent's folder

`~/.hobkins/ID/` (or `$HOBKIN_HOME/ID/`):

| File | What it is |
|---|---|
| `in` | A named pipe. `echo text > in` sends a message. A write waits until the agent is running; `hobkin.py send ID text` queues it instead. |
| `out` | Everything it says and does, with timestamps: messages, commands, output. `tail -f out` |
| `context.jsonl` | Exactly what the model is sent: line 1 is the system prompt, then one message per line. Rewritten after every change. |
| `MEMORY.md` | The agent's own notes. Part of its instructions on every turn, and kept when the conversation is compacted. |
| `state.json` | Model, context window (and where that number came from), status, pid, compaction count. |
| `work/` | Its working directory. |
| `secrets.env` | Optional `KEY=value` lines, set as environment variables in its shell (never put in its context). |
| `config.json` | Optional per-agent settings over the shared ones. |
| `*.md` | Optional per-agent instructions; replace shared files of the same name. |
| `archive/` | Old contexts, kept when it compacts. |
| `queue`, `lock` | Messages sent while it was stopped; the process lock. |

`~/.hobkins/_shared/` holds what all agents share: `config.json`, `AGENTS.md` (how
to work as a hobkin; created on first run, edit freely), an optional `SOUL.md`,
any other `.md` instructions, and `secrets.env`.

**The system prompt** is `SOUL.md`, then `AGENTS.md`, then the other `.md` files in
name order, then `MEMORY.md`. Each file is taken from the agent's folder if it
has one, otherwise from `_shared/`.

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

`python3 hobkin.py list` shows all agents, their status and their last line of output.

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

- `api`: `anthropic` (the Messages API) or `openai` (Chat Completions: OpenAI,
  OpenRouter, vLLM, llama.cpp, Ollama, ...).
- The key comes from the environment variable `key_env`, or else from the file
  `key_file`. It's removed from the agent's shell environment.
- `headers` (optional): extra HTTP headers.

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

## Its one tool

`bash`, run in `work/` as your user, with `secrets.env` in the environment plus
`HOBKIN_ID`, `HOBKIN_DIR` and `HOBKIN_WORK`. That's enough for git, tests,
`curl` to any API, and editing its own `MEMORY.md`. **Nothing asks for
approval:** an agent can do anything your account can. Agents in one account
stay apart by convention (their own folders), not by enforcement. Use a
separate account or container when that matters.

## Waking up when idle

When it has nothing to do, it waits for a message. With `idle_prompt` set, it
also gets that message after `idle_seconds` of quiet. For example,
`"Check for new work."` every 300 seconds.

## As an agent-tickets worker

Nothing in hobkin knows about agent-tickets; you give it the instructions and
the token:

```sh
python3 hobkin.py                 # note the id, then /stop it (Ctrl-C)
ID=7f3a91c2 URL=http://127.0.0.1:43917 TOKEN=the-token-for-this-agent
printf 'AGENT_TICKETS_URL=%s\nAGENT_TICKETS_TOKEN=%s\n' "$URL" "$TOKEN" > ~/.hobkins/$ID/secrets.env
chmod 600 ~/.hobkins/$ID/secrets.env
curl -s -H "Authorization: Bearer $TOKEN" "$URL/api/skill" > ~/.hobkins/$ID/TICKETS.md
echo '{"idle_prompt": "Run the agent-tickets wait loop (GET /api/wait) and do what it says.", "idle_seconds": 60}' \
  > ~/.hobkins/$ID/config.json
python3 hobkin.py $ID
```

`TICKETS.md` becomes part of its instructions. The idle prompt keeps it
checking for work, and it can also hold `/api/wait` open itself inside a
command. Give each worker its own token, so claims and comments show which
agent did what.

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
  surviving compaction.
