"""MCP server built on the OFFICIAL Python SDK (mcp>=1.10,<2), Streamable HTTP.

Uses the SDK's low-level Server so the hand-written JSON schemas in the role
modules (enums, min/max limits) are published exactly as written; the SDK also
validates inputs against them with jsonschema before our code runs.

Same tools, same safety layer, same port as the stdlib server (server.py), which
stays as a fallback: set MCP_IMPL=stdlib.
"""
import contextlib
import logging
import os

import anyio
import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import registry, safety
from .server import call_tool as dispatch, caller_from_headers

log = logging.getLogger("mcp-sdk")


class _DropClosedResource(logging.Filter):
    """mcp<=1.19 (what resolves for armv7/cp314) logs a harmless ClosedResourceError
    traceback after every stateless request. Keep journald readable on the Pi."""

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        return not (exc is not None and type(exc).__name__ == "ClosedResourceError")


logging.getLogger("mcp.server.streamable_http").addFilter(_DropClosedResource())


def build_app(instructions):
    app = Server("beambot-sim-node", version="0.2.0", instructions=instructions)

    @app.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
                annotations=types.ToolAnnotations(**t["annotations"]),
            )
            for t in registry.TOOLS.values()
        ]

    @app.call_tool()
    async def call_tool(name, arguments):
        caller = None
        try:
            req = app.request_context.request  # starlette Request (HTTP transport)
            if req is not None:
                caller = caller_from_headers(req.headers)
        except LookupError:
            pass
        # Tools block (servo slews, timed drive bursts): keep the event loop free
        # so safety_stop / sense_* calls still get through mid-motion.
        res = await anyio.to_thread.run_sync(dispatch, name, arguments, caller)
        return types.CallToolResult.model_validate(res)

    return app


def serve(host, port, instructions):
    app = build_app(instructions)
    # stateless + JSON responses: simplest thing for Teleport to proxy, no sticky sessions
    manager = StreamableHTTPSessionManager(app=app, json_response=True, stateless=True)

    class MCPEndpoint:  # a class instance => Starlette treats it as a raw ASGI app (no /mcp -> /mcp/ redirect)
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    async def healthz(_request):
        return JSONResponse({"ok": True, "impl": "sdk", "role": os.environ.get("ROBOT_ROLE"),
                             "estop": safety.state()})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            log.info("MCP (official SDK) on http://%s:%d/mcp with %d tools",
                     host, port, len(registry.TOOLS))
            yield

    starlette_app = Starlette(
        routes=[Route("/healthz", healthz), Route("/mcp", MCPEndpoint(), methods=["GET", "POST", "DELETE"])],
        lifespan=lifespan,
    )
    uvicorn.run(starlette_app, host=host, port=port, log_level="warning",
                access_log=False, lifespan="on")
