"""One web service: health endpoint, scheduler, owner-only Telegram and one worker."""
import hmac
import json
import logging
import os
import queue
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from domain import slot_info
from engine import Engine
from storage import Store
from telegram_api import Telegram

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("watchlist")


def config_errors(env):
    errors = [name+" missing" for name in ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","RUN_SECRET","DATABASE_URL") if not env.get(name)]
    if env.get("RUN_SECRET") and len(env["RUN_SECRET"])<24: errors.append("RUN_SECRET needs at least24 characters")
    chat = env.get("TELEGRAM_CHAT_ID","")
    if chat.startswith("-") and not env.get("TELEGRAM_OWNER_ID"): errors.append("TELEGRAM_OWNER_ID required for group chats")
    db = env.get("DATABASE_URL","")
    if db.startswith("sqlite") and env.get("ALLOW_EPHEMERAL_DB") != "true": errors.append("SQLite disabled for deployment; use durable PostgreSQL")
    if db and not db.startswith(("postgres://","postgresql://","postgresql+psycopg://","sqlite:///")): errors.append("Unsupported DATABASE_URL scheme")
    for name,default,minimum,maximum in (("TOP_N","15",1,15),("SCAN_BUDGET_SECONDS","1200",60,1200)):
        try:
            if not minimum<=int(env.get(name,default))<=maximum: raise ValueError()
        except ValueError: errors.append(name+" out of range")
    return errors


def authorized_message(message,chat,owner):
    return (str(message.get("chat",{}).get("id"))==str(chat)
        and str(message.get("from",{}).get("id"))==str(owner)
        and not message.get("from",{}).get("is_bot",False))


class Service:
    def __init__(self):
        self.stop = threading.Event()
        self.jobs = queue.PriorityQueue(maxsize=64)
        self.engine = None
        self.errors = config_errors(os.environ)
        self.last_scheduler_key = None
        self.processing = None
        self.bot_state = "CONFIG_PENDING"
        self.started = time.time()
        self.secret = os.getenv("RUN_SECRET","")

    def enqueue(self,job):
        if not self.engine: return False
        try:
            self.jobs.put_nowait((0 if job[0]=="scan" else 1,time.monotonic_ns(),job))
            return True
        except queue.Full:
            return False

    def initialize(self):
        while not self.stop.is_set():
            if self.errors:
                LOG.error("Configuration: %s", "; ".join(self.errors))
                return
            try:
                store = Store(os.environ["DATABASE_URL"])
                self.engine = Engine(store,Telegram(os.environ["TELEGRAM_BOT_TOKEN"],os.environ["TELEGRAM_CHAT_ID"]))
                self.errors = []
                # A previous process can have delivered before losing its response.
                for key,run in store.list("run"):
                    changed = False
                    if run.get("delivery")=="SENDING": run["delivery"]="UNCERTAIN"; changed=True
                    if run.get("state")=="FETCHING": run["state"]="INTERRUPTED"; changed=True
                    if changed: store.put("run",key,run)
                LOG.info("Durable store initialized")
                threading.Thread(target=self.poll,name="telegram",daemon=True).start()
                threading.Thread(target=self.scheduler,name="scheduler",daemon=True).start()
                self.worker()
                return
            except Exception as error:
                self.engine = None
                self.errors = ["Database initialization unavailable: "+type(error).__name__]
                LOG.error("Database initialization unavailable: %s",type(error).__name__)
                self.stop.wait(30)
                # Database outages are retried. Static invalid config needs redeploy.
                self.errors = config_errors(os.environ)

    def scheduler(self):
        while not self.stop.is_set():
            try:
                info = slot_info(time.time())
                if self.last_scheduler_key != info["key"]:
                    if self.engine.store.get("run",info["key"]) or self.enqueue(("scan",info)):
                        self.last_scheduler_key = info["key"]
            except Exception as error:
                LOG.error("Scheduler unavailable: %s",type(error).__name__)
            self.stop.wait(1)

    def worker(self):
        try:
            while not self.stop.is_set():
                try:
                    _,_,(job,payload) = self.jobs.get(timeout=1)
                except queue.Empty:
                    continue
                self.processing = job
                try:
                    if job == "scan":
                        result = self.engine.scan(payload)
                        LOG.info("Scan %s: %s",payload["key"],result)
                    else: self.engine.command(payload)
                except Exception as error:
                    LOG.error("Worker %s failed: %s",job,type(error).__name__)
                    if job == "command":
                        try: self.engine.telegram.text("Command could not complete. Check /status or try again later.")
                        except Exception: pass
                finally:
                    self.processing = None
                    self.jobs.task_done()
        finally:
            self.engine.browser.close()
            self.engine.store.close()

    def poll(self):
        bot = self.engine.telegram
        try:
            if bot.call("getWebhookInfo").get("url"):
                self.bot_state = "WEBHOOK_CONFLICT: use a dedicated bot without a webhook"
                LOG.error("Telegram existing webhook; polling disabled")
                return
            commands = [{"command":name,"description":description} for name,description in (
                ("daily","Latest Excel report"),("weeklycoin","Seven-day observed coverage"),("expand","Stored coin history, e.g. /expand BTC"),
                ("status","Scanner and source status"),("del","Remove coins, e.g. /del BTC,ETH"),("delall","Remove all active coins"),
                ("allow","Permit removed coin admission again"),("help","Command reference"))]
            bot.call("setMyCommands",{"commands":json.dumps(commands)})
        except Exception as error:
            LOG.error("Telegram setup unavailable: %s",type(error).__name__)
        chat = os.environ["TELEGRAM_CHAT_ID"]
        owner = os.getenv("TELEGRAM_OWNER_ID") or chat
        while not self.stop.is_set():
            try:
                offset = self.engine.store.get("meta","telegram_offset",0)
                updates = bot.call("getUpdates",{"offset":offset,"timeout":25,"allowed_updates":'["message"]'})
                self.bot_state = "POLLING"
                for update in updates:
                    message = update.get("message",{})
                    text = message.get("text","")
                    if authorized_message(message,chat,owner) and text.startswith("/"):
                        if not self.enqueue(("command",text)): break
                    self.engine.store.put("meta","telegram_offset",int(update["update_id"])+1)
            except Exception as error:
                self.bot_state = "UNAVAILABLE: "+type(error).__name__
                LOG.error("Telegram polling unavailable: %s",type(error).__name__)
                self.stop.wait(10)

    def status(self):
        result = {"ready":self.engine is not None,"config":self.errors,"processing":self.processing,
            "queued":self.jobs.qsize(),"telegram":self.bot_state,"uptime_seconds":int(time.time()-self.started)}
        if self.engine:
            try: result.update(self.engine.status())
            except Exception as error: result["database"] = type(error).__name__
        return result


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,format,*args):
            # No URL/query/header logging: credentials must not enter logs.
            pass

        def send(self,status,payload):
            body = json.dumps(payload,allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            self.wfile.write(body)

        def permitted(self):
            token = self.headers.get("Authorization","")
            return bool(service.secret) and hmac.compare_digest(token,"Bearer "+service.secret)

        def do_GET(self):
            path = self.path.split("?",1)[0]
            if path == "/health": return self.send(200,{"status":"alive"})
            if path == "/ready": return self.send(200 if service.engine else 503,{"ready":service.engine is not None})
            if path == "/":
                body = b"<!doctype html><html><meta name='viewport' content='width=device-width'><title>Crypto Watchlist</title><body style='font-family:Arial;max-width:650px;margin:10vh auto;padding:24px'><h1>Crypto Watchlist</h1><p>Telegram reports run every 30 minutes. All report times are IST.</p><p><a href='/health'>Service health</a> &middot; <a href='/ready'>Configuration readiness</a></p><p>Source quality and scanner state are available through your owner-only Telegram commands.</p></body></html>"
                self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            if path == "/status":
                return self.send(200,service.status()) if self.permitted() else self.send(401,{"error":"Unauthorized"})
            return self.send(404,{"error":"Not found"})

        def do_POST(self):
            if self.path != "/run": return self.send(404,{"error":"Not found"})
            if not self.permitted(): return self.send(401,{"error":"Unauthorized"})
            if not service.engine: return self.send(503,{"error":"Not ready"})
            info = slot_info(time.time())
            if service.engine.store.get("run",info["key"]): return self.send(409,{"error":"Current slot already recorded; use /daily for stored report"})
            if not service.enqueue(("scan",info)): return self.send(503,{"error":"Queue full"})
            return self.send(202,{"queued":info["scheduled_ist"]})
    return Handler


def main():
    service = Service()
    server = ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","10000"))),handler_for(service))
    threading.Thread(target=service.initialize,name="engine",daemon=True).start()
    def shutdown(signum,frame):
        service.stop.set()
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown)
    signal.signal(signal.SIGINT,shutdown)
    LOG.info("Web service listening on port %s",server.server_port)
    server.serve_forever(poll_interval=0.5)
    server.server_close()


if __name__ == "__main__": main()
