import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from domain import IST, FRAMES, EXCHANGES, stamp, day_start, slot_info, entry_pass, crosses, rsi, mfi, indicators
from sources import Exchange, candle, cmc_rows, utc_days_from_4h
from storage import Store
from engine import Engine, pack, parse_command
from reports import make_report
from app import config_errors, authorized_message, handler_for, ThreadingHTTPServer


def metrics(value=75,previous=74):
    return {tf:{"rsi":value,"previous_rsi":previous,"mfi":80} for tf in FRAMES}


class RuleTests(unittest.TestCase):
    def test_48_slots_and_ist_day_boundary(self):
        start = datetime(2026,11,12,6,tzinfo=IST).timestamp()
        slots = [slot_info(start+i*1800) for i in range(48)]
        self.assertEqual([s["slot"] for s in slots],list(range(1,49)))
        self.assertEqual(len({s["key"] for s in slots}),48)
        self.assertEqual(slot_info(start-1)["day"],"2026-11-11")
        self.assertEqual(slot_info(start+86400)["slot"],1)

    def test_all_three_must_increase(self):
        self.assertTrue(entry_pass(metrics()))
        for tf in FRAMES:
            for bad in ({"rsi":69,"previous_rsi":68},{"rsi":75,"previous_rsi":75},{"rsi":float("nan"),"previous_rsi":70},{}):
                values=metrics(); values[tf]=bad
                self.assertFalse(entry_pass(values))
        self.assertEqual(crosses({"1h":70,"4h":69},{"1h":69,"4h":68}),["1h"])

    def test_indicators_known_vectors(self):
        self.assertEqual(rsi(list(range(1,120))),100)
        self.assertEqual(rsi(list(range(120,1,-1))),0)
        self.assertIsNone(rsi([100]*120))
        closes=[44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28]
        self.assertAlmostEqual(rsi(closes),70.46413502109705,places=9)
        flows=[candle(i*3600,p,p+1,p-1,p,1) for i,p in enumerate((10,11,10,12))]
        self.assertAlmostEqual(mfi(flows,3),100-100/(1+23/10),places=9)
        rows = [candle(i*3600,100+i,102+i,99+i,101+i,10) for i in range(120)]
        self.assertEqual(mfi(rows),100)
        result=indicators(rows,"1h",120*3600)
        self.assertEqual(result["bar_close"],120*3600)
        # A still-forming candle cannot affect RSI.
        self.assertEqual(indicators(rows+[candle(120*3600,220,230,1,2,999999)],"1h",120*3600+1)["rsi"],100)
        with self.assertRaisesRegex(ValueError,"CANDLE_GAP"):
            indicators(rows[:80]+rows[81:],"1h",120*3600)
        with self.assertRaisesRegex(ValueError,"STALE"):
            indicators(rows[:-1],"1h",120*3600)

    def test_command_aliases(self):
        self.assertEqual(parse_command("/EXPANDBTC"),("expand","BTC"))
        self.assertEqual(parse_command("/DEL(BTC,ETH)"),("del","BTC,ETH"))
        self.assertEqual(parse_command("/weeklycoin@MyBot"),("weeklycoin",""))


class SourceTests(unittest.TestCase):
    def test_futures_error_does_not_fall_back_to_spot(self):
        exchange=Exchange("Binance")
        exchange.catalog["spot"]["BTC"]={"symbol":"BTCUSDT","price":100}
        exchange.errors["futures"]="HTTP_403"
        self.assertEqual(exchange.select("BTC")["status"],"ACCESS_ERROR")
        exchange.errors.clear()
        self.assertEqual(exchange.select("BTC")["symbol"],"BTCUSDT")
        self.assertEqual(exchange.select("XYZ")["status"],"NOT_LISTED")
        exchange.catalog["futures"]["BTC"]={"symbol":"BTCUSDT","price":None,"status":"TICKER_MISSING"}
        self.assertEqual(exchange.select("BTC")["status"],"TICKER_MISSING")

    def test_adapter_units_and_field_order(self):
        raw=[1700000000000,"10","12","9","11","50","550"]
        expected=candle(*raw[:6])
        for name in ("Binance","Bybit","Bitget","OKX","Gate","KuCoin"):
            exchange=Exchange(name)
            row={"market":"futures","symbol":"TEST","factor":0.01}
            if name in ("Binance","Bitget"): response=[raw]
            elif name=="Bybit": response={"result":{"list":[raw]}}
            elif name=="OKX": response={"data":[raw[:5]+["5000","50","550","1"]]}
            elif name=="Gate": response=[dict(t=1700000000,o="10",h="12",l="9",c="11",v=5000)]
            else: response={"data":[raw[:5]+[5000,550]]}
            if name=="Bitget": response={"data":response}
            with patch.object(exchange,"get",return_value=response):
                self.assertEqual(exchange.candles(row,"1h",1700000100),[expected],name)
        for name in ("Gate","KuCoin"):
            exchange=Exchange(name)
            response=[[1700000000,"550","11","12","9","10","50"]] if name=="Gate" else {"data":[[1700000000,"10","11","12","9","50","550"]]}
            with patch.object(exchange,"get",return_value=response):
                self.assertEqual(exchange.candles({"market":"spot","symbol":"TEST"},"1h",1700000100),[expected])

    def test_utc_daily_aggregation_requires_six_bars(self):
        rows=[candle(i*14400,10+i,12+i,9+i,11+i,50) for i in range(7)]
        daily=utc_days_from_4h(rows)
        self.assertEqual(len(daily),1)
        self.assertEqual(daily[0],candle(0,10,17,9,16,300))
        self.assertEqual(utc_days_from_4h(rows[1:]),[])

    def test_cmc_unknown_schema_fails_and_stock_tags_excluded(self):
        with self.assertRaisesRegex(ValueError,"SCHEMA"):
            cmc_rows({"data":{"list":[]}})
        coin={"id":1,"symbol":"BTC","name":"Bitcoin","quotes":[{"name":"USD","volume24h":100}]}
        stock={**coin,"id":2,"tags":["tokenized-stock"]}
        self.assertEqual(len(cmc_rows({"data":{"cryptoCurrencyList":[coin,stock]}})),1)


