# Working on hobkin

- **One file, standard library only:** `hobkin.py`. No third-party packages, no
  listening ports, no build step. Keep it that way unless it truly outgrows it.
- **Files are the interface** (`in` FIFO, `out` log, `context.jsonl`, `MEMORY.md`,
  `state.json`); see README.md. `context.jsonl` must always be exactly what the model
  is sent: the system prompt on line 1, then the messages.
- **Nothing model-specific is hard-coded:** models come from config.json, and context
  windows are asked of the API (or set in config, or learned from errors). Nothing
  agent-tickets-specific belongs in hobkin.py either; that's instructions and secrets.
- **Messages are stored in Anthropic's shape** (text / tool_use / tool_result blocks)
  and converted for OpenAI-style APIs in `Model`. Every tool call must get a result,
  and roles must alternate: the fake API in the tests rejects anything else.
- **Never put secrets in context:** model keys are removed from the agent's shell
  environment, and `secrets.env` values only ever reach it as environment variables.
- **Tests:** `python3 tests/test_hobkin.py` (real processes against
  `tests/fake_api.py`; no key, no cost). Add a check for every behaviour you change.
- Local git only for now (no remote yet).
