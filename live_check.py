"""Optional read-only source smoke check; never contacts Telegram."""
import argparse
import json
import time
from domain import stamp, indicators
from sources import Browser, markets


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--browser",action="store_true",help="Also load CMC and paginate Coinglass")
    parser.add_argument("--skip-exchanges",action="store_true")
    args=parser.parse_args()
    report={"captured_at":stamp(),"location":"local execution; not proof of Render connectivity"}
    if not args.skip_exchanges:
        report["exchanges"]={}
        for name,exchange in markets().items():
            row=exchange.select("BTC")
            result={"catalog_counts":{k:len(v) for k,v in exchange.catalog.items()},"errors":exchange.errors,"BTC":row}
            if row.get("price"):
                for frame in ("1h","4h","1d"):
                    try:
                        value=indicators(exchange.candles(row,frame,time.time()),frame,time.time())
                        result[frame]={k:v for k,v in value.items() if k!="candles"}
                    except Exception as error: result[frame]="UNAVAILABLE: "+type(error).__name__+" "+str(error)[:100]
            report["exchanges"][name]=result
    if args.browser:
        browser=Browser()
        try:
            try:
                universe=browser.universe()
                report["CMC"]={"count":len(universe["coins"]),"top3":universe["coins"][:3],"ambiguous":universe["ambiguous_symbols"]}
            except Exception as error: report["CMC"]="UNAVAILABLE: "+type(error).__name__+" "+str(error)[:250]
            try:
                rows,xhr=browser.coinglass()
                report["Coinglass"]={"count":len(rows),"BTC":rows.get("BTC"),"xhr":{k:v for k,v in xhr.items() if k!="payload"}}
            except Exception as error: report["Coinglass"]="UNAVAILABLE: "+type(error).__name__+" "+str(error)[:250]
        finally: browser.close()
    print(json.dumps(report,indent=2))


if __name__=="__main__": main()
