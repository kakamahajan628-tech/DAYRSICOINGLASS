# DAYRSICOINGLASS 2.0.0

This release separates scanning, Telegram commands and HTTP into three processes inside **one free Render web service**. PostgreSQL keeps the queue, watchlist, run ledger and history. Coinglass is an optional display source and is disabled in the free deployment profile.

## Deploy the complete release

1. Extract the ZIP and put its contents at the root of your GitHub repository. Upload hidden `.github` files too. Replace the old runtime files together; this is a complete release, not a one-file patch.
2. Keep the existing Render service and database. Runtime: **Docker**. Dockerfile: `./Dockerfile`, context: repository root. Clear old Build/Start Command overrides. The Docker command runs `python serve.py`; starting `app.py` alone does not start the workers.
3. Set the variables below, then manually deploy the new commit. No database reset is needed. Schema changes are additive; first startup creates indexes and `cg_jobs_v2` under a database lock.
4. Verify public `GET /health` and `HEAD /health` return 200. `/ready` returns 200 after the database and both worker heartbeats are available. Open `/` and sign in with the dashboard password. Use `/status` and `/daily` in the owner Telegram chat.
5. Set the external uptime monitor to HTTPS `https://YOUR-SERVICE.onrender.com/health`, every five minutes. This is a liveness probe. Use `/ready` for worker readiness and the daily Telegram digest for actual report delivery/coverage.

| Variable | Value / purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Existing dedicated BotFather token |
| `TELEGRAM_CHAT_ID` | Owner's numeric private chat ID, or group ID |
| `TELEGRAM_OWNER_ID` | Numeric owner user ID; required for groups |
| `DATABASE_URL` | Durable PostgreSQL connection URI, password URL-encoded |
| `RUN_SECRET` | Existing random secret, at least 24 characters. Blueprint can generate it. Owner Bearer authorization for `/status` and `POST /run` |
| `DASHBOARD_PASSWORD` | A separate unique password of at least 12 characters. If omitted, the dashboard stays disabled while Telegram runs |
| `COINGLASS_ENABLED` | `false` for this free profile |
| `SYSTEM_ALERTS_ENABLED` | `true`; system failures/recoveries and daily health only |
| `TOP_N` | `15`, maximum 15 |
| `SCAN_BUDGET_SECONDS` | `900`, allowed 60–1200 |
| `DATA_DIR` | `/tmp/coinglass-data`; temporary report files only |
| `CMC_API_KEY` | Optional official CMC listings key. Not required for public mode |
| `DATABASE_EXPIRES_AT` | Optional provider expiry timestamp with timezone, e.g. `2026-12-25T06:00:00+05:30`; no expiry date is guessed |

For Supabase, copy the database connection URI from the project's Connect panel. Use a reachable direct or session-pooler URI; the session pooler is usually appropriate for an IPv4-only host. This app requires TLS and disables psycopg server-side prepared statements for pooler compatibility. Do not paste API `anon`/`service_role` keys into `DATABASE_URL`. Backups need a connection that supports `pg_dump`, preferably direct/session mode.

Use the database role that owns the app's tables. Startup enables row-level security on the two app tables and revokes their access from Supabase's `anon`/`authenticated` roles when present. This prevents bypassing dashboard login through the public-schema API; the table owner continues operating without forced RLS.

An existing Telegram webhook is reported as a conflict and is not deleted automatically. Keep the bot dedicated to this application. `ALLOW_EPHEMERAL_DB=true` and SQLite are for disposable tests only.

## Data and rules

