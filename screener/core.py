"""Pure logic: order filtering, value extraction, de-duplication and the dashboard data build.
No network calls here, so it can be tested offline."""
import re
import html
import hashlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# ── Settings ─────────────────────────────────────────────────────────
USD_INR = 88.0
EUR_INR = 95.0
LOOKBACK_DAYS = 365          # window for the ranking (% of TTM revenue)
RECENT_DAYS = 90
BIG_ORDER_PCT = 25
MEANINGFUL_PCT = 10

ORDER_KW = re.compile(
    r"\b(order|orders|work order|purchase order|letter of award|letter of acceptance|"
    r"LoA|LoI|letter of intent|contract|contracts|bagged|bags|secured|secures|awarded|"
    r"receipt of order|award of order|wins?|won|L-?1 bidder|lowest bidder)\b", re.I)

NOISE_KW = re.compile(
    r"(court order|order of (the )?(hon|nclt|nclat|sat|sebi|high court|supreme)|"
    r"\bNCLT\b|\bNCLAT\b|tax order|assessment order|demand order|penalty|show cause|"
    r"\bGST\b.{0,60}order|order (passed|received|issued) (by|from) .{0,60}(tax|gst|commissioner|authority|tribunal)|"
    r"in order to|trading window|record date|newspaper|investor meet|analyst|earnings call|"
    r"\bESOP|allotment|postal ballot|intimation of loss|credit rating)", re.I)

# ── Value extraction ─────────────────────────────────────────────────
_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CUR = r"(?P<cur>₹|Rs\.?|INR|Rupees|USD|US\s?\$|\$|EUR|€)"
_UNIT = r"(?P<unit>crores?|cr\.?|lakhs?|lacs?|lkh|million|mn|billion|bn)"
PAT_A = re.compile(_CUR + r"\s*(?P<num>" + _NUM + r")\s*/?-?\s*" + _UNIT + r"?\b", re.I)
PAT_B = re.compile(r"(?P<num>" + _NUM + r")\s*" + _UNIT + r"\b", re.I)

EXCLUDE_CTX = re.compile(
    r"(order\s*book|order\s*backlog|turnover|revenue|net\s*worth|market\s*cap|cumulative|"
    r"total orders|so far|till date|ytd|year to date|paid.?up|share capital|bank guarantee|"
    r"performance guarantee|\bEMD\b|earnest|assets|\bAUM\b|under management|deposits|balance sheet|"
    r"loan book|valuation|consolidated|annual|12 months|fiscal|FY\s?\d|spend|budget|market size|"
    r"portfolio|loans?\b|lending|advances|disburs|credit book|book size|gross npa|npa|asset base|"
    r"transactions|transaction volume|payments? processed|processing|volumes?|customer base|footprint|"
    r"wallet|premium|insured|capital of|raised|funding|ipo|"
    r"industry|economy|sales of|company of|enterprise of)", re.I)
# Words right AFTER an amount that mean it describes a size, not an order ("a $14 billion company")
AFTER_EXCLUDE = re.compile(
    r"^\W{0,3}(?:(?!(?:for|from|to|by|with|towards|under|on|of|in)\b)\w+\W+){0,2}?(company|companies|enterprise|conglomerate|group|bank|lender|insurer|"
    r"organi[sz]ation|firm|retailer|economy|industry|market|in assets|of assets|assets|AUM|revenue|"
    r"turnover|deposits|valuation|business|in sales|annual|of loans|in loans|loan|portfolio|"
    r"of transactions|in transactions|aum|customers|order\s*book|order\s*backlog|order\s*pipeline|"
    r"backlog|pipeline|unexecuted|outstanding orders)", re.I)
# 2) the amount's own sentence must talk about an order / contract / deal
ORDER_SENTENCE = re.compile(
    r"(order|contract|deal|\bTCV\b|total contract|LoA|LoI|letter of|award|purchase|work order|"
    r"project|tender|bid|supply|EPC|engagement|received|bagged|secured|won|wins|signed|works|"
    r"services|installation|commissioning|construction|development|manufactur|delivery|implementation)", re.I)
FROM_CLIENT = re.compile(r"\bfrom\s+(?:M/s\.?\s*)?(?:the\s+)?[A-Z]")   # "Rs 45 Cr from Central Railway"
SENT_SPLIT = re.compile(r"(?<=[.;!?])\s+(?=[A-Z₹(\"'])|\s+\|\s+")
# A single order this big vs the company is usually a parsing error → flag it for checking
SUSPECT_PCT_REV = 300     # > 3× TTM revenue, or …
SUSPECT_PCT_MCAP = 25     # … > 25% of market cap


