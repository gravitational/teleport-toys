# Advanced simulation: a five-Pi robot behind Teleport 🧠🦾

A robot whose body is **five Raspberry Pi 3s**, each one an MCP server with a different job. An AI agent in a
**Beam** drives it. It gets no keys, no VPN and no direct path to any Pi. Teleport RBAC decides which *tools*
the agent sees (eyes yes, hands only after a human elevates it), and every tool call is in the audit log.

The hardware is simulated (`ROBOT_SIM=1`): the servers behave like a real robot (joint limits, time-boxed
motion, e-stop, a real PNG "camera frame") without wiring anything. Want one real robot on one Pi? See
[`../picar-beambot`](../picar-beambot/README.md).

```
 ┌──────────── Beam (Firecracker microVM) ─────────────┐
 │ claude  ── tsh mcp connect (beam identity) ──┐       │  no secrets in the beam
 └─────────────────────────────────────────────┼───────┘
                                               ▼
                                   Teleport (proxy + auth)
                           RBAC on tool names · audit · reverse tunnels
                                               │  outbound from each Pi
   ┌──────────┬──────────┬──────────┬───────────┴┬──────────┐
   pi1        pi2        pi3        pi4          pi5
   safety     vision     drive      arm          sensors
   e-stop     camera     motors     servos       distance/IMU
   └─ each: teleport (ssh + app_service) → mcp+http://127.0.0.1:8000/mcp
   └─ LAN UDP e-stop mesh: any node (or a button) latches STOP on all nodes, no cloud needed
```

## Tiers

| Tool prefix | Meaning | Role |
|---|---|---|
| `sense_*` | read-only (camera, distance, IMU, status) | `beambot-sim-observer` |
| `act_*` | moves hardware (drive, turn, arm, gripper) | `beambot-sim-operator` |
| `admin_*` | reset e-stop, change limits | `beambot-sim-admin` |
| `safety_*` | stop. Everyone, always. | all |

| Node | Tools |
|---|---|
| safety | `sense_node_status`, `sense_estop_state`, `safety_stop`, `admin_reset_estop`, `act_signal` (status light/buzzer) |
| vision | `sense_node_status`, `sense_capture_image` (returns a PNG the agent can see), plus the common `safety_stop` / `admin_reset_estop` |
| drive | `act_drive`, `act_turn` (≤2 s bursts), `sense_drive_state`, `safety_drive_stop` |
| arm | `act_arm_move_joint`, `act_arm_gripper`, `act_arm_home`, `sense_arm_pose` (joint limits enforced) |
| sensors | `sense_distance`, `sense_imu` |

## Try it on a laptop first (no Pis)

```bash
pip install "mcp>=1.10,<2"
python3 tests/test_mcp_client.py          # official-SDK server, all five roles on free local ports
python3 tests/test_mcp_client.py stdlib   # zero-dependency fallback server
```

The test uses the official MCP Python client against every role and checks RBAC-prefix naming, joint limits,
out-of-range refusals, time-boxed motion, and the e-stop: a burst on one node is cut short when another node
latches STOP.

## On five Pis

**You need:** five Raspberry Pi 3 (32-bit Arch Linux ARM, SSH aliases `pi1`…`pi5`), a Teleport cluster with
Beams, and `tsh`/`tctl` on your laptop. The installer is tested on Arch Linux ARM; other distros need the
package-install lines in `pi/setup_pi.sh` adapted.

```bash
# 1. Roles and a short-lived join token (deploy.sh hands it to each Pi over ssh's stdin, never on a command line)
tctl create -f teleport/roles.yaml
export TELEPORT_PROXY=<your-tenant>.beams.run:443
export TELEPORT_TOKEN=$(tctl tokens add --type=node,app --ttl=1h --format=text)

# 2. Tell deploy.sh about your Pis, then deploy (PARALLEL=1 for all at once, logs in ./logs/)
cp cluster.conf.example cluster.conf && $EDITOR cluster.conf
PARALLEL=1 ./deploy.sh                  # or ./deploy.sh pi4 for one node

# 3. Check, then revoke the token: every node that joined holds its own certificates now
tsh ls robot=beambot-sim                # five SSH nodes
tsh mcp ls robot=beambot-sim -v         # five MCP servers and the tools *you* are allowed
tctl tokens rm "$TELEPORT_TOKEN" && unset TELEPORT_TOKEN
```

`setup_pi.sh` syncs the clock, installs Teleport, creates a `robot` user, installs the MCP server
(official SDK, with an automatic stdlib fallback) and starts it as `beambot-sim.service` on `127.0.0.1:8000`.
It also writes a unicast `ESTOP_PEERS` list, because Wi-Fi client isolation can drop UDP broadcast.

**Real hardware later:** wire it, then `EXTRA="--real" ./deploy.sh pi4` (one node at a time). Joint limits live in
`pi/robot_mcp/roles/arm.py`; PCA9685 drive channels are set with `DRIVE_CHANNELS`. The real-hardware code paths
are **untested**: treat them as a starting point.

## Put an agent in a beam

From a normal (non-delegated) `tsh login`:

```bash
bash ../../beam-mcp-setup/scripts/beam_mcp_up.sh --roles beam-user,beambot-sim-observer \
     --labels robot=beambot-sim --allow 'sense_*,safety_*' --deny 'admin_*'
tsh beams scp beam/CLAUDE.beam.md <beam>:/home/beams/work/CLAUDE.md     # the agent's operating rules
tsh beams ssh <beam>
cd ~/work && claude --mcp-config ./mcp.json
```

The beam carries your delegated identity, so audit entries show you plus the beam's own workload identity.

## Demo script (3 minutes)

1. "Look around." The agent captures an image and reads the distance sensor. It works.
2. "Drive to the red block." The agent sees no `act_*` tools, explains its plan and asks for access.
3. A human gives it hands for a while by creating a second beam with `--roles beam-user,beambot-sim-operator`
   (`beam_mcp_up.sh --roles beam-user,beambot-sim-operator ...`). The `act_*` tools appear and the robot moves in small, checked steps.
4. Someone presses the e-stop (`safety_stop`). Everything stops via the LAN mesh and the next `act_*` call is refused.
5. Open the Teleport audit log and replay which identity made the robot do what.

Take the hands away by letting that beam expire or deleting it.

## Known limits

- **Elevation means a new beam.** A beam's roles are fixed when it is created (`--roles`), and Access Request
  permissions are not delegated into it, so granting "hands" means a new beam with the operator role. The
  `request:` block on the observer role is for human users.
- Teleport agents on the Pis must not be newer than the cluster. Production Beams need Teleport/`tsh` ≥ 18.11.9.
- The MCP SDK is pinned to `mcp==1.19.0` on-device, the newest release with prebuilt armv7 wheels for CPython 3.14.
  It logs a `ClosedResourceError` traceback after each stateless Streamable HTTP request; `sdk_server.py` filters it.
- The JWT Teleport forwards to each node is decoded for logging but **not verified** yet (a JWKS check is the obvious next step).
- The installer, `deploy.sh` and the Teleport enrollment need real Pis and a cluster, so they are not covered by the tests above.
