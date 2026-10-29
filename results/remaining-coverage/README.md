# Remaining cache behaviours: bounded local acceptance

All HTTP runtime probes use exact integrated source
`372b07b4f73fd500182bf8f960fb7e2d5ee51639`, native macOS/arm64 Skipper,
Go's selected 1.27.3 toolchain, native Prometheus 2.55.1, and owned
digest-pinned Valkey 8.1.1. The source remains clean. Exact process arguments,
binary/tool hashes, metric deltas, HTTP payloads and dashboard queries are
in [`integrated372b-result.json`](integrated372b-result.json).

## What was newly exercised

| Prior gap | Actual runtime evidence and independent oracle |
|---|---|
| Capacity eviction and eviction panel | 400 unique cold responses under a 358,400-byte L1 budget, divided across 256 shards. Final run: **204 evictions = 204 absent cache-only probes**, with 196 retained hits and exactly 400 origin calls. No oversized-entry series. Bytes remain below capacity; retained membership matches the entries gauge. This is actual per-shard capacity pressure, not simulated counters or host-wide memory exhaustion. |
| Queue drops | Origin barrier holds one worker while 256 distinct stale jobs fill the actual queue. Three more requests serve L1 STALE and emit exactly three drops. Release yields **257 started = 257 success**, total 517 origins (260 primes + 257 refreshes). The three dropped paths have only their priming origin event. |
| L1-only stale serving | All 260 queue-path responses are HTTP 200/STALE from L1; there is no L2 configured in that process. |
| Short-budget SIE faults | L1 TTL 300ms, SWR 100ms, SIE 3s. After TTL+SWR, origin 500/502/503/504 and disconnect each produce the original HTTP 200 payload/STALE, one real origin attempt and `stale/l1`. After retention, all four HTTP faults are returned rather than stale content. |
| Valkey disconnect and recovery | An owned RESP-aware TCP relay rejects data commands, while preserving connection handshakes. Cache-only returns 504/error.none; cold recovery fetches origin and falls back to L1; subsequent GET is fresh/l1; unsafe invalidation remains bypass.none with DELETE errors. Reconnect restores fresh/l2 reads and successful persistence; a fresh peer reads the repaired L2 value without origin. |
| Visible attempts versus logical operations | Every network probe compares exact operation/purpose/outcome deltas and request classification with HTTP/origin activity and relay transmissions. The failure run records 2 + 4 + 1 + 3 rejected wire commands for the four fault probes; none reaches Valkey MONITOR. `DisableRetry=true` is the actual client configuration. Connection handshakes are not data-operation retries. This does not claim support for counting arbitrary hidden retries in the L2 metric contract. |
| Dashboard query-through-proxy | Five real repository targets, panels 31, 60, 19, 34, 53, return finite **positive** exercised series directly and through owned Grafana 11.3.0. Both queries use the exact same historical evaluation timestamp and their result vectors match. |
| Grafana UI | Imported the real dashboard and loaded it in the browser canvas. All four Skipper scrape targets showed OK. Panel 60 visually rendered positive eviction ops/s and then zero when pressure stopped. Browser same-origin proxy returned 26 finite / 12 positive range points. See [`browser-ui.json`](browser-ui.json); screenshot remains in the conversation tool evidence. |
| Encode / unknown purpose | Implementation child ran seven direct StorageAPI cases five times under race against its own actual Valkey and Prometheus collector. See below. |

Eviction cardinality can differ across runs because UUID-scoped cache keys
hash to different shards. The assertion is exact equality between actual
evictions, absent membership and retained entry count, not a hardcoded value.
The browser inspection used the preceding successful run (201 evictions);
the final same-time proxy comparison reran the complete workload (204).

## Direct StorageAPI coverage, not an HTTP claim

The implementation child appended only
`filters/cache/l2_storage_api_test.go` in
`ff7e9b03545203567dc40969b5b47d1da4f5760a`, parent `372b07b4`.
Independent `git diff` confirmed 165 test-only lines, no runtime/docs delta.
The new tip was separately built and passed unchanged L1 and coalescing
checks; those reports are included as `ff7-*`.

The child supplied the actual-Valkey test report and complete repeat-race
log. Three encode purposes (response/metadata/unknown) each emit processing
encode +1, return an error, and issue **zero** SET/GET/DEL calls according to
Valkey INFO, with no operation/duration/fallback samples.
Four unknown contexts (absent/explicit/invalid/wrong type) each perform
SET1/GET3/DEL1, matching SET-success1, GET-success2, GET-miss1, DELETE-success1
and duration observations 1/3/1. Silent GET is included.

These inputs require direct Storage callers; malformed timestamps cannot
be created by normal HTTP proxy timestamps. They are not HTTP-triggered
faults and do not imply arbitrary clients can produce encode errors.

```bash
SKIPPER_TEST_OWNED_VALKEY_ADDR=127.0.0.1:32861 go test -race -v -count=5 ./filters/cache -run '^TestValkeyStorage_StorageAPIObservability$'
```

