"""Local safety layer. Enforced on the Pi, never trusted to the agent.

* Latched e-stop: once tripped, every act_* tool is refused until admin_reset_estop.
* LAN broadcast: any node (or the physical button on the safety Pi) can trip
  e-stop on every node via UDP broadcast. The packet can only STOP the robot,
  never move it, so it is deliberately unauthenticated and works even if the
  internet / Teleport / the Beam is down.
* Watchdog helper: timed motions always auto-stop.
"""
import logging
import os
import socket
import threading
import time

log = logging.getLogger("safety")

ESTOP_PORT = int(os.environ.get("ESTOP_PORT", "47474"))
ESTOP_MAGIC = b"BEAMBOT-ESTOP"
MAX_MOTION_MS = int(os.environ.get("MAX_MOTION_MS", "2000"))

_lock = threading.Lock()
_state = {"estopped": False, "reason": None, "since": None, "source": None}
_stop_hooks = []


def register_stop_hook(fn):
    """Role modules register a function that immediately de-energizes actuators."""
    _stop_hooks.append(fn)


def _run_stop_hooks():
    for fn in _stop_hooks:
        try:
            fn()
        except Exception:  # never let one hook block the others
            log.exception("stop hook failed")


def trip(reason, source="local", broadcast=True):
    with _lock:
        first = not _state["estopped"]
        _state.update(estopped=True, reason=reason, since=time.time(), source=source)
    _run_stop_hooks()
    if first:
        log.warning("E-STOP tripped: %s (source=%s)", reason, source)
    if broadcast:
        send_broadcast(reason)


def reset(by):
    with _lock:
        _state.update(estopped=False, reason=None, since=None, source=None)
    log.warning("E-STOP reset by %s", by)


def state():
    with _lock:
        return dict(_state)


def require_armed():
    from .registry import ToolError

    s = state()
    if s["estopped"]:
        raise ToolError(
            f"E-STOP is latched ({s['reason']!r} from {s['source']}). "
            "Actuation refused. A human must call admin_reset_estop."
        )


def stop_all_local():
    _run_stop_hooks()


def _targets():
    # Broadcast + explicit unicast peers: many Wi-Fi APs (client isolation) drop broadcast.
    bcast = os.environ.get("ESTOP_BROADCAST", "255.255.255.255")
    peers = [p.strip() for p in os.environ.get("ESTOP_PEERS", "").split(",") if p.strip()]
    return [bcast] + peers


def send_broadcast(reason):
    msg = ESTOP_MAGIC + b"|" + reason.encode()[:200]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for addr in _targets():
        for _ in range(3):  # UDP is lossy; repeat
            try:
                s.sendto(msg, (addr, ESTOP_PORT))
            except OSError as e:
                log.error("e-stop send to %s failed: %s", addr, e)
                break
    s.close()


def start_listener():
    def loop():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            s.bind(("", ESTOP_PORT))
        except OSError as e:
            log.error("cannot bind e-stop listener on %d: %s", ESTOP_PORT, e)
            return
        log.info("e-stop listener on udp/%d", ESTOP_PORT)
        while True:
            data, addr = s.recvfrom(512)
            if data.startswith(ESTOP_MAGIC):
                reason = data[len(ESTOP_MAGIC) + 1 :].decode(errors="replace") or "remote"
                trip(reason, source=f"lan:{addr[0]}", broadcast=False)

    threading.Thread(target=loop, name="estop-listener", daemon=True).start()


def timed(duration_ms, start_fn, stop_fn):
    """Run start_fn now and stop_fn after duration_ms (clamped). Blocks until done."""
    duration_ms = max(0, min(int(duration_ms), MAX_MOTION_MS))
    start_fn()
    deadline = time.monotonic() + duration_ms / 1000.0
    try:
        while time.monotonic() < deadline:
            if state()["estopped"]:
                break
            time.sleep(0.02)
    finally:
        stop_fn()
    return duration_ms
