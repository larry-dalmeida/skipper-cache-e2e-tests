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
