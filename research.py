"""Prospective, source-matched research. No orders, fees or profit promises."""
import argparse
import bisect
import csv
import json
import os
import statistics
import time
from datetime import datetime
from domain import FRAMES, stamp,day_start
from storage import Store

HORIZONS={"1h":3600,"4h":14400,"1d":86400,"7d":604800}


def identity(row):
    return tuple(row.get(k) for k in ("coin","exchange","market","symbol"))


def qualifies(row,threshold,top):
    metrics=row.get("metrics",{})
    return row.get("rank",999)>0 and row.get("rank",999)<=top and all(
        metrics.get(tf,{}).get("rsi") is not None and metrics[tf].get("previous_rsi") is not None
        and threshold<=metrics[tf]["rsi"]<=100 and metrics[tf]["rsi"]>metrics[tf]["previous_rsi"] for tf in FRAMES)


def outcomes(entry,prices,now,tolerance=600):
    """First actual ticker time >= horizon, at most ten minutes late.

    No interpolation or last-value carry forward; old quote times are rejected.
    MAE/MFE use observed 30-minute ticker samples, not intrabar extrema.
    """
    origin=entry["time"]
    rows=sorted((p for p in prices if p["time"]>=origin and p["time"]<=now),key=lambda p:p["time"])
    times=[p["time"] for p in rows]
    answer={}
    for name,seconds in HORIZONS.items():
        target=origin+seconds
        state="NOT_MATURE" if now<target else "MISSING_COVERAGE"
        item={"state":state,"return":None,"observed_at":None,"lag_seconds":None,"mae":None,"mfe":None,"max_drawdown":None}
        i=bisect.bisect_left(times,target)
        if now>=target and i<len(rows) and rows[i]["time"]-target<=tolerance:
            sample=rows[i]
            path=[entry["price"]]+[p["price"] for p in rows[:i+1]]
            peak=path[0]; worst=0
            for price in path:
                peak=max(peak,price); worst=min(worst,price/peak-1)
            item.update(state="OBSERVED",return_=sample["price"]/entry["price"]-1)
            item["return"]=item.pop("return_")
            item.update(observed_at=stamp(sample["time"]),lag_seconds=round(sample["time"]-target),
                mae=min(path)/entry["price"]-1,mfe=max(path)/entry["price"]-1,max_drawdown=worst,
                path_samples=len(path)-1,path_expected=int((sample["time"]-origin)//1800),
                path_complete=all(b-a<=2100 for a,b in zip([origin]+times[:i],times[:i+1])))
        answer[name]=item
    return answer


def build(store,now=None,days=21):
    from engine import unpack
    now=now or time.time()
    after=now-days*86400
    # Compress each prior day's weekly evidence into two 48-bit masks.
    # No full-history candle dictionaries are held in RAM.
    evidence={}
    for _,value in store.iter_records("observation",after=after-7*86400):
        row=unpack(value)
        if row.get("status")!="OK" or row.get("scheduled") is None: continue
        values=[row.get("metrics",{}).get(tf,{}).get("rsi") for tf in FRAMES]
        if any(v is None for v in values): continue
        day=int(day_start(row["scheduled"]))
        key=(row["symbol"],row.get("exchange"),row.get("market"),row.get("instrument"),day)
        state=evidence.setdefault(key,[0,0,False]); bit=1<<int((row["scheduled"]-day)//1800)
        state[0]|=bit
        if all(v>=70 for v in values): state[1]|=bit
        if row.get("gainer_exchanges"): state[2]=True
    variants=[{"name":f"RSI{threshold}_TOP{top}"+("_WEEKLY" if weekly else ""),"threshold":threshold,"top":top,"weekly":weekly,"signals":{},"weekly_unknown":0,"pending":0}
        for threshold,top,weekly in ((70,15,False),(75,15,False),(70,5,False),(75,5,False),(70,15,True))]
    for _,value in store.iter_records("candidate",after=after):
        row=unpack(value)
        if not after<=row["epoch"]<=now or not row.get("evaluated_at"): continue
        row["time"]=datetime.fromisoformat(row["evaluated_at"]).timestamp()
        for variant in variants:
            key=identity(row)
            if key in variant["signals"] or not qualifies(row,variant["threshold"],variant["top"]): continue
            if variant["weekly"]:
                end=int(day_start(row["scheduled"]))
                previous=[evidence.get((*key,day)) for day in range(end-7*86400,end,86400)]
                full=(1<<48)-1
                if any(not p or p[0]!=full for p in previous): variant["weekly_unknown"]+=1; continue
                if any(p[1]!=full or not p[2] for p in previous): continue
            variant["signals"][key]=row
    grouped={}
    for variant in variants:
        variant["entries"]=[]
        for key,row in variant["signals"].items(): grouped.setdefault(key,[]).append((variant,row))
    result=[]; summary=[]
    for key,signals in grouped.items():
        prices=[]
        prefix="|".join(key)+"|"
        for _,value in store.iter_records("research_day",after=after,key_prefix=prefix):
            daily=unpack(value)
            prices.extend({"time":sample[0],"price":sample[1]} for sample in daily["samples"].values() if after<=sample[0]<=now)
        # Compatibility for records from an earlier v2 development snapshot.
        for _,value in store.iter_records("research_price",after=after,key_suffix="|"+"|".join(key)):
            row=unpack(value)
            if row.get("source_time"): prices.append({"time":datetime.fromisoformat(row["source_time"]).timestamp(),"price":row["price"]})
        prices.sort(key=lambda p:p["time"])
        times=[p["time"] for p in prices]
        for variant,row in signals:
            i=bisect.bisect_left(times,row["time"])
            if i==len(prices) or prices[i]["time"]>min(now,row["time"]+2100): variant["pending"]+=1; continue
            reference=prices[i]
            entry={"variant":variant["name"],"coin":row["coin"],"exchange":row["exchange"],"market":row["market"],
                "instrument":row["symbol"],"time":reference["time"],"entered_at":stamp(reference["time"]),"signal_at":stamp(row["time"]),"price":reference["price"]}
            entry["outcomes"]=outcomes(entry,prices,now)
            variant["entries"].append(entry); result.append(entry)
    for variant in variants:
        entries=variant["entries"]
        for horizon in HORIZONS:
            matched=[e["outcomes"][horizon]["return"] for e in entries if e["outcomes"][horizon]["state"]=="OBSERVED"]
            summary.append({"variant":variant["name"],"horizon":horizon,"entries":len(entries),"observed":len(matched),
                "not_mature":sum(e["outcomes"][horizon]["state"]=="NOT_MATURE" for e in entries),
                "missing":sum(e["outcomes"][horizon]["state"]=="MISSING_COVERAGE" for e in entries),
                "mean_return":statistics.mean(matched) if matched else None,
                "median_return":statistics.median(matched) if matched else None,"weekly_unknown_evaluations":variant["weekly_unknown"],
                "entry_sample_missing":variant["pending"]})
    return {"generated_at":stamp(now),"window_days":days,"summary":summary,"entries":result,
        "method":"First qualifying observed candidate per coin/exchange/instrument per variant in this window. Entry reference is first actual ticker AFTER recorded rule evaluation, within 35 minutes. Source-matched USDT returns; 10-minute horizon tolerance. MAE/MFE/drawdown are sampled, incomplete paths flagged. Overlapping cohorts; no fees/funding/slippage. Prospective research, not an execution backtest."}


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output",default="research.json")
    args=parser.parse_args()
    store=Store(os.environ["DATABASE_URL"])
    try:
        result=build(store)
        with open(args.output,"w",encoding="utf-8") as handle: json.dump(result,handle,indent=2,allow_nan=False)
    finally: store.close()

if __name__=="__main__": main()
