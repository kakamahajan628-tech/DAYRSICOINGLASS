"""Public read-only sources. Unknown schemas and incomplete lists fail closed."""
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from domain import EXCHANGES, FRAMES, num, stamp
from memory_guard import BrowserMemoryGuard, release_memory


def public_json(base, path, **params):
    url = base+path+("?"+urlencode(params) if params else "")
    for attempt in range(2):
        try:
            request = Request(url, headers={"User-Agent":"CryptoWatchlist/1.0 public market data", "Accept":"application/json"})
            with urlopen(request, timeout=15) as response:
                payload = json.load(response)
            if isinstance(payload, dict):
                code = str(payload.get("code", payload.get("retCode", "0")))
                if code not in ("0", "00000", "200000", "200"):
                    raise ValueError("API_ERROR_"+code[:20])
            return payload
        except HTTPError as error:
            if error.code in (429,500,502,503,504) and attempt == 0:
                time.sleep(1.5)
                continue
            raise ValueError("HTTP_"+str(error.code)) from None
        except (TimeoutError, OSError):
            if attempt == 0:
                time.sleep(1)
                continue
            raise ValueError("NETWORK_UNAVAILABLE") from None


# Binance spot market-data host matches BinanceProvider in the user's attached
# square_desk/providers.py. The separate USDT-futures host is not a spot alias.
BASES = {"Binance":("https://fapi.binance.com","https://data-api.binance.vision"),
    "Bybit":("https://api.bybit.com",)*2, "OKX":("https://www.okx.com",)*2,
    "Bitget":("https://api.bitget.com",)*2, "Gate":("https://api.gateio.ws/api/v4",)*2,
    "KuCoin":("https://api-futures.kucoin.com","https://api.kucoin.com")}


def candle(t, o, h, l, c, v):
    values = [num(x) for x in (t,o,h,l,c,v)]
    if any(x is None for x in values):
        raise ValueError("INVALID_OHLCV")
    t,o,h,l,c,v = values
    if t > 100000000000: t /= 1000
    if min(o,h,l,c) <= 0 or v < 0 or h < max(o,l,c) or l > min(o,h,c):
        raise ValueError("INVALID_OHLCV_RANGE")
    return dict(t=int(t),o=o,h=h,l=l,c=c,v=v)


