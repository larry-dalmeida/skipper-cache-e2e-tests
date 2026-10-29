# Independent validation of reconstructed Skipper histories

These reports validate **unpushed source commits fetched directly from the
owning sessions into this e2e worktree's separate runtime clone**. The owners'
worktrees are not edited. E2e evidence is published by normal append; this
does not itself authorise or perform a Skipper history push.

## Correctness layer

New tip: `f17089f2ef5bab0e8ce46d2dfd23ed6049c817ec`.
Original final tip: `2702aa036eda2dc3b7eeabc17b95a1086ca6b985`.
Independently resolved **identical final trees**:
`485e8a3e6d73a667ab060d3dca73903a378647eb`.

The five reconstructed commits and exact commands are recorded in
[`correctness-f17089f-tree-and-commands.json`](correctness-f17089f-tree-and-commands.json).
This is actual Git tree equality, not a feature-name checklist or an assertion
copied from the source owner.

The exact new SHA was built natively and executed:

| Suite | Outcome |
|---|---|
| Unchanged two-process L1 reproduction | PASS: both A probes return 200/B/HIT, zero extra origin calls, total origins 2 |
| Unchanged coalescing suite | PASS: cold/warm 1 origin; synchronised ten-request burst/warm 1; Vary-star 3 |
| Real bounded L1 pressure | PASS: 400 primes; 196 evictions exactly match 196 absent cache-only probes |
| Full refresh queue | PASS: 260 L1 STALE responses, capacity 256 plus one blocked worker, 3 drops; 257 actual conditional 304 refreshes |
| Short-budget SIE | PASS: 500/502/503/504 and disconnect retain original 200/STALE body; all four HTTP faults returned after retention |
| Valkey disconnect and blackhole | PASS: cache-only errors, origin/L1 fallback, best-effort DELETE, timeout bounds and restored shared reads |
| Applicable legacy dashboard queries | PASS: finite positive eviction/drop/queue samples via native Prometheus |

Upper-only request/L2-operation metrics, route refresh outcome series,
L1 entries/capacity gauges and panel 53 are explicitly **NOT APPLICABLE**,
not silently fabricated as zeros or required prematurely in this layer.
The same HTTP/origin/wire assertions remain active. `--mode correctness`
selects only this explicit metric applicability; integrated mode retains
all upper-layer metric assertions.

Evidence includes separate L1/coalescing/remaining reports, build provenance,
actual socket relay/MONITOR observations and network logs. All owned resources
are removed. The latest harness unit suite passes **172** tests; the original
lower runtime run used the then-current 171-test suite.

## Observability layer

New tip: `bff0d80c0a20c9f641562a4a0d6901e217397e77`.
Original final tip: `ff7e9b03545203567dc40969b5b47d1da4f5760a`.
Independently resolved **identical final trees**:
`d434f896192040f858775198bac7850cc66a9bfb`.
The new correctness tip is its ancestor, with exactly five upper commits.
The entire inter-layer binary diff is byte-identical to the original
2702-to-ff7 diff; no feature source was lost during history reconstruction.

The exact new upper SHA was built and executed, not inferred green solely
from tree equivalence:

| Suite | Outcome |
|---|---|
| Unchanged L1 reproduction and coalescing | PASS, the same exact HTTP/body/origin requirements as the lower layer |
| Full request/L2 accuracy matrix | PASS, 24 cases, with independent origin/MONITOR/error and operation/purpose/outcome oracles |
| Existing dashboard contract workload | PASS, 43 target queries; eviction NOT EXERCISED in this workload, exercised by the separate pressure workload |
| L1 capacity pressure | PASS, 201 evictions = 201 absent cache-only probes, 199 retained hits and 400 origin calls |
| L1 stale / full refresh queue | PASS, 260 STALE responses, 3 actual overflow drops, 257 successful conditional 304 refreshes |
| Short-budget SIE faults and expiry | PASS, all four HTTP faults and a disconnect within retention, no stale HTTP response beyond retention |
| Disconnect / held-socket blackhole / recovery | PASS, exact logical-versus-visible-wire observations, L1 fallback and restored fresh-peer L2 reads |
| Actual StorageAPI encode / unknown purpose | Source owner reran the actual-Valkey focused race test on the final upper SHA; its report/log are included separately |
| Real Grafana datasource proxy and browser | PASS after one justified local-fixture retry, described below |
| Harness unit tests | PASS, 172 |

