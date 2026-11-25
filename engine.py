"""Durable, serialized scan engine. Reports describe observations, not predictions."""
import base64
import json
import os
import re
import time
import zlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from domain import EXCHANGES, FRAMES, day_start, slot_info, stamp, entry_pass, crosses, indicators,valid_symbol
from reports import make_report
from sources import Browser, markets
from memory_guard import container_memory,release_memory
from quality import coverage


def pack(value):
    return {"zlib_base64":base64.b64encode(zlib.compress(json.dumps(value,allow_nan=False,separators=(",",":")).encode(),6)).decode()}


def unpack(value):
    return json.loads(zlib.decompress(base64.b64decode(value["zlib_base64"]))) if value else None


def parse_command(text):
    text = text.strip()
    match = re.fullmatch(r"/del\(([^)]+)\)",text,re.I)
    if match: return "del", match[1]
    match = re.fullmatch(r"/expand([^\W_]{1,25})",text,re.I)
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
        self.budget = int(os.getenv("SCAN_BUDGET_SECONDS","900"))
        self.deadline = 0
        self.calls = 0
        self.call_lock=threading.Lock()

    def status(self):
        latest = self.store.get("meta","latest")
        watches = [w for _,w in self.store.list("watch") if w.get("active")]
        return {"watchlist_count":len(watches),"latest":latest,"timezone":"Asia/Kolkata",
            "memory":container_memory(),"last_browser_resources":getattr(self.browser,"diagnostics",{}),
            "rule_rsi":"Exchange completed candles, Wilder14; Coinglass separate live columns",
            "delivery":"48 scheduled slots/day; free host interruptions can cause gaps",
            "scanner_heartbeat":self.store.get("meta","scanner_heartbeat"),"commands_heartbeat":self.store.get("meta","commands_heartbeat"),
            "active_incidents":{k:v.get("message") for k,v in self.store.list("incident") if v.get("active")},
            "backup":self.store.get("meta","backup")}

    def active(self):
        return {k:v for k,v in self.store.list("watch") if v.get("active")}

    def save_snapshot(self, key, snapshot):
        self.store.put("snapshot",key,pack(snapshot))
        self.store.put_latest({"key":key,"captured_at":snapshot["captured_at"],"status":snapshot["status"],"scheduled_ist":snapshot["scheduled_ist"]})

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
                if cache.get("rsi") is not None and cache.get("previous_rsi") is not None: result[frame] = cache
                else: errors.append(frame+":UNDEFINED_RSI")
                continue
            if time.monotonic() >= self.deadline:
                errors.append(frame+":SCAN_BUDGET_EXCEEDED")
                continue
            try:
                with self.call_lock: self.calls += 1
                values = indicators(exchange.candles(row,frame,now),frame,now)
                if values.get("rsi") is None or values.get("previous_rsi") is None: raise ValueError("UNDEFINED_RSI")
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
        if not self.store.lease("scan",120): return "BUSY"
        if self.store.get("run",key):
            self.store.release("scan")
            return "ALREADY_RECORDED"
        lease_stop=threading.Event()
        def renew_lease():
            while not lease_stop.wait(20):
                try: self.store.renew("scan",120)
                except Exception: pass
        lease_thread=threading.Thread(target=renew_lease,daemon=True)
        run = {"scheduled_ist":info["scheduled_ist"],"scheduled":info["scheduled"],"day":info["day"],
            "state":"FETCHING","delivery":"PENDING","started_at":stamp(start)}
        self.store.put("run",key,run)
        self.deadline = time.monotonic()+self.budget
        self.calls = 0
        snapshot = {"scheduled_ist":info["scheduled_ist"],"scan_started_at":stamp(start),"captured_at":stamp(start),"epoch":start,"coins":[],
            "exchange_rows":[],"sources":{},"status":"PARTIAL"}
        try:
            lease_thread.start()
            # Prices first: slow browser refresh must not postpone the 06:00 capture.
            exchanges = self.market_loader()
            if not self.store.renew("scan",120): raise ValueError("SCAN_LEASE_LOST")
            for exchange in exchanges.values(): exchange.deadline=self.deadline
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
            refresh_state=self.store.get("meta","cmc_refresh",{})
            if universe is None or start-universe["epoch"] >= 86400 or info["slot"] == 1 or refresh_state.get("failed"):
                try:
                    universe = self.browser.universe()
                    self.store.put("meta","universe",universe)
                    self.store.put("meta","cmc_refresh",{"failed":False,"attempted_at":stamp(start)})
                except Exception as error:
                    snapshot["sources"]["CMC refresh"] = type(error).__name__+": "+str(error)[:150]
                    self.store.put("meta","cmc_refresh",{"failed":True,"attempted_at":stamp(start),"error":type(error).__name__})
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
            research_tracks={k:v for k,v in self.store.iter_records("research_track",after=start-8*86400)}
            needed.update(v["coin"] for v in research_tracks.values())
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
                if os.getenv("COINGLASS_ENABLED","false").lower()!="true":
                    raise ValueError("DISABLED_OPTIONAL_DISPLAY_SOURCE")
                cg,xhr = self.browser.coinglass()
                snapshot["sources"]["Coinglass"] = str(len(cg))+" displayed RSI rows; live, separate from rule RSI"
                self.store.put("meta","coinglass_xhr",pack(xhr))
                snapshot["sources"]["Coinglass XHR"] = {k:v for k,v in xhr.items() if k != "payload"}
            except Exception as error:
                cg = {}
                snapshot["sources"]["Coinglass"] = "DISABLED: optional display source (COINGLASS_ENABLED=false)" if str(error)=="DISABLED_OPTIONAL_DISPLAY_SOURCE" else "UNAVAILABLE: "+type(error).__name__+" "+str(error)[:120]
                self.browser.close()
            snapshot["sources"]["Browser resources"] = getattr(self.browser,"diagnostics",{})
            eligible = {c["symbol"]:c for c in universe["coins"] if c["symbol"] not in universe.get("ambiguous_symbols",[])} if valid_universe else {}
            if not self.store.renew("scan",120): raise ValueError("SCAN_LEASE_LOST")
            watch = self.active()
            selected, ranks = {}, {}
            all_symbols = set(eligible) | set(watch) | {v["coin"] for v in research_tracks.values()}
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
            # At most three independent venues fetch candles concurrently; each
            # venue stays sequential. Commands run in a different process.
            tasks={name:[] for name in exchanges}
            seen=set()
            retained=sorted(watch)
            offset=int(info["scheduled"]//1800)%max(1,len(retained))
            retained=retained[offset:]+retained[:offset]
            for symbol in retained:
                record=watch[symbol]; name=record["exchange"]
                row=exchanges[name].select(symbol,(record["market"],record["instrument"]))
                if row.get("price"):
                    target=(symbol,name,row["market"],row["symbol"])
                    tasks[name].append((target,dict(row,coin=symbol))); seen.add(target)
            for symbol in retained+sorted(set(ranks)-set(watch)):
                for name in EXCHANGES:
                    row=selected[(symbol,name)]
                    if row.get("price") and (symbol in watch or name in ranks.get(symbol,[])):
                        target=(symbol,name,row["market"],row["symbol"])
                        if target not in seen: tasks[name].append((target,row)); seen.add(target)
            def fetch_venue(name):
                return [(target,self.metrics(exchanges[name],row,start)) for target,row in tasks[name]]
            with ThreadPoolExecutor(max_workers=3,thread_name_prefix="candles") as pool:
                venue_order=list(tasks); offset=int(info["scheduled"]//1800)%len(venue_order)
                for batch in pool.map(fetch_venue,venue_order[offset:]+venue_order[:offset]): computed.update(batch)
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
                    metric,errors = computed.get((symbol,name,row["market"],row["symbol"]),({},["NOT_REQUESTED"]))
                    computed[(symbol,name,row["market"],row["symbol"])] = (metric,errors)
                    if not record and entry_pass(metric):
                        record = {"symbol":symbol,"name":eligible[symbol]["name"],"cmc_id":eligible[symbol]["id"],"active":True,
                            "suppressed":False,"exchange":name,"market":row["market"],"instrument":row["symbol"],
                            "entered_at":stamp(start),"entry_price":row["price"],"peak":row["price"],"previous_rsi":{}}
                        if (self.store.get("watch",symbol) or {}).get("suppressed"): continue
                        if not self.store.put_watch(symbol,record): continue
                        watch[symbol] = record
                        break
            for symbol,record in sorted(watch.items()):
                if not self.store.renew("scan",120): raise ValueError("SCAN_LEASE_LOST")
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
                stale_below=[tf for tf,value in current.items() if value is None and record.get("previous_rsi",{}).get(tf,100)<70]
                priority = ("BELOW 70: "+",".join(below)) if below else "TRACKING"
                if stale_below:
                    priority=(priority if below else "BELOW 70")+" (last valid: "+",".join(stale_below)+")"
                if breaks: priority = "NEW "+priority
                coin = {**record,"price":price,"day_gain":row.get("day_gain"),"metrics":metric,"coinglass":cg.get(symbol,{}),
                    "since_entry":price/record["entry_price"]-1 if price else None,"from_peak":price/record["peak"]-1 if price else None,
                    "updated_at":row.get("captured_at"),"priority":priority,"gainer_exchanges":ranks.get(symbol,[]),
                    "status":"OK" if len(metric)==3 and not errors and price else "PARTIAL: "+";".join(errors or [row.get("status","MISSING")])}
                # A delete issued while this scan was fetching takes precedence.
                if (self.store.get("watch",symbol) or {}).get("suppressed"): continue
                if not self.store.put_watch(symbol,record): continue
                snapshot["coins"].append(coin)
                self.store.put("observation",key+"|"+symbol,pack({**coin,"epoch":start,"slot":key,"scheduled":info["scheduled"],"day":info["day"]}))
            # Verify every daily-gainer candidate on all six exchanges as well.
            for symbol in sorted(set(watch)|set(ranks)):
                for exchange_name in EXCHANGES:
                    detail = dict(selected[(symbol,exchange_name)])
                    detail["coinglass"] = cg.get(symbol,{})
                    if detail.get("price") and (symbol in watch or exchange_name in ranks.get(symbol,[])):
                        values,detail_errors = computed.get((symbol,exchange_name,detail["market"],detail["symbol"]),(None,None))
                        if values is None: values,detail_errors = {},["NOT_REQUESTED"]
                        detail["metrics"] = values
                        if detail_errors: detail["error"] = ";".join(detail_errors)
                        detail["indicator_status"]="VALID" if len(values)==3 and not detail_errors else "INCOMPLETE"
                    else:
                        detail["indicator_status"]=("NOT_REQUESTED_RANK_GT_15" if detail.get("rank",0)>self.top_n else
                            "NOT_REQUESTED_BASELINE_MISSING" if detail.get("day_gain") is None else "NOT_REQUESTED_NOT_POSITIVE_GAINER") if detail.get("price") else detail.get("status","MISSING")
                    snapshot["exchange_rows"].append(detail)
            for coin in snapshot["coins"]:
                coin["coverage"]=coverage([r for r in snapshot["exchange_rows"] if r["coin"]==coin["symbol"]])
                self.store.put("observation",key+"|"+coin["symbol"],pack({**coin,"epoch":start,"slot":key,"scheduled":info["scheduled"],"day":info["day"]}))
            # Prospective candidate history includes failed rules. It is not a
            # reconstruction of old CMC universes or old top-gainer rankings.
            for detail in snapshot["exchange_rows"]:
                if detail.get("rank",999999)<=self.top_n and detail.get("price"):
                    self.store.put("candidate",key+"|"+detail["coin"]+"|"+detail["exchange"],
                        pack({**{k:detail.get(k) for k in ("coin","exchange","market","symbol","price","rank","captured_at","error")},
                            "metrics":{tf:{k:m.get(k) for k in ("rsi","previous_rsi")} for tf,m in detail.get("metrics",{}).items()},
                            "epoch":start,"evaluated_at":stamp(),"scheduled":info["scheduled"],"day":info["day"]}))
                    identity="|".join((detail["coin"],detail["exchange"],detail["market"],detail["symbol"]))
                    track={k:detail[k] for k in ("coin","exchange","market","symbol")}
                    self.store.put("research_track",identity,track)
                    research_tracks[identity]=track
            for identity,track in research_tracks.items():
                row=exchanges[track["exchange"]].select(track["coin"],(track["market"],track["symbol"]))
                if row.get("price"):
                    from datetime import datetime
                    source_epoch=datetime.fromisoformat(row["captured_at"]).timestamp()
                    daily_key=identity+"|"+info["day"]
                    previous=unpack(self.store.get("research_day",daily_key)) or {**track,"samples":{}}
                    previous["samples"][key]=[source_epoch,row["price"]]
                    self.store.put("research_day",daily_key,pack(previous))
            snapshot["gainers"] = [r for r in snapshot["exchange_rows"] if r.get("rank",999999)<=self.top_n]
            snapshot["sources"]["Candidate evaluation"] = {"verified_universe_coins":len(eligible),"positive_top_gainer_coins":len(ranks),
                "entry_rule_attempts":entry_attempts,"retained_rule_attempts":retained_attempts,
                "active_watchlist_coins":len(watch),"candle_requests":self.calls}
            snapshot["coins"].sort(key=lambda x:("BELOW 70" not in x["priority"],x["symbol"]))
            failures = (not valid_universe or not baseline or any(e.errors for e in exchanges.values())
                or any(c["status"] != "OK" for c in snapshot["coins"]) or any(r.get("error") for r in snapshot["exchange_rows"]))
            snapshot["status"] = "PARTIAL" if failures else "COMPLETE"
            snapshot["sources"]["Rule calculation"] = "RSI Wilder14 / MFI14; last 100+ contiguous completed UTC exchange candles, request limit200. No Coinglass values used for admission."
            snapshot["sources"]["Scan timing"] = {"lag_seconds":round(start-info["scheduled"]),"duration_seconds":round(time.time()-start),"candle_requests":self.calls}
            snapshot["sources"]["Weekly gainers definition"] = "At least one positive top15 observation on each of 7 IST trading days; 336 valid RSI snapshots required"
            snapshot["scan_finished_at"]=snapshot["captured_at"]=stamp()
            snapshot["sources"]["Capture timing"]="Ticker source times are per row; scan start and finish are separate. This is not a simultaneous market snapshot."
            self.save_snapshot(key,snapshot)
            run.update(state=snapshot["status"],finished_at=stamp())
            self.store.put("run",key,run)
            if not self.store.renew("scan",120): raise ValueError("SCAN_LEASE_LOST")
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
            lease_stop.set(); lease_thread.join(timeout=2)
            self.browser.close()
            self.store.release("scan")
            release_memory()

    def schedule_rows(self, epoch,as_of=None):
        start = int(day_start(epoch))
        as_of=epoch if as_of is None else as_of
        rows = []
        for i in range(48):
            key = str(start+i*1800)
            run = self.store.get("run",key,{})
            due=start+i*1800
            state = run.get("state","NOT_DUE" if due>as_of else "MISSED" if due<as_of-1800 else "DUE")
            rows.append([i+1,stamp(due),state,run.get("delivery", "NOT_DUE" if due>as_of else "NOT_SENT")])
        return rows

    def deliver(self, key, snapshot, run):
        file = self.output/("RSI_"+key+".xlsx")
        try:
            make_report(file,snapshot,schedule=self.schedule_rows(snapshot["epoch"],as_of=time.time()))
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
            return self.telegram.text("/daily Excel\n/weeklycoin seven-day coverage\n/expand BTC consolidated history\n/coin BTC price and RSI chart\n/top current gainers\n/below70 priority list\n/research prospective returns\n/health delivery summary\n/status system status\n/del BTC,ETH or /delall remove coins\n/allow BTC,ETH permits admission again\n30-minute reports; IST and USDT. System-health alerts only.",buttons=True)
        if command=="health":
            from health import daily_health
            return self.telegram.text(json.dumps(daily_health(self.store),indent=2))
        if command in ("top","below70"):
            latest=self.snapshot() or {}
            if command=="top":
                rows=sorted(latest.get("gainers",[]),key=lambda r:(r["exchange"],r["rank"]))
                lines=[f"{r['exchange']} #{r['rank']} {r['coin']} {r['day_gain']:+.2%}" for r in rows]
            else:
                lines=[f"{c['symbol']} {c['priority']} | {c.get('price')} USDT" for c in latest.get("coins",[]) if "BELOW 70" in c.get("priority","")]
            text=("TOP GAINERS" if command=="top" else "BELOW 70")+"\nSnapshot: "+latest.get("scheduled_ist","none")+"\n"+("\n".join(lines) or "No matching stored observations")
            for i in range(0,len(text),3800): self.telegram.text(text[i:i+3800],buttons=i==0)
            return
        if command=="coin":
            symbol=arg.upper()
            if not valid_symbol(symbol): return self.telegram.text("Use /coin BTC")
            rows=self.observations(symbol,after=time.time()-7*86400)
            if not rows: return self.telegram.text("No stored observations for "+symbol)
            from charts import coin_chart
            chart_rows=[]
            for row in rows:
                if chart_rows and (row["epoch"]-chart_rows[-1]["epoch"]>2100 or any(row.get(k)!=chart_rows[-1].get(k) for k in ("exchange","market","instrument"))):
                    chart_rows.append({"epoch":row["epoch"]-1})
                chart_rows.append(row)
            file=self.output/("chart_"+symbol+".png")
            try:
                coin_chart(chart_rows,symbol,file)
                return self.telegram.photo(file,symbol+" | last seven observed days | IST | USDT | gaps stay blank")
            finally: file.unlink(missing_ok=True)
        if command=="research":
            from research import build
            latest=self.snapshot() or {"coins":[],"sources":{}}
            file=self.output/("Research_"+str(time.time_ns())+".xlsx")
            try:
                make_report(file,latest,research=build(self.store))
                return self.telegram.document(file,"Prospective research; missing and immature outcomes remain explicit")
            finally: file.unlink(missing_ok=True)
        if command == "status":
            state=self.status(); latest=state.get("latest") or {}
            lines=["WATCHLIST SYSTEM (IST)","Retained coins: "+str(state["watchlist_count"]),
                "Latest slot: "+latest.get("scheduled_ist","none"),"Data: "+latest.get("status","not scanned")]
            for role in ("scanner","commands"):
                hb=state.get(role+"_heartbeat") or {}
                lines.append(role.capitalize()+" heartbeat: "+(str(max(0,int(time.time()-hb["epoch"])))+"s ago" if hb.get("epoch") else "not recorded"))
            lines.append("Coinglass: "+("optional enabled" if os.getenv("COINGLASS_ENABLED","false").lower()=="true" else "optional disabled"))
            lines.append("External backup: "+(stamp(state["backup"]["epoch"]) if state.get("backup") else "not recorded"))
            lines.append("System issues:")
            lines.extend(state["active_incidents"].values() or ["No active incidents recorded"])
            text="\n".join(lines)
            for i in range(0,len(text),3800): self.telegram.text(text[i:i+3800],buttons=i==0)
            return
        if command in ("del","delall","allow"):
            symbols = [s.upper() for s in re.split(r"[,\s]+",arg) if valid_symbol(s)]
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
            if not valid_symbol(symbol): return self.telegram.text("Use /expand BTC")
            observations = self.observations(symbol,after=time.time()-30*86400)
            if not observations: return self.telegram.text("No stored history for "+symbol)
            latest = dict(latest,coins=[observations[-1]],exchange_rows=[r for r in latest["exchange_rows"] if r["coin"]==symbol])
            history = [[r.get("updated_at"),symbol,r.get("exchange"),r.get("market"),r.get("price"),r.get("day_gain"),
                *[r.get("metrics",{}).get(tf,{}).get("rsi") for tf in FRAMES],
                *[r.get("metrics",{}).get(tf,{}).get("mfi") for tf in FRAMES],r.get("status")] for r in observations]
            bars = [[stamp(b["t"]),b["exchange"],b["market"],b["symbol"],b["frame"],b["o"],b["h"],b["l"],b["c"],b["v"]]
                for _,b in self.store.iter_records("bar",coin=symbol,after=time.time()-30*86400,limit=20000)]
            bars.sort(key=lambda b:(b[0],b[1],b[4]))
        elif command != "daily": return self.telegram.text("Unknown command. Use /help")
        file = self.output/("Command_"+str(time.time_ns())+".xlsx")
        try:
            make_report(file,latest,schedule=self.schedule_rows(latest.get("epoch",time.time()),as_of=time.time()),history=history,weekly=weekly,bars=bars)
            return self.telegram.document(file,"Stored report | "+command+" | "+stamp())
        finally:
            if file.exists(): file.unlink()
