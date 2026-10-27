"""End-to-end tests: real hobkin processes against tests/fake_api.py (no real model, no cost).

    python3 tests/test_hobkin.py
"""
import json, os, re, select, urllib.request, subprocess, sys, time, shutil, signal, tempfile
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
def start(h, aid=None, **extra_env):
    env = {**os.environ, "HOBKIN_HOME": h, "TESTKEY": "test-key", **extra_env}
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
def read_or_empty(path):
    return open(path).read() if os.path.exists(path) else ""
def ctx(d): return [json.loads(l) for l in open(d + "/context.jsonl")]
def last_results(d):
    """The last user message that carries tool results (the wrap-up check may follow it)."""
    return [m for m in ctx(d) if m["role"] == "user" and any(b.get("type") == "tool_result" for b in m["content"])][-1]
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
        check(f"[{api}] a new agent gets a pronounceable name", re.fullmatch(r"[bdfghjlmnprstvz][aeiou][bdglmnprstvz]kin", aid), aid)
        check(f"[{api}] context size from API", st.get("context_tokens") == 100000, st)
        say(d, "run: echo hello > a.txt && cat a.txt && echo FOO=$FOO KEY=${TESTKEY:-unset}")
        wait_for(d, "Done. Output was")
        check(f"[{api}] command ran in workspace", open(d + "/work/a.txt").read() == "hello\n")
        for _ in range(50):                                   # the wrap-up exchange follows the reply
            c = ctx(d)
            if len(c) >= 7: break
            time.sleep(0.2)
        check(f"[{api}] context file: system, task, tool round, reply, wrap-up check, reply",
              [m["role"] for m in c] == ["system", "user", "assistant", "user", "assistant", "user", "assistant"]
              and c[5]["content"][0]["text"].startswith("[hobkin: wrap-up check!"), [m["role"] for m in c])
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
        if api == "anthropic":   # images arrive inside the tool result
            import base64 as _b64
            open(d + "/work/dot.png", "wb").write(_b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="))
            say(d, 'call: view_image {"path": "dot.png"}'); wait_for(d, '"media_type": "image/png"', 10, file="context.jsonl")
            res = last_results(d)["content"][0]["content"]
            check("[image] anthropic: an image block inside the tool result", isinstance(res, list)
                  and res[1]["type"] == "image" and res[1]["source"]["media_type"] == "image/png", str(res)[:200])
        # an error inside a 200 response (OpenRouter's 504, Anthropic's overloaded) is retried, not a pause
        say(d, "hiccup please"); wait_for(d, "echo: hiccup please", 20)
        check(f"[{api}] an upstream error in a 200 body is retried", "model busy (5" in open(d + "/out").read()
              and "pausing until you send" not in open(d + "/out").read())
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
        last_user = last_results(d)
        check(f"[{api}] a message while a command runs: answered then (front desk), and the worker hears it with the result",
              any("you answered straight away" in b.get("text", "") and "operator: also check the wiki" in b.get("text", "")
                  and "FRONT DESK: still on it" in b.get("text", "") for b in last_user["content"]), last_user)
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
        check(f"[{api}] chat (piped) prints the reply", "agent> echo: hi from a pipe" in r.stdout and "operator>" not in r.stdout, r.stdout + r.stderr)
        # chat, interactive: in a pseudo-terminal (readline: history, input kept on screen)
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["HOBKIN_HOME"] = h
            os.environ["TERM"] = "xterm"
            os.execv(sys.executable, [sys.executable, HOB, "chat", aid])
        buf = b""
        def pump(until, t=20):
            nonlocal_buf = [b""]
            end = time.time() + t
            while time.time() < end:
                r_, _, _ = select.select([fd], [], [], 0.2)
                if r_:
                    try: nonlocal_buf[0] += os.read(fd, 4096)
                    except OSError: break
                if until(nonlocal_buf[0]): break
            return nonlocal_buf[0]
        buf += pump(lambda b: b"chatting with" in b)
        os.write(fd, b"typed in a terminal\n")
        buf += pump(lambda b: b"echo: typed in a terminal" in b)
        os.write(fd, b"half-typ")                      # start typing; the agent talks meanwhile
        time.sleep(0.5); say(d, "from elsewhere")
        later = pump(lambda b: b"echo: from elsewhere" in b and b"> half-typ" in b.split(b"echo: from elsewhere")[-1])
        check(f"[{api}] chat (terminal) redraws what you were typing after output",
              b"> half-typ" in later.split(b"echo: from elsewhere")[-1], later[-300:])
        os.write(fd, b"\x15\x1b[A\r")                 # Ctrl-U clears, up arrow recalls, Enter sends
        again = pump(lambda b: b"echo: typed in a terminal" in b)
        check(f"[{api}] chat (terminal) up arrow resends from history", b"echo: typed in a terminal" in again, again[-300:])
        os.write(fd, b"\x04")   # Ctrl-D leaves
        _, status = os.waitpid(pid, 0)
        check(f"[{api}] chat (terminal) round trip and Ctrl-D", b"echo: typed in a terminal" in buf and status == 0, buf[-400:])
        check(f"[{api}] chat history saved",     # libedit (macOS, some builds) writes spaces as \040
              "typed in a terminal" in open(d + "/.chat_history").read().replace("\\040", " "))
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
        mock(port, 12000, listctx)
        base = f"http://127.0.0.1:{port}" + ("/v1" if api == "openai" else "")
        h = home(api + "-small", {"api": api, "base_url": base, "model": "fake", "key_env": "TESTKEY", "max_output_tokens": 200})
        p, aid, d = start(h)
        for i in range(20):                                  # until the window has overflowed and been compacted
            say(d, f"run: seq 1 {1000 + i}")
            wait_for(d, f"{1000 + i}", 20); time.sleep(0.3)
            if "compacted; note" in open(d + "/out").read() and i >= 3:
                break
        last_n = 1000 + i
        out = open(d + "/out").read()
        st = json.load(open(d + "/state.json"))
        check(f"[{api}] compacted", "compacted; note" in out and os.listdir(d + "/archive"), out[-800:])
        arch = [n for n in os.listdir(d + "/archive") if n.startswith("context-")]
        check(f"[{api}] the archive holds the conversation, not the system prompt", arch and all(
            json.loads(l)["role"] != "system" for n in arch for l in open(f"{d}/archive/{n}")), arch)
        check(f"[{api}] out starts afresh at compaction, the old log archived", "up:" not in out
              and any(n.startswith("out-") and "up:" in open(f"{d}/archive/{n}").read() for n in os.listdir(d + "/archive")), out[:300])
        check(f"[{api}] context size known ({st.get('context_source')})", st.get("context_tokens") == 12000, st)
        check(f"[{api}] handoff note points at the archive", "Your whole earlier conversation is saved in" in open(d + "/context.jsonl").read())
        check(f"[{api}] request survives compaction", f"seq 1 {last_n}" in open(d + "/context.jsonl").read() or str(last_n) in open(d + "/out").read().split("compacted; note")[-1])
        for _ in range(100):                                   # let the last turn finish first
            if json.load(open(d + "/state.json")).get("status") == "idle": break
            time.sleep(0.2)
        say(d, "still there?"); wait_for(d, "operator> still there?")
        for _ in range(100):
            tail = open(d + "/out").read().split("operator> still there?")[-1]
            if "agent> echo:" in tail: break
            time.sleep(0.2)
        last_user = "".join(b.get("text", "") for b in [m for m in ctx(d) if m["role"] == "user"][-1]["content"])
        check(f"[{api}] answers after compaction, with the message in its context", "agent> echo:" in tail and "still there?" in last_user, tail[-300:])
        p.send_signal(signal.SIGTERM); p.wait(10)
    # the front desk: a message while a command runs is answered then, the command keeps going, the worker hears it
    h = home("frontdesk", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"idle_prompt": None})
    p, aid, d = start(h)
    say(d, "run: sleep 4; echo slow-done")
    wait_for(d, "$> sleep 4"); time.sleep(1)
    say(d, "how's it going?")
    wait_for(d, "FRONT DESK: still on it, re: how's it going?", 10)
    wait_for(d, "Done. Output was: slow-done", 15)
    out = open(d + "/out").read()
    check("[front desk] answered while the command ran, before it finished", out.index("FRONT DESK") < out.index("\n    slow-done"), out[-600:])
    worker_sees = json.dumps(last_results(d))
    check("[front desk] the worker gets the exchange with its command's result", "you answered straight away" in worker_sees
          and "FRONT DESK: still on it" in worker_sees, worker_sees[-400:])
    reqs = [json.loads(l)["body"] for l in open(LOG) if '"path": "/v1/messages"' in l]
    reqs = [r for r in reqs if "/frontdesk/" in json.dumps(r.get("system"))]          # this agent's requests
    fd = [i for i, r in enumerate(reqs) if "while that is still running" in json.dumps(r["messages"][-1:])]
    same = fd and len(reqs) > fd[-1] + 1 and reqs[fd[-1]]["messages"][:-1] == reqs[fd[-1] + 1]["messages"][:len(reqs[fd[-1]]["messages"]) - 1]
    check("[front desk] its request is the worker's conversation plus the message (a local KV cache is reused)", same,
          [len(r["messages"]) for r in reqs[-3:]])
    p.send_signal(signal.SIGTERM); p.wait(10)

    # context: a budget below the model's window; compaction at good moments; the checklist carried over
    h = home("ctxbudget", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY",
                           "max_output_tokens": 200}, {"idle_prompt": None, "context_budget": 24000, "compact_early": 0.5})
    p, aid, d = start(h)
    base = json.load(open(d + "/state.json"))
    say(d, 'call: write_file {"path": "CHECKLIST.md", "content": "# goal\\n- [ ] count\\n- [ ] report\\n"}')
    wait_for(d, "Created")
    for _ in range(50):
        if json.load(open(d + "/state.json")).get("status") == "idle": break
        time.sleep(0.1)
    say(d, 'multi-call: [["bash", {"command": "seq 1 9000"}], ["edit_file", {"path": "CHECKLIST.md", "old_text": "- [ ] count", "new_text": "- [x] count"}]]')
    wait_for(d, "compacted; note", 20)
    why = "".join(open(f"{d}/archive/{n}").read() for n in os.listdir(d + "/archive") if n.startswith("out-"))
    first = ctx(d)[1]["content"][0]["text"]
    check("[context] the window is capped by context_budget (the model says 100000)", base.get("context_tokens") == 100000)
    check("[context] a ticked checklist step past half the budget: compacted there, not mid-step",
          "compacting context (a checklist step is done" in why, why[-400:])
    check("[context] the checklist goes over word for word", "Your checklist, as it is now" in first and "- [x] count" in first
          and "- [ ] report" in first, first[:600])
    p.send_signal(signal.SIGTERM); p.wait(10)
    # Between tasks: past half, it compacts while idle, and doesn't wake itself up for it
    h = home("ctxidle", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY",
                         "max_output_tokens": 200}, {"idle_prompt": None, "context_budget": 24000, "compact_early": 0.5,
                                                     "output_limit": 50000})
    p, aid, d = start(h)
    say(d, "run: seq 1 9000")
    wait_for(d, "compacted; note", 20)
    why = "".join(open(f"{d}/archive/{n}").read() for n in os.listdir(d + "/archive") if n.startswith("out-"))
    for _ in range(50):
        if json.load(open(d + "/state.json")).get("status") == "idle": break
        time.sleep(0.1)
    time.sleep(1)
    msgs = ctx(d)[1:]
    check("[context] between tasks it compacts while idle, without waking itself", len(msgs) == 1
          and "compacting context (a task is done" in why
          and "nothing in progress" in msgs[0]["content"][0]["text"], [m["role"] for m in msgs])
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
    check("[files] read_file says how much is unread", "2 more lines unread (offset=4)" in json.dumps(last_results(d)))
    call("write_file", {"path": "t/REVIEW.md", "content": "A robust, comprehensive, production-ready framework."}, "reads like a committee wrote it")
    call("write_file", {"path": "t/plain.md", "content": "Ha! The parser eats its own tail at line 40. Fixed."}, "plain.md (1 lines)")
    check("[voice] plain writing gets no committee nudge", "committee" not in json.dumps(last_results(d)))
    call("edit_file", {"path": "t/a.txt", "old_text": "line3", "new_text": "L3"}, "appears 2 times (lines 3, 5)")
    call("edit_file", {"path": "t/a.txt", "old_text": "    3| line3\n    4| line4", "new_text": "LINE3\nline4"}, "line numbers from read_file removed")
    check("[files] edit with pasted line numbers still works", open(d + "/work/t/a.txt").read().endswith("LINE3\nline4\nline3\n"))
    call("edit_file", {"path": "t/a.txt", "old_text": "lime4", "new_text": "x"}, "The closest text in the file is lines 4-4")
    call("edit_file", {"path": "$HOBKIN_MEMORY", "old_text": "nothing", "new_text": "x"}, "The file is empty")
    call("write_file", {"path": "$HOBKIN_MEMORY", "content": "Current task: test\n"}, "Replaced ")
    check("[files] $HOBKIN_MEMORY path works", open(d + "/MEMORY.md").read() == "Current task: test\n")
    # images: an image result reaches an OpenAI-style API as a user message after the tool result
    import base64 as _b64
    png = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")
    open(d + "/work/t/dot.png", "wb").write(png)
    before = open(LOG).read().count('"image_url"')
    call("view_image", {"path": "t/dot.png"}, "Done. Output was: ")
    for _ in range(50):
        if open(LOG).read().count('"image_url"') > before: break
        time.sleep(0.1)
    check("[image] openai-style: sent as image_url after the tool result", open(LOG).read().count('"image_url"') > before)
    open(d + "/work/t/big.png", "wb").write(png + b"0" * (6 * 1024 * 1024))
    call("view_image", {"path": "t/big.png"}, "images must be under 5 MB")
    call("view_image", {"path": "t/a.txt"}, "isn't a PNG, JPEG, GIF or WebP")
    call("write_file", {"path": "$HOBKIN_BLOOPERS", "content": "- Tripped over my own heredoc. Lesson: use write_file!\n"}, "BLOOPERS.md (1 lines)")
    say(d, "hi"); wait_for(d, "echo: hi")
    check("[bloopers] the reel is in the prompt", "Tripped over my own heredoc" in ctx(d)[0]["content"])
    call("skill", {"name": "Release"}, "Skill: release")
    last = last_results(d)["content"][0]["content"]
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
    say(d2, "four"); wait_for(d2, "waiting for a request slot (3/min)", 10)
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
    check("[keys] missing key stops with a clear message", r.returncode != 0 and "no API key for model m (fake via" in r.stderr
          and "NO_SUCH_KEY_VAR" in r.stderr and "~/no-such-key-file" in r.stderr, r.stderr)
    check("[keys] no request was sent", len(open(LOG).read()) == before)
    # config: models come only from the files; "model" must name one of them
    h = home("cfg", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"})
    cfg = json.load(open(h + "/_shared/config.json")); cfg["model"] = "claude"          # the deleted default
    json.dump(cfg, open(h + "/_shared/config.json", "w"))
    r = subprocess.run([sys.executable, HOB, "cfg1"], env={**os.environ, "HOBKIN_HOME": h, "TESTKEY": "test-key"},
                       capture_output=True, text=True, timeout=20)
    check("[config] a model deleted from config.json isn't brought back by the defaults",
          r.returncode != 0 and "\"model\" is 'claude', which isn't one of the models defined in" in r.stderr and ": m." in r.stderr, r.stderr)
    del cfg["model"]; json.dump(cfg, open(h + "/_shared/config.json", "w"))
    p, aid, d = start(h, "cfg2")
    check("[config] the only model is used when \"model\" isn't set", "up: m (fake via" in open(d + "/out").read())
    os.makedirs(d, exist_ok=True)
    p.send_signal(signal.SIGTERM); p.wait(10)
    json.dump({"models": {"m": {"requests_per_minute": 99}, "extra": {"api": "openai", "base_url": "http://x/v1", "model": "y"}}},
              open(d + "/config.json", "w"))
    r = subprocess.run([sys.executable, HOB, "cfg2"], env={**os.environ, "HOBKIN_HOME": h, "TESTKEY": "test-key"},
                       capture_output=True, text=True, timeout=20)
    check("[config] two models and no \"model\": asks you to pick", r.returncode != 0 and "\"model\" isn't set" in r.stderr
          and "m, extra" in r.stderr, r.stderr)
    # reminders: fire while idle, validation, list/cancel, prompt section, late + repeating after a restart
    h = home("remind", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"})
    p, aid, d = start(h)
    def tool(name, args, marker):
        say(d, f"call: {name} " + json.dumps(args)); wait_for(d, marker, file="context.jsonl")
    tool("remind", {"message": "check the build log", "in": "12s"}, "Reminder #1 set for")
    wait_for(d, "reminder> [Reminder #1", 20)
    wait_for(d, "agent> echo: [Reminder #1", 10)
    check("[remind] fires on its own while idle", json.load(open(d + "/reminders.json"))["items"] == [])
    # due in the middle of a command: not an interruption; it arrives with that command's result
    tool("remind", {"message": "mid-task nudge", "in": "11s"}, "Reminder #2 set for")
    say(d, "run: sleep 15; echo finished-sleeping"); wait_for(d, "reminder> [Reminder #2", 25)
    wait_for(d, "agent> Done. Output was: finished-sleeping")
    last_user = last_results(d)["content"]
    check("[remind] due mid-command: command not interrupted, reminder rides with its result",
          last_user[0]["type"] == "tool_result" and "finished-sleeping" in last_user[0]["content"]
          and any("[Reminder #2" in b.get("text", "") and "mid-task nudge" in b.get("text", "") for b in last_user[1:]), last_user)
    tool("remind", {"message": "doorbell", "every": "1s"}, "Otherwise, enjoy the doorbell!")
    tool("reminders", {"cancel": 3}, "Cancelled reminder #3")
    tool("remind", {"message": "x", "at": "2000-01-01 10:00"}, "in the past: it's now")
    tool("remind", {"message": "x", "in": "soon"}, "can't read 'soon' as a length of time")
    tool("remind", {"message": "later", "in": "2h"}, "Reminder #4 set for")
    tool("remind", {"message": "daily standup notes", "at": "tomorrow 09:00", "every": "1d"}, "Reminder #5 set for")
    tool("reminders", {}, "Your reminders:")
    check("[remind] listed in the system prompt", "# Your reminders" in ctx(d)[0]["content"] and "daily standup notes" in ctx(d)[0]["content"])
    tool("reminders", {"cancel": 4}, "Cancelled reminder #4")
    say(d, "/reminders"); wait_for(d, "#5: ")
    say(d, "/stop"); p.wait(10)
    data = json.load(open(d + "/reminders.json"))
    check("[remind] cancel removed it", [r["id"] for r in data["items"]] == [5], data)
    three_days_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3 * 86400 - 600))
    data["items"][0]["due"] = three_days_ago
    json.dump(data, open(d + "/reminders.json", "w"))
    p, _, d = start(h, aid)
    wait_for(d, "late (hobkin wasn't running then); 3 earlier times were missed; repeats, next", 15)
    nxt = json.load(open(d + "/reminders.json"))["items"][0]["due"]
    check("[remind] late repeating reminder delivered once, rescheduled ahead", nxt > time.strftime("%Y-%m-%d %H:%M:%S"), nxt)
    open(d + "/reminders.json", "w").write("{broken")
    say(d, "hello"); wait_for(d, "echo: hello"); say(d, "again"); wait_for(d, "echo: again")
    check("[remind] a broken reminders.json is reported once", open(d + "/out").read().count("isn't valid JSON") == 1)
    p.send_signal(signal.SIGTERM); p.wait(10)
    # operator: names the sender of direct messages; automatic ones are marked as such
    h = home("who", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"operator": "frank", "idle_prompt": "Check for new work.", "idle_seconds": 3})
    p, aid, d = start(h)
    wait_for(d, "echo: Check for new work.", 10)
    say(d, "hello there"); wait_for(d, "echo: hello there")
    users = [m for m in ctx(d) if m["role"] == "user"]
    texts = [b.get("text", "") for m in users for b in m["content"]]
    check("[who] direct messages say who sent them", any(re.match(r"\[message from frank, \w+ [\d-]+ [\d:]+: they're talking with you now\. Answer them in your reply\]\nhello there\n\[hobkin: a task or an idea\?", t) for t in texts), texts)
    check("[who] idle prompt is marked automatic", any(re.match(r"\[from hobkin: idle prompt, \w+ [\d-]+ [\d:]+\]\nCheck for new work\.$", t) for t in texts), texts)
    for _ in range(3):                                   # idle_seconds is 3: they'd fire by now if not paused
        time.sleep(2)
    after_hello = open(d + "/out").read().split("frank> hello there")[-1]
    check("[chat] idle prompts wait while someone's talking with it", "idle prompt" not in after_hello, after_hello[-400:])
    say(d, "silent-run: echo shh"); wait_for(d, "Done. Output was: shh")
    check("[chat] acting on a direct message without a word gets one reminder that they're waiting",
          "frank is waiting for an answer" in json.dumps(last_results(d)), last_results(d))
    check("[who] system prompt names the operator", "Messages that arrive here directly are from frank" in ctx(d)[0]["content"])
    check("[memory] the prompt says memory is a sketch to check", "A sketch of where you were, not the truth" in ctx(d)[0]["content"]
          and "Your memory is the enemy, and you love beating it" in ctx(d)[0]["content"])
    check("[who] out log uses the name", "frank> hello there" in open(d + "/out").read())
    say(d, 'run: echo "Hi frank, working on the tickets now!"'); wait_for(d, "Done. Output was: Hi frank")
    say(d, "run: echo done-marker"); wait_for(d, "Done. Output was: done-marker")
    results = [b["content"] for m in ctx(d) if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"]
    check("[who] talking through echo gets nudged; script echos don't",
          "just say that in your reply" in results[-2] and "just say that in your reply" not in results[-1], results[-2:])
    r = subprocess.run([sys.executable, HOB, "chat", aid], input="via chat\n", env={**os.environ, "HOBKIN_HOME": h},
                       capture_output=True, text=True, timeout=30)
    check("[who] chat doesn't echo your own (named) messages", "echo: via chat" in r.stdout and "frank> via chat" not in r.stdout, r.stdout)
    p.send_signal(signal.SIGTERM); p.wait(10)
    # thinking, effort and extra_body reach the request, in each API's own shape
    for api, port, path_ in (("anthropic", 48701, "/v1/messages"), ("openai", 48702, "/chat/completions")):
        base = f"http://127.0.0.1:{port}" + ("/v1" if api == "openai" else "")
        h = home("think-" + api, {"api": api, "base_url": base, "model": "fake", "key_env": "TESTKEY",
                                   "thinking": "adaptive", "effort": "high", "extra_body": {"metadata": {"user_id": "t"}}})
        p, aid, d = start(h)
        say(d, "think about it"); wait_for(d, "echo: think about it")
        sent = [json.loads(l) for l in open(LOG) if path_ in l and "think about it" in l][-1]["body"]
        want = ({"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}} if api == "anthropic"
                else {"reasoning_effort": "high"})
        check(f"[{api}] thinking/effort/extra_body in the request", all(sent.get(k) == v for k, v in want.items())
              and sent.get("metadata") == {"user_id": "t"}, {k: sent.get(k) for k in ("thinking", "output_config", "reasoning_effort", "metadata")})
        p.send_signal(signal.SIGTERM); p.wait(10)

    # config: typos and wrong types are warned about, and the agent still starts
    h = home("warn", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY",
                      "requests_per_minut": 5}, {"idel_seconds": 3, "compact_at": "0.8"})
    p, aid, d = start(h)
    out = open(d + "/out").read()
    check("[config] unknown keys and wrong types warned, with suggestions",
          "unknown setting 'idel_seconds' (did you mean 'idle_seconds'?)" in out
          and "unknown setting 'models.m.requests_per_minut' (did you mean 'models.m.requests_per_minute'?)" in out
          and "setting 'compact_at' should be int or float, not \"0.8\"" in out, out[:900])
    # committee-speak in a reply gets one voice nudge
    say(d, "robust comprehensive seamless synergy leverage")
    for _ in range(50):
        if any(b.get("text", "").startswith("[hobkin: that reads like a committee") for m in ctx(d) for b in m["content"] if m["role"] == "user"): break
        time.sleep(0.2)
    nudges = sum(b.get("text", "").startswith("[hobkin: that reads like a committee") for m in ctx(d) for b in m["content"] if m["role"] == "user")
    check("[voice] a committee-speak reply gets one nudge", nudges == 1, nudges)
    p.send_signal(signal.SIGTERM); p.wait(10)
    # provider rate limits (429): wait exactly as long as the API says, guess well when it doesn't,
    # and share the cooldown so other agents on the same key don't poke it
    import urllib.request as _ur
    served = lambda: json.load(_ur.urlopen("http://127.0.0.1:48701/stats"))["rate_429s"]
    h = home("limits", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"})
    p1, a1, d1 = start(h); p2, a2, d2 = start(h)
    for mode, hint in (("header", "retry-after"), ("epoch", "x-ratelimit-reset"), ("body", "x-ratelimit-reset")):
        before, t0 = served(), time.time()
        say(d1, f"ratelimit:4:{mode} please"); wait_for(d1, f"echo: ratelimit:4:{mode} please", 30)
        took, out = time.time() - t0, open(d1 + "/out").read()
        check(f"[429] reset given as {mode}: one blocked call, then waits it out",
              served() - before == 1 and 3.5 <= took < 9 and f"the API said when ({hint})" in out, (served() - before, round(took, 1)))
    before, t0 = served(), time.time()
    say(d1, "ratelimit:4:none please"); wait_for(d1, "echo: ratelimit:4:none please", 40)
    check("[429] no reset given: guesses 15s, one blocked call", served() - before == 1 and time.time() - t0 >= 14
          and "no reset time given, so guessing: waiting 15s" in open(d1 + "/out").read(), (served() - before, round(time.time() - t0, 1)))
    before = served()
    marker = "rate limited by the API; the API said when (retry-after)"
    seen = open(d1 + "/out").read().count(marker)
    say(d1, "ratelimit:6:header shared")
    for _ in range(100):                                   # a new occurrence, not one from earlier
        if open(d1 + "/out").read().count(marker) > seen: break
        time.sleep(0.1)
    say(d2, "hello from the second agent")
    wait_for(d2, "another agent on this key hit it", 10)
    for _ in range(20):                                    # the log line comes a moment before state.json
        if json.load(open(d2 + "/state.json")).get("status") == "waiting (rate limited)": break
        time.sleep(0.1)
    check("[429] the cooldown is shared: the other agent waits without calling", served() - before == 1
          and json.load(open(d2 + "/state.json"))["status"] == "waiting (rate limited)", served() - before)
    wait_for(d1, "echo: ratelimit:6:header shared", 20); wait_for(d2, "echo: hello from the second agent", 20)
    check("[429] both carry on afterwards, still one blocked call", served() - before == 1, served() - before)
    for p_ in (p1, p2): p_.send_signal(signal.SIGTERM); p_.wait(10)

    # background jobs: report back when they finish, even across a restart
    h = home("jobs", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"})
    p, aid, d = start(h)
    say(d, 'call: bash {"command": "sleep 2; echo job-done-marker", "background": true}')
    wait_for(d, "Started background job #1", file="context.jsonl")
    wait_for(d, "Background job #1 finished: exit 0", 15)
    wait_for(d, "job-done-marker", 5, file="context.jsonl")
    check("[jobs] a background job reports back when it finishes", True)
    say(d, 'call: bash {"command": "sleep 3; echo after-restart", "background": true}')
    wait_for(d, "Started background job #2", file="context.jsonl")
    say(d, "/stop"); p.wait(10); time.sleep(4)
    p, _, d = start(h, aid)
    wait_for(d, "Background job #2 finished: exit 0", 15)
    check("[jobs] a job that finished while hobkin was stopped is reported after restart", "after-restart" in open(d + "/context.jsonl").read())
    p.send_signal(signal.SIGTERM); p.wait(10)

    # nudges stay quiet while the agent has its own wake-up set (a reminder or a job)
    h = home("quiet", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"idle_prompt": "nag me", "idle_seconds": 2})
    os.makedirs(h + "/quiet1")
    due = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7200))
    json.dump({"next_id": 2, "items": [{"id": 1, "message": "later", "due": due, "every": None, "set": due}]},
              open(h + "/quiet1/reminders.json", "w"))
    p, aid, d = start(h, "quiet1", HOBKIN_CHAT_QUIET="1")     # this test isn't about the pause during a chat
    say(d, "run: echo quiet-check"); wait_for(d, "Done. Output was: quiet-check"); time.sleep(4)
    texts = [b.get("text", "") for m in ctx(d) if m["role"] == "user" for b in m["content"]]
    check("[quiet] with a reminder set: no hot take, no wrap-up, no idle nag",
          not any("[hobkin: a task or an idea" in t or "wrap-up check" in t for t in texts)
          and "idle prompt" not in open(d + "/out").read(), [t[:60] for t in texts])
    os.remove(d + "/reminders.json")
    wait_for(d, "hobkin> idle prompt", 10)
    check("[quiet] once nothing's scheduled, the idle nag is back", True)
    p.send_signal(signal.SIGTERM); p.wait(10)

    # announce first: outward-facing commands wait for an announcement and a delay; /veto stops them
    h = home("announce", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"announce_first": {"test rockets": "launch-rockets"}, "announce_delay": 2, "idle_prompt": None})
    p, aid, d = start(h)
    def tool(name, args, marker):                          # waits for a *new* occurrence of the marker
        seen = open(d + "/context.jsonl").read().count(marker)
        say(d, f"call: {name} " + json.dumps(args))
        for _ in range(150):
            if open(d + "/context.jsonl").read().count(marker) > seen: return
            time.sleep(0.1)
        raise AssertionError(f"no new {marker!r}: " + open(d + "/out").read()[-600:])
    say(d, "run: echo launch-rockets now"); wait_for(d, "this looks outward-facing (test rockets)", file="context.jsonl")
    check("[announce] not run until announced", "launch-rockets now\n" not in "".join(
        r for r in [json.dumps(m) for m in ctx(d)] if "tool_result" in r))
    tool("bash", {"command": "echo launch-rockets now", "announced": "chat with frank"}, "Announced. It can run from")
    tool("bash", {"command": "echo launch-rockets now", "announced": "chat with frank"}, "Not yet: it can run from")
    time.sleep(2.5)
    tool("bash", {"command": "echo launch-rockets now", "announced": "chat with frank"}, "launch-rockets now\\n\\n[exit code 0")
    check("[announce] runs after the delay with no objection", "nobody objected: running it" in open(d + "/out").read())
    tool("bash", {"command": "echo launch-rockets twice", "announced": "chat"}, "Announced. It can run from")
    say(d, "/veto"); wait_for(d, "vetoed 1 announced command"); time.sleep(2.5)
    tool("bash", {"command": "echo launch-rockets twice", "announced": "chat"}, "vetoed this (test rockets)")
    check("[announce] /veto stops it", True)
    p.send_signal(signal.SIGTERM); p.wait(10)

    # daily budget: one last turn to say so, then paused
    h = home("budget", {"api": "openai", "base_url": "http://127.0.0.1:48702/v1", "model": "fake-costly", "key_env": "TESTKEY"},
             {"daily_budget_usd": 1.0, "idle_prompt": None})
    p, aid, d = start(h)
    for word in ("one", "two", "three"):
        say(d, word); wait_for(d, f"echo: {word}")
    say(d, "four"); wait_for(d, "today's budget ($1.0) is used up", 10, file="context.jsonl")
    say(d, "five"); wait_for(d, "daily budget used up", 10)
    for _ in range(20):
        if json.load(open(d + "/state.json")).get("status") == "paused (budget)": break
        time.sleep(0.1)
    check("[budget] a notice turn, then paused for the day", json.load(open(d + "/state.json")).get("status") == "paused (budget)")
    p.send_signal(signal.SIGTERM); p.wait(10)

    # the web: pages as text, paging, JSON, downloads, meta refresh, SSO with cookies, search, client certificates
    certs = os.path.join(BASE, "certs"); os.makedirs(certs)
    def ossl(*a):
        subprocess.run(["openssl", *a], cwd=certs, check=True, capture_output=True)
    # The extensions a real CA sets: Python 3.13+ checks them (VERIFY_X509_STRICT)
    ossl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=Test CA",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    leaf = "authorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
    open(os.path.join(certs, "server.cnf"), "w").write(leaf + "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n")
    open(os.path.join(certs, "client.cnf"), "w").write(leaf + "extendedKeyUsage=clientAuth\n")
    for who, extra in (("server", ["-extfile", "server.cnf"]), ("client", ["-extfile", "client.cnf"])):
        ossl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{who}.key", "-out", f"{who}.csr", "-subj", f"/CN=hobkin-{who}")
        ossl("x509", "-req", "-in", f"{who}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
             "-out", f"{who}.pem", "-days", "2", *extra)
    web = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_web.py"), "48790", "48791", certs]); procs.append(web)
    time.sleep(0.8)
    W = "http://127.0.0.1:48790"
    h = home("web", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"idle_prompt": None, "web": {"search_url": W + "/ddg", "client_cert":
                 {"cert": os.path.join(certs, "client.pem"), "key": os.path.join(certs, "client.key"),
                  "ca": os.path.join(certs, "ca.pem")}}})
    p, aid, d = start(h)
    def tool(name, args, marker):                          # waits for a *new* occurrence of the marker
        seen = open(d + "/context.jsonl").read().count(marker)
        say(d, f"call: {name} " + json.dumps(args))
        for _ in range(150):
            if open(d + "/context.jsonl").read().count(marker) > seen: return last_results(d)["content"][0]["content"]
            time.sleep(0.1)
        raise AssertionError(f"no new {marker!r}: " + open(d + "/out").read()[-600:])
    page = tool("fetch_url", {"url": W + "/page"}, "Team Wiki: Deploys")
    check("[web] a page as text: title, links numbered, scripts gone", "# Deploys" in page and "the runbook [2]" in page
          and "[2] http://127.0.0.1:48790/runbook" in page and "alert" not in page and "- check the dashboard" in page, page)
    page = tool("fetch_url", {"url": W + "/long", "offset": 401, "limit": 100}, "lines 401-500 of 1000")
    check("[web] long pages come in parts", "line 401" in page and "500 more lines (offset=501)" in page, page[-200:])
    page = tool("fetch_url", {"url": W + "/data.json"}, '\\"team\\": \\"blue\\"')
    check("[web] JSON pretty-printed", '"deploys": [' in page, page)
    tool("fetch_url", {"url": W + "/img.png", "save_as": "dl/dot.png"}, "Saved http://127.0.0.1:48790/img.png")
    check("[web] save_as downloads the file", open(d + "/work/dl/dot.png", "rb").read().startswith(b"\x89PNG"))
    page = tool("fetch_url", {"url": W + "/refresh"}, "Team Wiki: Deploys")
    check("[web] meta refresh followed", "make deploy" in page)
    page = tool("fetch_url", {"url": W + "/wiki/secret"}, "deploy password is in the vault")
    check("[web] SSO: redirect, auto-submitted login form, cookie, back to the page", "# Secret" in page, page)
    check("[web] the SSO cookie is kept for next time", "session" in open(d + "/cookies.txt").read())
    page = tool("fetch_url", {"url": "https://127.0.0.1:48791/secret"}, "read with the certificate of")
    check("[web] the client certificate, offered where asked", "certificate of hobkin-client" in page, page)
    res = tool("web_search", {"query": "python html parser"}, "html.parser")
    check("[web] search: titles, real URLs (not DuckDuckGo redirects), snippets; ads skipped",
          "1. html.parser — Simple HTML and XHTML parser\n   https://docs.python.org/3/library/html.parser.html" in res
          and "2. Parsing HTML in Python\n   https://example.org/parsing" in res and "Buy parsers" not in res, res)
    p.send_signal(signal.SIGTERM); p.wait(10)
    h = home("web2", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"idle_prompt": None, "web": {"search_url": W + "/ddg-blocked"}})
    p, aid, d = start(h)
    res = tool("web_search", {"query": "anything"}, "asking for a human check")
    check("[web] DuckDuckGo's bot check is reported plainly", True)
    res = tool("fetch_url", {"url": "https://127.0.0.1:48791/secret"}, "Couldn't fetch")
    check("[web] no certificate configured: the internal host refuses", True)
    p.send_signal(signal.SIGTERM); p.wait(10)

    # a long life: time stamps, size limits, fallback models and alerts, pruning, /restart
    h = home("long", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake-gone", "key_env": "TESTKEY"},
             {"idle_prompt": None, "fallback_models": ["n2", "nope"],
              "alert_command": "echo \"$HOBKIN_ALERT\" >> " + os.path.join(BASE, "alerts.txt")})
    cfg = json.load(open(h + "/_shared/config.json"))
    cfg["models"]["n2"] = {"api": "openai", "base_url": "http://127.0.0.1:48702/v1", "model": "fake", "key_env": "TESTKEY"}
    json.dump(cfg, open(h + "/_shared/config.json", "w"))
    d = os.path.join(h, "longlived"); os.makedirs(d + "/archive"); os.makedirs(d + "/jobs/1")
    json.dump({"last_step": time.time() - 3 * 86400 - 7200}, open(d + "/state.json", "w"))
    open(d + "/BLOOPERS.md", "w").write("".join(f"- slip {i}\n" for i in range(40)))
    old = time.time() - 40 * 86400
    for n in ("context-20200101-000000.jsonl", "out-20200101-000000.log", "context-20200102-000000.jsonl"):
        open(f"{d}/archive/{n}", "w").write("{}\n"); os.utime(f"{d}/archive/{n}", (old, old))
    open(d + "/jobs/1/exit", "w").write("0\n"); os.utime(d + "/jobs/1/exit", (old, old))
    p, aid, d = start(h, "longlived")
    check("[prune] old archives and job folders go; the newest archive stays",
          sorted(os.listdir(d + "/archive")) == ["context-20200102-000000.jsonl", "out-20200101-000000.log"]
          and not os.path.exists(d + "/jobs/1"), os.listdir(d + "/archive"))
    out = open(d + "/out").read()
    check("[config] an unknown fallback model is warned about", "fallback_models has 'nope'" in out, out[:600])
    say(d, "hello after the weekend"); wait_for(d, "echo: hello after the weekend")
    first = [b["text"] for b in ctx(d)[1]["content"] if b.get("type") == "text"][0]
    check("[time] a message says when, and how long since it last worked", re.match(
        r"\[message from operator, \w+ [\d-]+ [\d:]+; 3d 2h since you last worked: they're talking with you now\. Answer them in your reply\]\nhello after the weekend", first), first)
    out = open(d + "/out").read()
    check("[fallback] a withdrawn model switches to the next", "refused (404), switching to n2" in out
          and json.load(open(d + "/state.json"))["model"].startswith("n2"), out[-600:])
    alerts = open(os.path.join(BASE, "alerts.txt")).read()
    check("[alert] the switch is announced through alert_command", "model m (fake-gone via" in alerts and "switched to n2" in alerts, alerts)
    check("[limits] an overgrown blooper reel is flagged in the prompt", "your blooper reel is 40 lines, over its 30-line limit"
          in ctx(d)[0]["content"] and "your memory is" not in ctx(d)[0]["content"])
    say(d, 'call: read_file {"path": "$HOBKIN_BLOOPERS"}'); wait_for(d, "BLOOPERS.md:     1|")
    check("[limits] and nudged when it opens it", "over its 30-line limit. Prune it" in last_results(d)["content"][0]["content"])
    say(d, "run: echo $PPID > pid-before && echo /restart > $HOBKIN_DIR/in"); wait_for(d, "restarting")
    for _ in range(100):
        if open(d + "/out").read().count("up:") >= 2 and json.load(open(d + "/state.json")).get("status") == "idle": break
        time.sleep(0.1)
    texts = [b.get("text", "") for m in ctx(d) if m["role"] == "user" for b in m["content"]]
    check("[restart] /restart: same pid, fresh start, and the agent is told", p.poll() is None
          and json.load(open(d + "/state.json"))["pid"] == p.pid and any(t.startswith("[from hobkin: restarted as asked") for t in texts),
          open(d + "/out").read()[-500:])
    out = open(d + "/out").read()
    check("[restart] starts on the first model again (and falls back again)",
          out.count("up: m (fake-gone") == 2 and out.count("switching to n2") == 2, out[-800:])
    p.send_signal(signal.SIGTERM); p.wait(10)
    os.makedirs(h + "/lonely"); json.dump({"fallback_models": []}, open(h + "/lonely/config.json", "w"))
    p, aid, d = start(h, "lonely")
    say(d, "anyone?"); wait_for(d, "model error, pausing")
    for _ in range(50):
        if "paused" in open(os.path.join(BASE, "alerts.txt")).read(): break
        time.sleep(0.1)
    check("[alert] no fallback left: the pause is announced", "hobkin lonely paused: model" in open(os.path.join(BASE, "alerts.txt")).read())
    p.send_signal(signal.SIGTERM); p.wait(10)

    # /limits: the context window asked for again on demand
    mock(48705, 100000)
    h = home("limits", {"api": "anthropic", "base_url": "http://127.0.0.1:48705", "model": "fake", "key_env": "TESTKEY"},
             {"idle_prompt": None})
    p, aid, d = start(h)
    say(d, "/limits"); wait_for(d, "limits (/limits): context 100000 (unchanged")
    urllib.request.urlopen("http://127.0.0.1:48705/set_ctx/60000").read()
    say(d, "/limits"); wait_for(d, "context window now 60000 (was 100000")
    check("[limits] /limits picks up a changed window", json.load(open(d + "/state.json"))["context_tokens"] == 60000)
    p.send_signal(signal.SIGTERM); p.wait(10)

    # one-line setup: hobkin URL MODEL KEY --chat (in the background here; CI has no systemd)
    hs = os.path.join(BASE, "setup")
    env = {**os.environ, "HOBKIN_HOME": hs}
    run = lambda *a, **k: subprocess.run([sys.executable, HOB, *a], env=env, capture_output=True, text=True, timeout=60, **k)
    r = run("models", "http://127.0.0.1:48702/v1", "sk-x")
    check("[setup] hobkin models lists an endpoint's models", "2 models at http://127.0.0.1:48702/v1" in r.stdout
          and re.search(r"^  fake +100k context$", r.stdout, re.M), r.stdout + r.stderr)
    r = run("http://127.0.0.1:48702/v1", "fakr", "sk-x", "--no-service", "--id", "s1")
    check("[setup] a model the endpoint doesn't offer is refused, with the close ones", r.returncode != 0
          and "doesn't offer 'fakr'. Close: fake" in r.stderr and not os.path.exists(hs + "/s1"), r.stderr)
    r = run("http://127.0.0.1:48702/v1", "fake", "sk-secret", "--no-service", "--id", "s1", "--chat", input="hello setup\n")
    cfg = json.load(open(hs + "/_shared/config.json"))
    key_file = cfg["models"]["fake"]["key_file"]
    check("[setup] configures the endpoint as the default, the key in a file only you can read",
          cfg["model"] == "fake" and cfg["models"]["fake"]["base_url"] == "http://127.0.0.1:48702/v1"
          and open(key_file).read().strip() == "sk-secret" and oct(os.stat(key_file).st_mode & 0o777) == "0o600"
          and "sk-secret" not in open(hs + "/_shared/config.json").read(), cfg)
    check("[setup] starts the agent and --chat talks to it", r.returncode == 0 and "agent> echo: hello setup" in r.stdout,
          r.stdout + r.stderr)
    r = run("chat", "s1", input="/models\n")
    check("[setup] /models lists them in a chat, the one in use starred", re.search(r"\* fake +100k context", r.stdout), r.stdout)
    r = run("chat", "s1", input="run: echo hidden-out-$((40+2))\n")
    check("[chat] tool calls show as one line, their output hidden", "$> echo hidden-out-$((40+2))" in r.stdout
          and "    hidden-out-42" not in r.stdout and "agent> Done. Output was: hidden-out-42" in r.stdout, r.stdout)
    check("[chat] piped chat stays plain text (no colour codes)", "\x1b[" not in r.stdout, repr(r.stdout[:200]))
    sys.path.insert(0, os.path.join(HERE, ".."))
    import hobkin as hb
    painted = "\n".join(hb.chat_view(["2026-10-24 10:00:00 agent> **Done.** Fixed `parse()` in tests/a.py, see https://x.io/y.",
                                       "2026-10-24 10:00:01 => (exit code 1; now in ~/w)"], "frank> ", False, {}, True))
    check("[chat] colour on a terminal: code and file paths green, bold, links underlined, failures red",
          "\x1b[32mparse()\x1b[0m" in painted and "\x1b[32mtests/a.py\x1b[0m" in painted and "\x1b[1mDone.\x1b[0m" in painted
          and "\x1b[4mhttps://x.io/y\x1b[0m." in painted and "\x1b[31m=> (exit code 1" in painted, repr(painted))
    r = run("chat", "s1", "--full", input="run: echo shown-out-$((40+3))\n")
    check("[chat] --full shows tool output too", "    shown-out-43" in r.stdout, r.stdout)
    ctx_s1 = open(hs + "/s1/context.jsonl").read()
    check("[chat] agents are asked to backtick file names and paths", "put file names, paths, commands, ids" in ctx_s1)
    # Opening a chat asks the agent to say hello first
    run("send", "s1", "/hello")
    for _ in range(50):
        if "just opened a chat with you" in open(hs + "/s1/context.jsonl").read(): break
        time.sleep(0.2)
    c1 = open(hs + "/s1/context.jsonl").read()
    check("[chat] opening a chat asks the agent to introduce itself by name", "just opened a chat with you" in c1
          and "(you're s1)" in c1 and "command /hello" not in open(hs + "/s1/out").read(), c1[-400:])
    # Four steps out loud: a file change before a plan's been said isn't run (once); planning files always are
    r = run("chat", "s1", input='noplan-call: write_file {"path": "fix.txt", "content": "x"}\n')
    check("[steps] a file change before **What would:** is said isn't run, and says why", "not run: no plan said yet" in r.stdout
          and not os.path.exists(hs + "/s1/work/fix.txt"), r.stdout)
    r = run("chat", "s1", input='noplan-call: write_file {"path": "CHECKLIST.md", "content": "- [ ] x"}\n')
    check("[steps] the checklist (part of planning) can always be written", os.path.exists(hs + "/s1/work/CHECKLIST.md"), r.stdout)
    r = run("chat", "s1", input='call: write_file {"path": "fix.txt", "content": "x"}\n')
    check("[steps] with the plan said, the change goes through", os.path.exists(hs + "/s1/work/fix.txt"), r.stdout)
    check("[chat] an agent with no skills is told its work is what people ask, not the machine",
          "You have no skills, so no work system" in ctx_s1 and "don't go looking for work" in ctx_s1, ctx_s1[:300])
    r = run("chat", "s1", input="/model fakr\n")
    check("[model] /model with a name nobody offers says so", "no model 'fakr'" in r.stdout and "Close: fake" in r.stdout, r.stdout)
    r = run("chat", "s1", input="/model fake-costly\n")
    acfg = json.load(open(hs + "/s1/config.json"))
    check("[model] /model switches to a model the API offers, saved in the agent's config",
          "switched from fake (fake via" in r.stdout and "to fake-costly (fake-costly via" in r.stdout
          and acfg["model"] == "fake-costly" and acfg["models"]["fake-costly"]["key_file"] == key_file
          and json.load(open(hs + "/s1/state.json"))["model"].startswith("fake-costly"), r.stdout + json.dumps(acfg))
    r = run("chat", "s1", input="/model\n")
    check("[model] /model alone lists the configured ones", re.search(r"configured \(\* = in use\):.* fake, \*fake-costly$", r.stdout, re.M), r.stdout)
    run("send", "s1", "/stop")
    # --hearthboard: join a board (the fake web server plays one); a bad token is refused before anything's written
    r = run("--hearthboard", "http://127.0.0.1:48790/board", "hb-bad", "--no-service")
    check("[hearthboard] a token the board refuses stops it", r.returncode != 0 and "refused that token (HTTP 401)" in r.stderr,
          r.stdout + r.stderr)
    r = run("--hearthboard", "http://127.0.0.1:48790/board/", "hb-good", "--no-service", "--chat", input="hi board\n")
    ad = hs + "/worker-7"
    sec = open(ad + "/secrets.env").read() if os.path.exists(ad + "/secrets.env") else ""
    check("[hearthboard] joins: named after its board user, the board's skill and token its own",
          r.returncode == 0 and "joined http://127.0.0.1:48790/board as worker.7" in r.stdout
          and open(ad + "/skills/hearthboard/SKILL.md").read().startswith("---\nname: hearthboard")
          and not os.path.exists(hs + "/_shared/skills/hearthboard")
          and "HEARTHBOARD_TOKEN=hb-good" in sec and "HEARTHBOARD_URL=http://127.0.0.1:48790/board" in sec
          and oct(os.stat(ad + "/secrets.env").st_mode & 0o777) == "0o600", r.stdout + r.stderr + sec)
    check("[hearthboard] on the configured model, with hobkin waiting on the board for it",
          "hi board" in open(ad + "/context.jsonl").read() and "You're now a worker" in open(ad + "/context.jsonl").read()
          and "/api/wait" in json.load(open(ad + "/config.json"))["wake"]["url"]
          and "hb-good" not in open(ad + "/context.jsonl").read(), r.stdout)
    check("[hearthboard] other agents here don't get the board's skill (s1 isn't a worker)",
          "You have no skills, so no work system" in open(hs + "/s1/context.jsonl").read())
    # A setup on a machine that's already set up changes nothing shared: the new model is that agent's own
    shared_before = open(hs + "/_shared/config.json").read()
    r = run("http://127.0.0.1:48702/v1", "fake-costly", "sk-other", "--no-service", "--id", "s2")
    acfg = json.load(open(hs + "/s2/config.json"))
    check("[setup] on an existing install, only the new agent gets the model; shared config and keys untouched",
          r.returncode == 0 and open(hs + "/_shared/config.json").read() == shared_before
          and acfg["model"] == "fake-costly" and open(acfg["models"]["fake-costly"]["key_file"]).read().strip() == "sk-other"
          and acfg["models"]["fake-costly"]["key_file"].startswith(hs + "/s2/")
          and json.load(open(hs + "/s2/state.json"))["model"].startswith("fake-costly"), r.stdout + r.stderr)
    run("send", "s2", "/stop")
    # Two steps: an agent you already have (running) joins a board; it restarts in place and is told
    r = run("http://127.0.0.1:48702/v1", "fake", "--no-service", "--id", "s3")
    ups = open(hs + "/s3/out").read().count(" up: ")
    r = run("--hearthboard", "http://127.0.0.1:48790/board", "hb-good", "--id", "s3")
    for _ in range(100):
        if "You're now a worker on the hearthboard" in open(hs + "/s3/context.jsonl").read(): break
        time.sleep(0.2)
    c3 = open(hs + "/s3/context.jsonl").read()
    check("[hearthboard] an agent you already have joins: restarted in place, conversation kept, and told",
          r.returncode == 0 and "an agent you already have (running)" in r.stdout and "restarted" in r.stdout
          and open(hs + "/s3/out").read().count(" up: ") > ups and os.path.exists(hs + "/s3/skills/hearthboard/SKILL.md")
          and "HEARTHBOARD_TOKEN=hb-good" in open(hs + "/s3/secrets.env").read()
          and "/api/wait" in json.load(open(hs + "/s3/config.json"))["wake"]["url"]
          and "You're now a worker on the hearthboard at http://127.0.0.1:48790/board, as worker.7" in c3, r.stdout + r.stderr)
    run("send", "s3", "/stop")
    run("send", "worker-7", "/stop")
    for _ in range(50):
        if json.load(open(ad + "/state.json")).get("status") == "stopped": break
        time.sleep(0.2)
    for _ in range(50):
        if json.load(open(hs + "/s1/state.json")).get("status") == "stopped": break
        time.sleep(0.2)

    # safe mode: Landlock keeps commands' writes in the workspace, with no TCP (where the kernel has it)
    sys.path.insert(0, os.path.join(HERE, ".."))
    import hobkin as hbk
    abi = hbk.landlock_abi()
    h = home("safe", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"idle_prompt": None, "safe_mode": True})
    if not abi:
        env = {**os.environ, "HOBKIN_HOME": h, "TESTKEY": "test-key"}
        r = subprocess.run([sys.executable, HOB, "guard"], env=env, capture_output=True, text=True, timeout=30)
        check("[safe] no Landlock here: safe mode refuses to start", r.returncode != 0 and "needs Landlock" in r.stderr, r.stderr)
    else:
        p, aid, d = start(h, "guard")
        out = open(d + "/out").read()
        check(f"[safe] says what this kernel's Landlock (v{abi}) covers", "safe mode: Landlock v" in out, out[-500:])
        def bash(cmd, marker):                            # waits for the marker in the command's output
            seen = open(d + "/out").read().count("\n    " + marker)
            say(d, "run: " + cmd)
            for _ in range(100):
                if open(d + "/out").read().count("\n    " + marker) > seen: break
                time.sleep(0.1)
            return open(d + "/out").read().rsplit("$> " + cmd, 1)[-1]
        outside = os.path.join(BASE, "safe-outside.txt")
        bash("echo in > inside.txt; echo out > " + outside + "; echo cfg >> $HOBKIN_DIR/config.json; echo mem >> $HOBKIN_MEMORY; echo done-1", "done-1")
        check("[safe] commands write in the workspace and memory, not outside or to their own config",
              open(d + "/work/inside.txt").read() == "in\n" and not os.path.exists(outside)
              and "cfg" not in read_or_empty(d + "/config.json") and "cfg" not in read_or_empty(h + "/_shared/config.json")
              and "mem" in open(d + "/MEMORY.md").read())
        bash("cd sub 2>/dev/null || mkdir sub; cd sub; export KEPT=yes; echo done-2", "done-2")
        tail = bash("echo KEPT=$KEPT in $PWD; echo done-3", "done-3")
        check("[safe] the shell still keeps its folder and variables", "KEPT=yes in " in tail and "/work/sub" in tail, tail[:300])
        if abi >= 4:
            tail = bash("(exec 3<>/dev/tcp/127.0.0.1/48701 && echo CONNECTED) 2>&1; echo done-4", "done-4")
            check("[safe] no TCP connections from commands", "CONNECTED" not in tail, tail[:300])
        say(d, 'call: write_file {"path": "' + outside + '", "content": "x"}'); wait_for(d, "Not written: safe mode")
        check("[safe] write_file can't write outside the workspace either", not os.path.exists(outside))
        check("[safe] the agent is told how to work: read, then scripts for a human that log what they find",
              "write it as a script for a human" in open(d + "/context.jsonl").read()
              and "read the log in `for-review/logs/` yourself" in open(d + "/context.jsonl").read())
        p.send_signal(signal.SIGTERM); p.wait(10)

    # the front desk and a board's chat (the inbox): a message there is answered during a command, and posted back
    h = home("inboxdesk", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"idle_prompt": None})
    os.makedirs(h + "/desk")
    json.dump({"inbox": {"url": "http://127.0.0.1:48790/board/api/chat/inbox", "reply": "http://127.0.0.1:48790/board/api/chat/{thread}/messages",
                         "headers": {"Authorization": "Bearer ${BOARD_TOKEN}"}, "every": 1}}, open(h + "/desk/config.json", "w"))
    open(h + "/desk/secrets.env", "w").write("BOARD_TOKEN=tok-9\n")
    p, aid, d = start(h, "desk")
    say(d, "run: sleep 5; echo built"); wait_for(d, "Done. Output was: built", 20)
    posted = json.loads(urllib.request.urlopen("http://127.0.0.1:48790/board/posted").read())
    check("[inbox] a board chat message is answered during the command and posted back, once, as text",
          len(posted) == 1 and posted[0]["path"] == "/board/api/chat/7/messages" and posted[0]["type"].startswith("text/plain")
          and "FRONT DESK: still on it, re: is the build done?" in posted[0]["body"] and posted[0]["auth"] == "Bearer tok-9", posted)
    check("[inbox] the worker hears it", "frank wrote in chat (frank) and you answered straight away, posted there"
          in open(d + "/context.jsonl").read())
    p.send_signal(signal.SIGTERM); p.wait(10)

    # heartbeat: on duty (working, or a wake-up scheduled) prints to stdout; waiting for a message is silent
    h = home("beat", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"heartbeat_seconds": 1, "idle_prompt": None})
    log = os.path.join(BASE, "beat-stdout.txt")
    p = subprocess.Popen([sys.executable, HOB, "beat1"], env={**os.environ, "HOBKIN_HOME": h, "TESTKEY": "test-key"},
                         stdout=open(log, "w"), stderr=subprocess.STDOUT); procs.append(p)
    d = os.path.join(h, "beat1")
    for _ in range(50):
        if os.path.exists(d + "/out") and "up:" in open(d + "/out").read(): break
        time.sleep(0.1)
    beats = lambda: [l for l in open(log).read().splitlines() if "hobkin beat1 active:" in l]
    time.sleep(3)
    check("[heartbeat] silent while only a message can wake it", beats() == [], beats())
    say(d, "run: sleep 4; echo slept-for-beat"); wait_for(d, "Done. Output was: slept-for-beat", 20)
    check("[heartbeat] 'working' while it works", any(l.endswith("active: working") for l in beats()), beats())
    say(d, 'call: remind {"message": "check the queue", "in": "2h"}'); wait_for(d, "Reminder #1 set for", file="context.jsonl")
    time.sleep(3)
    check("[heartbeat] 'on duty' with a reminder coming", any("on duty, next check in" in l and "(reminder #1)" in l for l in beats()), beats()[-3:])
    say(d, "/pause"); time.sleep(1.5); n = len(beats()); time.sleep(3)
    check("[heartbeat] silent when paused", len(beats()) == n, beats()[-3:])
    p.send_signal(signal.SIGTERM); p.wait(10)

    # break glass: lifted brakes, the timeline, extensions (by command and by config edit) alerted, --off ends properly
    alerts_file = os.path.join(BASE, "glass-alerts.txt")
    h = home("glass", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"idle_prompt": None, "alert_command": f'echo "$HOBKIN_ALERT" >> {alerts_file}',
              "announce_first": {"push to main": r"git push origin main"}})
    os.makedirs(h + "/ops"); json.dump({"safe_mode": True} if abi else {}, open(h + "/ops/config.json", "w"))
    p, aid, d = start(h, "ops")
    genv = {**os.environ, "HOBKIN_HOME": h, "TESTKEY": "test-key"}
    def glass(*a): return subprocess.run([sys.executable, HOB, "breakglass", "ops", *a], env=genv, capture_output=True, text=True, timeout=150)
    def alerts(): return read_or_empty(alerts_file)
    def timeline(): return read_or_empty(next((os.path.join(d, "work", f) for f in os.listdir(d + "/work") if f.startswith("incident-")), ""))
    def ups(): return open(d + "/out").read().count("break-glass mode: on until")
    r = glass("db down", "--for", "1h"); wait_for(d, "break-glass mode: on until")
    check("[glass] on: alerted, and the prompt says it's an incident", "break-glass mode ON until" in alerts()
          and "Break glass: an incident" in ctx(d)[0]["content"], r.stdout + r.stderr + alerts())
    if abi:
        outside = os.path.join(BASE, "glass-outside.txt")
        say(d, f"run: echo fix > {outside}; echo wrote-outside"); wait_for(d, "Done. Output was: wrote-outside")
        check("[glass] safe mode lifted: writes outside the workspace", os.path.exists(outside))
    say(d, "run: echo git push origin main"); wait_for(d, "Done. Output was: git push origin main")
    check("[glass] nothing waits to be announced", "outward-facing" not in open(d + "/context.jsonl").read())
    check("[glass] hobkin's timeline: commands, what it said, what it was told",
          "echo git push origin main" in timeline() and "said:" in timeline() and "told: [message from" in timeline(), timeline()[-800:])
    glass("db down", "--for", "2h")
    for _ in range(50):
        if "(extension 1)" in alerts(): break
        time.sleep(0.2)
    g = json.load(open(d + "/config.json"))["break_glass"]
    check("[glass] an extension is counted, attributed and alerted", g.get("extensions") == 1 and g.get("extended_by")
          and "(extension 1)" in alerts(), (g, alerts()))
    g["until"] += 3600; json.dump({**json.load(open(d + "/config.json")), "break_glass": g}, open(d + "/config.json", "w"))
    n = ups(); say(d, "/restart")
    for _ in range(100):
        if ups() > n and "outside `hobkin breakglass`" in alerts(): break
        time.sleep(0.2)
    check("[glass] an edit of config.json's end time is alerted too", "outside `hobkin breakglass`" in alerts(), alerts())
    r = glass("--off"); wait_for(d, "break-glass mode has ended", 20, file="context.jsonl")
    for _ in range(50):
        if "break_glass" not in json.load(open(d + "/state.json")): break
        time.sleep(0.1)
    check("[glass] --off: timeline line, alert, summary asked (with MEMORY.md), state cleared",
          "ended (switched off)" in timeline() and "OFF (switched off)" in alerts()
          and "MEMORY.md" in json.dumps([m for m in ctx(d) if "break-glass mode has ended" in json.dumps(m)][0])
          and "break_glass" not in json.load(open(d + "/state.json")), (timeline()[-400:], alerts()))
    if abi:
        os.remove(outside)
        say(d, f"run: echo again > {outside}; echo tried-again"); wait_for(d, "\n    tried-again"); time.sleep(0.5)
        check("[glass] back in safe mode afterwards", not os.path.exists(outside))
    # time's up: it ends on its own
    glass("still flaky", "--for", "6s"); wait_for(d, "break-glass mode ended", 30)
    wait_for(d, "time's up", 20, file="context.jsonl")
    check("[glass] it ends when time's up, and asks for the summary", "OFF (time's up)" in alerts(), alerts())
    # salvage: nothing said (and the incident that ran out is over, not extended); an extension keeps it;
    # a reason makes it an ordinary incident
    r = glass("--for", "1h"); wait_for(d, "salvage what can't be replaced", 20)
    g = json.load(open(d + "/config.json"))["break_glass"]
    check("[salvage] no reason: a new salvage, with its brief (and packing itself) in the prompt", g.get("salvage") is True
          and not g.get("extensions")
          and "No guidance given: salvage" in ctx(d)[0]["content"] and "$HOBKIN_SELF pack" in ctx(d)[0]["content"], r.stdout + r.stderr)
    glass("--for", "2h"); g = json.load(open(d + "/config.json"))["break_glass"]
    check("[salvage] an extension with nothing said stays a salvage", g.get("salvage") is True and g.get("extensions") == 1, g)
    glass("disk full", "--for", "2h"); g = json.load(open(d + "/config.json"))["break_glass"]
    check("[salvage] a reason given makes it an ordinary incident", g["reason"] == "disk full" and "salvage" not in g, g)
    n = alerts().count("OFF (switched off)"); glass("--off")
    for _ in range(100):
        if alerts().count("OFF (switched off)") > n: break
        time.sleep(0.2)
    p.send_signal(signal.SIGTERM); p.wait(10)

    # break glass has its own budget: an incident's spending doesn't pause the agent afterwards
    h = home("glassbudget", {"api": "openai", "base_url": "http://127.0.0.1:48702/v1", "model": "fake-costly", "key_env": "TESTKEY"},
             {"daily_budget_usd": 1.0, "idle_prompt": None})
    p, aid, d = start(h)
    genv = {**os.environ, "HOBKIN_HOME": h, "TESTKEY": "test-key"}
    subprocess.run([sys.executable, HOB, "breakglass", aid, "outage", "--for", "1h", "--budget", "none"], env=genv, capture_output=True, timeout=150)
    for word in ("one", "two", "three", "four", "five"):
        say(d, word); wait_for(d, f"echo: {word}")
    check("[glass budget] the incident's spending isn't the daily budget's", "budget" not in json.load(open(d + "/state.json")).get("status", ""))
    subprocess.run([sys.executable, HOB, "breakglass", aid, "--off"], env=genv, capture_output=True, timeout=150)
    say(d, "after"); wait_for(d, "echo: after", 20)
    check("[glass budget] and afterwards it isn't paused for it", "daily budget used up" not in open(d + "/out").read())
    p.send_signal(signal.SIGTERM); p.wait(10)

    # its own tools: work/tools/NAME/ with tool.json and an executable run; offered (here in the OpenAI shape), called, broken ones explained
    h = home("owntools", {"api": "openai", "base_url": "http://127.0.0.1:48702/v1", "model": "fake", "key_env": "TESTKEY"}, {"idle_prompt": None})
    p, aid, d = start(h)
    t = d + "/work/tools"
    os.makedirs(t + "/strum"); os.makedirs(t + "/broken"); os.makedirs(t + "/bash"); os.makedirs(t + "/noexec")
    spec = {"description": "Strum a chord on the synth", "input_schema": {"type": "object", "properties": {"chord": {"type": "string"}}}}
    json.dump(spec, open(t + "/strum/tool.json", "w"))
    open(t + "/strum/run", "w").write("#!/usr/bin/env python3\nimport json, os, sys\nprint('strummed', json.load(sys.stdin)['chord'], 'in', os.path.basename(os.getcwd()))\n")
    os.chmod(t + "/strum/run", 0o755)
    open(t + "/broken/tool.json", "w").write("{")
    json.dump(spec, open(t + "/bash/tool.json", "w")); json.dump(spec, open(t + "/noexec/tool.json", "w")); open(t + "/noexec/run", "w").write("echo hi\n")
    say(d, "run: mkdir -p songs && cd songs"); wait_for(d, "now in songs/")
    say(d, 'call: strum {"chord": "E minor"}'); wait_for(d, "Done. Output was: strummed E minor in songs")
    body = [json.loads(l)["body"] for l in open(LOG) if '"path"' in l and '"strum"' in l][-1]
    check("[own tools] offered in the API's shape, run with its input in the shell's folder",
          any(x.get("function", {}).get("name") == "strum" for x in body["tools"]), [x.get("function", {}).get("name") for x in body["tools"]])
    sysp = ctx(d)[0]["content"]
    check("[own tools] the ones that can't be offered are listed with why", "tools/broken: not offered: tool.json isn't valid JSON" in sysp
          and "tools/bash: not offered: that's the name of a built-in tool" in sysp and "tools/noexec: not offered: it has no executable `run`" in sysp, sysp[-900:])
    p.send_signal(signal.SIGTERM); p.wait(10)

    # hobkin pack: a bundle without secrets that starts anywhere, told it's a restored copy
    h = home("pack", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY",
                      "headers": {"X-Auth": "header-secret"}}, {"idle_prompt": None})
    p, aid, d = start(h)
    open(d + "/secrets.env", "w").write("AWS_SECRET=very-secret\n")
    open(d + "/MEMORY.md", "w").write("Found: the orders db, dumped to s3://acme-backups/salvage/\n")
    open(d + "/work/salvaged.bin", "wb").write(b"x" * (3 * 1024 * 1024))
    say(d, "run: python3 $HOBKIN_SELF pack $HOBKIN_ID --to $HOBKIN_WORK/out && ls $HOBKIN_WORK/out")
    wait_for(d, "Done. Output was")
    p.send_signal(signal.SIGTERM); p.wait(10)
    import tarfile
    bundles = os.listdir(d + "/work/out")
    check("[pack] an agent packs itself with $HOBKIN_SELF", len(bundles) == 1 and bundles[0].startswith(f"hobkin-{aid}-"), bundles)
    tar = tarfile.open(os.path.join(d, "work/out", bundles[0])); names = tar.getnames()
    blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile() and not m.name.endswith("/hobkin.py"))
    check("[pack] no secrets, keys or model headers; each file once; big workspace files left out",
          b"very-secret" not in blob and b"header-secret" not in blob and len(names) == len(set(names))
          and not any(n.endswith(("secrets.env", "salvaged.bin", "/in", "/lock")) for n in names), names)
    top = os.path.join(BASE, "restored"); tar.extractall(top); top = os.path.join(top, bundles[0][:-7])
    check("[pack] RESTORE.md says how to start it and what was left out", "--id " + aid in open(top + "/RESTORE.md").read()
          and "salvaged.bin" in open(top + "/RESTORE.md").read())
    r = subprocess.run([sys.executable, top + "/hobkin.py", "http://127.0.0.1:48701", "fake", "test-key", "--id", aid, "--no-service",
                        "--api", "anthropic"], env={**os.environ, "HOBKIN_HOME": top + "/home"}, capture_output=True, text=True, timeout=60)
    rd = top + "/home/" + aid
    try:
        wait_for(rd, "started from a bundle of yourself"); wait_for(rd, "agent> echo:")
        c = ctx(rd)
        check("[pack] the restored copy starts with its memory, told it's a copy of the one in its old folder",
              "the orders db" in c[0]["content"] and any(d in json.dumps(m) and "started from a bundle" in json.dumps(m) for m in c[1:]), r.stdout + r.stderr)
    finally:
        subprocess.run([sys.executable, top + "/hobkin.py", "send", aid, "/stop"], env={**os.environ, "HOBKIN_HOME": top + "/home"})

    # a shared incident: --with joins them; each sees the other's work; a teammate's hold stops a command once
    alerts_file = os.path.join(BASE, "team-alerts.txt")
    h = home("team", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake", "key_env": "TESTKEY"},
             {"idle_prompt": None, "alert_command": f'echo "$HOBKIN_ALERT" >> {alerts_file}'})
    pf, _, df = start(h, "fixer"); ps, _, ds = start(h, "saver")
    tenv = {**os.environ, "HOBKIN_HOME": h, "TESTKEY": "test-key"}
    def hob_(*a): return subprocess.run([sys.executable, HOB, *a], env=tenv, capture_output=True, text=True, timeout=150)
    hob_("breakglass", "fixer", "api returning 500s", "--for", "1h")
    r = hob_("breakglass", "saver", "--for", "1h", "--with", "fixer")
    gf, gs = (json.load(open(x + "/config.json"))["break_glass"] for x in (df, ds))
    check("[team] --with: one incident; the salvager salvages, the fixer keeps its reason and joins",
          gf.get("incident") and gf.get("incident") == gs.get("incident") and gs.get("salvage") and gf["reason"] == "api returning 500s"
          and "joins incident" in r.stdout, r.stdout + r.stderr)
    wait_for(df, "Others on this incident", 20, file="context.jsonl")
    say(ds, 'call: hold {"what": "dumping the orders db: do not restart postgres", "match": "restart postgres"}')
    wait_for(ds, "Hold #1 set", file="context.jsonl")
    check("[team] each is told who else is on it, with their timeline", "**saver**: salvage; its timeline: " + ds in ctx(df)[0]["content"]
          and "**fixer**: api returning 500s" in ctx(ds)[0]["content"], ctx(df)[0]["content"][-700:])
    check("[team] the hold tool is offered only on a shared incident",
          '"name": "hold"' in [l for l in open(LOG) if '"path"' in l][-1])
    say(df, "run: echo restart postgres > restarted.txt"); wait_for(df, "has a hold on this", file="context.jsonl")
    check("[team] a teammate's hold stops a matching command once, and says whose and why",
          not os.path.exists(df + "/work/restarted.txt") and "dumping the orders db" in json.dumps(last_results(df)))
    check("[team] the fixer hears what the salvager did since its last look",
          "on this incident since your last look" in json.dumps(last_results(df)) and "hold #1: dumping the orders db" in json.dumps(last_results(df)),
          json.dumps(last_results(df))[:600])
    n = open(df + "/out").read().count("Done. Output was")
    say(df, "run: echo restart postgres > restarted.txt")
    for _ in range(100):
        if open(df + "/out").read().count("Done. Output was") > n: break
        time.sleep(0.1)
    for _ in range(50):
        if os.path.exists(df + "/work/restarted.txt") and "overrode saver's hold #1" in read_or_empty(alerts_file): break
        time.sleep(0.1)
    check("[team] the same command again goes through, alerted and on the record",
          os.path.exists(df + "/work/restarted.txt") and "overrode saver's hold #1" in read_or_empty(alerts_file), read_or_empty(alerts_file))
    say(df, "run: echo sudo rm -rf /var/lib/old"); wait_for(df, "Done. Output was: sudo rm", file="out")
    r = hob_("watch", "fixer", "--once")
    w = r.stdout
    check("[watch] one screen: the whole incident from one id, both banners, the holds",
          "incident " + gf["incident"] in w and " BREAK GLASS " in w and " SALVAGE " in w and "NO APPROVALS" in w
          and "acting as" in w and "#1 saver: dumping the orders db" in w, w + r.stderr)
    check("[watch] one feed: commands, holds, overrides; risky-looking commands flagged; totals",
          "saver" in w and "OVERRODE saver's hold #1" in w and "looks like: privileged, deletes" in w
          and "1 holds overridden" in w and "\x1b[" not in w, w)
    check("[watch] risky() guesses from the command line", hbk.risky("sudo systemctl restart nginx") == ["privileged", "service change"]
          and hbk.risky("aws s3 cp dump.sql s3://b/") == ["leaves the machine"] and hbk.risky("ls -la > /dev/null") == [])
    hob_("breakglass", "saver", "--off")
    for _ in range(100):
        if json.load(open(os.path.join(h, "_incidents", gf["incident"], "holds.json"))) == []: break
        time.sleep(0.2)
    check("[team] its holds are released when it's over",
          json.load(open(os.path.join(h, "_incidents", gf["incident"], "holds.json"))) == [])
    r = hob_("watch", "--once")
    check("[watch] with no one named: whoever is in break-glass mode", "fixer" in r.stdout and " SALVAGE " not in r.stdout, r.stdout)
    pf.send_signal(signal.SIGTERM); ps.send_signal(signal.SIGTERM); pf.wait(10); ps.wait(10)

    # systemd user services (only where a user session exists; skipped on CI runners)
    if subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0:
        h = home("svc", {"api": "anthropic", "base_url": "http://127.0.0.1:48701", "model": "fake",
                         "key_file": os.path.join(BASE, "svc-key")})
        open(os.path.join(BASE, "svc-key"), "w").write("test-key\n")
        env = {**os.environ, "HOBKIN_HOME": h}
        hob = lambda *a: subprocess.run([sys.executable, HOB, *a], env=env, capture_output=True, text=True, timeout=60)
        r = hob("service", "add", "svc1")
        unit_line = [l for l in r.stdout.splitlines() if "svc1: service" in l]
        template = [l.split("wrote ", 1)[1] for l in r.stdout.splitlines() if l.startswith("wrote ")]
        try:
            check("[service] add starts it", "svc1: service active" in r.stdout, r.stdout + r.stderr)
            check("[service] its own unit name for a non-default home", template and "/hobkin-" in template[0]
                  and f'HOBKIN_HOME={os.path.realpath(h)}' in open(template[0]).read(), template)
            unit = unit_line[0].split("(")[1].split(")")[0]
            d = os.path.join(h, "svc1")
            r = subprocess.run([sys.executable, HOB, "chat", "svc1"], input="hi service\n", env=env, capture_output=True, text=True, timeout=30)
            check("[service] the agent answers", "echo: hi service" in r.stdout, r.stdout + r.stderr)
            check("[service] list shows it", "service " in hob("list").stdout)
            say(d, "/stop"); time.sleep(2)
            state = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True).stdout.strip()
            check("[service] /stop stops it and it stays stopped", state == "inactive", state)
            r = hob("service", "add", "svc1")
            check("[service] add again starts it again", "svc1: service active" in r.stdout, r.stdout + r.stderr)
            r = hob("service", "remove", "svc1")
            state = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True).stdout.strip()
            wants = os.path.expanduser("~/.config/systemd/user/default.target.wants/" + unit)
            check("[service] remove stops it and takes it off boot", state == "inactive" and not os.path.lexists(wants), state)
            p, aid, d2 = start(h, "svc2")
            r = hob("service", "add", "svc2")
            check("[service] refuses an agent already running outside systemd", r.returncode != 0 and "already running outside systemd" in r.stderr, r.stderr)
            p.send_signal(signal.SIGTERM); p.wait(10)
        finally:
            hob("service", "remove", "svc1")
            for t in template:
                os.remove(t)
            subprocess.run(["systemctl", "--user", "daemon-reload"])
    else:
        print("SKIP [service] no systemd user session here")
    check("no invalid requests at all", "INVALID" not in open(LOG).read(), [l for l in open(LOG) if "INVALID" in l][:3])
finally:
    for p in procs:
        if p.poll() is None: p.kill()
    shutil.rmtree(BASE, ignore_errors=True)
print("FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
