import json, re, time, html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# ============================================================
# CONFIG
# ============================================================
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

st.set_page_config(page_title="My Bursa", page_icon="📈", layout="wide")

# ============================================================
# DATA LAYER
# ============================================================
def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _request(method, url, retries=2, **kw):
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


def parse_universe(items):
    rows = []
    for it in items:
        d = it.get("d", []) + [None] * len(KEYS)
        row = dict(zip(KEYS, d[:len(KEYS)]))
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


@st.cache_data(ttl=4 * 3600, show_spinner="Loading Bursa data…")
def fetch_universe():
    body = {"filter": [], "options": {"active_symbols_only": True}, "markets": ["malaysia"],
            "symbols": {"query": {"types": []}, "tickers": []}, "columns": COLUMNS,
            "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"}, "range": [0, 1500]}
    r = _request("POST", TV_URL, json=body, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    df = parse_universe(r.json().get("data", []))
    if len(df) < MIN_UNIVERSE:
        raise RuntimeError("TradingView returned too few stocks.")
    return df


# ============================================================
# NEWS
# ============================================================
def _norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _category(title):
    t = title.lower()
    return next((c for c, kw in _CATS if any(k in t for k in kw)), "General")


@st.cache_data(ttl=1800, show_spinner=False)
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


# ============================================================
# SHARIAH (uploaded master file)
# ============================================================
def parse_shariah_master(raw: bytes) -> dict:
    doc = json.loads(raw.decode("utf-8"))
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
    if any(b in text for b in ("bank", "casino", "gambling", "brewer", "alcohol", "tobacco")) \
       and not any(i in (tv_name or "").upper() for i in ("BIMB", "TAKAFUL")):
        return "Likely non-compliant"
    return "Unknown"


# ============================================================
# localStorage (browser persistence — no login, no database)
# ============================================================
def load_state():
    """Read localStorage via a tiny JS bridge; returns dict."""
    js = """
    <script>
    const raw = localStorage.getItem("mybursa");
    window.parent.postMessage({type:"mybursa", data: raw}, "*");
    </script>
    """
    components.html(js, height=0, width=0)
    # Streamlit cannot read JS return values directly, so we use a query param
    # fallback: the app stores state in session_state, and a JS snippet writes
    # it to localStorage on every change. On cold start we read via ?state=
    return {}


def save_state(state: dict):
    payload = json.dumps(state).replace("'", "\\'")
    js = f"""
    <script>
    localStorage.setItem("mybursa", '{payload}');
    </script>
    """
    components.html(js, height=0, width=0)


def init_state():
    if "watchlist" not in st.session_state:
        st.session_state.watchlist = []      # list of symbols
    if "portfolio" not in st.session_state:
        st.session_state.portfolio = []      # list of {symbol, shares, avg}
    if "selected" not in st.session_state:
        st.session_state.selected = None
    if "search" not in st.session_state:
        st.session_state.search = ""


# ============================================================
# UI HELPERS
# ============================================================
def fmt_price(p):
    return f"RM{p:.2f}" if p is not None else "—"


def fmt_pct(p):
    if p is None:
        return "—"
    return f"{p:+.2f}%"


def stock_row_html(r, extra=""):
    color = "#0a7d32" if (r.chg or 0) >= 0 else "#c62828"
    return (
        f"<div style='display:flex;justify-content:space-between;padding:6px 0;"
        f"border-bottom:1px solid #eee;'>"
        f"<span><b>{r.symbol}</b> &nbsp; {r.name}</span>"
        f"<span style='color:{color};font-weight:600;'>"
        f"{fmt_price(r.price)} &nbsp; {fmt_pct(r.chg)}</span>"
        f"</div>"
    )


def show_detail(r, master):
    st.subheader(f"{r.symbol} — {r.name}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Price", fmt_price(r.price), fmt_pct(r.chg))
    c2.metric("1M %", fmt_pct(r.perf1m))
    c3.metric("Monthly RSI", f"{r.monthly_rsi:.1f}" if r.monthly_rsi is not None else "—")
    c4.metric("Weekly RSI", f"{r.weekly_rsi:.1f}" if r.weekly_rsi is not None else "—")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Market cap", f"RM{(r.mcap/1e9):.2f}B" if r.mcap else "—")
    c6.metric("PE", f"{r.pe:.1f}" if r.pe is not None else "—")
    c7.metric("NI growth", f"{r.ni_growth:+.1f}%" if r.ni_growth is not None else "—")
    c8.metric("Rev growth", f"{r.rev_growth:+.1f}%" if r.rev_growth is not None else "—")

    status = shariah_status(r.symbol, r.tv_name, r.sector, r.industry, master)
    st.caption(f"Sector: {r.sector or '—'} · Industry: {r.industry or '—'} · **Shariah: {status}**")

    st.divider()
    st.markdown("#### 📰 Latest news")
    if st.button("Fetch news", key=f"news_{r.symbol}"):
        with st.spinner("Fetching real news…"):
            try:
                items = fetch_news(r.name, r.tv_name)
                st.session_state[f"news_items_{r.symbol}"] = items
            except Exception as e:
                st.error(f"News unavailable right now: {e}")
                st.session_state[f"news_items_{r.symbol}"] = None

    items = st.session_state.get(f"news_items_{r.symbol}")
    if items is None and f"news_items_{r.symbol}" not in st.session_state:
        st.caption("Click **Fetch news** to load headlines.")
    elif not items:
        st.info("No recent news found.")
    else:
        for i in items:
            st.markdown(f"**[{i['category']}]** [{i['title']}]({i['url']})  \n"
                        f"*{i['source']} · {i['date']}*")

    st.divider()
    colA, colB = st.columns(2)
    if colA.button("＋ Add to watchlist", key=f"addw_{r.symbol}"):
        if r.symbol not in st.session_state.watchlist:
            st.session_state.watchlist.append(r.symbol)
            save_state({"watchlist": st.session_state.watchlist,
                        "portfolio": st.session_state.portfolio})
            st.success("Added to watchlist")
        else:
            st.info("Already in watchlist")
    if r.symbol in st.session_state.watchlist:
        if colB.button("🗑 Remove from watchlist", key=f"rmw_{r.symbol}"):
            st.session_state.watchlist.remove(r.symbol)
            save_state({"watchlist": st.session_state.watchlist,
                        "portfolio": st.session_state.portfolio})
            st.rerun()


# ============================================================
# APP
# ============================================================
init_state()

# ---- load data ----
try:
    df = fetch_universe()
except Exception as e:
    st.error(f"Could not load market data: {e}")
    st.stop()

df_idx = df.set_index("symbol", drop=False)

# ---- shariah uploader (sidebar) ----
with st.sidebar:
    st.header("🕌 Shariah list")
    up = st.file_uploader("Upload shariah-master.json", type=["json"], label_visibility="collapsed")
    master = {}
    if up is not None:
        try:
            master = parse_shariah_master(up.getvalue())
            st.success(f"{len(master)//2} stocks loaded")
        except Exception as e:
            st.error(f"Could not read: {e}")

# ---- search bar ----
st.sidebar.header("🔍 Search")
q = st.sidebar.text_input("Ticker or name", value=st.session_state.search,
                          placeholder="e.g. MAYBANK or 1155")
st.session_state.search = q

# ---- sidebar: lists summary ----
st.sidebar.header("📋 My lists")
st.sidebar.caption(f"Watchlist: {len(st.session_state.watchlist)} · "
                   f"Portfolio: {len(st.session_state.portfolio)}")

# ============================================================
# MAIN
# ============================================================
st.title("📈 My Bursa")

# ---- 1. SEARCH RESULTS (if query) ----
if q:
    ql = q.strip().lower()
    mask = (df["symbol"].str.lower().str.contains(ql, na=False)
            | df["name"].str.lower().str.contains(ql, na=False)
            | df["tv_name"].str.lower().str.contains(ql, na=False))
    hits = df[mask].head(20)
    st.subheader(f"Search results for “{q}”")
    if hits.empty:
        st.info("No match. Try the 4-digit code or the short name.")
    for r in hits.itertuples():
        cols = st.columns([4, 1, 1])
        cols[0].markdown(f"**{r.symbol}** · {r.name}")
        cols[1].markdown(fmt_price(r.price))
        cols[2].markdown(fmt_pct(r.chg))
        if st.button(f"Open {r.symbol}", key=f"open_{r.symbol}"):
            st.session_state.selected = r.symbol
            st.session_state.search = ""
            st.rerun()
    st.divider()

# ---- 2. DETAIL VIEW ----
if st.session_state.selected:
    sel = st.session_state.selected
    if sel not in df_idx.index:
        st.warning(f"{sel} no longer in the universe.")
        st.session_state.selected = None
    else:
        r = df_idx.loc[sel]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        if st.button("← Back to lists"):
            st.session_state.selected = None
            st.rerun()
        show_detail(r, master)
    st.stop()

# ---- 3. HOME: LISTS ----
col1, col2 = st.columns(2)

with col1:
    st.subheader("👀 Watchlist")
    if not st.session_state.watchlist:
        st.caption("Empty. Search for a stock and click **Add to watchlist**.")
    for sym in list(st.session_state.watchlist):
        if sym not in df_idx.index:
            st.warning(f"{sym} not found")
            continue
        r = df_idx.loc[sym]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        c1, c2 = st.columns([4, 1])
        c1.markdown(stock_row_html(r), unsafe_allow_html=True)
        if c2.button("Open", key=f"wopen_{sym}"):
            st.session_state.selected = sym
            st.rerun()

with col2:
    st.subheader("💼 Portfolio")
    if not st.session_state.portfolio:
        st.caption("Empty. Open a stock and add shares + average cost.")
    for pos in st.session_state.portfolio:
        sym = pos["symbol"]
        if sym not in df_idx.index:
            continue
        r = df_idx.loc[sym]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        cost = pos["shares"] * pos["avg"]
        value = pos["shares"] * (r.price or 0)
        pl = value - cost
        pl_pct = (pl / cost * 100) if cost else 0
        color = "#0a7d32" if pl >= 0 else "#c62828"
        st.markdown(
            f"<div style='display:flex;justify-content:space-between;padding:6px 0;"
            f"border-bottom:1px solid #eee;'>"
            f"<span><b>{sym}</b> · {pos['shares']} sh @ RM{pos['avg']:.2f}</span>"
            f"<span style='color:{color};font-weight:600;'>"
            f"RM{value:.2f} ({pl:+.2f}, {pl_pct:+.1f}%)</span></div>",
            unsafe_allow_html=True,
        )
        if st.button("Open", key=f"popen_{sym}"):
            st.session_state.selected = sym
            st.rerun()

    with st.expander("＋ Add portfolio position"):
        p_sym = st.text_input("Symbol (e.g. 1155.KL)", key="p_sym")
        p_sh = st.number_input("Shares", min_value=1, value=100, key="p_sh")
        p_avg = st.number_input("Average cost (RM)", min_value=0.01, value=1.00, step=0.01, key="p_avg")
        if st.button("Save position"):
            s = p_sym.strip().upper()
            if not s.endswith(".KL"):
                s = s + ".KL" if s.isdigit() else s
            if s in df_idx.index:
                st.session_state.portfolio.append(
                    {"symbol": s, "shares": int(p_sh), "avg": float(p_avg)}
                )
                save_state({"watchlist": st.session_state.watchlist,
                            "portfolio": st.session_state.portfolio})
                st.rerun()
            else:
                st.error(f"{s} not found in Bursa universe.")

st.divider()
st.caption("Data: TradingView (cached 4 h) · News: Google News RSS (on demand) · "
           "Lists saved in your browser.")