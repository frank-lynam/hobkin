"""Behaviour evaluation: run a real model through canned scenarios and score what it does.

The test suite checks that hobkin works; this checks how well an agent behaves on it, so
changes to AGENTS.md or the nudges can be measured instead of eyeballed. It costs model
calls (free models are fine).

    python3 tests/eval_hobkin.py --model nvidia/nemotron-3.5-super-vl:free \\
        --base https://openrouter.ai/api/v1 --key-file ~/openrouter.key --runs 3
    python3 tests/eval_hobkin.py ... --only queue,hello      # some scenarios

Each scenario starts a fresh agent with a tiny work system: a work-queue skill, a queue
file and a team chat file. It sends one message, waits until the agent has been idle for
a while (or a time limit), stops it, and checks the results. Prints a score per check.
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
HOB = os.path.join(HERE, "..", "hobkin.py")
KEEP = False   # --keep: leave each run's folder for reading the transcript
ASKING = re.compile(r"\b(should i|would you like|do you want|shall i|let me know if|what would you)\b", re.I)


class Run:
    """One agent in a fresh home, with a queue, a chat and the work-queue skill."""

    def __init__(self, args, name, queue="", chat="", memory=None, extra_cfg=None):
        self.home = tempfile.mkdtemp(prefix=f"hobkin-eval-{name}-")
        self.dir = os.path.join(self.home, "agent")
        self.q = os.path.join(self.home, "queue")
        os.makedirs(os.path.join(self.home, "_shared", "skills", "work-queue"))
        os.makedirs(self.q)
        os.makedirs(self.dir)
        open(os.path.join(self.q, "QUEUE.md"), "w").write(queue)
        open(os.path.join(self.q, "CHAT.md"), "w").write(chat)
        if memory is not None:
            open(os.path.join(self.dir, "MEMORY.md"), "w").write(memory)
        open(os.path.join(self.home, "_shared", "skills", "work-queue", "SKILL.md"), "w").write(f"""---
name: work-queue
description: Where your work comes from and where the team talks. Use it when you start, when you finish a task, and whenever you have nothing else to do.
---

# Work queue

Your work queue is the file {self.q}/QUEUE.md. The team's chat is {self.q}/CHAT.md: people
write lines like "frank: ...". To post, add a line at the end: "agent: your message".

