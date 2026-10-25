# Skipper HTTP Cache Demo Runbook

Draft for the Skipper HTTP cache talk. Results come from the Grafana 13.2.4 runs
listed in section 5. Values marked **not measured** were not captured in those
runs. Commands were drafted from the code and checked against the stack.

## 1. Pre-flight checklist

- [ ] Docker/Kubernetes stack is up: `make stack` (or `make all` for the full
      path: venv, stack, Skipper, load).
- [ ] Skipper is started from a checkout with the cache metrics build:
      `make start SKIPPER_CHECKOUT=/path/to/skipper`.
      Confirm the build sha in `.runtime/skipper.sha` (**unverified**: file name
      taken from the harness notes, check it exists after `make start`).
- [ ] Run `make urls` and keep the output visible during the demo.
- [ ] Keep the Mac awake: `caffeinate -dims` in a separate terminal (**not verified** on macOS).

### URLs

| Service | URL | Purpose |
|---|---|---|
| Grafana | http://127.0.0.1:3000 | Dashboards and chaos annotations |
| Cache Operations | http://127.0.0.1:3000/d/cache-performance-local | Cache and origin panels |
| Retail Application | http://127.0.0.1:3000/d/retail-application-local | Client-facing panels |
| Prometheus | http://127.0.0.1:9090 | Ad-hoc PromQL |
| Skipper | http://127.0.0.1:9999 | Traffic entry point |
| Skipper metrics | http://127.0.0.1:9911 | Raw `skipper_*` metrics |
| Mock origin control | http://127.0.0.1:9091 | `/config`, `/release`, `/reset`, `/events`, `/metrics` |

## 2. Scenario map

| Talk scenario | Harness command | Duration | Status |
|---|---|---|---|
| 1. Cold vs warm cache | `make load SCENARIO=smoke` | 120 s | Harness has no explicit warm-up step (see 3.1) |
| 2. Hot-family burst | `make load SCENARIO=soak` | 1800 s | Real feature: 3 families boosted x5 every 60 s |
| 3. Origin failure, stale-if-error | `make chaos NAME=origin-stale` | 300 s | Real |
| 4a. Valkey killed mid-run | `make chaos NAME=valkey-kill` | 300 s | Real |
| 4b. Skipper killed mid-run | `make chaos NAME=skipper-kill` | 300 s | Real |

Each run writes `results/runs/<ts>-<scenario>/` with `run.json`, Locust CSVs,
`faults.json` (chaos only), and `report.md`. Generate or refresh a report with
`make report RUN=<dir>`.

### Key definitions (say these before the demo)

- **Cache hit ratio**: fresh hits / (hit + miss + stale) from Skipper's
  `skipper_custom_total{key=~"cache.(hit|miss|stale)"}`.
- **Origin offload** = clamp(1 − origin RPS / client RPS, 0, 1). This is **not**
  the cache hit ratio. Offload can be below the hit ratio when stale or refresh
  traffic reaches the origin.
- **Time to First Hit** = `skipper_cache_first_hit_seconds`, seconds from metrics
  init to the first cache hit.

## 3. Scenarios

### 3.1 Scenario 1: cold vs warm cache

**Goal:** show the cache filling up. Early requests miss and go to origin; later
requests hit.

**Command**

```bash
make load SCENARIO=smoke   # 120 s baseline
```

**Harness behaviour (verified in code)**

- The Locust cohorts are `swr` (en-US) and `hard` (de-DE). Each is a distinct
  cache identity.
- There is **no explicit warm-up or cache-flush step**. "Cold" means the first
  seconds after the cache is empty. Warm-up happens through normal traffic.
- Whether Valkey (L2) is empty at start depends on the previous run. **Unverified.**
  For a truly cold demo, restart the stack before the run.

**Watch**

| Cache Operations | Origin request rate | High at the start, flattening |
| Cache Operations | Time to First Hit | Seconds value, then stable |
| Retail Application | Latency p95 by route | Higher early, then lower as hits dominate |

**Expected signal:** hit ratio climbs from near 0 to a stable plateau. Results
Results (smoke run, 2 min): cumulative hit ratio 0.653 at 30 s and 0.772 at
120 s. Window hit ratio 0.779. p95 latency 22 ms. Time to First Hit reads
17.05 s (process init to first hit, not warm-up). See section 5.

**Audience talk-track:** "Every first request for a new key pays the origin
round trip. After that, Skipper answers from memory. Watch the miss rate fall
while the origin request rate flattens."

