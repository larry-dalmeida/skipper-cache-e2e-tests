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

## Focused L1 freshness regression

`make l1-freshness SKIPPER_CHECKOUT=/path/to/clean/skipper` builds the exact
upstream revision pinned in `verify_l1_freshness.py` and starts **two native
Skipper processes**, the existing controlled Python origin, and a disposable
Valkey Docker container on dynamic loopback ports. It does not use or change
minikube, the main stack, production, or an existing Valkey. Requires Python,
Go, Git and a running Docker daemon; no Locust dependencies are needed.

A warms version A in L1; write-around instance B verifies the shared value.
After A's 2-second warm TTL plus 1-second SWR (well inside 30-second SIE
retention), B explicitly refreshes L2 to version B. B confirms a fresh shared
HIT with no origin request. A must then serve **200 / B / HIT / zero extra
origin calls**, both for `only-if-cached` and a normal request. The bounded
wait is derived from a confirmed warm hit; A receives no intervening SWR
requests. A dedicated uncached readiness route does not prime the fixture.

The verifier exits **1 for a regression**, **2 for setup/infrastructure errors**,
and 0 only for the intended correct behaviour. It is not an expected-failure
test or skipped reproduction. Logs, commands, binary build information,
image digest, raw L2 entry, counters, bodies and status evidence are retained
in `.runtime/l1-freshness/`; processes and container are cleaned up on exit.
`--sha` permits an explicitly pinned alternative clean checkout; `--output`
keeps repeated runs separate. The `only-if-cached` probe isolates the L1 lookup
from upstream cold-coalescing behaviour; the normal probe also records the
coalescing-error metric if that separate issue is encountered.

### Recorded comparison (29 October 2026)

