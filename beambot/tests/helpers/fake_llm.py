"""Scripted stand-in for the model providers the dashboard talks to.

Serves the OpenAI Responses API (POST /v1/responses) and the Anthropic Messages API (POST /v1/messages).
Tests load a script with POST /__script {"steps": [...]}; every model turn pops the next step:
    {"tool": "snapshot", "args": {}}   -> the model asks for a tool call
    {"tools": [{"tool": "drive", "args": {...}}, ...]}   -> several tool calls in one turn
    {"text": "all done"}               -> the model answers and the agent loop ends
Every request body is recorded and readable at GET /__requests.

    python fake_llm.py <port>
"""
import json
import sys
import uuid

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

STATE = {"script": [], "requests": []}


def _next():
    return STATE["script"].pop(0) if STATE["script"] else {"text": "(script exhausted)"}


async def set_script(req):
    STATE["script"] = list((await req.json())["steps"])
    STATE["requests"].clear()
    return JSONResponse({"ok": True})


async def get_requests(req):
    return JSONResponse(STATE["requests"])


async def responses(req):
    body = await req.json()
    STATE["requests"].append({"api": "responses", "body": body})
    step = _next()
    if step.get("redirect"):          # what Teleport does for an identity that can't see the model app
        return Response(status_code=302, headers={"location": "https://example.beams.run/web/launch/openai"})
    if "tool" in step or "tools" in step:
        out = [{"type": "function_call", "call_id": "call_" + uuid.uuid4().hex[:8], "name": c["tool"],
                "arguments": json.dumps(c.get("args", {}))} for c in step.get("tools") or [step]]
    else:
        out = [{"type": "message", "content": [{"type": "output_text", "text": step["text"]}]}]
    return JSONResponse({"output": out})


async def messages(req):
    body = await req.json()
    STATE["requests"].append({"api": "messages", "body": body})
    step = _next()
    if step.get("redirect"):
        return Response(status_code=302, headers={"location": "https://example.beams.run/web/launch/anthropic"})
    calls = step.get("tools") or ([step] if "tool" in step else [])
    if calls:
        content = [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:8], "name": c["tool"],
                    "input": c.get("args", {})} for c in calls]
    else:
        content = [{"type": "text", "text": step["text"]}]
    return JSONResponse({"id": "msg_" + uuid.uuid4().hex[:8], "type": "message", "role": "assistant",
                         "model": body.get("model"), "content": content, "stop_sequence": None,
                         "stop_reason": "tool_use" if calls else "end_turn",
                         "usage": {"input_tokens": 1, "output_tokens": 1}})


app = Starlette(routes=[
    Route("/__script", set_script, methods=["POST"]),
    Route("/__requests", get_requests),
    Route("/v1/responses", responses, methods=["POST"]),
    Route("/v1/messages", messages, methods=["POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