def is_suspect(v, rev, mcap):
    if v != v or v is None:
        return False
    return bool((rev and v / rev * 100 > SUSPECT_PCT_REV) or (mcap and v / mcap * 100 > SUSPECT_PCT_MCAP))
FAVOUR_CTX = re.compile(
    r"(worth|valued|value|amount|aggregat|consideration|contract price|order of|order for|"
    r"order from|LoA|letter of|work order|purchase order)", re.I)


def to_crore(num, unit, cur):
    v = float(num.replace(",", ""))
    unit = (unit or "").lower().rstrip(".")
    cur = (cur or "").lower()
    if unit.startswith(("lakh", "lac", "lkh")):
        v /= 100
    elif unit in ("million", "mn"):
        v /= 10
    elif unit in ("billion", "bn"):
        v *= 100
    elif unit.startswith("cr"):
        pass
    else:
        v /= 1e7  # plain rupees
    if "usd" in cur or "$" in cur:
        v *= USD_INR
    elif "eur" in cur or "€" in cur:
        v *= EUR_INR
    return v


# ── PDF rupee-symbol repair ──────────────────────────────────────────
# Many Indian filings draw "₹" with a special rupee font. PDF text readers often turn that glyph
# into a plain character such as "2" or "`", so "₹22 crores" is read as "222 crores".
# We look at each character's font: a lone character in a different font family that sits directly
# in front of a number is the rupee glyph, not part of the number.
RUPEE_FONT = re.compile(r"rupee|inr|foradian|rupi", re.I)


def _font_family(name):
    name = str(name or "")
    name = name.split("+", 1)[-1]                                    # drop subset prefix "ABCDEF+"
    name = re.split(r"[-,]", name, 1)[0]                              # drop "-Bold", ",Italic" …
    return re.sub(r"(Bold|Italic|Oblique|Regular|Medium|Light|Black|Semibold|MT|PS)+$", "", name, flags=re.I).lower()


def repair_rupee_chars(chars):
    """Fix pdfplumber char dicts in place. Returns how many characters were turned into ₹."""
    fixed = 0
    n = len(chars)
    for i, ch in enumerate(chars):
        t = ch.get("text", "")
        if t == "₹" or len(t) != 1:
            continue
        # 1) any character drawn in a rupee font is the rupee sign
        if RUPEE_FONT.search(str(ch.get("fontname", ""))) and not t.isalpha():
            ch["text"] = "₹"; fixed += 1; continue
        # next visible character
        j = i + 1
        while j < n and chars[j].get("text", "").isspace():
            j += 1
        if j >= n or not chars[j].get("text", "").isdigit():
            continue
        # 2) a backtick right before a number is the Rupee Foradian glyph
        if t == "`":
            ch["text"] = "₹"; fixed += 1; continue
        # 3) a single digit in a different font family, directly before a number
        if t.isdigit():
            fam, nxt = _font_family(ch.get("fontname")), _font_family(chars[j].get("fontname"))
            prev = chars[i - 1] if i > 0 else None
            prev_same_run = prev is not None and prev.get("text", "").isalnum() and \
                _font_family(prev.get("fontname")) == fam
            if fam != nxt and not prev_same_run and j == i + 1:
                ch["text"] = "₹"; fixed += 1
    return fixed


def _sentence(t, start, end):
    """The sentence (or ' | '-separated headline part) that contains t[start:end]."""
    a = 0
    for sm in SENT_SPLIT.finditer(t):
        if sm.end() <= start:
            a = sm.end()
        elif sm.start() >= end:
            return t[a: sm.start()]
    return t[a:]


