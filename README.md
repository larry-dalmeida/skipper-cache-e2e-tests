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

## Local endpoints and dashboards

- [Cache Operations](http://localhost:3000/d/cache-performance-local/cache-operations)
- [Retail Application](http://localhost:3000/d/retail-application-local/retail-application)
- [Prometheus](http://localhost:9090)
- [Skipper](http://localhost:9999/get)
- [Raw Skipper metrics](http://localhost:9911/metrics)
- Mock origin: `http://127.0.0.1:8081`; control port `:9091`; Valkey: `localhost:6379`.

Dashboards are version-controlled JSON in `dashboards/` and are loaded into the
`cache-dashboard` ConfigMap by `start-stack.sh`. Edit the JSON, then re-apply:

```bash
kubectl create configmap cache-dashboard --from-file=dashboards/ \
  --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -
kubectl rollout restart deployment/grafana
```

Server-side apply is required because the dashboards exceed the 256 KB
client-side annotation limit. The `local-prometheus` datasource UID is set in
`stack.yaml`. Every panel uses that UID.

On a metrics-enabled revision, after sending traffic:

```bash
python3 verify-metrics.py
```

The metric verifier executes every expression in every `dashboards/*.json`
against Prometheus.

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

`CohortShape` runs the swarm endlessly (no run time). Each cohort (`en-US`, `de-DE`
Accept-Language, so two cache identities) gets a random VU target in 10..30, with the
total capped at `--max-vus` (default 50, max 50); targets re-roll every 60s. Start the
UI-less supervisor with the Start button or `POST /swarm` on 8089 (the shape then owns VUs).
Keys are replaced by the endpoint catalogue (`catalogue.py`), built from the real
Contentful path snapshot in `contentful-cache-paths.json`. Each request picks a weighted
family and locale (532 entries: 9 families across 26 shop locales plus the Contentful
spaces, footers and vouchers paths). Shop endpoints (products, categories, search,
navigation, promotions) make up 25% of traffic. These are mock online-shop endpoints
on the Skipper route table, which `python3 catalogue.py routes > routes.eskip` renders.
Stats are named `cohort/family`. CSV stats land in `.runtime/locust_*.csv` and the log
in `.runtime/locust.log`.
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
python3 cachectl.py check verify -- --label X --sha Y   # also: tiers, ratio, metrics, coalescing
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
