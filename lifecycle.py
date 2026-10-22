#!/usr/bin/env python3
"""Foreground supervisors with identity-checked PID records, never broad kills."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".runtime"
STATE.mkdir(exist_ok=True)


def identity(pid):
    return subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        text=True).strip()


def ready(url, timeout=90, children=()):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(p.poll() is not None for p in children):
            raise RuntimeError("process exited before readiness")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"readiness timeout: {url}")


def supervise(commands, probes, name):
    record = STATE / (name + ".json")
    if record.exists():
        raise RuntimeError(f"{record} exists; use teardown.sh first")
    ports = {"skipper": (9999, 9911),
             "forwards": (3000, 8080, 6379, 9090)}.get(name, ())
    for port in ports:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    record.touch(exist_ok=False)
    children = []

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for command in commands:
            child = subprocess.Popen(command, cwd=ROOT)
            children.append(child)
        record.write_text(json.dumps({
            "supervisor": {"pid": os.getpid(), "identity": identity(os.getpid())},
            "children": [{"pid": p.pid, "identity": identity(p.pid)}
                         for p in children]}))
        for probe in probes:
            ready(probe, children=children)
        print(f"READY: {name}: " + ", ".join(probes), flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(0.5)
        raise RuntimeError("a supervised process exited")
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        record.unlink(missing_ok=True)


def teardown(names=("traffic", "skipper", "forwards")):
    for name in names:
        if name not in ("traffic", "skipper", "forwards"):
            raise ValueError("Stop target must be traffic, skipper, or forwards")
        record = STATE / (name + ".json")
        if not record.exists():
            continue
        state = json.loads(record.read_text())
        live = set()
        for process in [state["supervisor"]] + state["children"]:
            try:
                current = identity(process["pid"])
            except subprocess.CalledProcessError:
                continue
            if current != process["identity"]:
                raise RuntimeError(f"PID identity changed; refusing {process['pid']}")
            live.add(process["pid"])
        try:
            os.kill(state["supervisor"]["pid"], signal.SIGTERM)
        except ProcessLookupError:
            if any(p["pid"] in live for p in state["children"]):
                raise RuntimeError("supervisor missing; inspect child PIDs manually")
            record.unlink()
            continue
        deadline = time.monotonic() + 25
        while record.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        if record.exists():
            raise RuntimeError(f"shutdown timeout: {record}")
        # Supervisor owns cleanup/removal. Never delete the namespace implicitly.


if __name__ == "__main__":
    if sys.argv[1] == "stop":
        teardown(sys.argv[2:] or ("traffic", "skipper", "forwards"))
    elif sys.argv[1] == "traffic":
        supervise([[sys.executable, "-u", str(ROOT / "traffic.py")]],
                  ["http://127.0.0.1:9999/get?traffic-readiness=1"], "traffic")
    elif sys.argv[1] == "forwards":
        supervise([
            ["kubectl", "--context", "minikube", "-n", "cache-e2e",
             "port-forward", "--address=127.0.0.1", "svc/" + service, ports]
            for service, ports in (("grafana", "3000:3000"),
                                   ("mock-backend", "8080:8080"),
                                   ("valkey", "6379:6379"),
                                   ("prometheus", "9090:9090"))
        ], ["http://127.0.0.1:3000/api/health", "http://127.0.0.1:8080/get",
            "http://127.0.0.1:9090/-/ready"], "forwards")
    elif sys.argv[1] == "skipper":
        checkout = Path(sys.argv[2]).resolve()
        sha = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        if subprocess.check_output(
                ["git", "-C", str(checkout), "status", "--porcelain"], text=True):
            raise RuntimeError("Skipper checkout must be clean")
        binary = STATE / ("skipper-" + sha)
        subprocess.run(["go", "build", "-o", str(binary), "./cmd/skipper"],
                       cwd=checkout, check=True)
        print(f"Starting Skipper {sha}", flush=True)
        supervise([[str(binary), "-address=127.0.0.1:9999",
                    "-metrics-flavour=prometheus", "-enable-swarm",
                    "-enable-l2-cache", "-swarm-valkey-urls=127.0.0.1:6379",
                    "-swarm-valkey-dial-timeout=2s",
                    "-swarm-valkey-conn-timeout=2s",
                    "-wait-for-healthcheck-interval=0",
                    "-routes-file=" + str(ROOT / "routes.eskip")]],
                  ["http://127.0.0.1:9999/get?readiness=" + sha,
                   "http://127.0.0.1:9911/metrics"], "skipper")
