import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch,MagicMock
from urllib.request import Request,urlopen,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
import test_release as legacy
from app import Service,handler_for,ThreadingHTTPServer
from domain import FRAMES,EXCHANGES,slot_info,stamp,indicators
from engine import Engine,pack
from storage import Store
from sources import Exchange,candle,cg_header_indexes,Browser
from quality import coverage,source_rows
from research import outcomes,build
from health import Health,daily_health
from worker import execute,recover_interrupted

class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); self.url="sqlite:///"+str(Path(self.folder.name)/"db.sqlite")
        self.a=Store(self.url); self.b=Store(self.url)
    def tearDown(self): self.a.close(); self.b.close(); self.folder.cleanup()
    def test_jobs_deduplicate_and_require_claim_owner(self):
        self.assertTrue(self.a.enqueue("scan:1","scan",{"key":"1"}))
        self.assertFalse(self.b.enqueue("scan:1","scan",{}))
        job=self.a.claim("scan","a",60)
        self.assertFalse(job["reclaimed"]); self.assertIsNone(self.b.claim("scan","b",60))
        self.assertFalse(self.b.finish_job(job["id"],"b","bad"))
        self.assertTrue(self.a.finish_job(job["id"],"a","done"))
        self.assertIsNone(self.a.claim("scan","a",60))
    def test_expired_claim_recovery(self):
        self.a.enqueue("cmd:1","command","/daily")
        self.a.claim("command","a",-1)
        job=self.b.claim("command","b",60)
        self.assertTrue(job["reclaimed"])
        self.assertFalse(self.a.finish_job("cmd:1","a","stale"))
    def test_stale_owner_cannot_release_successor_lease(self):
        self.assertTrue(self.a.lease("scan",-1)); self.assertTrue(self.b.lease("scan",60))
        self.a.release("scan")
        self.assertFalse(self.a.lease("scan",60)); self.assertTrue(self.b.renew("scan",60))
    def test_scan_write_cannot_revive_deleted_coin(self):
        self.a.put("watch","BTC",{"active":True})
        self.b.put("watch","BTC",{"active":False,"suppressed":True})
        self.assertFalse(self.a.put_watch("BTC",{"active":True}))
        self.assertFalse(self.a.get("watch","BTC")["active"])
    def test_bar_query_filters_in_database(self):
        self.a.put("bar","first",{"coin":"BTC","c":100})
        self.a.put("bar","second",{"coin":"ETH","c":20})
        self.assertEqual(list(self.a.iter_records("bar",coin="BTC")),[("first",{"coin":"BTC","c":100})])
    def test_interrupted_command_never_resends(self):
        engine=MagicMock(); engine.store=self.a; health=MagicMock()
        result=execute(engine,health,{"kind":"command","payload":"/daily","reclaimed":True},"commands")
        self.assertIn("UNCERTAIN",result); engine.command.assert_not_called()
    def test_recovery_does_not_interrupt_an_active_scanner(self):
        info=slot_info(time.time()); self.a.put("run",info["key"],{"state":"FETCHING"})
        self.a.lease("scan",60)
        engine=MagicMock(); engine.store=self.b
        self.assertEqual(execute(engine,MagicMock(),{"kind":"scan","payload":info},"scanner"),"BUSY")
        self.assertEqual(self.a.get("run",info["key"])["state"],"FETCHING")
    def test_daily_ledger_has_exactly_48_slots(self):
        now=legacy.datetime(2026,11,24,6,tzinfo=legacy.IST).timestamp()
        info=slot_info(now-86400)
        self.a.put("run",info["key"],{"delivery":"DELIVERED","state":"PARTIAL"})
        report=daily_health(self.a,now)
        self.assertEqual(report["delivered"],1); self.assertEqual(report["missed"],47)
    def test_old_scan_cannot_replace_newer_latest_pointer(self):
        self.a.put_latest({"key":"150","status":"COMPLETE"})
        self.b.put_latest({"key":"200","status":"COMPLETE"})
        self.b.put_latest({"key":"100","status":"FAILED"})
        self.assertEqual(self.a.get("meta","latest")["key"],"200")
    def test_restart_preserves_uncertain_delivery_without_resend(self):
        self.a.put("run","old",{"state":"COMPLETE","delivery":"SENDING"})
        bot=MagicMock(); recover_interrupted(self.a,Health(self.a,bot))
        self.assertEqual(self.a.get("run","old")["delivery"],"UNCERTAIN")
    def test_health_deduplicates_and_reports_recovery(self):
        bot=MagicMock(); health=Health(self.a,bot)
        health.incident("source",True,"failed"); health.incident("source",True,"failed again")
        health.incident("source",False,"recovered")
        self.assertEqual(bot.text.call_count,2)
        self.assertFalse(self.a.get("incident","source")["active"])