- Six USDT venues: Binance, Bybit, OKX, Bitget, Gate and KuCoin. Futures first; Spot only after a successful Futures catalog proves the coin is absent. A Futures HTTP 403/451 is an access failure and **does not** permit silent Spot substitution. Binance Spot remains `https://data-api.binance.vision`; Futures remains `https://fapi.binance.com`. Spot metadata uses official compact parameters.
- CMC means reported USD 24-hour volume, not an order-book liquidity score. Public mode first requests the complete inventory and checks `totalCount` against the returned unique IDs, then excludes tokenized stocks/ETFs/bonds and sorts locally. This handles source rank/quote mismatches without loosening the volume-order guard. If a complete inventory is unavailable, the earlier bounded, strictly verified paginated path remains a fallback. Unknown schemas/incomplete membership pause new admissions. A verified cache can be used for up to 30 hours; a failed refresh retries on each scan. CMC public endpoints are undocumented and can change.
- At 06:00 IST, store each instrument's first public ticker response received in the 06:00–06:03 window. A missed baseline is not replaced with rolling 24-hour gains. Existing retained coins continue to be measured.
- Every half hour, positive top-15 gainers since that baseline become candidates. Admission needs all three **completed UTC** 1H/4H/1D Wilder RSI14 values in 70–100 and strictly above their respective previous completed-candle RSI. MFI14 and actual OHLCV are reported. At least 100 contiguous closed bars are required; older warm-up data before a gap is discarded. Flat/undefined RSI is unavailable, not zero.
- Bitget daily bars are assembled from six complete UTC 4H candles per day. Historical pages use the documented 100-row maximum and move backwards until enough closed history exists. KuCoin gaps remain gaps; no synthetic flat candles are enabled.
- Retained coins stay until `/del` or `/delall`. Their admission venue/instrument stays pinned. A delete during an in-flight scan cannot revive the coin. `/allow` only removes admission suppression. Below-70 crossings change report priority; there are no trading alerts.
- `/weeklycoin` needs all 336 valid half-hour observations in the seven completed IST trading days ending at the latest 06:00, all three RSI values at least 70, and at least one positive top-15 observation on each day. Missing coverage cannot qualify.

## Telegram and dashboard

| Command | Result |
|---|---|
| `/daily` | Latest scheduled Excel file |
| `/weeklycoin` | Weekly qualification and explicit 336-slot coverage |
| `/expand BTC`, `/EXPANDBTC` | Consolidated last 30 observed days, MFI/RSI/price and stored OHLCV |
| `/coin BTC` | Seven-day observed price/RSI PNG chart; gaps and source changes break lines |
| `/top` | Stored top gainers by venue, with snapshot time |
| `/below70` | Current priority rows |
| `/research` | Excel with forward returns and rule variants |
| `/health` | Previous completed trading day's delivery/data-quality counts |
| `/status` | Source, worker, configuration and incident status |
| `/del BTC,ETH`, `/DEL(BTC,ETH)`, `/delall` | Remove active coins, preserve history and suppress re-admission |
| `/allow BTC,ETH` | Permit future rule-based re-admission |
| `/help` | Command reference and inline daily/weekly/top/priority buttons |

The dashboard shows retained coins, coverage counts, observed RSI/price history and source status. It refreshes from stored observations, not a live streaming feed. Owner login uses an eight-hour signed HttpOnly, Secure, SameSite cookie, login throttling and same-origin checks. Secrets do not appear in URLs. Public health endpoints expose no market history or credentials.

Excel uses typed dates/numbers, native filter tables, frozen headers and clean Arial fonts. Scan start, finish and per-row quote times are distinct. `NOT_DUE` marks future slots; the delivery ledger is explicitly as of file generation, before that file can be uploaded. Coverage counts valid venue/timeframe cells out of the six-venue, three-timeframe matrix (18), including unavailable/unlisted venues. Detailed reasons are in Exchange Detail. Missing numeric values stay blank.

## Operation and resource limits

The supervisor restarts failed children with backoff. Database jobs use unique IDs and owner-bound claims; scan leases renew and expire after worker loss. Old interrupted or uncertain Telegram sends are retained without automatic duplicate sends. Old queued slots are marked missed rather than backfilled with current prices. `POST /run` queues the current unrecorded slot only.

Scanner requests run with a three-venue concurrency bound, network timeouts and a scan budget. Commands can finish while the scanner fetches. This does not provide independent service scaling: all three processes share the free instance's CPU/RAM. Full coin observation history is preserved; database storage must be monitored as the retained list grows.

Snapshot cache: 7 days. Candidate/research daily price batches and stored OHLCV: 30 days. Research tracking continues for 8 days after an instrument last appears as a candidate. Fixtures: 7 days. Completed jobs: 14 days. `/expand` exports 30 days, `/coin` shows 7; older coin observations remain in PostgreSQL. Price samples are compressed by instrument/day, and research processes one instrument's price history at a time.

