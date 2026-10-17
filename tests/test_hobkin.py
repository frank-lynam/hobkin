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
        # the shell keeps its folder between calls (and says where it is)
        say(d, "run: mkdir -p sub/deeper && cd sub/deeper")
        wait_for(d, "now in sub/deeper/ in your workspace")
        say(d, "run: pwd && echo $HOBKIN_MEMORY")
        wait_for(d, "    " + d + "/work/sub/deeper\n    " + d + "/MEMORY.md")
        check(f"[{api}] cd carries over; HOBKIN_MEMORY set", True)
        say(d, "run: cd .. && rmdir deeper")
        wait_for(d, "now in sub/ in your workspace")
        say(d, "run: cd $HOBKIN_WORK && echo back-home"); wait_for(d, "Done. Output was: back-home")
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
        mock(port, 8000, listctx)
        base = f"http://127.0.0.1:{port}" + ("/v1" if api == "openai" else "")
        h = home(api + "-small", {"api": api, "base_url": base, "model": "fake", "key_env": "TESTKEY", "max_output_tokens": 200})
        p, aid, d = start(h)
        for i in range(8):
            say(d, f"run: seq 1 {1000 + i}")
            wait_for(d, f"{1000 + i}", 20); time.sleep(0.3)
        out = open(d + "/out").read()
        st = json.load(open(d + "/state.json"))
        check(f"[{api}] compacted", "compacted; note" in out and os.listdir(d + "/archive"), out[-800:])
        check(f"[{api}] context size known ({st.get('context_source')})", st.get("context_tokens") == 8000, st)
        check(f"[{api}] request survives compaction", "seq 1 1007" in open(d + "/context.jsonl").read() or "1007" in open(d + "/out").read().split("compacted; note")[-1])
        time.sleep(1); say(d, "still there?"); wait_for(d, "echo: still there?")
        p.send_signal(signal.SIGTERM); p.wait(10)
    # skills: shared, overridden per agent, extra dirs from config; listed (not inlined) in the prompt
    extra = os.path.join(BASE, "extra-skills")
    h = home("skills", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"skill_dirs": [extra]})
    def skill(root, folder, header, body="SECRET-BODY-TEXT"):
        os.makedirs(os.path.join(root, folder), exist_ok=True)
        open(os.path.join(root, folder, "SKILL.md"), "w").write(f"---\n{header}\n---\n\n{body}\n")
    skill(h + "/_shared/skills", "greet", "name: greet\ndescription: Say hello properly.")
    skill(h + "/_shared/skills", "deploy", "name: deploy\ndescription: >\n  Ship a release:\n  build, tag and push.")
    skill(extra, "greet", "name: greet\ndescription: lower priority, should lose")
    skill(extra, "notes", 'name: "notes"\ndescription: "Keep tidy notes."')
    skill(h + "/_shared/skills", "nofront", "", body="no front matter here")
    p, aid, d = start(h)
    skill(d + "/skills", "greet", "name: greet\ndescription: The agent's own greeting rules.")
    say(d, "run: cat " + d + "/skills/greet/SKILL.md")
    wait_for(d, "Done. Output was")
    system = ctx(d)[0]["content"]
    check("[skills] listed with descriptions", "**deploy**: Ship a release: build, tag and push." in system
          and "**notes**: Keep tidy notes." in system, system[-900:])
    check("[skills] agent's own skill wins, then shared over extra", "The agent's own greeting rules." in system
          and "lower priority" not in system, system[-900:])
    check("[skills] bodies aren't inlined", "SECRET-BODY-TEXT" not in system)
    check("[skills] folder name used when there's no front matter", "**nofront**: (no description)" in system, system[-900:])
    check("[skills] the agent can read one", "SECRET-BODY-TEXT" in open(d + "/out").read())
    p.send_signal(signal.SIGTERM); p.wait(10)
    # shell state carries over; secrets never saved; reset; login profile on a fresh shell
    fake_home = os.path.join(BASE, "fakehome"); os.makedirs(fake_home)
    open(os.path.join(fake_home, ".profile"), "w").write("export LOGIN_MARK=from-profile\n")
    h = home("shell", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"})
    os.environ["HOME"], real_home = fake_home, os.environ["HOME"]
    try:
        p, aid, d = start(h)
    finally:
        os.environ["HOME"] = real_home
    open(d + "/secrets.env", "w").write("SECRET_TOKEN=hunter2\n")
    def run(cmd, marker):
        say(d, "run: " + cmd); wait_for(d, "Done. Output was: " + marker)
    run("echo login=$LOGIN_MARK", "login=from-profile")
    run("export A=1; B='two words'; arr=(x y); greet() { echo hi $1; }; alias ll='echo listed'; mkdir -p v/bin; "
        "printf '#!/bin/sh\\necho venv-python\\n' > v/bin/py; chmod +x v/bin/py; export PATH=$PWD/v/bin:$PATH; cd v",
        "[exit code 0; now in v/")
    say(d, "run: echo A=$A B=$B arr=${arr[1]}; greet bob; ll; py; echo token=$SECRET_TOKEN")
    wait_for(d, "    A=1 B=two words arr=y\n    hi bob\n    listed\n    venv-python\n    token=hunter2\n")
    saved = open(d + "/shell.sh").read()
    check("[shell] state carries over (vars, arrays, functions, aliases, PATH, folder)", True)
    check("[shell] secrets and HOBKIN_* never saved in shell.sh", "hunter2" not in saved and "HOBKIN_WORK" not in saved and "SECRET_TOKEN" not in saved, saved[:300])
    run("exit 3", "[exit code 3")
    run("echo still A=$A", "still A=1")
    say(d, 'call: bash {"command": "echo fresh A=$A login=$LOGIN_MARK; pwd", "reset": true}')
    wait_for(d, "Done. Output was: fresh A= login=")
    after = open(d + "/out").read().split("(fresh shell)")[-1]
    check("[shell] reset starts fresh in the workspace", "=> (exit code 0; now in your workspace)\n    fresh A= login=from-profile" in after, after[:400])
    p.send_signal(signal.SIGTERM); p.wait(10)

    # file tools: exact text in and out, helpful edit errors, paging; the skill tool
    h = home("files", {"api": "openai", "base_url": "http://127.0.0.1:48702/v1", "model": "fake", "key_env": "TESTKEY"})
    os.makedirs(h + "/_shared/skills/release/scripts")
    open(h + "/_shared/skills/release/SKILL.md", "w").write("---\nname: release\ndescription: Ship it.\n---\n\nRun scripts/go.sh\n")
    open(h + "/_shared/skills/release/scripts/go.sh", "w").write("echo go\n")
    p, aid, d = start(h)
    tricky = 'say "hi" & echo $HOME \\n `x` \'q\'\n\ttab\nline3\nline4\nline3\n'
    def call(tool, args, marker):
        say(d, f"call: {tool} " + json.dumps(args)); wait_for(d, marker, file="context.jsonl")
    call("write_file", {"path": "t/a.txt", "content": tricky}, "Created ")
    check("[files] write_file keeps text exactly", open(d + "/work/t/a.txt").read() == tricky)
    call("read_file", {"path": "t/a.txt", "offset": 2, "limit": 2}, "showing lines 2-3 of 5")
    call("edit_file", {"path": "t/a.txt", "old_text": "line3", "new_text": "L3"}, "appears 2 times (lines 3, 5)")
    call("edit_file", {"path": "t/a.txt", "old_text": "    3| line3\n    4| line4", "new_text": "LINE3\nline4"}, "line numbers from read_file removed")
    check("[files] edit with pasted line numbers still works", open(d + "/work/t/a.txt").read().endswith("LINE3\nline4\nline3\n"))
    call("edit_file", {"path": "t/a.txt", "old_text": "lime4", "new_text": "x"}, "The closest text in the file is lines 4-4")
    call("edit_file", {"path": "$HOBKIN_MEMORY", "old_text": "nothing", "new_text": "x"}, "The file is empty")
    call("write_file", {"path": "$HOBKIN_MEMORY", "content": "Current task: test\n"}, "Replaced ")
    check("[files] $HOBKIN_MEMORY path works", open(d + "/MEMORY.md").read() == "Current task: test\n")
    call("skill", {"name": "Release"}, "Skill: release")
    last = [m for m in ctx(d) if m["role"] == "user"][-1]["content"][0]["content"]
    check("[skill] tool gives folder, files and instructions", d.split("/files/")[0] + "/files/_shared/skills/release" in last
          and "scripts/go.sh" in last and "Run scripts/go.sh" in last, last)
    call("skill", {"name": "nope"}, "No skill named 'nope'. Skills: release")
    p.send_signal(signal.SIGTERM); p.wait(10)
    # requests_per_minute: shared across agents on the same API/model/key
    h = home("rpm", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY",
                     "requests_per_minute": 3})
    p1, a1, d1 = start(h); p2, a2, d2 = start(h)
    say(d1, "one"); wait_for(d1, "echo: one")
    say(d2, "two"); wait_for(d2, "echo: two")
    say(d1, "three"); wait_for(d1, "echo: three")
    say(d2, "four"); wait_for(d2, "rate limit (3/min): waiting", 10)
    time.sleep(1)
    check("[rpm] 4th request across two agents waits", "echo: four" not in open(d2 + "/out").read())
    rate_files = [f for f in os.listdir(h + "/_shared") if f.startswith(".requests-")]
    check("[rpm] one shared request log, no key in it", len(rate_files) == 1 and "test-key" not in rate_files[0]
          and len(open(h + "/_shared/" + rate_files[0]).read().split()) == 3, rate_files)
    say(d2, "/stop"); p2.wait(10)
    check("[rpm] /stop works while waiting", p2.returncode == 0)
    p1.send_signal(signal.SIGTERM); p1.wait(10)
    # a model that expects a key but has none: stop with a clear message, no request sent
    h = home("nokey", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake",
                       "key_env": "NO_SUCH_KEY_VAR", "key_file": "~/no-such-key-file"})
    before = len(open(LOG).read())
    r = subprocess.run([sys.executable, HOB, "nokey1"], env={**os.environ, "HOBKIN_HOME": h}, capture_output=True, text=True, timeout=20)
    check("[keys] missing key stops with a clear message", r.returncode != 0 and "no API key for model 'm'" in r.stderr
          and "NO_SUCH_KEY_VAR" in r.stderr and "~/no-such-key-file" in r.stderr, r.stderr)
    check("[keys] no request was sent", len(open(LOG).read()) == before)
    check("no invalid requests at all", "INVALID" not in open(LOG).read(), [l for l in open(LOG) if "INVALID" in l][:3])
finally:
    for p in procs:
        if p.poll() is None: p.kill()
    shutil.rmtree(BASE, ignore_errors=True)
print("FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
