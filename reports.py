"""Production XLSX renderer for a standalone Python deployment."""
from datetime import datetime
from pathlib import Path
import math
import textwrap
import xlsxwriter
from domain import IST, stamp, day_start
from quality import source_rows

HEADERS = ["Coin", "Name", "Priority", "Rule exchange", "Market", "Price USDT", "06:00 gain %",
    "Return since entry %", "Recorded peak USDT", "From peak %", "RSI 1H", "RSI 4H", "RSI 1D",
    "RSI change 1H", "RSI change 4H", "RSI change 1D", "MFI 1H", "MFI 4H", "MFI 1D",
    "Coinglass RSI 1H", "Coinglass RSI 4H", "Coinglass RSI 1D", "Tracking since IST", "Source time IST", "Data status",
    "Valid RSI cells", "Expected RSI cells", "RSI coverage %", "Missing coverage"]


def make_report(file, snapshot, schedule=None, history=None, weekly=None, bars=None,research=None):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(file, {"strings_to_formulas":False,"strings_to_urls":False})
    title = workbook.add_format({"font_name":"Arial","font_size":16,"bold":True,"font_color":"#18354F"})
    body = workbook.add_format({"font_name":"Arial","font_size":10,"valign":"vcenter"})
    decimal = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"0.0","valign":"vcenter"})
    price = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"0.############","valign":"vcenter"})
    scientific = workbook.add_format({"num_format":"0.000000E+00"})
    amount = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"#,##0.00","valign":"vcenter"})
    volume = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"#,##0.0000","valign":"vcenter"})
    percent = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"0.00%;[Red]-0.00%;0.00%","valign":"vcenter"})
    date = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"dd-mmm-yyyy hh:mm:ss","valign":"vcenter"})
    header = workbook.add_format({"font_name":"Arial","font_size":10,"bold":True,"text_wrap":True,"valign":"vcenter","align":"center","font_color":"#FFFFFF","bg_color":"#214C70"})
    wrap = workbook.add_format({"font_name":"Arial","font_size":10,"text_wrap":True,"valign":"top"})
    warning = workbook.add_format({"bg_color":"#FFF0C2","font_color":"#704B00"})
    priority = workbook.add_format({"bg_color":"#FCE3E3","font_color":"#9F2020","bold":True})

    def sheet(name, heading, headers, rows, formats=None):
        widths={1:32,28:48} if name=="RSI Watchlist" else {0:48,1:110} if name in ("Data Status","Research Method") else {}
        ws = workbook.add_worksheet(name)
        ws.hide_gridlines(2)
        ws.set_default_row(22)
        ws.set_column(0,len(headers)-1,18,body)
        ws.set_column(0,0,15,body)
        ws.write(1,0,heading,title)
        ws.write(2,0,f"Scheduled IST: {snapshot.get('scheduled_ist','on demand')}   Scan start: {snapshot.get('scan_started_at',snapshot.get('captured_at','unavailable'))}",body)
        ws.write(3,0,f"Scan finished IST: {snapshot.get('scan_finished_at') or 'not recorded'}. Status: {snapshot.get('status','UNKNOWN')}. Prices: USDT.",body)
        ws.write(4,0,"Blank numbers mean unavailable. Source times differ by row. Delivery ledger is as of file generation, before this upload.",body)
        ws.freeze_panes(7,1)
        columns = [{"header":label,"header_format":header, **({"format":formats[i]} if formats and formats[i] else {})} for i,label in enumerate(headers)]
        if formats:
            for row in rows:
                for i,value in enumerate(row):
                    if formats[i] is date and isinstance(value,str):
                        row[i] = datetime.fromisoformat(value).astimezone(IST).replace(tzinfo=None)
        # Native tables need one data row. An empty row is not a qualifying coin.
        ws.add_table(6,0,6+max(1,len(rows)),len(headers)-1,{"name":"T_"+name.replace(" ","_"),
            "columns":columns,"data":rows if rows else [[None]*len(headers)],"style":"Table Style Medium 2"})
        ws.set_row(6,42,body)
        if formats:
            for i,fmt in enumerate(formats):
                if fmt is date: ws.set_column(i,i,29,body)
                elif fmt in (price,amount,volume): ws.set_column(i,i,24,body)
                elif fmt is wrap: ws.set_column(i,i,widths.get(i,36),body)
                if fmt is price:
                    ws.conditional_format(7,i,6+max(1,len(rows)),i,{"type":"cell","criteria":"between","minimum":1e-300,"maximum":1e-10,"format":scientific})
            for j,row in enumerate(rows):
                lines=max([1]+[len(textwrap.wrap(str(v or ""),width=max(8,int(widths.get(i,36)*.9)))) for i,v in enumerate(row) if formats[i] is wrap])
                ws.set_row(7+j,max(22,lines*14+4))
        return ws

    rows = []
    for coin in snapshot.get("coins",[]):
        metrics = coin.get("metrics",{})
        rsis = [metrics.get(tf,{}).get("rsi") for tf in ("1h","4h","1d")]
        changes = [(metrics.get(tf,{}).get("rsi")-metrics.get(tf,{}).get("previous_rsi"))
            if metrics.get(tf,{}).get("rsi") is not None and metrics.get(tf,{}).get("previous_rsi") is not None else None for tf in ("1h","4h","1d")]
        cg = coin.get("coinglass",{})
        rows.append([coin["symbol"],coin.get("name"),coin.get("priority"),coin.get("exchange"),coin.get("market"),
            coin.get("price"),coin.get("day_gain"),coin.get("since_entry"),coin.get("peak"),coin.get("from_peak"),
            *rsis,*changes,*[metrics.get(tf,{}).get("mfi") for tf in ("1h","4h","1d")],
            *[cg.get("rsi_"+tf) for tf in ("1h","4h","1d")],coin.get("entered_at"),coin.get("updated_at"),coin.get("status"),
            *[coin.get("coverage",{}).get(k) for k in ("valid","expected","ratio","summary")]])
    formats = [body]*len(HEADERS)
    for col in (5,8): formats[col]=price
    for col in (6,7,9): formats[col]=percent
    for col in range(10,22): formats[col]=decimal
    formats[22]=formats[23]=date
    formats[2]=formats[24]=wrap
    formats[1]=wrap
    formats[27]=percent; formats[28]=wrap
    ws = sheet("RSI Watchlist","RSI watchlist",HEADERS,rows,formats)
    ws.set_column(1,1,32,body)
    ws.set_column(22,23,29,body)
    ws.set_column(24,24,36,body)
    ws.conditional_format(7,10,max(7,6+len(rows)),12,{"type":"cell","criteria":"between","minimum":70,"maximum":100,"format":warning})
    ws.conditional_format(7,2,max(7,6+len(rows)),2,{"type":"text","criteria":"containing","value":"BELOW 70","format":priority})
    detail_headers = ["Coin","Exchange","Market","Instrument","Price USDT","06:00 gain %","Gainer rank","24h turnover USDT","Status","Captured IST","Detail","RSI 1H","RSI 4H","RSI 1D","MFI 1H","MFI 4H","MFI 1D","Baseline captured IST","Coinglass RSI 1H","Coinglass RSI 4H","Coinglass RSI 1D"]
    detail_formats = [body,body,body,body,price,percent,body,amount,body,date,wrap,*[decimal]*6,date,*[decimal]*3]
    def detail_rows(source):
        return [[r.get(k) for k in ("coin","exchange","market","symbol","price","day_gain","rank","turnover","status","captured_at")]+[r.get("error") or r.get("indicator_status")]+
            [r.get("metrics",{}).get(tf,{}).get(indicator) for indicator in ("rsi","mfi") for tf in ("1h","4h","1d")]+[r.get("baseline_at")]+
            [r.get("coinglass",{}).get("rsi_"+tf) for tf in ("1h","4h","1d")] for r in source]
    detail_sheet=sheet("Exchange Detail","Exchange verification",detail_headers,detail_rows(snapshot.get("exchange_rows",[])),detail_formats)
    detail_sheet.set_column(8,8,24,body)
    sheet("Daily Gainers","Positive gainers since 06:00 IST",detail_headers,detail_rows(snapshot.get("gainers",[])),detail_formats)
    indicator_rows = []
    for row in snapshot.get("exchange_rows",[]):
        for tf,values in row.get("metrics",{}).items():
            last = (values.get("candles") or [{}])[-1]
            indicator_rows.append([row["coin"],row["exchange"],row.get("market"),row.get("symbol"),tf,
                values.get("rsi"),values.get("previous_rsi"),values.get("mfi"),
                stamp(values["bar_open"]) if values.get("bar_open") is not None else None,
                stamp(values["bar_close"]) if values.get("bar_close") is not None else None,
                *[last.get(k) for k in ("o","h","l","c","v")],values.get("history_bars")])
    sheet("Indicator Bars","Completed-candle indicator details",["Coin","Exchange","Market","Instrument","Timeframe","RSI","Previous RSI","MFI","Bar open IST","Bar close IST","Open USDT","High USDT","Low USDT","Close USDT","Volume base units","Warm-up bars"],indicator_rows,
        [body]*5+[decimal]*3+[date]*2+[price]*4+[volume,body]).set_column(8,9,29,body)
    sources = source_rows(snapshot.get("sources",{}))
    source_sheet = sheet("Data Status","Data and run status",["Source / setting","Result"],sources,[wrap,wrap])
    source_sheet.set_column(0,0,48,wrap)
    source_sheet.set_column(1,1,110,wrap)
    if schedule is not None:
        sheet("48 Slots","Daily report slots",["Slot","Scheduled IST","Snapshot status","Delivery status"],schedule,[body,date,body,body]).set_column(1,3,32,body)
    if history is not None:
        sheet("Coin History","Stored coin observations",["Captured IST","Coin","Exchange","Market","Price USDT","Day gain %","RSI 1H","RSI 4H","RSI 1D","MFI 1H","MFI 4H","MFI 1D","Status"],history,
            [date,body,body,body,price,percent,*[decimal]*6,body]).set_column(0,0,29,body)
    if weekly is not None:
        weekly_sheet=sheet("Weekly Coverage","Observed seven-day coverage",["Coin","RSI coverage","Expected slots","Observed valid slots","Below-70 slots","Gainer days / 7","Result"],weekly)
        weekly_sheet.set_column(1,1,25,body)
        weekly_sheet.set_column(6,6,32,body)
    if bars is not None:
        sheet("Observed OHLCV","Observed completed candles",["Candle open IST","Exchange","Market","Instrument","Timeframe","Open USDT","High USDT","Low USDT","Close USDT","Volume base units"],bars,
            [date,body,body,body,body,*[price]*4,volume]).set_column(0,0,29,body)
    if research is not None:
        cols=["variant","horizon","entries","observed","not_mature","missing","mean_return","median_return","weekly_unknown_evaluations","entry_sample_missing"]
        sheet("Research Summary","Prospective variant comparison",["Variant","Horizon","Entries","Observed outcomes","Not mature","Missing coverage","Mean return %","Median return %","Weekly unknown evaluations","Missing entry sample"],
            [[row.get(c) for c in cols] for row in research["summary"]],[wrap,body,*[body]*4,percent,percent,body,body])
        outcomes=[]
        for entry in research["entries"]:
            for horizon,item in entry["outcomes"].items():
                outcomes.append([entry["variant"],entry["coin"],entry["exchange"],entry["market"],entry["instrument"],entry["signal_at"],entry["entered_at"],entry["price"],horizon,
                    item["state"],item["return"],item["observed_at"],item["lag_seconds"],item["mae"],item["mfe"],item["max_drawdown"],item.get("path_complete")])
        sheet("Research Outcomes","Source-matched forward returns",["Variant","Coin","Exchange","Market","Instrument","Rule evaluated IST","Entry reference IST","Entry price USDT","Horizon","Outcome status","Return %","Outcome IST","Horizon lag seconds","Sampled MAE %","Sampled MFE %","Sampled max drawdown %","Sample path complete"],
            outcomes,[wrap,*[body]*4,date,date,price,body,wrap,percent,date,body,percent,percent,percent,body])
        sheet("Research Method","Research definitions",["Setting","Definition"],[["Method",research["method"]],["Window days",research["window_days"]],["Generated IST",research["generated_at"]]],[body,wrap]).set_column(1,1,110,wrap)
    workbook.close()
    return file
