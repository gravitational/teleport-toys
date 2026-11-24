"""The always-on simulated robot for Kubernetes + its Teleport agent. No cluster here: manifests are checked
for consistency, the container's server is started the way the Dockerfile starts it (reached under the in-cluster
DNS name), and deploy.sh runs against fake kubectl/helm/docker/tctl."""
import json
import os
import stat
import subprocess
import sys

import httpx
import pytest
import yaml

from conftest import ROBOT_DIR, ROOT, HELPERS, _clean_env, free_port, wait_http

K8S = ROOT / "examples/picar-beambot/k8s"
DNS = "picar-sim.picar-sim.svc.cluster.local"


def docs(name):
    return [d for d in yaml.safe_load_all((K8S / name).read_text()) if d]


def by_kind(kind):
    return next(d for d in docs("sim-robot.yaml") if d["kind"] == kind)


def test_manifests_are_consistent():
    dep, svc = by_kind("Deployment"), by_kind("Service")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    port = c["ports"][0]
    assert svc["spec"]["selector"] == dep["spec"]["selector"]["matchLabels"] == dep["spec"]["template"]["metadata"]["labels"]
    assert svc["spec"]["ports"][0]["targetPort"] == port["name"] and svc["spec"]["ports"][0]["port"] == 8765 == port["containerPort"]
    assert dep["spec"]["replicas"] == 1
    assert c["readinessProbe"]["httpGet"]["port"] == port["name"]
    assert c["securityContext"]["runAsNonRoot"] is True
    assert {d["metadata"]["namespace"] for d in (dep, svc)} == {"picar-sim"}


def test_agent_publishes_the_apps_the_dashboard_looks_for():
    v = yaml.safe_load((K8S / "teleport-agent-values.yaml").read_text())
    apps = {a["name"]: a for a in v["apps"]}
    # start.sh defaults: ROBOT_MCP_NAME=picar-mcp, ROBOT_STREAM_APP=picar-stream
    assert apps["picar-mcp"]["uri"] == f"mcp+http://{DNS}:8765/mcp"
    assert apps["picar-stream"]["uri"] == f"http://{DNS}:8765"
    assert all(a["labels"] == {"project": "picar-beambot"} for a in apps.values())
    assert v["roles"] == "app" and v["joinTokenSecret"] == {"create": False, "name": "picar-sim-join-token"}
    assert "authToken" not in v and "proxyAddr" not in v, "no secrets or site names in the values file"


def test_dockerfile_matches_the_manifest():
    d = (K8S / "Dockerfile").read_text()
    assert "SIMULATE=1" in d and "ROBOT_HOST=0.0.0.0" in d and "ROBOT_PORT=8765" in d and "USER 10001" in d
    for f in ("robot_mcp.py", "robot_hat_lite.py", "requirements.txt"):
        assert f in d and (ROBOT_DIR / f).exists()


def test_dockerfile_makes_files_readable_for_the_non_root_user():
    """Regression: sources saved 0600 were copied root-owned 0600 and the pod (uid 10001) died with exit 2."""
    lines = [l.strip() for l in (K8S / "Dockerfile").read_text().splitlines()]
    copy = max(i for i, l in enumerate(lines) if l.startswith("COPY robot_mcp.py"))
    chmod = next(i for i, l in enumerate(lines) if l.startswith("RUN chmod") and "a+rX" in l)
    user = next(i for i, l in enumerate(lines) if l.startswith("USER "))
    assert copy < chmod < user


