# Coinglass + six-exchange Telegram watchlist — Render ZIP

Yeh self-contained Python source package hai. ZIP ke andar Dockerfile, Render Blueprint,
Telegram commands, 30-minute scheduler, PostgreSQL persistence, XLSX reports aur offline tests hain.
PC chalu rakhne ki zaroorat deployment ke baad nahi hai. Render par actual deployment abhi nahi hua hai.

**Pehle `START_HERE_HINDI.md` padhein.** Render ZIP directly upload karke deploy nahi karta;
ZIP ke contents GitHub repository ke root mein upload karke Render se connect karein.

## Confirmed source choice

Entry RSI = exchange ke **completed** 1H, 4H aur 1D candles se calculated Wilder RSI(14).
Teeno RSI 70–100 mein aur apni previous completed candle se strictly increasing hone chahiye.
Coinglass ka website-displayed/live RSI alag columns mein rahega; woh entry calculation nahi hai.
MFI(14) typical price × base-asset volume se calculate hota hai. Yeh indicator hai, measured cash
inflow/outflow ya exchange bank deposits nahi.

## What runs

- IST trading day 06:00 to next05:30, 48 scheduled slots, every30minutes.
- First public ticker response during06:00–06:03 is the baseline; actual capture time is stored.
  Restart/late deployment par missing baseline ko 24h change se replace nahi kiya jata.
  Next valid06:00 baseline tak new admissions pause; existing coins remain tracked.
- CMC public browser listing: top500 by **reported24h USD volume**, not order-book liquidity.
  Refresh06:00; cached complete universe can be used under30hours with its actual source time.
  Incomplete500/sort/schema errors pause admissions. No substitute exchange-only universe.
- Binance, Bybit, OKX, Bitget, Gate, KuCoin. USDT perpetual/futures first; same-exchange spot
  only after verified futures absence. Futures request error never triggers spot fallback.
- Positive gain since baseline, top15 **within the verified CMC universe** on any exchange,
  plus all three RSI conditions on that same exchange. Among qualifying exchanges use the
  listed exchange order. Rank scope is explicitly within the500, not every exchange listing.
- Watchlist source/instrument is pinned on admission; no silent source switching. Delisted/
  inaccessible sources leave blank numeric values and a status. Coins persist after leaving
  gainers/top500; /del and /delall suppress re-addition while retaining all stored history.
- Below70 crossings and continuing below70 status get report priority. No separate trading alerts.
- Scheduled XLSX delivery is silent, with source errors and actual capture timestamps.
  Only current missed/late slot is attempted at restart. Historical missing slots are marked
  MISSED, never filled with a current snapshot pretending to be historical.
- Daily Gainers, RSI Watchlist, Exchange Detail, Data Status and48Slots sheets; filtered native
  Excel tables, frozen headings, typed date/numeric cells, clean fonts and priority colouring.
- `/expand BTC` adds stored half-hour observations and observed completed OHLCV. History
  starts when the service observes the coin; no invented old Coinglass history or screenshots.
- `/weeklycoin`: seven **complete IST trading days ending at latest06:00**. Requires all336
  half-hour observations valid and all three RSIs≥70; daily top15 presence at least once on
  each of those7days. Incomplete weeks show coverage and cannot qualify. This is the chosen
  interpretation of “week in gainers”; not a requirement to rank in every single scan.

## Important boundaries, already reflected in reports

This package is not a promise of uninterrupted free hosting or complete public-site access.
Coinglass/CMC can change markup/schema or restrict automation. The browser captures the
Coinglass public RSI XHR response automatically; its `data` has been observed as an encoded
string. No unsupported decoder or private key is included: displayed RSI is read from the
rendered, freshness-checked Crypto/All-coins table with pagination. Latest raw XHR body/hash
is saved privately in the database, not published through HTTP.

Only exact base-symbol matches are admitted (legacy KuCoin XBT→BTC explicitly mapped).
Duplicate symbols inside the selected CMC universe are excluded from new admissions.
CMC stock tags/names and known exchange stock flags are excluded. Symbol matching and tags
are not a universal contract-address identity registry: unlabelled stock proxies or a symbol
collision outside that500 can require an additional mapping/exclusion rule. No automatic
1000/1M token multiplier alias is guessed. Prices are selected USDT instruments, CMC universe
volume remains USD and is labelled as such.