### Grafana failure and bounded recovery are separate evidence

The first final-upper remaining-behaviour report is deliberately still
**FAIL overall**, with four core cases **PASS** and the explicit error
`owned local Grafana readiness`. Container diagnostics show running/not-OOM;
SQLite schema migrations on the shared Docker overlay were taking roughly
0.2–0.4 seconds each and had not finished at the unchanged 60-second budget.
See `observability-bff0d80-remaining-ui-startup-error.json` and its Grafana log.
This is not labelled a Skipper producer regression or overwritten as PASS.

One justified fix uses a private **64MiB tmpfs** for that disposable Grafana's
data directory, retaining the existing 256MiB/0.5CPU limits and readiness
budget. Only Grafana was retried: the **actual bff Prometheus TSDB** was
reopened by an owned native Prometheus, with no new Skipper workload.
Each original historical query and evaluation timestamp was reproduced;
all five positive panel vectors matched exactly, by labels/timestamps/values,
before and through the new Grafana datasource proxy.

[`observability-bff0d80-grafana-replay.json`](observability-bff0d80-grafana-replay.json)
is the separate **PASS** retry report. This is new upper-source series/query/UI
evidence, not a relabelled historical 372b browser pass. The actual upper
eviction chart rendered in the browser, and a same-origin range query returned
25 finite / 24 positive points. The dropped-job chart rendered its
`queue cache.reval_dropped` series; its existing 0..100 ops/s axis makes the
small test rate appear close to zero. Exact positive values are asserted by
the proxy checks. Browser observations are saved in
[`observability-bff0d80-browser-ui.json`](observability-bff0d80-browser-ui.json);
screenshots remain in the conversation tool evidence.

```bash
python3 verify_cache_remaining.py --checkout .runtime/skipper-narrative-observability --sha bff0d80c0a20c9f641562a4a0d6901e217397e77 --grafana-replay .runtime/narrative-upper-remaining/result.json --output .runtime/narrative-upper-grafana-retry --ui-hold-seconds 180
```

Replay requires the original owned `.runtime/.../tsdb`, which is not uploaded
as binary noise. A fresh clone can instead rerun the documented full
`verify_cache_remaining.py ... --grafana` workload, now using the same private
tmpfs fixture. Original and retry reports retain their distinct statuses.

## Joint push gate and remaining limits

[`joint-push-gate.json`](joint-push-gate.json) records both exact new source
SHAs, baseline trees, five-commit counts, per-suite outcomes, the separate
Grafana recovery, commands and owned-resource cleanup.
The source owners' focused race/fmt/lint and StorageAPI logs are attributed
as owner-executed checks; e2e tree/runtime assertions are independently executed.
The source owner's **full shortcheck still has known Docker-provider/etcd and
JWT fixture failures**, not a globally green source-suite claim.

Twenty-four-hour wall-clock retention, production behaviour, exhaustive RFC/
SIE combinations, every network topology, real retail locale taxonomy and
every browser panel/link remain outside this bounded acceptance. None is
fabricated as a successful zero. All owned resources are stopped/removed;
no production or shared resources were modified.

## Published panel screenshots

These are actual Grafana 11.3.0 screenshots of **bff0d80c** producer data,
not older Oct25/26 captures, synthetic plots, or a new Skipper workload.
The retained upper-run TSDB was reopened with native Prometheus and the same
owned/private-tmpfs Grafana fixture. Historical display time is
**2026-10-29 10:07:36.946 to 10:08:58.178 UTC**.
Installed Chrome 156.0.8078.73 captured each solo panel at 1280x720 with an
isolated user-data directory. Only the private imported dashboard's legend
was changed to show max/last values with three decimals; original PromQL,
axis defaults, samples and the repository dashboard are unchanged.

