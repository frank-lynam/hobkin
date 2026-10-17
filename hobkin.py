#!/usr/bin/env python3
"""hobkin: one unattended agent per process, driven through plain files.

    hobkin                 start a new agent (prints its id)
    hobkin ID              resume agent ID
    hobkin chat ID         talk to a running agent in this terminal
    hobkin send ID TEXT    message an agent (queued if it isn't running)
    hobkin list            list agents
    hobkin --version

(Or run the file directly: python3 hobkin.py ...)

Each agent lives in ~/.hobkins/ID/ (or $HOBKIN_HOME/ID/):

    in              FIFO: echo "text" > in     (lines like /stop are commands)
    out             what it says and does:     tail -f out
    context.jsonl   exactly what the model sees: the system prompt, then one message per line
    MEMORY.md       the agent's own notes; part of its instructions, kept across compaction
    state.json      model, context size, status, pid
    work/           its working directory

Instructions are the .md files in ~/.hobkins/_shared/ and the agent's folder
(an agent's own file replaces the shared one of the same name). Settings:
_shared/config.json, overridden per agent by ID/config.json. Environment variables
for the agent's shell: _shared/secrets.env and ID/secrets.env (KEY=value lines).

Standard library only. No ports: it only makes outgoing HTTP calls to the model API.
"""

__version__ = "0.1.1"

import errno
import difflib
import fcntl
import hashlib
import json
import os
import re
import secrets
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
from datetime import date, datetime, timedelta
import urllib.request

HOME = os.path.expanduser(os.environ.get("HOBKIN_HOME", "~/.hobkins"))
SHARED = os.path.join(HOME, "_shared")
USER_AGENT = "hobkin/0.1"

DEFAULT_CONFIG = {
    "model": "claude",
    "models": {
        "claude": {
            "api": "anthropic",
            "base_url": "https://api.anthropic.com",
            "model": "claude-sonnet-5-5",
            "key_env": "ANTHROPIC_API_KEY",
            "key_file": "~/.anthropic.key",
            "context_tokens": None,      # optional: set to override; null = ask the API
            "requests_per_minute": None, # optional: cap, shared by every agent using this API, model and key
        },
        "openrouter": {
            "api": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "anthropic/claude-sonnet-5.5",
            "key_file": "~/openrouter.key",
        },
        "local": {
            "api": "openai",
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "local-model",
        },
    },
    # When the agent has nothing to do: wait for a message, or after idle_seconds
    # send idle_prompt (if set), e.g. "Check for new work."
    "idle_prompt": None,
    "idle_seconds": 300,
    "bash_timeout": 600,       # seconds per command
    "output_limit": 30000,     # characters of command output kept (head + tail)
    "compact_at": 0.8,         # compact when the context passes this share of the window
    # Extra folders of skills (each skill a folder with SKILL.md), lowest priority first.
    # Always searched after these: _shared/skills, then the agent's own skills/.
    "skill_dirs": [],
    "max_output_tokens": 16000,
}

