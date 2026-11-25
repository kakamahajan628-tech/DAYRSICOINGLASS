"""System-health incidents only. No price or trading notifications."""
import os
import time
from domain import day_start,stamp


def daily_health(store,now=None):
    now=now or time.time(); end=int(day_start(now)); start=end-86400
    counts={"scheduled":48,"delivered":0,"uncertain":0,"failed":0,"missed":0,"coins_valid":0,"coins_total":0}
    for slot in range(start,end,1800):
        run=store.get("run",str(slot),{})
        state=run.get("delivery")
        if state=="DELIVERED": counts["delivered"]+=1
        elif state=="UNCERTAIN": counts["uncertain"]+=1
        elif state in ("FAILED","NOT_SENT") or run.get("state")=="FAILED": counts["failed"]+=1
        else: counts["missed"]+=1
    from engine import unpack
    for _,value in store.iter_records("observation",after=start):
        row=unpack(value)
        if start<=row.get("scheduled",0)<end:
            counts["coins_total"]+=1; counts["coins_valid"]+=row.get("status")=="OK"
    return {"start_ist":stamp(start),"end_ist":stamp(end),**counts}


class Health:
    def __init__(self,store,bot): self.store,self.bot=store,bot

    def incident(self,key,active,message):
        old=self.store.get("incident",key,{})
        changed=bool(old.get("active"))!=bool(active)
        state={"active":bool(active),"message":message,"checked_at":stamp(),"notified_at":old.get("notified_at",0)}
        # Persist before sending: a crash never causes an alert flood. An
        # uncertain send is visible in /status; it isn't retried blindly.
        notify=changed and (not active or time.time()-old.get("notified_at",0)>1800)
        if notify: state["notified_at"]=time.time()
        self.store.put("incident",key,state)
        if notify and self.bot and os.getenv("SYSTEM_ALERTS_ENABLED","true").lower()=="true":
            try: self.bot.text(("SYSTEM ISSUE: " if active else "SYSTEM RECOVERED: ")+message)
            except Exception: state["notification"]="UNCERTAIN"; self.store.put("incident",key,state)

    def scan(self,engine,key):
        snapshot=engine.snapshot(key) or {}; run=self.store.get("run",key,{})
        self.incident("scan_failed",snapshot.get("status")=="FAILED","Scan "+key+" failed; check /status")
        self.incident("delivery",run.get("delivery") in ("FAILED","UNCERTAIN"),"Report delivery "+run.get("delivery","unknown")+" at "+snapshot.get("scheduled_ist",key))
        sources=snapshot.get("sources",{})
        admission=sources.get("Admission decision","")
        self.incident("admission",admission.startswith("PAUSED"),"Admission "+admission)
        self.incident("empty_scan",not snapshot.get("coins") and not snapshot.get("gainers") and snapshot.get("status")=="PARTIAL","No evaluated watchlist or gainer data at "+snapshot.get("scheduled_ist",key))
        for name in ("Binance","Bybit","OKX","Bitget","Gate","KuCoin"):
            data=sources.get(name,{})
            errors=data.get("errors",{}) if isinstance(data,dict) else {}
            self.incident("source_"+name,bool(errors),name+" source errors: "+", ".join(k+"="+v for k,v in errors.items()))
        if os.getenv("COINGLASS_ENABLED","false").lower()=="true":
            cg=str(sources.get("Coinglass",""))
            self.incident("coinglass",cg.startswith("UNAVAILABLE"),cg)

    def daily(self,now=None):
        now=now or time.time(); report=daily_health(self.store,now)
        key=report["end_ist"]
        if self.store.get("meta","daily_health_sent")==key: return
        self.store.put("meta","daily_health",report)
        self.store.put("meta","daily_health_sent",key)
        if self.bot and os.getenv("SYSTEM_ALERTS_ENABLED","true").lower()=="true":
            self.bot.text(f"DAILY SYSTEM HEALTH\n{report['start_ist']} to {key}\nDelivered {report['delivered']}/48; uncertain {report['uncertain']}; failed {report['failed']}; missed {report['missed']}.\nValid coin observations {report['coins_valid']}/{report['coins_total']}.\nNo trading alerts.")
        expiry=os.getenv("DATABASE_EXPIRES_AT","")
        if expiry:
            from datetime import datetime
            try:
                remaining=(datetime.fromisoformat(expiry).timestamp()-now)/86400
                self.incident("database_expiry",remaining<7,"Database expires in "+str(round(remaining,1))+" days")
            except ValueError: self.incident("database_expiry_config",True,"DATABASE_EXPIRES_AT must be an ISO timestamp with timezone")
