# Skipper cache wire-level verification

Reproducible local checks for [Skipper PR #4365](https://github.com/zalando/skipper/pull/4365)
(cold-miss coalescing) and [PR #4364](https://github.com/zalando/skipper/pull/4364)
(cache metrics). Uses real HTTP requests, go-httpbin access logs, Valkey, Prometheus
and Grafana. No production access or nginx equivalence claim.

**[Human-readable verification results](results/README.md)** include exact source
SHAs, UTC run times, positive/negative controls, race-test commands, measured
histogram samples and a dashboard screenshot.

## Requirements and scope

- A clean, separate checkout of the desired [Skipper](https://github.com/zalando/skipper)
  revision; Go version matching its `go.mod`.
- Python 3.9+, Bash, Git, curl, kubectl and minikube with a working driver.
  `jq` is useful for inspecting saved JSON. The core harness needs no Python packages; only the optional Locust load needs `requirements-locust.txt`.
- Optional browser verification: Node.js 22+ (built-in WebSocket) and Chrome/Chromium.
- Free host ports: `3000`, `8080`, `6379`, `9090`, `9999`, `9911`.
- A disposable **minikube** context and namespace **cache-e2e**. Scripts own the
  named resources in `stack.yaml`; do not share that namespace with other work.
  Images need to be downloadable or already cached.

All kubectl calls pin `--context minikube`; the scripts do not switch your shared
kubeconfig, reset Valkey, use broad process kills, or stop unrelated services.
Services are ClusterIP; port-forwards and proxy traffic bind to host loopback.
Grafana anonymous access is **Viewer**, not Admin. Skipper's default metrics
listener binds `:9911` so minikube can scrape it: restrict host network access
with your local firewall, and never run this setup on an exposed host.

## Run

Start a local minikube profile without changing the current kubeconfig context:

```bash
minikube start --profile minikube --keep-context
```

Use separate terminals from this repository root. Supervisors stay in the
foreground: keep their terminals open; Ctrl-C stops only their own children.
Readiness is polled; startup is not assumed after a fixed sleep.

```bash
# Terminal 1: apply resources, wait for readiness, supervise four port-forwards.
./start-stack.sh

# Terminal 2: point to your clean checkout; builds and reports its exact HEAD.
export SKIPPER_CHECKOUT=/path/to/clean/skipper-checkout
./run-skipper.sh

# Terminal 3: assert actual wire behavior and save a fresh, ignored report.
./verify-coalescing.sh --label my-revision \
  --sha "$(git -C "$SKIPPER_CHECKOUT" rev-parse HEAD)" \
  --output .runtime/result.json

# Optional: ongoing small cold+warm requests for live charts.
python3 lifecycle.py traffic
```

Verification requires **HTTP 200 and matching response path/query/body**, one
origin request for ten barrier-synchronized requests to a unique one-second
delayed cold key, no additional origin call for its warm request, and three
origin requests for three requests to a `Vary: *` response. UUIDs scope origin
logs to each individual case. Every request exception is collected, and any
failed assertion makes the command exit nonzero. No broad log-tail differences
or discarded background curl statuses are used.

The route is force-cache mode: `cache("30s", "10s", "15s")`. These checks do not
cover every HTTP cache policy, Vary variant, stale behavior or cache deployment.

## Local endpoints and observability

- [Grafana dashboard](http://localhost:3000/d/ag1x69aba3qpse/skipper-cache-verification)
- [Prometheus](http://localhost:9090)
- [Skipper](http://localhost:9999/get)
- [Raw Skipper metrics](http://localhost:9911/metrics)
- Mock origin: `http://localhost:8080/get`; Valkey: `localhost:6379`.

`dashboard.json` is a provisioned, exportable dashboard with nineteen data panels
and one failure/fallback row. Regenerate
it with `python3 dashboard.py`. Every panel and query uses the `datasource`
variable rather than a hardcoded datasource UID. A multi-select `instance`
variable scopes every query together with `job="skipper-local"`.
Provisioning creates its own
Prometheus datasource; Grafana reaches the Kubernetes Prometheus service.
go-httpbin **does not export `/metrics`** and is deliberately not scraped.

On a metrics-enabled revision, after sending traffic:

```bash
python3 verify-metrics.py \
  --sha "$(git -C "$SKIPPER_CHECKOUT" rev-parse HEAD)"
export CHROME=/path/to/chrome-or-chromium
node verify-browser.mjs
# Inspect .runtime/dashboard.png; no browser profile or logs are committed.
```

The metric verifier executes every dashboard expression against both Prometheus
and the Grafana datasource proxy. The browser waits for all nineteen panel queries,
captures a screenshot, and checks datasource/title loading.

### Overall versus tier results

- **Overall Cache Result by Status Code Class**: `hit|miss` only, grouped by
  `result,code_class`. No stale/error/tier outcomes are included.
- **L1 Hits by Status Code Class**: `l1_hit` only, grouped by `code_class`.
- **L2 Hits by Status Code Class**: `l2_hit` only, grouped by `code_class`.
- **Cache Hit vs Miss Rate** and **Cache Hit vs Miss Rate (stacked)** use identical flat
  `cache.hit|cache.miss` expressions grouped by key. The stacked view uses normal stacking,
  not 100%, so it retains traffic volume. Tier hits are never extra outcomes.

All five charts use the same `$__rate_interval` window, req/s units and scope.
The exporter supports code-class labels on both tier counters. Live checks
verified `hit/2xx`, `l1_hit/2xx`, and `l2_hit/2xx`; other hit code classes are
unexercised. Exact PR4364 still lacks a live overall `miss` sample because its
coalescing fetch fails before that instrumentation. The status-class chart is
explicitly incomplete on that head. The flat `cache.miss` counter *does* cover
those fallback requests, so the line/stack composition views use the complete
flat schema. They never substitute `l2_miss` or `coalesce_error`.

### Fresh hit percentage and failure signals

Fresh hit percentage uses flat request-outcome counters because the labeled
schema omits real fallback misses on exact PR4364:

```promql
100 * sum(rate(skipper_custom_total{key="cache.hit", ...}[$__rate_interval]))
    / sum(rate(skipper_custom_total{key=~"cache\\.(hit|miss|stale)", ...}[$__rate_interval]))
```

The full production expression is in `dashboard.py` / `dashboard.json`: its
numerator zero-fills absent hits only against an existing request denominator,
filters denominator `> 0`, and requires all selected Skipper scrapes to be up.
No traffic, absent outcomes or failed/absent scrape yields **No traffic / no data**,
not 0% or 100%. A real miss-only denominator with a lazy absent hit counter yields
0%. No errors or tier counters enter this denominator. Stale responses have a
separate req/s panel and are not fresh hits. No offload-ratio substitute is used.
`python3 verify-ratio.py` checks eight semantic cases with isolated synthetic
vectors evaluated by the real Prometheus engine, without modifying live counters.

The failure/fallback row groups compatible event rates:

- L2 get errors / set fallbacks.
- Storage faults by `backend,op`.
- Coalesce errors / bypass errors, explicitly **per waiter**, not origin failure
  or client-5xx rates.
- Revalidation errors / dropped jobs.
- LRU evictions / oversized entries.
- Current revalidation queue depth, a **gauge**, not a rate.

All rate panels use `$__rate_interval`; Grafana calculates this from its query
step and scrape cadence. The CLI metric check resolves it explicitly to `1m`
for reproducibility. Lazy absent counters display **Not exercised / no samples**,
not invented zeros. The scrape-health panel distinguishes unreachable targets.
A queue depth sample of 0 is a real observed empty queue. Controlled failure Go
tests verify selected paths without clearing/disrupting shared Valkey; most live
failure counters remain unexercised. See the results for that distinction.

For a controlled real L1-to-L2 check (no shared-store flush):

```bash
python3 verify-tiers.py \
  --sha "$(git -C "$SKIPPER_CHECKOUT" rev-parse HEAD)"
```

The launcher sets `-cache-l1-ttl=2s`, below the route's 30-second fresh lifetime.
The tier check requests a UUID-scoped key, observes an immediate L1-hit increment,
then polls until L1 expires and an L2-hit increment appears, while requiring
identical HTTP 200 bodies and exactly one origin request in matching access logs.
The demo traffic sends an additional post-expiry request to keep tier plots active.
Use `python3 -m unittest -v test_dashboard test_harness` for panel contract tests.

`skipper_custom_duration_seconds_{bucket,sum,count}` has the exact `key` values
`cache.coalesce_wait_duration`, `cache.l2_get_duration`, `cache.l2_set_duration`.
Exported units are **seconds**. Histogram p95s are bucket estimates; the first
5 ms bucket is too coarse to infer precise microsecond waits. Coalesce wait
includes the leader's work, not just a follower queue wait.

Storage error expression uses `sum by (backend, op) (rate(
skipper_cache_storage_error_total{...}[$__rate_interval]))`. **Absent is not a
verified zero**: the dashboard and results explicitly mark this case not
exercised. Controlled fault-injection Go tests cover error paths without
disrupting the running Valkey instance.

**The PRs are independent.** #4365 fixes coalescing but lacks #4364's metric
additions. #4364 alone still produces ten origin calls and `coalesce_error`
on the cold burst. This is an expected negative control, not proof of a
metrics regression. No combined revision is used in the recorded results.

## Interactive Locust load (optional)

Requires a running Skipper (`lifecycle.py skipper`). Locust is pinned in
`requirements-locust.txt`; install it in a virtualenv:

```bash
python3 -m venv .venv-locust && .venv-locust/bin/pip install -r requirements-locust.txt
.venv-locust/bin/python lifecycle.py locust   # UI at http://127.0.0.1:8089
```

Use the UI to Start/Stop the swarm; `--max-vus` (default 5, max 50) caps concurrent
users. Users alternate between two cohorts (`en-US`, `de-DE` Accept-Language, so two
cache identities) and pick keys 70% primary, 20% related, 10% cooler from 53 keys.
The run latches stopped on the first identity mismatch (response path, query or echoed
headers differ) or when more than 5% of at least 50 requests fail. Restart the driver
to clear the latch. Stop with `./teardown.sh locust`.

Not covered: Locust metrics export to Prometheus, expiry-boundary preflight, or
multi-replica checks; this stack has one Skipper and the route TTLs are 30s/10s/15s.
Header echo matching assumes the mock backend echoes `Host`, `Authorization`, and
`Accept-Language` as arrays; that was verified only against a stand-in origin.

## Single entry point: `cachectl.py`

Every action below is also runnable on its own; `cachectl.py` only composes the existing scripts. kubectl is pinned to context `minikube` and namespace `cache-e2e`.

```sh
python3 cachectl.py stack up                  # start-stack.sh
python3 cachectl.py stack down                # teardown.sh (stops local processes, keeps namespace)
python3 cachectl.py skipper start -- ~/src/skipper
python3 cachectl.py skipper restart -- ~/src/skipper
python3 cachectl.py skipper stop
python3 cachectl.py fault origin down         # scale mock-backend to 0
python3 cachectl.py fault origin up           # back to 1 and wait for rollout
python3 cachectl.py fault valkey down         # scale valkey to 0
python3 cachectl.py fault valkey up
python3 cachectl.py load                      # Locust web UI on 127.0.0.1:8089
python3 cachectl.py check unit                # offline unit tests
python3 cachectl.py check verify -- --label X --sha Y   # also: tiers, ratio, metrics, coalescing, browser
```

Run `python3 test_cachectl.py` to check the argument mapping without touching the cluster.

## Stop, switch revision, and validate the harness

```bash
./teardown.sh skipper  # stop only the tracked Skipper before changing checkout
./run-skipper.sh /path/to/another/clean/checkout
./teardown.sh traffic # optional: stop just the synthetic traffic
./teardown.sh         # stop this repo's tracked traffic, Skipper and forwards
python3 -m unittest -v test_harness
bash -n ./*.sh
```

The supervisors record exact PID/start-time/command identities under `.runtime/`
and refuse to signal a reused/mismatched PID. They use graceful termination and
a bounded wait before killing **only their own child**. State is ignored by Git.
Namespace resources, minikube and Valkey data are left untouched by teardown.
Delete resources manually only when you deliberately intend to discard this
entire disposable namespace.

Local Valkey port-forward latency can exceed Skipper's 25 ms connection defaults.
The launcher uses explicit 2-second dial/connection timeouts. A startup timeout
is not evidence that shared Valkey should be flushed.

The original local experiment scripts were the starting point for these safer
supervisors and assertions; Skipper itself and the container images remain
upstream dependencies, not code copied into this repository.
