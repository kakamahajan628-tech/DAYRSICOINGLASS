"""Small server-side plots, loaded only by the command process."""
from datetime import datetime
from domain import IST,FRAMES


def coin_chart(rows,symbol,file):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    times=[datetime.fromtimestamp(r["epoch"],IST) for r in rows]
    fig,(price,rsi)=plt.subplots(2,1,figsize=(10,6),sharex=True,layout="constrained")
    price.plot(times,[r.get("price",float("nan")) or float("nan") for r in rows],color="#2463a5",linewidth=1.5,marker=".",markersize=3)
    price.set_title(symbol+" — observed price and closed-candle RSI")
    price.set_ylabel("USDT"); price.grid(alpha=.15)
    for tf,color in zip(FRAMES,("#2463a5","#ba7600","#218262")):
        rsi.plot(times,[r.get("metrics",{}).get(tf,{}).get("rsi") if r.get("metrics",{}).get(tf,{}).get("rsi") is not None else float("nan") for r in rows],label=tf.upper(),color=color,marker=".",markersize=3)
    # Blank rows inserted by callers create chart breaks for missing slots.
    rsi.axhline(70,color="#c44",linestyle="--",linewidth=.8); rsi.set_ylim(0,100)
    rsi.set_ylabel("RSI14"); rsi.legend(ncol=3,loc="upper left"); rsi.grid(alpha=.15)
    rsi.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M IST",tz=IST))
    fig.savefig(file,dpi=130); plt.close(fig)
    return file
