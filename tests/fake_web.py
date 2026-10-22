"""A small web for the tests: pages, a long page, JSON, an image, a meta refresh, a SAML-style SSO
login (redirect, auto-submitting form, cookie), DuckDuckGo-like results and its bot check, and an
HTTPS host that requires a client certificate.

    fake_web.py HTTP_PORT HTTPS_PORT CERT_DIR      (CERT_DIR holds ca.pem, server.pem, server.key)
"""
import json, os, ssl, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d4944415478da63fccf00000301010018dd8db40000000049454e44ae426082")
PAGE = """<html><head><title>Team Wiki: Deploys</title><style>.x{}</style></head><body>
<nav><a href="/">Home</a></nav><h1>Deploys</h1><p>Run <b>make deploy</b> after reading <a href="/runbook">the runbook</a>.</p>
<script>alert("no")</script><ul><li>check the dashboard</li><li>tell the channel</li></ul></body></html>"""
DDG = """<html><body>
<div class="result results_links"><h2 class="result__title"><a rel="nofollow" class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fhtml.parser.html&amp;rut=abc">html.parser &mdash; Simple HTML and XHTML parser</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">This module defines a class <b>HTMLParser</b> which serves as the basis for parsing.</a></div>
<div class="result result--ad"><a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=ads.example">Buy parsers</a></div>
<div class="result results_links"><h2 class="result__title"><a rel="nofollow" class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fparsing&amp;rut=def">Parsing HTML in Python</a></h2>
<a class="result__snippet" href="x">A gentle guide to <b>parsing</b>.</a></div></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = urllib.parse.parse_qs(query)
        if getattr(self.server, "tls", False):        # the client-certificate host
            cert = self.connection.getpeercert() or {}
            cn = dict(x[0] for x in cert.get("subject", ())).get("commonName", "?")
            return self.send(200, f"<html><body><p>Internal page, read with the certificate of {cn}.</p></body></html>")
        if path == "/page":
            return self.send(200, PAGE)
        if path == "/long":
            return self.send(200, "".join(f"line {i}\n" for i in range(1, 1001)), "text/plain")
        if path == "/data.json":
            return self.send(200, json.dumps({"team": "blue", "deploys": [1, 2]}), "application/json")
        if path == "/img.png":
            return self.send(200, PNG, "image/png")
        if path == "/refresh":
            return self.send(200, '<html><head><meta http-equiv="refresh" content="0; url=/page"></head><body>Moving...</body></html>')
        if path == "/wiki/secret":
            if "session=ok" in (self.headers.get("cookie") or ""):
                return self.send(200, "<html><body><h1>Secret</h1><p>The deploy password is in the vault.</p></body></html>")
            return self.send(302, "", headers={"Location": "/sso/login?return=/wiki/secret"})
        if path == "/sso/login":
            back = q.get("return", ["/"])[0]
            return self.send(200, f"""<html><body onload="document.forms[0].submit()"><noscript>JavaScript is off: press Continue.</noscript>
<form method="post" action="/sso/acs"><input type="hidden" name="SAMLResponse" value="signed-assertion">
<input type="hidden" name="RelayState" value="{back}"><input type="submit" value="Continue"></form></body></html>""")
        if path in ("/board/api/me", "/board/api/skill"):     # a hearthboard, for hobkin --hearthboard
            if self.headers.get("authorization") != "Bearer hb-good":
                return self.send(401, json.dumps({"error": "bad token"}), "application/json")
            if path.endswith("/me"):
                return self.send(200, json.dumps({"username": "worker.7", "kind": "agent", "guidance": ""}), "application/json")
            return self.send(200, "---\nname: hearthboard\ndescription: Work with the board\n---\n# Board skill\n", "text/markdown")
        if path == "/ddg":
            return self.send(200, DDG)
        if path == "/ddg-blocked":
            return self.send(200, '<html><body><form id="challenge-form" action="//duckduckgo.com/anomaly.js?cc=botnet">'
                                  '<div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div></form></body></html>')
        self.send(404, "<html><body>Not found</body></html>")

    def do_POST(self):
        data = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("content-length") or 0)).decode())
        if self.path == "/sso/acs" and data.get("SAMLResponse") == ["signed-assertion"]:
            return self.send(302, "", headers={"Location": data.get("RelayState", ["/"])[0], "Set-Cookie": "session=ok; Path=/"})
        self.send(403, "<html><body>Bad login</body></html>")


if __name__ == "__main__":
    http_port, https_port, certs = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    plain = ThreadingHTTPServer(("127.0.0.1", http_port), H)
    secure = ThreadingHTTPServer(("127.0.0.1", https_port), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(certs, "server.pem"), os.path.join(certs, "server.key"))
    ctx.load_verify_locations(os.path.join(certs, "ca.pem"))
    ctx.verify_mode = ssl.CERT_REQUIRED                 # no client certificate, no page
    secure.socket = ctx.wrap_socket(secure.socket, server_side=True)
    secure.tls = True
    threading.Thread(target=secure.serve_forever, daemon=True).start()
    plain.serve_forever()
