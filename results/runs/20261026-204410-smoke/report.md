# Run report: 20261026-204410-smoke

- **Scenario:** smoke
- **Started:** 2026-10-26T20:44:10+0100
- **Ended:** 2026-10-26T20:46:11+0100
- **Duration:** 2m 01s
- **Max VUs:** 60
- **cache-e2e commit:** a588ac6
- **Skipper SHA:** d767f6362226c6260e4288120e21136a3102cb26

## Summary

| Requests | Failures | Failure % | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | RPS (avg) |
|---|---|---|---|---|---|---|---|
| 6398 | 0 | 0.00% | 8.1 | 5.0 | 24.0 | 34.0 | 53.08 |

## Endpoints

| Method | Endpoint | Requests | Failures | Failure % | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | RPS (avg) |
|---|---|---|---|---|---|---|---|---|---|
| GET | hard/contentful_footers_direct | 1184 | 0 | 0.00% | 6.1 | 4.0 | 18.0 | 30.0 | 9.82 |
| GET | swr/contentful_footers_direct | 1054 | 0 | 0.00% | 6.2 | 4.0 | 17.0 | 29.0 | 8.74 |
| GET | swr/contentful_spaces | 674 | 0 | 0.00% | 4.2 | 3.0 | 11.0 | 17.0 | 5.59 |
| GET | hard/contentful_spaces | 672 | 0 | 0.00% | 4.4 | 3.0 | 12.0 | 20.0 | 5.57 |
| GET | hard/contentful_footers_external | 439 | 0 | 0.00% | 8.2 | 6.0 | 24.0 | 31.0 | 3.64 |
| GET | swr/contentful_footers_external | 371 | 0 | 0.00% | 9.0 | 7.0 | 23.0 | 33.0 | 3.08 |
| GET | hard/shop_navigation | 284 | 0 | 0.00% | 9.6 | 8.0 | 24.0 | 33.0 | 2.36 |
| GET | swr/shop_navigation | 252 | 0 | 0.00% | 8.1 | 7.0 | 19.0 | 26.0 | 2.09 |
| GET | hard/shop_products | 161 | 0 | 0.00% | 17.2 | 15.0 | 34.0 | 51.0 | 1.34 |
| GET | hard/contentful_vouchers | 154 | 0 | 0.00% | 12.4 | 10.0 | 31.0 | 52.0 | 1.28 |

Top 10 of 30 endpoints by request count.

## Failures

None recorded.

## Exceptions

None recorded.

## Grafana

- [Cache Operations](http://127.0.0.1:54438/d/cache-performance-local?orgId=1&from=1793043850303&to=1793043971617)
- [Retail Application](http://127.0.0.1:54438/d/retail-application-local?orgId=1&from=1793043850303&to=1793043971617)

Time range: `from=1793043850303` to `to=1793043971617` (epoch ms). The base URL is the one in `.runtime/urls.txt` at report time; ports change between runs.

## Panel screenshots

No panel screenshots. Run `python3 screenshots.py <run folder>`.
