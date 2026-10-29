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
are removed. The harness unit suite passes **171** tests.

## Observability layer

Independent verification of the reconstructed upper tip is pending its exact
local commit handoff. The already published baseline and feature checks remain
under [PR 4392/4393](../pr439x/README.md) and
[remaining coverage](../remaining-coverage/README.md); they are not relabelled
as tests of a yet-unsupplied new SHA.
