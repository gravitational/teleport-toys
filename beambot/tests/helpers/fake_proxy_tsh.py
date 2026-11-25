"""A fake `tsh` for check_mcp_bypass.sh: `tsh proxy app <app> --port <p>` serves a pretend MCP endpoint locally.

FAKE_MODE picks the behaviour:
  good         tools/list hides resume; a resume call gets Teleport's JSON-RPC RBAC denial        -> PASS
  leaky_list   tools/list shows resume                                                         -> FAIL
  leaky_call   the list is clean but the resume call returns a result                           -> FAIL
  latched      the resume call returns a result that says the robot's latch refused it          -> FAIL (it reached the robot)
  odd_error    the resume call fails with an error that is not Teleport's                       -> INCONCLUSIVE
  empty_list   tools/list returns nothing                                                      -> INCONCLUSIVE
  wrong_list   the list has tools that are not this robot's                                    -> INCONCLUSIVE
  dead         the proxy listens and hangs up without answering (HTTP 000)                     -> INCONCLUSIVE
  silent       tsh prints nothing, never listens                                               -> INCONCLUSIVE
  refuses      tsh exits with an error                                                         -> INCONCLUSIVE
  denied_init  initialize gets a 403 with Teleport's text                                      -> PASS
FAKE_STREAM=missing makes `proxy app picar-stream` say the app is not found.
"""
import json
import os
import socketserver
import sys
import time
from http.server import BaseHTTPRequestHandler

MODE = os.environ.get("FAKE_MODE", "good")
DENY = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "RBAC is enforced by your Teleport roles. "
        "Contact your Teleport Admin for more details.", "data": {"error": {"message": "access to resource denied."}}}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj=None, raw=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("mcp-session-id", "fake-session")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        if self.server.stream:
            return self.reply(404, raw=b"not found")
        if MODE == "dead":
            self.connection.close()
            return
        m = body.get("method")
        if m == "initialize":
            if MODE == "denied_init":
                return self.reply(403, raw=b"access denied: RBAC is enforced by your Teleport roles")
            return self.reply(200, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}}})
        if m == "tools/list":
            names = {"empty_list": [], "wrong_list": ["foo", "bar"], "leaky_list": ["drive", "get_telemetry", "resume", "snapshot", "stop"]}.get(
                MODE, ["drive", "get_telemetry", "look", "say", "snapshot", "stop"])
            return self.reply(200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": n} for n in names]}})
        if m == "tools/call":
            if MODE == "leaky_call" or MODE == "leaky_list":
                return self.reply(200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "driving"}], "isError": False}})
            if MODE == "latched":
                return self.reply(200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text",
                                    "text": "Error executing tool resume: refused: emergency stop is latched"}], "isError": True}})
            if MODE == "odd_error":
                return self.reply(200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32000, "message": "internal error"}})
            return self.reply(200, DENY)
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
