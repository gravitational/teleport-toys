"""Zero-dependency MCP server (Streamable HTTP transport, JSON responses).

Why no SDK: the official `mcp` package pulls in pydantic-core (Rust), which has
no armv7 wheels for Arch's Python and takes forever to build on a Pi 3 with
1 GB RAM. MCP over Streamable HTTP is just JSON-RPC over POST, so stdlib is enough.

Binds to 127.0.0.1 only: the ONLY way in is through the local Teleport
Application Service (uri: mcp+http://127.0.0.1:8000/mcp), so every call is
authenticated, RBAC-filtered and audited by Teleport.
"""
import base64
import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import registry, safety

log = logging.getLogger("mcp")

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "beambot-sim-node", "version": "0.1.0"}

LAST_CALLER = {"value": None}
_call_lock = threading.Lock()  # one actuation at a time per node


def caller_from_headers(headers):
    """Best-effort identity of who is calling, from the Teleport-signed JWT.

    Teleport injects it when the app has
      rewrite: {headers: ["Authorization: Bearer {{internal.jwt}}"]}
    NOTE: decoded, not verified (stretch goal: verify against
    https://<proxy>/.well-known/jwks.json). Safe-ish because we only listen on
    localhost behind Teleport, but treat this as display/audit only.
    """
    tok = None
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
    tok = tok or headers.get("Teleport-Jwt-Assertion")
    if not tok or tok.count(".") != 2:
        return None
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return {
            "username": claims.get("username") or claims.get("sub"),
            "roles": claims.get("roles"),
            "traits": {k: v for k, v in (claims.get("traits") or {}).items() if k in ("logins",)},
        }
    except Exception:
        return None


def rpc_result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def rpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle_message(msg, caller, instructions):
    """Returns a response dict for requests, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return rpc_error(None, -32600, "invalid request")
    method = msg.get("method")
    id_ = msg.get("id")
    is_notification = "id" not in msg
    params = msg.get("params") or {}

    if method is None:  # a response from the client; we never send requests
        return None
    if is_notification:
        return None

    if method == "initialize":
        req = params.get("protocolVersion")
        ver = req if req in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return rpc_result(id_, {
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": instructions,
        })
    if method == "ping":
        return rpc_result(id_, {})
    if method == "tools/list":
        return rpc_result(id_, {"tools": registry.public_list()})
    if method == "tools/call":
        return rpc_result(id_, call_tool(params.get("name"), params.get("arguments"), caller))
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return rpc_result(id_, {key: []})
    return rpc_error(id_, -32601, f"method not found: {method}")


def call_tool(name, arguments, caller):
    t = registry.TOOLS.get(name)
    who = (caller or {}).get("username") or "unknown"
    if not t:
        return {"isError": True, "content": [{"type": "text", "text": f"unknown tool: {name}"}]}
    started = time.monotonic()
    try:
        args = registry.validate(t["inputSchema"], arguments)
        if name.startswith("act_"):
            safety.require_armed()
            with _call_lock:
                out = t["fn"](**args)
        else:
            kw = dict(args)
            if name.startswith(("admin_", "safety_")):
                kw["_caller"] = who
            out = t["fn"](**kw)
        res = {"content": registry.to_content(out), "isError": False}
    except registry.ToolError as e:
        res = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:  # hardware faults etc.
        log.exception("tool %s crashed", name)
        res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    ms = int((time.monotonic() - started) * 1000)
    LAST_CALLER["value"] = {"username": who, "tool": name, "at": time.time()}
    log.info("tool=%s caller=%s args=%s error=%s ms=%d",
             name, who, json.dumps(arguments or {}), res["isError"], ms)
    return res


def make_handler(instructions, path="/mcp"):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "beambot-sim-mcp/0.1"

        def log_message(self, fmt, *a):
            log.debug("%s %s", self.address_string(), fmt % a)

        def _send(self, code, body=None, headers=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/healthz"):
                return self._send(200, {"ok": True, "role": os.environ.get("ROBOT_ROLE"),
                                        "estop": safety.state()})
            # We don't offer a server->client SSE stream; spec says 405 is fine.
            self._send(405, None, {"Allow": "POST, DELETE"})

        def do_DELETE(self):
            self._send(200, None)

        def do_POST(self):
            if self.path.split("?")[0].rstrip("/") not in (path.rstrip("/"), ""):
                return self._send(404, rpc_error(None, -32600, "not found"))
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, rpc_error(None, -32700, "parse error"))

            caller = caller_from_headers(self.headers)
            msgs = body if isinstance(body, list) else [body]
            responses = [r for r in (handle_message(m, caller, instructions) for m in msgs) if r]

            headers = {}
            if any(isinstance(m, dict) and m.get("method") == "initialize" for m in msgs):
                headers["Mcp-Session-Id"] = uuid.uuid4().hex
            elif self.headers.get("Mcp-Session-Id"):
                headers["Mcp-Session-Id"] = self.headers["Mcp-Session-Id"]

            if not responses:
                return self._send(202, None, headers)
            out = responses if isinstance(body, list) else responses[0]
            self._send(200, out, headers)

    return Handler


def serve(host, port, instructions):
    httpd = ThreadingHTTPServer((host, port), make_handler(instructions))
    httpd.daemon_threads = True
    log.info("MCP listening on http://%s:%d/mcp with %d tools", host, port, len(registry.TOOLS))
    httpd.serve_forever()