### 3.2 Scenario 2: hot-family burst

**Goal:** show that a few popular product families absorb most of the traffic,
and that the cache keeps them fast when they spike.

**Command**

```bash
make load SCENARIO=soak   # 1800 s steady state
```

**Harness behaviour (verified in code)**

- On each VU re-roll (every `REFRESH_SECONDS=60`), `CATALOGUE.set_boost` picks 3
  random families and multiplies their weight by 5.
- The chosen families are **not logged to disk**. Identify them from the
  "Cache outcomes by family" and "Requests by family" panels, not from a file.
- Load is shaped between 20 and 150 VUs per cohort, with a max of 300 VUs.

**Watch**

| Dashboard | Panel | Expected signal |
|---|---|---|
| Cache Operations | Cache outcomes by family | Boosted families show the most traffic |
| Cache Operations | Fresh hit ratio | Stays high even when boosts change |
| Cache Operations | Origin offload % | High, with dips right after each re-roll |
| Retail Application | Requests by family | Spikes on the boosted families |

**Note:** "Cache outcomes by family" needs the `skipper-cache-prs` build.
Without it, that panel will be empty.

**Expected signal:** boosted families rise on each re-roll, and the hit ratio
recovers within a few seconds. Results **not measured**: the burst scenario was
not run in this pass.

**Audience talk-track:** "Every minute the spotlight moves to three random
product families at five times normal traffic. The cache keeps up because
popular keys stay fresh in memory."

### 3.3 Scenario 3: origin failure with stale-if-error

**Goal:** show Skipper continuing to serve cached content while the origin is
down.

**Command**

```bash
make chaos NAME=origin-stale   # 300 s
```

**Harness behaviour (verified in code)**

- At 60 s the mock origin's `default` case returns 503. At 150 s it is restored
  (90 s outage).