class StoreTests(unittest.TestCase):
    def test_restart_and_single_lease(self):
        with tempfile.TemporaryDirectory() as folder:
            url="sqlite:///"+str(Path(folder)/"db.sqlite")
            first=Store(url); second=Store(url)
            first.put("watch","BTC",{"active":True})
            self.assertTrue(first.lease("scan"))
            self.assertFalse(second.lease("scan"))
            first.release("scan")
            self.assertTrue(second.lease("scan"))
            first.close(); second.close()
            third=Store(url)
            self.assertTrue(third.get("watch","BTC")["active"])
            third.close()


class FakeBrowser:
    def universe(self):
        return {"coins":[{"id":1,"symbol":"BTC","name":"Bitcoin"},{"id":1027,"symbol":"ETH","name":"Ethereum"}],
            "ambiguous_symbols":[],"epoch":__import__("time").time(),"captured_at":stamp()}
    def coinglass(self): return {c:{"rsi_1h":80,"rsi_4h":81,"rsi_1d":82} for c in ("BTC","ETH")},{"data_type":"str"}
    def close(self): pass


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{"DATA_DIR":self.temp.name})
        self.env.start()
        self.store=Store("sqlite:///"+str(Path(self.temp.name)/"db.sqlite"))
        self.now=datetime(2026,11,12,6,tzinfo=IST).timestamp()
        self.price=100
        self.values=metrics()
        self.engine=Engine(self.store,browser=FakeBrowser(),market_loader=self.loader)
        self.engine.metrics=lambda *args:(deepcopy(self.values),[])
    def tearDown(self):
        self.store.close(); self.env.stop(); self.temp.cleanup()
    def loader(self):
        result={}
        for name in EXCHANGES:
            exchange=Exchange(name)
            exchange.observed={"futures":self.now,"spot":self.now}
            for symbol in ("BTC","ETH"):
                exchange.catalog["futures"][symbol]={"coin":symbol,"exchange":name,"market":"futures","symbol":symbol+"USDT",
                    "price":self.price,"turnover":1000,"status":"OK","captured_at":stamp(self.now)}
            result[name]=exchange
        return result
    def scan(self):
        with patch("engine.time.time",return_value=self.now):
            return self.engine.scan(slot_info(self.now))
    def test_admission_decline_retention_and_manual_suppression(self):
        self.assertEqual(self.scan(),"COMPLETE")
        self.assertEqual(self.engine.active(),{}) # 06:00 gain is zero.
        self.now+=1800; self.price=110
        self.assertEqual(self.scan(),"COMPLETE")
        self.assertEqual(set(self.engine.active()),{"BTC","ETH"})
        self.assertEqual(self.scan(),"ALREADY_RECORDED")
        self.now+=1800; self.price=80; self.values=metrics(69,75)
        self.scan()
        snapshot=self.engine.snapshot()
        self.assertEqual(len(snapshot["coins"]),2)
        self.assertTrue(all("BELOW 70" in c["priority"] for c in snapshot["coins"]))
        self.assertAlmostEqual(snapshot["coins"][0]["from_peak"],80/110-1)
        self.assertEqual(self.engine.delete(["BTC"]),1)
        self.now+=1800; self.price=120; self.values=metrics(80,79)
        self.scan()
        self.assertNotIn("BTC",self.engine.active())
        self.assertEqual(len(self.engine.observations("BTC")),2)
    def test_missed_baseline_never_uses_24h_change(self):
        self.now+=5*3600
        self.assertEqual(self.scan(),"PARTIAL")
        self.assertEqual(self.engine.active(),{})
        self.assertIn("MISSING",self.engine.snapshot()["sources"]["06:00 baseline"])
    def test_delivery_uncertainty_is_durable(self):
        class Bot:
            def document(self,*args): raise TimeoutError()
        self.engine.telegram=Bot()
        self.scan()
        run=self.store.get("run",slot_info(self.now)["key"])
        self.assertEqual(run["delivery"],"UNCERTAIN")
        self.assertEqual(self.scan(),"ALREADY_RECORDED")
    def test_pinned_spot_does_not_borrow_new_futures_metrics(self):
        original=self.loader
        def loader():
            result=original()
            for exchange in result.values():
                exchange.catalog["spot"]["BTC"]={**exchange.catalog["futures"]["BTC"],"market":"spot","price":self.price-.5}
            return result
        self.engine.market_loader=loader
        self.scan()
        self.store.put("watch","BTC",{"symbol":"BTC","active":True,"exchange":"Binance","market":"spot","instrument":"BTCUSDT",
            "entry_price":99.5,"peak":99.5,"entered_at":stamp(self.now),"previous_rsi":{"1h":75}})
        self.engine.metrics=lambda exchange,row,now:(metrics(65,75) if row["market"]=="spot" else metrics(80,79),[])
        self.now+=1800; self.price=110
        self.scan()
        current=self.engine.snapshot()
        btc=next(c for c in current["coins"] if c["symbol"]=="BTC")
        detail=next(r for r in current["exchange_rows"] if r["coin"]=="BTC" and r["exchange"]=="Binance")
        self.assertEqual(btc["metrics"]["1h"]["rsi"],65)
        self.assertEqual(btc["market"],"spot")
        self.assertAlmostEqual(btc["day_gain"],109.5/99.5-1)
        self.assertEqual(detail["market"],"futures")
        self.assertEqual(detail["metrics"]["1h"]["rsi"],80)
    def test_weekly_requires_every_slot(self):
        end=self.now
        self.store.put("watch","BTC",{"active":True})
        for i in range(336):
            epoch=end-(336-i)*1800
            row={"symbol":"BTC","epoch":epoch,"scheduled":epoch,"status":"OK","metrics":metrics(),
                "gainer_exchanges":["Binance"],"day":slot_info(epoch)["day"]}
            self.store.put("observation",str(int(epoch))+"|BTC",pack(row))
        self.assertEqual(self.engine.weekly(end)[0][-1],"QUALIFIED")
        row["metrics"]["4h"]["rsi"]=69
        self.store.put("observation",str(int(epoch))+"|BTC",pack(row))
        self.assertEqual(self.engine.weekly(end)[0][-1],"NOT_QUALIFIED")
        row["status"]="PARTIAL"
        self.store.put("observation",str(int(epoch))+"|BTC",pack(row))
        self.assertEqual(self.engine.weekly(end)[0][-1],"INSUFFICIENT_HISTORY")


