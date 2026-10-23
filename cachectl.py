#!/usr/bin/env python3
"""Single entry point for the cache e2e harness.

Each subcommand maps to an argv list built by `build_commands`, so the mapping
is testable without touching the cluster. Existing scripts remain runnable on
their own; this dispatcher only composes them.
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTEXT = "minikube"
NAMESPACE = "cache-e2e"
KUBECTL = ["kubectl", "--context", CONTEXT, "-n", NAMESPACE]
# Replica counts for fault injection; restoring to 1 is the default workload state.
FAULT_TARGETS = {"origin": "deploy/mock-backend", "valkey": "deploy/valkey"}
VERIFY_SCRIPTS = {"verify": "verify.py", "tiers": "verify-tiers.py",
                  "ratio": "verify-ratio.py", "metrics": "verify-metrics.py"}


def build_commands(args, extra):
    """Return a list of argv lists for the parsed command. Pure: no side effects."""
    cmd = args.command
    if cmd == "stack":
        if args.action == "up":
            return [["bash", str(ROOT / "start-stack.sh")]]
        if args.action == "down":
            return [["bash", str(ROOT / "teardown.sh")]]
    if cmd == "skipper":
        if args.action == "start":
            return [["bash", str(ROOT / "run-skipper.sh"), *extra]]
        if args.action == "stop":
            return [["bash", str(ROOT / "teardown.sh"), "skipper"]]
        if args.action == "restart":
            return [["bash", str(ROOT / "teardown.sh"), "skipper"],
                    ["bash", str(ROOT / "run-skipper.sh"), *extra]]
    if cmd == "fault":
        target = FAULT_TARGETS[args.target]
        if args.action == "down":
            return [[*KUBECTL, "scale", target, "--replicas=0"]]
        if args.action == "up":
            return [[*KUBECTL, "scale", target, "--replicas=1"],
                    [*KUBECTL, "rollout", "status", target, "--timeout=120s"]]
    if cmd == "load":
        return [[sys.executable, str(ROOT / "lifecycle.py"), "locust"]]
    if cmd == "check":
        if args.name == "unit":
            return [[sys.executable, "-m", "unittest", "test_harness", "test_locust",
                     "test_dashboard", "-v"]]
        if args.name == "coalescing":
            return [["bash", str(ROOT / "verify-coalescing.sh"), *extra]]
        if args.name == "browser":
            return [["node", str(ROOT / "verify-browser.mjs"), *extra]]
        return [[sys.executable, str(ROOT / VERIFY_SCRIPTS[args.name]), *extra]]
    raise ValueError(f"unsupported command: {cmd} {getattr(args, 'action', '')}".strip())


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("stack", help="Kubernetes stack (minikube)")
    s.add_argument("action", choices=["up", "down"])

    k = sub.add_parser("skipper", help="Local Skipper process; start takes a checkout path or SKIPPER_CHECKOUT")
    k.add_argument("action", choices=["start", "stop", "restart"])

    f = sub.add_parser("fault", help="Scale origin or Valkey to 0 / 1 replicas")
    f.add_argument("target", choices=sorted(FAULT_TARGETS))
    f.add_argument("action", choices=["down", "up"])

    sub.add_parser("load", help="Start Locust web UI on 127.0.0.1:8089 against Skipper on :9999")

    c = sub.add_parser("check", help="Run a test or verification; extra args pass through")
    c.add_argument("name", choices=["unit", "coalescing", "browser", *sorted(VERIFY_SCRIPTS)])
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Split off passthrough args after "--" so argparse does not reject them.
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    args = parser().parse_args(argv)
    if args.command == "skipper" and args.action in ("start", "restart") and not extra:
        import os
        if not os.environ.get("SKIPPER_CHECKOUT"):
            parser().error("skipper start/restart needs a checkout: pass it after -- or set SKIPPER_CHECKOUT")
    for command in build_commands(args, extra):
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