Daily maintenance runs once after 06:05 IST when the host is available. It reports the previous day's 48 delivery slots and valid coin observations, checks exchange API schemas plus CMC membership, keeps dated public-response fixtures and sends deduplicated system-issue/recovery messages. API access failures are distinguished from successful parser contracts. The local fixtures in this ZIP were captured on 24 November 2026; future fixtures accumulate in the database.

Render Free can spin down after 15 idle minutes and has monthly instance/bandwidth limits. An uptime monitor cannot prevent provider suspension, regional restrictions, database outages or memory restarts. **48 slots/day is the schedule, not a guarantee of 48 successful deliveries on a free host.** Free Render PostgreSQL also expires; use your durable provider and check its current plan. Official references: [Render Free](https://render.com/docs/free), [Bitget candles/history](https://www.bitget.com/docs/catalog/market/market-data), [KuCoin missing-tick policy](https://www.kucoin.com/docs-new/v2/rest/ua/get-klines).

## Optional Coinglass capture

The default image does not download Chromium. Setting `COINGLASS_ENABLED=true` alone is insufficient: build a browser-enabled image with `docker build --build-arg INSTALL_BROWSER=true .`, then enable the variable. The container memory guard stays active and closes new browser children on pressure. Free 512 MB operation with Chromium is not guaranteed.

Capture validates table headers, pagination and displayed timestamps. The encoded XHR envelope is retained for diagnosis; there is no invented decoder or access-control bypass. Coinglass displayed/live RSI goes in separate columns and never drives admission. Failure or disabled capture does not invalidate otherwise available exchange RSI.

## Forward-return research

`python research.py --output research.json` or `/research` compares RSI 70 vs 75, top 15 vs top 5, and a fully observed prior-week variant. Rejected as well as admitted top-gainer candidates are recorded prospectively. The first qualifying candidate per coin/venue/instrument/variant in the 21-day window defines a cohort. Its entry reference is the first actual ticker **after** the recorded rule evaluation, within 35 minutes; missing entry samples are counted.

Outcomes use the first source-matched sample at/after 1H, 4H, 1D and 7D, with a ten-minute tolerance. `NOT_MATURE` and `MISSING_COVERAGE` are explicit. MAE, MFE and maximum drawdown are based on observed half-hour ticker prices, with incomplete sample paths flagged. Weekly evidence requires the prior seven complete IST days, not future observations. Reports show the denominator and missing outcomes for each variant. Cohorts overlap and fees/funding/slippage are excluded.

This is prospective research, not a historical execution backtest. The old Excel upload cannot reconstruct past CMC membership, rejected candidates or true missing candles. Public OHLCV alone cannot reconstruct those rules. No trading/execution keys or order functions exist.

## Backup and checks

`.github/workflows/backup.yml` prepares an encrypted custom-format `pg_dump` every Sunday 07:00 IST or manually. Configure repository Actions secrets `BACKUP_DATABASE_URL` and `BACKUP_ENCRYPTION_KEY` (random, at least 32 characters). The archive is checked with `pg_restore --list`, encrypted with authenticated Fernet/PBKDF2, uploaded as a 30-day Actions artifact, then the external upload is recorded in the database. No unencrypted dump is uploaded. Until this succeeds, backup status remains unconfigured/stale; a local temporary file is not counted as an external backup.

Install backup-only `cryptography>=46,<51` and a compatible PostgreSQL client to decrypt: `python backup.py --decrypt downloaded.dump.enc --output restored.dump`, with the same encryption key in the environment. Validate its checksum and archive listing, then restore into a **new, empty test database**, using `pg_restore --no-owner --no-acl`. Compare record counts and watchlist/history before considering a restore drill complete. This release has not restored your real database; preserve the encryption key separately from artifacts. PostgreSQL client version must support the source server. [pg_dump documentation](https://www.postgresql.org/docs/current/app-pgdump.html).

Run `python run_tests.py`. `python live_check.py --output preflight.json` checks public BTC candle indicators and CMC without Telegram messages. `.github/workflows/ci.yml` includes Linux tests, a disposable PostgreSQL integration and a Docker build/test. Those hosted checks must pass in your repository before relying on deployment. See `VALIDATION.md` for local results and limits.