class SourceQualityTests(unittest.TestCase):
    def test_complete_cmc_inventory_is_resorted_without_trusting_source_rank(self):
        rows=[{"id":i,"symbol":"C"+str(i),"name":"Coin "+str(i),"quotes":[{"name":"USD","volume24h":10000-i}]} for i in range(1,801)]
        rows[10],rows[400]=rows[400],rows[10]
        payload={"data":{"totalCount":"800","cryptoCurrencyList":rows}}
        with patch("sources.public_json",return_value=payload): universe=Browser().complete_universe()
        self.assertEqual([r["id"] for r in universe["coins"]],list(range(1,501)))
        self.assertEqual(universe["verification"]["mode"],"COMPLETE_INVENTORY")
    def test_incomplete_cmc_inventory_is_never_claimed_complete(self):
        rows=[{"id":i,"symbol":"C"+str(i),"name":"Coin "+str(i),"quotes":[{"name":"USD","volume24h":10000-i}]} for i in range(1,801)]
        for total,items in ((801,rows),(800,rows[:-1]+[rows[0]])):
            with patch("sources.public_json",return_value={"data":{"totalCount":str(total),"cryptoCurrencyList":items}}):
                with self.assertRaisesRegex(ValueError,"INVENTORY_NOT_VERIFIED"): Browser().complete_universe()
    def test_header_mapping_survives_column_reordering(self):
        self.assertEqual(cg_header_indexes(["Coin","RSI 1D","1H","4H"]),[2,3,1])
        self.assertEqual(cg_header_indexes(["1H","4H","24H"]),[0,1,2])
        with self.assertRaisesRegex(ValueError,"SCHEMA_CHANGED"): cg_header_indexes(["1H","4H","12H"])
    def test_coverage_counts_missing_and_unrequested(self):
        rows=[{"exchange":"OKX","metrics":legacy.metrics()},{"exchange":"Gate","metrics":{"1h":{"rsi":80}},"error":"1d:CANDLE_GAP"},{"exchange":"Binance","status":"ACCESS_ERROR"}]
        result=coverage(rows); self.assertEqual((result["valid"],result["expected"]),(4,9))
        self.assertIn("CANDLE_GAP",result["details"])
    def test_source_status_uses_scalar_rows(self):
        rows=source_rows({"Binance":{"errors":{"futures":"HTTP_451"},"spot":526}})
        self.assertEqual(rows,[["Binance / errors / futures","HTTP_451"],["Binance / spot","526"]])
    def test_bitget_history_paginates_with_documented_limit(self):
        exchange=Exchange("Bitget"); base=1700000000//86400*86400; now=base+200*86400+3
        all_bars=[[t*1000,"100","102","99","101","5"] for t in range(base,base+200*86400,14400)]
        calls=[]
        def get(market,path,**params):
            self.assertEqual(path,"/api/v3/market/history-candles"); self.assertEqual(params["limit"],100)
            calls.append(params)
            eligible=[r for r in all_bars if r[0]<=params["endTime"]]
            return {"data":list(reversed(eligible[-100:]))}
        with patch.object(exchange,"get",side_effect=get):
            rows=exchange.candles({"market":"futures","symbol":"BTCUSDT"},"1d",now)
        self.assertGreater(len(calls),6); self.assertEqual(len(rows),200)
        self.assertEqual(rows[-1]["t"],base+199*86400); self.assertEqual(rows[-1]["v"],30)
        self.assertTrue(all(b["endTime"]<a["endTime"] for a,b in zip(calls,calls[1:])))
    def test_bitget_stalled_pagination_is_rejected(self):
        exchange=Exchange("Bitget"); page=[[i*14400000,"100","102","99","101","5"] for i in range(100)]
        with patch.object(exchange,"get",return_value={"data":page}):
            with self.assertRaisesRegex(ValueError,"STALLED"): exchange.candles({"market":"spot","symbol":"BTCUSDT"},"1d",200*86400)
    def test_gap_in_old_warmup_is_discarded(self):
        bars=[candle(i*3600,100+i,102+i,99+i,101+i,10) for i in range(220) if i!=10]
        values=indicators(bars,"1h",220*3600)
        self.assertEqual(values["history_bars"],209)
    def test_recorded_response_parsers(self):
        folder=Path(__file__).parent/"fixtures"/"2026-11-24"
        passes=0
        for file in folder.glob("*.json"):
            fixture=json.loads(file.read_text(encoding="utf-8"))
            if fixture.get("result")!="PASS": continue
            name,market=file.stem.split("_",1); name={n.lower():n for n in EXCHANGES}[name]
            requests=fixture["requests"]; exchange=Exchange(name)
            def get(market,path,**params):
                for r in requests:
                    if r["path"]==path: return r["response"]
                raise AssertionError("Missing recorded request "+path)
            with patch.object(exchange,"get",side_effect=get):
                catalog=exchange.load(market); self.assertIn("BTC",catalog,file.name)
                bars=exchange.candles(catalog["BTC"],"1h",time.time()); self.assertTrue(bars,file.name)
            passes+=1
        self.assertGreaterEqual(passes,10,"Actual recorded successful responses must ship with this release")