DEFAULT_AGENTS_MD = """# How you work

You are an unattended agent running under hobkin. Nobody watches while you work.
Your job: finish each task completely and correctly, keep your workspace
organized, and report clearly when you're done.

## Your tools

- **bash** runs commands. The shell stays as you left it between calls, like a
  terminal: your folder, variables, exports, functions, aliases and an
  activated Python venv all carry over. Every result ends with "now in ..."
  (the folder you're in). If the shell gets into a bad state, such as a broken
  PATH, run your command with `reset: true` to start a fresh shell.
- **read_file** shows a file with line numbers, or lists a folder. Read a file
  before you change it.
- **write_file** creates a file or replaces all of it. **edit_file** replaces
  one exact piece of text in a file. Use these two for every file you create or
  change, never `echo`, heredocs or `sed`: they take your text exactly as
  written, so you never have to escape quotes, `$` or backslashes.
  - For edit_file, copy old_text exactly from read_file's output, without the
    line numbers, and include two or three lines around the change so it
    matches only once. If it says the text isn't found, read the file again;
    don't guess.
  - Read the result each tool shows you, and check it's what you meant.
- **remind** sets a reminder to wake yourself later ("check the build in
  20m", "follow up tomorrow at 09:00"), once or repeating. When it's due you
  get a message with your text, so write everything you'll need: what to
  check, ids, paths. **reminders** lists them or cancels one. To wait for
  something that takes more than a couple of minutes, don't `sleep` in bash:
  set a reminder, note it in your checklist, and end your turn.
- **skill** opens a skill by name (see the Skills list). Always open the skill
  before doing a task it covers, and follow its steps in order. Its files are
  in the folder it tells you; use those full paths.
- Paths in the file tools are relative to the folder bash is in;
  `$HOBKIN_WORK`, `$HOBKIN_MEMORY` and `~` work too.
- Commands are killed after a time limit. Run anything long (installs, builds,
  servers) in the background and check on it:
  `nohup make build > build.log 2>&1 &`, then later `tail -20 build.log`.
- Never run anything that waits for keyboard input (editors, `less`, prompts).
  Use non-interactive flags: `-y`, `git --no-pager`, `PAGER=cat`.
- Read the output before moving on. For long output use `head`, `tail`,
  `grep` or `wc -l` rather than printing everything.

## The routine for every task

1. **Understand it.** Write the goal in one sentence. If something you need is
   missing and you can't find it, ask in your reply and stop. Don't guess.
2. **Make a folder** for the task in your workspace, with a short clear name,
   and go into it: `mkdir -p $HOBKIN_WORK/fix-login && cd $HOBKIN_WORK/fix-login`.
   Everything for the task goes in it. Work from inside it.
3. **Write a checklist** in that folder, `CHECKLIST.md`, before doing the work:
   the goal at the top, then small steps as `- [ ]` lines. Each step should be
   something you can check. End with "verify the result" and "tidy up".
4. **Work through the list in order, one step at a time.** After each step,
   check that it really worked, then mark it `- [x]` in the file. If you find
   more work, add steps. Never mark a step done that you haven't checked.
5. **Verify the whole result** at the end: run the tests, run the program, look
   at the output. "It should work" is not done. "I ran it and it printed 377"
   is done.
6. **Tidy up and report** (see "Finishing a task").

Keep MEMORY.md pointing at your current task, for example:
`Current task: fix-login (work/fix-login/CHECKLIST.md), on step 4 of 7.`
When you come back to work (after a break, a restart or a compacted
conversation), read MEMORY.md and the checklist first, and carry on from the
first unchecked step.

## When something fails

- Read the error message. Fix the cause, not the symptom.
- Don't repeat a failed command unchanged. Change something on every try.
- After three failed attempts at the same step, stop that approach. Write in the
  checklist what you tried and what happened, then try a different way, or
  report the problem.
- Never fake success. Don't skip or delete failing tests, and don't say a step
  worked when it didn't. If something isn't working, say so plainly.

## Keeping organized

- Your workspace holds task folders and nothing else; no loose files. When you
  continue a task, use its existing folder.
- In a task folder: `CHECKLIST.md`, `NOTES.md` for things you find out, and the
  work itself. Give files clear names (not `test2_final_new.py`).
- Stay inside your workspace unless the task says otherwise. Other agents may
  share this machine; leave their folders alone.
- Don't delete anything you didn't create unless you're asked to.
- Secrets are environment variables in your shell. Use them by name (`$NAME`).
  Never print them, and never write them into files or logs.

## Memory: $HOBKIN_MEMORY

- It's shown to you on every turn and is kept when your conversation is
  compacted. Your conversation is not: long ones are summarised and details
  get lost. If you'll need it later, write it down.
- Keep it short (under 40 lines) and true: the current task and step, facts
  you'll need again (paths, ids, decisions, what the operator prefers), and
  lessons learned ("tests need `make deps` first").
- Update it when you start a task, finish an important step, or learn
  something. Delete lines that are no longer true. Write it with write_file
  or edit_file at path `$HOBKIN_MEMORY` (a `MEMORY.md` anywhere else is just a
  file; only this one is remembered).

## Finishing a task

Tidy up:
- Stop background processes you started, unless the task is to leave them running.
- Delete scratch and temporary files you made, and caches such as
  `__pycache__/`. Keep the results, `CHECKLIST.md` and `NOTES.md`.
- Look at what's actually there: `ls -la` the task folder and your workspace.
  Files that belong to the task but ended up elsewhere get moved into its folder.
- Every checklist step is either checked, or has a note saying why not.
- Update MEMORY.md: the task is done, and where the result is.

Then reply with a short report:
- **Done** or **not done** (and what's left).
- **What changed**: files made or changed, with paths. Only list paths you've
  just seen with `ls`.
- **How you checked it.**
- **Anything the operator needs to do or decide.**

Then stop calling tools. The next message will wake you.

## Messages from the operator

- They arrive as user messages. Ones that come while you're mid-task are
  marked "[message from operator]". Read them straight away: they can change
  the plan. If they do, update the checklist.
- When you're unsure what the operator wants and a wrong guess would be costly
  or hard to undo, ask and stop.
"""
TOOLS = [
    {"name": "bash",
     "description": ("Run a bash command. You get its output (stdout and stderr), its exit code and the folder "
                     "you're now in. The shell stays as you left it between calls, like a terminal: the folder, "
                     "variables, exports, functions, aliases and an activated venv all carry over. It starts as a "
                     "login shell. Don't use it to write or edit files: use write_file and edit_file."),
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string", "description": "The command to run."},
         "timeout": {"type": "integer", "description": "Seconds before it's killed (default and maximum: the configured limit)."},
         "reset": {"type": "boolean", "description": "Start a fresh login shell first, in your workspace (use if the shell is in a bad state)."}},
         "required": ["command"]}},
    {"name": "read_file",
     "description": ("Show a file with line numbers (or list a folder). Long files come in pages: use offset "
                     "and limit to read more. Paths are relative to your shell's current folder; $HOBKIN_WORK, "
                     "$HOBKIN_MEMORY and ~ work."),
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "offset": {"type": "integer", "description": "First line to show (1 = the start)."},
         "limit": {"type": "integer", "description": "How many lines to show (default 400)."}},
         "required": ["path"]}},
    {"name": "write_file",
     "description": ("Create a file, or replace everything in it, with exactly the text you give (no escaping "
                     "needed). Folders are created as needed."),
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "content": {"type": "string", "description": "The whole new content of the file."}},
         "required": ["path", "content"]}},
    {"name": "edit_file",
     "description": ("Replace one exact piece of text in a file. old_text must match the file exactly (copy it "
                     "from read_file, without the line numbers) and appear only once: include a few lines around "
                     "the change to make it unique. replace_all changes every match instead."),
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "old_text": {"type": "string", "description": "The exact text to find."},
         "new_text": {"type": "string", "description": "What to put there instead."},
         "replace_all": {"type": "boolean"}},
         "required": ["path", "old_text", "new_text"]}},
    {"name": "remind",
     "description": ("Set a reminder to wake yourself later: when it's due you get a message with its text. Give "
                     "`in` (e.g. \"20m\", \"2h\", \"1d\", \"1h30m\") or `at` (local time: \"2026-10-17 09:00\", "
                     "\"09:00\" for the next 9 o'clock, \"tomorrow 09:00\"), and optionally `every` to repeat (at least "
                     "5m). Use it instead of waiting in bash, then end your turn. The reply says exactly when it will fire."),
     "input_schema": {"type": "object", "properties": {
         "message": {"type": "string", "description": "What to tell yourself: what to check or do, with any ids or paths."},
         "in": {"type": "string"}, "at": {"type": "string"}, "every": {"type": "string"}},
         "required": ["message"]}},
    {"name": "reminders",
     "description": "List your reminders, or cancel one by id.",
     "input_schema": {"type": "object", "properties": {"cancel": {"type": "integer", "description": "Id of a reminder to cancel."}}}},
    {"name": "skill",
     "description": "Open a skill by name: its instructions, its folder and its files. Open the skill before a task it covers.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

# Saved after every command, restored before the next: the shell's folder, variables,
# functions and aliases (see run_bash). Bash's own variables and readonly ones are skipped.
SHELL_SAVE = r"""__hobkin_save() {
  pwd > "$__hobkin_cwd"
  (
    umask 077
    {
      for __f in $(compgen -A function); do
        case $__f in __hobkin*) ;; *) declare -f "$__f" ;; esac
      done
      alias -p
      for __v in $(compgen -v); do
        case " $HOBKIN_NOSAVE " in *" $__v "*) continue ;; esac
        case $__v in
          BASH*|FUNCNAME|PIPESTATUS|RANDOM|SRANDOM|SECONDS|LINENO|SHLVL|PPID|UID|EUID|GROUPS|HOSTNAME|\
          HOSTTYPE|OSTYPE|MACHTYPE|_|PWD|OLDPWD|SHELLOPTS|BASHOPTS|DIRSTACK|EPOCHREALTIME|EPOCHSECONDS|\
          COLUMNS|LINES|HISTCMD|OPTIND|OPTARG|OPTERR|IFS|PS1|PS2|PS4|COMP_*|__*) continue ;;
        esac
        __d=$(declare -p "$__v" 2>/dev/null) || continue
        [[ $__d =~ ^declare\ -[a-zA-Z]*r ]] && continue
        printf '%s\n' "$__d"
      done
    } > "$__hobkin_state.tmp" 2>/dev/null && mv -f "$__hobkin_state.tmp" "$__hobkin_state"
  )
}
trap __hobkin_save EXIT
shopt -s expand_aliases
"""

COMMANDS = ("/stop", "/pause", "/resume", "/compact", "/status", "/interrupt", "/reminders")
REMINDER_LIMIT = 20          # per agent
REMINDER_MIN_EVERY = 300     # seconds between repeats, at least
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def parse_duration(text):
    """'20m', '2h', '1d', '1h30m', '90s', '2 weeks' -> seconds."""
    t = str(text).strip().lower().replace(" ", "")
    parts = re.findall(r"(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|wks?|w)", t)
    if not parts or "".join(n + u for n, u in parts) != t:
        raise ValueError(f"can't read {text!r} as a length of time: use e.g. 30m, 2h, 1d or 1h30m")
    return sum(float(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[u[0]] for n, u in parts)


def parse_at(text, now_t):
    """A local time: 'YYYY-MM-DD HH:MM[:SS]' (or with T), 'HH:MM' (the next one), 'tomorrow HH:MM'."""
    t, tomorrow = str(text).strip(), False
    if t.lower().startswith("tomorrow"):
        t, tomorrow = t[8:].strip(), True
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            if not tomorrow:
                return datetime.strptime(t, fmt).timestamp()
        except ValueError:
            pass
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            clock = datetime.strptime(t, fmt).time()
        except ValueError:
            continue
        day = date.fromtimestamp(now_t) + timedelta(days=1 if tomorrow else 0)
        when = datetime.combine(day, clock).timestamp()
        return when if tomorrow or when > now_t else when + 86400
    raise ValueError(f"can't read {text!r} as a time: use '2026-10-17 09:00', '09:00' or 'tomorrow 09:00'")


def when_text(t):
    return time.strftime("%a %Y-%m-%d %H:%M", time.localtime(t))


def span_text(seconds):
    s, out = int(round(seconds)), []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            out.append(f"{s // size}{unit}")
            s %= size
    return " ".join(out[:2]) or f"{s}s"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default


def write_atomic(path, text):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def merged(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(agent_dir):
    """Settings: built-in defaults, then _shared/config.json, then the agent's config.json.
    Models are different: they come only from the files (the built-in ones are just the starter
    config.json), so deleting a model from your config really removes it. An agent's file can add
    models or change settings on one; those merge over the shared entry."""
    cfg = {k: v for k, v in DEFAULT_CONFIG.items() if k not in ("models", "model")}
    cfg["models"], cfg["model"], files = {}, None, []
    for path in (os.path.join(SHARED, "config.json"), os.path.join(agent_dir, "config.json")):
        if os.path.exists(path):
            try:
                data = json.loads(read(path))
            except json.JSONDecodeError as e:
                sys.exit(f"hobkin: {path}: {e}")
            models = data.pop("models", None) or {}
            cfg = merged(cfg, data)
            for name, m in models.items():
                cfg["models"][name] = merged(cfg["models"].get(name, {}), m)
            files.append(path)
    if not files:
        cfg["models"], cfg["model"] = DEFAULT_CONFIG["models"], DEFAULT_CONFIG["model"]
    cfg["_files"] = files
    return cfg


def load_env_file(path):
    env = {}
    for line in read(path).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return env


def skill_header(path):
    """name and description from a SKILL.md's front matter (--- key: value ---).
    Handles plain, quoted and block (| or >) values."""
    lines = read(path).splitlines()
    meta = {}
    if lines and lines[0].strip() == "---":
        key = None
        for line in lines[1:]:
            if line.strip() == "---":
                break
            m = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", line)
            if m and not line.startswith((" ", "\t")):
                key, val = m.group(1), m.group(2).strip()
                meta[key] = "" if val in ("|", ">", "|-", ">-") else val.strip("'\"")
            elif key and line.strip():
                meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta.get("name"), meta.get("description")


def find_skills(agent_dir, extra_dirs=()):
    """{name: (description, path to SKILL.md)}. Later folders replace earlier ones by name:
    config skill_dirs, then _shared/skills, then the agent's own skills/."""
    skills = {}
    for root in [os.path.expanduser(d) for d in extra_dirs] + [os.path.join(SHARED, "skills"), os.path.join(agent_dir, "skills")]:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            path = os.path.join(root, entry, "SKILL.md")
            if os.path.isfile(path):
                name, desc = skill_header(path)
                skills[name or entry] = (desc or "(no description)", path)
    return skills


def ensure_shared():
    os.makedirs(os.path.join(SHARED, "skills"), exist_ok=True)
    if not os.path.exists(os.path.join(SHARED, "config.json")):
        write_atomic(os.path.join(SHARED, "config.json"), json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    if not os.path.exists(os.path.join(SHARED, "AGENTS.md")):
        write_atomic(os.path.join(SHARED, "AGENTS.md"), DEFAULT_AGENTS_MD)


def is_running(agent_dir):
    """True if some process holds the agent's lock."""
    try:
        fd = os.open(os.path.join(agent_dir, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def http_json(method, url, headers, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json", "user-agent": USER_AGENT, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class ApiError(Exception):
    def __init__(self, status, text, retry_after=None):
        super().__init__(f"HTTP {status}: {text[:500]}")
        self.status, self.text, self.retry_after = status, text, retry_after


# ---------------------------------------------------------------------- model adapters

class Model:
    """One configured model. Messages are kept in Anthropic's shape (content blocks:
    text, tool_use, tool_result) and converted for OpenAI-style APIs."""

    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.api = cfg.get("api", "openai")
        self.base = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        key = os.environ.get(cfg.get("key_env") or "", "") or read(os.path.expanduser(cfg.get("key_file") or "/nonexistent")).strip()
        self.key = key
        self.extra_headers = cfg.get("headers", {})

    def headers(self):
        h = dict(self.extra_headers)
        if self.api == "anthropic":
            h.update({"anthropic-version": "2023-06-01"})
            if self.key:
                h["x-api-key"] = self.key
        elif self.key:
            h["authorization"] = f"Bearer {self.key}"
        return h

    def rate_file(self):
        """Request log shared by every agent that uses this API, model and key (the key only hashed)."""
        tag = hashlib.sha256(f"{self.base}|{self.model}|{self.key}".encode()).hexdigest()[:16]
        return os.path.join(SHARED, f".requests-{tag}")

    def rate_slot(self, rpm):
        """Claim a request slot under the per-minute cap: 0 if claimed, else seconds to wait."""
        path = self.rate_file()
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            now_t = time.time()
            recent = [float(x) for x in f.read().split() if now_t - float(x) < 60]
            if len(recent) >= rpm:
                return max(0.1, min(recent) + 60 - now_t)
            recent.append(now_t)
            f.seek(0)
            f.truncate()
            f.write("\n".join(f"{t:.3f}" for t in recent) + "\n")
            return 0

    def describe(self):
        return f"{self.name} ({self.model} via {self.base})"

    # --- limits: ask the API; None when it won't say
    def query_limits(self):
        """(context_tokens, max_output_tokens, source) from the API, or (None, None, None)."""
        h = self.headers()
        try:
            if self.api == "anthropic":
                info = http_json("GET", f"{self.base}/v1/models/{self.model}", h, timeout=30)
                return info.get("max_input_tokens"), info.get("max_tokens"), "anthropic models API"
            listing = http_json("GET", f"{self.base}/models", h, timeout=30)
            for m in listing.get("data", []):
                if m.get("id") == self.model:
                    top = m.get("top_provider") or {}
                    ctx = (m.get("context_length") or m.get("max_model_len") or m.get("context_window")
                           or m.get("max_context_length") or top.get("context_length"))
                    out = top.get("max_completion_tokens") or m.get("max_completion_tokens")
                    if ctx:
                        return int(ctx), out, "models list"
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            pass
        root = re.sub(r"/v1$", "", self.base)
        try:  # llama.cpp server
            props = http_json("GET", f"{root}/props", h, timeout=10)
            ctx = props.get("n_ctx") or (props.get("default_generation_settings") or {}).get("n_ctx")
            if ctx:
                return int(ctx), None, "llama.cpp /props"
        except (urllib.error.URLError, OSError, ValueError):
            pass
        try:  # Ollama
            show = http_json("POST", f"{root}/api/show", h, {"model": self.model}, timeout=10)
            for k, v in (show.get("model_info") or {}).items():
                if k.endswith(".context_length"):
                    return int(v), None, "ollama /api/show"
        except (urllib.error.URLError, OSError, ValueError):
            pass
        return None, None, None

    # --- one request
    def call(self, system, messages, max_tokens, tools=TOOLS, tool_choice=None):
        """Returns (content_blocks, stop_reason, input_tokens)."""
        if self.api == "anthropic":
            msgs = json.loads(json.dumps(messages))
            if msgs and isinstance(msgs[-1]["content"], list) and msgs[-1]["content"]:
                msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}  # prompt caching
            body = {"model": self.model, "max_tokens": max_tokens, "messages": msgs,
                    "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                    "tools": list(tools)}
            if tool_choice == "none":
                body["tool_choice"] = {"type": "none"}
            r = self._post(f"{self.base}/v1/messages", body)
            u = r.get("usage") or {}
            used = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            return r.get("content") or [], r.get("stop_reason"), used
        body = {"model": self.model, "messages": self._to_openai(system, messages),
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["input_schema"]}} for t in tools]}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tool_choice == "none":
            body["tool_choice"] = "none"
        r = self._post(f"{self.base}/chat/completions", body)
        if "choices" not in r:
            raise ApiError(0, json.dumps(r)[:500])
        choice = r["choices"][0]
        msg = choice.get("message") or {}
        blocks = []
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"command": fn.get("arguments") or ""}
            blocks.append({"type": "tool_use", "id": tc.get("id") or f"call_{secrets.token_hex(6)}",
                           "name": fn.get("name"), "input": args})
        return blocks, choice.get("finish_reason"), (r.get("usage") or {}).get("prompt_tokens") or 0

    def _post(self, url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "user-agent": USER_AGENT, **self.headers()})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            ra = e.headers.get("retry-after") if e.headers else None
            raise ApiError(e.code, text, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None) from None

    @staticmethod
    def _to_openai(system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            if m["role"] == "assistant":
                text = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                         for b in blocks if b["type"] == "tool_use"]
                msg = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:
                for b in blocks:
                    if b["type"] == "tool_result":
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
                text = "\n\n".join(b["text"] for b in blocks if b["type"] == "text")
                if text:
                    out.append({"role": "user", "content": text})
        return out


def context_limit_from_error(text):
    """Pull the model's context size out of a 'too long' error, if it says."""
    for pat in (r"(\d[\d,]*)\s*tokens?\s*>\s*(\d[\d,]*)", r"maximum context length is (\d[\d,]*)",
                r"context (?:length|window)[^0-9]{0,40}(\d[\d,]*)"):
        m = re.search(pat, text, re.I)
        if m:
            return int(m.groups()[-1].replace(",", ""))
    return None


def is_too_long(err):
    return err.status in (400, 413) and re.search(r"too long|context length|context window|maximum context|too many tokens", err.text, re.I)


# ---------------------------------------------------------------------- the agent

class Agent:
    def __init__(self, agent_id):
        self.id = agent_id
        self.dir = os.path.join(HOME, agent_id)
        self.work = os.path.join(self.dir, "work")
        self.p = lambda name: os.path.join(self.dir, name)
        self.stop = False
        self.paused = False
        self.error = False         # paused because the model API refused; a message clears it
        self.working = False
        self.interrupt = False
        self.compact_requested = False
        self.pending = []          # operator messages not yet given to the model
        self.fifo_buf = b""
        self.fifo_last = 0.0

    # --- setup
    def boot(self):
        ensure_shared()
        os.makedirs(self.work, exist_ok=True)
        self.lock_fd = os.open(self.p("lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit(f"hobkin: {self.id} is already running (pid {self.load_state().get('pid', '?')})")
        if not os.path.exists(self.p("in")):
            os.mkfifo(self.p("in"), 0o600)
        # Read end, plus our own write end so the pipe never reports end-of-file when a writer leaves
        self.fifo_r = os.open(self.p("in"), os.O_RDONLY | os.O_NONBLOCK)
        self.fifo_w = os.open(self.p("in"), os.O_WRONLY | os.O_NONBLOCK)
        if not os.path.exists(self.p("MEMORY.md")):
            write_atomic(self.p("MEMORY.md"), "")
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self._on_signal)

        self.cfg = load_config(self.dir)
        name, models = self.cfg.get("model"), self.cfg["models"]
        files = " and ".join(self.cfg["_files"]) or "config.json"
        if not models:
            sys.exit(f"hobkin: no models defined in {files} (add one under \"models\")")
        if not name and len(models) == 1:
            name = next(iter(models))
        if name not in models:
            what = f"\"model\" is {name!r}, which" if name else "\"model\" isn't set, and with more than one model hobkin"
            sys.exit(f"hobkin: {what} isn't one of the models defined in {files}: {', '.join(models)}. "
                     f"Set \"model\" to one of those.")
        self.model = Model(name, self.cfg["models"][name])
        if not self.model.key and (self.model.cfg.get("key_env") or self.model.cfg.get("key_file")):
            where = " or ".join(x for x in (
                f"the environment variable {self.model.cfg['key_env']}" if self.model.cfg.get("key_env") else "",
                f"the file {self.model.cfg['key_file']}" if self.model.cfg.get("key_file") else "") if x)
            others = ", ".join(n for n in self.cfg["models"] if n != name) or "none"
            msg = (f"no API key for model {self.model.describe()}: put it in {where}. "
                   f"To use a different model, set \"model\" in {os.path.join(SHARED, 'config.json')} "
                   f"or {self.p('config.json')} (other models defined: {others}).")
            self.log("hobkin", msg)
            sys.exit(f"hobkin: {msg}")
        self.state = self.load_state()
        self.state.update({"id": self.id, "pid": os.getpid(), "model": self.model.describe()})
        self.state.setdefault("created", now())
        self.messages = self.load_context()
        self.discover_limits()
        self.repair_context()
        self.save_state("starting")
        self.log("hobkin", f"{self.id} up: {self.model.describe()}, context "
                 f"{self.state.get('context_tokens') or 'unknown'} ({self.state.get('context_source') or 'will learn from errors'})")
        self.log("hobkin", f"talk: echo 'hello' > {self.p('in')}    watch: tail -f {self.p('out')}")

    def _on_signal(self, signum, frame):
        if self.stop:  # second signal: leave now
            raise KeyboardInterrupt
        self.stop = True

    def load_state(self):
        try:
            return json.loads(read(os.path.join(HOME, self.id, "state.json"), "{}"))
        except json.JSONDecodeError:
            return {}

    def save_state(self, status=None):
        if status:
            self.state["status"] = status
        self.state["updated"] = now()
        write_atomic(self.p("state.json"), json.dumps(self.state, indent=2) + "\n")

    def discover_limits(self):
        """Context window and output limit: config override, else ask the API (cached per model)."""
        key = f"{self.model.api}|{self.model.base}|{self.model.model}"
        if self.state.get("limits_for") != key:
            for k in ("context_tokens", "context_source", "api_max_output"):
                self.state.pop(k, None)
            self.state["limits_for"] = key
        if self.model.cfg.get("context_tokens"):
            self.state["context_tokens"], self.state["context_source"] = int(self.model.cfg["context_tokens"]), "config"
        elif not self.state.get("context_tokens"):
            ctx, out, src = self.model.query_limits()
            if ctx:
                self.state["context_tokens"], self.state["context_source"] = int(ctx), src
            if out:
                self.state["api_max_output"] = int(out)

    def max_output(self):
        want = int(self.model.cfg.get("max_output_tokens") or self.cfg.get("max_output_tokens") or 16000)
        api = self.state.get("api_max_output")
        return min(want, api) if api else want

    # --- context file: line 1 is the system prompt, then one message per line
    def system_prompt(self):
        names = set()
        for d in (SHARED, self.dir):
            names.update(f for f in os.listdir(d) if f.endswith(".md"))
        order = [n for n in ("SOUL.md", "AGENTS.md") if n in names]
        order += sorted(names - {"SOUL.md", "AGENTS.md", "MEMORY.md"})
        parts = [f"You are hobkin agent {self.id}. Your workspace is {self.work} ($HOBKIN_WORK). "
                 f"Your memory file is {self.p('MEMORY.md')} ($HOBKIN_MEMORY)."]
        for n in order:
            own = self.p(n)
            text = read(own) if os.path.exists(own) else read(os.path.join(SHARED, n))
            if text.strip():
                parts.append(text.strip())
        skills = find_skills(self.dir, self.cfg.get("skill_dirs") or [])
        if skills:
            parts.append("# Skills\n\nA skill is a set of instructions (and sometimes scripts) for one kind of task. "
                         "Before you start a task that a skill below covers, open it with the `skill` tool and follow "
                         "it step by step.\n\n"
                         + "\n".join(f"- **{n}**: {d}" for n, (d, p) in sorted(skills.items())))
        if os.path.exists(self.p("reminders.json")):
            data, ok = self.load_reminders()
            if ok and data["items"]:
                parts.append("# Your reminders\n\n" + "\n".join(self.reminder_lines(data["items"])))
        memory = read(self.p("MEMORY.md")).strip()
        parts.append(f"# Your memory ({self.p('MEMORY.md')})\n\n{memory or '(empty)'}")
        return "\n\n---\n\n".join(parts)

    def load_context(self):
        msgs = []
        for line in read(self.p("context.jsonl")).splitlines():
            if line.strip():
                m = json.loads(line)
                if m.get("role") in ("user", "assistant"):
                    msgs.append(m)
        return msgs

    def save_context(self):
        lines = [json.dumps({"role": "system", "content": self.system_prompt()}, ensure_ascii=False)]
        lines += [json.dumps(m, ensure_ascii=False) for m in self.messages]
        write_atomic(self.p("context.jsonl"), "\n".join(lines) + "\n")

    def repair_context(self):
        """After a crash: answer tool calls that never ran, and pick up where it left off."""
        if not self.messages:
            return
        last = self.messages[-1]
        calls = [b for b in last["content"] if b.get("type") == "tool_use"] if last["role"] == "assistant" else []
        if calls:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b["id"], "content": "Not run: hobkin restarted before this command ran.",
                 "is_error": True} for b in calls]})
        self.working = self.messages[-1]["role"] == "user"
        self.save_context()

    # --- output log
    def log(self, who, text):
        text = text.rstrip()
        if not text:
            return
        lines = text.splitlines()
        entry = f"{now()} {who}> {lines[0]}\n" + "".join(f"    {l}\n" for l in lines[1:])
        with open(self.p("out"), "a", encoding="utf-8") as f:
            f.write(entry)
        if sys.stdout.isatty():
            sys.stdout.write(entry)
            sys.stdout.flush()

    # --- input: the FIFO and the offline queue
    def poll_input(self, timeout):
        """Wait up to `timeout` seconds for input; collect complete messages into self.pending."""
        deadline = time.time() + timeout
        before = (len(self.pending), self.paused, self.compact_requested)
        self._drain_queue()
        while True:
            left = max(0.0, deadline - time.time())
            # A message is whatever arrives in one burst (0.3 s of quiet ends it); the
            # offline queue is checked every second
            wait = min(left, 0.3 if self.fifo_buf else 1.0)
            try:
                ready, _, _ = select.select([self.fifo_r], [], [], wait)
            except InterruptedError:
                ready = []
            if ready:
                try:
                    chunk = os.read(self.fifo_r, 65536)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    self.fifo_buf += chunk
                    self.fifo_last = time.time()
                    continue
            if self.fifo_buf and time.time() - self.fifo_last >= 0.3:
                self._take(self.fifo_buf.decode(errors="replace"))
                self.fifo_buf = b""
            self._drain_queue()
            if self.stop or time.time() >= deadline or (len(self.pending), self.paused, self.compact_requested) != before:
                return

    def _drain_queue(self):
        q = self.p("queue")
        if not os.path.exists(q):
            return
        with open(q, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            lines = f.read().splitlines()
            f.seek(0)
            f.truncate()
        for line in lines:
            if line.strip():
                self._take(json.loads(line))

    def _take(self, text):
        body = []
        for line in text.splitlines():
            cmd = line.strip().split(" ", 1)[0].lower()
            if cmd in COMMANDS and line.strip().lower() == cmd:
                self.command(cmd)
            else:
                body.append(line)
        body = "\n".join(body).strip()
        if body:
            self.log("you", body)
            self.pending.append(body)

    def command(self, cmd):
        self.log("hobkin", f"command {cmd}")
        if cmd == "/stop":
            self.stop = True
        elif cmd == "/pause":
            self.paused = True
            self.save_state("paused")
        elif cmd == "/resume":
            self.paused = self.error = False
            self.save_state("working" if self.working else "idle")
        elif cmd == "/compact":
            self.compact_requested = True   # done between steps, never mid-command
        elif cmd == "/interrupt":
            self.interrupt = True
        elif cmd == "/reminders":
            data, _ = self.load_reminders()
            self.log("hobkin", "\n".join(self.reminder_lines(data["items"])) or "no reminders")
        elif cmd == "/status":
            self.log("hobkin", json.dumps({k: self.state.get(k) for k in
                                           ("status", "model", "context_tokens", "last_input_tokens", "compactions")}))

    # --- tools
    def secret_names(self):
        return list(load_env_file(os.path.join(SHARED, "secrets.env"))) + list(load_env_file(self.p("secrets.env")))

    def tool_env(self):
        env = dict(os.environ)
        env.pop(self.model.cfg.get("key_env") or "", None)   # the model key isn't the agent's to use
        env.update(load_env_file(os.path.join(SHARED, "secrets.env")))
        env.update(load_env_file(self.p("secrets.env")))
        env.update({"HOBKIN_ID": self.id, "HOBKIN_DIR": self.dir, "HOBKIN_WORK": self.work,
                    "HOBKIN_MEMORY": self.p("MEMORY.md")})
        return env

    def cwd(self):
        cwd = self.state.get("cwd") or self.work
        return cwd if os.path.isdir(cwd) else self.work

    def where(self, cwd):
        rel = os.path.relpath(cwd, self.work)
        return "your workspace" if rel == "." else (f"{rel}/ in your workspace" if not rel.startswith("..") else cwd)

    def run_bash(self, command, timeout=None, reset=False):
        """Each command is its own bash process, so a hung or crashed one can't take the shell
        with it. The shell still behaves like one that persists: the first runs as a login shell,
        and every command starts from the state the last one left (shell.sh)."""
        limit = int(self.cfg["bash_timeout"])
        timeout = max(1, min(int(timeout or limit), limit))
        state_file, cwd_file = self.p("shell.sh"), self.p(".cwd")
        if reset:
            for f in (state_file, cwd_file):
                if os.path.exists(f):
                    os.remove(f)
            self.state["cwd"] = self.work
        cwd = self.cwd()
        self.log("$", ("(fresh shell) " if reset else "") + command)
        self.interrupt = False
        env = self.tool_env()
        env["HOBKIN_NOSAVE"] = " ".join(self.secret_names() + ["HOBKIN_ID", "HOBKIN_DIR", "HOBKIN_WORK",
                                                              "HOBKIN_MEMORY", "HOBKIN_NOSAVE"])
        restore = os.path.exists(state_file)
        script = (f"__hobkin_state={shlex.quote(state_file)}\n__hobkin_cwd={shlex.quote(cwd_file)}\n"
                  + (f"source {shlex.quote(state_file)} >/dev/null 2>&1\n" if restore else "")
                  + SHELL_SAVE + command)
        argv = ["bash", "-c", script] if restore else ["bash", "--login", "-c", script]
        with tempfile.TemporaryFile() as outf:
            proc = subprocess.Popen(argv, cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=outf, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            start, reason = time.time(), None
            while proc.poll() is None:
                self.poll_input(0.5)                 # keep reading the FIFO (commands work mid-run)
                if self.stop:
                    reason = "interrupted: hobkin is stopping"
                elif self.interrupt:
                    reason = "interrupted by the operator"
                elif time.time() - start > timeout:
                    reason = f"timed out after {timeout}s"
                if reason:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(5)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    proc.wait()
                    break
            size = outf.seek(0, os.SEEK_END)
            keep = int(self.cfg["output_limit"])
            outf.seek(0)
            if size <= keep:
                out = outf.read().decode(errors="replace")
            else:
                head = outf.read(keep // 2).decode(errors="replace")
                outf.seek(size - keep // 2)
                tail = outf.read().decode(errors="replace")
                out = f"{head}\n... [{size - keep} bytes cut] ...\n{tail}"
        new_cwd = read(cwd_file).strip()
        if new_cwd and os.path.isdir(new_cwd):
            cwd = new_cwd
        self.state["cwd"] = cwd
        status = (reason or f"exit code {proc.returncode}") + f"; now in {self.where(cwd)}"
        shown = out if len(out) < 2000 else out[:1000] + "\n...\n" + out[-800:]
        self.log("=", f"({status})\n{shown}" if shown.strip() else f"({status})")
        return f"{out}\n[{status}]".strip(), bool(reason) or proc.returncode != 0

    def resolve(self, path):
        """A tool path: $HOBKIN_* and ~ expanded, relative to the shell's current folder."""
        names = {"HOBKIN_WORK": self.work, "HOBKIN_DIR": self.dir, "HOBKIN_MEMORY": self.p("MEMORY.md"),
                 "HOME": os.path.expanduser("~")}
        path = re.sub(r"\$\{?(HOBKIN_WORK|HOBKIN_DIR|HOBKIN_MEMORY|HOME)\}?", lambda m: names[m.group(1)], path.strip())
        return os.path.normpath(os.path.join(self.cwd(), os.path.expanduser(path)))

    def read_file(self, path, offset=None, limit=None):
        full = self.resolve(path)
        if os.path.isdir(full):
            entries = sorted(os.listdir(full))
            shown = [e + ("/" if os.path.isdir(os.path.join(full, e)) else "") for e in entries[:500]]
            return f"{full} (folder, {len(entries)} entries):\n" + "\n".join(shown), False
        if not os.path.isfile(full):
            return f"No such file: {full}", True
        data = open(full, "rb").read()
        if b"\0" in data[:8000]:
            return f"{full} is a binary file ({len(data)} bytes); not shown.", True
        lines = data.decode(errors="replace").splitlines()
        start = max(1, int(offset or 1))
        count = max(1, int(limit or 400))
        chunk = lines[start - 1:start - 1 + count]
        body = "\n".join(f"{n:>5}| {l[:2000]}" for n, l in enumerate(chunk, start))
        end = start + len(chunk) - 1
        if not lines:
            return f"{full} is empty.", False
        more = f" (showing lines {start}-{end} of {len(lines)}; use offset={end + 1} for more)" if end < len(lines) or start > 1 else ""
        return f"{full}{more}:\n{body}", False

    def write_file(self, path, content):
        full = self.resolve(path)
        existed = os.path.exists(full)
        if os.path.isdir(full):
            return f"{full} is a folder.", True
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        note = ""
        if "\n" not in content and "\\n" in content and len(content) > 80:
            note = ("\nNote: the file has no line breaks but contains the characters \\n. "
                    "If you meant new lines, write real line breaks in content.")
        return f"{'Replaced' if existed else 'Created'} {full} ({n} lines){note}", False

    def edit_file(self, path, old, new, replace_all=False):
        full = self.resolve(path)
        if not os.path.isfile(full):
            return f"No such file: {full}. Use write_file to create it.", True
        text = open(full, encoding="utf-8", errors="replace").read()
        if not old:
            return "old_text is empty. To write a whole file use write_file.", True
        note = ""
        if old not in text:
            # Common slips: line numbers copied from read_file, or Windows line endings
            stripped = re.sub(r"(?m)^\s*\d+\| ?", "", old)
            if stripped != old and stripped in text:
                old, new, note = stripped, re.sub(r"(?m)^\s*\d+\| ?", "", new), " (line numbers from read_file removed)"
            elif "\r\n" in text and old.replace("\n", "\r\n") in text:
                old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        count = text.count(old)
        if count == 0:
            return "old_text isn't in the file (it must match exactly, including spaces and indentation)." \
                + self.closest(text, old), True
        if count > 1 and not replace_all:
            lines = [text.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(old), text)]
            return (f"old_text appears {count} times (lines {', '.join(map(str, lines[:20]))}). Include more of the "
                    "surrounding lines so it matches once, or set replace_all."), True
        first = text.index(old)
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        # Show the result around the (first) change, so it can be checked
        lines = text.splitlines()
        at = text.count("\n", 0, first)
        lo, hi = max(0, at - 3), min(len(lines), at + new.count("\n") + 4)
        shown = "\n".join(f"{n:>5}| {l}" for n, l in enumerate(lines[lo:hi], lo + 1))
        return f"Edited {full}{' (' + str(count) + ' places)' if replace_all and count > 1 else ''}{note}. Now:\n{shown}", False

    @staticmethod
    def closest(text, old):
        """Where the file has text most like old_text, to show in an error."""
        lines, want = text.splitlines(), old.strip("\n").splitlines()
        if not lines:
            return " The file is empty: use write_file to give it content."
        if not want or len(lines) > 20000:
            return ""
        n, target, best = len(want), "\n".join(want), (0.0, 0)
        for i in range(max(1, len(lines) - n + 1)):
            m = difflib.SequenceMatcher(None, "\n".join(lines[i:i + n]), target)
            if m.real_quick_ratio() > best[0] and m.quick_ratio() > best[0]:
                r = m.ratio()
                if r > best[0]:
                    best = (r, i)
        if best[0] < 0.5:
            return " Nothing similar found: read_file it again and copy the text exactly."
        i = best[1]
        shown = "\n".join(f"{k:>5}| {l}" for k, l in enumerate(lines[i:i + n], i + 1))
        return f" The closest text in the file is lines {i + 1}-{i + n}:\n{shown}"

    def open_skill(self, name):
        skills = find_skills(self.dir, self.cfg.get("skill_dirs") or [])
        key = name.strip().lower()
        match = next((n for n in skills if n.lower() == key), None)
        if not match:
            return f"No skill named {name!r}. Skills: {', '.join(sorted(skills)) or 'none'}.", True
        desc, path = skills[match]
        folder = os.path.dirname(path)
        files = []
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            files += [os.path.relpath(os.path.join(root, f), folder) for f in names if not f.startswith(".")]
        files = sorted(files)
        return (f"Skill: {match}\nFolder: {folder}\nFiles: {', '.join(files[:100])}\n"
                f"Paths in these instructions are relative to that folder: use {folder}/<path>.\n\n{read(path)}"), False

    # --- reminders: reminders.json in the agent's folder (you can read and edit it)
    def warn_once(self, key, text):
        """Log a problem with a hand-edited file once per version of that file, not on every check."""
        if getattr(self, "_warned", {}).get(key) != text:
            self._warned = {**getattr(self, "_warned", {}), key: text}
            self.log("hobkin", text)

    def load_reminders(self):
        try:
            data = json.loads(read(self.p("reminders.json"), "{}") or "{}")
        except json.JSONDecodeError as e:
            self.warn_once("reminders", f"reminders.json isn't valid JSON ({e}); ignoring it until it's fixed")
            return {"next_id": 1, "items": []}, False
        items = []
        for r in data.get("items", []):
            try:
                r["_due"] = datetime.strptime(r["due"], TIME_FMT).timestamp()
                r["_every"] = parse_duration(r["every"]) if r.get("every") else None
                items.append(r)
            except (KeyError, ValueError) as e:
                self.warn_once(f"reminder {r}", f"skipping a reminder in reminders.json I can't read ({e}): {r}")
        return {"next_id": data.get("next_id", 1 + max([r.get("id", 0) for r in items] or [0])), "items": items}, True

    def save_reminders(self, data):
        items = sorted(({k: v for k, v in r.items() if not k.startswith("_")} for r in data["items"]), key=lambda r: r["due"])
        write_atomic(self.p("reminders.json"), json.dumps({"next_id": data["next_id"], "items": items}, indent=2) + "\n")

    def reminder_lines(self, items):
        now_t = time.time()
        return [f"#{r['id']}: {when_text(r['_due'])} (in {span_text(max(0, r['_due'] - now_t))})"
                + (f", repeats every {r['every']}" if r.get("every") else "") + f": {r['message']}"
                for r in sorted(items, key=lambda r: r["_due"])]

    def tool_remind(self, inp):
        message = str(inp.get("message") or "").strip()
        if not message:
            return "A reminder needs a message: what to check or do when it fires.", True
        now_t = time.time()
        data, ok = self.load_reminders()
        if not ok:
            return "reminders.json is broken; ask the operator to fix it.", True
        if len(data["items"]) >= REMINDER_LIMIT:
            return f"You already have {REMINDER_LIMIT} reminders (the most allowed). Cancel some with the reminders tool.", True
        try:
            every = parse_duration(inp["every"]) if inp.get("every") else None
            if every is not None and every < REMINDER_MIN_EVERY:
                return f"Reminders can repeat at most every {REMINDER_MIN_EVERY // 60} minutes.", True
            if inp.get("in") and inp.get("at"):
                return "Give either `in` or `at`, not both.", True
            if inp.get("in"):
                due = now_t + parse_duration(inp["in"])
            elif inp.get("at"):
                due = parse_at(inp["at"], now_t)
            elif every:
                due = now_t + every
            else:
                return "Say when: `in` (e.g. \"20m\") or `at` (e.g. \"tomorrow 09:00\"), and/or `every`.", True
        except ValueError as e:
            return f"{e}. It's now {when_text(now_t)}.", True
        if due < now_t + 10:
            return f"That's {'in the past' if due < now_t else 'too soon'}: it's now {when_text(now_t)}.", True
        rid = data["next_id"]
        data["next_id"] += 1
        data["items"].append({"id": rid, "message": message, "due": time.strftime(TIME_FMT, time.localtime(due)),
                              "every": inp.get("every") or None, "set": time.strftime(TIME_FMT, time.localtime(now_t)),
                              "_due": due})
        self.save_reminders(data)
        return (f"Reminder #{rid} set for {when_text(due)} (in {span_text(due - now_t)})"
                + (f", then every {inp['every']}" if every else "") + f". It's now {when_text(now_t)}."), False

    def tool_reminders(self, inp):
        data, ok = self.load_reminders()
        note = ""
        if inp.get("cancel") is not None:
            rid = int(inp["cancel"])
            kept = [r for r in data["items"] if r["id"] != rid]
            if len(kept) == len(data["items"]):
                return f"There's no reminder #{rid}.\n" + ("\n".join(self.reminder_lines(data["items"])) or "You have no reminders."), True
            data["items"] = kept
            self.save_reminders(data)
            note = f"Cancelled reminder #{rid}.\n"
        lines = self.reminder_lines(data["items"])
        return note + (("Your reminders:\n" + "\n".join(lines)) if lines else "You have no reminders.") \
            + f"\nIt's now {when_text(time.time())}.", False

    def take_due_reminders(self):
        """Messages for reminders that are due; repeating ones move to their next time."""
        if not os.path.exists(self.p("reminders.json")):
            return []
        now_t = time.time()
        data, ok = self.load_reminders()
        due = [r for r in data["items"] if r["_due"] <= now_t] if ok else []
        if not due:
            return []
        out = []
        for r in due:
            late = now_t - r["_due"]
            text = f"[Reminder #{r['id']}, set {r.get('set', '?')[:16]} for {when_text(r['_due'])}"
            text += f"; {span_text(late)} late (hobkin wasn't running then)" if late > 90 else ""
            if r["_every"]:
                missed, nxt = 0, r["_due"]
                while nxt <= now_t:
                    nxt += r["_every"]
                    missed += 1
                r["_due"], r["due"] = nxt, time.strftime(TIME_FMT, time.localtime(nxt))
                text += (f"; {missed - 1} earlier times were missed" if missed > 1 else "") + f"; repeats, next {when_text(nxt)}"
            else:
                data["items"].remove(r)
            text += f". It's now {when_text(now_t)}.]\n{r['message']}"
            self.log("reminder", text)
            out.append(text)
        self.save_reminders(data)
        return out

    def next_reminder_in(self):
        if not os.path.exists(self.p("reminders.json")):
            return None
        data, ok = self.load_reminders()
        return max(0.0, min(r["_due"] for r in data["items"]) - time.time()) if ok and data["items"] else None

    def run_tool(self, name, inp):
        """(result text, is_error) for one tool call."""
        try:
            if name == "bash":
                return self.run_bash(str(inp.get("command", "")), inp.get("timeout"), bool(inp.get("reset")))
            if name == "read_file":
                self.log("read", str(inp.get("path")))
                return self.read_file(str(inp["path"]), inp.get("offset"), inp.get("limit"))
            if name == "write_file":
                result = self.write_file(str(inp["path"]), str(inp["content"]))
                self.log("write", result[0])
                return result
            if name == "edit_file":
                result = self.edit_file(str(inp["path"]), str(inp["old_text"]), str(inp["new_text"]), bool(inp.get("replace_all")))
                self.log("edit", result[0])
                return result
            if name == "remind":
                result = self.tool_remind(inp)
                self.log("remind", result[0])
                return result
            if name == "reminders":
                result = self.tool_reminders(inp)
                self.log("reminders", result[0])
                return result
            if name == "skill":
                self.log("skill", str(inp.get("name")))
                return self.open_skill(str(inp["name"]))
            return f"Unknown tool {name!r}. Tools: {', '.join(t['name'] for t in TOOLS)}.", True
        except KeyError as e:
            return f"Missing argument {e} for {name}.", True
        except (OSError, ValueError, TypeError) as e:
            return f"{name} failed: {e}", True

    # --- the model
    def ask(self, messages, tool_choice=None):
        """Call the model, retrying transient failures. Raises ApiError for real ones."""
        delay = 2
        while True:
            rpm = self.model.cfg.get("requests_per_minute")
            if rpm:
                wait = self.model.rate_slot(int(rpm))
                if wait:
                    self.log("hobkin", f"rate limit ({rpm}/min): waiting {wait:.0f}s")
                    self.save_state("waiting (rate limit)")
                    self.poll_input(wait)
                    if self.stop:
                        raise ApiError(0, "stopping")
                    continue
            try:
                blocks, stop_reason, used = self.model.call(self.system_prompt(), messages, self.max_output(),
                                                            tool_choice=tool_choice)
                if used:
                    self.state["last_input_tokens"] = used
                return blocks, stop_reason
            except ApiError as e:
                if e.status not in RETRY_STATUS:
                    raise
                wait = e.retry_after or delay
                self.log("hobkin", f"model busy ({e.status}); retrying in {int(wait)}s")
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                wait = delay
                self.log("hobkin", f"can't reach the model ({e}); retrying in {int(wait)}s")
            self.poll_input(wait)
            if self.stop:
                raise ApiError(0, "stopping")
            delay = min(delay * 2, 300)

    def needs_compaction(self):
        ctx = self.state.get("context_tokens")
        used = self.state.get("last_input_tokens") or 0
        if not ctx or used <= float(self.cfg["compact_at"]) * (ctx - self.max_output()):
            return False
        if len(self.messages) <= 2:   # nothing left to compact: the instructions themselves are the bulk
            if not self.state.get("warned_small_context"):
                self.log("hobkin", f"warning: the instructions alone use about {used} of {ctx} tokens; "
                         "this model's context window is too small to work well")
                self.state["warned_small_context"] = True
            return False
        return True

    def compact(self, why):
        """Replace the conversation with the agent's own summary of it; archive the old one."""
        if not self.messages:
            return
        self.log("hobkin", f"compacting context ({why})")
        ask = {"role": "user", "content": [{"type": "text", "text": (
            "Your conversation is about to be cleared to free up context. Write a handoff note to yourself: "
            "the task and its status, ids, file paths, decisions made, what you were about to do next, and "
            "anything the operator asked for. Be complete but concise. Reply with the note only.")}]}
        history = json.loads(json.dumps(self.messages))
        if history[-1]["role"] == "user":     # a user turn can't follow a user turn
            history[-1]["content"] = history[-1]["content"] + ask["content"]
        else:
            history.append(ask)
        for attempt in range(4):
            try:
                blocks, _ = self.ask(history, tool_choice="none")
                break
            except ApiError as e:
                if not is_too_long(e) or attempt == 3:
                    self.log("hobkin", f"compaction failed: {e}")
                    return
                # Too long even to summarise: shorten old tool output, then drop the oldest turns
                for m in history[:-2]:
                    for b in m["content"]:
                        if b.get("type") == "tool_result" and len(b["content"]) > 1500:
                            b["content"] = b["content"][:700] + "\n...[cut]...\n" + b["content"][-500:]
                if attempt >= 1:
                    # Drop the oldest third, restarting at a user message; tool results whose
                    # calls were dropped become plain text so the history stays valid
                    start = max(1, len(history) // 3)
                    while start < len(history) - 1 and history[start]["role"] != "user":
                        start += 1
                    if start < len(history) - 1:
                        first = [{"type": "text", "text": "[earliest messages dropped]"}] + [
                            {"type": "text", "text": "[output of an earlier command dropped]"} if b.get("type") == "tool_result" else b
                            for b in history[start]["content"]]
                        history = [{"role": "user", "content": first}] + history[start + 1:]
        note = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        # The newest turn isn't answered yet: carry it over as it is (results shortened)
        carry = []
        if self.messages[-1]["role"] == "user":
            for b in self.messages[-1]["content"]:
                if b.get("type") == "text":
                    carry.append(b["text"])
                elif b.get("type") == "tool_result":
                    out = b["content"] if len(b["content"]) <= 4000 else b["content"][:2000] + "\n...[cut]...\n" + b["content"][-1500:]
                    carry.append(f"[Result of your last command]\n{out}")
        text = f"[Your context was compacted. Your handoff note:]\n\n{note}\n\n" + (
            "\n\n".join(carry) if carry else "[Carry on.]")
        os.makedirs(self.p("archive"), exist_ok=True)
        os.replace(self.p("context.jsonl"), self.p(f"archive/context-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
        self.messages = [{"role": "user", "content": [{"type": "text", "text": text}]}]
        self.state["compactions"] = self.state.get("compactions", 0) + 1
        self.state["last_input_tokens"] = 0
        self.working = True
        self.save_context()
        self.save_state()
        self.log("hobkin", f"compacted; note:\n{note}")

    def step(self):
        """One model call, then run any commands it asked for."""
        if self.compact_requested:
            self.compact_requested = False
            self.compact("requested")
        elif self.needs_compaction():
            self.compact("context nearly full")
        try:
            blocks, stop_reason = self.ask(self.messages)
        except ApiError as e:
            if self.stop:
                return
            if is_too_long(e):
                limit = context_limit_from_error(e.text)
                if limit:
                    self.state.update({"context_tokens": limit, "context_source": "learned from an error"})
                self.compact("model said the context is too long")
                return
            hint = (f"\nThe API refused the key for model {self.model.describe()}: "
                    "check it, and check \"model\" in config.json picks the API you meant.") if e.status in (401, 403) else ""
            self.log("hobkin", f"model error, pausing until you send a message or /resume:\n{e}{hint}")
            self.paused = self.error = True
            self.save_state("error")
            return
        if not blocks:
            blocks = [{"type": "text", "text": "(no reply)"}]
        self.messages.append({"role": "assistant", "content": blocks})
        self.save_context()
        for b in blocks:
            if b.get("type") == "text":
                self.log("agent", b["text"])
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        if stop_reason in ("max_tokens", "length") and not calls:
            self.log("hobkin", "reply hit the output limit")
        if not calls:
            self.working = False
            self.save_state()
            return
        results = []
        for b in calls:
            if self.stop:
                content, err = "Not run: hobkin is stopping.", True
            else:
                content, err = self.run_tool(b.get("name"), b.get("input") or {})
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": content, "is_error": err})
        # Reminders that came due and operator messages that came in meanwhile ride along
        for text in self.take_due_reminders():
            results.append({"type": "text", "text": text})
        for text in self.pending:
            results.append({"type": "text", "text": f"[message from operator]\n{text}"})
        self.pending = []
        self.messages.append({"role": "user", "content": results})
        self.save_context()
        self.save_state()

    def run(self):
        self.boot()
        self.save_context()
        idle_since = time.time()
        try:
            while not self.stop:
                if self.paused:
                    self.save_state("error" if self.error else "paused")
                    waiting = len(self.pending)
                    self.poll_input(3600)
                    if self.error and len(self.pending) > waiting:
                        self.paused = self.error = False      # a message after an error: try again
                    continue
                if self.compact_requested and not self.working:
                    self.compact_requested = False
                    self.compact("requested")
                due = self.take_due_reminders()
                if due or self.pending:
                    self.add_user("\n\n".join(due + self.pending))
                    self.pending = []
                if self.working:
                    self.save_state("working")
                    self.step()
                    idle_since = time.time()
                    self.poll_input(0)
                    continue
                self.save_state("idle")
                prompt, every = self.cfg.get("idle_prompt"), float(self.cfg.get("idle_seconds") or 300)
                wait = max(0.0, idle_since + every - time.time()) if prompt else 3600
                nxt = self.next_reminder_in()
                self.poll_input(min(wait, nxt) if nxt is not None else wait)
                if prompt and not self.pending and not self.paused and time.time() - idle_since >= every:
                    self.log("hobkin", "idle prompt")
                    self.add_user(prompt)
                    idle_since = time.time()
        except KeyboardInterrupt:
            pass
        finally:
            self.save_context()
            self.state["pid"] = None
            self.save_state("stopped")
            self.log("hobkin", "stopped")

    def add_user(self, text):
        block = {"type": "text", "text": text}
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})
        self.working = True
        self.save_context()


# ---------------------------------------------------------------------- command line

def valid_id(agent_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", agent_id or ""):
        sys.exit(f"hobkin: bad id {agent_id!r} (letters, digits, - and _)")
    return agent_id


def cmd_send(agent_id, text):
    d = os.path.join(HOME, valid_id(agent_id))
    if not os.path.isdir(d):
        sys.exit(f"hobkin: no agent {agent_id}")
    if is_running(d):
        try:
            fd = os.open(os.path.join(d, "in"), os.O_WRONLY | os.O_NONBLOCK)
            os.set_blocking(fd, True)
            with os.fdopen(fd, "wb") as f:
                f.write(text.encode() + b"\n")
            print(f"sent to {agent_id}")
            return
        except OSError as e:
            if e.errno not in (errno.ENXIO, errno.ENOENT):
                raise
    with open(os.path.join(d, "queue"), "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(text) + "\n")
    print(f"{agent_id} isn't running; queued for when it starts")


def cmd_chat(agent_id):
    """Talk to a running agent in one terminal: its output appears as it comes, and each line
    you type (or paste) is sent. Piped input: send it all, print the reply, exit."""
    d = os.path.join(HOME, valid_id(agent_id))
    if not os.path.isdir(d) or not is_running(d):
        sys.exit(f"hobkin: {agent_id} isn't running (start it with: hobkin {agent_id})")
    fifo = os.open(os.path.join(d, "in"), os.O_WRONLY | os.O_NONBLOCK)
    os.set_blocking(fifo, True)
    out_path, state_path = os.path.join(d, "out"), os.path.join(d, "state.json")
    tty = sys.stdin.isatty()
    with open(out_path, "rb") as f:
        recent = f.read().decode(errors="replace").splitlines()[-12:]
        pos = f.tell()
    mine = False                     # inside one of our own "you>" entries (not echoed back)

    def show_new():
        nonlocal pos, mine
        with open(out_path, "rb") as f:
            f.seek(pos)
            chunk = f.read()
        cut = chunk.rfind(b"\n") + 1   # whole lines only
        pos += cut
        for line in chunk[:cut].decode(errors="replace").splitlines():
            if not line.startswith("    "):
                mine = line[20:].startswith("you> ")
            if not mine:
                print(line[11:] if not line.startswith("    ") else line, flush=True)  # drop the date

    if tty:
        print("\n".join(l[11:] if not l.startswith("    ") else l for l in recent))
        print(f"-- chatting with {agent_id}: type and press Enter to send; /stop, /pause, /interrupt work; Ctrl-D to leave --", flush=True)
    sent_at, eof = 0.0, False
    try:
        while True:
            ready = select.select([sys.stdin] if not eof else [], [], [], 0.3)[0]
            if ready:
                line = sys.stdin.readline()
                if line:
                    os.write(fifo, line.encode())
                    sent_at = time.time()
                else:
                    eof = True
                    if tty:
                        break
            show_new()
            if eof and not tty:
                # Piped: leave once the agent has gone idle again after our message
                time.sleep(0.5)
                st = json.loads(read(state_path, "{}") or "{}")
                if time.time() - sent_at > 2 and st.get("status") in ("idle", "paused", "error", "stopped"):
                    show_new()
                    break
                if not is_running(d):
                    show_new()
                    break
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fifo)


def cmd_list():
    if not os.path.isdir(HOME):
        return print("no agents yet")
    for name in sorted(os.listdir(HOME)):
        d = os.path.join(HOME, name)
        if name.startswith("_") or not os.path.isdir(d):
            continue
        try:
            st = json.loads(read(os.path.join(d, "state.json"), "{}"))
        except json.JSONDecodeError:
            st = {}
        status = (st.get("status") or "?") if is_running(d) else "stopped"
        last = [l for l in read(os.path.join(d, "out")).splitlines() if l and not l.startswith("    ")][-1:]
        print(f"{name:10} {status:8} {st.get('model', '')[:40]:40} {last[0][:70] if last else ''}")


def main(argv):
    if argv[:1] in (["-h"], ["--help"], ["help"]):
        print(__doc__)
        return
    if argv[:1] in (["-V"], ["--version"]):
        print(f"hobkin {__version__}")
        return
    if argv[:1] == ["list"]:
        return cmd_list()
    if argv[:1] == ["chat"]:
        if len(argv) != 2:
            sys.exit("usage: hobkin chat ID")
        return cmd_chat(argv[1])
    if argv[:1] == ["send"]:
        if len(argv) < 3:
            sys.exit("usage: hobkin send ID TEXT")
        return cmd_send(argv[1], " ".join(argv[2:]))
    if len(argv) > 1:
        sys.exit(__doc__)
    if argv:
        agent_id = valid_id(argv[0])
    else:
        os.makedirs(HOME, exist_ok=True)
        agent_id = secrets.token_hex(4)
        while os.path.exists(os.path.join(HOME, agent_id)):
            agent_id = secrets.token_hex(4)
        print(agent_id, flush=True)
    os.makedirs(os.path.join(HOME, agent_id), exist_ok=True)
    Agent(agent_id).run()


def cli():
    """Entry point for the installed `hobkin` command."""
    main(sys.argv[1:])


if __name__ == "__main__":
    cli()
