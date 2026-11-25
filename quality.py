"""Explicit coverage and readable source diagnostics; missing values stay missing."""
from domain import FRAMES


def coverage(rows):
    valid=0
    problems=[]
    summaries=[]
    for row in rows:
        metrics=row.get("metrics",{})
        missing=[tf for tf in FRAMES if metrics.get(tf,{}).get("rsi") is None]
        valid+=3-len(missing)
        if missing:
            reason=row.get("error") or row.get("indicator_status") or row.get("status","MISSING")
            problems.append(row.get("exchange","?")+": "+",".join(missing)+" ("+reason+")")
            summaries.append(row.get("exchange","?")+" "+str(3-len(missing))+"/3")
    expected=len(rows)*3
    return {"valid":valid,"expected":expected,"ratio":valid/expected if expected else None,
            "details":"; ".join(problems) or "All requested RSI values available",
            "summary":"; ".join(summaries) or "All six venues complete"}


def source_rows(sources):
    """One scalar per row, rather than Python repr dictionaries in Excel."""
    result=[]
    def visit(path,value):
        if isinstance(value,dict):
            if not value: result.append([path,"No reported errors" if path.endswith("errors") else "None"])
            for key,item in value.items(): visit(path+" / "+str(key),item)
        elif isinstance(value,(list,tuple)):
            result.append([path,", ".join(str(x) for x in value) or "None"])
        else: result.append([path,str(value) if value is not None else "Unavailable"])
    for key,value in sources.items(): visit(key,value)
    return result
