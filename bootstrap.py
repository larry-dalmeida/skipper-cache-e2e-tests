#!/usr/bin/env python3
"""Idempotent local stack setup and fail-closed end-to-end verification."""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import lifecycle

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".runtime"


def run(argv, **kwargs):
    return subprocess.run([str(arg) for arg in argv], check=True, cwd=ROOT, **kwargs)


def prerequisites(checkout):
    for command in ("python3", "go", "git", "curl", "kubectl", "minikube", "docker"):
        if not shutil.which(command):
            raise RuntimeError(f"Missing prerequisite: {command}; install it and retry")
    checkout = Path(checkout).expanduser().resolve()
    if not (checkout / "go.mod").is_file():
        raise RuntimeError(f"SKIPPER_CHECKOUT must name a clean Skipper checkout: {checkout}")
    if subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain"], text=True):
        raise RuntimeError("Skipper checkout must be clean")
    required = re.search(r"^go (\S+)", (checkout / "go.mod").read_text(), re.MULTILINE)
    # Go's automatic toolchain selection is evaluated in the checkout, just like the build.
    actual = subprocess.check_output(["go", "env", "GOVERSION"], cwd=checkout, text=True).strip()
    if required is None:
        raise RuntimeError("Skipper go.mod has no Go version directive")
    version = lambda value: tuple(int(n) for n in value.removeprefix("go").split("."))
    if version(actual) < version(required[1]):
        raise RuntimeError(f"Skipper requires Go >= {required[1]}, selected {actual}; enable GOTOOLCHAIN=auto or install it")
    print(f"Prerequisites OK; Skipper toolchain {actual} (requires {required[1]})", flush=True)
    run(["docker", "info"], stdout=subprocess.DEVNULL)
    return checkout


def alive(name):
    record = STATE / (name + ".json")
    if not record.exists():
        return False
    state = json.loads(record.read_text())
    live = []
    for process in [state["supervisor"], *state["children"]]:
        try:
            current = lifecycle.identity(process["pid"])
        except subprocess.CalledProcessError:
            live.append(False)
            continue
        if current != process["identity"]:
            raise RuntimeError(f"PID identity changed for {name}; inspect {record} before retrying")
        live.append(True)
    if live[0]:
        return True
    if any(live):
        raise RuntimeError(f"Orphaned children for {name}; inspect {record} before retrying")
    record.unlink()
    return False


def ensure_supervisor(name, *args):
    if not alive(name):
        out = STATE / (name + "-run.out")
        with out.open("w") as log:
            process = subprocess.Popen([sys.executable, "lifecycle.py", name, *map(str, args)],
                                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        (STATE / (name + ".pid")).write_text(str(process.pid) + "\n")
        deadline = time.monotonic() + 240
        while f"READY: {name}" not in out.read_text():
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited before readiness; see {out}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{name} readiness timeout; see {out}")
            time.sleep(0.25)
    else:
        print(f"Reusing {name} supervisor", flush=True)
    if name == "forwards":
        lifecycle.ready("http://127.0.0.1:8080/get")
    elif name == "ui":
        lifecycle.ui_ready()
    elif name == "origin":
        lifecycle.ready("http://127.0.0.1:9091/health")
        lifecycle.ready("http://127.0.0.1:8081/")
        lifecycle.apply_origin_cases()
    elif name == "skipper":
        lifecycle.ready("http://127.0.0.1:9999/get")
        lifecycle.ready("http://127.0.0.1:9911/metrics")


def start(checkout):
    for name in ("forwards", "ui", "origin"):
        ensure_supervisor(name)
    sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    sha_file = STATE / "skipper.sha"
    if alive("skipper") and (not sha_file.exists() or sha_file.read_text().strip() != sha):
        lifecycle.teardown(("skipper",))
    ensure_supervisor("skipper", checkout)
    print((STATE / "urls.txt").read_text(), flush=True)


def smoke_result(folder):
    with (folder / "locust_stats.csv").open() as handle:
        rows = [row for row in csv.DictReader(handle) if row["Name"] == "Aggregated"]
    if len(rows) != 1 or int(rows[0]["Request Count"]) <= 0 or int(rows[0]["Failure Count"]) != 0:
        raise RuntimeError(f"Smoke failed or has no requests: {folder / 'locust_stats.csv'}")
    print(f"Smoke PASS: {rows[0]['Request Count']} requests, 0 failures", flush=True)


def verify():
    folder = lifecycle.run_locust("smoke")
    # Run both verifiers even when one fails, so the evidence is complete.
    failures = []
    try:
        smoke_result(folder)
    except RuntimeError as error:
        failures.append(str(error))
    sha = (STATE / "skipper.sha").read_text().strip()
    for script, extra, output in (("verify.py", ["--label", "coalescing"], "coalescing.json"),
                                  ("verify-metrics.py", ["--wait-seconds", "0"], "metrics.json")):
        result = subprocess.run([sys.executable, script, "--sha", sha, *extra,
                                 "--output", str(folder / output)], cwd=ROOT)
        if result.returncode:
            failures.append(f"{script} exited {result.returncode}: {folder / output}")
    print(f"Report: {folder / 'report.md'}", flush=True)
    if failures:
        raise RuntimeError("; ".join(failures))
    print("Verification PASS", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("setup", "start", "verify", "chaos"))
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--name", default="origin-stale")
    parser.add_argument("--duration", type=int)
    args = parser.parse_args()
    checkout = prerequisites(args.checkout)
    if args.mode != "start":
        if subprocess.run(["minikube", "status", "--profile", "minikube"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            run(["minikube", "start", "--profile", "minikube", "--keep-context"])
        run(["make", "venv"])
        python = STATE / "venv" / "bin" / "python"
        if Path(sys.prefix).resolve() != (STATE / "venv").resolve():
            os.execv(str(python), [str(python), str(ROOT / "bootstrap.py"), *sys.argv[1:]])
        run(["bash", "start-stack.sh"])
    start(checkout)
    if args.mode == "verify":
        verify()
    elif args.mode == "chaos":
        lifecycle.run_chaos(args.name, args.duration)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