def test_container_server_answers_under_the_cluster_dns_name():
    """The Teleport agent calls the Service by its DNS name, so the server must not reject that Host header."""
    port = free_port()
    p = subprocess.Popen([sys.executable, "robot_mcp.py"], cwd=ROBOT_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=_clean_env(SIMULATE=1, ROBOT_HOST="0.0.0.0", ROBOT_PORT=port, SAY_CMD="true"))
    try:
        wait_http(f"http://127.0.0.1:{port}/telemetry")
        h = {"Host": f"{DNS}:8765", "content-type": "application/json", "accept": "application/json, text/event-stream"}
        assert httpx.get(f"http://127.0.0.1:{port}/telemetry", headers=h).status_code == 200
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        r = httpx.post(f"http://127.0.0.1:{port}/mcp", headers=h, json=init)
        assert r.status_code == 200 and "serverInfo" in r.text, r.text[:300]
    finally:
        p.terminate()


# --------------------------------------------------------------------------- deploy.sh
@pytest.fixture
def cluster(teleport, tmp_path):
    log = tmp_path / "k8s.jsonl"
    log.touch()
    for prog in ("kubectl", "helm", "docker", "kind", "minikube", "k3d", "curl"):
        w = teleport.bin / prog
        w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HELPERS / "fake_cli.py"}" {prog} "$@"\n')
        w.chmod(w.stat().st_mode | stat.S_IEXEC)

    def run(*args, **env):
        e = teleport.env(**{"FAKE_CLI_LOG": log, "TELEPORT_VERSION": "18.11.9", **env})
        return subprocess.run(["bash", str(K8S / "deploy.sh"), *args], env=e, capture_output=True, text=True, timeout=60)

    def calls(prog, *prefix):
        rows = [json.loads(l) for l in log.read_text().splitlines() if l]
        return [r for r in rows if r["prog"] == prog and r["args"][:len(prefix)] == list(prefix)]

    return type("C", (), {"run": staticmethod(run), "calls": staticmethod(calls)})


def test_deploy_installs_robot_and_agent(cluster, teleport):
    r = cluster.run()
    assert r.returncode == 0, r.stdout + r.stderr
    build = cluster.calls("docker", "build")[0]["args"]
    assert "picar-sim:dev" in build and build[-1].endswith("/robot")
    helm = cluster.calls("helm", "upgrade")[0]["args"]
    assert "teleport/teleport-kube-agent" in helm and helm[helm.index("--version") + 1] == "18.11.9"
    assert "proxyAddr=example.beams.run:443" in helm              # taken from the current tsh login
    assert any(a.endswith("teleport-agent-values.yaml") for a in helm)
    secret = [c for c in cluster.calls("kubectl", "apply") if "Secret" in c["stdin"]][0]
    import base64
    assert base64.b64decode(secret["stdin"].split("auth-token: ")[1].split()[0]) == b"fake-join-token-0123"
    assert "picar-sim-join-token" in secret["stdin"]
    assert teleport.calls("tctl", "tokens", "add")[0]["args"][:4] == ["tokens", "add", "--type=app", "--ttl=30m"]


def test_the_join_token_never_appears_on_a_command_line(cluster):
    cluster.run()
    for prog in ("kubectl", "helm", "docker"):
        assert "fake-join-token" not in json.dumps([c["args"] for c in cluster.calls(prog)])


@pytest.mark.parametrize("ctx,loader", [("kind-dev", "kind"), ("minikube", "minikube"), ("k3d-dev", "k3d")])
def test_image_is_loaded_into_the_local_cluster(cluster, ctx, loader):
    assert cluster.run(FAKE_KUBE_CONTEXT=ctx).returncode == 0 and cluster.calls(loader)


def test_docker_desktop_needs_no_load_and_unknown_context_is_explained(cluster):
    assert cluster.run(FAKE_KUBE_CONTEXT="docker-desktop").returncode == 0
    r = cluster.run(FAKE_KUBE_CONTEXT="prod-eks")
    assert r.returncode != 0 and "PUSH=1" in r.stderr and "LOAD_CMD" in r.stderr


def test_push_mode_for_remote_clusters(cluster):
    r = cluster.run(FAKE_KUBE_CONTEXT="prod-eks", PUSH=1, IMAGE="registry.example/picar-sim:1")
    assert r.returncode == 0 and cluster.calls("docker", "push")[0]["args"] == ["push", "registry.example/picar-sim:1"]
    assert "image: registry.example/picar-sim:1" in [c for c in cluster.calls("kubectl", "apply")][0]["stdin"]


