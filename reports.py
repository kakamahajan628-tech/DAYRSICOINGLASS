"""Production XLSX renderer for a standalone Python deployment."""
from datetime import datetime
from pathlib import Path
import math
import xlsxwriter
from domain import IST, stamp, day_start

HEADERS = ["Coin", "Name", "Priority", "Rule exchange", "Market", "Price USDT", "06:00 gain %",
    "Return since entry %", "Recorded peak USDT", "From peak %", "RSI 1H", "RSI 4H", "RSI 1D",
    "RSI change 1H", "RSI change 4H", "RSI change 1D", "MFI 1H", "MFI 4H", "MFI 1D",
    "Coinglass RSI 1H", "Coinglass RSI 4H", "Coinglass RSI 1D", "Tracking since IST", "Source time IST", "Data status"]


def make_report(file, snapshot, schedule=None, history=None, weekly=None, bars=None):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(file, {"strings_to_formulas":False,"strings_to_urls":False})
    title = workbook.add_format({"font_name":"Arial","font_size":16,"bold":True,"font_color":"#18354F"})
    body = workbook.add_format({"font_name":"Arial","font_size":10,"valign":"vcenter"})
    decimal = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"0.0"})
    price = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"0.############"})
    scientific = workbook.add_format({"num_format":"0.000000E+00"})
    amount = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"#,##0.00"})
    volume = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"#,##0.0000"})
    percent = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"0.00%;[Red]-0.00%;0.00%"})
    date = workbook.add_format({"font_name":"Arial","font_size":10,"num_format":"dd-mmm-yyyy hh:mm:ss"})
    header = workbook.add_format({"font_name":"Arial","font_size":10,"bold":True,"text_wrap":True,"valign":"vcenter","font_color":"#FFFFFF","bg_color":"#214C70"})
    wrap = workbook.add_format({"font_name":"Arial","font_size":10,"text_wrap":True,"valign":"top"})
    warning = workbook.add_format({"bg_color":"#FFF0C2","font_color":"#704B00"})
    priority = workbook.add_format({"bg_color":"#FCE3E3","font_color":"#9F2020","bold":True})

    def sheet(name, heading, headers, rows, formats=None):
        ws = workbook.add_worksheet(name)
        ws.hide_gridlines(2)
        ws.set_default_row(22)
        ws.set_column(0,len(headers)-1,18,body)
        ws.set_column(0,0,15,body)
        ws.write(1,0,heading,title)
        ws.write(2,0,f"Scheduled IST: {snapshot.get('scheduled_ist','on demand')}   Captured IST: {snapshot.get('captured_at','unavailable')}",body)
        ws.write(3,0,f"Status: {snapshot.get('status','UNKNOWN')}. Blank numeric cells mean unavailable. Currency: USDT. Source times: IST.",body)
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
                elif fmt is wrap: ws.set_column(i,i,36,body)
                if fmt is price:
                    ws.conditional_format(7,i,6+max(1,len(rows)),i,{"type":"cell","criteria":"between","minimum":1e-300,"maximum":1e-10,"format":scientific})
            for j,row in enumerate(rows):
                lines=max([1]+[math.ceil(len(str(v or ""))/32) for i,v in enumerate(row) if formats[i] is wrap])
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
            *[cg.get("rsi_"+tf) for tf in ("1h","4h","1d")],coin.get("entered_at"),coin.get("updated_at"),coin.get("status")])
    formats = [body]*len(HEADERS)
    for col in (5,8): formats[col]=price
    for col in (6,7,9): formats[col]=percent
    for col in range(10,22): formats[col]=decimal
    formats[22]=formats[23]=date
    formats[2]=formats[24]=wrap
    ws = sheet("RSI Watchlist","RSI watchlist",HEADERS,rows,formats)
    ws.set_column(1,1,26,body)
    ws.set_column(22,23,29,body)
    ws.set_column(24,24,36,body)
    ws.conditional_format(7,10,max(7,6+len(rows)),12,{"type":"cell","criteria":"between","minimum":70,"maximum":100,"format":warning})
    ws.conditional_format(7,2,max(7,6+len(rows)),2,{"type":"text","criteria":"containing","value":"BELOW 70","format":priority})
    detail_headers = ["Coin","Exchange","Market","Instrument","Price USDT","06:00 gain %","Gainer rank","24h turnover USDT","Status","Captured IST","Detail","RSI 1H","RSI 4H","RSI 1D","MFI 1H","MFI 4H","MFI 1D","Baseline captured IST","Coinglass RSI 1H","Coinglass RSI 4H","Coinglass RSI 1D"]
    detail_formats = [body,body,body,body,price,percent,body,amount,body,date,wrap,*[decimal]*6,date,*[decimal]*3]
    def detail_rows(source):
        return [[r.get(k) for k in ("coin","exchange","market","symbol","price","day_gain","rank","turnover","status","captured_at","error")]+
            [r.get("metrics",{}).get(tf,{}).get(indicator) for indicator in ("rsi","mfi") for tf in ("1h","4h","1d")]+[r.get("baseline_at")]+
            [r.get("coinglass",{}).get("rsi_"+tf) for tf in ("1h","4h","1d")] for r in source]
    sheet("Exchange Detail","Exchange verification",detail_headers,detail_rows(snapshot.get("exchange_rows",[])),detail_formats).set_column(8,10,32,body)
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
    sources = [[k,str(v)] for k,v in snapshot.get("sources",{}).items()]
    source_sheet = sheet("Data Status","Data and run status",["Source / setting","Result"],sources,[body,wrap])
    source_sheet.set_column(0,0,32,body)
    source_sheet.set_column(1,1,110,wrap)
    for i in range(len(sources)): source_sheet.set_row(7+i,60)
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
    workbook.close()
    return file