The regression **reproduces on both revisions**; later dispatch fixes in
[zalando/skipper#4387](https://github.com/zalando/skipper/pull/4387) do **not**
address this L1 lookup defect. Setup/control assertions succeeded, not skipped.

| Build | Exact source revision | Result |
|---|---|---|
| Latest upstream HEAD at investigation, `v0.28.56` | `2237f1e885a2efab21a524f8d96e8c87d58bbf69` | FAIL, exit 1 |
| Open PR 4387 head, `v0.28.51-0.20261026193401-d767f6362226` | `d767f6362226c6260e4288120e21136a3102cb26` | FAIL, exit 1 |

| Probe after B's confirmed fresh shared HIT | Expected | Actual on both builds |
|---|---|---|
| A, `only-if-cached` | 200 / B / HIT / 0 origin calls | 504 / empty body / MISS / 0 origin calls |
| A, normal GET | 200 / B / HIT / 0 origin calls | 200 / B / MISS / **1 extra origin call** |

Each run made **3 total origin requests instead of 2** (prime A, refresh B,
unnecessary A fetch). A's first failing probe records `cache.l1_hit +1` and
no L2 hit, despite a raw fresh B entry in Valkey. No STALE response is
expected or observed: the old L1 entry is deliberately **past SWR**, retained
only by SIE. Baseline also increments `cache.coalesce_error` on the prime
and final normal request, then falls through to the ordinary backend. PR
4387 removes that observed error, but still fetches origin unnecessarily.
The first probe never enters coalesce, independently proving the lookup defect.

Both use unmodified native macOS/arm64 Skipper builds, Go's selected
`go1.27.3` toolchain, and isolated Linux/arm64 Valkey 8.1.1 containers pinned to
`sha256:6a57d58c0a37cf7acc3045ac0fd6dd91be339774106ab4d9ca088013a096a99f`.
There is **no Skipper container image** in these runs. Full binary hashes,
build information, runner/Docker versions, commands, fixture events, payloads,
raw L2 entries and metric deltas are in
[`upstream-head.json`](results/l1-freshness/upstream-head.json) and
[`pr4387-head.json`](results/l1-freshness/pr4387-head.json).

Exact comparison commands, using the two clean isolated source checkouts:

```bash
python3 verify_l1_freshness.py --checkout .runtime/skipper-l1-freshness --output .runtime/l1-freshness-baseline
python3 verify_l1_freshness.py --checkout .runtime/skipper-l1-freshness-pr4387 --sha d767f6362226c6260e4288120e21136a3102cb26 --output .runtime/l1-freshness-pr4387
```

PR 4387 was open/unmerged, based on `larry-almeida-zse-cache-timeouts-pr`
at `7e48ec0fb570d5e26b9ad551ec7a9d25dfef209d`, not main. Its head is not
contained in the measured upstream HEAD: common ancestor
`1f6cc38b924956fb790da60fa67084010d8506e7`, with 7 upstream-only and 26
PR-only commits. This compares distinct builds, not identical merged code.

The smallest justified upstream fix is to replace `!entry.IsStale(now)` in
the L1-first lookup with an **explicit within-TTL freshness check**. `IsStale`
is SWR-window membership, not the complement of freshness. Keep SIE retention
and SWR semantics intact; do not globally redefine `IsStale` or evict SIE
fallback entries to mask the issue. The source default L1 warm TTL is 60s;
this reproduction deliberately shortens it to 2s. These local runs establish
the defect, not its occurrence in production.

### Correctness candidate validation (29 October 2026)

The **unchanged** focused L1 regression passes against committed correctness
candidate `2702aa036eda2dc3b7eeabc17b95a1086ca6b985`, based on
`9e7271a6bf11679ca2709843792f0c3185e1cd9c`. Both A probes now return
**200 / B / HIT / zero extra origin calls**, with **2 total origin requests**.
The first probe records an L2 hit; the following normal GET records an L1 hit
on the newly warmed B. This fixes the specific previously reproduced defect.

```bash
python3 verify_l1_freshness.py --checkout .runtime/skipper-l1-freshness-candidate2702 --sha 2702aa036eda2dc3b7eeabc17b95a1086ca6b985 --output .runtime/l1-freshness-candidate2702
```

The native binary reports
`v0.28.51-0.20261029081926-2702aa036eda`, runtime `go1.27.3`, SHA-256
`8e9e717af5b8bac2475b620f486cc0593a8a16ec17c3f8a24f2fcb063ad886bf`.
The source checkout remained clean. Runner/image provenance and exact outcomes
are recorded in [`candidate2702-head.json`](results/l1-freshness/candidate2702-head.json).

The existing, unmodified `verify.py` behavioural suite also passes using this
same hash-verified binary, a new loopback-only Valkey container, and a fresh
controlled origin. An isolated launcher at
`.runtime/verify_candidate_behaviour.py` supplied dynamic ports and cleaned up
its resources:

```bash
python3 .runtime/verify_candidate_behaviour.py --l1-result .runtime/l1-freshness-candidate2702/result.json --output .runtime/behaviour-candidate2702
```

Cold plus warm requires exactly **1 origin call**; a synchronized **10-request
cold burst plus warm** also requires exactly **1**; three sequential
`Vary: *` requests require exactly **3**. All cases passed HTTP 200, exact
path/query and applicable body equality checks. Exact `verify.py` invocation,
route and binary provenance are preserved in
[`candidate2702-coalescing-provenance.json`](results/l1-freshness/candidate2702-coalescing-provenance.json);
case results are in
[`candidate2702-coalescing.json`](results/l1-freshness/candidate2702-coalescing.json).
This bounded validation does not cover every SIE/Vary combination, distributed
coalescing, long-running load, or the future observability dashboard contract.
No candidate source, production resources, or existing test expectations changed.

### Observability and client-only dashboard acceptance

Final observability head **`99f94588825535c5cd2dfea7da95b88550a94d2b` passes**
bounded local Prometheus acceptance, including real direct/pipeline success,
503 and disconnect samples. The native Skipper binary reports
`v0.28.51-0.20261029084459-99f945888255`, Go `1.27.3`; its SHA-256 is
`017ca6d9e1efb87d7b3c294fbaa16828bde1a32a4bb4c693de59179035f3b137`.
Native Prometheus 2.55.1 directly scraped loopback listeners every second.
Its official release archive was checked against the release SHA256SUMS;
binary SHA-256 is
`fe3091bf20675955bddd2f0ccb86b674e331ba492ea749b7dc7c281b797d1fc3`.
Valkey remained a fresh, digest-pinned disposable container.

The companion changes in `dashboards/cache-performance.json` are limited to
panels **44, 7, 48, 50 and 55**. Client RPS remains universal, using
`skipper_custom_total{key="client.requests"}`, not cache outcome counts.
Non-cache, uncacheable, failed and no-route requests count once; authenticated
cache listener replays and requests explicitly marked by `healthcheck()` do
not. An arbitrary health-like URL is not implicitly excluded. The deployed
dashboard and Skipper build must both be updated to use this new contract;
this work does not publish an additional e2e-repository PR or change Grafana.

Observed accounting: **41 external client requests**, versus **55 listener
requests** (41 external + 13 internal pipeline requests + 1 explicit
healthcheck). Five separate client-counter probes covered non-cache 200,
uncacheable 200, backend 503, no-route 404 and a forged
`X-Cache-Revalidate: 1` header; each incremented the client counter exactly
once. Before traffic, the initialized client counter yielded a real zero RPS,
but zero-denominator offload/ratio queries and the unobserved first-hit gauge
returned **no samples**, not synthetic healthy zeros.

Twelve real background refreshes produced **12 started = 4 success + 8
failed**. Started and terminal outcomes remain separate plotted series, not
24 attempts. All six exercised family/outcome rates were positive, and all
five direct/pipeline status-class latency groups had finite quantiles.
Direct disconnects were `error`; pipeline disconnects became `5xx` at the
listener. Origin histograms measure dispatch through response headers, not
body transfer, and pipeline observations include listener/filter overhead.
The 28 real origin calls included four disconnects. Each of the four configured
Cache-Control directives counted 20 cache-dispatched HTTP responses, and
`no-store` counted once; overlapping directive counters are not summed as a
response denominator. Non-cache origin calls do not emit cache-dispatch metrics.

All **44 dashboard target queries executed** against actual emitted series:
43 passed and the unexercised eviction panel was explicitly **NOT EXERCISED**.
One singleton uncacheable histogram had no observed increment between scrapes,
so its unused-family quantile remained NaN and is separately recorded, not
treated as zero. Shared process L1 snapshots still use `max by (instance)`,
not a sum over replicated route labels. Origin latency uses seconds, gauges
use bytes/entries, rates use requests or operations per second, and offload
and fetch/client panels use 0..1 ratio units. The final offload and fetch/client
values were 0.3282347 and 0.6717653 at the same evaluation timestamp.
Their numerator is **all-caller origin traffic**, including refreshes; they
are not cache hit ratio or route-attributed offload. Different origin and
selected Skipper populations can also change the interpretation.

Exact acceptance command:

```bash
python3 .runtime/verify_observability_acceptance.py --checkout .runtime/skipper-observability99f9 --sha 99f94588825535c5cd2dfea7da95b88550a94d2b --output .runtime/observability99f9-final
```

The exact scratch launcher is preserved as
[`acceptance-launcher.py.txt`](results/observability/acceptance-launcher.py.txt)
(restore it under `.runtime/` to rerun). The report with exact queries,
responses, commands, hashes, counter probes and gaps is
[`99f9-final-acceptance.json`](results/observability/99f9-final-acceptance.json).
Raw before/after Skipper and origin expositions are alongside it. Earlier
`26bb35f7` observations are **superseded**, not final dashboard acceptance:
the first run exposed the listener-denominator defect and lazy-counter
sampling limitations; later attempts encountered real local Valkey/query/
Prometheus-readiness infrastructure errors. Those error reports are retained.

This acceptance does **not** prove Grafana rendering or datasource-proxy
behaviour, eviction/queue-drop load, or the real retail locale taxonomy.
It used no production resources, did not edit the candidate source, and
cleaned up its owned processes and containers.

## Requirements and scope

The focused follow-up does not require the shared minikube stack:

```bash
python3 verify_cache_observability.py --checkout /path/to/clean/skipper --sha EXACT_SHA --mode integrated --output .runtime/accuracy
python3 verify_cache_dashboards.py --checkout /path/to/clean/skipper --sha EXACT_SHA --output .runtime/dashboards
python3 verify_cache_remaining.py --checkout /path/to/clean/skipper --sha EXACT_SHA --output .runtime/remaining --grafana
```

These launchers use owned processes, dynamic loopback ports and disposable
containers, with two-second Valkey dial/connection budgets. The dashboard
launchers additionally require native Prometheus (`--prometheus-binary`);
the recorded version is 2.55.1. `--grafana` starts a disposable loopback-only
Grafana 11.3.0 with anonymous Admin access for importing the local dashboard;
it never connects to or modifies the shared Grafana. Services are removed
on exit. `--ui-hold-seconds 180` temporarily holds that owned instance for
browser inspection; creating `OUTPUT/browser-release` releases it early.
For the correctness-only layer, pass `--mode correctness` to
`verify_cache_remaining.py`: HTTP/origin/socket assertions still run, while
upper-only metric families and panel 53 are explicitly NOT APPLICABLE.
The default integrated mode continues to require those metrics.

[PR 4392/4393 accuracy evidence](results/pr439x/README.md) records independent
source-head comparisons and integrated request/storage classifications.
[Remaining-coverage evidence](results/remaining-coverage/README.md) adds actual
capacity eviction, saturated refresh-queue drops, L1 stale/SIE behaviour,
network disconnect/recovery and local Grafana proxy/UI execution. It also
links the separately executed direct StorageAPI encode/unknown-purpose cases.
Historical NOT EXERCISED entries above describe those earlier workloads;
the follow-up does not rewrite them as if they were originally exercised.
No finite suite establishes every cache behaviour or production equivalence.

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

Cache Operations includes **Origin fetches by dispatch path** (panel 62), next to
the per-family origin panels. It requires zalando/skipper#4387: `direct` means
force-mode static-backend fetches that bypass the proxy pipeline, access log and
backend metrics; `pipeline` means self-loopback through the proxy pipeline.
Both cold misses and revalidations are counted. Before #4387 is deployed, or
without origin fetches, the panel is optional (`Not exercised / no samples`)
for `verify-metrics.py`. Historical screenshot sets do not include this panel;
its screenshot slug is `origin-fetches-by-dispatch-path`.

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
