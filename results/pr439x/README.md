# PR 4392 / 4393 runtime accuracy comparison

Publication follow-up: the user subsequently authorised publishing the companion
e2e branch. Commits `534c0df` and `52b27bc`, plus the earlier uncommitted L1
harness/docs/reports, were published with `33dd7d1`; blackhole coverage followed
in `ab3f9f1`. The local/uncommitted notes below describe the initial validation
state, not the current publication state. See
[remaining coverage](../remaining-coverage/README.md) for subsequent tests and
explicit limits. No additional pull request or production changes were made.

Recorded 29 October 2026 using clean native macOS/arm64 Skipper builds,
Go's selected `go1.27.3` toolchain, fresh controlled origins and separate
Valkey containers. No production resources or candidate sources changed.

## Result

| Source | Exact revision | Feature accuracy matrix | Separate L1 freshness reproduction |
|---|---|---|---|
| [zalando/skipper#4392](https://github.com/zalando/skipper/pull/4392), initially pinned | `4e86999d7dc0f6ce40a4647723b8b36c6458b973` | PASS, 24 cases | FAIL, known retained-L1 defect |
| PR 4392, moved live head | `a572728a5fda06aa67e7f98ac3cc51a7fec6f4ce` | PASS, same 24 cases rerun | Same source tree as initial pin; failure above is not a new independent run |
| [zalando/skipper#4393](https://github.com/zalando/skipper/pull/4393) | `39bbc400b5516274b8ad8cb8b6823695c87bc43d` | PASS, 24 cases | FAIL, known retained-L1 defect |
| Integrated observability candidate | `372b07b4f73fd500182bf8f960fb7e2d5ee51639` | PASS, both features, 24 cases | PASS |

PR 4392 moved during validation. Its old and new heads have the exact same
tree, `cc6d61b9599365b1ec72ce63199d06f00a4e2336`; this was a history-only
rewrite, not a feature change. The moved head was nevertheless rebuilt and
the feature matrix rerun to preserve exact current-head provenance.
Implementation and coordinating sessions were notified.

The independent source PRs do **not** contain each other's feature.
PR 4392 has `skipper_cache_request_total`, but no new L2 operation/processing
families. PR 4393 has the L2 operation/processing families but no new request
family. These absences are reported explicitly, never synthesised as zeros.
Both preserve the original L1 freshness bug. Their real L1 regression failures
are correctness failures, not infrastructure errors and not a failure of the
feature-specific metric assertions.

The integrated candidate includes both own feature deltas plus the existing
correctness and client-only-counter work. Its unchanged L1 reproduction
returns **200 / B / HIT / zero extra origin calls** for both probes, with
two total origin calls. Existing behavioural checks also pass: cold+warm
requires one origin call, ten synchronised cold requests+warm require one,
and three sequential `Vary: *` requests require three.

The existing strengthened native Prometheus/dashboard acceptance also passes
on this integrated SHA: **43 target queries PASS, eviction NOT EXERCISED**.
It confirms 41 external requests versus 55 listener requests, positive
success/fault refresh rates, finite exercised latency quantiles, zero-client
denominator behaviour and the previously agreed unit/aggregation contracts.
This is actual PromQL execution, not Grafana UI or datasource-proxy validation.

## Independent runtime oracles

`verify_cache_observability.py` uses real HTTP and controlled origin accounting.
Each step records its exact response, expected request outcome/tier, origin
delta, metric deltas and Valkey activity. It does not merely test that a series
exists or that a query is finite.

For healthy storage operations, a separate `valkey-cli MONITOR` connection
records actual GET/SET/DEL transmissions. Initial key presence plus command
order determines GET success versus clean miss; `vary:` keys identify metadata
independently of metric labels. SET updates key presence; deleting an absent
key remains a successful DELETE, not a clean GET miss. All per-step L2
operation/purpose/outcome counts match that oracle.

Fault probes change only the disposable container's default-user ACL to reject
GET, SET or DEL, restoring permissions in `finally` blocks. Exact logical
invocation expectations are checked by purpose, and Valkey's independent
`INFO commandstats` **calls plus rejected_calls** match total metric invocations.
ACL errors do not create processing/decode errors. These measured cases have
one wire attempt per client method; they do not prove anything about hidden
retry cardinality during network partitions.

Representation probes deliberately replace an identified UUID-scoped entry
or metadata sentinel with malformed JSON. A successful Valkey GET still counts
as `outcome="success"`; the separate processing family counts `stage="decode"`.
The corrupt response is read twice (lookup and SIE snapshot), yielding two
response/decode errors before an origin fetch repairs it. Corrupt metadata
produces one metadata/decode error while the healthy response is served fresh
from L2 without an origin call.

## Exact classifications

| Probe | Integrated request classification | Independent activity and storage observations |
|---|---|---|
| Cold GET | `origin/none +1` | 1 origin; metadata GET miss +1, response GET miss +2, response SET success +1 |
| Warm GET | `fresh/l1 +1` | 0 origin; metadata lookup may still call L2, response comes from L1 |
| Other process, no L1 warming | `fresh/l2 +1` | 0 origin; real response GET success |
| Vary read before warming, then warm read | `fresh/l2 +1`, then `fresh/l1 +1` | Read provenance is captured before warming, not inferred from post-read L1 contents |
| Cache-only miss | `cache_only_miss/none +1` | HTTP 504, no origin; clean GET misses |
| HEAD on L2 response | `fresh/l2 +1` | 0 origin; response GET success +2 and response SET success +1, including silent HEAD handling |
| `no-cache`, `no-store`, unsafe POST | `bypass/none +1` each | Each reaches origin; POST emits response and metadata DELETEs |
| Origin HTTP 503 | `origin/none +1` | HTTP failure is not a transport/lookup error; 1 origin |
| Corrupt response | `error/none +1` | Recovered HTTP 200 after 1 origin, GET success + decode processing errors; not a GET transport error |
| Corrupt metadata | `fresh/l2 +1` | 0 origin; metadata processing/decode +1, healthy response GET success |
| ACL-rejected cache-only GET | `error/none +1` | HTTP 504, 0 origin; one metadata and one response GET error |
| ACL-rejected metadata/response SET | `origin/none +1` | 1 origin; persistence failure does not change serving source; failed Sets fall back to L1 |
| ACL-rejected invalidation | `bypass/none +1` | 1 origin; response and metadata DELETE error +1 each |
| Ten synchronised cold requests | `origin/none +1`, `coalesced/none +9` | Exactly 1 origin, 10 equal HTTP 200 bodies; L2 counts logical storage calls, not ten origin fetches |
| Stale response plus background 304 | `stale/l2 +1` | Exactly 1 origin; metadata GET miss +1, response GET success +4, response SET success +1, including silent revalidation and 304 reads |
| RFC cold pipeline dispatch | `origin/none +1` | Exactly 1 origin despite internal listener replay; metadata GET miss +2, response GET miss +1, response SET success +2 |
| Forged raw `X-Cache-Revalidate: 1` | `bypass/none +1` | External request must not disappear as an authenticated internal replay |

Each runtime matrix contains **33 external probes** (24 cases, with ten
requests in the burst) plus three explicitly recorded cache-priming requests.
The integrated final request counter therefore totals **36**. The original
request-source head totals **35** because its raw-header exclusion drops the
forged-header probe. That is a **known source accuracy limitation**, recorded
as such rather than hidden by the feature-matrix PASS label; the integrated
authenticated context fixes it. Its 35 total counts exactly match all other
scenario expectations plus the three setup requests.

## Commands and provenance

```bash
python3 verify_cache_observability.py --checkout .runtime/skipper-source4392-a572 --sha a572728a5fda06aa67e7f98ac3cc51a7fec6f4ce --mode request-source --output .runtime/source4392-a572-accuracy
python3 verify_cache_observability.py --checkout .runtime/skipper-source4393 --sha 39bbc400b5516274b8ad8cb8b6823695c87bc43d --mode storage-source --output .runtime/source4393-accuracy
python3 verify_cache_observability.py --checkout .runtime/skipper-observability372b --sha 372b07b4f73fd500182bf8f960fb7e2d5ee51639 --mode integrated --output .runtime/integrated372b-metrics
python3 verify_l1_freshness.py --checkout .runtime/skipper-observability372b --sha 372b07b4f73fd500182bf8f960fb7e2d5ee51639 --output .runtime/integrated372b-l1
python3 .runtime/verify_candidate_behaviour.py --l1-result .runtime/integrated372b-l1/result.json --output .runtime/integrated372b-coalescing
python3 .runtime/verify_observability_acceptance.py --checkout .runtime/skipper-observability372b --sha 372b07b4f73fd500182bf8f960fb7e2d5ee51639 --output .runtime/integrated372b-dashboards
make test
```

The earlier L1 verifier remains unchanged and uncommitted in this worktree;
the existing launcher scripts remain under `.runtime/`. The focused new commit
does not sweep those earlier files into scope. Their run reports are included
here as evidence, not as a claim that those harness files have been published.

Skipper is built natively from clean exact revisions, **not an image**.
Each JSON report includes version, selected build toolchain, binary SHA-256,
harness SHA-256 and exact process arguments. Valkey is pinned to
`valkey/valkey:8.1.1-alpine@sha256:6a57d58c0a37cf7acc3045ac0fd6dd91be339774106ab4d9ca088013a096a99f`.
Every Skipper uses explicit two-second Valkey dial/connection budgets and
dynamic loopback ports. Native Prometheus is the previously checksum-verified
2.55.1 binary and scrapes actual loopback listeners directly.

Evidence here includes exact source and integrated metric reports, monitor
logs, integrated raw expositions, separate known-failing source L1 reports,
and passing integrated L1/coalescing/dashboard reports. No owned service is
left running. The prior local dashboard commit `534c0df` is preserved.

## Genuine unexercised scope

- Serialization **encode** errors and `purpose="unknown"` require direct Storage
  callers or unit-only fixtures; no HTTP route exercised them.
- No partition/reconnect retry cardinality, eviction/queue-drop load, all SIE
  combinations, 24-hour retention or production workload was tested.
- Stale L2 was exercised; no separate L1-only stale-serving process was added.
- The new request/storage counters were validated directly against activity,
  while the pre-existing dashboard queries were executed with real Prometheus.
  No new dashboard panels for these two families or Grafana rendering/proxy
  claims are included.
- Synthetic dashboard route families are not real retail locale coverage.
  Unobserved families/events are not successful zeros.