class ScanReliabilityTests(unittest.TestCase):
    def fixture(self):
        case=legacy.ScanTests(); case.setUp(); self.addCleanup(case.tearDown); return case
    def test_cmc_failed_refresh_retries_next_scan_with_valid_cache(self):
        case=self.fixture(); case.scan(); case.now+=1800
        case.store.put("meta","cmc_refresh",{"failed":True})
        with patch.object(case.engine.browser,"universe",side_effect=ValueError("temporary")) as fetch:
            case.scan(); case.now+=1800; case.scan()
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(case.engine.snapshot()["sources"]["CMC admission"],"AVAILABLE")
    def test_disabled_coinglass_is_not_required_for_main_scan(self):
        case=self.fixture()
        with patch.dict(os.environ,{"COINGLASS_ENABLED":"false"}),patch.object(case.engine.browser,"coinglass",side_effect=AssertionError("disabled")) as capture:
            self.assertEqual(case.scan(),"COMPLETE")
        capture.assert_not_called()
        self.assertIn("DISABLED",case.engine.snapshot()["sources"]["Coinglass"])
    def test_future_slots_are_not_due(self):
        case=self.fixture(); rows=case.engine.schedule_rows(case.now)
        self.assertEqual(len(rows),48); self.assertEqual(rows[-1][2:], ["NOT_DUE","NOT_DUE"])
    def test_missing_rsi_keeps_last_valid_below70_priority(self):
        case=self.fixture(); case.scan(); case.now+=1800; case.price=110; case.scan()
        case.now+=1800; case.values=legacy.metrics(69,75); case.scan()
        case.now+=1800; case.engine.metrics=lambda *args:({},["1h:CANDLE_GAP"]); case.scan()
        coin=case.engine.snapshot()["coins"][0]
        self.assertIn("BELOW 70",coin["priority"]); self.assertIn("last valid",coin["priority"])
        self.assertFalse(coin["metrics"]); self.assertTrue(coin["status"].startswith("PARTIAL"))
    def test_command_can_complete_while_scan_fetch_is_blocked(self):
        case=self.fixture(); case.scan(); case.now+=1800; case.price=110
        entered=threading.Event(); release=threading.Event(); original=case.engine.metrics
        def slow(*args): entered.set(); release.wait(5); return original(*args)
        case.engine.metrics=slow
        bot=MagicMock(); other=Engine(case.store,bot,browser=legacy.FakeBrowser())
        errors=[]
        def scan():
            try: case.scan()
            except Exception as e: errors.append(e)
        thread=threading.Thread(target=scan); thread.start()
        try:
            self.assertTrue(entered.wait(3)); other.command("/top"); bot.text.assert_called(); self.assertTrue(thread.is_alive())
        finally: release.set(); thread.join(6)
        self.assertFalse(errors)