| Actual panel | Screenshot |
|---|---|
| 60: Cache evictions, instance `eviction` | [PNG](screenshots/bff0d80-cache-evictions.png) |
| 19: Refresh errors / dropped jobs, instance `queue` | [PNG](screenshots/bff0d80-refresh-drops.png) |
| 7: Request rate using the external-client counter | [PNG](screenshots/bff0d80-client-rps.png) |

[`observability-bff0d80-panel-captures.json`](observability-bff0d80-panel-captures.json)
records source SHA, historical bounds, PNG digests/dimensions, unchanged
targets and independent positive same-origin query-range responses.
The browser's coarser query step and one-second verification queries sample
different instants, so their maxima need not be identical.
The replay and its owned services are removed after capture.

## JWT shortcheck correction and test-only successor

The original copied owner report called `TestJwtMetrics` unrelated. That
classification was **incorrect**: the 401/403 whole-counter-map assertions
omitted the new `client.requests = 1` entry. This is a coupled test-expectation
regression, not an unrelated timing failure. The source owner folded its
minimal expectation/comment correction into the third narrative commit,
replaying commits four and five.

The new upper tip is `3c14f755ab15f551dc3c1a5e2a1c83b6a3eaebf1`.
Independent comparison against bff shows exactly one changed path,
`filters/auth/jwt_metrics_test.go`; all producer and documentation bytes
are unchanged. The five-commit upper narrative and lower parent are retained.
The new SHA is rebuilt and checked with L1, unchanged coalescing, the full
24-case metric matrix and the focused JWT test. The complete bff remaining/
Grafana coverage and screenshots remain attributed to bff, with reuse
justified by producer-byte equality, not relabelled as new-SHA runtime runs.
See [`observability-3c14f75-test-only-gate.json`](observability-3c14f75-test-only-gate.json).
Docker-provider/etcd fixture failures remain a distinct source-shortcheck
environment limitation; this does not assert that the full suite is green.
The owner's single final full-shortcheck audit now passes `filters/auth`;
eight packages still fail Docker fixture setup: root `skipper`, `etcd/etcdtest`,
`filters/cache`, `net`, `net/redistest`, `net/valkeytest`, `proxy` and `ratelimit`.
No clean-foundation full-suite comparison was run, so these are **not proved
pre-existing**. The attributed [owner validation](jwt-owner/jwt-fix-validation.json),
[original JWT failure](jwt-owner/jwt-before-fix.log) and
[final full-shortcheck log](jwt-owner/jwt-fix-shortcheck.log) retain the evidence.

## Human-only attribution revision

The user subsequently requested removal of AI co-author trailers from both
five-commit source narratives. The new correctness tip is
`cb8ac7e00d29bf8c578e5578dc57c462af6689ac`; the new upper tip is
`e7786e047b92f5eae3ca19c56c2f2a24aae273fb`.
Independent local fetches verify **all ten corresponding commit trees** are
unchanged, not just the final trees. Larry's author/committer identities,
author/commit dates and human Signed-off-by trailers are preserved.
Each message changes only by removing its AI co-author line. The foundation
is untouched, each layer still contains five commits, and the upper layer
is based on the new correctness tip.

[`human-attribution-trees.json`](human-attribution-trees.json) records every
old/new commit pair, tree, subject and checked metadata.
[`human-attribution-push-gate.json`](human-attribution-push-gate.json) links
actual new-SHA builds/L1/coalescing for both tips, the upper 24-case metric
matrix and the newly rerun **172-test harness baseline**. Full earlier
behaviour evidence is reused only because all producer/test/documentation
bytes are identical; it is not mislabelled as a new full workload run.

