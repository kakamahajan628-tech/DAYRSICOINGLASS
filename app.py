"""Light HTTP/Telegram frontend. Scanning and commands use durable DB jobs."""
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from domain import slot_info,day_start,valid_symbol
from engine import Engine
from storage import Store
from telegram_api import Telegram

LOG=logging.getLogger("watchlist")

def config_errors(env):
    errors=[name+" missing" for name in ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","RUN_SECRET","DATABASE_URL") if not env.get(name)]
    if env.get("RUN_SECRET") and len(env["RUN_SECRET"])<24: errors.append("RUN_SECRET needs at least 24 characters")
    if env.get("TELEGRAM_CHAT_ID","").startswith("-") and not env.get("TELEGRAM_OWNER_ID"): errors.append("TELEGRAM_OWNER_ID required for group chats")
    db=env.get("DATABASE_URL","")
    if db.startswith("sqlite") and env.get("ALLOW_EPHEMERAL_DB")!="true": errors.append("SQLite disabled for deployment; use durable PostgreSQL")
    if db and not db.startswith(("postgres://","postgresql://","postgresql+psycopg://","sqlite:///")): errors.append("Unsupported DATABASE_URL scheme")
    for name,default,low,high in (("TOP_N","15",1,15),("SCAN_BUDGET_SECONDS","900",60,1200)):
        try:
            if not low<=int(env.get(name,default))<=high: raise ValueError()
        except ValueError: errors.append(name+" out of range")
    if env.get("DASHBOARD_PASSWORD") and len(env["DASHBOARD_PASSWORD"])<12: errors.append("DASHBOARD_PASSWORD needs at least 12 characters")
    if env.get("COINGLASS_ENABLED","false").lower() not in ("true","false"): errors.append("COINGLASS_ENABLED must be true or false")
    return errors

def authorized_message(message,chat,owner):
    return str(message.get("chat",{}).get("id"))==str(chat) and str(message.get("from",{}).get("id"))==str(owner) and not message.get("from",{}).get("is_bot",False)

class Service:
    def __init__(self):
        self.stop=threading.Event(); self.engine=None; self.errors=config_errors(os.environ)
        self.secret=os.getenv("RUN_SECRET",""); self.password=os.getenv("DASHBOARD_PASSWORD","")
        self.started=time.time(); self.bot_state="STARTING"

    def enqueue(self,job,job_id=None):
        if not self.engine: return False
        kind,payload=job
        job_id=job_id or ("scan:"+payload["key"] if kind=="scan" else "command:"+secrets.token_hex(12))
        return self.engine.store.enqueue(job_id,kind,payload)

    def initialize(self):
        while not self.stop.is_set():
            self.errors=config_errors(os.environ)
            if self.errors: LOG.error("Configuration invalid: %s","; ".join(self.errors)); return
            try:
                store=Store(os.environ["DATABASE_URL"])
                self.engine=Engine(store,Telegram(os.environ["TELEGRAM_BOT_TOKEN"],os.environ["TELEGRAM_CHAT_ID"]))
                threading.Thread(target=self.poll,daemon=True,name="telegram").start()
                self.scheduler(); return
            except Exception as error:
                self.engine=None; LOG.error("Frontend initialization: %s",type(error).__name__); self.stop.wait(15)

    def scheduler(self):
        while not self.stop.is_set():
            try:
                store=self.engine.store; now=time.time(); info=slot_info(now)
                if not store.get("run",info["key"]): self.enqueue(("scan",info))
                if now-day_start(now)>=300: self.enqueue(("maintenance",{"day":info["day"]}),"maintenance:"+info["day"])
                store.put("meta","frontend_heartbeat",{"epoch":now,"telegram":self.bot_state})
                if now-self.started>120:
                    from health import Health
                    health=Health(store,self.engine.telegram)
                    for role in ("scanner","commands"):
                        heartbeat=store.get("meta",role+"_heartbeat",{})
                        health.incident(role+"_heartbeat",now-heartbeat.get("epoch",0)>90,role+" heartbeat is stale")
            except Exception as error: LOG.error("Scheduler: %s",type(error).__name__)
            self.stop.wait(5)

    def poll(self):
        store=self.engine.store; bot=self.engine.telegram
        chat=os.environ["TELEGRAM_CHAT_ID"]; owner=os.getenv("TELEGRAM_OWNER_ID") or chat; setup=False
        while not self.stop.is_set():
            try:
                if not (store.renew("telegram-poller",70) or store.lease("telegram-poller",70)):
                    self.bot_state="STANDBY"; self.stop.wait(10); continue
                if not setup:
                    if bot.call("getWebhookInfo").get("url"):
                        self.bot_state="WEBHOOK_CONFLICT"; self.stop.wait(30); continue
                    names=("daily","weeklycoin","expand","coin","top","below70","research","health","status","del","delall","allow","help")
                    bot.call("setMyCommands",{"commands":json.dumps([{"command":n,"description":n+" report or command"} for n in names])}); setup=True
                updates=bot.call("getUpdates",{"offset":store.get("meta","telegram_offset",0),"timeout":25,"allowed_updates":'["message","callback_query"]'})
                self.bot_state="POLLING"
                for item in updates:
                    callback=item.get("callback_query"); message=item.get("message",{})
                    if callback: message={"chat":callback.get("message",{}).get("chat",{}),"from":callback.get("from",{}),"text":"/"+callback.get("data","")}
                    text=message.get("text","")
                    if authorized_message(message,chat,owner) and text.startswith("/"):
                        self.enqueue(("command",text),"telegram:"+str(item["update_id"]))
                        if callback: bot.call("answerCallbackQuery",{"callback_query_id":callback["id"],"text":"Queued"})
                    store.put("meta","telegram_offset",int(item["update_id"])+1)
            except Exception as error:
                self.bot_state="UNAVAILABLE: "+type(error).__name__; LOG.error("Telegram: %s",type(error).__name__); self.stop.wait(10)
        store.release("telegram-poller")

    def status(self):
        result={"ready":self.engine is not None,"config":self.errors,"telegram":self.bot_state,"uptime_seconds":int(time.time()-self.started)}
        if self.engine:
            try:
                result.update(self.engine.status()); store=self.engine.store
                result.update(scanner=store.get("meta","scanner_heartbeat"),commands=store.get("meta","commands_heartbeat"),incidents=dict(store.list("incident")),daily_health=store.get("meta","daily_health"),contracts=store.get("meta","contracts"),backup=store.get("meta","backup"))
            except Exception as error: result["database"]=type(error).__name__
        return result

    def ready(self):
        if not self.engine: return False
        try:
            return all(time.time()-self.engine.store.get("meta",role+"_heartbeat",{}).get("epoch",0)<90 for role in ("scanner","commands"))
        except Exception: return False

def handler_for(service):
    failures={}; lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(15)

        def log_message(self,*args): pass

        def respond(self,status,body,mime="application/json; charset=utf-8",headers=None):
            body=json.dumps(body,allow_nan=False).encode() if isinstance(body,(dict,list)) else body.encode() if isinstance(body,str) else body
            self.send_response(status); self.send_header("Content-Type",mime); self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Referrer-Policy","same-origin"); self.send_header("X-Frame-Options","DENY")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for key,value in (headers or {}).items(): self.send_header(key,value)
            self.end_headers()
            if self.command!="HEAD": self.wfile.write(body)

        def send(self,status,payload): return self.respond(status,payload)

        def permitted(self):
            return bool(service.secret) and hmac.compare_digest(self.headers.get("Authorization","").encode(),("Bearer "+service.secret).encode())

        def signature(self,value):
            return hmac.new((service.secret+getattr(service,"password","")).encode(),value.encode(),hashlib.sha256).hexdigest()

        def session(self):
            if not getattr(service,"password",""): return False
            try:
                token=next(c.strip().split("=",1)[1] for c in self.headers.get("Cookie","").split(";") if c.strip().startswith("watch_session="))
                epoch,nonce,signature=token.split("."); value=epoch+"."+nonce
                return 0<=time.time()-int(epoch)<28800 and hmac.compare_digest(signature,self.signature(value))
            except (StopIteration,ValueError): return False

        def same_origin(self):
            origin=self.headers.get("Origin")
            return not origin or urlsplit(origin).netloc==self.headers.get("Host")

        def do_HEAD(self): self.do_GET()

        def do_GET(self):
            parsed=urlsplit(self.path); path=parsed.path
            if path=="/health": return self.send(200,{"status":"alive"})
            if path=="/ready":
                ready=service.ready() if hasattr(service,"ready") else service.engine is not None
                return self.send(200 if ready else 503,{"ready":ready})
            if path=="/status": return self.send(200,service.status()) if self.permitted() else self.send(401,{"error":"Unauthorized"})
            if path in ("/","/login") and not self.session():
                if not getattr(service,"password",""): return self.respond(503,"Dashboard disabled. Set DASHBOARD_PASSWORD (at least 12 characters). Telegram continues independently.","text/plain; charset=utf-8")
                return self.respond(200,"<!doctype html><html><meta name='viewport' content='width=device-width'><title>Watchlist sign in</title><body style='font:16px Arial;max-width:420px;margin:15vh auto;padding:24px'><h1>Market watchlist</h1><p>Owner sign in</p><form method='post' action='/login'><label>Password <input type='password' name='password' autocomplete='current-password' required></label><button>Sign in</button></form></body></html>","text/html; charset=utf-8")
            if path in ("/","/dashboard.js"):
                if not self.session(): return self.send(401,{"error":"Unauthorized"})
                name="dashboard.html" if path=="/" else "dashboard.js"
                return self.respond(200,(Path(__file__).parent/"web"/name).read_bytes(),"text/html; charset=utf-8" if path=="/" else "text/javascript; charset=utf-8")
            if path=="/login" and self.session(): return self.respond(303,b"",headers={"Location":"/"})
            if path.startswith("/api/"):
                if not self.session(): return self.send(401,{"error":"Unauthorized"})
                if not service.engine: return self.send(503,{"error":"Not ready"})
                if path=="/api/watchlist":
                    latest=service.engine.snapshot() or {}
                    return self.send(200,{"scheduled_ist":latest.get("scheduled_ist"),"scan_finished_at":latest.get("scan_finished_at"),"status":latest.get("status"),"coins":latest.get("coins",[]),"sources":latest.get("sources",{})})
                if path=="/api/history":
                    import re
                    symbol=parse_qs(parsed.query).get("coin",[""])[0].upper()
                    if not valid_symbol(symbol): return self.send(400,{"error":"Invalid coin"})
                    rows=service.engine.observations(symbol,after=time.time()-7*86400)
                    return self.send(200,[{k:r.get(k) for k in ("epoch","price","metrics","exchange","market","status")} for r in rows[-336:]])
                if path=="/api/health": return self.send(200,service.status())
            return self.send(404,{"error":"Not found"})

        def do_POST(self):
            path=urlsplit(self.path).path
            if path=="/run":
                if not self.permitted(): return self.send(401,{"error":"Unauthorized"})
                if not service.engine: return self.send(503,{"error":"Not ready"})
                info=slot_info(time.time())
                if service.engine.store.get("run",info["key"]): return self.send(409,{"error":"Current slot already recorded"})
                service.enqueue(("scan",info)); return self.send(202,{"queued":info["scheduled_ist"]})
            if not self.same_origin(): return self.send(403,{"error":"Origin rejected"})
            if path=="/logout": return self.respond(303,b"",headers={"Location":"/login","Set-Cookie":"watch_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict; Secure"})
            if path!="/login": return self.send(404,{"error":"Not found"})
            password=getattr(service,"password","")
            if not password: return self.send(503,{"error":"Dashboard disabled"})
            address=self.client_address[0]; now=time.time()
            with lock:
                if len(failures)>1024: failures.clear()
                count,until=failures.get(address,(0,now+900))
                if now>until: count,until=0,now+900
                if count>=5: return self.send(429,{"error":"Try again after 15 minutes"})
            try: length=int(self.headers.get("Content-Length","0"))
            except ValueError: return self.send(400,{"error":"Invalid length"})
            if not 0<length<=2048: return self.send(413,{"error":"Invalid request size"})
            supplied=parse_qs(self.rfile.read(length).decode("utf-8",errors="replace")).get("password",[""])[0]
            if not hmac.compare_digest(supplied.encode(),password.encode()):
                with lock: failures[address]=(count+1,until)
                return self.send(401,{"error":"Invalid password"})
            with lock: failures.pop(address,None)
            value=str(int(now))+"."+secrets.token_hex(16); token=value+"."+self.signature(value)
            local=os.getenv("ALLOW_INSECURE_LOCAL")=="true" and self.client_address[0] in ("127.0.0.1","::1")
            cookie="watch_session="+token+"; Path=/; Max-Age=28800; HttpOnly; SameSite=Strict"+("" if local else "; Secure")
            return self.respond(303,b"",headers={"Location":"/","Set-Cookie":cookie})
    return Handler

def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    service=Service(); server=ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","10000"))),handler_for(service))
    threading.Thread(target=service.initialize,daemon=True).start()
    def stop(*args):
        service.stop.set(); threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    try: server.serve_forever(poll_interval=.5)
    finally: service.stop.set(); server.server_close()

if __name__=="__main__": main()
