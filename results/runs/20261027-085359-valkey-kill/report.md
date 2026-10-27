# Run report: 20261027-085359-valkey-kill

- **Scenario:** valkey-kill
- **Started:** 2026-10-27T08:53:59+0100
- **Ended:** 2026-10-27T08:55:30+0100
- **Duration:** 1m 31s
- **Max VUs:** 300
- **cache-e2e commit:** 41c60ba
- **Skipper SHA:** d767f6362226c6260e4288120e21136a3102cb26

## Summary

| Requests | Failures | Failure % | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | RPS (avg) |
|---|---|---|---|---|---|---|---|
| 10544 | 0 | 0.00% | 4.0 | 2.0 | 16.0 | 26.0 | 116.81 |

## Endpoints

| Method | Endpoint | Requests | Failures | Failure % | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | RPS (avg) |
|---|---|---|---|---|---|---|---|---|---|
| GET | hard/contentful_footers_direct | 2404 | 0 | 0.00% | 2.6 | 1.0 | 10.0 | 19.0 | 26.63 |
| GET | hard/contentful_spaces | 1523 | 0 | 0.00% | 2.0 | 1.0 | 6.0 | 11.0 | 16.87 |
| GET | hard/contentful_footers_external | 1099 | 0 | 0.00% | 3.2 | 1.0 | 12.0 | 22.0 | 12.18 |
| GET | swr/contentful_footers_direct | 959 | 0 | 0.00% | 3.3 | 2.0 | 12.0 | 22.0 | 10.62 |
| GET | hard/static_assets | 667 | 0 | 0.00% | 2.3 | 1.0 | 7.0 | 21.0 | 7.39 |
| GET | swr/contentful_spaces | 607 | 0 | 0.00% | 2.2 | 1.0 | 6.0 | 14.0 | 6.72 |
| GET | swr/contentful_footers_external | 443 | 0 | 0.00% | 4.1 | 2.0 | 14.0 | 25.0 | 4.91 |
| GET | hard/contentful_vouchers | 412 | 0 | 0.00% | 4.3 | 2.0 | 15.0 | 26.0 | 4.56 |
| GET | hard/shop_products | 342 | 0 | 0.00% | 8.8 | 7.0 | 23.0 | 36.0 | 3.79 |
| GET | swr/static_assets | 257 | 0 | 0.00% | 3.0 | 1.0 | 10.0 | 20.0 | 2.85 |

Top 10 of 30 endpoints by request count.

## Failures

None recorded.

## Exceptions

None recorded.

## Grafana

- [Cache Operations](http://127.0.0.1:54438/d/cache-performance-local?orgId=1&from=1793087639410&to=1793087730553)
- [Retail Application](http://127.0.0.1:54438/d/retail-application-local?orgId=1&from=1793087639410&to=1793087730553)

Time range: `from=1793087639410` to `to=1793087730553` (epoch ms). The base URL is the one in `.runtime/urls.txt` at report time; ports change between runs.

## Panel screenshots

No panel screenshots. Run `python3 screenshots.py <run folder>`.

## Fault timeline

| Time | Action | Scheduled offset | Detail |
|---|---|---|---|
| 2026-10-27T08:54:26+0100 | valkey-down | 27s | valkey down |
| 2026-10-27T08:54:44+0100 | valkey-up | 45s | valkey up |
| 2026-10-27T08:55:30+0100 | restore |  | origin: origin default case status 200; valkey: valkey up |
