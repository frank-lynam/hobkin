"""Fake model API for the tests: Anthropic- and OpenAI-shaped, and strict about message
structure (alternating roles, every tool call answered). Usage: fake_api.py PORT CONTEXT [list|nolist]"""
import json, sys, re, time, math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
CTX = int(sys.argv[2]) if len(sys.argv) > 2 else 100000
LIST_CTX = sys.argv[3] != "nolist" if len(sys.argv) > 3 else True
import os
HICCUPS = {}
RATE = {"until": 0.0, "mode": None, "count": 0, "seen": set()}
LOG = open(os.environ.get("FAKE_API_LOG", "fake_api.log"), "a")
def flat(content):
    """A tool result or user content as text (lists hold text and image parts)."""
    if isinstance(content, str): return content
    return " ".join(b.get("text", "") if b.get("type") == "text" else "[" + b.get("type", "") + "]" for b in content)
def last_text(blocks):
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
def decide(last_user_texts, tool_results, summary):
    if summary: return ("text", "SUMMARY: handoff note")
    if tool_results: return ("text", "Done. Output was: " + tool_results[-1][:60].replace("\n", " "))
    t = re.sub(r"(?m)^\[(message from [^\]]*|from hobkin[^\]]*)\]\n", "", last_user_texts)   # sender labels
    t = re.sub(r"(?m)\n?^\[hobkin: [^\n]*\]$", "", t)                                          # nudges
    m = re.search(r"noplan-call:\s*(\w+)\s+(\{.*\})\s*$", t, re.S)   # a call without saying a plan first
    if m: return ("noplan", (m.group(1), json.loads(m.group(2))))
    m = re.search(r"call:\s*(\w+)\s+(\{.*\})\s*$", t, re.S)      # call: TOOL {json input}
    if m: return ("tool", (m.group(1), json.loads(m.group(2))))
    m = re.search(r"silent-run:\s*(.+)", t, re.S)                 # a tool call with no words at all
    if m: return ("silent", ("bash", {"command": m.group(1).strip()}))
    m = re.search(r"run:\s*(.+)", t, re.S)
    if m: return ("tool", ("bash", {"command": m.group(1).strip()}))
    return ("text", "echo: " + t.strip()[:80])
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj, headers=None):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("content-type", "application/json")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/set_ctx/"):          # the window changes (a server restarted)
            global CTX; CTX = int(self.path.split("/")[-1]); return self.send(200, {"ctx": CTX})
        if self.path == "/stats":
            return self.send(200, {"rate_429s": RATE["count"]})
        if self.path.startswith("/v1/models/"):
            return self.send(200, {"id": self.path.split("/")[-1], "max_input_tokens": CTX, "max_tokens": 4096})
        if self.path == "/v1/models":
            return self.send(200, {"data": [{"id": m, **({"context_length": CTX} if LIST_CTX else {})} for m in ("fake", "fake-costly")]})
        self.send(404, {"error": "nope"})
    def do_POST(self):
        try:
            return self._post()
        except AssertionError as e:
            LOG.write(json.dumps({'INVALID': str(e)}) + '\n'); LOG.flush()
            return self.send(422, {'error': {'message': 'INVALID: ' + str(e)}})
    def _post(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        LOG.write(json.dumps({"path": self.path, "body": body}) + "\n"); LOG.flush()
        if body.get("model") == "fake-gone":         # a model that's been withdrawn
            return self.send(404, {"error": {"message": "No endpoints found for fake-gone.", "code": 404}})
        size = len(json.dumps(body)) // 4
        # Rate limiting like a real provider: "ratelimit:SECONDS:MODE" in a message blocks every request
        # for SECONDS; MODE says how the reset is given (header / epoch / body / none)
        m = re.search(r"ratelimit:(\d+):(\w+)", json.dumps(body.get("messages", [])[-1:]))
        if m and m.group(0) not in RATE["seen"]:
            RATE["seen"].add(m.group(0)); RATE["until"] = time.time() + int(m.group(1)); RATE["mode"] = m.group(2)
        if time.time() < RATE["until"]:
            RATE["count"] += 1
            left, mode = RATE["until"] - time.time(), RATE["mode"]
            headers = {"Retry-After": str(math.ceil(left))} if mode == "header" else \
                      {"X-RateLimit-Reset": str(int(RATE["until"] * 1000))} if mode == "epoch" else {}
            err = {"code": 429, "message": "Rate limit exceeded"}
            if mode == "body":
                err["metadata"] = {"headers": {"X-RateLimit-Reset": str(int(RATE["until"] * 1000))}}
            return self.send(429, {"type": "error", "error": {"type": "rate_limit_error", **err}}
                             if self.path == "/v1/messages" else {"error": err}, headers)
        if "hiccup" in json.dumps(body.get("messages", [])[-1:]) and not HICCUPS.get(self.path):
            HICCUPS[self.path] = True                  # once per API: an error inside a 200 response
            return self.send(200, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
                             if self.path == "/v1/messages" else
                             {"id": "gen-1", "error": {"message": "Upstream idle timeout exceeded", "code": 504}})
        if self.path == "/v1/messages":
            assert self.headers.get("x-api-key") == "test-key", "missing key"
            msgs = body["messages"]; prev_calls = set()
            for i, m in enumerate(msgs):
                assert m["role"] == ("user" if i % 2 == 0 else "assistant"), f"roles must alternate at {i}"
                blocks = m["content"]
                if m["role"] == "user":
                    ids = [b["tool_use_id"] for b in blocks if b["type"] == "tool_result"]
                    assert set(ids) == prev_calls, f"tool results {ids} don't match calls {prev_calls} at {i}"
                    prev_calls = set()
                else:
                    prev_calls = {b["id"] for b in blocks if b["type"] == "tool_use"}
            assert not prev_calls, "last assistant tool calls unanswered"
            if size > CTX:
                return self.send(400, {"type": "error", "error": {"type": "invalid_request_error", "message": f"prompt is too long: {size} tokens > {CTX} maximum"}})
            last = msgs[-1]["content"]
            kind, val = decide(last_text(last), [flat(b["content"]) for b in last if b["type"] == "tool_result"], body.get("tool_choice", {}).get("type") == "none")
            content = [{"type": "text", "text": val}] if kind == "text" else ([] if kind == "silent" else [{"type": "text", "text": "Running it." if kind == "noplan" else "**What would:** running it."}]) + [{"type": "tool_use", "id": f"toolu_{len(msgs)}", "name": val[0], "input": val[1]}]
            return self.send(200, {"content": content, "stop_reason": "tool_use" if kind == "tool" else "end_turn", "usage": {"input_tokens": size}})
        if self.path == "/v1/chat/completions":
            msgs = body["messages"]; assert msgs[0]["role"] == "system"; pending = set()
            for i, m in enumerate(msgs[1:], 1):
                if m["role"] == "tool":
                    assert m["tool_call_id"] in pending, f"stray tool result at {i}"; pending.discard(m["tool_call_id"])
                else:
                    assert not pending, f"unanswered calls before {i}"
                    if m["role"] == "assistant": pending = {c["id"] for c in m.get("tool_calls") or []}
            if size > CTX:
                return self.send(400, {"error": {"message": f"This model's maximum context length is {CTX} tokens. However, you requested {size} tokens."}})
            tail = []; 
            for m in reversed(msgs):
                if m["role"] == "assistant": break
                tail.insert(0, m)
            texts = "\n".join(flat(m["content"]) for m in tail if m["role"] == "user")
            kind, val = decide(texts, [m["content"] for m in tail if m["role"] == "tool"], body.get("tool_choice") == "none")
            msg = {"role": "assistant", "content": val} if kind == "text" else {"role": "assistant", "content": None if kind == "silent" else ("Running it." if kind == "noplan" else "**What would:** running it."), "tool_calls": [{"id": f"call_{len(msgs)}", "type": "function", "function": {"name": val[0], "arguments": json.dumps(val[1])}}]}
            return self.send(200, {"choices": [{"message": msg, "finish_reason": "tool_calls" if kind == "tool" else "stop"}], "usage": {"prompt_tokens": size, **({"cost": 0.4} if body.get("model") == "fake-costly" else {})}})
        self.send(404, {})
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
