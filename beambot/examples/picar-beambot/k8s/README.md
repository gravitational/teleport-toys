# An always-on PiCar-X in your Kubernetes cluster 🤖☸️

No robot, no laptop that has to stay awake. This puts the **simulated PiCar-X** (same seven MCP tools, same camera
and telemetry) in a local Kubernetes cluster and a **Teleport agent** next to it that dials out to your cluster.
Afterwards `picar-mcp` and `picar-stream` show up in Teleport like the real thing, so a beam can use them any time.

```
 beam ──tsh──▶ Teleport proxy ◀──outbound only── Teleport agent (pod) ──▶ picar-sim (pod, :8765 MCP, :8766 stream)
```

Nothing listens on the internet: the agent connects out, and the robot is a ClusterIP Service.

## Run it

You need `docker`, `kubectl`, `helm`, a local cluster (Docker Desktop, kind, minikube, k3d, OrbStack) and a
`tsh login` as someone who may create join tokens (`tctl`).

```bash
./deploy.sh            # build, load, deploy robot + agent (a minute or two)
tsh mcp ls             # picar-mcp should appear
tsh apps ls            # picar-stream too
./deploy.sh down       # remove it all
```

It reads the proxy from your `tsh` login and picks the newest stable agent chart that isn't newer than your cluster
(a dev build of the next major gets the previous major's chart). Override with `TELEPORT_PROXY` / `TELEPORT_VERSION`.

**Getting the image to the cluster** depends on where it runs:

| Cluster | How |
|---|---|
| Docker Desktop, OrbStack, Rancher Desktop, kind, minikube, k3d on this machine | automatic |
| Another machine (a homelab k3s, say), no registry | `LOAD_CMD='ssh <node> sudo k3s ctr images import -' ./deploy.sh` (plain containerd: `sudo ctr -n k8s.io images import -`) |
| Any cluster with a registry | `IMAGE=<registry>/picar-sim:1 PUSH=1 ./deploy.sh` |

`deploy.sh` deploys the image under a tag derived from the robot's sources (`…:build-<hash>`), so re-running it after a code change always rolls new pods, even if you reuse the tag you typed. (Re-pushing an old tag is ignored by nodes that already have it.)

Building on an Apple-silicon Mac for an amd64 cluster (or the reverse)? Add `PLATFORM=linux/amd64`.

## How the agent joins

With a short-lived **join token**. `deploy.sh` mints a 30-minute app token, hands it to the cluster as a Secret through
stdin (never on a command line, never in a file), and **revokes it as soon as the agent is up**. After that the agent
runs on the certificates Teleport issued it and renews them itself, so nothing long-lived is left in the cluster.
Re-running `deploy.sh` mints a fresh token.

Teleport's `kubernetes` join method avoids tokens altogether, but it is not available on Teleport Cloud for in-cluster
auth, and the OIDC variant needs your cluster's service-account issuer reachable from the internet with a publicly
trusted certificate (see [Kubernetes OIDC joining](https://goteleport.com/docs/configuration/resource-guides/kubernetes-oidc-join-token/)).
That is easy on EKS/GKE and a project of its own on a homelab, so the example uses the token.

## Who can see it

Teleport marks MCP apps itself, and the built-in `mcp-user` role matches all of them, so a beam created with
`--roles beam-user,mcp-user` sees `picar-mcp` straight away (`beam-user` is what gives it model access). But `mcp-user`
grants every tool, including `resume`, so anything in that beam (Claude Code, a script) can lift an emergency stop.
To limit tools, use `../teleport/roles.yaml` (`picar-viewer` sees `snapshot, get_telemetry, stop`; `picar-driver`
everything except `resume`): both match `picar-mcp` through the
`project: picar-beambot` label this example sets, plus Teleport's own MCP marker. For the video panel and the dashboard's
Resume button add the `picar-stream` role. The stream app is a second port with no `/mcp`, so opening it can watch,
stop and resume the car but never drive it.

## Prove it end to end

```bash
tsh beams add --roles beam-user,picar-driver   # roles from ../teleport/roles.yaml; mcp-user also lets the beam resume a stop
tsh beams ssh <beam>
# inside, with this repo copied in:
bash beam-mcp-setup/scripts/verify_in_beam.sh --app picar-mcp
```

Or deploy the dashboard against it: `cd ../beam-app && BEAM_ROLES=beam-user,picar-driver,picar-stream USE_DBS=0 ./deploy-beam.sh`.

## Troubleshooting

| You see | Do this |
|---|---|
| `tsh mcp ls` is empty after a few minutes | `kubectl -n teleport-agent logs deploy/picar-sim-agent` (or `statefulset/…`). A bad or expired token shows up as a join error: run `./deploy.sh` again for a fresh one. |
| Pod `ImagePullBackOff` | The image isn't on that cluster's nodes: see the table above (and check `kubectl config current-context`). |
| Pod `exec format error` | Wrong CPU architecture: rebuild with `PLATFORM=linux/amd64` or `linux/arm64` to match the nodes. |
| App is listed but calls fail | `kubectl -n picar-sim logs deploy/picar-sim`, and `kubectl -n picar-sim get endpoints picar-sim`. |
