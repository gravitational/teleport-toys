"""A fake `tsh` for check_mcp_bypass.sh: `tsh proxy app <app> --port <p>` serves a pretend MCP endpoint locally.

FAKE_MODE picks the behaviour:
  good         tools/list hides resume; a resume call gets Teleport's JSON-RPC RBAC denial        -> PASS
  leaky_list   tools/list shows resume                                                         -> FAIL
  shouty_list  tools/list shows "Resume"                                                       -> FAIL
  leaky_call   the list is clean but the resume call returns a result                           -> FAIL
  latched      the resume call returns a result that says the robot's latch refused it          -> FAIL (it reached the robot)
  odd_error    the resume call fails with an error that is not Teleport's                       -> INCONCLUSIVE
  odd_denied   the resume call fails with another server's error that mentions "access denied"   -> INCONCLUSIVE
  teleport_403 the resume call gets a 403 that mentions teleport but is not its RBAC denial       -> INCONCLUSIVE
  wrong_id     Teleport's RBAC denial, but for another request id                              -> INCONCLUSIVE
  empty_list   tools/list returns nothing                                                      -> INCONCLUSIVE
  wrong_list   the list has tools that are not this robot's                                    -> INCONCLUSIVE
  dead         the proxy listens and hangs up without answering (HTTP 000)                     -> INCONCLUSIVE
  silent       tsh prints nothing, never listens                                               -> INCONCLUSIVE
  refuses      tsh exits with an error                                                         -> INCONCLUSIVE
  denied_init  initialize gets a 403 with Teleport's text                                      -> PASS
  expired_init initialize gets a 403 "Teleport session expired: run tsh login"                 -> INCONCLUSIVE
  forward      a plain proxy with no tool filter, forwarding to the real MCP endpoint in FAKE_UPSTREAM (the bypass)
  forward_hide like forward, but tools/list hides resume (a list filter with no call filter)
FAKE_SSE=1 answers as an SSE stream with a log line and Teleport's denial of another request id before the answer.
FAKE_CALLS=<file> appends "<tool name><TAB><arguments as JSON>" for every tools/call the fake receives.
FAKE_STREAM=missing makes `proxy app picar-stream` say the app is not found.
"""
import json
import os
import socketserver
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

MODE = os.environ.get("FAKE_MODE", "good")
DENY = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "RBAC is enforced by your Teleport roles. "
        "Contact your Teleport Admin for more details.", "data": {"error": {"message": "access to resource denied."}}}}


def deny(rid):
    return {**DENY, "id": rid}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj=None, raw=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        ctype = "application/json"
        if raw is None and "id" in obj and os.environ.get("FAKE_SSE") == "1":
            log = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "working"}}
            data = b"".join(b"event: message\r\ndata: " + json.dumps(m).encode() + b"\r\n\r\n" for m in (log, deny(99), obj))
            ctype = "text/event-stream"
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("mcp-session-id", "fake-session")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def forward(self, raw, method):
        hdr = {k: v for k, v in self.headers.items() if k.lower() in ("content-type", "accept", "mcp-session-id", "mcp-protocol-version")}
        req = urllib.request.Request(os.environ["FAKE_UPSTREAM"], data=raw, headers=hdr, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            r = e
        data = r.read()
        if MODE == "forward_hide" and method == "tools/list":
            def hide(line):
                if not line.startswith(b"data:"):
                    return line
                msg = json.loads(line[5:])
                msg["result"]["tools"] = [t for t in msg["result"]["tools"] if t["name"] != "resume"]
                return b"data: " + json.dumps(msg).encode()
            data = b"\n".join(hide(x) for x in data.split(b"\n"))
        self.send_response(r.status)
        for k, v in r.headers.items():
            if k.lower() in ("content-type", "mcp-session-id"):
                self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.loads(raw or b"{}")
        if self.server.stream:
            return self.reply(404, raw=b"not found")
        if body.get("method") == "tools/call" and os.environ.get("FAKE_CALLS"):
            with open(os.environ["FAKE_CALLS"], "a") as f:
                f.write(body["params"]["name"] + "\t" + json.dumps(body["params"].get("arguments")) + "\n")
        if MODE.startswith("forward"):
            return self.forward(raw, body.get("method"))
        if MODE == "dead":
            self.connection.close()
            return
        m = body.get("method")
        if m == "initialize":
            if MODE == "denied_init":
                return self.reply(403, raw=b"access denied: RBAC is enforced by your Teleport roles")
            if MODE == "expired_init":
                return self.reply(403, raw=b"Teleport session expired: run tsh login")
            return self.reply(200, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}}})
        if m == "tools/list":
            names = {"empty_list": [], "wrong_list": ["foo", "bar"], "leaky_list": ["drive", "get_telemetry", "resume", "snapshot", "stop"],
                     "shouty_list": ["drive", "get_telemetry", "Resume", "snapshot", "stop"]}.get(
                MODE, ["drive", "get_telemetry", "look", "say", "snapshot", "stop"])
            return self.reply(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": [{"name": n} for n in names]}})
        if m == "tools/call" and body["params"]["name"] == "get_telemetry":   # a tool every PiCar role has
            t = {"moving": False, "halted": False}
            return self.reply(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {"content": [{"type": "text", "text": json.dumps(t)}], "isError": False}})
        if m == "tools/call":
            rid = body.get("id")
            if MODE == "leaky_call" or MODE == "leaky_list":
                return self.reply(200, {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "driving"}], "isError": False}})
            if MODE == "latched":
                return self.reply(200, {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text",
                                    "text": "Error executing tool resume: refused: emergency stop is latched"}], "isError": True}})
            if MODE == "odd_error":
                return self.reply(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "internal error"}})
            if MODE == "odd_denied":
                return self.reply(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "internal error",
                                                                              "data": "upstream said: access denied"}})
            if MODE == "teleport_403":
                return self.reply(403, raw=b"missing teleport-jwt-assertion header")
            if MODE == "wrong_id":
                return self.reply(200, deny(7))
            return self.reply(200, deny(rid))
        return self.reply(202, {})


class S(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv):
    if argv[:2] != ["proxy", "app"]:
        print(f"fake tsh: unsupported {argv}", file=sys.stderr)
        return 1
    app, port = argv[2], int(argv[argv.index("--port") + 1])
    if MODE == "refuses":
        print("ERROR: MCP applications cannot be proxied as plain HTTP", file=sys.stderr)
        return 1
    if os.environ.get("FAKE_STREAM") == "missing" and app == "picar-stream":
        print(f'ERROR: app "{app}" not found, use `tsh apps ls` to see registered apps', file=sys.stderr)
        return 1
    if MODE == "silent" and app != "picar-stream":
        time.sleep(60)
        return 0
    srv = S(("127.0.0.1", port), H)
    srv.stream = app == "picar-stream"
    print(f"Proxying connections to {app} on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
