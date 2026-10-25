"""Timed fault injection run alongside Locust: schedules, faults.json and Grafana annotations."""
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".runtime"
ORIGIN_CONTROL = "http://127.0.0.1:9091"
SKIPPER_READY = "http://127.0.0.1:9999/get?chaos-ready=1"
BASE_DURATION = 300

# Offsets are seconds at BASE_DURATION; schedule() scales them to other durations.
SCENARIOS = {
    "origin-stale": {
        "description": "origin default case returns 503; stale hits should absorb the outage",
        "steps": [(60, "origin-503", "origin default case 503"),
                  (150, "origin-200", "origin default case restored to 200")]},
    "valkey-kill": {
        "description": "valkey scaled to 0, then back to 1 with an empty cache",
        "steps": [(90, "valkey-down", "valkey scaled to 0"),
                  (150, "valkey-up", "valkey scaled to 1")]},
    "skipper-kill": {
        "description": "supervised Skipper child killed; supervisor restarts it",
        "steps": [(90, "skipper-kill", "Skipper child PID killed (SIGTERM)")]},
}

# "open" starts a window, "close" ends the last open window, "instant" is self-closing.
ACTION_KINDS = {"origin-503": "open", "origin-200": "close",
                "valkey-down": "open", "valkey-up": "close",
                "skipper-kill": "instant"}


def schedule(name, duration=BASE_DURATION):
    """Pure: (offset_seconds, action, description) steps for a run of `duration` seconds."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown chaos scenario {name!r}; choose from {', '.join(SCENARIOS)}")
    if duration <= 0:
        raise ValueError("duration must be positive")
    steps = [(round(offset * duration / BASE_DURATION), action, description)
             for offset, action, description in SCENARIOS[name]["steps"]]
    if any(offset >= duration for offset, _, _ in steps):
        raise ValueError(f"{name} has a fault at or after the {duration}s duration")
    return steps


def write_faults(path, timeline):
    """Atomic write so a reader never sees a half-written faults.json."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(timeline, indent=2) + "\n")
    os.replace(temporary, path)


def annotate(grafana, scenario, text, time_ms, auth=None):
    """Posts a Grafana annotation. Returns False and warns on failure; never raises."""
    if not grafana:
        print("chaos: no Grafana URL; annotation skipped", file=sys.stderr, flush=True)
        return False
    credentials = auth or os.environ.get("GRAFANA_AUTH", "admin:admin")
    body = json.dumps({"time": time_ms, "tags": ["chaos", scenario], "text": text}).encode()
    request = urllib.request.Request(
        grafana.rstrip("/") + "/api/annotations", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()})
    try:
        urllib.request.urlopen(request, timeout=5).read()
        return True
    except Exception as error:
        print(f"chaos: annotation not posted ({error}); continuing", file=sys.stderr, flush=True)
        return False


def stamp():
    return time.strftime("%H:%M:%S")


def run_schedule(name, duration, steps, run, stop, grafana, actions,
                 annotator=annotate, epoch=time.time, clock=time.monotonic):
    """Runs the steps until Locust sets `stop`, then restores. Writes run/faults.json as it goes."""
    path = run / "faults.json"
    timeline = {"scenario": name, "duration_seconds": duration,
                "run_started_ms": int(epoch() * 1000), "run_ended_ms": None, "faults": []}
    write_faults(path, timeline)
    started = clock()
    open_entry = None
    try:
        for offset, action, description in steps:
            if stop.wait(max(0.0, started + offset - clock())):
                break
            kind = ACTION_KINDS[action]
            entry = {"action": action, "scheduled_offset": offset,
                     "actual_epoch_ms": int(epoch() * 1000), "ended_epoch_ms": None, "detail": None}
            print(f"{stamp()} chaos {action}: {description}", flush=True)
            if kind == "instant":
                annotator(grafana, name, f"chaos start: {description}", entry["actual_epoch_ms"])
            try:
                entry["detail"] = actions[action]()
            except Exception as error:
                entry["detail"] = f"error: {error}"
                print(f"{stamp()} chaos {action} failed: {error}", file=sys.stderr, flush=True)
            if kind == "instant":
                entry["ended_epoch_ms"] = int(epoch() * 1000)
                annotator(grafana, name, f"chaos end: {description}", entry["ended_epoch_ms"])
            elif kind == "open":
                open_entry = entry
                annotator(grafana, name, f"chaos start: {description}", int(epoch() * 1000))
            else:
                entry["ended_epoch_ms"] = entry["actual_epoch_ms"]
                if open_entry is not None:
                    open_entry["ended_epoch_ms"] = entry["actual_epoch_ms"]
                    open_entry = None
                annotator(grafana, name, f"chaos end: {description}", entry["actual_epoch_ms"])
            timeline["faults"].append(entry)
            write_faults(path, timeline)
        stop.wait()
    finally:
        now = int(epoch() * 1000)
        try:
            restore_detail = actions["restore"]()
        except Exception as error:
            restore_detail = f"restore raised: {error}"
            print(f"chaos: restore raised: {error}", file=sys.stderr, flush=True)
        if open_entry is not None:
            open_entry["ended_epoch_ms"] = now
            annotator(grafana, name, f"chaos end: {open_entry['detail'] or open_entry['action']}", now)
        timeline["faults"].append({"action": "restore", "scheduled_offset": None,
                                   "actual_epoch_ms": now, "ended_epoch_ms": now,
                                   "detail": restore_detail})
        timeline["run_ended_ms"] = int(epoch() * 1000)
        write_faults(path, timeline)
    return timeline


def default_actions(identity):
    """Real fault actions. `identity(pid)` returns the ps identity used to verify a PID before a kill."""
    import cachectl

    def origin(status):
        def post():
            body = json.dumps({"case": "default", "status": status}).encode()
            request = urllib.request.Request(ORIGIN_CONTROL + "/config", data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=5).read()
            return f"origin default case status {status}"
        return post

    def valkey(action):
        def run():
            if cachectl.main(["fault", "valkey", action]) != 0:
                raise RuntimeError(f"cachectl fault valkey {action} failed")
            return f"valkey {action}"
        return run

    def skipper_kill():
        record = json.loads((STATE / "skipper.json").read_text())
        child = record["children"][0]
        if identity(child["pid"]) != child["identity"]:
            raise RuntimeError(f"Skipper PID {child['pid']} identity changed; refusing to kill")
        started = time.monotonic()
        os.kill(child["pid"], signal.SIGTERM)
        while time.monotonic() - started < 90:
            time.sleep(0.5)
            current = json.loads((STATE / "skipper.json").read_text())["children"][0]
            if current["pid"] == child["pid"]:
                continue
            try:
                urllib.request.urlopen(SKIPPER_READY, timeout=2).read()
            except OSError:
                continue
            return (f"killed PID {child['pid']}; supervisor restarted as {current['pid']}, "
                    f"ready after {time.monotonic() - started:.1f}s")
        raise RuntimeError("Skipper not ready 90s after kill")

    def restore():
        details = []
        for label, step in (("origin", origin(200)), ("valkey", valkey("up"))):
            try:
                details.append(f"{label}: {step()}")
            except Exception as error:
                details.append(f"{label} restore failed: {error}")
                print(f"chaos: {label} restore failed: {error}", file=sys.stderr, flush=True)
        return "; ".join(details)

    return {"origin-503": origin(503), "origin-200": origin(200),
            "valkey-down": valkey("down"), "valkey-up": valkey("up"),
            "skipper-kill": skipper_kill, "restore": restore}
