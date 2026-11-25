"""Independent scanner and command workers. PostgreSQL coordinates restarts."""
import logging
import os
import signal
import sys
import threading
import time
import uuid
from domain import stamp,slot_info
from engine import Engine
from storage import Store
from telegram_api import Telegram
from health import Health

LOG=logging.getLogger("watchlist")

def recover_interrupted(store,health):
    stale=store.inflight_runs(time.time()-2*86400)
    if not stale or not store.lease("scan",60): return
    try:
        for key,run in stale:
            if run.get("state")=="FETCHING": run.update(state="INTERRUPTED",finished_at=stamp())
            run["delivery"]="UNCERTAIN" if run.get("delivery")=="SENDING" else "NOT_SENT"
            store.put("run",key,run)
        health.incident("interrupted",True,str(len(stale))+" interrupted scan/delivery records retained; no automatic duplicate send")
    finally: store.release("scan")

def maintenance(engine,health):
    from contracts import run_contracts
    health.daily()
    for result in run_contracts(engine.store):
        for market,status in result["markets"].items():
            health.incident("contract_"+result["exchange"]+"_"+market,status!="PASS",result["exchange"]+" "+market+" daily contract: "+status)
    store=engine.store; now=time.time()
    # Full coin observations and admission/deletion history are preserved.
    # Bounded high-volume caches have documented retention.
    for kind,days in (("snapshot",7),("candidate",30),("research_day",30),("research_price",30),("research_track",8),("bar",30),("fixture",7)):
        store.prune(kind,now-days*86400)
    store.prune_jobs(now-14*86400)
    backup=store.get("meta","backup",{})
    health.incident("backup",not backup or now-backup.get("epoch",0)>8*86400,"No verified external database backup recorded in the last 8 days")
    return "DAILY_CHECKS_COMPLETE"

def execute(engine,health,job,role):
    store=engine.store; kind=job["kind"]
    if kind=="scan":
        info=job["payload"]; run=store.get("run",info["key"])
        if run and run.get("state")=="FETCHING":
            # Only mark interrupted after owning the expired scan lease.
            if not store.lease("scan",60): return "BUSY"
            try:
                run.update(state="INTERRUPTED",delivery="UNCERTAIN" if run.get("delivery")=="SENDING" else "NOT_SENT",finished_at=stamp())
                store.put("run",info["key"],run)
            finally: store.release("scan")
            health.incident("interrupted",True,"Previous scan was interrupted at "+info["scheduled_ist"])
            return "INTERRUPTED"
        if info["key"]!=slot_info(time.time())["key"] and not run:
            store.put("run",info["key"],{"state":"MISSED","delivery":"NOT_SENT","scheduled":info["scheduled"],"scheduled_ist":info["scheduled_ist"],"day":info["day"]})
            return "MISSED_NO_BACKFILL"
        result=engine.scan(info)
        health.scan(engine,info["key"])
        return result
    if kind=="command":
        if job.get("reclaimed"):
            health.incident("command_uncertain",True,"Interrupted Telegram command retained as uncertain; request the report again")
            return "UNCERTAIN_NO_AUTOMATIC_RESEND"
        engine.command(job["payload"])
        return "COMMAND_COMPLETE"
    return maintenance(engine,health)

def main(role=None):
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    role=role or sys.argv[1]; stop=threading.Event(); owner=uuid.uuid4().hex
    signal.signal(signal.SIGTERM,lambda *args:stop.set()); signal.signal(signal.SIGINT,lambda *args:stop.set())
    while not stop.is_set():
        store=None
        try:
            from app import config_errors
            errors=config_errors(os.environ)
            if errors: LOG.error("Worker config invalid: %s","; ".join(errors)); stop.wait(30); continue
            store=Store(os.environ["DATABASE_URL"]); bot=Telegram(os.environ["TELEGRAM_BOT_TOKEN"],os.environ["TELEGRAM_CHAT_ID"])
            engine=Engine(store,bot); health=Health(store,bot)
            heartbeat_stop=threading.Event()
            def heartbeat():
                while not heartbeat_stop.is_set():
                    try: store.put("meta",role+"_heartbeat",{"epoch":time.time(),"owner":owner})
                    except Exception: pass
                    heartbeat_stop.wait(15)
            thread=threading.Thread(target=heartbeat,daemon=True); thread.start()
            try:
                last_recovery=0
                while not stop.is_set():
                    if role=="scanner" and time.monotonic()-last_recovery>=30:
                        recover_interrupted(store,health); last_recovery=time.monotonic()
                    kinds=("scan",) if role=="scanner" else ("command","maintenance")
                    job=None
                    for kind in kinds:
                        job=store.claim(kind,owner,engine.budget+300 if kind=="scan" else 1800)
                        if job: break
                    if not job: stop.wait(1); continue
                    try:
                        result=execute(engine,health,job,role)
                        # BUSY is safe to retry after this claim expires; never
                        # acknowledge a scan that another deployment still owns.
                        if result!="BUSY": store.finish_job(job["id"],owner,result)
                        else: store.requeue_job(job["id"],owner); stop.wait(5)
                        LOG.info("%s job result: %s",role,result)
                    except Exception as error:
                        store.finish_job(job["id"],owner,type(error).__name__,"FAILED")
                        health.incident("worker_"+role,True,role+" job failed: "+type(error).__name__)
                        LOG.error("%s job failed: %s",role,type(error).__name__)
            finally:
                heartbeat_stop.set(); thread.join(timeout=2); engine.browser.close()
        except Exception as error: LOG.error("%s worker unavailable: %s",role,type(error).__name__); stop.wait(10)
        finally:
            if store: store.close()

if __name__=="__main__": main()
