#!/usr/bin/env python3
"""Foreground supervisors with identity-checked PID records, never broad kills."""
import csv
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalogue  # noqa: E402
import chaos  # noqa: E402
import locust_workload as wl  # noqa: E402
import report  # noqa: E402

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".runtime"
STATE.mkdir(exist_ok=True)

# Skipper exports traces to Tempo's OTLP/HTTP receiver through the forwards.
OTEL_OPTIONS = ("{tracesExporter: otlp, resourceAttributes: {service.name: skipper-local}, "
                "exporterOtlp: {protocol: http/protobuf, endpoint: http://127.0.0.1:4318}}")


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


def supervise(commands, probes, name, log_file=None, restart=False):
    """Runs children until stopped. With restart=True an exited child is respawned after 2s."""
    record = STATE / (name + ".json")
    if record.exists():
        raise RuntimeError(f"{record} exists; use teardown.sh first")
    ports = {"skipper": (9999, 9911),
             "locust": (8089,)}.get(name, ())
    for port in ports:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    record.touch(exist_ok=False)
    children = []
    output = open(log_file, "w") if log_file else None

    def stop(*_):
        raise KeyboardInterrupt

    def spawn(command):
        return subprocess.Popen(command, cwd=ROOT, stdout=output,
                                stderr=subprocess.STDOUT if output else None)

    def save():
        record.write_text(json.dumps({
            "supervisor": {"pid": os.getpid(), "identity": identity(os.getpid())},
            "children": [{"pid": p.pid, "identity": identity(p.pid)}
                         for p in children]}))

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for command in commands:
            children.append(spawn(command))
        save()
        for probe in probes:
            ready(probe, children=children)
        print(f"READY: {name}: " + ", ".join(probes), flush=True)
        while True:
            for index, child in enumerate(children):
                if child.poll() is None:
                    continue
                if not restart:
                    raise RuntimeError("a supervised process exited")
                print(f"{name} child {child.pid} exited ({child.returncode}); restarting", flush=True)
                time.sleep(2)
                children[index] = spawn(commands[index])
                save()
            time.sleep(0.5)
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
        if output:
            output.close()


UI_SERVICES = (("grafana", "/api/health"), ("prometheus", "/-/ready"),
               ("loki", "/ready"), ("tempo", "/ready"))
# Internal ports Skipper and Locust need at fixed addresses.
FORWARDS = (("mock-backend", "8080:8080"), ("valkey", "6379:6379"), ("tempo", "4318:4318"))


def keep_alive(name, commands, ready_check):
    """Run each (label, argv) until stopped, restarting exits; live PIDs are recorded for teardown."""
    record = STATE / (name + ".json")
    if record.exists():
        raise RuntimeError(f"{record} exists; run make stop first")
    record.touch(exist_ok=False)
    lock = threading.Lock()
    live = {}
    stopping = threading.Event()

    def save():
        with lock:
            if stopping.is_set():
                return
            children = [{"pid": p.pid, "identity": identity(p.pid)} for p in live.values()]
            record.write_text(json.dumps({
                "supervisor": {"pid": os.getpid(), "identity": identity(os.getpid())},
                "children": children}))

    def runner(index, label, argv):
        with open(STATE / f"{name}-{label}.log", "w") as log:
            while True:
                with lock:
                    if stopping.is_set():
                        return
                    child = subprocess.Popen(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                    live[index] = child
                save()
                child.wait()
                with lock:
                    live.pop(index, None)
                if stopping.is_set():
                    return
                print(f"{name}/{label} exited with {child.returncode}; restarting", flush=True)
                time.sleep(2)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threads = [threading.Thread(target=runner, args=(i, label, argv), daemon=True)
               for i, (label, argv) in enumerate(commands)]
    try:
        for thread in threads:
            thread.start()
        ready_check()
        print(f"READY: {name}", flush=True)
        while not stopping.is_set():
            time.sleep(0.5)
    finally:
        with lock:
            stopping.set()
            procs = list(live.values())
        for child in procs:
            child.terminate()
        for child in procs:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        record.unlink(missing_ok=True)


def ui_ready(timeout=120):
    """Wait for each `minikube service` tunnel to print its URL, then check the UI answers."""
    deadline = time.monotonic() + timeout
    urls = {}
    for label, path in UI_SERVICES:
        log = STATE / f"ui-{label}.log"
        found = None
        while found is None:
            found = re.search(r"http://127\.0\.0\.1:\d+", log.read_text() if log.exists() else "")
            if found is None:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"no URL for {label}; see {log}")
                time.sleep(0.5)
        urls[label] = found.group(0)
        ready(urls[label] + path, timeout=max(1.0, deadline - time.monotonic()))
    lines = [f"{label}: {url}" for label, url in urls.items()]
    lines.append(f"cache-operations: {urls['grafana']}/d/cache-performance-local/cache-operations")
    lines.append(f"retail-application: {urls['grafana']}/d/retail-application-local/retail-application")
    (STATE / "urls.txt").write_text("\n".join(lines) + "\n")


