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

from unittest.mock import patch,MagicMock

from urllib.request import Request, urlopen

from urllib.error import HTTPError

import xml.etree.ElementTree as ET

from domain import IST, FRAMES, EXCHANGES, stamp, day_start, slot_info, entry_pass, crosses, rsi, mfi, indicators

from sources import Exchange, candle, cmc_rows, utc_days_from_4h, Browser

from memory_guard import BrowserMemoryGuard

from storage import Store

from engine import Engine, pack, parse_command

from reports import make_report

from app import config_errors, authorized_message, handler_for, ThreadingHTTPServer

def metrics(value=75,previous=74):
    return {tf:{"rsi":value,"previous_rsi":previous,"mfi":80} for tf in FRAMES}

class SourceTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{"CMC_API_KEY":""})
        self.environment.start()

    def tearDown(self): self.environment.stop()


    def cmc_listing_fixture(self,start,count=100):
        return {"data":{"cryptoCurrencyList":[{"id":i,"symbol":"C"+str(i),"name":"Coin "+str(i),
            "quotes":[{"name":"USD","volume24h":10000-i}]} for i in range(start,start+count)]}}

    def test_cmc_small_order_reversal_is_resorted_not_rejected(self):
        def listing(base,path,**params):
            payload=self.cmc_listing_fixture(params["start"])
            rows=payload["data"]["cryptoCurrencyList"]
            rows[30],rows[31]=rows[31],rows[30]
            return payload
        with patch("sources.public_json",side_effect=listing),patch("sources.time.sleep"):
            universe=Browser().universe()
        self.assertEqual([r["id"] for r in universe["coins"]],list(range(1,501)))
        self.assertGreaterEqual(universe["verification"]["eligible_rows_resorted"],600)

    def test_cmc_stock_exclusions_do_not_fail_first_page_row_count(self):
        def listing(base,path,**params):
            payload=self.cmc_listing_fixture(params["start"])
            for row in payload["data"]["cryptoCurrencyList"]:
                if row["id"]<=120: row["tags"]=["tokenized-stock"]
            return payload
        with patch("sources.public_json",side_effect=listing),patch("sources.time.sleep"):
            universe=Browser().universe()
        self.assertEqual([r["id"] for r in universe["coins"]],list(range(121,621)))

    def test_cmc_ignored_volume_sort_is_still_rejected(self):
        payload=self.cmc_listing_fixture(1)
        payload["data"]["cryptoCurrencyList"][2]["quotes"][0]["volume24h"]=1000000
        with patch("sources.public_json",return_value=payload):
            with self.assertRaisesRegex(ValueError,"VOLUME_SORT_NOT_VERIFIED"):
                Browser().universe()

    def test_cmc_repeated_page_is_not_a_complete_universe(self):
        with patch("sources.public_json",return_value=self.cmc_listing_fixture(1)),patch("sources.time.sleep"):
            with self.assertRaisesRegex(ValueError,"DUPLICATE_PAGINATION"):
                Browser().universe()

    def test_cmc_small_page_overlap_is_deduplicated(self):
        def listing(base,path,**params):
            offset=params["start"]
            start=offset if offset==1 else offset-1
            return self.cmc_listing_fixture(start)
        with patch("sources.public_json",side_effect=listing),patch("sources.time.sleep"):
            universe=Browser().universe()
        self.assertEqual(len({r["id"] for r in universe["coins"]}),500)
        self.assertEqual([r["id"] for r in universe["coins"]],list(range(1,501)))
        self.assertGreater(universe["verification"]["page_overlap_deduplicated"],0)

    def test_official_cmc_source_is_opt_in_and_keeps_key_out_of_data(self):
        payload={"status":{"error_code":0},"data":[{"id":i,"symbol":"C"+str(i),"name":"Coin "+str(i),
            "quote":{"USD":{"volume_24h":10000-i}}} for i in range(1,1001)]}
        with patch.dict(os.environ,{"CMC_API_KEY":"fixture-not-a-real-key"}),patch("sources.public_json",return_value=payload) as request:
            universe=Browser().universe()
        self.assertEqual(len(universe["coins"]),500)
        self.assertEqual(request.call_args.args,("https://pro-api.coinmarketcap.com","/v1/cryptocurrency/listings/latest"))
        self.assertEqual(request.call_args.kwargs["sort"],"volume_24h")
        self.assertEqual(request.call_args.kwargs["headers"],{"X-CMC_PRO_API_KEY":"fixture-not-a-real-key"})
        self.assertNotIn("fixture-not-a-real-key",json.dumps(universe))

    def test_official_cmc_incomplete_or_error_response_is_not_used(self):
        for payload in ({"status":{"error_code":1001},"data":[]},
            {"status":{"error_code":0},"data":[]}):
            with patch.dict(os.environ,{"CMC_API_KEY":"fixture-not-a-real-key"}),patch("sources.public_json",return_value=payload):
                with self.assertRaisesRegex(ValueError,"CMC_API_"): Browser().universe()

    def test_cmc_top500_does_not_launch_chromium(self):
        def listing(base,path,**params):
            self.assertEqual(base,"https://api.coinmarketcap.com")
            self.assertEqual(path,"/data-api/v3/cryptocurrency/listing")
            start=params["start"]
            return {"data":{"cryptoCurrencyList":[{"id":i,"symbol":"C"+str(i),"name":"Coin "+str(i),
                "quotes":[{"name":"USD","volume24h":10000-i}]} for i in range(start,start+100)]}}
        browser=Browser()
        with patch.object(browser,"start",side_effect=AssertionError("CMC must not launch a browser")),patch("sources.public_json",side_effect=listing),patch("sources.time.sleep"):
            universe=browser.universe()
        self.assertEqual(len(universe["coins"]),500)
        self.assertEqual(universe["coins"][0]["symbol"],"C1")
        self.assertEqual(universe["coins"][-1]["symbol"],"C500")
        self.assertIsNone(browser.runtime)

    def test_browser_layers_close_on_capture_success_and_failure(self):
        for failure in (False,True):
            browser=Browser()
            context,child,runtime=MagicMock(),MagicMock(),MagicMock()
            browser.context,browser.browser,browser.runtime=context,child,runtime
            context.close.side_effect=RuntimeError("already closed")
            guard=MagicMock(); guard.start.return_value=guard; guard.exceeded=False
            with patch("sources.BrowserMemoryGuard",return_value=guard),patch.object(browser,"_coinglass",side_effect=ValueError("source unavailable") if failure else None,return_value=({},{})):
                if failure:
                    with self.assertRaisesRegex(ValueError,"source unavailable"): browser.coinglass()
                else: self.assertEqual(browser.coinglass(),({},{}))
            context.close.assert_called_once(); child.close.assert_called_once(); runtime.stop.assert_called_once()
            guard.stop.assert_called_once()
            self.assertIsNone(browser.runtime)
            self.assertIsNone(browser.browser)

    def test_browser_memory_pressure_aborts_capture(self):
        browser=Browser()
        guard=MagicMock(); guard.start.return_value=guard; guard.exceeded=True
        with patch("sources.BrowserMemoryGuard",return_value=guard),patch.object(browser,"_coinglass") as capture:
            with self.assertRaisesRegex(ValueError,"MEMORY_BUDGET_EXCEEDED"): browser.coinglass()
            capture.assert_not_called()
        guard.stop.assert_called_once()

    def test_memory_guard_preserves_parent_and_existing_children(self):
        baseline=MagicMock(pid=10); baseline.create_time.return_value=1000
        older=MagicMock(pid=11); older.create_time.return_value=900
        created=MagicMock(pid=12); created.create_time.return_value=1001
        guard=BrowserMemoryGuard(reader=lambda:{"usage_bytes":110,"limit_bytes":100})
        guard.started=1000; guard.limit=100; guard.baseline={10}
        with patch("memory_guard.descendants",return_value=[baseline,older,created]):
            self.assertTrue(guard.sample())
        baseline.kill.assert_not_called(); older.kill.assert_not_called(); created.kill.assert_called_once()
        self.assertTrue(guard.exceeded)

    def test_binance_spot_uses_attached_project_market_data_resource(self):
        exchange=Exchange("Binance")
        metadata={"rateLimits":[],"symbols":[
            {"symbol":"BTCUSDT","baseAsset":"BTC","quoteAsset":"USDT","status":"TRADING","isSpotTradingAllowed":True},
            {"symbol":"OLDUSDT","baseAsset":"OLD","quoteAsset":"USDT","status":"BREAK","isSpotTradingAllowed":True},
            {"symbol":"BTCUSDC","baseAsset":"BTC","quoteAsset":"USDC","status":"TRADING","isSpotTradingAllowed":True}]}
        tickers=[{"symbol":"BTCUSDT","lastPrice":"65000.25","quoteVolume":"1200000000.50"}]
        raw=[1700000000000,"10","12","9","11","50","550"]
        with patch("sources.public_json",side_effect=[metadata,tickers,[raw],[raw],[raw]]) as request:
            found=exchange.load("spot")
            self.assertEqual(set(found),{"BTC"})
            self.assertEqual(found["BTC"]["market"],"spot")
            self.assertEqual(found["BTC"]["price"],65000.25)
            self.assertEqual(found["BTC"]["turnover"],1200000000.50)
            for frame in FRAMES:
                self.assertEqual(exchange.candles(found["BTC"],frame,1700000100),[candle(*raw[:6])])
            self.assertEqual(request.call_args_list[0].args,("https://data-api.binance.vision","/api/v3/exchangeInfo"))
            self.assertEqual(request.call_args_list[0].kwargs,{"permissions":"SPOT","showPermissionSets":"false","symbolStatus":"TRADING"})
            self.assertEqual(request.call_args_list[1].kwargs,{})
            self.assertEqual([call.kwargs["interval"] for call in request.call_args_list[2:]],list(FRAMES))
            self.assertTrue(all(call.args[0]=="https://data-api.binance.vision" for call in request.call_args_list))

    def test_binance_futures_candles_keep_their_futures_resource(self):
        raw=[1700000000000,"10","12","9","11","50","550"]
        with patch("sources.public_json",return_value=[raw]) as request:
            result=Exchange("Binance").candles({"market":"futures","symbol":"BTCUSDT"},"1h",1700000100)
            self.assertEqual(result,[candle(*raw[:6])])
            self.assertEqual(request.call_args.args,("https://fapi.binance.com","/fapi/v1/klines"))



