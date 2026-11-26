#!/usr/bin/env bash
# Put a simulated PiCar-X in your local Kubernetes cluster and publish it to Teleport.
# Run on your laptop after `tsh login` as someone who can create join tokens (tctl).
#
#   ./deploy.sh          build, load into the cluster, deploy the robot + a Teleport agent
#   ./deploy.sh down     remove both (the apps vanish from Teleport once the agent stops)
#
# Optional env:
#   TELEPORT_PROXY=<your-tenant>.beams.run:443   default: the proxy of your current tsh login
#   TELEPORT_VERSION=18.11.9                     agent chart version. Default: the newest stable chart that is not newer
#                                                than the cluster (a dev/pre-release cluster gets the previous major)
#   IMAGE=picar-sim:dev                          image name
#   PLATFORM=linux/amd64                         build for the cluster's CPU (a Mac builds arm64 by default)
#   PUSH=1                                       docker push $IMAGE to a registry the cluster can pull from
#   LOAD_CMD='ssh node sudo k3s ctr images import -'
#                                                for clusters on another machine without a registry: the image is
#                                                piped (docker save) into this command
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${IMAGE:-picar-sim:dev}"
AGENT_NS=teleport-agent
log() { echo "==> $*" >&2; }
die() { echo "Error: $*" >&2; exit 1; }

command -v kubectl >/dev/null || die "kubectl not found"
command -v helm >/dev/null || die "helm not found"

if [[ "${1:-}" == "down" ]]; then
  helm uninstall picar-sim-agent -n "$AGENT_NS" 2>/dev/null || true
  kubectl delete namespace "$AGENT_NS" picar-sim --ignore-not-found
  log "removed"; exit 0
fi

command -v tsh >/dev/null || die "tsh not found"
command -v tctl >/dev/null || die "tctl not found (needed to create the join token)"
command -v docker >/dev/null || die "docker not found"
tsh status >/dev/null 2>&1 || die "not logged in: tsh login --proxy=<your-tenant>.beams.run:443"

PROXY="${TELEPORT_PROXY:-$(tsh status --format=json | python3 -c 'import json,sys; u=json.load(sys.stdin)["active"]["profile_url"]; print(u.split("://",1)[-1].rstrip("/"))')}"
[[ -n "$PROXY" ]] || die "could not work out the proxy: set TELEPORT_PROXY"
log "Teleport proxy $PROXY"

