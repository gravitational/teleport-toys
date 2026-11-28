"""Entry point: python3 -m robot_mcp  (config via env, see /etc/beambot-sim.env)."""
import importlib
import logging
import os
import signal
import sys

from . import safety, server

ROLES = {
    "safety": "Safety supervisor: physical e-stop button, buzzer/LED signalling.",
    "vision": "Eyes: camera capture for the agent to look at the scene.",
    "drive": "Legs: drive base (forward/back/turn), always time-limited.",
    "arm": "Hands: servo arm + gripper with per-joint angle limits.",
    "sensors": "Senses: distance (ultrasonic) and IMU.",
}


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    role = os.environ.get("ROBOT_ROLE")
    if role not in ROLES:
        sys.exit(f"ROBOT_ROLE must be one of {list(ROLES)}, got {role!r}")
    sim = os.environ.get("ROBOT_SIM", "1") == "1"

    from . import common  # noqa: F401  (registers shared tools)
    importlib.import_module(f".roles.{role}", __package__)

    safety.start_listener()
    if role == "safety":
        safety.start_heartbeat()  # the other nodes refuse to move when they stop hearing it

    def shutdown(*_):
        safety.stop_all_local()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)

    instructions = (
        f"This is the '{role}' node of BeamBot, a physical robot. {ROLES[role]} "
        f"{'SIMULATED hardware. ' if sim else ''}"
        "Tool prefixes: sense_* read-only, act_* moves hardware (time/angle-limited on the node), "
        "safety_* always allowed. If anything looks wrong call safety_stop."
    )
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8000"))
    impl = os.environ.get("MCP_IMPL", "sdk")
    if impl == "sdk":
        try:
            from . import sdk_server as impl_mod
        except ImportError as e:  # SDK not installed in this interpreter -> stdlib fallback
            logging.getLogger("main").warning("official MCP SDK unavailable (%s); using stdlib server", e)
            impl_mod = server
    else:
        impl_mod = server
    try:
        impl_mod.serve(host, port, instructions)
    finally:
        safety.stop_all_local()


if __name__ == "__main__":
    main()
