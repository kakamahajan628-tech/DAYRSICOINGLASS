"""Public endpoint preflight. No bot messages, database writes or admission."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from domain import EXCHANGES,FRAMES,stamp,indicators
from sources import Browser,Exchange

def check(name):
    exchange=Exchange(name).refresh(); now=time.time(); exchange.deadline=time.monotonic()+120
    row=exchange.select("BTC")
    result={"exchange":name,"catalogs":{k:len(v) for k,v in exchange.catalog.items()},"errors":exchange.errors,"selected_status":row["status"],"market":row.get("market"),"frames":{}}
    if row.get("price"):
        for tf in FRAMES:
            try:
                values=indicators(exchange.candles(row,tf,now),tf,now)
                result["frames"][tf]={"status":"PASS","rsi":values["rsi"],"mfi":values["mfi"],"history_bars":values["history_bars"],"bar_close_ist":stamp(values["bar_close"])}
            except Exception as error:
                result["frames"][tf]={"status":"UNAVAILABLE","reason":str(error)[:100] if isinstance(error,ValueError) else type(error).__name__}
    return result

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output"); args=parser.parse_args()
    start=time.time()
    with ThreadPoolExecutor(max_workers=3) as pool: exchanges=list(pool.map(check,EXCHANGES))
    try:
        universe=Browser().universe(); cmc={"status":"PASS","coins":len(universe["coins"]),"verification":universe["verification"],"captured_at":universe["captured_at"]}
    except Exception as error: cmc={"status":"UNAVAILABLE","reason":str(error)[:150] if isinstance(error,ValueError) else type(error).__name__}
    result={"checked_at":stamp(),"duration_seconds":round(time.time()-start,1),"location":"Machine running this command; not evidence of access from another hosting region", "exchanges":exchanges,"cmc":cmc,"coinglass":"Optional, not launched by preflight"}
    if args.output:
        path=Path(args.output); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(result,indent=2,allow_nan=False),encoding="utf-8")
    print(json.dumps({"checked_at":result["checked_at"],"duration_seconds":result["duration_seconds"],"valid_BTC_timeframes":sum(v.get("status")=="PASS" for e in exchanges for v in e["frames"].values()),"cmc":cmc}))

if __name__=="__main__": main()
