# Run report: 20261025-201011-skipper-kill

- **Scenario:** skipper-kill
- **Started:** 2026-10-25T20:10:11+0100
- **Ended:** 2026-10-25T20:10:11+0100
- **Duration:** 0m 00s
- **Max VUs:** 300
- **cache-e2e commit:** 62170d5
- **Skipper SHA:** 8a80d62e-uncommitted

## Summary

No `Aggregated` row in `locust_stats.csv` (file missing or empty).

## Endpoints

No per-endpoint rows in `locust_stats.csv`.

## Failures

`locust_failures.csv` not found.

## Exceptions

`locust_exceptions.csv` not found.

## Grafana

- [Cache Operations](http://127.0.0.1:55853/d/cache-performance-local?orgId=1&from=1792955411184&to=1792955411346)
- [Retail Application](http://127.0.0.1:55853/d/retail-application-local?orgId=1&from=1792955411184&to=1792955411346)

Time range: `from=1792955411184` to `to=1792955411346` (epoch ms). The base URL is the one in `.runtime/urls.txt` at report time; ports change between runs.

## Panel screenshots

No panel screenshots. Run `python3 screenshots.py <run folder>`.

## Fault timeline

| Time | Action | Scheduled offset | Detail |
|---|---|---|---|
| 2026-10-25T20:10:11+0100 | restore |  | origin: origin default case status 200; valkey: valkey up |