### Correctness commit story and panel evidence

| Commit | Story |
|---|---|
| `78a9bf85d15c06bb3d8a74555779ed7eece421f7` | Normalise origin dispatch and preserve coalesced follower bodies |
| `d10650af3b973cfa5695631ebf6e2b1f607d2616` | Isolate Vary variants and preserve representations across 304 responses |
| `b308eefa0dfcc721a561c9a0508d244491c53abd` | Keep the last-good response when refresh fails |
| `bdd9dc6b6ee1b5b9d8bea7ae0e0858e6baa545fc` | Honour configured backend phase timeouts for cache-origin fetches |
| `cb8ac7e00d29bf8c578e5578dc57c462af6689ac` | Consult shared L2 once L1 freshness expires |

These are **actual correctness-only f170 producer screenshots**, with exact
tree equivalence to the new cb8 tip, captured by replaying the retained lower
Prometheus TSDB. They are not upper-stack illustrations. The historical window
is **2026-10-29 10:01:52.547 to 10:03:13.034 UTC**.

| Supported legacy panel | Correctness-only screenshot |
|---|---|
| 60: Cache evictions, instance `eviction` | [PNG](screenshots/f17089f-cache-evictions.png) |
| 19: Refresh errors / dropped jobs, instance `queue` | [PNG](screenshots/f17089f-refresh-drops.png) |
| 34: Refresh queue depth, actual maximum 256 | [PNG](screenshots/f17089f-refresh-queue-depth.png) |

The existing lower workload independently observed 196 real evictions,
3 overflow drops, a queue depth of 256 and 257 successful conditional
refreshes. Panel screenshots illustrate these exercised legacy series;
they are not stand-alone proofs of every commit's correctness. Use the
L1/coalescing reports and source tests for the corresponding exact assertions.
Panel 34 provides the third direct queue-saturation view. No panel 7/44
`client.requests`, upper request/tier counter or upper screenshot is used
to imply correctness-only metric support.

[`correctness-f17089f-panel-captures.json`](correctness-f17089f-panel-captures.json)
records original producer SHA, metadata-equivalent SHA, historical bounds,
PNG dimensions/checksums, unchanged PromQL and finite positive query responses.
Only private Grafana legend tables/decimal presentation changed; source
dashboards and metric samples did not. The owned Grafana/native-Prometheus
replay and isolated headless Chrome processes were stopped after capture.

## Fresh-clone verification commands

These commands use the public harness without the shared minikube stack,
Locust or production resources. The tested host is **macOS arm64 with Docker
Desktop**, Python 3.9+, Make, Git, curl and an installed Go toolchain that
supports `GOTOOLCHAIN=auto`; the source requires Go 1.27.3. Docker must be
running and images/releases downloadable. Linux needs a matching native
Prometheus binary and Docker host networking adaptation for Grafana's
`host.docker.internal`; it is not claimed validated by this command block.
The new source SHA becomes fetchable from GitHub after its gated owner push.
Harness code is pinned to 737258ec; this follow-up changes evidence/docs only.

```bash
set -e
git clone --branch larry-almeida-zse-l1-freshness-e2e-reproduction --single-branch \
  https://github.com/larry-dalmeida/skipper-cache-e2e-tests.git skipper-cache-validation
cd skipper-cache-validation
git checkout --detach 737258ec03a184b4f04bec1844669a33698834e2
export GOTOOLCHAIN=auto
mkdir -p .runtime/prometheus-native
curl -fL --retry 2 -o .runtime/prometheus-native/prometheus-2.55.1.darwin-arm64.tar.gz \
  https://github.com/prometheus/prometheus/releases/download/v2.55.1/prometheus-2.55.1.darwin-arm64.tar.gz
curl -fL --retry 2 -o .runtime/prometheus-native/sha256sums.txt \
  https://github.com/prometheus/prometheus/releases/download/v2.55.1/sha256sums.txt
(
  cd .runtime/prometheus-native
  grep 'prometheus-2.55.1.darwin-arm64.tar.gz$' sha256sums.txt | shasum -a 256 -c -
  tar -xzf prometheus-2.55.1.darwin-arm64.tar.gz
)
PROM="$PWD/.runtime/prometheus-native/prometheus-2.55.1.darwin-arm64/prometheus"
docker pull valkey/valkey:8.1.1-alpine@sha256:6a57d58c0a37cf7acc3045ac0fd6dd91be339774106ab4d9ca088013a096a99f
# Inspected as provenance by the dashboard launcher; runtime Prometheus is native.
docker pull prom/prometheus:v2.55.1
docker pull grafana/grafana-oss@sha256:a0f881232a6fb71a0554a47d0fe2203b6888fe77f4cefb7ea62bed7eb54e13c3
make test
```

