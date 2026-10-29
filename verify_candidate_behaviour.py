#!/usr/bin/env python3
"""Launch isolated resources for the existing verify.py behaviour suite."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import verify
from origin.mock_origin import make_servers
from verify_l1_freshness import (
    VALKEY_IMAGE, command, free_port, require, stop_process, wait_ready,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--l1-result", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    baseline = json.loads(Path(args.l1_result).read_text())
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    sha = baseline["provenance"]["skipper_sha"]
    binary = Path(args.l1_result).resolve().parent / "skipper"
    require(hashlib.sha256(binary.read_bytes()).hexdigest() ==
            baseline["provenance"]["binary_sha256"], "binary differs from candidate L1 run")
    report = {"provenance": baseline["provenance"], "status": "ERROR",
              "verifier_sha256": hashlib.sha256(Path(verify.__file__).read_bytes()).hexdigest()}
    name = "skipper-behaviour-" + uuid.uuid4().hex[:12]
    try:
        with contextlib.ExitStack() as stack:
            command("docker", "run", "--detach", "--name", name,
                    "--publish", "127.0.0.1::6379", VALKEY_IMAGE,
                    "valkey-server", "--save", "", "--appendonly", "no")
            stack.callback(command, "docker", "rm", "--force", name)
            port = command("docker", "port", name, "6379/tcp").rsplit(":", 1)[1]
            deadline = time.monotonic() + 10
            while command("docker", "exec", name, "valkey-cli", "ping") != "PONG":
                require(time.monotonic() < deadline, "Valkey readiness timed out")
                time.sleep(0.05)
            servers = make_servers(http_port=0, control_port=0)
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                stack.callback(server.server_close)
                stack.callback(server.shutdown)
            origin = f"http://127.0.0.1:{servers[0].server_port}"
            control = f"http://127.0.0.1:{servers[1].server_port}"
            routes = output / "routes.eskip"
            routes.write_text(
                f'behaviour: * -> cache("30s", "10s", "15s") -> "{origin}";\n'
                'ready: Path("/ready") -> status(200) -> <shunt>;\n')
            http_port, metrics_port = free_port(), free_port()
            base = f"http://127.0.0.1:{http_port}"
            argv = [str(binary), f"-address=127.0.0.1:{http_port}",
                    f"-support-listener=127.0.0.1:{metrics_port}",
                    "-metrics-flavour=prometheus", "-proxy-preserve-host",
                    "-enable-swarm", "-enable-l2-cache",
                    f"-swarm-valkey-urls=127.0.0.1:{port}",
                    "-swarm-valkey-dial-timeout=2s", "-swarm-valkey-conn-timeout=2s",
                    "-cache-l1-ttl=2s", "-wait-for-healthcheck-interval=0",
                    f"-routes-file={routes}"]
            report["skipper_command"] = argv
            report["route"] = routes.read_text()
            log = stack.enter_context((output / "skipper.log").open("w"))
            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
            stack.callback(stop_process, process)
            wait_ready(base + "/ready", process)
            verify_argv = [
                sys.executable, str(Path(verify.__file__).resolve()),
                "--base", base, "--control", control,
                "--label", "candidate-" + sha[:12], "--sha", sha,
                "--output", str(output / "coalescing.json"),
            ]
            report["verification_command"] = verify_argv
            result = subprocess.run(verify_argv, timeout=60, check=False)
            report["exit_code"] = result.returncode
            report["status"] = "PASS" if result.returncode == 0 else "FAIL"
    finally:
        (output / "provenance.json").write_text(json.dumps(report, indent=2) + "\n")
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