def utc_days_from_4h(rows):
    days = {}
    for row in rows:
        days.setdefault(row["t"]//86400*86400, {})[row["t"]] = row
    result = []
    for start,group in sorted(days.items()):
        expected = [start+i*14400 for i in range(6)]
        if sorted(group) != expected: continue
        ordered = [group[t] for t in expected]
        result.append(candle(start,ordered[0]["o"],max(r["h"] for r in ordered),min(r["l"] for r in ordered),ordered[-1]["c"],sum(r["v"] for r in ordered)))
    return result


class Exchange:
    def __init__(self, name):
        self.name = name
        self.catalog = {"futures":{},"spot":{}}
        self.errors = {}
        self.observed = {}

    def get(self, market, path, **params):
        return public_json(BASES[self.name][market=="spot"], path, **params)

    def refresh(self):
        for market in ("futures","spot"):
            try:
                self.catalog[market] = self.load(market)
                if not self.catalog[market]: raise ValueError("EMPTY_USDT_CATALOG")
                self.errors.pop(market,None)
                self.observed[market] = time.time()
            except Exception as error:
                self.catalog[market] = {}
                self.errors[market] = str(error)[:80] if isinstance(error,ValueError) else type(error).__name__
        return self

    def load(self, market):
        future = market == "futures"
        found = {}

        def add(base, symbol, price, turnover, factor=1):
            base = "BTC" if base == "XBT" else base
            price, turnover = num(price), num(turnover)
            if not base: return
            if price is not None and price <= 0: price = None
            # Same-symbol identities are checked against the CMC universe separately.
            if base in found: raise ValueError("DUPLICATE_BASE_INSTRUMENT_"+base)
            found[base] = {"exchange":self.name,"market":market,"symbol":symbol,"coin":base,
                "price":price,"turnover":turnover,"factor":num(factor),"status":"OK" if price else "TICKER_MISSING","captured_at":stamp()}

        if self.name == "Binance":
            prefix = "/fapi/v1" if future else "/api/v3"
            instruments = self.get(market,prefix+"/exchangeInfo")["symbols"]
            prices = {x["symbol"]:x for x in self.get(market,prefix+"/ticker/24hr")}
            for x in instruments:
                if x.get("quoteAsset") != "USDT" or x.get("status") != "TRADING": continue
                if future and x.get("contractType") != "PERPETUAL": continue
                p = prices.get(x["symbol"],{})
                add(x["baseAsset"],x["symbol"],p.get("lastPrice"),p.get("quoteVolume"))
        elif self.name == "Bybit":
            category = "linear" if future else "spot"
            instruments, cursor = [], ""
            for _ in range(10):
                params = {"category":category,"limit":1000}
                if cursor: params["cursor"] = cursor
                data = self.get(market,"/v5/market/instruments-info",**params)["result"]
                instruments.extend(data["list"])
                cursor = data.get("nextPageCursor")
                if not cursor: break
            else: raise ValueError("INCOMPLETE_INSTRUMENT_PAGINATION")
            prices = {x["symbol"]:x for x in self.get(market,"/v5/market/tickers",category=category)["result"]["list"]}
            for x in instruments:
                if x.get("quoteCoin") != "USDT" or x.get("status") != "Trading": continue
                if future and x.get("contractType") != "LinearPerpetual": continue
                p = prices.get(x["symbol"],{})
                add(x["baseCoin"],x["symbol"],p.get("lastPrice"),p.get("turnover24h"))
        elif self.name == "OKX":
            kind = "SWAP" if future else "SPOT"
            instruments = self.get(market,"/api/v5/public/instruments",instType=kind)["data"]
            prices = {x["instId"]:x for x in self.get(market,"/api/v5/market/tickers",instType=kind)["data"]}
            for x in instruments:
                if x.get("state") != "live": continue
                if future and (x.get("settleCcy") != "USDT" or x.get("ctType") != "linear"): continue
                if not future and x.get("quoteCcy") != "USDT": continue
                base = x["instId"].split("-")[0] if future else x["baseCcy"]
                p = prices.get(x["instId"],{})
                # SWAP volCcy24h is base units, SPOT volCcy24h is quote turnover.
                turn = None if future else p.get("volCcy24h")
                add(base,x["instId"],p.get("last"),turn)
        elif self.name == "Bitget":
            category = "USDT-FUTURES" if future else "SPOT"
            instruments = self.get(market,"/api/v3/market/instruments",category=category)["data"]
            prices = {x["symbol"]:x for x in self.get(market,"/api/v3/market/tickers",category=category)["data"]}
            for x in instruments:
                if x.get("quoteCoin") != "USDT": continue
                if str(x.get("isRwa","")).lower() == "yes" or str(x.get("isReality","")).lower() == "yes": continue
                if x.get("instrumentType",x.get("symbolType","crypto")) not in ("crypto",""): continue
                if future and x.get("type") != "perpetual": continue
                if x.get("status") not in (None,"online","normal","live"): continue
                p = prices.get(x["symbol"],{})
                add(x["baseCoin"],x["symbol"],p.get("lastPrice"),p.get("turnover24h"))
        elif self.name == "Gate":
            instruments = self.get(market,"/futures/usdt/contracts" if future else "/spot/currency_pairs")
            prices = {x["contract" if future else "currency_pair"]:x for x in self.get(market,"/futures/usdt/tickers" if future else "/spot/tickers")}
            for x in instruments:
                symbol = x["name" if future else "id"]
                if not symbol.endswith("_USDT") or x.get("in_delisting"): continue
                if not future and x.get("trade_status") != "tradable": continue
                if future and x.get("type") != "direct": continue
                p = prices.get(symbol,{})
                add(symbol[:-5],symbol,p.get("last"),p.get("volume_24h_quote" if future else "quote_volume"),x.get("quanto_multiplier",1))
        elif self.name == "KuCoin":
            if future:
                for x in self.get(market,"/api/v1/contracts/active")["data"]:
                    if x.get("quoteCurrency") != "USDT" or x.get("settleCurrency") != "USDT" or x.get("isInverse"): continue
                    if x.get("status") != "Open" or x.get("marketType","CRYPTO") != "CRYPTO": continue
                    if x.get("assetClass","CRYPTO") != "CRYPTO": continue
                    add(x["baseCurrency"],x["symbol"],x.get("lastTradePrice"),x.get("turnoverOf24h"),x.get("multiplier"))
            else:
                prices = {x["symbol"]:x for x in self.get(market,"/api/v1/market/allTickers")["data"]["ticker"]}
                for x in self.get(market,"/api/v2/symbols")["data"]:
                    if x.get("quoteCurrency") != "USDT" or not x.get("enableTrading"): continue
                    p = prices.get(x["symbol"],{})
                    add(x["baseCurrency"],x["symbol"],p.get("last"),p.get("volValue"))
        return found

    def select(self, coin, pinned=None):
        # A failed futures request is not evidence that the future is absent.
        if pinned:
            market, symbol = pinned
            row = self.catalog[market].get(coin)
            if market in self.errors: return {"status":"ACCESS_ERROR","error":self.errors[market]}
            return row if row and row["symbol"] == symbol else {"status":"NOT_LISTED"}
        if "futures" in self.errors: return {"status":"ACCESS_ERROR","error":self.errors["futures"]}
        if coin in self.catalog["futures"]: return self.catalog["futures"][coin]
        if "spot" in self.errors: return {"status":"ACCESS_ERROR","error":self.errors["spot"]}
        return self.catalog["spot"].get(coin,{"status":"NOT_LISTED"})

    def candles(self, instrument, frame, now):
        market,symbol = instrument["market"],instrument["symbol"]
        future = market == "futures"
        if self.name == "Binance":
            data = self.get(market,("/fapi/v1" if future else "/api/v3")+"/klines",symbol=symbol,interval=frame,limit=200)
            return [candle(*x[:6]) for x in data]
        if self.name == "Bybit":
            data = self.get(market,"/v5/market/kline",category="linear" if future else "spot",symbol=symbol,
                interval={"1h":"60","4h":"240","1d":"D"}[frame],limit=200)["result"]["list"]
            return [candle(*x[:6]) for x in data]
        if self.name == "OKX":
            data = self.get(market,"/api/v5/market/candles",instId=symbol,bar={"1h":"1H","4h":"4H","1d":"1Dutc"}[frame],limit=200)["data"]
            return [candle(x[0],*x[1:5],x[6]) for x in data if x[8] == "1"]
        if self.name == "Bitget":
            data = self.get(market,"/api/v3/market/candles",category="USDT-FUTURES" if future else "SPOT",symbol=symbol,
                interval="1H" if frame == "1h" else "4H",limit=1000 if frame == "1d" else 200)["data"]
            rows = [candle(*x[:6]) for x in data]
            return utc_days_from_4h(rows) if frame == "1d" else rows
        if self.name == "Gate":
            if future:
                data = self.get(market,"/futures/usdt/candlesticks",contract=symbol,interval=frame,limit=200)
                factor = instrument.get("factor")
                if factor is None: raise ValueError("CONTRACT_MULTIPLIER_MISSING")
                return [candle(x["t"],x["o"],x["h"],x["l"],x["c"],float(x["v"])*factor) for x in data]
            data = self.get(market,"/spot/candlesticks",currency_pair=symbol,interval=frame,limit=200)
            return [candle(x[0],x[5],x[3],x[4],x[2],x[6]) for x in data]
        if self.name == "KuCoin":
            if future:
                data = self.get(market,"/api/v1/kline/query",symbol=symbol,granularity=FRAMES[frame]//60,
                    **{"from":int((now-201*FRAMES[frame])*1000),"to":int(now*1000)})["data"]
                factor = instrument.get("factor")
                if factor is None: raise ValueError("CONTRACT_MULTIPLIER_MISSING")
                return [candle(x[0],*x[1:5],float(x[5])*factor) for x in data]
            data = self.get(market,"/api/v1/market/candles",symbol=symbol,type={"1h":"1hour","4h":"4hour","1d":"1day"}[frame],
                startAt=int(now-201*FRAMES[frame]),endAt=int(now))["data"]
            return [candle(x[0],x[1],x[3],x[4],x[2],x[5]) for x in data]
        raise ValueError("UNKNOWN_EXCHANGE")


def markets():
    with ThreadPoolExecutor(max_workers=6) as pool:
        # Different exchanges can load in parallel, each exchange remains sequential.
        return dict(zip(EXCHANGES,pool.map(lambda name:Exchange(name).refresh(),EXCHANGES)))


def cmc_rows(payload):
    data = payload.get("data",{}).get("cryptoCurrencyList")
    if not isinstance(data,list): raise ValueError("CMC_LISTING_SCHEMA_CHANGED")
    result = []
    for x in data:
        tags = json.dumps(x.get("tags",[])).lower()
        text = (tags+" "+str(x.get("slug", ""))+" "+str(x.get("name", ""))).lower()
        if any(t in text for t in ("tokenized-stock","tokenized stock","tokenized-equit","stock-token","stock token","equity-token")): continue
        quotes = x.get("quotes",[])
        quote = next((q for q in quotes if q.get("name") == "USD"),None) if isinstance(quotes,list) else quotes.get("USD")
        volume = num((quote or {}).get("volume24h"))
        if not x.get("id") or not x.get("symbol") or volume is None: raise ValueError("CMC_ROW_SCHEMA_CHANGED")
        result.append({"id":int(x["id"]),"symbol":x["symbol"].upper(),"name":x["name"],"volume24h_usd":volume})
    return result


class Browser:
    """Created and called only by the engine worker; Playwright is thread-affine."""
    def __init__(self):
        self.runtime = self.context = self.page = None
        self.browser = None
        self.xhr = {}
        self.diagnostics = {}

    def start(self):
        if self.page and not self.page.is_closed(): return
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        kwargs = {"headless":True,"args":["--disable-dev-shm-usage","--no-sandbox","--renderer-process-limit=1","--disable-extensions","--disable-background-networking",
            "--disable-gpu","--disable-webgl","--js-flags=--max-old-space-size=128"]}
        if os.getenv("BROWSER_CHANNEL"): kwargs["channel"] = os.environ["BROWSER_CHANNEL"]
        self.browser = self.runtime.chromium.launch(**kwargs)
        self.context = self.browser.new_context(locale="en-US",timezone_id="Asia/Kolkata",viewport={"width":1680,"height":800},service_workers="block")
        def route_request(route):
            host=urlsplit(route.request.url).hostname or ""
            analytics=("google-analytics.com","googletagmanager.com","doubleclick.net","googlesyndication.com","clarity.ms")
            blocked=route.request.resource_type in ("image","media","font") or any(host==d or host.endswith("."+d) for d in analytics)
            route.abort() if blocked else route.continue_()
        self.context.route("**/*",route_request)
        self.page = self.context.new_page()
        self.page.on("response",self.observe)

    def observe(self,response):
        if urlsplit(response.url).hostname != "capi.coinglass.com" or "/api/index/rsi/list" not in urlsplit(response.url).path: return
        try:
            data = response.json()
            raw = json.dumps(data,separators=(",",":"))
            self.xhr = {"path":"/api/index/rsi/list","http":response.status,"data_type":type(data.get("data")).__name__,
                "sha256":hashlib.sha256(raw.encode()).hexdigest(),"captured_at":stamp(),"payload":data}
        except Exception:
            self.xhr = {"status":"XHR_NOT_JSON","captured_at":stamp()}

    def universe(self):
        # Same public CMC listing JSON; no SPA, Chromium or retained APIResponse bodies.
        all_rows = []
        # CMC has used different public sort parameter spellings. Only accept one
        # when its returned values prove a globally descending volume page.
        base = "https://api.coinmarketcap.com"
        path = "/data-api/v3/cryptocurrency/listing"
        sort_key = None
        for candidate in ("volume24h","volume_24h"):
            params = {"start":1,"limit":100,"sortBy":candidate,"sortType":"desc","convert":"USD","cryptoType":"all","tagType":"all"}
            rows = cmc_rows(public_json(base,path,**params))
            if len(rows)>=90 and all(b["volume24h_usd"]<=a["volume24h_usd"] for a,b in zip(rows,rows[1:])):
                sort_key = candidate
                break
        if not sort_key: raise ValueError("CMC_VOLUME_SORT_NOT_VERIFIED")
        for offset in range(1,2001,100):
            rows = cmc_rows(public_json(base,path,start=offset,limit=100,sortBy=sort_key,sortType="desc",convert="USD",cryptoType="all",tagType="all"))
            if not rows: raise ValueError("CMC_INCOMPLETE_TOP500")
            all_rows.extend(rows)
            if len(all_rows) >= 500: break
            time.sleep(.3)
        ids = [r["id"] for r in all_rows]
        if len(set(ids)) != len(ids): raise ValueError("CMC_DUPLICATE_PAGINATION")
        if any(b["volume24h_usd"] > a["volume24h_usd"]*1.02 for a,b in zip(all_rows,all_rows[1:])):
            raise ValueError("CMC_VOLUME_SORT_NOT_VERIFIED")
        # Re-sort small changes occurring between paged requests, reject ambiguous symbols.
        selected = sorted(all_rows,key=lambda r:r["volume24h_usd"],reverse=True)[:500]
        if len(selected) != 500: raise ValueError("CMC_INCOMPLETE_TOP500")
        duplicate = {r["symbol"] for r in selected if sum(x["symbol"]==r["symbol"] for x in selected)>1}
        return {"coins":selected,"ambiguous_symbols":sorted(duplicate),"captured_at":stamp(),"epoch":time.time(),
            "source":"CMC public listing JSON; reported 24h USD volume; stock-tag filter; symbol collisions excluded from admission"}

    def coinglass(self):
        guard=BrowserMemoryGuard().start()
        try:
            if guard.exceeded: raise ValueError("COINGLASS_MEMORY_BUDGET_EXCEEDED")
            result=self._coinglass()
            if guard.exceeded: raise ValueError("COINGLASS_MEMORY_BUDGET_EXCEEDED")
            return result
        except Exception:
            if guard.exceeded: raise ValueError("COINGLASS_MEMORY_BUDGET_EXCEEDED") from None
            raise
        finally:
            self.close()
            guard.stop()
            self.diagnostics=guard.summary()
            release_memory()

    def _coinglass(self):
        from playwright.sync_api import expect
        self.start()
        page = self.page
        self.xhr = {}
        page.goto("https://www.coinglass.com/RsiHeatMap",wait_until="domcontentloaded",timeout=60000)
        accept = page.get_by_role("button",name="Accept",exact=True)
        if accept.is_visible(): accept.click()
        expect(page.get_by_role("button",name="24h %",exact=True)).to_be_enabled(timeout=90000)
        page.get_by_role("combobox",name="Coin range",exact=True).click()
        page.get_by_text("Crypto",exact=True).last.click()
        count = page.get_by_role("combobox",name="Coins shown",exact=True)
        if count.inner_text().strip() != "All coins":
            count.click()
            page.get_by_text("All coins",exact=True).last.click()
        page.wait_for_timeout(1000)
        # Pagination is bounded; a partial table is never advertised as the full feed.
        text = page.locator("#rsi-table").inner_text()
        match = re.search(r"([\d,]+) items",text)
        if not match: raise ValueError("CG_TOTAL_COUNT_MISSING")
        total = int(match[1].replace(",",""))
        if total < 20 or total > 3000: raise ValueError("CG_UNEXPECTED_COUNT")
        feed = page.get_by_role("button",name=re.compile(r"^Updated ")).first
        label = feed.get_attribute("aria-label") or feed.inner_text()
        clock = re.search(r"Updated (\d{1,2}):(\d{2}):(\d{2})",label)
        if not clock: raise ValueError("CG_SOURCE_TIME_MISSING")
        local_seconds = (time.time()+19800)%86400
        age = (local_seconds-(int(clock[1])*3600+int(clock[2])*60+int(clock[3])))%86400
        if age > 600: raise ValueError("CG_STALE_FEED")
        result = {}
        for _ in range(160):
            raw = page.locator("#rsi-table tbody tr").evaluate_all("rows => rows.map(r => ({cells:Array.from(r.querySelectorAll('td')).map(c=>c.innerText),label:r.querySelector('button[aria-label^=\"Show \"]')?.getAttribute('aria-label')}))")
            added = 0
            for row in raw:
                symbol = re.fullmatch(r"Show (.+) on the chart",row.get("label") or "")
                cells = row["cells"]
                if not symbol or len(cells)<10: continue
                symbol = symbol[1]
                if symbol in result: continue
                values = [num(cells[i].replace(",", "").replace("%", "")) for i in (6,7,9)]
                if any(v is not None and not 0<=v<=100 for v in values): raise ValueError("CG_INVALID_RSI")
                result[symbol] = dict(zip(("rsi_1h","rsi_4h","rsi_1d"),values),captured_at=stamp(),feed_age_seconds=round(age),bar_state="displayed/live; not used for entry")
                added += 1
            if len(result) >= total: break
            next_page = page.get_by_role("button",name="Next page",exact=True)
            if next_page.is_disabled() or not added: break
            old_first = raw[0].get("label") if raw else None
            next_page.click()
            page.wait_for_function("old => document.querySelector('#rsi-table tbody tr button[aria-label^=\"Show \" ]')?.getAttribute('aria-label') !== old",arg=old_first,timeout=15000)
        if len(result) != total: raise ValueError("CG_INCOMPLETE_PAGINATION_"+str(len(result))+"_OF_"+str(total))
        return result, self.xhr

    def close(self):
        # Release every layer even when a browser crashes or an earlier close fails.
        context,browser,runtime=self.context,self.browser,self.runtime
        self.page=self.context=self.browser=self.runtime=None
        for obj,method in ((context,"close"),(browser,"close"),(runtime,"stop")):
            if obj:
                try: getattr(obj,method)()
                except Exception: pass
