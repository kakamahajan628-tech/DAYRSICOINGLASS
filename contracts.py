"""Daily read-only source contracts with dated real-response fixtures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from domain import EXCHANGES,FRAMES,stamp
from sources import Exchange,BASES,Browser


def check_exchange(name,recorder=None):
    exchange=Exchange(name); original=exchange.get
    requests=[]
    def record(market,path,**params):
        payload=original(market,path,**params)
        sample={"market":market,"path":path,"params":params,"response":payload}
        requests.append(sample)
        return payload
    exchange.get=record
    result={"exchange":name,"checked_at":stamp(),"markets":{},"schemas":{}}
    for market in ("futures","spot"):
        requests.clear()
        try:
            catalog=exchange.load(market)
            if not catalog: raise ValueError("EMPTY_USDT_CATALOG")
            row=catalog.get("BTC")
            if not row: raise ValueError("BTC_INSTRUMENT_MISSING")
            bars=exchange.candles(row,"1h",time.time())
            if name=="Bitget":
                daily=exchange.candles(row,"1d",time.time())
                if len(daily)<100: raise ValueError("BITGET_DAILY_HISTORY_TOO_SHORT")
            if not bars: raise ValueError("EMPTY_KLINE")
            if any(not all(k in b for k in ("t","o","h","l","c","v")) for b in bars): raise ValueError("KLINE_SCHEMA_CHANGED")
            result["markets"][market]="PASS"
            result["schemas"][market]={"catalog_size":len(catalog),"parsed_bars":len(bars)}
        except Exception as error:
            reason=str(error)[:100] if isinstance(error,ValueError) else type(error).__name__
            result["markets"][market]=("ACCESS_UNAVAILABLE: " if reason.startswith(("HTTP_403","HTTP_451","HTTP_429","NETWORK_")) else "FAIL: ")+reason
        if recorder:
            # Only public payloads are recorded, never request headers or URLs
            # with credentials. Keep BTC records, retaining real field shapes.
            selected=[]
            def trim(value):
                if isinstance(value,list):
                    if all(not isinstance(x,(dict,list)) for x in value): return value
                    btc=[x for x in value if isinstance(x,dict) and any("BTC" in str(x.get(k,"")) or "XBT" in str(x.get(k,"")) for k in ("symbol","instId","name","contract","currency_pair","id"))]
                    def exact_btc(row):
                        return any(str(row.get(k,"")) in ("BTCUSDT","XBTUSDTM","BTC-USDT","BTC-USDT-SWAP","BTC_USDT") for k in ("symbol","instId","name","contract","currency_pair","id"))
                    btc.sort(key=lambda x:not exact_btc(x))
                    return [trim(x) for x in (btc[:2] if btc else value[:3])]
                if isinstance(value,dict): return {k:trim(v) for k,v in value.items()}
                return value
            for request in requests: selected.append({**request,"response":trim(request["response"])})
            recorder(name,market,{"recorded_at":stamp(),"source_bases":BASES[name],"requests":selected,"result":result["markets"][market]})
    return result


def run_contracts(store,fixture_dir=None):
    def record(name,market,payload):
        store.put("fixture",stamp()[:10]+"|"+name+"|"+market,payload)
        if fixture_dir:
            root=Path(fixture_dir); root.mkdir(parents=True,exist_ok=True)
            (root/(name.lower()+"_"+market+".json")).write_text(json.dumps(payload,indent=2),encoding="utf-8")
    results=[check_exchange(name,record) for name in EXCHANGES]
    try:
        universe=Browser().universe()
        cmc={"exchange":"CMC","checked_at":stamp(),"markets":{"listing":"PASS"},"schemas":{"coins":len(universe["coins"])}}
    except Exception as error:
        reason=str(error)[:100] if isinstance(error,ValueError) else type(error).__name__
        cmc={"exchange":"CMC","checked_at":stamp(),"markets":{"listing":"UNAVAILABLE: "+reason},"schemas":{}}
    results.append(cmc)
    store.put("meta","contracts",{"checked_at":stamp(),"results":results})
    return results

def main():
    from storage import Store
    parser=argparse.ArgumentParser(); parser.add_argument("--record",default=None); args=parser.parse_args()
    store=Store(os.environ["DATABASE_URL"])
    try: print(json.dumps(run_contracts(store,args.record),indent=2))
    finally: store.close()

if __name__=="__main__": main()
