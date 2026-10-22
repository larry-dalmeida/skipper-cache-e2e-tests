#!/usr/bin/env python3
"""Wire-level checks against Skipper and a go-httpbin origin; no fetch stubs."""
import argparse
import concurrent.futures
import datetime
import json
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def origin_count(namespace, since, marker):
    logs = subprocess.check_output(
        ["kubectl", "--context", "minikube", "-n", namespace, "logs",
         "deployment/mock-backend", "--since-time=" + since], text=True,
        stderr=subprocess.PIPE, timeout=15,
    )
    return sum(marker in line and "method=GET" in line and "uri=" in line
               for line in logs.splitlines())


def fetch(base, path, vary=False):
    with urllib.request.urlopen(base + path, timeout=15) as response:
        require(response.status == 200, f"HTTP {response.status}, expected 200")
        body = json.load(response)
        if vary:
            require(response.headers.get("Vary") == "*", "missing Vary: * header")
            require(body.get("Vary") == ["*"], "unexpected Vary response body")
        else:
            actual = urllib.parse.urlsplit(body["url"])
            expected = urllib.parse.urlsplit(path)
            require(actual.path == expected.path, "response path mismatch")
            require(urllib.parse.parse_qs(actual.query) ==
                    urllib.parse.parse_qs(expected.query), "response query mismatch")
        return body


def verify(args):
    run_id = uuid.uuid4().hex
    since = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    report = {"label": args.label, "skipper_sha": args.sha, "utc": since,
              "run_id": run_id, "cases": []}

    def case(name, action):
        try:
            details = action()
            result = {"name": name, "status": "PASS", **details}
        except Exception as error:
            result = {"name": name, "status": "FAIL", "error": str(error)}
        report["cases"].append(result)
        print(f'{result["status"]}: {name}: {json.dumps(result, sort_keys=True)}',
              flush=True)

    def count(marker):
        # Wait for access logs to settle; require repeated stable observations.
        previous, stable, deadline = None, 0, time.monotonic() + 8
        while time.monotonic() < deadline:
            value = origin_count(args.namespace, since, marker)
            stable = stable + 1 if value == previous else 0
            if value > 0 and stable >= 3:
                return value
            previous = value
            time.sleep(0.2)
        raise AssertionError(f"origin logs did not settle (last count={previous})")

    def single():
        marker = run_id + "-single"
        path = "/get?run=" + marker + "&check=path-query"
        cold, warm = fetch(args.base, path), fetch(args.base, path)
        calls = count(marker)
        require(cold == warm, "warm response body differs")
        require(calls == 1, f"origin calls={calls}, expected 1 for cold+warm")
        return {"responses": 2, "origin_calls": calls, "http_status": 200,
                "body_path_query": "matched"}

    def burst():
        marker = run_id + "-burst"
        path = "/delay/1?run=" + marker + "&check=path-query"
        barrier = threading.Barrier(10, timeout=15)

        def request(_):
            barrier.wait()
            return fetch(args.base, path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(pool.map(request, range(10)))
        calls = count(marker)
        warm = fetch(args.base, path)
        after_warm = count(marker)
        require(calls == 1 and after_warm == 1,
                f"origin cold={calls}, after warm={after_warm}; expected 1,1")
        require(all(body == responses[0] for body in responses + [warm]),
                "burst/warm response bodies differ")
        return {"concurrent_responses": 10, "origin_cold": calls,
                "origin_after_warm": after_warm, "http_status": 200,
                "body_path_query": "matched"}

    def vary():
        marker = run_id + "-vary"
        path = "/response-headers?Vary=%2A&run=" + marker
        for _ in range(3):
            body = fetch(args.base, path, vary=True)
            require(body.get("run") == [marker], "Vary body query mismatch")
        calls = count(marker)
        require(calls == 3, f"origin calls={calls}, expected 3")
        return {"responses": 3, "origin_calls": calls, "http_status": 200}

    case("single cold then warm", single)
    case("synchronized 10-request cold burst then warm", burst)
    case("Vary star never cached", vary)
    report["success"] = all(c["status"] == "PASS" for c in report["cases"])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f'Result: {"PASS" if report["success"] else "FAIL"}; {output}')
    return 0 if report["success"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:9999")
    parser.add_argument("--namespace", default="cache-e2e")
    parser.add_argument("--label", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", default=".runtime/result.json")
    raise SystemExit(verify(parser.parse_args()))
