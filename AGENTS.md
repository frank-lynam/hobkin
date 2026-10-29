# Working on hobkin

- **Finding your way:** `grep -n '^# ----' hobkin.py` lists its sections (settings, tools,
  nudges, helpers, config, safe mode, web, model api, agent, service, command line), and `grep -n '    # --- '`
  the Agent's parts. Read the section you need with an offset rather than the whole file.
  New code goes in the section it belongs to; keep the map at the top in step.
- **One file, standard library only:** `hobkin.py`. No third-party packages, no
  listening ports, no build step. Keep it that way unless it truly outgrows it.
- **Files are the interface** (`in` FIFO, `out` log, `context.jsonl`, `MEMORY.md`,
  `state.json`); see README.md. `context.jsonl` must always be exactly what the model
  is sent: the system prompt on line 1, then the messages.
- **Nothing model-specific is hard-coded:** models come from config.json, and context
  windows are asked of the API (or set in config, or learned from errors). Nothing
  tracker-specific belongs in the running agent either; that's instructions (skills) and secrets. The one
  exception is setup: `--hearthboard` (`join_hearthboard`) only fetches the board's skill and writes the token
  and idle prompt, so a worker is one command.
- **Messages are stored in Anthropic's shape** (text / tool_use / tool_result blocks)
  and converted for OpenAI-style APIs in `Model`. Every tool call must get a result,
  and roles must alternate: the fake API in the tests rejects anything else.
- **Never put secrets in context:** model keys are removed from the agent's shell
  environment, and `secrets.env` values only ever reach it as environment variables.
- **The web is stdlib too:** `fetch_url` turns HTML into text with `html.parser`
  (`PageText`) and follows SSO steps that need no typing; one client certificate
  (`tls_context`) is offered to any site that asks. Anything needing JavaScript or
  a site-specific login belongs in a skill, not hobkin.py.
- **No auto-updates or migrations.** Never write code that detects an older
  install and changes its files, configs, services or skills, and no legacy-name
  fallbacks. Assume a fresh install in an austere environment. Setup commands on
  a machine that's already set up add to it (the new agent's own folder) and
  never rewrite what's shared; `service add` won't replace another hobkin's
  service template without `--force`.
- **Safe mode must never fail open.** Every command and job an agent runs goes through `sandboxed()`;
  hobkin's own writes on the agent's behalf go through `writable()`. If Landlock isn't there or the
  start-up probe doesn't hold, the agent doesn't start. Nothing in an agent's reach may turn it off
  (its config.json is outside what it can write).
- **Tests:** `python3 tests/test_hobkin.py` (real processes against
  `tests/fake_api.py`; no key, no cost). Add a check for every behaviour you change.
- GitHub: `frank-lynam/hobkin` (remote `origin` uses the `github-hobkin` SSH alias). Commit and push finished work.