class ResearchTests(unittest.TestCase):
    def test_maturity_missing_and_sampled_drawdown(self):
        entry={"time":0,"price":100}; prices=[{"time":1800,"price":120},{"time":3601,"price":80},{"time":14401,"price":90}]
        result=outcomes(entry,prices,15000)
        self.assertAlmostEqual(result["1h"]["return"],-.2); self.assertAlmostEqual(result["1h"]["max_drawdown"],80/120-1)
        self.assertEqual(result["7d"]["state"],"NOT_MATURE")
        self.assertFalse(result["4h"]["path_complete"])
    def test_missing_horizon_is_not_forward_filled(self):
        result=outcomes({"time":0,"price":100},[{"time":7200,"price":120}],10000)
        self.assertEqual(result["1h"]["state"],"MISSING_COVERAGE"); self.assertIsNone(result["1h"]["return"])
    def test_first_price_after_evaluation_avoids_lookahead(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store("sqlite:///"+str(Path(folder)/"db.sqlite"))
            try:
                now=time.time(); base=now-20000
                row={"coin":"BTC","exchange":"OKX","market":"futures","symbol":"BTC-USDT-SWAP","rank":1,"price":50,"metrics":legacy.metrics(80,79),"epoch":base,"scheduled":int(base//1800)*1800,"evaluated_at":stamp(base+600)}
                store.put("candidate","1",pack(row))
                for i,(seconds,price) in enumerate(((0,50),(1800,100),(5400,110),(16200,90))):
                    store.put("research_price",str(i)+"|BTC|OKX|futures|BTC-USDT-SWAP",pack({**row,"epoch":base+seconds,"source_time":stamp(base+seconds),"price":price}))
                result=build(store,now=now)
                entry=next(e for e in result["entries"] if e["variant"]=="RSI70_TOP15")
                self.assertEqual(entry["price"],100); self.assertAlmostEqual(entry["outcomes"]["1h"]["return"],.1)
                self.assertEqual(next(s for s in result["summary"] if s["variant"].endswith("WEEKLY"))["entries"],0)
            finally: store.close()

class ArtifactTests(unittest.TestCase):
    def test_unicode_coin_commands_preserve_safe_symbols(self):
        from engine import parse_command
        from domain import valid_symbol
        self.assertEqual(parse_command('/EXPAND币安人生'),('expand','币安人生'))
        self.assertTrue(valid_symbol('币安人生')); self.assertFalse(valid_symbol('../BTC'))
    def test_real_chart_renderer_handles_gaps(self):
        from charts import coin_chart
        with tempfile.TemporaryDirectory() as folder:
            now=time.time(); file=Path(folder)/"chart.png"
            rows=[{"epoch":now-5400,"price":100,"metrics":legacy.metrics()}, {"epoch":now-1801}, {"epoch":now-1800,"price":90,"metrics":legacy.metrics(65,70)}]
            coin_chart(rows,"FIXTURE",file)
            self.assertTrue(file.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")); self.assertGreater(file.stat().st_size,10000)
    def test_backup_connection_uses_environment(self):
        from backup import pg_env
        env=pg_env("postgresql://user:p%40ssword@database.example:5432/dbname")
        self.assertEqual(env["PGPASSWORD"],"p@ssword"); self.assertEqual(env["PGSSLMODE"],"require"); self.assertEqual(env["PGDATABASE"],"dbname")

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.service=Service(); self.service.password="correct-password-123"; self.service.secret="x"*32
        self.server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(self.service)); self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.base="http://127.0.0.1:"+str(self.server.server_port)
    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.thread.join()
    def test_head_has_no_body(self):
        with urlopen(Request(self.base+"/health",method="HEAD")) as response:
            self.assertEqual(response.status,200); self.assertEqual(response.read(),b""); self.assertGreater(int(response.headers["Content-Length"]),0)
    def test_private_apis_reject_unsigned_cookie(self):
        for path in ("/api/watchlist","/api/history?coin=BTC","/api/health","/dashboard.js"):
            with self.assertRaises(HTTPError) as error: urlopen(Request(self.base+path,headers={"Cookie":"watch_session=123.x.forged"}))
            self.assertEqual(error.exception.code,401)
    def test_login_cookie_secure_and_password_not_exposed(self):
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self,*args): return None
        opener=build_opener(NoRedirect())
        with self.assertRaises(HTTPError) as redirect:
            opener.open(Request(self.base+"/login",data=b"password=correct-password-123"))
        response=redirect.exception; self.assertEqual(response.code,303)
        cookie=response.headers["Set-Cookie"]; self.assertIn("HttpOnly",cookie); self.assertIn("Secure",cookie); self.assertIn("SameSite=Strict",cookie)
        with urlopen(Request(self.base+"/",headers={"Cookie":cookie.split(";",1)[0]})) as page:
            html=page.read().decode(); self.assertIn("Retained coins",html); self.assertNotIn(self.service.password,html); self.assertNotIn(self.service.secret,html)
    def test_cross_origin_login_rejected(self):
        with self.assertRaises(HTTPError) as error: urlopen(Request(self.base+"/login",data=b"password=correct-password-123",headers={"Origin":"https://evil.example"}))
        self.assertEqual(error.exception.code,403)
    def test_login_throttle(self):
        for i in range(6):
            with self.assertRaises(HTTPError) as error: urlopen(Request(self.base+"/login",data=b"password=wrong"))
        self.assertEqual(error.exception.code,429)

if __name__=="__main__": unittest.main()