# ── image ───────────────────────────────────────────────────────────────────
log "Building $IMAGE"
docker build -q ${PLATFORM:+--platform "$PLATFORM"} -f Dockerfile -t "$IMAGE" ../robot >/dev/null
# Deploy a tag derived from the sources, not the one you typed. Re-pushing an existing tag is ignored by nodes that
# already have it (imagePullPolicy IfNotPresent), and an unchanged Deployment spec never rolls: you would keep running
# the old robot. A new tag whenever the code changes makes the cluster pick it up.
SRC_HASH="$( (cat Dockerfile ../robot/*.py ../robot/requirements.txt; echo "${PLATFORM:-}") | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-10)"
BUILT="$IMAGE"
if [[ "$BUILT" == *:* && "${BUILT##*:}" != */* ]]; then REPO="${BUILT%:*}"; else REPO="$BUILT"; fi
IMAGE="$REPO:build-$SRC_HASH"
docker tag "$BUILT" "$IMAGE"
log "Deploying as $IMAGE"
if [[ "${PUSH:-0}" == "1" ]]; then
  docker push "$IMAGE"
elif [[ -n "${LOAD_CMD:-}" ]]; then
  log "Sending the image with: $LOAD_CMD"
  docker save "$IMAGE" | bash -c "$LOAD_CMD"
else
  CTX="$(kubectl config current-context)"
  case "$CTX" in
    kind-*)          kind load docker-image "$IMAGE" --name "${CTX#kind-}" ;;
    minikube)        minikube image load "$IMAGE" ;;
    k3d-*)           k3d image import "$IMAGE" -c "${CTX#k3d-}" ;;
    docker-desktop|rancher-desktop|orbstack) : ;;      # these share the local docker image store
    *) die "can't tell how to get the image into cluster context '$CTX'. Either IMAGE=<registry/image> PUSH=1, or LOAD_CMD='ssh <node> sudo k3s ctr images import -' (k3s) / 'ssh <node> sudo ctr -n k8s.io images import -' (containerd)." ;;
  esac
fi

# ── robot ───────────────────────────────────────────────────────────────────
log "Deploying the simulated robot"
sed "s|image: picar-sim:dev|image: ${IMAGE}|" sim-robot.yaml | kubectl apply -f -
kubectl -n picar-sim rollout status deploy/picar-sim --timeout=120s

# ── Teleport agent ──────────────────────────────────────────────────────────
# Pick the agent chart first: nothing below should be created if we can't install it.
helm repo add teleport https://charts.releases.teleport.dev >/dev/null 2>&1 || true
helm repo update teleport >/dev/null
if [[ -z "${TELEPORT_VERSION:-}" ]]; then
  CLUSTER_VERSION="$(curl -fsS "https://${PROXY}/webapi/ping" | python3 -c 'import json,sys; print(json.load(sys.stdin)["server_version"])')"
  [[ -n "$CLUSTER_VERSION" ]] || die "could not read the cluster version: set TELEPORT_VERSION"
  TELEPORT_VERSION="$(helm search repo teleport/teleport-kube-agent --versions -o json | python3 -c '
import json, re, sys
cluster = sys.argv[1]
def parse(v):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)$", v.strip())      # anything else (2.0, nightly, pre-releases) is ignored
    return tuple(map(int, m.groups())) if m else None
cv = parse(re.match(r"[\d.]+", cluster).group(0))
pre = "-" in cluster                                   # dev / rc cluster: its own major is not released yet
stable = [v for v in (parse(c.get("version", "")) for c in json.load(sys.stdin)) if v]
ok = [v for v in stable if (v[0] < cv[0] if pre else v <= cv)]
print(".".join(map(str, max(ok))) if ok else "")' "$CLUSTER_VERSION")"
  [[ -n "$TELEPORT_VERSION" ]] || die "no teleport-kube-agent chart is old enough for cluster $CLUSTER_VERSION: set TELEPORT_VERSION"
  log "Cluster is $CLUSTER_VERSION: using agent chart $TELEPORT_VERSION"
fi
VERSION="$TELEPORT_VERSION"

log "Creating a 30-minute join token"
TOKEN="$(tctl tokens add --type=app --ttl=30m --format=text)"
[[ -n "$TOKEN" ]] || die "tctl returned no token"
kubectl get ns "$AGENT_NS" >/dev/null 2>&1 || kubectl create ns "$AGENT_NS" >/dev/null
# The token reaches the cluster as a Secret, through stdin: never on a command line or in a values file.
B64="$(printf '%s' "$TOKEN" | base64 | tr -d '\n')"
kubectl apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Secret
metadata:
  name: picar-sim-join-token
  namespace: ${AGENT_NS}
type: Opaque
data:
  auth-token: ${B64}
YAML

log "Installing the Teleport agent"
helm upgrade --install picar-sim-agent teleport/teleport-kube-agent --version "$VERSION" -n "$AGENT_NS" \
  -f teleport-agent-values.yaml --set "proxyAddr=${PROXY}" --wait --timeout 3m

# The agent has joined and holds its own certificates now: the token has no further use, so kill it.
tctl tokens rm "$TOKEN" >/dev/null 2>&1 || log "could not revoke the join token (it expires on its own in 30 minutes)"
unset TOKEN

log "Done. In a minute 'tsh mcp ls' should show picar-mcp, and 'tsh apps ls' picar-stream."