STOP_TARGETS = ("locust", "traffic", "skipper", "logs", "forwards", "origin", "ui")


def apply_origin_cases(control="http://127.0.0.1:9091"):
    """Register the caching-need scenarios on the mock origin once it is ready."""
    try:
        ready(control + "/health", timeout=90)
        for case, config in catalogue.ORIGIN_CASES.items():
            body = json.dumps({"case": case, **config}).encode()
            request = urllib.request.Request(control + "/config", data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=5).read()
    except Exception as error:
        print(f"origin case configs not applied: {error}", file=sys.stderr)


def teardown(names=STOP_TARGETS):
    for name in names:
        if name not in STOP_TARGETS:
            raise ValueError("Stop target must be locust, traffic, skipper, logs, forwards, origin, or ui")
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


def run_locust(scenario, shape=None, chaos=None):
    """Runs Locust for one scenario; each run gets results/runs/<timestamp>-<scenario>/.

    `chaos(run, stop)`, if given, runs in a helper thread beside a timed Locust run and must
    return once `stop` is set; it is joined before the run is finished.
    """
    if shape is None:
        if scenario not in wl.SCENARIOS:
            raise SystemExit(f"unknown scenario {scenario!r}; choose from {', '.join(wl.SCENARIOS)}")
        shape = wl.SCENARIOS[scenario]
    try:
        urllib.request.urlopen("http://127.0.0.1:9999/get?locust-precheck=1", timeout=3)
    except OSError as error:
        raise SystemExit(f"Skipper is not reachable on 127.0.0.1:9999 ({error}); start it first")
    run = ROOT / "results" / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{scenario}"
    run.mkdir(parents=True)
    commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    skipper_file = STATE / "skipper.sha"
    skipper_sha = skipper_file.read_text().strip() if skipper_file.exists() else None
    started = time.time()
    (run / "run.json").write_text(json.dumps({
        "scenario": scenario, "duration_seconds": shape["duration"], "max_vus": shape["max_vus"],
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "started_ms": int(started * 1000), "cache_e2e_commit": commit, "skipper_sha": skipper_sha},
        indent=2) + "\n")
    command = [sys.executable, "-m", "locust", "-f", str(ROOT / "locustfile.py"),
               "--host", "http://127.0.0.1:9999", "--stop-timeout", "10",
               "--max-vus", str(shape["max_vus"]), "--csv", str(run / "locust"),
               "--logfile", str(run / "locust.log")]
    caffeinate = [["caffeinate", "-dims"]] if shutil.which("caffeinate") else []
    try:
        if shape["duration"]:
            # Timed runs exit on their own; the web UI is only for endless runs.
            wrapped = ["caffeinate", "-dims"] + command if caffeinate else command
            stop = threading.Event()
            worker = threading.Thread(target=chaos, args=(run, stop), daemon=True) if chaos else None
            if worker:
                worker.start()
            try:
                env = dict(os.environ, LOCUST_CHAOS_TOLERANT="1") if chaos else None
                subprocess.run(wrapped + ["--headless", "--duration", str(shape["duration"])],
                               cwd=ROOT, check=False, env=env)
            finally:
                if worker:
                    stop.set()
                    worker.join(timeout=300)
                    if worker.is_alive():
                        print("chaos worker still restoring after 300s; check the stack", file=sys.stderr)
        else:
            command += ["--web-host", "127.0.0.1", "--web-port", "8089"]
            supervise([command] + caffeinate, ["http://127.0.0.1:8089/"], "locust",
                      log_file=run / "console.log")
    finally:
        finish_run(run)
    print(f"results: {run}", flush=True)
    trip = report.guard_trip(run)
    if trip is not None:
        raise SystemExit(f"run aborted early: {trip}")


def run_chaos(name, duration=None):
    """Timed Locust run with scheduled faults; see chaos.SCENARIOS for the fault plan."""
    if name not in chaos.SCENARIOS:
        raise SystemExit(f"unknown chaos scenario {name!r}; choose from {', '.join(chaos.SCENARIOS)}")
    duration = duration or chaos.BASE_DURATION
    steps = chaos.schedule(name, duration)
    grafana = grafana_base()
    actions = chaos.default_actions(identity)

    def worker(run, stop):
        chaos.run_schedule(name, duration, steps, run, stop, grafana, actions)

    run_locust(name, shape={"duration": duration, "max_vus": wl.MAX_VUS_CEILING}, chaos=worker)


def grafana_base():
    urls = STATE / "urls.txt"
    return report.grafana_base(urls.read_text()) if urls.exists() else None


def finish_run(run):
    """Stamps the end time and writes report.md. Best effort: failures are printed, never raised."""
    ended = time.time()
    try:
        meta_path = run / "run.json"
        meta = json.loads(meta_path.read_text())
        meta.update(ended=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ended)),
                    ended_ms=int(ended * 1000))
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    except Exception as error:
        print(f"end time not recorded for {run.name}: {error}", file=sys.stderr, flush=True)
    try:
        path = report.write_report(run, grafana_base())
        print(f"report: {path}", flush=True)
    except Exception as error:
        print(f"report not written for {run.name}: {error}", file=sys.stderr, flush=True)