- The mock origin's default headers are `max-age=2, stale-while-revalidate=5,
  stale-if-error=30`.
- The restore step runs in a `finally`, so the origin is restored even if the
  run fails.
- Annotations are posted to Grafana with tags `chaos` and `origin-stale`
  (auth from `GRAFANA_AUTH`, default `admin:admin`).

**Stale window (important, read before the demo)**

- The stale-if-error window is about **2 s to 32 s after expiry** (max-age 2 s +
  stale-if-error 30 s). Stale responses are only served inside that window.
- After that, requests fail with **503**. A 90 s outage will therefore show
  stale serves at the start and 503s for most of the rest.
- **Unverified:** Skipper's exact handling of origin headers versus route TTL
  (`cache("30s","10s","15s")` in `routes.eskip`). Confirm the observed window
  against the dashboards before you quote the 2–32 s figure.

**Watch**

| Dashboard | Panel | Expected signal |
|---|---|---|
| Cache Operations | Origin offload % | Rises toward 100 % during the outage |
| Cache Operations | Hit/miss/stale rate | Stale rate spikes, then falls |
| Cache Operations | Origin responses by status | 503s from the origin during 60–150 s |
| Cache Operations | Refresh errors | Rises during the outage |
| Retail Application | Non-2xx by family | Rises after the stale window ends |

**Expected signal:** stale responses first, then 503s. Results (origin-stale run, 5 min): 31.5% of requests failed
(16,324 of 51,817), all `status` errors from the origin 503 window at 60–150 s.
Stale serves, first-503 time, and recovery time were **not measured** separately.

**Audience talk-track:** "The origin is down for 90 seconds. For the first few
seconds Skipper answers from its stale copy. After the stale window closes,
clients start to see errors, which is the honest limit of stale-if-error."

### 3.4 Scenario 4: Valkey or Skipper killed mid-run

**Goal:** show what breaks and what keeps working when a dependency or the
cache process fails.

**Commands**

```bash
make chaos NAME=valkey-kill    # scale Valkey to 0 at 90 s, back at 150 s
make chaos NAME=skipper-kill   # SIGTERM Skipper at 90 s, supervisor restarts after 2 s
```

**Harness behaviour (verified in code)**

- `valkey-kill`: Valkey is scaled to 0 at 90 s and restored at 150 s.
- `skipper-kill`: SIGTERM goes to the recorded Skipper child PID at 90 s. The
  supervisor restarts it 2 s later.

**Watch**

| Dashboard | Panel | Expected signal (Valkey) | Expected signal (Skipper) |
|---|---|---|---|
| Cache Operations | L2 get errors | Rises at 90 s | Quiet |
| Cache Operations | L2 set errors | Rises at 90 s | Quiet |
| Cache Operations | L1/L2 hit rate | L2 drops to 0; L1 keeps serving | Both drop briefly |
| Cache Operations | Coalesce errors | Quiet or small | Spike at kill |
| Cache Operations | Refresh queue depth | Rises if refresh blocks | Resets on restart |
| Retail Application | Latency p95 by route | Modest increase | Brief spike |
| Retail Application | Non-2xx by family | Quiet | Short burst during restart |

**Expected signal (Valkey):** L1 keeps hits in memory, L2 errors rise, and
latency changes little. The cache degrades gracefully.

**Expected signal (Skipper):** a short error burst and a dip in client RPS
while the supervisor restarts the process. Metrics reset, so
`skipper_cache_first_hit_seconds` will change. Results (skipper-kill
run): 287 failures in 56,406 requests (0.51%). Skipper was back about 2.5 s after
the kill. Valkey-kill run: 0 client errors. L2 error peak **not measured**.

**Audience talk-track:** "We removed the shared cache, then removed the proxy
itself. The shared cache failing only costs us L2 hits. Killing Skipper costs a
few seconds of errors until the supervisor brings it back."

## 4. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Grafana panels empty | Prometheus target down, or the wrong instance regex | Check `up{job="skipper-local"}` in Prometheus at :9090 |
| "Cache outcomes by family" empty | Skipper build lacks `skipper-cache-prs` | Rebuild Skipper from the PR branch and re-run `make start` |
| Port already in use | Another process holds 8080, 8081, 8089, 9091, 9999 or 9911 | `make stop`, then check with `lsof -i :<port>`. Update `make urls` output if ports changed |
| 503 from Skipper, not origin | Origin down past the stale window | Expected in scenario 3 after about 32 s. Compare with `origin responses by status` |
| Stale serves but no 503 | Outage shorter than the stale window | Expected for short outages. Check the chaos window |
| No chaos annotations in Grafana | `GRAFANA_AUTH` wrong or Grafana not on :3000 | Set `GRAFANA_AUTH=user:pass` and re-run |
| Run stopped mid-demo | Mac slept, or SafetyGuard latched at 5 % failures | Keep the Mac awake (`caffeinate -dims`). Check `run.json` |

**Unverified items to confirm before the talk:**

1. `caffeinate -dims` keeps macOS awake during a run.
2. The stale window (about 2 to 32 s) matches Skipper's behaviour.
3. `.runtime/skipper.sha` is written by `make start`.
4. Valkey starts empty on each stack start (affects scenario 1 cold state).
5. Grafana annotations from chaos appear on both dashboards.

## 5. Results

Runs on Grafana 13.2.4 with `streamingEnabled.search: false`. Reports are in
`results/runs/<run>/report.md` with panel screenshots.

Window hit ratio = hit / (hit + miss + stale), computed over each run's window
from Prometheus. The dashboard "Cache hit ratio" stat uses the last minute, so
it differs (smoke: 83.9% stat vs 77.9% window).

| Run | Requests | Failures | p95 (ms) | RPS | Hit ratio (window) | Origin requests |
|---|---|---|---|---|---|---|
| smoke (2 m) | 6,538 | 0 | 22 | 54.3 | 0.779 | 1,626 |
| origin-stale (5 m) | 51,817 | 16,324 (31.5%) | 15 | 172.7 | 0.872 | 7,296 |
| valkey-kill (5 m) | 49,528 | 0 | 13 | 164.6 | 0.859 | 7,765 |
| skipper-kill (5 m) | 56,406 | 287 (0.51%) | 14 | 187.7 | 0.881 | 7,436 |
| soak (45.5 m) | 298,335 | 3 (0.00%) | 16 | 109.2 | 0.875 | 40,884 |

| Scenario | Key metric | Result |
|---|---|---|
| 1. Cold vs warm | Hit ratio at 30 s / 120 s | 0.653 / 0.772 (cumulative, smoke) |
| 1. Cold vs warm | Time to First Hit | 17.05 s (process init to first hit) |
| 2. Hot-family burst | Offload % during boost | not measured (not run) |
| 3. Origin stale | Stale serves / first 503 | not measured separately; 31.5% failures at 60–150 s |
| 3. Origin stale | Recovery time after 150 s | not measured |
| 4a. Valkey killed | Peak L2 error count | not measured; 0 client errors |
| 4b. Skipper killed | Error count in 90–150 s | 287 total failures; restart about 2.5 s |
