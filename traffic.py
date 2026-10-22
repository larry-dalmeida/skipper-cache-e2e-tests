#!/usr/bin/env python3
"""Small continuous cold/L1/L2 workload; launcher L1 TTL=2s; no cache flush."""
import time
import uuid
import urllib.request

if __name__ == "__main__":
    while True:
        url = "http://127.0.0.1:9999/delay/0.1?demo=" + uuid.uuid4().hex
        try:
            for _ in range(2):
                with urllib.request.urlopen(url, timeout=10) as response:
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                    response.read()
            # Let the launcher's 2s L1 warming expire, while L2 remains fresh for 30s.
            time.sleep(2.2)
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                response.read()
            print("PASS: synthetic cold/L1/post-expiry HTTP 200 (see verify-tiers.py for tier assertions)",
                  flush=True)
        except Exception as error:
            print(f"FAIL: synthetic traffic: {error}", flush=True)
        time.sleep(10)
