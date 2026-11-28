"""Tool registry + tiny JSON-schema validator (stdlib only).

Naming convention is load-bearing: Teleport RBAC filters MCP tools by name.
  sense_*   read-only (camera, sensors, status)        -> beambot-sim-observer
  act_*     moves hardware                             -> beambot-sim-operator
  safety_*  can only make the robot safer (stop/estop) -> everyone
  admin_*   clears the e-stop                          -> beambot-sim-admin
"""
import json
import math

TOOLS = {}
PREFIXES = ("sense_", "act_", "safety_", "admin_")


class ToolError(Exception):
    """Raised by a tool to return an MCP result with isError=true."""


def tool(name, description, properties=None, required=None):
    if not name.startswith(PREFIXES):
        raise ValueError(f"tool {name!r} must start with one of {PREFIXES}")

    def deco(fn):
        TOOLS[name] = {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": name.startswith("sense_"),
                "destructiveHint": name.startswith(("act_", "admin_")),
            },
            "fn": fn,
        }
        return fn

    return deco


def public_list():
    return [{k: v for k, v in t.items() if k != "fn"} for t in TOOLS.values()]


_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate(schema, args):
    """Validate args against the small schema subset we use. Returns cleaned args."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    props = schema.get("properties", {})
    for r in schema.get("required", []):
        if r not in args:
            raise ToolError(f"missing required argument: {r}")
    out = {}
    for k, v in args.items():
        if k not in props:
            raise ToolError(f"unknown argument: {k}")
        p = props[k]
        t = p.get("type")
        if t:
            py = _TYPES[t]
            # bool is a subclass of int; reject it for numeric fields
            if isinstance(v, bool) and t in ("integer", "number"):
                raise ToolError(f"{k} must be {t}")
            if t == "integer" and isinstance(v, float) and v.is_integer():
                v = int(v)
            if not isinstance(v, py):
                raise ToolError(f"{k} must be {t}, got {type(v).__name__}")
            if isinstance(v, float) and not math.isfinite(v):  # NaN passes every < and > check below
                raise ToolError(f"{k} must be a finite number")
        if "enum" in p and v not in p["enum"]:
            raise ToolError(f"{k} must be one of {p['enum']}")
        if "minimum" in p and v < p["minimum"]:
            raise ToolError(f"{k} must be >= {p['minimum']}")
        if "maximum" in p and v > p["maximum"]:
            raise ToolError(f"{k} must be <= {p['maximum']}")
        out[k] = v
    for k, p in props.items():
        if k not in out and "default" in p:
            out[k] = p["default"]
    return out


def to_content(result):
    """Normalize a tool's return value into MCP content blocks."""
    if isinstance(result, list) and all(isinstance(b, dict) and "type" in b for b in result):
        return result
    if isinstance(result, str):
        return [{"type": "text", "text": result}]
    return [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
