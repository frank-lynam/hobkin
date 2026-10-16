"""End-to-end tests: real hobkin processes against tests/fake_api.py (no real model, no cost).

    python3 tests/test_hobkin.py
"""
import json, os, select, subprocess, sys, time, shutil, signal, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
HOB = os.path.join(HERE, "..", "hobkin.py")
BASE = tempfile.mkdtemp(prefix="hobkin-test-")
LOG = os.path.join(BASE, "fake_api.log")
os.environ["FAKE_API_LOG"] = LOG
procs = []
def mock(port, ctx, listctx="list"):
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_api.py"), str(port), str(ctx), listctx]); procs.append(p); time.sleep(0.5); return p
def home(name, model_cfg, extra=None):
    h = os.path.join(BASE, name); os.makedirs(h + "/_shared", exist_ok=True)
    cfg = {"model": "m", "models": {"m": model_cfg}, "idle_seconds": 300, **(extra or {})}
    json.dump(cfg, open(h + "/_shared/config.json", "w")); return h
def start(h, aid=None):
    env = {**os.environ, "HOBKIN_HOME": h, "TESTKEY": "test-key"}
    p = subprocess.Popen([sys.executable, HOB] + ([aid] if aid else []), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    procs.append(p)
    if not aid: aid = p.stdout.readline().strip()
    d = os.path.join(h, aid)
    for _ in range(50):
        if os.path.exists(d + "/in") and "up:" in open(d + "/out").read() if os.path.exists(d + "/out") else False: break
        time.sleep(0.1)
    return p, aid, d
def say(d, text): 
    with open(d + "/in", "w") as f: f.write(text + "\n")
def wait_for(d, needle, t=15, file="out"):
    end = time.time() + t
    while time.time() < end:
        if needle in open(os.path.join(d, file)).read(): return True
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {needle!r} in {file}:\n" + open(os.path.join(d, "out")).read()[-1500:])
def ctx(d): return [json.loads(l) for l in open(d + "/context.jsonl")]
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {info}")); 
    if not cond: global failed; failed = True
failed = False
try:
    for api, port in (("anthropic", 48701), ("openai", 48702)):
        mock(port, 100000)
        base = f"http://127.0.0.1:{port}" + ("/v1" if api == "openai" else "")
        h = home(api, {"api": api, "base_url": base, "model": "fake", "key_env": "TESTKEY"})
        p, aid, d = start(h)
        st = json.load(open(d + "/state.json"))
        check(f"[{api}] context size from API", st.get("context_tokens") == 100000, st)
        say(d, "run: echo hello > a.txt && cat a.txt && echo FOO=$FOO KEY=${TESTKEY:-unset}")
        wait_for(d, "Done. Output was")
        check(f"[{api}] command ran in workspace", open(d + "/work/a.txt").read() == "hello\n")
        c = ctx(d)
        check(f"[{api}] context file: system + 4 messages", [m["role"] for m in c] == ["system", "user", "assistant", "user", "assistant"], [m["role"] for m in c])
        check(f"[{api}] model key hidden from the shell", "KEY=unset" in open(d + "/out").read())
        # secrets + live send via CLI
        open(d + "/secrets.env", "w").write("FOO=bar\n")
        r = subprocess.run([sys.executable, HOB, "send", aid, "run: echo FOO=$FOO"], env={**os.environ, "HOBKIN_HOME": h}, capture_output=True, text=True)
        check(f"[{api}] send to running agent", "sent to" in r.stdout, r.stdout + r.stderr)
        wait_for(d, "FOO=bar")
        # second process on same id refuses
        r = subprocess.run([sys.executable, HOB, aid], env={**os.environ, "HOBKIN_HOME": h}, capture_output=True, text=True, timeout=10)
        check(f"[{api}] lock prevents double start", "already running" in r.stderr, r.stderr)
        # interrupt a long command
        say(d, "run: sleep 30")
        wait_for(d, "$> sleep 30"); say(d, "/interrupt")
        wait_for(d, "interrupted by the operator")
        # message while a command runs rides along with the result
        say(d, "run: sleep 2; echo slept")
        wait_for(d, "$> sleep 2"); say(d, "also check the wiki")
        wait_for(d, "Done. Output was: slept")
        last_user = [m for m in ctx(d) if m["role"] == "user"][-1]
        check(f"[{api}] mid-command message added after results", any("[message from operator]\nalso check the wiki" in b.get("text", "") for b in last_user["content"]), last_user)
        # stop, queue while stopped, resume
        say(d, "/stop"); p.wait(10)
        check(f"[{api}] /stop exits", p.returncode == 0, p.stderr.read())
        r = subprocess.run([sys.executable, HOB, "send", aid, "hello while you were out"], env={**os.environ, "HOBKIN_HOME": h}, capture_output=True, text=True)
        check(f"[{api}] queued when stopped", "queued" in r.stdout, r.stdout)
        n = len(ctx(d))
        p, _, d = start(h, aid)
        wait_for(d, "echo: hello while you were out")
        check(f"[{api}] resumed with context kept", len(ctx(d)) == n + 2, (n, len(ctx(d))))
        r = subprocess.run([sys.executable, HOB, "list"], env={**os.environ, "HOBKIN_HOME": h}, capture_output=True, text=True)
        check(f"[{api}] list shows it", aid in r.stdout and "idle" in r.stdout, r.stdout)
        # chat, piped: sends, prints the reply, exits
        r = subprocess.run([sys.executable, HOB, "chat", aid], input="hi from a pipe\n", env={**os.environ, "HOBKIN_HOME": h},
                           capture_output=True, text=True, timeout=30)
        check(f"[{api}] chat (piped) prints the reply", "agent> echo: hi from a pipe" in r.stdout and "you>" not in r.stdout, r.stdout + r.stderr)
        # chat, interactive: in a pseudo-terminal
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["HOBKIN_HOME"] = h
            os.execv(sys.executable, [sys.executable, HOB, "chat", aid])
        buf = b""; end = time.time() + 20; sent = False
        while time.time() < end and b"echo: typed in a terminal" not in buf:
            r_, _, _ = select.select([fd], [], [], 0.2)
            if r_:
                try: buf += os.read(fd, 4096)
                except OSError: break
            if not sent and b"chatting with" in buf:
                os.write(fd, b"typed in a terminal\n"); sent = True
        os.write(fd, b"\x04")   # Ctrl-D leaves
        _, status = os.waitpid(pid, 0)
        check(f"[{api}] chat (terminal) round trip and Ctrl-D", b"echo: typed in a terminal" in buf and status == 0, buf[-400:])
        p.send_signal(signal.SIGTERM); p.wait(10)
        # crash repair: context ending in an unanswered tool call
        c = ctx(d); assert c[-1]["role"] == "assistant"; c[-1] = {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_crash", "name": "bash", "input": {"command": "echo never"}}]}
        open(d + "/context.jsonl", "w").write("\n".join(json.dumps(m) for m in c) + "\n")
        p, _, d = start(h, aid)
        wait_for(d, "Done. Output was: Not run: hobkin restarted")
        check(f"[{api}] crash repaired and continued", True)
        p.send_signal(signal.SIGTERM); p.wait(10)
        check(f"[{api}] no invalid requests", "INVALID" not in open(LOG).read())

    # compaction on a tiny context; context size learned from error when the API doesn't list it
    for api, port, listctx in (("anthropic", 48703, "list"), ("openai", 48704, "nolist")):
        mock(port, 3000, listctx)
        base = f"http://127.0.0.1:{port}" + ("/v1" if api == "openai" else "")
        h = home(api + "-small", {"api": api, "base_url": base, "model": "fake", "key_env": "TESTKEY", "max_output_tokens": 200})
        p, aid, d = start(h)
        for i in range(8):
            say(d, f"run: seq 1 {300 + i}")
            wait_for(d, f"{300 + i}", 20); time.sleep(0.3)
        out = open(d + "/out").read()
        st = json.load(open(d + "/state.json"))
        check(f"[{api}] compacted", "compacted; note" in out and os.listdir(d + "/archive"), out[-800:])
        check(f"[{api}] context size known ({st.get('context_source')})", st.get("context_tokens") == 3000, st)
        check(f"[{api}] request survives compaction", "seq 1 307" in open(d + "/context.jsonl").read() or "307" in open(d + "/out").read().split("compacted; note")[-1])
        time.sleep(1); say(d, "still there?"); wait_for(d, "echo: still there?")
        p.send_signal(signal.SIGTERM); p.wait(10)
    check("no invalid requests at all", "INVALID" not in open(LOG).read(), [l for l in open(LOG) if "INVALID" in l][:3])
finally:
    for p in procs:
        if p.poll() is None: p.kill()
    shutil.rmtree(BASE, ignore_errors=True)
print("FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