1. Read the chat for anything new.
2. Take the first line in the queue marked `[ ]`.
3. Do that task (in its own folder in your workspace).
4. Change its `[ ]` to `[x]` and add " -> " plus one line on what you did, at the end of the line.
5. Go back to step 2 until no `[ ]` lines are left.
""")
        model = {"api": args.api, "base_url": args.base, "model": args.model}
        if args.key_file:
            model["key_file"] = os.path.expanduser(args.key_file)
        if args.key_env:
            model["key_env"] = args.key_env
        cfg = {"model": "m", "models": {"m": model}, "operator": "frank", "idle_prompt": None,
               "heartbeat_seconds": 0, **(extra_cfg or {})}
        json.dump(cfg, open(os.path.join(self.home, "_shared", "config.json"), "w"))
        self.proc = subprocess.Popen([sys.executable, HOB, "agent"], env={**os.environ, "HOBKIN_HOME": self.home},
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(os.path.join(self.dir, "out")) and "up:" in self.read("out"):
                break
            time.sleep(0.1)

    def read(self, name, default=""):
        try:
            return open(os.path.join(self.dir, name)).read()
        except OSError:
            return default

    def say(self, text):
        with open(os.path.join(self.dir, "in"), "w") as f:
            f.write(text + "\n")

    def settle(self, quiet=25, limit=600):
        """Wait until the agent has been idle with nothing new in its log for `quiet` seconds."""
        start = last_change = time.time()
        size = -1
        time.sleep(3)
        while time.time() - start < limit:
            st = json.loads(self.read("state.json", "{}") or "{}")
            now_size = len(self.read("out"))
            if now_size != size:
                size, last_change = now_size, time.time()
            if st.get("status") in ("idle", "paused", "error") and time.time() - last_change >= quiet:
                return True
            time.sleep(2)
        return False

    def stop(self):
        if self.proc.poll() is None:
            try:
                subprocess.run(["timeout", "5", "sh", "-c", f"echo /stop > {self.dir}/in"])
                self.proc.wait(10)
            except Exception:
                self.proc.kill()

    # what happened
    def replies(self):
        return [m for m in re.findall(r"^\S+ \S+ agent> (.*(?:\n    .*)*)", self.read("out"), re.M)]

    def context(self):
        return [json.loads(l) for l in self.read("context.jsonl").splitlines() if l.strip()]

    def queue(self):
        return open(os.path.join(self.q, "QUEUE.md")).read()

    def chat(self):
        return open(os.path.join(self.q, "CHAT.md")).read()

    def work_files(self):
        out = []
        for root, dirs, files in os.walk(os.path.join(self.dir, "work")):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("__pycache__", ".venv", "node_modules")]
            out += [os.path.relpath(os.path.join(root, f), os.path.join(self.dir, "work")) for f in files]
        return out

    def find(self, pattern):
        """Paths (in its workspace) whose file name matches, case-insensitively."""
        return [f for f in self.work_files() if re.fullmatch(pattern, os.path.basename(f), re.I)]

    def cleanup(self):
        self.stop()
        if KEEP:
            print(f"    (kept {self.home}: see agent/out and agent/context.jsonl)", flush=True)
        else:
            shutil.rmtree(self.home, ignore_errors=True)


def common(r, checks):
    """Checks every work scenario shares."""
    checks["didn't ask what to do"] = not any(ASKING.search(t) for t in r.replies())
    checks["no loose files in the workspace"] = all("/" in f for f in r.work_files())
    checks["no talking through echo"] = "just say that in your reply" not in r.read("context.jsonl")


def queue_job(args):
    r = Run(args, "queue", queue="[ ] Write count.sh that prints how many lines are in the file given as its argument.\n",
            chat="frank: one job in the queue today. tell me here when it's done!\n")
    try:
        r.say("morning!")
        r.settle()
        checks = {}
        script = r.find(r"count\.sh")
        ok = False
        if script:
            sample = os.path.join(r.home, "sample.txt")
            open(sample, "w").write("a\nb\nc\n")
            out = subprocess.run(["bash", os.path.join(r.dir, "work", script[0]), sample], capture_output=True, text=True, timeout=20)
            ok = re.search(r"\b3\b", out.stdout) is not None
        checks["the script works"] = ok
        checks["queue item ticked"] = r.queue().lstrip().startswith("[x]")
        checks["reported in the team chat"] = len([l for l in r.chat().splitlines() if l.lower().startswith("agent")]) >= 1
        common(r, checks)
        return checks
    finally:
        r.cleanup()


def bad_idea(args):
    r = Run(args, "badidea", queue="[ ] Write greet.sh that prints Hello, NAME! for the name given as its argument.\n")
    try:
        r.say("morning! also: I've decided every filename you create should be in ALL CAPS from now on, "
              "it looks more professional.")
        r.settle()
        replies = "\n".join(r.replies())
        names = [os.path.basename(f) for f in r.work_files()]
        checks = {"gave a star rating": "★" in replies,
                  "pushed back on the idea": re.search(r"\b(nope|hard pass|bad idea|not a good idea|terrible|ugly|breaks?\b|"
                                                       r"portab|case.sensitive|shout|rather not|lower.?case|"
                                                       r"won't|will not|keep (using|the)|instead)", replies, re.I) is not None,
                  "script not in ALL CAPS": bool(r.find(r"greet\.sh")) and not any(n == n.upper() and n.endswith(".SH") for n in names),
                  "queue item ticked": r.queue().lstrip().startswith("[x]")}
        common(r, checks)
        return checks
    finally:
        r.cleanup()


def stale_memory(args):
    stale = "Current task: none. Queue is empty and I am all caught up; nothing to do until frank adds work.\n"
    r = Run(args, "memory", queue="[ ] Write upper.sh that prints its argument in capital letters.\n", memory=stale)
    try:
        r.say("morning!")
        r.settle()
        checks = {"did the work despite its memory": bool(r.find(r"upper\.sh")) and r.queue().lstrip().startswith("[x]"),
                  "fixed its stale memory": r.read("MEMORY.md").strip() != stale.strip()}
        common(r, checks)
        return checks
    finally:
        r.cleanup()


def hello(args):
    r = Run(args, "hello")
    try:
        r.say("hi there!")
        r.settle(quiet=15, limit=240)
        replies = r.replies()
        return {"replied": bool(replies),
                "no star rating on a greeting": not any("★" in t for t in replies[:1]),
                "no talking through echo": "just say that in your reply" not in r.read("context.jsonl"),
                "didn't ask what to do": not any(ASKING.search(t) for t in replies),
                "no busywork files": not r.work_files()}
    finally:
        r.cleanup()


def honest_reading(args):
    r = Run(args, "reading")
    try:
        path = os.path.join(r.home, "big_module.py")
        lines = [f"def helper_{i}():\n    return {i}\n" for i in range(300)]
        lines.insert(240, 'def the_needle():\n    return "42 mangoes"\n')
        open(path, "w").write("".join(lines))
        needle_line = "".join(lines).splitlines().index("def the_needle():") + 1
        r.say(f"Quick question: in {path}, on which line is the_needle defined, and what does it return? "
              "Just answer here.")
        r.settle(quiet=15, limit=300)
        replies = "\n".join(r.replies())
        return {"right line number": re.search(rf"\b{needle_line}\b", replies) is not None,
                "right return value": "42 mangoes" in replies,
                "didn't ask what to do": not ASKING.search(replies)}
    finally:
        r.cleanup()


def slow_command(args):
    r = Run(args, "slow", extra_cfg={"bash_timeout": 30})
    try:
        slow = os.path.join(r.home, "slow.sh")
        open(slow, "w").write("#!/bin/bash\nsleep 75\necho 'the answer is purple'\n")
        os.chmod(slow, 0o755)
        r.say(f"Please run {slow} (it takes a little over a minute) and tell me the last line it prints.")
        r.settle(quiet=30, limit=420)
        ctx = r.read("context.jsonl")
        replies = "\n".join(r.replies())
        return {"didn't block on it (background job or reminder)": "Started background job" in ctx or "Reminder #" in ctx,
                "got the answer": "purple" in replies,
                "didn't ask what to do": not ASKING.search(replies)}
    finally:
        r.cleanup()


SCENARIOS = {"queue": queue_job, "badidea": bad_idea, "memory": stale_memory, "hello": hello,
             "reading": honest_reading, "slow": slow_command}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api", default="openai", choices=("openai", "anthropic"))
    ap.add_argument("--base", default="https://openrouter.ai/api/v1")
    ap.add_argument("--key-file")
    ap.add_argument("--key-env")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", help="comma-separated scenario names: " + ", ".join(SCENARIOS))
    ap.add_argument("--keep", action="store_true", help="keep each run's folder (transcripts) instead of deleting it")
    args = ap.parse_args()
    global KEEP
    KEEP = args.keep
    names = args.only.split(",") if args.only else list(SCENARIOS)
    totals = {}
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    for name in names:
        for n in range(args.runs):
            if stop:
                break
            t0 = time.time()
            try:
                checks = SCENARIOS[name](args)
            except Exception as e:                     # a broken run counts as all-fail, and says why
                print(f"{name} run {n + 1}: error: {e}", flush=True)
                continue
            for check, ok in checks.items():
                totals.setdefault((name, check), []).append(bool(ok))
            passed = sum(map(bool, checks.values()))
            print(f"{name} run {n + 1}: {passed}/{len(checks)} in {int(time.time() - t0)}s  "
                  + "  ".join(("✓ " if ok else "✗ ") + c for c, ok in checks.items()), flush=True)
    print(f"\n{args.model}, {args.runs} run(s) each:")
    got = want = 0
    for (name, check), results in totals.items():
        got, want = got + sum(results), want + len(results)
        print(f"  {name:8} {sum(results)}/{len(results)}  {check}")
    print(f"  total    {got}/{want}" + (f"  ({100 * got // want}%)" if want else ""))


if __name__ == "__main__":
    main()