Correctness layer at the human-attribution tip, from that same directory/shell:

```bash
git clone --filter=blob:none --no-checkout https://github.com/zalando/skipper.git .runtime/skipper-lower
git -C .runtime/skipper-lower fetch origin cb8ac7e00d29bf8c578e5578dc57c462af6689ac
git -C .runtime/skipper-lower checkout --detach cb8ac7e00d29bf8c578e5578dc57c462af6689ac
python3 verify_l1_freshness.py --checkout .runtime/skipper-lower --sha cb8ac7e00d29bf8c578e5578dc57c462af6689ac --output .runtime/lower-l1
python3 verify_candidate_behaviour.py --l1-result .runtime/lower-l1/result.json --output .runtime/lower-coalescing
python3 verify_cache_remaining.py --checkout .runtime/skipper-lower --sha cb8ac7e00d29bf8c578e5578dc57c462af6689ac --mode correctness --prometheus-binary "$PROM" --output .runtime/lower-remaining
```

Final upper layer, from that same directory/shell:

```bash
git clone --filter=blob:none --no-checkout https://github.com/zalando/skipper.git .runtime/skipper-upper
git -C .runtime/skipper-upper fetch origin e7786e047b92f5eae3ca19c56c2f2a24aae273fb
git -C .runtime/skipper-upper checkout --detach e7786e047b92f5eae3ca19c56c2f2a24aae273fb
python3 verify_l1_freshness.py --checkout .runtime/skipper-upper --sha e7786e047b92f5eae3ca19c56c2f2a24aae273fb --output .runtime/upper-l1
python3 verify_candidate_behaviour.py --l1-result .runtime/upper-l1/result.json --output .runtime/upper-coalescing
python3 verify_cache_observability.py --checkout .runtime/skipper-upper --sha e7786e047b92f5eae3ca19c56c2f2a24aae273fb --mode integrated --output .runtime/upper-metrics
python3 verify_cache_dashboards.py --checkout .runtime/skipper-upper --sha e7786e047b92f5eae3ca19c56c2f2a24aae273fb --prometheus-binary "$PROM" --output .runtime/upper-dashboards
python3 verify_cache_remaining.py --checkout .runtime/skipper-upper --sha e7786e047b92f5eae3ca19c56c2f2a24aae273fb --prometheus-binary "$PROM" --output .runtime/upper-remaining --grafana
(cd .runtime/skipper-upper && go test -race -count=1 ./filters/auth -run '^TestJwtMetrics$')
```

Every verifier exits nonzero on failure. L1, metrics, dashboards and remaining
coverage write `result.json`; coalescing writes `coalescing.json` and
`provenance.json` under its explicit output directory. The Grafana workload
asserts proxy queries and stops services automatically; add
`--ui-hold-seconds 180` only when inspecting
its printed `BROWSER_READY` URL. A fresh clone runs the real workload rather
than relying on the unuploaded historical TSDB. To reproduce the original
upper producer baseline instead, use exact SHA
`bff0d80c0a20c9f641562a4a0d6901e217397e77` for the upper checkout and every
upper `--sha` argument; its known JWT expectation failure is then expected.
