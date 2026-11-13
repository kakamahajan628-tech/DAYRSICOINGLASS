"""Durable, serialized scan engine. Reports describe observations, not predictions."""
import base64
import json
import os
import re
import time
import zlib
from pathlib import Path
from domain import EXCHANGES, FRAMES, day_start, slot_info, stamp, entry_pass, crosses, indicators
from reports import make_report
from sources import Browser, markets
from memory_guard import container_memory,release_memory


def pack(value):
    return {"zlib_base64":base64.b64encode(zlib.compress(json.dumps(value,allow_nan=False,separators=(",",":")).encode(),6)).decode()}


def unpack(value):
    return json.loads(zlib.decompress(base64.b64decode(value["zlib_base64"]))) if value else None


def parse_command(text):
    text = text.strip()
    match = re.fullmatch(r"/del\(([^)]+)\)",text,re.I)
    if match: return "del", match[1]
    match = re.fullmatch(r"/expand([a-z0-9]+)",text,re.I)
    if match: return "expand", match[1]
    parts = text.split(maxsplit=1)
    command = parts[0].split("@")[0].lstrip("/").lower() if parts else "help"
    return command, parts[1].strip() if len(parts)>1 else ""


class Engine:
    def __init__(self, store, telegram=None, browser=None, market_loader=markets):
        self.store,self.telegram = store,telegram
        self.browser = browser or Browser()
        self.market_loader = market_loader
        self.top_n = int(os.getenv("TOP_N","15"))
        self.output = Path(os.getenv("DATA_DIR","/tmp/coinglass-data"))
        self.output.mkdir(parents=True,exist_ok=True)
        self.budget = int(os.getenv("SCAN_BUDGET_SECONDS","1200"))
        self.deadline = 0
        self.calls = 0

    def status(self):
        latest = self.store.get("meta","latest")
        watches = [w for _,w in self.store.list("watch") if w.get("active")]
        return {"watchlist_count":len(watches),"latest":latest,"timezone":"Asia/Kolkata",
            "memory":container_memory(),"last_browser_resources":getattr(self.browser,"diagnostics",{}),
            "rule_rsi":"Exchange completed candles, Wilder14; Coinglass separate live columns",
            "delivery":"48 scheduled slots/day; free host interruptions can cause gaps"}

    def active(self):
        return {k:v for k,v in self.store.list("watch") if v.get("active")}

    def save_snapshot(self, key, snapshot):
        self.store.put("snapshot",key,pack(snapshot))
        self.store.put("meta","latest",{"key":key,"captured_at":snapshot["captured_at"],"status":snapshot["status"],"scheduled_ist":snapshot["scheduled_ist"]})

    def snapshot(self, key=None):
        key = key or (self.store.get("meta","latest") or {}).get("key")
        return unpack(self.store.get("snapshot",key)) if key else None

    def metrics(self, exchange, row, now):
        result, errors = {}, []
        for frame, duration in FRAMES.items():
            key = "|".join((exchange.name,row["market"],row["symbol"],frame))
            cache = self.store.get("indicator",key)
            expected = int(now//duration)*duration
            if cache and cache.get("bar_close") == expected:
                result[frame] = cache
                continue
            if time.monotonic() >= self.deadline:
                errors.append(frame+":SCAN_BUDGET_EXCEEDED")
                continue
            try:
                self.calls += 1
                values = indicators(exchange.candles(row,frame,now),frame,now)
                self.store.put("indicator",key,values)
                result[frame] = values
                # Keep observed OHLCV, not invented history. Deduplicate each closed bar.
                for bar in values["candles"]:
                    self.store.put("bar",key+"|"+str(bar["t"]),{"coin":row["coin"],"exchange":exchange.name,
                        "market":row["market"],"symbol":row["symbol"],"frame":frame,**bar})
                time.sleep(0.12)
            except Exception as error:
                errors.append(frame+":"+(str(error)[:90] if isinstance(error,ValueError) else type(error).__name__))
        return result,errors

    def scan(self, schedule=None):
        start = time.time()
        info = schedule or slot_info(start)
        key = info["key"]
        if self.store.get("run",key): return "ALREADY_RECORDED"
        if not self.store.lease("scan",self.budget+300): return "BUSY"
        run = {"scheduled_ist":info["scheduled_ist"],"scheduled":info["scheduled"],"day":info["day"],
            "state":"FETCHING","delivery":"PENDING","started_at":stamp(start)}
        self.store.put("run",key,run)
        self.deadline = time.monotonic()+self.budget
        self.calls = 0
        snapshot = {"scheduled_ist":info["scheduled_ist"],"captured_at":stamp(start),"epoch":start,"coins":[],
            "exchange_rows":[],"sources":{},"status":"PARTIAL"}
        try:
            # Prices first: slow browser refresh must not postpone the 06:00 capture.
            exchanges = self.market_loader()
            baseline = self.store.get("baseline",info["day"])
            if baseline is None and info["slot"] == 1 and start-info["scheduled"] <= 120:
                prices = {}
                for name,exchange in exchanges.items():
                    for market,catalog in exchange.catalog.items():
                        for row in catalog.values():
                            observed = exchange.observed.get(market,0)
                            if 0 <= observed-info["scheduled"] <= 180 and row.get("price"):
                                prices["|".join((name,market,row["symbol"]))] = {"price":row["price"],"observed_at":row["captured_at"]}
                if prices:
                    baseline = {"prices":prices,"captured_at":stamp(),"scheduled_ist":info["scheduled_ist"],
                        "policy":"First public ticker response in 06:00–06:03 IST; actual source timestamp retained"}
                    self.store.put("baseline",info["day"],baseline)
            snapshot["sources"]["06:00 baseline"] = baseline.get("policy") if baseline else "MISSING: no substitute 24h gains; admission paused until next valid 06:00 baseline"
            universe = self.store.get("meta","universe")
            if universe is None or start-universe["epoch"] >= 86400 or info["slot"] == 1:
                try:
                    universe = self.browser.universe()
                    self.store.put("meta","universe",universe)
                except Exception as error:
                    snapshot["sources"]["CMC refresh"] = type(error).__name__+": "+str(error)[:150]
            valid_universe = universe is not None and start-universe["epoch"] < 30*3600
            snapshot["sources"]["CMC universe"] = (str(len(universe["coins"]))+" rows; source "+universe["captured_at"]) if universe else "UNAVAILABLE"
            snapshot["sources"]["CMC admission"] = "AVAILABLE" if valid_universe else "PAUSED: no verified complete universe under 30h old"
            if valid_universe: snapshot["sources"]["CMC verification"] = universe.get("verification",{})
            snapshot["sources"]["Admission decision"] = ("PAUSED_CMC_UNAVAILABLE" if not valid_universe else
                "PAUSED_BASELINE_MISSING" if not baseline else "EVALUATING_VERIFIED_UNIVERSE")
            # Tickers are captured before 06:00 baseline work, then compacted
            # before Chromium starts. Retained coins outside today's500 stay.
            needed={c["symbol"] for c in universe["coins"]} if valid_universe else set()
            needed.update(self.active())
            baseline_keys=set()
            for name,exchange in exchanges.items():
                exchange.catalog_counts={market:len(rows) for market,rows in exchange.catalog.items()}
                for market,rows in exchange.catalog.items():
                    exchange.catalog[market]={symbol:row for symbol,row in rows.items() if symbol in needed}
                    baseline_keys.update("|".join((name,market,row["symbol"])) for row in exchange.catalog[market].values())
            if baseline:
                baseline={**baseline,"prices":{k:v for k,v in baseline["prices"].items() if k in baseline_keys}}
            release_memory()
            try:
                cg,xhr = self.browser.coinglass()
                snapshot["sources"]["Coinglass"] = str(len(cg))+" displayed RSI rows; live, separate from rule RSI"
                self.store.put("meta","coinglass_xhr",pack(xhr))
                snapshot["sources"]["Coinglass XHR"] = {k:v for k,v in xhr.items() if k != "payload"}
            except Exception as error:
                cg = {}
                snapshot["sources"]["Coinglass"] = "UNAVAILABLE: "+type(error).__name__+" "+str(error)[:120]
                self.browser.close()
            snapshot["sources"]["Browser resources"] = getattr(self.browser,"diagnostics",{})
            eligible = {c["symbol"]:c for c in universe["coins"] if c["symbol"] not in universe.get("ambiguous_symbols",[])} if valid_universe else {}
            watch = self.active()
            selected, ranks = {}, {}
            all_symbols = set(eligible) | set(watch)
            for name,exchange in exchanges.items():
                snapshot["sources"][name] = {"errors":exchange.errors,**exchange.catalog_counts,
                    "retained_target_instruments":sum(len(rows) for rows in exchange.catalog.values())}
                gainers = []
                for symbol in sorted(all_symbols):
                    row = dict(exchange.select(symbol))
                    row.update(coin=symbol,exchange=name)
                    base_key = "|".join((name,row.get("market",""),row.get("symbol","")))
                    base = (baseline or {}).get("prices",{}).get(base_key)
                    row["day_gain"] = row["price"]/base["price"]-1 if base and row.get("price") else None
                    row["baseline_at"] = base.get("observed_at") if base else None
                    if row["status"] == "OK" and not base: row["status"] = "BASELINE_MISSING"
                    selected[(symbol,name)] = row
                    if symbol in eligible and row.get("day_gain",0) is not None and row.get("day_gain",0)>0:
                        gainers.append((symbol,row["day_gain"]))
                gainers.sort(key=lambda x:(-x[1],x[0]))
                for rank,(symbol,_) in enumerate(gainers,1):
                    selected[(symbol,name)]["rank"] = rank
                    if rank <= self.top_n: ranks.setdefault(symbol,[]).append(name)
            computed = {}
            entry_attempts=retained_attempts=0
            # Existing coins are measured before new admissions, even outside today's top500.
            for symbol in sorted(set(watch)|set(ranks),key=lambda s:(s not in watch,s)):
                record = watch.get(symbol)
                if record:
                    names = [record["exchange"]]
                else:
                    names = ranks.get(symbol,[])
                    if (self.store.get("watch",symbol) or {}).get("suppressed"): continue
                for name in names:
                    row = selected[(symbol,name)]
                    if record:
                        pinned = exchanges[name].select(symbol,(record["market"],record["instrument"]))
                        row = dict(pinned,coin=symbol,exchange=name,day_gain=None)
                    if row.get("price") is None: continue
                    if record: retained_attempts+=1
                    else: entry_attempts+=1
                    metric,errors = self.metrics(exchanges[name],row,start)
                    computed[(symbol,name,row["market"],row["symbol"])] = (metric,errors)
                    if not record and entry_pass(metric):
                        record = {"symbol":symbol,"name":eligible[symbol]["name"],"cmc_id":eligible[symbol]["id"],"active":True,
                            "suppressed":False,"exchange":name,"market":row["market"],"instrument":row["symbol"],
                            "entered_at":stamp(start),"entry_price":row["price"],"peak":row["price"],"previous_rsi":{}}
                        self.store.put("watch",symbol,record)
                        watch[symbol] = record
                        break
            for symbol,record in sorted(watch.items()):
                name = record["exchange"]
                row = dict(exchanges[name].select(symbol,(record["market"],record["instrument"])))
                baseline_row = selected.get((symbol,name),{})
                base_key = "|".join((name,record["market"],record["instrument"]))
                base = (baseline or {}).get("prices",{}).get(base_key)
                row["day_gain"] = row["price"]/base["price"]-1 if base and row.get("price") else None
                if row.get("symbol") == baseline_row.get("symbol") and row.get("market") == baseline_row.get("market"):
                    row["rank"] = baseline_row.get("rank")
                metric,errors = computed.get((symbol,name,record["market"],record["instrument"]),({},["RULE_SOURCE_UNAVAILABLE"]))
                price = row.get("price")
                if price: record["peak"] = max(record["peak"],price)
                current = {tf:metric.get(tf,{}).get("rsi") for tf in FRAMES}
                breaks = crosses(record.get("previous_rsi",{}),current)
                if breaks: record["below70_since"] = stamp(start)
                # Missing data does not erase the last known RSI used to detect the next crossing.
                for tf,value in current.items():
                    if value is not None: record.setdefault("previous_rsi",{})[tf] = value
                below = [tf for tf,value in current.items() if value is not None and value<70]
                priority = ("BELOW 70: "+",".join(below)) if below else "TRACKING"
                if breaks: priority = "NEW "+priority
                coin = {**record,"price":price,"day_gain":row.get("day_gain"),"metrics":metric,"coinglass":cg.get(symbol,{}),
                    "since_entry":price/record["entry_price"]-1 if price else None,"from_peak":price/record["peak"]-1 if price else None,
                    "updated_at":row.get("captured_at"),"priority":priority,"gainer_exchanges":ranks.get(symbol,[]),
                    "status":"OK" if len(metric)==3 and not errors and price else "PARTIAL: "+";".join(errors or [row.get("status","MISSING")])}
                snapshot["coins"].append(coin)
                self.store.put("watch",symbol,record)
                self.store.put("observation",key+"|"+symbol,pack({**coin,"epoch":start,"slot":key,"scheduled":info["scheduled"],"day":info["day"]}))
            # Verify every daily-gainer candidate on all six exchanges as well.
            for symbol in sorted(set(watch)|set(ranks)):
                for exchange_name in EXCHANGES:
                    detail = dict(selected[(symbol,exchange_name)])
                    detail["coinglass"] = cg.get(symbol,{})
                    if detail.get("price") and (symbol in watch or exchange_name in ranks.get(symbol,[])):
                        values,detail_errors = computed.get((symbol,exchange_name,detail["market"],detail["symbol"]),(None,None))
                        if values is None: values,detail_errors = self.metrics(exchanges[exchange_name],detail,start)
                        detail["metrics"] = values
                        if detail_errors: detail["error"] = ";".join(detail_errors)
                    snapshot["exchange_rows"].append(detail)
            snapshot["gainers"] = [r for r in snapshot["exchange_rows"] if r.get("rank",999999)<=self.top_n]
            snapshot["sources"]["Candidate evaluation"] = {"verified_universe_coins":len(eligible),"positive_top_gainer_coins":len(ranks),
                "entry_rule_attempts":entry_attempts,"retained_rule_attempts":retained_attempts,
                "active_watchlist_coins":len(watch),"candle_requests":self.calls}
            snapshot["coins"].sort(key=lambda x:("BELOW 70" not in x["priority"],x["symbol"]))
            failures = (not cg or not valid_universe or not baseline or any(e.errors for e in exchanges.values())
                or any(c["status"] != "OK" for c in snapshot["coins"]) or any(r.get("error") for r in snapshot["exchange_rows"]))
            snapshot["status"] = "PARTIAL" if failures else "COMPLETE"
            snapshot["sources"]["Rule calculation"] = "RSI Wilder14 / MFI14; last 100+ contiguous completed UTC exchange candles, request limit200. No Coinglass values used for admission."
            snapshot["sources"]["Scan timing"] = {"lag_seconds":round(start-info["scheduled"]),"duration_seconds":round(time.time()-start),"candle_requests":self.calls}
            snapshot["sources"]["Weekly gainers definition"] = "At least one positive top15 observation on each of 7 IST trading days; 336 valid RSI snapshots required"
            self.save_snapshot(key,snapshot)
            run.update(state=snapshot["status"],finished_at=stamp())
            self.store.put("run",key,run)
            self.deliver(key,snapshot,run)
            return snapshot["status"]
        except Exception as error:
            # Preserve diagnosis without connection URLs or Telegram tokens.
            run.update(state="FAILED",delivery="NOT_SENT",error=type(error).__name__,finished_at=stamp())
            self.store.put("run",key,run)
            snapshot["sources"]["Run failure"] = type(error).__name__
            snapshot["status"] = "FAILED"
            self.save_snapshot(key,snapshot)
            return "FAILED"
        finally:
            self.browser.close()
            self.store.release("scan")
            release_memory()

    def schedule_rows(self, epoch):
        start = int(day_start(epoch))
        rows = []
        for i in range(48):
            key = str(start+i*1800)
            run = self.store.get("run",key,{})
            state = run.get("state","MISSED" if start+i*1800 < epoch-1800 else "PENDING")
            rows.append([i+1,stamp(start+i*1800),state,run.get("delivery", "NOT_SENT")])
        return rows

    def deliver(self, key, snapshot, run):
        file = self.output/("RSI_"+key+".xlsx")
        try:
            make_report(file,snapshot,schedule=self.schedule_rows(snapshot["epoch"]))
            run["delivery"] = "RENDERED"
            self.store.put("run",key,run)
            if self.telegram:
                # Never blindly resend a request whose Telegram acceptance is unknown.
                run["delivery"] = "SENDING"
                self.store.put("run",key,run)
                sent = self.telegram.document(file,"RSI watchlist | "+snapshot["scheduled_ist"]+" | "+snapshot["status"])
                run.update(delivery="DELIVERED",telegram_file_id=sent["document"]["file_id"])
                self.store.put("run",key,run)
        except Exception as error:
            run["delivery"] = "UNCERTAIN" if run.get("delivery")=="SENDING" else "FAILED"
            run["delivery_error"] = type(error).__name__
            self.store.put("run",key,run)
        finally:
            if file.exists(): file.unlink()

    def observations(self, symbol=None, after=0):
        rows = []
        for key,value in self.store.iter_records("observation",after=after,key_suffix="|"+symbol if symbol else None):
            row = unpack(value)
            if row["epoch"] >= after: rows.append(row)
        return sorted(rows,key=lambda r:r["epoch"])

    def weekly(self, now):
        # Seven complete IST trading days, ending at the latest 06:00 baseline.
        end = int(day_start(now))
        expected = set(range(end-336*1800,end,1800))
        summary={symbol:{"valid":set(),"below":set(),"days":set()} for symbol in self.active()}
        # Stream compressed observations; retain only slot IDs and coverage counters.
        for key,value in self.store.iter_records("observation",after=end-336*1800):
            symbol=key.split("|",1)[-1]
            if symbol not in summary: continue
            row=unpack(value)
            slot=row["scheduled"]
            if slot not in expected or row.get("status")!="OK": continue
            metrics=row.get("metrics",{})
            if any(metrics.get(tf,{}).get("rsi") is None for tf in FRAMES): continue
            state=summary[symbol]
            state["valid"].add(slot)
            if any(metrics[tf]["rsi"]<70 for tf in FRAMES): state["below"].add(slot)
            if row.get("gainer_exchanges"): state["days"].add(row["day"])
        result = []
        for symbol,state in summary.items():
            valid,below,days=len(state["valid"]),len(state["below"]),len(state["days"])
            passed=valid==336 and not below and days>=7
            result.append([symbol,"COMPLETE" if valid==336 else "INCOMPLETE",336,valid,below,days,
                "QUALIFIED" if passed else ("INSUFFICIENT_HISTORY" if valid<336 else "NOT_QUALIFIED")])
        return result

    def delete(self, symbols=None):
        symbols = symbols or list(self.active())
        count = 0
        for symbol in symbols:
            record = self.store.get("watch",symbol) or {"symbol":symbol}
            if record.get("active"): count += 1
            record.update(active=False,suppressed=True,removed_at=stamp())
            self.store.put("watch",symbol,record)
        return count

    def command(self,text):
        command,arg = parse_command(text)
        if command in ("help","start"):
            return self.telegram.text("Commands: /daily (latest XLSX), /weeklycoin (7-day coverage), /expand BTC (stored coin history), /status, /del BTC,ETH, /delall. /allow BTC,ETH removes deletion suppression. Automatic XLSX every 30 minutes; no separate trading alerts. All times IST, prices USDT.")
        if command == "status": return self.telegram.text(json.dumps(self.status(),indent=2))
        if command in ("del","delall","allow"):
            symbols = [s.upper() for s in re.split(r"[,\s]+",arg) if re.fullmatch(r"[a-zA-Z0-9]{1,25}",s)]
            if command != "delall" and not symbols: return self.telegram.text("Use /"+command+" BTC,ETH")
            if command == "allow":
                for symbol in symbols:
                    record = self.store.get("watch",symbol) or {"symbol":symbol,"active":False}
                    record["suppressed"] = False
                    self.store.put("watch",symbol,record)
                return self.telegram.text("Admission suppression removed: "+",".join(symbols)+". Normal entry rules still apply.")
            count = self.delete(None if command == "delall" else symbols)
            return self.telegram.text(str(count)+" active coins removed; history retained. Removed coins stay suppressed until /allow.")
        latest = self.snapshot()
        if not latest: return self.telegram.text("No stored snapshot yet. First scan is queued; check /status after a few minutes.")
        if command == "daily":
            key = self.store.get("meta","latest")["key"]
            run = self.store.get("run",key,{})
            if run.get("telegram_file_id"):
                return self.telegram.call("sendDocument",{"chat_id":self.telegram.chat,"document":run["telegram_file_id"],"disable_notification":"true"})
        history = weekly = bars = None
        if command == "weeklycoin":
            weekly = self.weekly(time.time())
            qualified = {r[0] for r in weekly if r[-1]=="QUALIFIED"}
            latest = dict(latest,coins=[c for c in latest["coins"] if c["symbol"] in qualified],
                exchange_rows=[r for r in latest["exchange_rows"] if r["coin"] in qualified],gainers=[])
        elif command == "expand":
            symbol = arg.upper()
            if not re.fullmatch(r"[A-Z0-9]{1,25}",symbol): return self.telegram.text("Use /expand BTC")
            observations = self.observations(symbol)
            if not observations: return self.telegram.text("No stored history for "+symbol)
            latest = dict(latest,coins=[observations[-1]],exchange_rows=[r for r in latest["exchange_rows"] if r["coin"]==symbol])
            history = [[r.get("updated_at"),symbol,r.get("exchange"),r.get("market"),r.get("price"),r.get("day_gain"),
                *[r.get("metrics",{}).get(tf,{}).get("rsi") for tf in FRAMES],
                *[r.get("metrics",{}).get(tf,{}).get("mfi") for tf in FRAMES],r.get("status")] for r in observations]
            bars = [[stamp(b["t"]),b["exchange"],b["market"],b["symbol"],b["frame"],b["o"],b["h"],b["l"],b["c"],b["v"]]
                for _,b in self.store.list("bar") if b["coin"]==symbol]
            bars.sort(key=lambda b:(b[0],b[1],b[4]))
        elif command != "daily": return self.telegram.text("Unknown command. Use /help")
        file = self.output/("Command_"+str(int(time.time()))+".xlsx")
        try:
            make_report(file,latest,schedule=self.schedule_rows(time.time()),history=history,weekly=weekly,bars=bars)
            return self.telegram.document(file,"Stored report | "+command+" | "+stamp())
        finally:
            if file.exists(): file.unlink()
