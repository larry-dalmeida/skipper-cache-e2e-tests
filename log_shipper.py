#!/usr/bin/env python3
"""Tail Skipper's log file and push its lines to Loki."""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / ".runtime" / "skipper.log"
PUSH_URL = "http://127.0.0.1:3100/loki/api/v1/push"
BATCH = 500


def push(lines):
    now = time.time_ns()
    values = [[str(now + index), line] for index, line in enumerate(lines)]
    body = json.dumps({"streams": [{"stream": {"job": "skipper"},
                                    "values": values}]}).encode()
    request = urllib.request.Request(PUSH_URL, data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5):
        pass


def main():
    offset = 0
    pending = ""
    while True:
        try:
            size = LOG.stat().st_size
        except FileNotFoundError:
            time.sleep(0.5)
            continue
        if size < offset:
            # Skipper restarted and truncated its log.
            offset, pending = 0, ""
        if size > offset:
            with LOG.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(size - offset)
            parts = (pending + chunk.decode("utf-8", "replace")).split("\n")
            lines = [line for line in parts[:-1] if line]
            try:
                for start in range(0, len(lines), BATCH):
                    push(lines[start:start + BATCH])
            except OSError as error:
                # Offset and pending are unchanged, so the same bytes are retried.
                print(f"Loki push failed, retrying: {error}", file=sys.stderr, flush=True)
                time.sleep(2)
                continue
            offset += len(chunk)
            pending = parts[-1]
        time.sleep(0.5)


if __name__ == "__main__":
    main()