def latest_run():
    runs = ROOT / "results" / "runs"
    folders = sorted(p for p in runs.iterdir() if (p / "run.json").exists()) if runs.exists() else []
    if not folders:
        raise SystemExit("no runs yet")
    return folders[-1]


def list_runs():
    runs = ROOT / "results" / "runs"
    if not runs.exists():
        print("no runs yet")
        return
    print(f"{'run':<34}{'scenario':<10}{'requests':>10}{'failures':>10}{'p95_ms':>9}")
    for run in sorted(runs.iterdir()):
        meta = run / "run.json"
        stats = run / "locust_stats.csv"
        if not meta.exists():
            continue
        scenario = json.loads(meta.read_text())["scenario"]
        requests = failures = p95 = "-"
        if stats.exists():
            with stats.open() as handle:
                for row in csv.DictReader(handle):
                    if row["Name"] == "Aggregated":
                        requests, failures = row["Request Count"], row["Failure Count"]
                        p95 = row["95%"]
        print(f"{run.name:<34}{scenario:<10}{requests:>10}{failures:>10}{p95:>9}")


if __name__ == "__main__":
    if sys.argv[1] == "stop":
        teardown(sys.argv[2:] or STOP_TARGETS)
    elif sys.argv[1] == "locust":
        run_locust(sys.argv[2] if len(sys.argv) > 2 else "endless")
    elif sys.argv[1] == "chaos":
        if len(sys.argv) < 3:
            raise SystemExit(f"usage: lifecycle.py chaos <{'|'.join(chaos.SCENARIOS)}> [duration_seconds]")
        run_chaos(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
    elif sys.argv[1] == "runs":
        list_runs()
    elif sys.argv[1] == "report":
        run = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else latest_run()
        if not run.is_dir():
            raise SystemExit(f"not a run folder: {run}")
        print(report.write_report(run, grafana_base()))
    elif sys.argv[1] == "traffic":
        supervise([[sys.executable, "-u", str(ROOT / "traffic.py")]],
                  ["http://127.0.0.1:9999/get?traffic-readiness=1"], "traffic")
    elif sys.argv[1] == "forwards":
        keep_alive("forwards", [
            (service, ["kubectl", "--context", "minikube", "-n", "cache-e2e",
                       "port-forward", "--address=127.0.0.1", "svc/" + service, ports])
            for service, ports in FORWARDS],
            lambda: ready("http://127.0.0.1:8080/get"))
    elif sys.argv[1] == "ui":
        keep_alive("ui", [
            (label, ["minikube", "service", label, "-n", "cache-e2e", "--url"])
            for label, _ in UI_SERVICES], ui_ready)
    elif sys.argv[1] == "urls":
        print((STATE / "urls.txt").read_text(), end="")
    elif sys.argv[1] == "await":
        name = sys.argv[2]
        out = STATE / (name + "-run.out")
        pid = int((STATE / (name + ".pid")).read_text())
        started = time.monotonic()
        while True:
            if f"READY: {name}" in (out.read_text() if out.exists() else ""):
                print(f"{name} ready", flush=True)
                break
            try:
                os.kill(pid, 0)
            except OSError:
                raise SystemExit(f"{name} exited before READY; see {out}")
            if time.monotonic() - started > 240:
                raise SystemExit(f"{name} not ready after 240s; see {out}")
            time.sleep(0.5)
    elif sys.argv[1] == "origin":
        origin = ROOT / "origin" / "mock_origin.py"
        threading.Thread(target=apply_origin_cases, daemon=True).start()
        supervise([[sys.executable, "-u", origin, "--http-port", "8081",
                     "--control-port", "9091"]],
                  ["http://127.0.0.1:9091/health", "http://127.0.0.1:8081/"],
                  "origin", log_file=STATE / "origin.log")
    elif sys.argv[1] == "logs":
        supervise([[sys.executable, "-u", str(ROOT / "log_shipper.py")]],
                  ["http://127.0.0.1:3100/ready"], "logs")
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
        # Runs read this so each report names the Skipper revision it measured.
        (STATE / "skipper.sha").write_text(sha + "\n")
        print(f"Starting Skipper {sha}", flush=True)
        supervise([[str(binary), "-address=127.0.0.1:9999",
                    "-metrics-flavour=prometheus", "-proxy-preserve-host",
                    "-serve-route-counter", "-serve-route-metrics", "-enable-swarm",
                    "-enable-l2-cache", "-swarm-valkey-urls=127.0.0.1:6379",
                    "-swarm-valkey-dial-timeout=2s",
                    "-swarm-valkey-conn-timeout=2s",
                    "-cache-l1-ttl=2s",
                    "-wait-for-healthcheck-interval=0",
                    "-routes-file=" + str(ROOT / "routes.eskip"),
                    "-open-telemetry=" + OTEL_OPTIONS]],
                  ["http://127.0.0.1:9999/get?readiness=" + sha,
                   "http://127.0.0.1:9911/metrics"], "skipper",
                  log_file=STATE / "skipper.log", restart=True)
