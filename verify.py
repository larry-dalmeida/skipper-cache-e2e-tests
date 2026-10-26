#!/usr/bin/env python3
"""Wire-level checks against Skipper and the harness-controlled Python origin; no fetch stubs."""
import argparse
import concurrent.futures
import datetime
import json
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def origin_count(control, marker):
    query = urllib.parse.urlencode({"case": marker})
    with urllib.request.urlopen(control + "/events?" + query, timeout=15) as response:
        return json.load(response)["counts"]["total"]


def configure(control, marker, **behavior):
    payload = {"case": marker, "cache_control": "public, max-age=30", **behavior}
    request = urllib.request.Request(control + "/config", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        require(response.status == 200, "origin configuration failed")


def fetch(base, path, vary=False):
    with urllib.request.urlopen(base + path, timeout=15) as response:
        require(response.status == 200, f"HTTP {response.status}, expected 200")
        body = json.load(response)
        if vary:
            require(response.headers.get("Vary") == "*", "missing Vary: * header")
        expected = urllib.parse.urlsplit(path)
        require(body["path"] == expected.path, "response path mismatch")
        require(urllib.parse.parse_qs(body["query"]) ==
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
        return origin_count(args.control, marker)

    def single():
        marker = run_id + "-single"
        configure(args.control, marker)
        path = "/get?case=" + marker + "&check=path-query"
        cold, warm = fetch(args.base, path), fetch(args.base, path)
        calls = count(marker)
        require(cold == warm, "warm response body differs")
        require(calls == 1, f"origin calls={calls}, expected 1 for cold+warm")
        return {"responses": 2, "origin_calls": calls, "http_status": 200,
                "body_path_query": "matched"}

    def burst():
        marker = run_id + "-burst"
        configure(args.control, marker, delay=1)
        path = "/delay/1?case=" + marker + "&check=path-query"
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
        configure(args.control, marker, vary="*")
        path = "/response-headers?case=" + marker
        for _ in range(3):
            fetch(args.base, path, vary=True)
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
    parser.add_argument("--control", default="http://127.0.0.1:9091")
    parser.add_argument("--label", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", default=".runtime/result.json")
    raise SystemExit(verify(parser.parse_args()))