class StoreTests(unittest.TestCase):
    def test_streamed_history_filters_coin_in_database(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store("sqlite:///"+str(Path(folder)/"db.sqlite"))
            try:
                for key in ("1|BTC","2|ETH","3|BTC","4|WBTC"):
                    store.put("observation",key,{"key":key})
                self.assertEqual([key for key,_ in store.iter_records("observation",key_suffix="|BTC")],["1|BTC","3|BTC"])
            finally: store.close()



class FakeBrowser:
    def universe(self):
        return {"coins":[{"id":1,"symbol":"BTC","name":"Bitcoin"},{"id":1027,"symbol":"ETH","name":"Ethereum"}],
            "ambiguous_symbols":[],"epoch":__import__("time").time(),"captured_at":stamp()}
    def coinglass(self): return {c:{"rsi_1h":80,"rsi_4h":81,"rsi_1d":82} for c in ("BTC","ETH")},{"data_type":"str"}
    def close(self): pass

class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{"DATA_DIR":self.temp.name,"COINGLASS_ENABLED":"true"})
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

    def test_verified_universe_reaches_real_closed_candle_rule_evaluations(self):
        self.scan()
        self.now+=1800; self.price=110
        del self.engine.metrics
        def candles(row,frame,now):
            seconds=FRAMES[frame]; end=int(now//seconds)*seconds
            closes=[100+i*.5-(1 if i%5==3 else 0) for i in range(200)]
            return [candle(end-(200-i)*seconds,c,c+1,c-1,c,100) for i,c in enumerate(closes)]
        with patch.object(Exchange,"candles",side_effect=candles),patch("engine.time.sleep"):
            self.assertEqual(self.scan(),"COMPLETE")
        current=self.engine.snapshot()
        evaluation=current["sources"]["Candidate evaluation"]
        self.assertGreater(evaluation["entry_rule_attempts"],0)
        self.assertGreater(evaluation["candle_requests"],0)
        self.assertEqual(set(self.engine.active()),{"BTC","ETH"})
        self.assertTrue(all(entry_pass(c["metrics"]) for c in current["coins"]))

    def test_retained_coin_outside_universe_survives_memory_compaction(self):
        self.scan()
        self.now+=1800; self.price=110
        self.scan()
        universe=FakeBrowser().universe()
        universe["coins"]=[r for r in universe["coins"] if r["symbol"]=="ETH"]
        self.store.put("meta","universe",universe)
        self.now+=1800; self.price=90
        self.scan()
        btc=next(c for c in self.engine.snapshot()["coins"] if c["symbol"]=="BTC")
        self.assertEqual(btc["price"],90)
        self.assertIn("BTC",self.engine.active())

