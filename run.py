"""Daily job: fetch new order announcements from BSE + NSE, update data/orders.csv,
refresh fundamentals and write docs/data.json for the dashboard.

  python -m screener.run              # daily: scan the last 4 days
  python -m screener.run --days 365   # first run / backfill
"""
import argparse
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from screener import core

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDERS_CSV = os.path.join(ROOT, "data", "orders.csv")
FUND_JSON = os.path.join(ROOT, "data", "fundamentals.json")
OUT_JSON = os.path.join(ROOT, "docs", "data.json")
IST = timezone(timedelta(hours=5, minutes=30))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
BSE_HEADERS = {"User-Agent": UA, "Referer": "https://www.bseindia.com/",
               "Origin": "https://www.bseindia.com", "Accept": "application/json, text/plain, */*"}
NSE_HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Accept-Language": "en-US,en;q=0.9",
               "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements"}


def date_chunks(start, end, step):
    s = start
    while s <= end:
        e = min(s + timedelta(days=step - 1), end)
        yield s, e
        s = e + timedelta(days=1)


# ── BSE ──────────────────────────────────────────────────────────────
def fetch_bse(start, end):
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    sess = requests.Session()
    sess.headers.update(BSE_HEADERS)
    rows = []
    for s, e in date_chunks(start, end, 15):
        page = 1
        while True:
            params = {"pageno": page, "strCat": "Company Update", "strPrevDate": s.strftime("%Y%m%d"),
                      "strScrip": "", "strSearch": "P", "strToDate": e.strftime("%Y%m%d"),
                      "strType": "C", "subcategory": "Award of Order / Receipt of Order"}
            try:
                data = sess.get(url, params=params, timeout=30).json()
            except Exception as ex:
                print(f"  BSE {s:%d-%b} p{page}: {ex}")
                break
            table = data.get("Table") or []
            if not table:
                break
            rows.extend(table)
            total = (data.get("Table1") or [{}])[0].get("ROWCNT", 0) or 0
            if page * 50 >= total:
                break
            page += 1
            time.sleep(0.4)
        print(f"  BSE {s:%d-%b-%y}→{e:%d-%b-%y}: {len(rows)} rows")
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    get = lambda c: df[c] if c in df else pd.Series([None] * len(df))
    att = get("ATTACHMENTNAME")
    return pd.DataFrame({
        "exchange": "BSE",
        "date": pd.to_datetime(get("NEWS_DT"), errors="coerce"),
        "company": get("SLONGNAME"),
        "code": get("SCRIP_CD").astype(str),
        "isin": None,
        "headline": (get("NEWSSUB").fillna("") + " | " + get("HEADLINE").fillna("")).str.strip(" |"),
        "text": get("MORE").fillna(""),
        "pdf_url": [f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{a}" if a else None for a in att],
    })


# ── NSE ──────────────────────────────────────────────────────────────
def nse_session():
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    for u in ("https://www.nseindia.com", NSE_HEADERS["Referer"]):
        try:
            s.get(u, timeout=30)
        except Exception:
            pass
    return s


def fetch_nse(start, end):
    api = "https://www.nseindia.com/api/corporate-announcements"
    sess = nse_session()
    rows = []
    for s, e in date_chunks(start, end, 7):
        params = {"index": "equities", "from_date": s.strftime("%d-%m-%Y"), "to_date": e.strftime("%d-%m-%Y")}
        data = []
        for attempt in range(3):
            try:
                r = sess.get(api, params=params, timeout=40)
                if r.status_code in (401, 403):
                    sess = nse_session()
                    raise RuntimeError(f"HTTP {r.status_code}")
                data = r.json()
                break
            except Exception as ex:
                print(f"  NSE {s:%d-%b} try {attempt + 1}: {ex}")
                time.sleep(3 + 3 * attempt)
        if isinstance(data, dict):
            data = data.get("data", [])
        rows.extend(data or [])
        print(f"  NSE {s:%d-%b-%y}→{e:%d-%b-%y}: {len(rows)} rows")
        time.sleep(1)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    get = lambda c: df[c] if c in df else pd.Series([None] * len(df))
    desc, text = get("desc").fillna(""), get("attchmntText").fillna("")
    out = pd.DataFrame({
        "exchange": "NSE",
        "date": pd.to_datetime(get("an_dt"), format="%d-%b-%Y %H:%M:%S", errors="coerce"),
        "company": get("sm_name"),
        "code": get("symbol"),
        "isin": get("sm_isin"),
        "headline": desc + " | " + text.str.slice(0, 200),
        "text": text,
        "pdf_url": get("attchmntFile"),
    })
    keep = desc.str.contains(r"order|contract", case=False, na=False) | text.str.contains(core.ORDER_KW, na=False)
    return out[keep.values]


# ── PDFs for missing values ──────────────────────────────────────────
def pdf_text(url, pages=3):
    import pdfplumber
    try:
        h = BSE_HEADERS if "bseindia" in str(url) else NSE_HEADERS
        r = requests.get(url, headers=h, timeout=30)
        if r.status_code != 200 or b"%PDF" not in r.content[:1024]:
            return ""
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            return " ".join((p.extract_text() or "") for p in pdf.pages[:pages])
    except Exception:
        return ""


def fill_from_pdfs(orders, max_pdfs):
    tried = orders.get("pdf_tried")
    if tried is None:
        orders["pdf_tried"] = False
    orders["pdf_tried"] = orders["pdf_tried"].fillna(False).astype(bool)
    need = orders[orders["order_cr"].isna() & orders["pdf_url"].notna() & ~orders["pdf_tried"]]
    need = need.sort_values("date", ascending=False).index[:max_pdfs]
    print(f"Reading {len(need)} PDFs for missing order values…")
    for i, idx in enumerate(need, 1):
        v, snip = core.extract_order_value(pdf_text(orders.at[idx, "pdf_url"]))
        orders.at[idx, "pdf_tried"] = True
        if not np.isnan(v):
            orders.at[idx, "order_cr"] = v
            orders.at[idx, "value_src"] = "pdf: " + snip
        if i % 50 == 0:
            print(f"  {i}/{len(need)}")
        time.sleep(0.3)
    return orders


# ── Fundamentals (cached, refreshed weekly) ──────────────────────────
def refresh_fundamentals(tickers, cache, today, max_calls=600, max_age_days=7):
    import yfinance as yf
    stale = [t for t in tickers
             if t not in cache or
             (today - datetime.fromisoformat(cache[t].get("updated", "2000-01-01"))).days >= max_age_days]
    stale = stale[:max_calls]
    print(f"Fetching fundamentals for {len(stale)} tickers…")
    for i, t in enumerate(stale, 1):
        rec = {"updated": today.strftime("%Y-%m-%d")}
        try:
            tk = yf.Ticker(t)
            info = tk.info or {}
            rev = info.get("totalRevenue")
            if not rev:   # fall back to the sum of the last 4 quarters
                q = tk.quarterly_income_stmt
                if q is not None and "Total Revenue" in q.index:
                    vals = q.loc["Total Revenue"].dropna().iloc[:4]
                    rev = float(vals.sum()) if len(vals) == 4 else None
            rec.update({
                "name": info.get("longName") or info.get("shortName"),
                "mcap_cr": (info.get("marketCap") or np.nan) / 1e7,
                "rev_ttm_cr": (rev or np.nan) / 1e7,
                "sector": info.get("sector"),
                "industry": info.get("industry"),
            })
        except Exception as ex:
            rec["error"] = str(ex)[:120]
        cache[t] = {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in rec.items()}
        if i % 50 == 0:
            print(f"  {i}/{len(stale)}")
        time.sleep(0.2)
    return cache


# ── Main ─────────────────────────────────────────────────────────────
def load_orders():
    if os.path.exists(ORDERS_CSV):
        df = pd.read_csv(ORDERS_CSV, dtype={"code": str, "id": str})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        return df
    return pd.DataFrame(columns=core.ORDER_COLS + ["pdf_tried"])


def main(days, max_pdfs, skip_nse=False):
    now = datetime.now(IST).replace(tzinfo=None)
    start = now - timedelta(days=days)
    print(f"Scanning {start:%d-%b-%Y} → {now:%d-%b-%Y}")

    raw = [fetch_bse(start, now)]
    if not skip_nse:
        raw.append(fetch_nse(start, now))
    raw = pd.concat([r for r in raw if not r.empty], ignore_index=True) if any(not r.empty for r in raw) else pd.DataFrame()
    new = core.prepare_new(raw, now)
    existing = load_orders()
    before = len(existing)
    orders = core.merge_orders(existing, new)
    print(f"Orders on file: {before} → {len(orders)} (+{len(orders) - before} new)")

    orders = fill_from_pdfs(orders, max_pdfs)
    os.makedirs(os.path.dirname(ORDERS_CSV), exist_ok=True)
    orders.to_csv(ORDERS_CSV, index=False)

    cache = json.load(open(FUND_JSON)) if os.path.exists(FUND_JSON) else {}
    window = orders[pd.to_datetime(orders["date"]) >= now - timedelta(days=core.LOOKBACK_DAYS)]
    tickers = sorted(set(core.ticker_for(window).values()))
    cache = refresh_fundamentals(tickers, cache, now)
    json.dump(cache, open(FUND_JSON, "w"), indent=0)

    data = core.build_dashboard_data(orders, cache, now)
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump(data, open(OUT_JSON, "w"), separators=(",", ":"))
    print(f"Dashboard data: {len(data['companies'])} companies, {len(data['orders'])} orders")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=4, help="days to scan (365 for the first run)")
    ap.add_argument("--max-pdfs", type=int, default=150, help="PDFs to open per run for missing values")
    ap.add_argument("--skip-nse", action="store_true")
    a = ap.parse_args()
    main(a.days, a.max_pdfs, a.skip_nse)
