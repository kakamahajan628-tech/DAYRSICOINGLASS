# Release validation — 12 November 2026

This is a source release, not a completed Render deployment or a guarantee of public-source access.
No bot token, Render account, PostgreSQL instance or UptimeRobot account was configured during packaging.

| Check | Actual result |
|---|---|
| Offline test suite |17 passed on Windows Python3.12 |
| Admission and retention | Synthetic integration:06:00 baseline, zero-gain exclusion, later entry, below70 priority, continued tracking, owner suppression |
| Indicator arithmetic | Wilder14 reference vector, monotonic/flat RSI, MFI positive/negative flows, forming-bar exclusion, stale/gapped-bar rejection |
| Market/source separation | Futures access failure does not trigger spot; pinned spot does not borrow metrics/baseline from a newly available future with the same ticker text |
| Exchange candle adapters | Fixture field order, millisecond/second normalization, contract-to-base volume, UTC daily aggregation |
| Persistence | SQLite close/reopen and two-connection lease exclusion tested; **live PostgreSQL connection not tested** |
| Scheduling |48 unique slots,06:00 IST day boundary, missing-baseline admission pause |
| Weekly report |336 valid observations required; one below70 or missing slot prevents qualification |
| Delivery | Simulated Telegram timeout stored UNCERTAIN; same slot not automatically resent |
| Owner/HTTP access | Wrong chat/user rejected; /health public; /ready503 before ready; /status401 without bearer secret |
| XLSX structure | ZIP integrity, native tables, freeze panes, real numeric date cells, formula-injection protection |
| XLSX visual QA | All9 possible sheets imported/rendered for review using artifact-tool; row/column widths, large volume formatting and weekly result width adjusted |
| Coinglass live browser check | Success,21:49 IST:544 unique Crypto/All-coins displayed RSI rows, freshness check, complete pagination |
| Coinglass XHR | HTTP200 outer JSON captured; `data` was a string; SHA256 recorded. DOM RSI is used for the separate Coinglass columns |
| CMC live top500 | Initial response failed descending-volume validation. Adapter now tries both public sort spellings and requires proof of descending volume. Retests hit DNS failure, so **final500 acquisition remains unverified** |
| Exchange live check this release | Bitget catalog requests encountered NETWORK_UNAVAILABLE locally; no source was reclassified as NOT_LISTED |
| Earlier endpoint probes,11Nov | OKX/Bitget/Gate/KuCoin tickers returned data, Binance/Bybit network failures; OKX candles403. These were PC-side observations, not Render certification |
| Linux Docker image | Not built locally: Docker CLI unavailable |
| Render512MB browser stability | Not tested |
| Real Telegram Excel delivery | Not tested; requires owner's dedicated bot credentials |
|48 successful live deliveries in one day | Not established; requires a full deployed observation day |

Packaging verifies Python syntax, archive CRC, an explicit file allowlist and SHA256 manifest.
No profile, session, token, local DB, dependency folder or historical report is included in the ZIP.
Synthetic QA workbooks and screenshots stay outside the deployment package.

## Behavior when a live check fails

- CMC missing/incomplete/sort-unverified: no new watchlist admissions; no substitute universe.
- Missing06:00 baseline: no new entries for those instruments; wait for next valid06:00 capture.
- Coinglass unavailable: blank Coinglass columns, explicit source status; exchange RSI stays separate.
- Futures discovery inaccessible: ACCESS_ERROR; do not infer absence or fall back to spot.
- Missing indicator candles: blank indicator values and PARTIAL coverage, never invented zeros.
- Interrupted host: no fabricated historical Coinglass snapshots and no false336-slot weekly qualification.

## Remaining deployment checks

After connecting the user's services, verify `/ready`, owner-only command replies, CMC complete500,
all six venues' catalogs/candles from the actual Render region, the next06:00 baseline, a real XLSX
attachment, a redeploy against the same PostgreSQL database and a full48-slot day. Review the Excel
Data Status sheet rather than treating an HTTP200 heartbeat as proof of complete market data.

Render free services can sleep after15minutes idle, have ephemeral files and a shared750-hour
monthly allowance. A5minute UptimeRobot monitor is a heartbeat/wake strategy, not an SLA.
See [Render](https://render.com/docs/free) and [UptimeRobot](https://uptimerobot.com/pricing/).