class DeploymentTests(unittest.TestCase):
    def test_owner_auth_and_durable_config(self):
        msg={"chat":{"id":-123},"from":{"id":99},"text":"/delall"}
        self.assertTrue(authorized_message(msg,-123,99))
        self.assertFalse(authorized_message(msg,-123,100))
        self.assertFalse(authorized_message(msg,123,99))
        env=dict(TELEGRAM_BOT_TOKEN="test",TELEGRAM_CHAT_ID="-123",RUN_SECRET="x"*32,DATABASE_URL="sqlite:///x")
        self.assertEqual(len(config_errors(env)),2)
        env.update(TELEGRAM_OWNER_ID="99",DATABASE_URL="postgresql://test")
        self.assertEqual(config_errors(env),[])
    def test_health_is_public_and_status_private(self):
        class Service:
            secret="x"*32
            engine=None
            def status(self): return {"test":"ok"}
        server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(Service()))
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        base="http://127.0.0.1:"+str(server.server_port)
        try:
            self.assertEqual(json.load(urlopen(base+"/health")),{"status":"alive"})
            for path,code in (("/status",401),("/ready",503)):
                with self.assertRaises(HTTPError) as caught: urlopen(base+path)
                self.assertEqual(caught.exception.code,code)
            request=Request(base+"/status",headers={"Authorization":"Bearer "+Service.secret})
            self.assertEqual(json.load(urlopen(request)),{"test":"ok"})
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_xlsx_tables_dates_and_no_formula_injection(self):
        with tempfile.TemporaryDirectory() as folder:
            time="2026-11-12T06:30:00+05:30"
            snapshot={"captured_at":time,"scheduled_ist":time,"status":"FIXTURE_ONLY","coins":[{"symbol":"BTC","name":"=BAD()",
                "price":123.45,"metrics":metrics(),"entered_at":time,"updated_at":time}],"sources":{"test":"Fixture only"}}
            file=make_report(Path(folder)/"report.xlsx",snapshot,schedule=[[1,time,"COMPLETE","DELIVERED"]])
            with zipfile.ZipFile(file) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(len([n for n in archive.namelist() if n.startswith("xl/tables/table")]),6)
                ns={"s":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                sheet=ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                self.assertFalse(sheet.findall(".//s:f",ns))
                cell=sheet.find(".//s:c[@r='W8']",ns)
                self.assertNotEqual(cell.get("t"),"s")
                self.assertGreater(float(cell.find("s:v",ns).text),45000)
                self.assertEqual(sheet.find(".//s:pane",ns).get("ySplit"),"7")


if __name__=="__main__": unittest.main()
