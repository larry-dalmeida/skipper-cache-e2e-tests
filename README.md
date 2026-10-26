# Skipper cache wire-level verification

Reproducible local checks for the Skipper cache PR stack, including
[PR #4370](https://github.com/zalando/skipper/pull/4370) (cold-miss coalescing),
using real HTTP requests, a controlled local Python origin, Valkey, Prometheus,
Grafana, Loki and Tempo. No production access or nginx equivalence claim.

**[Human-readable verification results](results/README.md)** include historical
source SHAs, run times, controls, metric samples and dashboard screenshots.

## One-command setup and verification

From a clone of this repository, with a clean Skipper checkout available:

```bash
make verify SKIPPER_CHECKOUT=/path/to/clean/skipper
```

This checks prerequisites, starts the `minikube` profile when needed (without
switching your shared context), creates `.runtime/venv` with pinned Locust,
applies the stack, waits for supervised forwards/UI/local origin/Skipper, builds
Skipper, runs the **120-second smoke** workload, then runs coalescing and every
dashboard metric query. It prints `results/runs/<timestamp>-smoke/report.md` and
saves `coalescing.json`, `metrics.json` and raw metric samples in that run folder.
The exact running revision is read from `.runtime/skipper.sha`. Any failed check
exits nonzero; a failed metric panel is not silently treated as success.

Run the same command again: healthy supervisors are reused, resources are
reconciled, and Skipper alone is restarted when the requested checkout SHA
changes. Unique coalescing cache keys prevent interference from previous runs.
The stack stays running after success **or failure** for inspection. A busy port
owned outside this repository is not killed; inspect `.runtime/*-run.out`.
Do not run overlapping load/chaos/verification commands on the same stack.

For setup plus ongoing interactive load instead:

```bash
make all SKIPPER_CHECKOUT=/path/to/clean/skipper  # endless Locust UI :8089
# Or setup only, without load/verification:
make setup SKIPPER_CHECKOUT=/path/to/clean/skipper
```

`make all SCENARIO=smoke SKIPPER_CHECKOUT=...` remains supported; unlike
`make verify`, it runs only the chosen workload, not the two verifiers.

## Requirements and scope

- Python **3.9+**, Make, Bash, Git, curl, kubectl, minikube and Docker with its
  daemon running and a working minikube driver. Images must be downloadable or cached.
- A clean, separate [Skipper](https://github.com/zalando/skipper) checkout and Go
  meeting its `go.mod` directive. Go's automatic toolchain selection runs in
  that checkout (`GOTOOLCHAIN=auto` permits downloading the required version).
- Free fixed local ports: **8080** (go-httpbin compatibility backend), **8081**
  (Python origin), **6379** (Valkey), **4318** (OTLP), **9091** (origin control),
  **9999** (Skipper), **9911** (metrics), and **8089** for endless Locust only.
- Grafana, Prometheus, Loki and Tempo use **dynamic** loopback ports assigned by
  `minikube service`; see `.runtime/urls.txt` or `make urls`.
  Ports **3000/9090 are not used** by this setup.
- A disposable **minikube** context and namespace **cache-e2e**. Scripts own the
  named resources in `stack.yaml`; do not share that namespace with other work.

All kubectl calls pin `--context minikube`; setup never flushes Valkey or kills
unrelated processes. Services are ClusterIP and forwarding/proxy traffic binds
loopback. Grafana anonymous access is **Viewer**, not Admin. Skipper's default
metrics listener binds `:9911` for minikube scraping: restrict host network access
with your firewall and never run this setup on an exposed host.

## Step-by-step flow

Use this only when you want to run individual phases:

```bash
minikube start --profile minikube --keep-context
make venv
make stack
make start SKIPPER_CHECKOUT=/path/to/clean/skipper
make load SCENARIO=smoke
make coalescing
.runtime/venv/bin/python verify-metrics.py --sha "$(cat .runtime/skipper.sha)"
make urls
```

`make start` backgrounds identity-tracked supervisors and waits for readiness;
no extra terminals are required. `make load` is foreground and Ctrl-C stops
only that load. `make stop` stops this repository's local supervisors and deletes
deployments/services/configmaps, retaining storage PVCs and the namespace.
Minikube itself is left running. Use `./teardown.sh skipper` to stop only Skipper.

### Coalescing verifier (PR #4370)

`make coalescing` uses the Python origin started by setup, not Kubernetes
`deployment/mock-backend` access logs. The origin's `/config` control API sets
one-second delay or `Vary: *` for UUID-scoped cases; `/events?case=<uuid-case>`
counts exactly those origin requests, without resets or log-tail races.

Assertions: HTTP 200 and matching body path/query; a synchronized **10-request
cold burst => exactly 1 origin call**; its warm follow-up => **0 extra calls**;
and **3 sequential Vary-star requests => 3 origin calls**. All response bodies
in a cached burst must match. Exceptions/assertions produce FAIL and nonzero exit.

For a custom report path or proxy/control URL:

```bash
./verify-coalescing.sh --label my-revision --sha "$(cat .runtime/skipper.sha)" \
  --base http://127.0.0.1:9999 --control http://127.0.0.1:9091 \
  --output .runtime/coalescing.json
```

The force-cache route uses `cache("30s", "10s", "15s")`. This is one Skipper,
not comprehensive policy, expiry-boundary or multi-replica coverage.

## Chaos in one command

```bash
make chaos SKIPPER_CHECKOUT=/path/to/clean/skipper NAME=origin-stale
make chaos SKIPPER_CHECKOUT=/path/to/clean/skipper NAME=valkey-kill DURATION=90
make chaos SKIPPER_CHECKOUT=/path/to/clean/skipper NAME=skipper-kill
```

With `SKIPPER_CHECKOUT` supplied these first perform the same idempotent setup.
On an already-running stack, `make chaos NAME=origin-stale` is also supported.
**Chaos already runs its own timed Locust workload; do not run parallel `make load`.**
The default duration is 300 seconds. Origin returns 503 at 60s and restores 200
at 150s; Valkey scales to zero at 90s and restores at 150s; Skipper's recorded
child is killed at 90s and its supervisor restarts it. Offsets scale with
`DURATION`. Fault restoration runs on exit and `faults.json` records outcomes.
Grafana annotations use `admin:admin` (override `GRAFANA_AUTH=user:pass`).
Expected chaos HTTP errors are recorded without failing the smoke safety guard;
identity mismatches still abort. Chaos does not run the coalescing/metric verifiers.

## Endpoints, dashboards and results

```bash
make urls   # real dynamic Grafana/Prometheus/Loki/Tempo URLs and dashboard links
make runs   # one-line summaries of results/runs/*
make report # regenerate latest report (or RUN=results/runs/<dir>)
```

Skipper: `http://127.0.0.1:9999/get`; metrics: `http://127.0.0.1:9911/metrics`;
origin: `http://127.0.0.1:8081`; origin control: `http://127.0.0.1:9091`.
The dashboards are **Cache Operations** (`cache-performance-local`) and
**Retail Application** (`retail-application-local`). Their JSON lives in
`dashboards/`; `make stack` reapplies the ConfigMap using server-side apply
(the dashboard payload exceeds client-side annotation limits).

The metric verifier expands dashboard variables from their All/default selections
and resolves Grafana time macros before querying both Prometheus and Grafana.
Standalone runs retry required queries for up to 45s each; `make verify` uses
`--wait-seconds 0 --scenario smoke` after the two-minute smoke warm-up.
Histogram p95 panels pass when at least one series is finite (inactive buckets
can yield NaN); missing/all-NaN panels and failed/down scrapes still fail.
Other query types require every returned series to be finite.

Only **Refresh errors / dropped jobs** and **Cache evictions** may be absent
(`NOT EXERCISED`) during smoke. The explicit scenario allowlists are in
`verify-metrics.py`; `--scenario origin-stale` requires refresh-error samples,
while `--scenario strict` requires every panel. A present nonfinite sample is
never excused by the missing-event allowlist. The former blanket optional-panel
metadata exemption is not used. Query errors always fail.

Error-rate dashboard panels show zero only when an actual live traffic series
exists for that origin/family. They never substitute `vector(0)` for missing
traffic or unavailable targets. Positive error rates remain visible.

The route-labelled cache build exports `skipper_cache_route_result_total` and
`skipper_cache_route_reval_total`; origin duration uses the `route` label on
`skipper_cache_origin_duration_seconds`. The route-family panels derive their
display grouping with `label_replace` from `<family>__<variant>` route IDs.
Revalidation retains the `outcome` dimension, including `failed` for origin
HTTP 5xx, transport errors and store failures. After origin-stale chaos, inspect
`skipper_cache_route_reval_total{outcome="failed"}` in Prometheus or raw Skipper
metrics; it must increase during the fault, not just show `started`/`success`.

## Workloads and reports

`make load SCENARIO=smoke` runs for 2 minutes; `SCENARIO=soak` for 30 minutes;
`SCENARIO=endless` runs until stopped with the web UI at `127.0.0.1:8089`.
Use `make chaos`, not `make load`, to inject the named faults.

Requests select weighted family/locale entries from `contentful-cache-paths.json`
through `catalogue.py`; shop endpoints contribute 25% of traffic. `CohortShape`
re-rolls cohort VU targets every 60s, bounded by the scenario's ceiling. Stats
are named `cohort/family`. The safety guard aborts on identity mismatch or more
than 5% failures after at least 50 requests; verification additionally requires
zero smoke failures. Reports contain CSV summaries, failures, exceptions and
Grafana links for the run window. Screenshots are optional (Playwright required):

```bash
.runtime/venv/bin/python screenshots.py results/runs/<dir>
# Optional --only <slug>... or --height <px>
```

`cachectl.py` remains an individual-script wrapper; use `make verify` for the
complete fresh-clone flow. Its legacy backend scaling commands affect the
compatibility go-httpbin backend, not the local Python origin used by Skipper.

## Harness tests and process safety

```bash
make test
bash -n ./*.sh
```

Supervisors record exact PID/start-time/command identities under `.runtime/`
and refuse reused/mismatched PIDs. They terminate only their own children.
State is ignored by Git. Valkey connection/dial timeouts are explicitly 2s
because local forwarding can exceed Skipper's 25ms defaults; startup failures
are not evidence that shared Valkey should be flushed.