That disposable address is historical and has been removed. Repeating the
test requires a new owned instance. The child reports fmt/lint/cache+metrics
race pass, but **full source shortcheck fails** in Docker-provider/etcd and
JWT fixture suites. The compact error record and full successful focused
log are included; do not describe the entire Skipper source suite as green.

## Commands

```bash
python3 verify_cache_remaining.py --checkout .runtime/skipper-observability372b --output .runtime/remaining372b-final --grafana
python3 verify_cache_remaining.py --checkout .runtime/skipper-observability372b --output .runtime/remaining372b-attempt2 --grafana --ui-hold-seconds 180
python3 verify_l1_freshness.py --checkout .runtime/skipper-observability-ff7 --sha ff7e9b03545203567dc40969b5b47d1da4f5760a --output .runtime/ff7-l1
python3 verify_candidate_behaviour.py --l1-result .runtime/ff7-l1/result.json --output .runtime/ff7-coalescing
make test
```

All **170** harness unit tests pass, including seven new RESP/sample-oracle
tests. The published `verify_cache_dashboards.py` and
`verify_candidate_behaviour.py` are the earlier scratch launchers promoted
to repository paths (root resolution and generic report label adjusted).
Existing L1 and coalescing assertions were not relaxed.

The initial remaining-coverage attempt failed on an invalid **test fixture**
(`cache(...,"0s",...)` SWR is rejected by the parser, causing a 404 seed).
Its failure report/log are retained. The corrected positive-100ms-SWR
scenario actually ran and passed; this was not a Skipper runtime defect.

## Blackhole follow-up

The follow-up on exact `ff7e9b03545203567dc40969b5b47d1da4f5760a`
also **holds connections open and suppresses data-command responses**,
instead of immediately disconnecting them. A real-socket unit check proves
that this helper times out rather than returning EOF and permits a fresh
connection after restoration.

[`ff7-blackhole-result.json`](ff7-blackhole-result.json) records the full
remaining-behaviour workload, including this addition. Cache-only returns
504/MISS and `error/none` after **6.182s** with two GET-error observations;
cold fetch returns 200/MISS and `error/none` after **9.975s**, with one origin,
three GET errors and one SET error, then successful L1 fallback.
The next request is 200/HIT and `fresh/l1` after its metadata lookup times
out (**3.014s**). Removing the blackhole restores `fresh/l2`, successful
shared persistence and the fresh-peer read.

Every blackholed operation has exactly one observed wire attempt: two for
cache-only, four for cold fetch and one for the subsequent L1 hit. None is
forwarded to actual Valkey, and no decode/encode error is invented.
The final network workload has five origin calls, including the additional
blackholed cold request. Raw relay traces, MONITOR and Skipper timeout logs
are saved alongside the report.

The configured two-second socket/connection budgets are **not a single
two-second end-to-end HTTP deadline**. Sequential metadata/representation/
persistence calls and client reconnection bookkeeping are included in these
observed timings. The bounded integration assertions allow 8s cache-only and
12s cold recovery; they do not assert that every logical operation lasts
exactly two seconds or that retries are represented by L2 operation counters.

```bash
python3 verify_cache_remaining.py --checkout .runtime/skipper-observability-ff7 --sha ff7e9b03545203567dc40969b5b47d1da4f5760a --output .runtime/remaining-ff7-blackhole-final
python3 verify_cache_observability.py --checkout .runtime/skipper-observability-ff7 --sha ff7e9b03545203567dc40969b5b47d1da4f5760a --mode integrated --output .runtime/ff7-publication-metrics
python3 verify_cache_dashboards.py --checkout .runtime/skipper-observability-ff7 --sha ff7e9b03545203567dc40969b5b47d1da4f5760a --output .runtime/ff7-publication-dashboards
```

The same exact ff7 tip passes the full 24-case metric matrix and the existing
43-query dashboard workload (eviction remains unexercised **in that separate
workload**, already exercised by the pressure fixture). All **171** harness
unit tests pass.

An earlier blackhole run passed every behavioural probe but its optional
Grafana container failed the 60-second readiness budget. Its genuine
infrastructure failure report is retained as
`ff7-blackhole-grafana-readiness-error.json`, not relabelled PASS.
The final blackhole run does not launch a redundant Grafana instance;
the earlier successful same-time proxy and real-browser evidence remains
valid for that separately recorded workload.

## Still not claimed

- Twenty-four hours of wall-clock retention, production workload or traffic,
  production dashboards, and real retail locale taxonomy.
- Exhaustive RFC directive/method/validator combinations or every SIE timing
  permutation. Common HTTP faults, a transport fault and the expiry boundary
  are now exercised, not an exhaustive Cartesian product.
- Unreachable-listener dial failure and statistical timeout distributions.
  Immediate disconnect and held-socket/read-blackhole failures are both now
  exercised; this is not every network topology or retry policy.
- Full browser rendering of every panel, Loki/Tempo/retail links. This fixture
  scrapes only Skipper; missing origin scrape/traffic is explicit, not green.
- A globally passing Skipper source suite: unrelated full-shortcheck failures
  remain recorded separately.

All owned processes and containers are stopped/removed. Shared containers,
the shared minikube environment and production were untouched.