def extract_order_value(text):
    """Return (value in ₹ crore, snippet) or (nan, None)."""
    if not isinstance(text, str) or not text.strip():
        return np.nan, None
    t = re.sub(r"\s+", " ", html.unescape(text))      # "&#8377;" → "₹"
    cands, seen = [], set()
    for pat in (PAT_A, PAT_B):
        for m in pat.finditer(t):
            if m.start() in seen:
                continue
            gd = m.groupdict()
            if pat is PAT_A and not gd.get("unit"):
                if float(gd["num"].replace(",", "")) < 1e5:   # "Rs 500" alone is not an order size
                    continue
            seen.update(range(m.start(), m.end()))
            before = t[max(0, m.start() - 40): m.start()]
            before = re.split(r"[.;]\s+(?=[A-Z])", before)[-1]  # same sentence only
            if EXCLUDE_CTX.search(before) or AFTER_EXCLUDE.search(t[m.end(): m.end() + 30]):
                continue
            sent = _sentence(t, m.start(), m.end())
            if not (ORDER_SENTENCE.search(sent) or FROM_CLIENT.search(sent)):
                continue
            try:
                v = to_crore(gd["num"], gd.get("unit"), gd.get("cur"))
            except ValueError:
                continue
            if v <= 0 or v > 200_000:
                continue
            score = (2 if FAVOUR_CTX.search(before) else 0) + (1 if pat is PAT_A else 0)
            cands.append((score, m.start(), v, t[max(0, m.start() - 40): m.end() + 10]))
    if not cands:
        return np.nan, None
    cands.sort(key=lambda c: (-c[0], c[1]))
    return round(cands[0][2], 2), cands[0][3].strip()


# NSE filing categories. Only these can be order wins; everything else (tax/penalty actions,
# litigation, bonds, schemes, shareholder notices …) is thrown out, whatever its text says.
NSE_ORDER_CATS = re.compile(r"^\s*(bagging|awarding|receiving).{0,40}(order|contract)", re.I)
NSE_MAYBE_CATS = re.compile(r"^\s*(press release|updates?|general updates?|news|outcome of|agreements?|"
                            r"business update)", re.I)
# For press releases / updates the text itself must clearly say the company WON an order
STRONG_WIN = re.compile(
    r"(receiv\w*|receipt of|bag(s|ged)?|secur\w*|won|wins?|award(ed)? (to|of|with)|been awarded|"
    r"declared (as )?(the )?L-?1|lowest bidder|letter of (award|acceptance|intent)|\bLoA\b|\bLoI\b|"
    r"work orders?|purchase orders?|orders? (worth|valued|of rs|of ₹|of inr|aggregat)|"
    r"contracts? (worth|valued|of rs|of ₹))", re.I)
REG_NOISE = re.compile(
    r"(action\(?s\)? (taken|initiated)|orders? passed|\btax\b|penalt|\bfine\b|imposition|demand notice|"
    r"show.?cause|customs|duty|levy|\bGST\b|assessment|tribunal|appeal|court|NCLT|NCLAT|SEBI|"
    r"\bbonds?\b|debenture|\bNCDs?\b|\bAT1\b|rights issue|preferential|QIP|allotment|fund rais|"
    r"credit rating|dividend|buy.?back|amalgamation|merger|demerger|scheme of|pendency|litigation|"
    r"\bnotes\b|tender offer|medium.?term|\bMTN\b|borrowing|\bloan\b|"
    r"cancel|terminat|withdraw|revok|revocation|foreclos|short.?clos|annul)", re.I)


def is_order_win(exchange, headline, text):
    headline = headline or ""
    text = text or ""
    blob = f"{headline} {text}"
    if exchange == "BSE":   # BSE sub-category is already "Award of Order / Receipt of Order"
        return not (NOISE_KW.search(headline) or REG_NOISE.search(headline))
    cat = headline.split(" | ", 1)[0]
    if NSE_ORDER_CATS.search(cat):
        return not REG_NOISE.search(blob)
    if NSE_MAYBE_CATS.search(cat):
        return bool(STRONG_WIN.search(blob)) and not (NOISE_KW.search(blob) or REG_NOISE.search(blob))
    return False


def name_key(name):
    s = str(name or "").upper()
    s = re.sub(r"\b(LIMITED|LTD|THE|INDIA|PVT|PRIVATE|CO|COMPANY|CORPORATION|CORP)\b", "", s)
    return re.sub(r"[^A-Z0-9]", "", s)


def row_id(exchange, code, date, headline):
    raw = f"{exchange}|{code}|{pd.Timestamp(date):%Y%m%d%H%M}|{(headline or '')[:80]}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


ORDER_COLS = ["id", "exchange", "date", "company", "code", "isin", "name_key", "headline",
              "text", "pdf_url", "order_cr", "value_src", "first_seen", "parse_v"]


