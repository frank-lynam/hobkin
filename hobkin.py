#!/usr/bin/env python3
"""hobkin: one unattended agent per process, driven through plain files.

    hobkin.py                 start a new agent (prints its id)
    hobkin.py ID              resume agent ID
    hobkin.py send ID TEXT    message an agent (queued if it isn't running)
    hobkin.py list            list agents

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

import errno
import fcntl
import json
import os
import re
import secrets
import select
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
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
            "context_tokens": None,   # optional: set to override; null = ask the API
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
    "max_output_tokens": 16000,
}

DEFAULT_AGENTS_MD = """# How you work

You are an unattended agent running under hobkin. Nobody is watching live: work
through your task on your own and keep going until it's done.

- **Your tool is `bash`.** Each call is a fresh shell started in your workspace
  ($HOBKIN_WORK), so `cd` doesn't carry over: use `cd dir && ...` or absolute paths.
  Commands time out (see the tool description); run long jobs in the background
  (`nohup ... > log 2>&1 &`) and check on them.
- **Workspace:** keep everything in $HOBKIN_WORK, one folder per task. Other agents
  may share this machine; stay out of their folders.
- **Secrets** are environment variables in your shell. Use them by name (`$NAME`);
  never print or log their values.
- **Memory:** $HOBKIN_DIR/MEMORY.md is part of these instructions on every turn and
  survives when your conversation is compacted. Keep it short and current: what
  you're doing, key facts, where things are. Long conversations get summarised, so
  anything important belongs in MEMORY.md or in files.
- **Messages** from your operator arrive as user messages; ones that arrive while
  you're mid-task are marked "[message from operator]". Follow them.
- **When there's nothing left to do,** reply with a one-line status and stop calling
  tools. You'll be woken by the next message.
"""

BASH_TOOL = {
    "name": "bash",
    "description": "Run a bash command in your workspace and get its output (stdout and stderr) and exit code.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to run."},
            "timeout": {"type": "integer", "description": "Seconds before it's killed (default and maximum: the configured limit)."},
        },
        "required": ["command"],
    },
}

COMMANDS = ("/stop", "/pause", "/resume", "/compact", "/status", "/interrupt")
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
    cfg = DEFAULT_CONFIG
    for path in (os.path.join(SHARED, "config.json"), os.path.join(agent_dir, "config.json")):
        if os.path.exists(path):
            try:
                cfg = merged(cfg, json.loads(read(path)))
            except json.JSONDecodeError as e:
                sys.exit(f"hobkin: {path}: {e}")
    return cfg


def load_env_file(path):
    env = {}
    for line in read(path).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return env


def ensure_shared():
    os.makedirs(SHARED, exist_ok=True)
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
    def call(self, system, messages, max_tokens, tools=(BASH_TOOL,), tool_choice=None):
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
        name = self.cfg["model"]
        if name not in self.cfg["models"]:
            sys.exit(f"hobkin: model {name!r} isn't in config.json 'models'")
        self.model = Model(name, self.cfg["models"][name])
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
        parts = [f"You are hobkin agent {self.id}. Your folder is {self.dir}; your workspace is {self.work}."]
        for n in order:
            own = self.p(n)
            text = read(own) if os.path.exists(own) else read(os.path.join(SHARED, n))
            if text.strip():
                parts.append(text.strip())
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
        elif cmd == "/status":
            self.log("hobkin", json.dumps({k: self.state.get(k) for k in
                                           ("status", "model", "context_tokens", "last_input_tokens", "compactions")}))

    # --- tools
    def tool_env(self):
        env = dict(os.environ)
        env.pop(self.model.cfg.get("key_env") or "", None)   # the model key isn't the agent's to use
        env.update(load_env_file(os.path.join(SHARED, "secrets.env")))
        env.update(load_env_file(self.p("secrets.env")))
        env.update({"HOBKIN_ID": self.id, "HOBKIN_DIR": self.dir, "HOBKIN_WORK": self.work})
        return env

    def run_bash(self, command, timeout):
        limit = int(self.cfg["bash_timeout"])
        timeout = max(1, min(int(timeout or limit), limit))
        self.log("$", command)
        self.interrupt = False
        with tempfile.TemporaryFile() as outf:
            proc = subprocess.Popen(["bash", "-c", command], cwd=self.work, env=self.tool_env(),
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
        status = reason or f"exit code {proc.returncode}"
        shown = out if len(out) < 2000 else out[:1000] + "\n...\n" + out[-800:]
        self.log("=", f"({status})\n{shown}" if shown.strip() else f"({status})")
        return f"{out}\n[{status}]".strip(), bool(reason) or proc.returncode != 0

    # --- the model
    def ask(self, messages, tool_choice=None):
        """Call the model, retrying transient failures. Raises ApiError for real ones."""
        delay = 2
        while True:
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
        return bool(ctx) and used > float(self.cfg["compact_at"]) * (ctx - self.max_output())

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
            self.log("hobkin", f"model error, pausing until you send a message or /resume:\n{e}")
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
            elif b.get("name") != "bash":
                content, err = f"Unknown tool {b.get('name')!r}; the only tool is bash.", True
            else:
                inp = b.get("input") or {}
                content, err = self.run_bash(str(inp.get("command", "")), inp.get("timeout"))
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": content, "is_error": err})
        # Operator messages that came in meanwhile ride along with the results
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
                if self.pending:
                    self.add_user("\n\n".join(self.pending))
                    self.pending = []
                if self.working:
                    self.save_state("working")
                    self.step()
                    idle_since = time.time()
                    self.poll_input(0)
                    continue
                self.save_state("idle")
                prompt, every = self.cfg.get("idle_prompt"), float(self.cfg.get("idle_seconds") or 300)
                self.poll_input(max(0.0, idle_since + every - time.time()) if prompt else 3600)
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
    if argv[:1] == ["list"]:
        return cmd_list()
    if argv[:1] == ["send"]:
        if len(argv) < 3:
            sys.exit("usage: hobkin.py send ID TEXT")
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


if __name__ == "__main__":
    main(sys.argv[1:])
