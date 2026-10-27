# Run report: 20261026-204622-origin-stale

- **Scenario:** origin-stale
- **Started:** 2026-10-26T20:46:22+0100
- **Ended:** 2026-10-26T20:51:23+0100
- **Duration:** 5m 01s
- **Max VUs:** 300
- **cache-e2e commit:** a588ac6
- **Skipper SHA:** d767f6362226c6260e4288120e21136a3102cb26

## Summary

| Requests | Failures | Failure % | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | RPS (avg) |
|---|---|---|---|---|---|---|---|
| 30095 | 7453 | 24.76% | 550.3 | 3.0 | 4800.0 | 5000.0 | 100.13 |

## Endpoints

| Method | Endpoint | Requests | Failures | Failure % | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | RPS (avg) |
|---|---|---|---|---|---|---|---|---|---|
| GET | hard/shop_products | 4115 | 804 | 19.54% | 716.2 | 4.0 | 5000.0 | 5000.0 | 13.69 |
| GET | hard/contentful_spaces | 3127 | 1671 | 53.44% | 329.4 | 1.0 | 3300.0 | 5000.0 | 10.40 |
| GET | hard/contentful_footers_direct | 2795 | 675 | 24.15% | 414.4 | 2.0 | 4400.0 | 5000.0 | 9.30 |
| GET | swr/shop_products | 2374 | 446 | 18.79% | 776.5 | 5.0 | 5000.0 | 5000.0 | 7.90 |
| GET | hard/contentful_footers_external | 2256 | 233 | 10.33% | 271.6 | 1.0 | 3000.0 | 5000.0 | 7.51 |
| GET | swr/contentful_spaces | 1671 | 892 | 53.38% | 325.6 | 1.0 | 3300.0 | 5000.0 | 5.56 |
| GET | swr/contentful_footers_direct | 1606 | 378 | 23.54% | 472.7 | 3.0 | 4500.0 | 5000.0 | 5.34 |
| GET | swr/contentful_footers_external | 1406 | 128 | 9.10% | 319.5 | 2.0 | 4000.0 | 5000.0 | 4.68 |
| GET | hard/contentful_vouchers | 1213 | 276 | 22.75% | 485.6 | 3.0 | 4500.0 | 5000.0 | 4.04 |
| GET | hard/shop_search | 1031 | 248 | 24.05% | 793.7 | 5.0 | 5000.0 | 5000.0 | 3.43 |

Top 10 of 30 endpoints by request count.

## Failures

| Method | Endpoint | Error | Occurrences |
|---|---|---|---|
| GET | swr/shop_products | CatchResponseError('status') | 446 |
| GET | hard/shop_products | CatchResponseError('status') | 804 |
| GET | hard/shop_personal | CatchResponseError('status') | 1 |
| GET | swr/shop_search | CatchResponseError('status') | 124 |
| GET | swr/site_meta | CatchResponseError('status') | 18 |
| GET | hard/contentful_vouchers | CatchResponseError('status') | 276 |
| GET | swr/shop_categories | CatchResponseError('status') | 86 |
| GET | swr/shop_images | CatchResponseError('status') | 114 |
| POST | swr/shop_mutations | CatchResponseError('status') | 107 |
| GET | hard/shop_search | CatchResponseError('status') | 248 |
| HEAD | hard/static_head | CatchResponseError('status') | 58 |
| GET | hard/shop_categories | CatchResponseError('status') | 126 |
| GET | hard/shop_promotions | CatchResponseError('status') | 71 |
| GET | hard/contentful_footers_direct | CatchResponseError('status') | 675 |
| GET | hard/site_meta | CatchResponseError('status') | 35 |
| GET | hard/contentful_spaces | CatchResponseError('status') | 1671 |
| GET | swr/static_assets | CatchResponseError('status') | 71 |
| GET | hard/contentful_footers_external | CatchResponseError('status') | 233 |
| GET | swr/shop_promotions | CatchResponseError('status') | 49 |
| GET | hard/static_assets | CatchResponseError('status') | 140 |

10 more rows not shown.

## Exceptions

None recorded.

## Grafana

- [Cache Operations](http://127.0.0.1:54438/d/cache-performance-local?orgId=1&from=1793043982012&to=1793044283227)
- [Retail Application](http://127.0.0.1:54438/d/retail-application-local?orgId=1&from=1793043982012&to=1793044283227)

Time range: `from=1793043982012` to `to=1793044283227` (epoch ms). The base URL is the one in `.runtime/urls.txt` at report time; ports change between runs.

## Panel screenshots

No panel screenshots. Run `python3 screenshots.py <run folder>`.

## Fault timeline

| Time | Action | Scheduled offset | Detail |
|---|---|---|---|
| 2026-10-26T20:47:22+0100 | origin-503 | 60s | origin default case status 503 |
| 2026-10-26T20:48:52+0100 | origin-200 | 150s | origin default case status 200 |
| 2026-10-26T20:51:23+0100 | restore |  | origin: origin default case status 200; valkey: valkey up |

## Route revalidation fault evidence

- Skipper: `d767f6362226c6260e4288120e21136a3102cb26`
- `skipper_cache_route_reval_total{outcome="failed"}`: **+155** across **77 routes** (before 0, after 155).
- [Counter and Prometheus query evidence](route-revalidation-evidence.json)
- [Route revalidation exposition](route-revalidation-exposition.txt)
- Post-chaos dashboard verification: **43 PASS / 1 NOT EXERCISED / 0 FAIL** ([metrics.json](metrics.json)).
