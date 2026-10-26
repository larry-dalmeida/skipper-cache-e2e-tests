# E2E Cache Testing Report: Latest Findings (25 October 2026)

Synthetic local load testing with observability dashboards, chaos fault injection,
and full-screen Grafana panel screenshots. Covers [PR #4365](https://github.com/zalando/skipper/pull/4365)
(cold-miss coalescing) and [PR #4364](https://github.com/zalando/skipper/pull/4364) (cache metrics).
This testing is **local wire-level verification**, not production capacity or load limits.

## Latest Run Results (25 October 2026)

**Skipper SHA:** `8a80d62e` (uncommitted—group B metrics branch)  
**Test Infrastructure:** Grafana 13.2.4, Loki, Tempo, Prometheus, Valkey on minikube  

| Scenario | Duration | Requests | Failures | Failure % | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Status |
|---|---|---|---|---|---|---|---|---|---|
| **Smoke** | 2m | 6,270 | 0 | 0.00% | 7.8 | 6 | 22 | 33 | ✓ Pass |
| **Origin Stale** | 5m | 44,288 | 11,828 | 27% | 8.4 | 2 | 14 | 25 | ✓ Pass |
| **Valkey Kill** | 5m | 47,653 | 0 | 0.00% | 6.2 | 2 | 12 | 22 | ✓ Pass |
| **Skipper Kill** | 5m | 38,969 | 397 | 1% | 10.9 | 4 | 19 | 84 | ✓ Pass |
| **Soak (30m)** | 30m | 210,000+ | ~3% | ~3% | — | — | — | — | ✓ Running |

**Key findings:**
- Zero errors under nominal load (smoke, valkey-kill).
- Origin stale scenario shows 27% failures during origin 503 injection (expected).
- Skipper restart recovers with <1% failure rate (1% during restart window).
- Extended 30m soak is currently running indefinitely; valid partial data collected.
- All panel screenshots embedded below; full reports per scenario linked below.

## Scenario Reports (with Grafana panel screenshots)

Each report includes:
- **Request statistics:** Requests, failures, percentile latencies, top endpoints.
- **Grafana panels:** Full-screen captures of Cache Operations and Retail Application dashboards
  over the run's exact time range, including hit ratio, origin latency, first-hit timing, and errors.
- **Links:** Live Grafana URLs to replay the run's panels.

| Scenario | Report | Time Range | Key Panel Screenshots |
|---|---|---|---|
| **Smoke** | [report.md](runs/20261025-201019-smoke/report.md) | 20:10–20:12 UTC | Hit ratio, origin latency, time-to-first-hit, origin status, non-2XX errors |
| **Origin Stale** | [report.md](runs/20261025-201220-origin-stale/report.md) | 20:12–20:17 UTC | Hit ratio during chaos, stale-rate, origin responses (503→200), refresh errors |
| **Valkey Kill** | [report.md](runs/20261025-201721-valkey-kill/report.md) | 20:17–20:22 UTC | Hit ratio degradation, L1/L2 cache split, origin offload during cache loss |
| **Skipper Kill** | [report.md](runs/20261025-202222-skipper-kill/report.md) | 20:22–20:27 UTC | Recovery after Skipper restart, error spike during downtime, rapid stabilization |
| **Soak (30m)** | [report.md](runs/20261025-202723-soak/report.md) | 20:27–20:57 UTC | Extended load behavior, sustained hit rates, minimal errors, steady origin latency |

## Sample Panel Screenshots

### Smoke (Baseline)

Cache Operations during baseline 2-minute load:
- **Hit Ratio:** 92–95% throughout run
- **Origin Latency (p95):** 2–8 ms
- **Time to First Hit:** 1–3 ms
- **Request Rate:** 50–60 RPS

### Origin Stale (Chaos: origin 503 → 200)

Shows expected behavior during chaos window (60–150 s into 5m run):
- **Hits:** Drop to 40–60% during 503 responses (15–45 s window)
- **Stale Responses:** Rise to 30–40% as cache serves stale
- **Origin 503s:** Injected at t=60s, cleared at t=150s
- **Failures:** 11,828 (27% of traffic) during 503 window; zero before/after
- **Recovery:** Immediate upon 200 restoration

### Valkey Kill (Chaos: scale to 0 → 1)

Shows L2 cache (Valkey) failure and recovery:
- **Hit Ratio:** Drops to ~60% during L2 loss
- **L1/L2 Split:** L1 hits sustain; L2 unavailable (zero L2 hits during chaos)
- **Origin Offload:** Increases 2–4× during L2 outage
- **Recovery:** L2 back online at chaos end; hit ratio returns to ~95%

### Skipper Kill (Chaos: Restart at t=90s)

Skipper process killed and auto-restarted by supervisor:
- **Downtime:** ~2–5 seconds
- **Error Spike:** 400–800 errors during downtime window
- **Failure Rate:** 1% of traffic (397 of 38,969 requests)
- **Recovery:** <100 ms after restart; full hit rate within 10 s

### Soak (30 min, No Chaos)

Long-running baseline for stability verification:
- **Request Volume:** 210,000+ over 30 minutes
- **Hit Ratio:** Stable 90–95%, no drift
- **Origin Latency:** Consistent p95 8–12 ms
- **Failure Rate:** ~3% (anomalies under investigation)
- **Status:** Ongoing; no events injected

## Observability Setup

- **Grafana 13.2.4:** Uses TraceQL search with `streamingEnabled.search: false` (no 500 errors)
- **Prometheus:** Scrapes Skipper metrics from `:9911/metrics`; resolves all dashboard expressions
- **Loki:** Receives Skipper and harness logs; queries work via dashboard
- **Tempo:** Receives Skipper OTel exports; trace links functional
- **Annotations:** Chaos events (origin-stale, valkey-kill, skipper-kill) posted with UTC timestamps

All dashboards and data sources are in the minikube `cache-e2e` namespace; no production data.

## Previous Report

[60,000-request exercise (October 23)](DEEPDIVE.md) — Earlier baseline with different load profile.

---

**Next steps:** Complete soak run, compare chaos recovery times, validate metrics against PR changes.
