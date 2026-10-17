"""Fake model API for the tests: Anthropic- and OpenAI-shaped, and strict about message
structure (alternating roles, every tool call answered). Usage: fake_api.py PORT CONTEXT [list|nolist]"""
import json, sys, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
CTX = int(sys.argv[2]) if len(sys.argv) > 2 else 100000
LIST_CTX = sys.argv[3] != "nolist" if len(sys.argv) > 3 else True
import os
HICCUPS = {}
LOG = open(os.environ.get("FAKE_API_LOG", "fake_api.log"), "a")
def last_text(blocks):
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
def decide(last_user_texts, tool_results, summary):
    if summary: return ("text", "SUMMARY: handoff note")
    if tool_results: return ("text", "Done. Output was: " + tool_results[-1][:60].replace("\n", " "))
    t = re.sub(r"(?m)^\[(message from [^\]]*|from hobkin[^\]]*)\]\n", "", last_user_texts)   # sender labels
    t = re.sub(r"(?m)\n?^\[hobkin: [^\n]*\]$", "", t)                                          # nudges
    m = re.search(r"call:\s*(\w+)\s+(\{.*\})\s*$", t, re.S)      # call: TOOL {json input}
    if m: return ("tool", (m.group(1), json.loads(m.group(2))))
    m = re.search(r"run:\s*(.+)", t, re.S)
    if m: return ("tool", ("bash", {"command": m.group(1).strip()}))
    return ("text", "echo: " + t.strip()[:80])
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/v1/models/"):
            return self.send(200, {"id": self.path.split("/")[-1], "max_input_tokens": CTX, "max_tokens": 4096})
        if self.path == "/v1/models":
            return self.send(200, {"data": [{"id": "fake", **({"context_length": CTX} if LIST_CTX else {})}]})
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
        size = len(json.dumps(body)) // 4
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
            kind, val = decide(last_text(last), [b["content"] for b in last if b["type"] == "tool_result"], body.get("tool_choice", {}).get("type") == "none")
            content = [{"type": "text", "text": val}] if kind == "text" else [{"type": "text", "text": "Running it."}, {"type": "tool_use", "id": f"toolu_{len(msgs)}", "name": val[0], "input": val[1]}]
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
            texts = "\n".join(m["content"] for m in tail if m["role"] == "user")
            kind, val = decide(texts, [m["content"] for m in tail if m["role"] == "tool"], body.get("tool_choice") == "none")
            msg = {"role": "assistant", "content": val} if kind == "text" else {"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{len(msgs)}", "type": "function", "function": {"name": val[0], "arguments": json.dumps(val[1])}}]}
            return self.send(200, {"choices": [{"message": msg, "finish_reason": "tool_calls" if kind == "tool" else "stop"}], "usage": {"prompt_tokens": size}})
        self.send(404, {})
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
