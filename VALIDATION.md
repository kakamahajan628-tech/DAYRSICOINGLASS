# Validation record — 2.0.0

This release was built from the uploaded `DAYRSICOINGLASS-main (2).zip` and inspected against the actual `RSI_1795528800.xlsx` report. The reference report contains 11 retained coins, 186 exchange rows and 60 gainer rows. Its CMC universe was available; its recorded scan took 895 seconds. Bitget had no valid 1D RSI in that report, and Coinglass stopped at the container memory guard. These observations informed the fixes; they were not assumed from a pasted audit alone.

## Local verification

The final local regression run passes 69 tests. Owner dashboard login, the actual 11-row reference watchlist, desktop/mobile layouts and JavaScript execution were additionally checked with headless Chromium. No JavaScript errors were observed.

The offline suite covers RSI/MFI vectors, complete-candle gaps and timing, Futures-first rules and Binance hosts, CMC completeness/ranking failures, actual recorded source response parsers, Bitget historical pagination, browser cleanup/guard behavior, database persistence/queue ownership/restart behavior, deletion during a scan, concurrent command handling, all 48 slots, weekly coverage, forward-return maturity/coverage/lookahead, signed owner login and HTTP HEAD, native XLSX tables/dates and chart rendering.

Top-level Python dependency versions are pinned to locally exercised versions. The suite uses disposable SQLite databases; it does not use the real bot token or production database.

## Actual public checks, 24 November 2026 IST

- Metadata and 1H candle parser contracts passed for both Futures and Spot on all six exchanges from the local machine. Twelve dated fixtures are included, with only public BTC response samples and real field shapes; these are not fabricated fixtures and do not represent several days of observations.
- Bitget Futures and Spot historical daily bars were also retrieved through the paginated 4H history endpoint.
- The closed-candle BTC preflight returned 17 of 18 RSI/MFI timeframe combinations. KuCoin's daily series had a real candle gap and remained unavailable.
- The complete CMC response had 7,878 reported records and exactly 7,878 unique IDs. Local stock/ETF/bond filtering and volume sorting produced 500 selected coins. The earlier live volume-sort path failed on out-of-order rows; no larger tolerance was substituted.
- Coinglass browser capture was not used for the free-profile preflight. Its optional guard/cleanup paths were tested with controlled fixtures. No encoded XHR decoder or paid Coinglass API access was claimed.

These local results do not establish endpoint availability from the Render data center. The user's reference report had Binance Futures HTTP 451 and Bybit HTTP 403; this release reports such failures without bypassing regional restrictions or treating them as proof of no Futures listing.

## Report and UI review

The user's actual workbook was read without modifying the original. The report-v2 preview preserves its available values and leaves missing values blank. Coverage is derived from the imported exchange rows. The original report did not store an exact scan-finish timestamp, so the preview labels it not recorded instead of inventing one. Native tables, panes and typed dates were checked, and the sheets were rendered for visual review. No current live results are implied by this preview.

## Checks still requiring the target environment

- A real Linux Docker build and its free-container memory peak. Docker is not installed on the local machine; the provided GitHub Actions workflow runs that build/test.
- The disposable PostgreSQL integration in GitHub Actions, followed by readiness against the user's real managed database. Local queue/lease tests used SQLite.
- Telegram delivery, daily health messages and callbacks using the real bot, plus a 48-slot soak test on Render.
- Enabling GitHub backup secrets, successful external encrypted upload and a restore drill into a new empty database. Only the connection/environment handling is tested locally; no production dump/restore was performed.
- Multi-day recorded fixtures and enough prospective history for 1D/7D outcomes and complete weekly cohorts. Missing history stays explicit.

The release is not represented as an error-free, deployed or production-soaked system. Free-host limits, public endpoint changes and regional access remain material dependencies.
