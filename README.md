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
  `jq` is useful for inspecting saved JSON. No Python packages are needed.
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

`dashboard.json` is a provisioned, exportable twelve-panel dashboard. Regenerate
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
and the Grafana datasource proxy. The browser waits for all twelve panel queries,
captures a screenshot, and checks datasource/title loading.

### Overall versus tier results

- **Overall Cache Result by Status Code Class**: `hit|miss` only, grouped by
  `result,code_class`. No stale/error/tier outcomes are included.
- **L1 Hits by Status Code Class**: `l1_hit` only, grouped by `code_class`.
- **L2 Hits by Status Code Class**: `l2_hit` only, grouped by `code_class`.
- **Cache Hit vs Miss Rate (lines)** and **(stacked)** use identical overall
  `hit|miss` expressions grouped by result. The stacked view uses normal stacking,
  not 100%, so it retains traffic volume. Tier hits are never extra outcomes.

All five charts use the same five-minute rate window, req/s units and scope.
The exporter supports code-class labels on both tier counters. Live checks
verified `hit/2xx`, `l1_hit/2xx`, and `l2_hit/2xx`; other hit code classes are
unexercised. Exact PR4364 still lacks a live overall `miss` sample because its
coalescing fetch fails before that instrumentation. The charts **do not**
substitute `l2_miss` or `coalesce_error`, and never fabricate a zero miss series.

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

Storage error expression:
`sum by (backend, op) (skipper_cache_storage_error_total)`. **Absent is not a
verified zero**: the dashboard and results explicitly mark this case not
exercised. Controlled fault-injection Go tests cover error paths without
disrupting the running Valkey instance.

**The PRs are independent.** #4365 fixes coalescing but lacks #4364's metric
additions. #4364 alone still produces ten origin calls and `coalesce_error`
on the cold burst. This is an expected negative control, not proof of a
metrics regression. No combined revision is used in the recorded results.

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