Public API access is region/IP-dependent. Missing values are blank with ACCESS_ERROR,
NOT_LISTED, BASELINE_MISSING, TICKER_MISSING or indicator error details. No proxy or restriction
bypass is included. Tick errors, browser errors or a1200-second scan budget can make a report
PARTIAL. Existing coins are evaluated before new candidates. Retained coins request metrics
on all six available instruments; new candidate metrics are requested on their top15 exchanges.
OKX futures24h quote turnover is blank because its ticker does not provide exact quote turnover.

RSI uses100+ contiguous closed bars (request200; KuCoin roughly201). Bitget1D UTC candles
are aggregated from six complete4H bars, requested1000 (roughly166days). Other daily bars
use UTC boundaries. Daily *ranking baseline* is still06:00 IST, not daily-candle midnight.
This can differ slightly from Coinglass/chart RSI due to venue, market, live/closed state,
candle boundaries and finite warm-up. Source/candle timestamps are retained.

## Runtime and HTTP

`python app.py` runs one process. Health server remains responsive while the single worker
serializes browser scans and owner commands. Six exchange catalogs load in parallel.
Telegram long polling uses a separate thread; use a dedicated bot without an existing webhook.
Do not scale replicas or run another poller for the same bot/database.

| Endpoint | Access | Purpose |
|---|---|---|
| GET `/` | Public | Small service landing page, no market data/secrets |
| GET `/health` | Public | HTTP200 process heartbeat; UptimeRobot target |
| GET `/ready` | Public | HTTP200 after database setup;503 while configuration is incomplete |
| GET `/status` | Bearer RUN_SECRET | Queue, sources and latest run |
| POST `/run` | Bearer RUN_SECRET | Queue current scheduled slot if not already recorded |

Uptime requests do not create extra reports. A slot is recorded once. Database lease prevents
overlapping scans. A Telegram upload timeout is stored as UNCERTAIN and not blindly resent;
use `/daily` for an explicit repeat. A restarted process marks old in-progress runs interrupted.
Commands wait behind an active scan (up to approximately20minutes plus bounded browser I/O).

PostgreSQL stores watchlists, suppression, baselines, compressed snapshots, observations, OHLCV,
indicator cache, Telegram offset/file IDs and run ledger. Render's temporary directory only
holds XLSX during upload; it is deleted afterwards. No automatic history deletion. Monitor
your database provider's storage limit and export/upgrade before it fills.

## Validation and diagnostics

Python3.12. Install `pip install -r requirements.txt`, then `python run_tests.py`.
Offline tests cover48slots, closed candles/gaps, RSI admission/crossings, market fallback,
volume units, UTC aggregation, restart persistence, lease exclusion, deletion suppression,
336-slot weekly coverage, uncertain delivery, owner auth, HTTP auth and native XLSX dates/tables.
Test data is synthetic and never presented as a live report.

Optional read-only live check (no Telegram sends):

```
python live_check.py
python live_check.py --browser
```

Chromium must be installed locally for the second command; Dockerfile installs it automatically.
Build image locally with `docker build -t crypto-watchlist .` where Docker is available.
Then `docker run --env-file .env -p 10000:10000 crypto-watchlist`.

Read `VALIDATION.md` for actual checks and remaining live checks. Never commit your bot token,
database connection URL/password or RUN_SECRET. All authorization is owner-chat/user based;
the service has no exchange trading keys and does not submit orders.

## Provider references

- [Render free service limits](https://render.com/docs/free)
- [Render Blueprint reference](https://render.com/docs/blueprint-spec)
- [UptimeRobot plans/intervals](https://uptimerobot.com/pricing/)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Bitget public market data](https://www.bitget.com/docs/catalog/market/market-data)
- [KuCoin futures candles](https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-klines)
- [Gate public API](https://www.gate.com/docs/developers/apiv4/en/)
