import html, json, re, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

TV_URL = "https://scanner.tradingview.com/malaysia/scan"
COLUMNS = ["name", "description", "close", "change", "RSI", "RSI[1]", "RSI|1W", "RSI|1M",
           "market_cap_basic", "price_earnings_ttm", "price_52_week_high", "price_52_week_low",
           "sector", "industry", "volume", "Perf.1M", "debt_to_equity", "current_ratio",
           "total_revenue", "net_income", "free_cash_flow", "total_revenue_yoy_growth_fy",
           "net_income_yoy_growth_fy", "isin"]
KEYS = ["tv_name", "name", "price", "chg", "rsi", "rsi_prev", "weekly_rsi", "monthly_rsi",
        "mcap", "pe", "high52", "low52", "sector", "industry", "volume", "perf1m", "de",
        "current_ratio", "revenue", "net_income", "fcf", "rev_growth", "ni_growth", "isin"]
MIN_UNIVERSE = 100
MIN_DATA_SHARE = 0.5

_MAP = ("1155:MAYBANK,1295:PBBANK,1023:CIMB,5819:HLBANK,5183:PETDAG,6033:PETGAS,5681:PCHEM,"
        "5225:IHH,6012:MAXIS,6888:AXIATA,5285:SIME,1961:IOICORP,2445:KLK,4707:NESTLE,5248:GAMUDA,"
        "3182:GENTING,4715:GENM,3816:MISC,5031:SIMEPROP,5109:TOPGLOV,7153:KOSSAN,5253:HARTA,"
        "5099:CAPITALA,3069:MFCB,5185:AFFIN,1818:BURSA,0166:INARI,2291:GENP,4197:SDG,5222:KPJ,"
        "4863:TM,7277:DIALOG,5398:YTL,8664:SPSETIA,5139:AHEALTH,7113:SUPERMX,4634:PHARMA,"
        "6084:YTLPOWR,5218:SAPEG,0080:D&O,7052:PADINI,7084:QL,5235:KLKPLT,5236:MATRIX,"
        "5005:UNISEM,5250:SUNWAY,7164:VS,5347:TENAGA,1015:AMBANK,1066:RHBBANK,1171:MBSB,"
        "0138:MYEG,0097:DND,0083:MI,7008:GREATECH,6947:DIGI,8869:PMETAL,5296:AEON,5301:F&N,"
        "3336:IJM,5263:SUNCON,5264:WCT,5288:IOIPG")
ALPHA_TO_NUM = {a: n for n, a in (p.split(":") for p in _MAP.split(","))}

_CORP = re.compile(r"\b(berhad|bhd\.?|holdings?|group|corp(oration)?|plc)\b", re.I)
_CATS = [("Earnings", ("profit", "earnings", "revenue", "quarter", "q1", "q2", "q3", "q4", "loss")),
         ("Dividend", ("dividend", "payout")),
         ("Deal / contract", ("contract", "award", "acquisition", "acquire", "joint venture", "deal", "m&a"))]


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _request(method, url, retries=3, **kw):
    delay = 2
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2