def prepare_new(raw, today):
    """raw: DataFrame with exchange,date,company,code,isin,headline,text,pdf_url → cleaned order rows."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=ORDER_COLS)
    df = raw.copy()
    for c in ("isin", "text", "pdf_url", "headline"):
        if c not in df:
            df[c] = None
    df["text"] = df["text"].fillna("").astype(str)
    df["headline"] = df["headline"].fillna("").astype(str)
    df = df[[is_order_win(e, h, t) for e, h, t in zip(df["exchange"], df["headline"], df["text"])]].copy()
    if df.empty:
        return pd.DataFrame(columns=ORDER_COLS)
    df["company"] = df["company"].fillna("").astype(str).str.strip()
    df["code"] = df["code"].astype(str)
    df["name_key"] = df["company"].map(name_key)
    df["id"] = [row_id(e, c, d, h) for e, c, d, h in zip(df["exchange"], df["code"], df["date"], df["headline"])]
    vals = [extract_order_value(f"{h} {t}") for h, t in zip(df["headline"], df["text"])]
    df["order_cr"] = [v[0] for v in vals]
    df["value_src"] = [("text: " + v[1]) if v[1] else None for v in vals]
    df["first_seen"] = today.strftime("%Y-%m-%d")
    df["parse_v"] = PARSE_VERSION
    df["text"] = df["text"].str.slice(0, 600)
    return df[ORDER_COLS]


def merge_orders(existing, new):
    """Append new rows, keep the first time each was seen, and drop the BSE/NSE duplicates of one order."""
    allr = pd.concat([existing, new], ignore_index=True)
    allr["date"] = pd.to_datetime(allr["date"], errors="coerce")
    allr = allr.dropna(subset=["date"])
    allr = allr.sort_values("first_seen").drop_duplicates("id", keep="first")
    allr["_day"] = allr["date"].dt.date
    allr["_val"] = pd.to_numeric(allr["order_cr"], errors="coerce").round(0).fillna(-1)
    # prefer the NSE row (it carries the trading symbol), then the earliest seen
    allr["_pref"] = (allr["exchange"] != "NSE").astype(int)
    allr = allr.sort_values(["_pref", "first_seen"])
    allr = allr.drop_duplicates(["name_key", "_day", "_val"], keep="first")
    # The same order is often announced again later (agreement signed, LoA → contract, press release).
    # Same company + same value (to ₹0.1 Cr) within 90 days → count it once.
    allr["_v1"] = pd.to_numeric(allr["order_cr"], errors="coerce").round(1)
    allr = allr.sort_values("date")
    dup = pd.Series(False, index=allr.index)
    for (_, v), g in allr[allr["_v1"] > 0].groupby(["name_key", "_v1"]):
        last = None
        for idx, d in g["date"].items():
            if last is not None and (d - last).days <= 90:
                dup[idx] = True
            else:
                last = d
    allr = allr[~dup].drop(columns="_v1")
    return allr.drop(columns=["_day", "_val", "_pref"]).sort_values("date", ascending=False).reset_index(drop=True)


def ticker_for(orders):
    """name_key → yfinance ticker (NSE symbol if we have one, else BSE code)."""
    nse = orders[orders["exchange"] == "NSE"].groupby("name_key")["code"].first()
    bse = orders[orders["exchange"] == "BSE"].groupby("name_key")["code"].first()
    out = {k: f"{v}.BO" for k, v in bse.items()}
    out.update({k: f"{v}.NS" for k, v in nse.items()})
    return out


def _f(x, nd=1):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(x) else round(x, nd)


N_QUARTERS = 8
PARSE_VERSION = 4   # bump when the value rules change → stored orders get re-checked


def quarter_starts(today, n=N_QUARTERS):
    """Start dates of the last n calendar quarters, oldest first (the last one is the current, partial quarter)."""
    y, q = today.year, (today.month - 1) // 3
    out = []
    for _ in range(n):
        out.append(pd.Timestamp(year=y, month=q * 3 + 1, day=1))
        q -= 1
        if q < 0:
            q, y = 3, y - 1
    return out[::-1]


def fy_label(ts):
    """Indian FY quarter label: Apr–Jun 2026 → 'Q1 FY27'."""
    fq = (ts.month - 4) % 12 // 3 + 1
    fy = ts.year + 1 if ts.month >= 4 else ts.year
    return f"Q{fq} FY{fy % 100:02d}"


def o_hist_start(orders):
    d = pd.to_datetime(orders["date"], errors="coerce").min()
    return None if pd.isna(d) else d.strftime("%Y-%m-%d")


def build_dashboard_data(orders, fundamentals, today):
    """Return the dict written to docs/data.json."""
    cutoff = pd.Timestamp(today - timedelta(days=LOOKBACK_DAYS))
    cut_recent = pd.Timestamp(today - timedelta(days=RECENT_DAYS))
    o = orders.copy()
    o["date"] = pd.to_datetime(o["date"])
    o["order_cr"] = pd.to_numeric(o["order_cr"], errors="coerce")

    # Quarterly order value for the last 8 quarters (uses the full history, not just 365 days)
    qs = quarter_starts(today)
    hist = o[o["date"] >= qs[0]].copy()
    hist["qi"] = sum((hist["date"] >= q).astype(int) for q in qs) - 1
    qval = hist.groupby(["name_key", "qi"])["order_cr"].sum(min_count=1)
    qcnt = hist.groupby(["name_key", "qi"]).size()

    o = o[o["date"] >= cutoff]
    tick = ticker_for(o)
    o["ticker"] = o["name_key"].map(tick)

    def fund(t, k):
        return (fundamentals.get(t) or {}).get(k)

    companies, order_rows = [], []
    for key, g in o.groupby("name_key"):
        t = g["ticker"].iloc[0]
        rev, mcap = fund(t, "rev_ttm_cr"), fund(t, "mcap_cr")
        total = g["order_cr"].sum(min_count=1)
        recent = g[g["date"] >= cut_recent]
        pct_rev = total / rev * 100 if (rev and total == total) else None
        nse_sym = g.loc[g["exchange"] == "NSE", "code"].head(1).tolist()
        bse_code = g.loc[g["exchange"] == "BSE", "code"].head(1).tolist()
        companies.append({
            "key": key,
            "name": fund(t, "name") or g.sort_values("exchange", ascending=False)["company"].iloc[0],
            "ticker": t,
            "nse": nse_sym[0] if nse_sym else None,
            "bse": bse_code[0] if bse_code else None,
            "sector": fund(t, "sector"),
            "industry": fund(t, "industry"),
            "mcap_cr": _f(mcap, 0),
            "rev_ttm_cr": _f(rev, 0),
            "n_orders": int(len(g)),
            "n_valued": int(g["order_cr"].notna().sum()),
            "n_check": int(sum(is_suspect(v, rev, mcap) for v in g["order_cr"])),
            "value_cr": _f(total),
            "pct_rev": _f(pct_rev),
            "pct_mcap": _f(total / mcap * 100 if (mcap and total == total) else None),
            "n_recent": int(len(recent)),
            "value_recent_cr": _f(recent["order_cr"].sum(min_count=1)),
            "biggest_cr": _f(g["order_cr"].max()),
            "last_date": g["date"].max().strftime("%Y-%m-%d"),
            "q_val": [_f(qval.get((key, i)), 0) for i in range(len(qs))],
            "q_n": [int(qcnt.get((key, i), 0)) for i in range(len(qs))],
        })
        for _, r in g.iterrows():
            v = r["order_cr"]
            order_rows.append({
                "key": key,
                "date": r["date"].strftime("%Y-%m-%d"),
                "ex": r["exchange"],
                "value_cr": _f(v, 2),
                "pct_rev": _f(v / rev * 100 if (rev and v == v) else None),
                "chk": is_suspect(v, rev, mcap),
                "headline": str(r["headline"] or "")[:240],
                "src": (str(r["value_src"])[:140] if isinstance(r["value_src"], str) else None),
                "pdf": r["pdf_url"] if isinstance(r["pdf_url"], str) else None,
                "seen": str(r["first_seen"]),
            })

    companies.sort(key=lambda c: (c["pct_rev"] is None, -(c["pct_rev"] or 0)))
    for i, c in enumerate(companies, 1):
        c["rank"] = i if c["pct_rev"] is not None else None
    order_rows.sort(key=lambda r: (r["date"], r["seen"]), reverse=True)
    return {
        "updated": today.strftime("%Y-%m-%d %H:%M"),
        "lookback_days": LOOKBACK_DAYS,
        "recent_days": RECENT_DAYS,
        "big_pct": BIG_ORDER_PCT,
        "meaningful_pct": MEANINGFUL_PCT,
        "q_labels": [fy_label(q) for q in qs],
        "history_from": o_hist_start(orders),
        "companies": companies,
        "orders": order_rows,
    }