def test_down_removes_everything(cluster):
    r = cluster.run("down")
    assert r.returncode == 0 and cluster.calls("helm", "uninstall") and cluster.calls("kubectl", "delete")


def chart_version(cluster, server_version):
    r = cluster.run(TELEPORT_VERSION="", FAKE_SERVER_VERSION=server_version)
    assert r.returncode == 0, r.stderr
    helm = cluster.calls("helm", "upgrade")[0]["args"]
    return helm[helm.index("--version") + 1]


def test_dev_cluster_gets_the_previous_majors_newest_stable_chart(cluster):
    """Seen on a real cluster: 19.0.0-dev.beams.launch.2. No chart has that version, and 19.x isn't released."""
    assert chart_version(cluster, "19.0.0-dev.beams.launch.2") == "18.12.0"


def test_stable_cluster_gets_a_chart_no_newer_than_itself(cluster):
    assert chart_version(cluster, "18.11.9") == "18.11.9"


def test_older_cluster_gets_an_older_chart(cluster):
    assert chart_version(cluster, "18.3.0") == "18.2.0"


def test_image_can_be_sent_to_a_remote_cluster_without_a_registry(cluster, tmp_path):
    """k3s homelab: context is just 'default'. docker save is piped into whatever LOAD_CMD says."""
    sink = tmp_path / "received.tar"
    r = cluster.run(FAKE_KUBE_CONTEXT="default", LOAD_CMD=f"cat > {sink}")
    assert r.returncode == 0, r.stderr
    assert sink.read_text() == "IMAGEDATA" and cluster.calls("docker", "save")[0]["args"] == ["save", "picar-sim:dev"]


def test_platform_is_passed_to_the_build(cluster):
    cluster.run(PLATFORM="linux/amd64")
    build = cluster.calls("docker", "build")[0]["args"]
    assert build[build.index("--platform") + 1] == "linux/amd64"


def test_odd_chart_versions_in_the_repo_are_ignored(cluster):
    """A real helm repo lists versions like 2.0 or nightly builds: skip what we can't read, never crash."""
    charts = json.dumps([{"version": "2.0"}, {"version": "nightly"}, {"version": "18.2.1"}, {"version": "v18.4.0"}, {"version": "19.0.0-dev.2"}, {}])
    r = cluster.run(TELEPORT_VERSION="", FAKE_CHARTS=charts, FAKE_SERVER_VERSION="19.0.0-dev.beams.launch.2")
    assert r.returncode == 0, r.stderr
    helm = cluster.calls("helm", "upgrade")[0]["args"]
    assert helm[helm.index("--version") + 1] == "18.4.0"


def test_no_token_is_created_when_no_chart_fits(cluster, teleport):
    r = cluster.run(TELEPORT_VERSION="", FAKE_CHARTS="[]")
    assert r.returncode != 0 and "no teleport-kube-agent chart" in r.stderr
    assert teleport.calls("tctl", "tokens") == []


def test_join_token_is_revoked_once_the_agent_is_up(cluster, teleport):
    assert cluster.run().returncode == 0
    rm = teleport.calls("tctl", "tokens", "rm")
    assert len(rm) == 1 and rm[0]["args"] == ["tokens", "rm", "fake-join-token-0123"]
    order = [c["args"][:2] for c in teleport.calls("tctl")]
    assert order.index(["tokens", "add"]) < order.index(["tokens", "rm"])


def test_token_is_not_revoked_if_the_install_fails(cluster, teleport):
    """If helm fails the agent never joined: keep the token (it expires in 30 min) so a retry can still use it."""
    r = cluster.run(FAKE_HELM_FAIL="1")
    assert r.returncode != 0 and teleport.calls("tctl", "tokens", "rm") == []