@st.cache_data(ttl=4 * 3600, show_spinner="Downloading Bursa universe from TradingView…")
def fetch_universe():
    body = {"filter": [], "options": {"active_symbols_only": True}, "markets": ["malaysia"],
            "symbols": {"query": {"types": []}, "tickers": []}, "columns": COLUMNS,
            "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"}, "range": [0, 1500]}
    r = _request("POST", TV_URL, json=body, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    data = r.json().get("data", [])
    df = parse_universe(data)
    if len(df) < MIN_UNIVERSE:
        raise RuntimeError(f"Universe has only {len(df)} stocks — TradingView may have changed.")
    ok = df["monthly_rsi"].notna() & df["perf1m"].notna()
    if ok.mean() < MIN_DATA_SHARE:
        raise RuntimeError("RSI/1M data missing for most stocks — TradingView column format changed.")
    return df


def parse_universe(items):
    rows = []
    for it in items:
        d = it.get("d", [])
        d = d + [None] * (len(KEYS) - len(d))
        row = dict(zip(KEYS, d))
        for k in KEYS:
            if k not in ("tv_name", "name", "sector", "industry", "isin"):
                row[k] = _num(row[k])
        code, src = "", "name"
        m = re.match(r"MYL(\d{4})", row["isin"] or "")
        if m:
            code, src = m.group(1), "isin"
        elif row["tv_name"] in ALPHA_TO_NUM:
            code, src = ALPHA_TO_NUM[row["tv_name"]], "map"
        row["symbol"] = f"{code}.KL" if code else f"{row['tv_name']}.KL"
        row["code_src"] = src
        row["tv_symbol"] = it.get("s", "")
        row["name"] = row["name"] or row["tv_name"]
        rows.append(row)
    df = pd.DataFrame(rows).astype(object)
    return df.where(df.notna(), None)


def parse_shariah_master(raw: bytes) -> dict:
    """Accepts shariah-master.json (SC SAC list) → {CODE: 'Compliant'|'Non-compliant'}."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Not valid JSON: {e}")
    data = doc.get("data", doc) if isinstance(doc, dict) else {}
    out = {}
    for k, v in data.items():
        k = str(k).strip().upper()
        status = "Non-compliant" if "non" in str(v).lower() else "Compliant"
        out[k] = status
        out[k.replace(".KL", "")] = status
    return out


def shariah_status(symbol, tv_name, sector, industry, master):
    keys = [symbol.upper(), symbol.upper().replace(".KL", ""), (tv_name or "").upper()]
    for k in keys:
        if k in master:
            return master[k]
    text = f"{sector or ''} {industry or ''}".lower()
    bad = ("bank", "casino", "gambling", "brewer", "alcohol", "tobacco", "distiller")
    if any(b in text for b in bad) and not any(i in (tv_name or "").upper() for i in ("BIMB", "TAKAFUL")):
        return "Likely non-compliant (sector estimate)"
    return "Unknown"


def screen(df, master, perf_max, rsi_min, rsi_max, w_rsi_max, shariah_only):
    rows = []
    for r in df.itertuples():
        if None in (r.perf1m, r.monthly_rsi, r.weekly_rsi, r.ni_growth):
            continue
        if not (r.perf1m <= perf_max and r.rsi_min_check if False else True):
            pass
        if not (r.perf1m <= perf_max
                and rsi_min <= r.monthly_rsi <= rsi_max
                and r.weekly_rsi < w_rsi_max
                and r.ni_growth > 0):
            continue
        checks = {
            "RevGrowth>3%": None if r.rev_growth is None else r.rev_growth > 3,
            "NI growth>0": None if r.ni_growth is None else r.ni_growth > 0,
            "FCF>0": None if r.fcf is None else r.fcf > 0,
            "CurrentRatio>1": None if r.current_ratio is None else r.current_ratio > 1,
            "D/E<1.5": None if r.de is None else r.de < 1.5,
        }
        if checks["CurrentRatio>1"] is False and checks["FCF>0"] is False:
            continue
        passed = sum(v is True for v in checks.values())
        known = sum(v is not None for v in checks.values())
        nd = len(checks) - known
        score = f"{passed}/{len(checks)}" + (f" ({nd} n/d)" if nd else "")
        status = shariah_status(r.symbol, r.tv_name, r.sector, r.industry, master)
        if shariah_only and status != "Compliant":
            continue
        rows.append({
            "symbol": r.symbol, "name": r.name,
            "price (RM)": r.price, "1M %": r.perf1m,
            "mcap (RM M)": round(r.mcap / 1e6, 1) if r.mcap else None,
            "weekly RSI": r.weekly_rsi, "monthly RSI": r.monthly_rsi,
            "NI growth %": r.ni_growth, "PE": r.pe,
            "score": score, "passed": passed,
            "shariah": status, "sector": r.sector,
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["passed", "mcap (RM M)"], ascending=False).drop(columns=["passed"])
    return out


def _norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _category(title):
    t = title.lower()
    return next((c for c, kw in _CATS if any(k in t for k in kw)), "General")


def fetch_news(name, tv_name="", days=30, limit=5):
    core = _CORP.sub("", name).strip(" ,.-") or name
    q = f'"{core}" (Bursa OR Malaysia OR shares OR profit OR dividend) when:{days}d'
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=en-MY&gl=MY&ceid=MY:en"
    r = _request("GET", url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    root = ET.fromstring(r.text)
    need = [_norm(core)] + ([_norm(tv_name)] if len(tv_name) >= 4 else [])
    seen, items = set(), []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if not title or not link or not pub:
            continue
        try:
            when = parsedate_to_datetime(pub)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if when < cutoff or when > now + timedelta(hours=1):
            continue
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        elif " - " in title:
            title, _, source = title.rpartition(" - ")
        key = _norm(title)
        if key in seen or not any(n and n in key for n in need):
            continue
        seen.add(key)
        items.append({"date": when.astimezone().strftime("%Y-%m-%d %H:%M"),
                      "title": title, "source": source or "unknown",
                      "category": _category(title), "url": link})
    return items[:limit]


# ============================ UI ============================
st.set_page_config(page_title="Bursa Screener", page_icon="📈", layout="wide")
st.title("📈 Bursa Malaysia Stock Screener")
st.caption("Data: TradingView scanner · News: Google News RSS · Shariah: your uploaded master file")

with st.sidebar:
    st.header("⚙️ Filters")
    perf_max = st.slider("1-month performance ≤ (%)", -30.0, 5.0, -3.0, 0.5)
    rsi_min, rsi_max = st.slider("Monthly RSI range", 0, 100, (45, 65))
    w_rsi_max = st.slider("Weekly RSI <", 0, 100, 55)
    shariah_only = st.checkbox("Shariah-compliant only", value=False)

    st.divider()
    st.header("🕌 Shariah master file")
    sh_file = st.file_uploader(
        "Upload shariah-master.json",
        type=["json"],
        help="SC SAC list. Re-upload whenever it's updated — no redeploy needed.",
    )
    master = {}
    if sh_file is not None:
        try:
            master = parse_shariah_master(sh_file.getvalue())
            st.success(f"Loaded {len(master) // 2} stocks from master file.")
        except ValueError as e:
            st.error(f"Could not read file: {e}")

    st.divider()
    st.header("📰 News")
    news_days = st.number_input("News window (days)", 1, 90, 30)
    news_limit = st.number_input("Max articles per stock", 1, 20, 5)

    st.divider()
    if st.button("🔄 Refresh data now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ---------- run screen ----------
try:
    df = fetch_universe()
except Exception as e:
    st.error(f"Could not load market data: {e}")
    st.stop()

st.caption(f"Universe: **{len(df)}** stocks · "
           f"last refreshed {datetime.now().strftime('%H:%M')} (cached up to 4 h)")

res = screen(df, master, perf_max, rsi_min, rsi_max, w_rsi_max, shariah_only)

if res.empty:
    st.warning("No stocks passed the filters. Loosen the sliders or check the Shariah file.")
else:
    c1, c2, c3 = st.columns(3)
    c1.metric("Matches", len(res))
    c2.metric("Shariah-compliant", int((res["shariah"] == "Compliant").sum()))
    c3.metric("Median 1M %", f"{res['1M %'].median():.1f}")

    st.dataframe(
        res, use_container_width=True, hide_index=True,
        column_config={
            "price (RM)": st.column_config.NumberColumn(format="%.2f"),
            "1M %": st.column_config.NumberColumn(format="%.2f"),
            "mcap (RM M)": st.column_config.NumberColumn(format="%.0f"),
            "weekly RSI": st.column_config.NumberColumn(format="%.1f"),
            "monthly RSI": st.column_config.NumberColumn(format="%.1f"),
            "NI growth %": st.column_config.NumberColumn(format="%.1f"),
            "PE": st.column_config.NumberColumn(format="%.1f"),
        },
    )

    st.download_button(
        "⬇️ Download results (CSV)",
        res.to_csv(index=False).encode("utf-8"),
        file_name=f"bursa_screen_{datetime.now():%Y%m%d_%H%M}.csv",
        mime="text/csv",
    )

    st.divider()
    st.subheader("📰 News (on demand)")
    st.caption("Pick a stock and click Fetch — news is only downloaded when you ask for it.")
    pick = st.selectbox("Stock", res["symbol"] + " — " + res["name"], index=0)
    if st.button("Fetch news for selected stock"):
        sym = pick.split(" — ")[0]
        row = df[df["symbol"] == sym].iloc[0]
        with st.spinner("Fetching real news…"):
            try:
                items = fetch_news(row["name"], row["tv_name"], int(news_days), int(news_limit))
            except Exception as e:
                st.error(f"News source unavailable right now: {e}")
                items = None
        if items is not None:
            if not items:
                st.info("No recent news found in the chosen window.")
            for i in items:
                st.markdown(f"**[{i['category']}]** {i['title']}  \n"
                            f"*{i['source']} · {i['date']}*  \n"
                            f"[Read article]({i['url']})")

    st.divider()
    weak = res[res["shariah"] == "Unknown"]
    if not weak.empty:
        st.warning(f"Shariah status unknown for {len(weak)} stock(s) — "
                   f"upload an updated master file to fill these in.")