"""Time, indicator and watchlist rules. No order execution."""
from datetime import datetime, timedelta, timezone
import math

IST = timezone(timedelta(hours=5, minutes=30), "IST")
FRAMES = {"1h": 3600, "4h": 14400, "1d": 86400}
EXCHANGES = ("Binance", "Bybit", "OKX", "Bitget", "Gate", "KuCoin")


def num(value):
    try:
        result = float(value)
        return result if value not in (None, "") and not isinstance(value, bool) and math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def stamp(epoch=None):
    return datetime.fromtimestamp(epoch, IST).isoformat(timespec="seconds") if epoch is not None else datetime.now(IST).isoformat(timespec="seconds")


def day_start(epoch):
    local = datetime.fromtimestamp(epoch, IST)
    start = local.replace(hour=6, minute=0, second=0, microsecond=0)
    return (start - timedelta(days=1) if local < start else start).timestamp()


def slot_info(epoch):
    scheduled = int(epoch // 1800) * 1800
    start = day_start(scheduled)
    return {"key": str(scheduled), "scheduled": scheduled, "scheduled_ist": stamp(scheduled),
            "day": datetime.fromtimestamp(start, IST).date().isoformat(), "slot": int((scheduled-start)//1800)+1}


def valid_rsi(value):
    value = num(value)
    return value is not None and 0 <= value <= 100


def entry_pass(metrics):
    return all(valid_rsi(metrics.get(tf, {}).get("rsi")) and valid_rsi(metrics.get(tf, {}).get("previous_rsi"))
        and 70 <= metrics[tf]["rsi"] <= 100 and metrics[tf]["rsi"] > metrics[tf]["previous_rsi"] for tf in FRAMES)


def crosses(previous, current):
    return [tf for tf in FRAMES if valid_rsi(previous.get(tf)) and valid_rsi(current.get(tf))
            and previous[tf] >= 70 and current[tf] < 70]


def rsi(closes, length=14):
    if len(closes) <= length:
        return None
    changes = [b-a for a, b in zip(closes, closes[1:])]
    gain = sum(max(c, 0) for c in changes[:length])/length
    loss = sum(max(-c, 0) for c in changes[:length])/length
    for change in changes[length:]:
        gain = (gain*(length-1)+max(change, 0))/length
        loss = (loss*(length-1)+max(-change, 0))/length
    return (100.0 if gain else None) if not loss else 100-100/(1+gain/loss)


def mfi(candles, length=14):
    if len(candles) <= length:
        return None
    tp = [(r["h"]+r["l"]+r["c"])/3 for r in candles]
    positive = negative = 0
    for i in range(len(candles)-length, len(candles)):
        flow = tp[i]*candles[i]["v"]
        if tp[i] > tp[i-1]:
            positive += flow
        elif tp[i] < tp[i-1]:
            negative += flow
    return (100.0 if positive else None) if not negative else 100-100/(1+positive/negative)


def indicators(candles, frame, now):
    seconds = FRAMES[frame]
    rows = sorted({r["t"]: r for r in candles if r["t"]+seconds <= now}.values(), key=lambda r:r["t"])
    if len(rows) < 100:
        raise ValueError("INSUFFICIENT_CLOSED_HISTORY")
    expected = int(now//seconds)*seconds-seconds
    if rows[-1]["t"] != expected:
        raise ValueError("STALE_OR_MISALIGNED_CANDLE")
    if any(b["t"]-a["t"] != seconds for a, b in zip(rows[-100:], rows[-99:])):
        raise ValueError("CANDLE_GAP")
    closes = [r["c"] for r in rows]
    return {"rsi": rsi(closes), "previous_rsi": rsi(closes[:-1]), "mfi": mfi(rows),
            "bar_open": rows[-1]["t"], "bar_close": rows[-1]["t"]+seconds,
            "close": rows[-1]["c"], "high": rows[-1]["h"], "low": rows[-1]["l"],
            "volume_base": rows[-1]["v"], "history_bars": len(rows), "candles": rows[-8:]}
