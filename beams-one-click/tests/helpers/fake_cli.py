"""Fake kubectl / helm / docker / kind for testing deploy scripts. Logs argv and anything piped in on stdin
to $FAKE_CLI_LOG, answers the few questions the scripts ask."""
import json
import os
import sys

prog, args = sys.argv[1], sys.argv[2:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["FAKE_CLI_LOG"], "a") as f:
    f.write(json.dumps({"prog": prog, "args": args, "stdin": stdin}) + "\n")
if prog == "kubectl" and args[:2] == ["config", "current-context"]:
    print(os.environ.get("FAKE_KUBE_CONTEXT", "docker-desktop"))
elif prog == "docker" and args[:1] == ["save"]:
    sys.stdout.write("IMAGEDATA")
elif prog == "curl":
    print(json.dumps({"server_version": os.environ.get("FAKE_SERVER_VERSION", "18.11.9")}))
elif prog == "helm" and args[:1] == ["upgrade"] and os.environ.get("FAKE_HELM_FAIL"):
    sys.exit(1)
elif prog == "helm" and args[:2] == ["search", "repo"]:
    print(os.environ.get("FAKE_CHARTS", '[{"version":"19.0.0-dev.1"},{"version":"18.11.9"},{"version":"18.12.0"},{"version":"18.2.0"},{"version":"17.9.9"}]'))
elif prog == "kubectl" and args[:1] == ["get"]:
    sys.exit(1)                      # "namespace not found" -> script creates it
