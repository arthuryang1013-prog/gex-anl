"""
nq_gex_collector.py -- multi-source GEX collector for the NQ GEX monitor (READ-ONLY).

Sources (all read from IBKR, all mapped into NQ price coordinates):
    NQ  = CME NQ futures options   (multiplier 20,  gamma: Black-76)
    QQQ = QQQ ETF options          (multiplier 100, gamma: Black-Scholes with dividend yield)
    NDX = Nasdaq-100 index options (multiplier 100, gamma: Black-Scholes with dividend yield)

What is stored for every contract of every snapshot (nq_gex.sqlite, read by nq_gex_dashboard.py):
    gex      = sign x gamma x OPEN INTEREST x multiplier x S^2 x 0.01     (previous-day positions)
    gex_vol  = sign x gamma x today's VOLUME x multiplier x S^2 x 0.01    (today's flow; this is what Barchart uses)
    strike_nq = strike x (NQ price / source price), with all three prices read at the same instant
                at the start of a cycle.

Behaviour
  * QQQ and NDX are only polled during US options hours (Mon-Fri 09:30-16:15 New York; holidays are not known to
    this script, so a holiday shows up as "no usable quotes" warnings). NQ is polled around the clock.
  * One cycle = every contract of every active source (~2,500 contracts, roughly 4-6 minutes).
  * Contracts that come back incomplete are retried once with a longer wait; if still incomplete they reuse their
    last good values and are flagged stale=1.
  * IBKR's flood of "Error 10090 / 10089 / 10091" market-data warnings is counted and summarised, not printed.
  * Read-only API connection: it never places orders.

Run:   python3 nq_gex_collector.py      (or use start_gex.command)    Stop with Ctrl+C.
Needs: pip install ib_async numpy pandas scipy

MODEL ASSUMPTIONS
  - Dealers long calls / short puts (a convention, not a verified fact; Barchart uses the same convention).
  - OI is the previous day's figure; volume is today's running total, with no buyer/seller or dealer/customer split.
  - Flip curves hold each contract's IV fixed while price moves (sticky-strike).
  - QQQ / NDX are mapped to NQ with one proportional ratio (ignores futures basis drift).
  - Dividend yields below are rough placeholders; set your own.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from ib_async import IB, Future, FuturesOption, Index, Option, Stock
from scipy.stats import norm

try:
    import gex_levels            # same folder; builds the TradingView levels after the US open
except Exception:
    gex_levels = None

# ----------------------------- config -----------------------------
HOST = "127.0.0.1"
PORT_OVERRIDE = None                      # e.g. 4001 to force one port
PORTS_TO_TRY = [4001, 4002, 7497, 7496]   # Gateway live/paper, TWS live/paper
CLIENT_ID = 12
MARKET_DATA_TYPE = 3                      # live if subscribed, otherwise delayed (to my knowledge)
INTERVAL_SECS = 180                       # target time between cycle starts (OI is cached, so a flow-only cycle is faster)
ENABLED = ["NQ", "QQQ", "NDX"]            # NQ must stay: it defines the price coordinates
SRC = {
    #        multiplier  strike step  band around spot  max days to expiry  dividend yield (None = futures option)
    "NQ":  dict(mult=20,  step=25, band=0.04, max_dte=7, q=None),
    "QQQ": dict(mult=100, step=1,  band=0.04, max_dte=7, q=0.006),
    "NDX": dict(mult=100, step=50, band=0.04, max_dte=7, q=0.007),
}
RISK_FREE = 0.04
BATCH = 80                # market-data lines per batch (+3 spot lines; IBKR's minimum allocation is 100)
SETTLE_SECS = 6                            # settle for the OI refresh (slow generic tick '101')
FLOW_SETTLE_SECS = 4                       # settle for intraday flow-only cycles (greeks+volume, OI from cache)
OI_REFRESH_SECS = 3 * 3600                 # re-pull OI at most once per 3h per source (OI is a prior-day ~daily figure)
RETRY_INCOMPLETE = True   # one more attempt, with a longer wait, for contracts that came back incomplete
RETRY_SETTLE_SECS = 10
US_OPEN, US_CLOSE = dt.time(9, 30), dt.time(16, 15)
USE_UNIVERSE_CACHE = True                  # 合約清單按「美東交易日」存成 universe_cache.json;同一天重啟收集器直接讀檔,不用重建
CACHE_MAX_SPOT_DRIFT = 0.015               # 快取建立時的現價與現在差超過 1.5% 就不用(履約價範圍是依現價切的)
PREBUILD_ENABLED = True                    # 開盤前預先建 QQQ/NDX 合約清單(開盤那一刻就能直接抓,不用等 20~30 分鐘)
PREBUILD_ET = dt.time(8, 0)                # 美東幾點開始預建(台北冬令 21:00、夏令 20:00);比開盤 09:30 早 1.5 小時
PREBUILD_RETRY_SECS = 600                  # 預建失敗後隔多久再試
PREWARM_OI = True                          # 預建後順便抓一次未平倉量(OI),開盤第一輪就不用走慢速 OI 流程
IGNORE_MARKET_HOURS = False               # True = poll QQQ/NDX at any time (e.g. for testing)
EXPORT_LEVELS = True                       # after the US open write levels_today.txt (for the TradingView indicator)
LEVELS_REEXPORT_ET = ["10:30"]              # refresh the levels at these New York times too (VOL levels are noise at 09:30); [] = first wave only
RETENTION_DAYS = None                      # None = keep every snapshot forever (needed for long replays); e.g. 14 = delete snapshots older than 14 days at start-up
QUIET_CODES = {"10089", "10090", "10091", "354", "300"}  # IBKR market-data warnings that are counted instead of printed
NY = ZoneInfo("America/New_York")
try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    HERE = os.getcwd()
DB_PATH = os.path.join(HERE, "nq_gex.sqlite")
CACHE_PATH = os.path.join(HERE, "universe_cache.json")
STATUS_PATH = os.path.join(HERE, "collector_status.json")   # 網站讀這個檔,把收集器現在在做什麼顯示在圖上

# ----------------------------- status file (for the web page) -----------------------------
# phase: starting / connecting(連不上 Gateway) / connected / building(建合約清單) / pulling(抓報價) / recording(正常) / error
# link : IBKR 與 Gateway 之間的連線狀態(ok / lost / unknown),由 IBKR 的 1100/1101/1102 訊息更新
# A background thread rewrites the file every few seconds, so "updated" is a heartbeat even while a slow
# IBKR call blocks the main loop; the web page treats a file that stopped updating as "collector not running".
_st_lock = threading.Lock()
_st = dict(phase="starting", detail="", since=time.time(), started=time.time(), pid=os.getpid(),
           link="unknown", link_msg="", retries=0, error="", last_snapshot=None, last_ok=None)


def _write_status_locked():
    _st["updated"] = time.time()
    tmp = STATUS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_st, f, ensure_ascii=False)
        os.replace(tmp, STATUS_PATH)
    except Exception:
        pass          # a status-file problem must never stop the collector


def set_status(phase=None, detail=None, **kw):
    with _st_lock:
        if phase is not None and phase != _st["phase"]:
            _st["phase"] = phase
            _st["since"] = time.time()
        if detail is not None:
            _st["detail"] = detail
        _st.update(kw)
        _write_status_locked()


def _status_heartbeat():
    while True:
        time.sleep(5)
        with _st_lock:
            _write_status_locked()


def _on_ib_error(reqId, errorCode, errorString, contract=None):
    """IBKR 的連線類訊息:1100 連線中斷 / 1101,1102 恢復 / 1300 通訊埠被關閉。其他(市場資料警告等)不管。"""
    if errorCode == 1100:
        set_status(link="lost", link_msg="IBKR 與 Gateway 之間斷線")
    elif errorCode in (1101, 1102):
        set_status(link="ok", link_msg="")
    elif errorCode == 1300:
        set_status(link="lost", link_msg="Gateway 的 API 通訊埠被關閉")


# ----------------------------- math -----------------------------
def bs_gamma(S, K, T, iv, r, q):
    """Black-Scholes gamma with continuous yield q. With q == r it equals Black-76 gamma on a future."""
    S, K, T, iv, r, q = (np.asarray(x, dtype=float) for x in (S, K, T, iv, r, q))
    sd = iv * np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * iv**2) * T) / sd
    return np.exp(-q * T) * norm.pdf(d1) / (S * sd)


def expiry_dt(expiry: str, detail):
    """IBKR's lastTradeTime + timeZoneId if both parse, else 16:00 ET."""
    d = dt.datetime.strptime(expiry, "%Y%m%d")
    lt = (getattr(detail, "lastTradeTime", "") or "").strip()
    tzid = (getattr(detail, "timeZoneId", "") or "").strip()
    if lt and tzid:
        try:
            t = dt.datetime.strptime(lt[:8], "%H:%M:%S")
            return d.replace(hour=t.hour, minute=t.minute, tzinfo=ZoneInfo(tzid))
        except Exception:
            pass
    return d.replace(hour=16, tzinfo=NY)


def profile(df: pd.DataFrame, grid: np.ndarray, nq_spot: float, wcol: str) -> np.ndarray:
    """Net GEX versus NQ price. Each contract's underlying moves proportionally with the NQ grid."""
    if df.empty:
        return np.zeros_like(grid)
    Fg = df["F"].values[:, None] * (grid[None, :] / nq_spot)
    g = bs_gamma(Fg, df["strike"].values[:, None], df["T"].values[:, None], df["iv"].values[:, None],
                 RISK_FREE, df["q"].values[:, None])
    sign = np.where(df["cp"].values == "C", 1.0, -1.0)[:, None]
    return (sign * g * df[wcol].values[:, None] * df["mult"].values[:, None] * Fg**2 * 0.01).sum(axis=0)


def find_flip(grid, prof, ref):
    flips = []
    for k in np.where(np.sign(prof[:-1]) * np.sign(prof[1:]) < 0)[0]:
        x0, x1, y0, y1 = grid[k], grid[k + 1], prof[k], prof[k + 1]
        flips.append(x0 - y0 * (x1 - x0) / (y1 - y0))
    return float(min(flips, key=lambda x: abs(x - ref))) if flips else None


def compute_snapshot(rows: list[dict], spots: dict, nq_spot: float):
    """rows need: src, conId, expiry, dte, cls, strike, cp, iv, F, oi, vol, mult, q, T, stale.
    Returns (DataFrame, summary dict, per-source dict)."""
    df = pd.DataFrame(rows)
    df["sign"] = np.where(df["cp"] == "C", 1, -1)
    df["gamma"] = bs_gamma(df["F"].values, df["strike"].values, df["T"].values, df["iv"].values, RISK_FREE, df["q"].values)
    df = df[np.isfinite(df["gamma"])].copy()
    unit = df["sign"] * df["gamma"] * df["mult"] * df["F"] ** 2 * 0.01      # $ per 1% move per unit of position
    df["gex"] = unit * df["oi"]
    df["gex_vol"] = unit * df["vol"]
    df["strike_nq"] = df["strike"] * df["src"].map(lambda s: nq_spot / spots[s])

    grid = np.linspace(nq_spot * 0.96, nq_spot * 1.04, 241)
    ex0 = df[df["dte"] > 0]
    summary = dict(
        spot=float(nq_spot),
        net_gex_all=float(df["gex"].sum()),
        net_gex_ex0=float(ex0["gex"].sum()),
        flip_all=find_flip(grid, profile(df, grid, nq_spot, "oi"), nq_spot),
        flip_ex0=find_flip(grid, profile(ex0, grid, nq_spot, "oi"), nq_spot) if len(ex0) else None,
        n_contracts=int(len(df)),
        n_stale=int(df["stale"].sum()),
        sources=",".join(s for s in ENABLED if s in set(df["src"])),
    )
    per_src = {}
    for s in summary["sources"].split(","):
        sub = df[df["src"] == s]
        ratio = nq_spot / spots[s]
        flip = find_flip(grid, profile(sub, grid, nq_spot, "oi"), nq_spot)
        flip_v = find_flip(grid, profile(sub, grid, nq_spot, "vol"), nq_spot)
        per_src[s] = dict(spot=spots[s], ratio=ratio, n=len(sub), stale=int(sub["stale"].sum()),
                          net_oi=float(sub["gex"].sum()), net_vol=float(sub["gex_vol"].sum()),
                          flip_oi=flip, flip_vol=flip_v)
    return df, summary, per_src


# ----------------------------- database -----------------------------
NEW_CONTRACT_COLS = [("src", "TEXT"), ("strike_nq", "REAL"), ("vol", "REAL"), ("gex_vol", "REAL"),
                     ("mult", "REAL"), ("q", "REAL")]


def init_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")  # lets the dashboard read while this script writes
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            ts TEXT PRIMARY KEY, spot REAL, net_gex_all REAL, net_gex_ex0 REAL,
            flip_all REAL, flip_ex0 REAL, n_contracts INTEGER, n_stale INTEGER);
        CREATE TABLE IF NOT EXISTS contracts (
            ts TEXT, expiry TEXT, dte INTEGER, cls TEXT, strike REAL, cp TEXT, oi REAL, iv REAL,
            F REAL, T REAL, gamma REAL, gex REAL, stale INTEGER);
        CREATE INDEX IF NOT EXISTS idx_contracts_ts ON contracts(ts);
        """
    )
    # upgrade an older NQ-only database in place; its history is kept (treated as NQ, OI-weighted)
    cols = {r[1] for r in con.execute("PRAGMA table_info(contracts)")}
    for name, typ in NEW_CONTRACT_COLS:
        if name not in cols:
            con.execute(f"ALTER TABLE contracts ADD COLUMN {name} {typ}")
    scols = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
    if "sources" not in scols:
        con.execute("ALTER TABLE snapshots ADD COLUMN sources TEXT")
    con.execute("UPDATE contracts SET src='NQ' WHERE src IS NULL")
    con.execute("UPDATE contracts SET strike_nq=strike WHERE strike_nq IS NULL")
    con.execute("UPDATE contracts SET mult=20 WHERE mult IS NULL")
    con.execute("UPDATE contracts SET q=? WHERE q IS NULL", (RISK_FREE,))
    con.execute("UPDATE snapshots SET sources='NQ' WHERE sources IS NULL")
    con.commit()
    return con


def _f(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def save_snapshot(con: sqlite3.Connection, ts: str, df: pd.DataFrame, s: dict):
    con.execute(
        "INSERT OR REPLACE INTO snapshots (ts, spot, net_gex_all, net_gex_ex0, flip_all, flip_ex0, n_contracts, n_stale, sources) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, s["spot"], s["net_gex_all"], s["net_gex_ex0"], _f(s["flip_all"]), _f(s["flip_ex0"]),
         s["n_contracts"], s["n_stale"], s["sources"]),
    )
    con.execute("DELETE FROM contracts WHERE ts=?", (ts,))  # a re-saved timestamp must not duplicate its rows
    con.executemany(
        "INSERT INTO contracts (ts, expiry, dte, cls, strike, cp, oi, iv, F, T, gamma, gex, stale, src, strike_nq, vol, gex_vol, mult, q) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, r.expiry, int(r.dte), r.cls, float(r.strike), r.cp, float(r.oi), float(r.iv), float(r.F), float(r.T),
          float(r.gamma), float(r.gex), int(r.stale), r.src, float(r.strike_nq), float(r.vol), float(r.gex_vol),
          float(r.mult), float(r.q)) for r in df.itertuples()],
    )
    con.commit()


def prune_old(con: sqlite3.Connection):
    """Optional: delete snapshots older than RETENTION_DAYS (run once at start-up)."""
    if not RETENTION_DAYS:
        return
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    n = con.execute("DELETE FROM contracts WHERE ts < ?", (cutoff,)).rowcount
    con.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
    con.commit()
    if n:
        print(f"Removed {n:,} contract rows older than {RETENTION_DAYS} days; compacting the file ...")
        con.execute("VACUUM")


# ----------------------------- IBKR helpers -----------------------------
class QuietFilter(logging.Filter):
    """Counts (instead of printing) the flood of market-data warnings that ib_async logs at ERROR level."""

    def __init__(self):
        super().__init__()
        self.counts = Counter()

    def filter(self, record) -> bool:
        m = re.match(r"Error (\d+),", record.getMessage())
        if m and m.group(1) in QUIET_CODES:
            self.counts[m.group(1)] += 1
            return False
        return True


def connect(ib: IB) -> int:
    tries = 0
    while True:
        for p in ([PORT_OVERRIDE] if PORT_OVERRIDE else PORTS_TO_TRY):
            try:
                ib.connect(HOST, p, clientId=CLIENT_ID, readonly=True, timeout=5)
                print(f"Connected to IBKR on port {p}")
                set_status("connected", f"已連上 IBKR(連接埠 {p})", link="ok", link_msg="", retries=0, error="")
                return p
            except Exception:
                ib.disconnect()
        tries += 1
        print(f"Could not connect on ports {PORTS_TO_TRY}. Is TWS / IB Gateway logged in with the API enabled? Retrying in 15s ...")
        set_status("connecting", "連不上 IB Gateway:可能沒登入、被登出、或 API 沒開", retries=tries)
        time.sleep(15)


def ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x)) and x > 0


def price_of(t):
    px = t.marketPrice()
    if not ok(px):
        px = t.close
    return float(px) if ok(px) else None


def us_options_open(now_ny: dt.datetime) -> bool:
    if IGNORE_MARKET_HOURS:
        return True
    return now_ny.weekday() < 5 and US_OPEN <= now_ny.time() <= US_CLOSE


def make_underlying(ib: IB, src: str):
    if src == "NQ":
        details = ib.reqContractDetails(Future("NQ", exchange="CME"))
        today_str = _now_ny().strftime("%Y%m%d")
        c = sorted((d.contract for d in details if d.contract.lastTradeDateOrContractMonth[:8] >= today_str),
                   key=lambda c: c.lastTradeDateOrContractMonth)[0]
        return c, "FUT"
    c = Stock("QQQ", "SMART", "USD") if src == "QQQ" else Index("NDX", "NASDAQ", "USD")
    ib.qualifyContracts(c)
    return c, ("STK" if src == "QQQ" else "IND")


def list_contracts(ib: IB, src: str, und, sectype: str, spot: float) -> list[dict]:
    cfg = SRC[src]
    chains = ib.reqSecDefOptParams(und.symbol, "CME" if src == "NQ" else "", sectype, und.conId)
    chains = [c for c in chains if c.exchange == "CME"] if src == "NQ" else ([c for c in chains if c.exchange == "SMART"] or list(chains))
    today = _now_ny().date()
    lo, hi = spot * (1 - cfg["band"]), spot * (1 + cfg["band"])
    out, seen = [], set()
    for ch in chains:
        for exp in sorted(ch.expirations):
            dte = (dt.datetime.strptime(exp, "%Y%m%d").date() - today).days
            if not 0 <= dte <= cfg["max_dte"] or (ch.tradingClass, exp) in seen:
                continue
            seen.add((ch.tradingClass, exp))
            set_status("building", f"建立 {src} 合約清單(已處理 {len(seen)} 組到期日,{len(out)} 個合約)")
            if src == "NQ":
                probe = FuturesOption(symbol="NQ", lastTradeDateOrContractMonth=exp, exchange="CME", tradingClass=ch.tradingClass)
            else:
                probe = Option(symbol=und.symbol, lastTradeDateOrContractMonth=exp, exchange="SMART", currency="USD",
                               tradingClass=ch.tradingClass)
            for d in ib.reqContractDetails(probe):
                c = d.contract
                if lo <= c.strike <= hi and round(c.strike % cfg["step"], 6) == 0:
                    out.append(dict(c=c, src=src, expiry=exp, dte=dte, cls=c.tradingClass, exp_dt=expiry_dt(exp, d)))
    return out


# ----------------------------- contract-list cache (per New York trading date) -----------------------------
_cache_lock = threading.Lock()
_CONTRACT_FIELDS = ("conId", "symbol", "secType", "lastTradeDateOrContractMonth", "strike", "right", "exchange",
                    "currency", "multiplier", "tradingClass", "localSymbol")


def _now_ny() -> dt.datetime:
    return dt.datetime.now(NY)


def _items_to_json(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        c = it["c"]
        out.append(dict(c={k: getattr(c, k, None) for k in _CONTRACT_FIELDS}, src=it["src"], expiry=it["expiry"],
                        dte=it["dte"], cls=it["cls"], exp_dt=it["exp_dt"].isoformat()))
    return out


def _items_from_json(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r["c"])
        kind = FuturesOption if d.pop("secType") == "FOP" else Option
        c = kind(**{k: v for k, v in d.items() if v not in (None, "")})
        out.append(dict(c=c, src=r["src"], expiry=r["expiry"], dte=r["dte"], cls=r["cls"],
                        exp_dt=dt.datetime.fromisoformat(r["exp_dt"])))
    return out


def cache_save(src: str, day: dt.date, spot: float, items: list[dict]):
    """把剛建好的合約清單存檔。任何錯誤都吞掉(快取只是加速,不能影響收集)。"""
    if not USE_UNIVERSE_CACHE or not items:
        return
    try:
        with _cache_lock:
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data[src] = dict(date=day.isoformat(), spot=spot, saved=time.time(), items=_items_to_json(items))
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, CACHE_PATH)
    except Exception as e:
        print(f"[cache] save failed: {e}")


def cache_load(src: str, day: dt.date, spot: float):
    """同一個美東日、現價沒漂移太多的快取才回傳 items;否則 None。"""
    if not USE_UNIVERSE_CACHE:
        return None
    try:
        with _cache_lock:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                e = json.load(f).get(src)
        if not e or e.get("date") != day.isoformat() or not e.get("items"):
            return None
        if abs(spot / float(e["spot"]) - 1.0) > CACHE_MAX_SPOT_DRIFT:
            print(f"[cache] {src}: price moved {abs(spot / float(e['spot']) - 1) * 100:.1f}% since the list was built, rebuilding")
            return None
        return _items_from_json(e["items"])
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[cache] load failed ({e}), rebuilding")
        return None


def build_universe(ib: IB, src: str, now_ny: dt.datetime) -> dict:
    t_start = time.time()
    und, sectype = make_underlying(ib, src)
    t = ib.reqMktData(und, "", False, False)  # stays subscribed (1 line) so all spots can be read at one instant
    spot = None
    for _ in range(10):
        ib.sleep(1)
        spot = price_of(t)
        if spot:
            break
    if not spot:
        ib.cancelMktData(und)
        raise RuntimeError("no underlying price (missing subscription, or market closed)")
    t_list = time.time()
    items = cache_load(src, now_ny.date(), spot)
    how = "from cache"
    if items is None:
        items = list_contracts(ib, src, und, sectype, spot)
        cache_save(src, now_ny.date(), spot, items)
        how = "built from IBKR"
    print(f"Universe {src}: {getattr(und, 'localSymbol', '') or und.symbol} ~ {spot:,.2f}, {len(items)} option contracts in scope "
          f"({how}, {time.time() - t_list:.0f}s; whole step {time.time() - t_start:.0f}s)")
    return dict(und=und, spot_t=t, items=items, date=now_ny.date())


def pull(ib: IB, items: list[dict], settle: float, oi_cache: dict, want_oi: bool = True):
    """Market-data pull in batches. want_oi=True adds the '101' OI generic tick and caches OI
    (slow, needs a longer settle); want_oi=False pulls greeks+volume only and reads OI from the cache
    (fast, for intraday cycles). Returns (rows, incomplete)."""
    rows, incomplete = [], []
    generic = "101" if want_oi else ""
    for i in range(0, len(items), BATCH):
        batch = items[i : i + BATCH]
        tickers = [ib.reqMktData(it["c"], generic, False, False) for it in batch]
        ib.sleep(settle)
        for it, t in zip(batch, tickers):
            c = it["c"]
            key = (it["src"], c.conId)
            g = t.modelGreeks
            if want_oi:
                oi = t.callOpenInterest if c.right == "C" else t.putOpenInterest
                if oi is not None and not (isinstance(oi, float) and math.isnan(oi)):
                    oi_cache[key] = float(oi)
            oi = oi_cache.get(key)
            if g is None or not ok(g.impliedVol) or not ok(g.undPrice) or oi is None:
                incomplete.append(it)
                continue
            vol = t.volume
            vol = 0.0 if (vol is None or (isinstance(vol, float) and math.isnan(vol))) else float(vol)
            if vol < 0 or vol >= 2_000_000_000:   # IBKR sends 2147483647 (int32 max) when it has no volume figure: treat as 'unknown' = 0
                vol = 0.0
            cfg = SRC[it["src"]]
            rows.append(dict(src=it["src"], conId=c.conId, expiry=it["expiry"], dte=it["dte"], cls=it["cls"],
                             strike=c.strike, cp=c.right, iv=float(g.impliedVol), F=float(g.undPrice), oi=oi, vol=vol,
                             mult=cfg["mult"], q=RISK_FREE if cfg["q"] is None else cfg["q"]))
        for it in batch:
            ib.cancelMktData(it["c"])
    return rows, incomplete


def prewarm_oi(ib: IB, state: dict, src: str):
    """預先抓一次 OI 填進 oi_cache。只有「幾乎全部合約都拿到 OI」才把 oi_last 設成現在——
    否則開盤第一輪會誤以為 OI 是新的、結果用空的快取,整輪合約都 incomplete。"""
    items = state["uni"][src]["items"]
    t0 = time.time()
    set_status("building", f"預先抓取 {src} 未平倉量(OI)")
    pull(ib, items, SETTLE_SECS, state.setdefault("oi_cache", {}), want_oi=True)
    have = sum(1 for it in items if (src, it["c"].conId) in state["oi_cache"])
    cover = have / len(items) if items else 0.0
    if cover >= 0.9:
        state.setdefault("oi_last", {})[src] = time.time()
    print(f"[prewarm] {src}: OI for {have}/{len(items)} contracts ({cover * 100:.0f}%) in {time.time() - t0:.0f}s"
          + ("" if cover >= 0.9 else " - too few, the open cycle will pull OI itself"))


def prebuild_universes(ib: IB, state: dict, now: dt.datetime):
    """美東 08:00–09:30(週一到週五),QQQ/NDX 今天的清單還沒建就先建。每次呼叫最多建一個來源,
    讓 NQ 的快照不會被長時間卡住。失敗會稍後重試;全程不影響原本的開盤流程(開盤時沒建好就照舊重建)。"""
    if not PREBUILD_ENABLED or now.weekday() >= 5 or not (PREBUILD_ET <= now.time() < US_OPEN):
        return
    pb = state.setdefault("prebuild", {})
    for s in ENABLED:
        if s == "NQ":
            continue
        u = state["uni"].get(s)
        if u is not None and u["date"] == now.date():
            continue
        if time.time() - pb.get(s, 0.0) < PREBUILD_RETRY_SECS:
            continue
        pb[s] = time.time()
        try:
            if u is not None:
                ib.cancelMktData(u["und"])
                state["uni"].pop(s, None)
            set_status("building", f"開盤前預先建立 {s} 合約清單")
            state["uni"][s] = build_universe(ib, s, now)
            state["last_good"].pop(s, None)
            state["fresh"].add(s)
            if PREWARM_OI:
                prewarm_oi(ib, state, s)
        except Exception as e:
            print(f"[prebuild] {s} failed ({e}); will retry in {PREBUILD_RETRY_SECS // 60} min, or build at the open as before")
        return          # 一次只建一個來源


def quick_nq_snapshot(ib: IB, con: sqlite3.Connection, state: dict):
    """美股開盤時 QQQ/NDX 要先重建上千檔合約清單 + 重拉 OI,整輪 20~30 分鐘。在那之前先存一筆
    「只有 NQ」的快照,讓熱圖開盤那一段不要整個空白(盤前半夜本來就都是 NQ-only,前端會自動
    carry-forward 昨天的 QQQ/NDX)。任何錯誤都靜默跳過,絕不影響主循環。"""
    try:
        u = state["uni"].get("NQ")
        if u is None or u["date"] != _now_ny().date():
            return
        spot = price_of(u["spot_t"])
        if not spot:
            return
        items = u["items"]
        rows, _inc = pull(ib, items, FLOW_SETTLE_SECS, state.setdefault("oi_cache", {}), want_oi=False)
        if not rows:
            return
        rows = [dict(r, stale=0) for r in rows]
        now = _now_ny()
        exp_by_key = {it["c"].conId: it["exp_dt"] for it in items}
        for r in rows:
            r["T"] = max((exp_by_key[r["conId"]] - now).total_seconds(), 3600) / (365 * 24 * 3600)
        df, summary, _per_src = compute_snapshot(rows, {"NQ": spot}, spot)
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        save_snapshot(con, ts, df, summary)
        print(f"[early NQ snapshot] {summary['n_contracts']} contracts saved at {ts}")
    except Exception as e:
        print(f"[early NQ snapshot] skipped: {e}")


# ----------------------------- one cycle -----------------------------
def run_cycle(ib: IB, state: dict, con: sqlite3.Connection | None = None):
    """Pull every active source and compute the snapshot. Returns (df, summary, per_src, info) or None."""
    now = _now_ny()
    open_us = us_options_open(now)
    active = [s for s in ENABLED if s == "NQ" or open_us]

    for s in list(active):  # (re)build the contract list when the New York date changes
        u = state["uni"].get(s)
        if u is None or u["date"] != now.date():
            if u is not None:
                ib.cancelMktData(u["und"])
                state["uni"].pop(s, None)
            try:
                set_status("building", f"建立 {s} 合約清單")
                state["uni"][s] = build_universe(ib, s, now)
                state["last_good"].pop(s, None)
                state["fresh"].add(s)
            except Exception as e:
                print(f"!! {s}: could not build the contract list ({e})")
                if s == "NQ":
                    raise
                active.remove(s)
        # 開盤空窗防護:NQ 已就緒、QQQ/NDX 今天還沒建合約清單時,先存一筆「只有 NQ」的快照,
        # 這樣 QQQ/NDX 重建 + 重拉 OI 的那 20~30 分鐘,熱圖不會整個空白。
        if s == "NQ" and con is not None and open_us and len(active) > 1:
            if any(state["uni"].get(x) is None or state["uni"].get(x)["date"] != now.date() for x in ("QQQ", "NDX")):
                quick_nq_snapshot(ib, con, state)

    if not open_us:       # 盤前:NQ 清單就緒後,順便預建 QQQ/NDX(一次一個來源,見 prebuild_universes)
        prebuild_universes(ib, state, now)

    spots = {s: price_of(state["uni"][s]["spot_t"]) for s in active}   # all spots read at the same instant
    for s in [s for s in active if not spots[s]]:
        print(f"!! {s}: no live underlying price this cycle, skipping it")
        active.remove(s)
    if "NQ" not in active:
        raise RuntimeError("no NQ price")
    nq_spot = spots["NQ"]

    all_rows, info = [], {}
    oi_last = state.setdefault("oi_last", {})
    for s in active:
        items = state["uni"][s]["items"]
        set_status("pulling", f"抓取 {s} 報價({len(items)} 個合約)")
        # OI is a prior-day figure published ~once a day, so refresh it only every OI_REFRESH_SECS.
        # When refreshing, the slow OI pull also returns greeks+volume, so reuse it for this cycle.
        refresh = time.time() - oi_last.get(s, 0.0) >= OI_REFRESH_SECS
        if refresh:
            oi_last[s] = time.time()
            print(f"    {s}: OI refreshed (cached for the next {OI_REFRESH_SECS // 3600}h)")
        rows, inc = pull(ib, items, SETTLE_SECS if refresh else FLOW_SETTLE_SECS, state["oi_cache"], want_oi=refresh)
        if inc and RETRY_INCOMPLETE:
            more, inc = pull(ib, inc, RETRY_SETTLE_SECS, state["oi_cache"], want_oi=refresh)
            rows += more
        lg = state["last_good"].setdefault(s, {})
        got = {r["conId"] for r in rows}
        for r in rows:
            lg[r["conId"]] = r
        stale = [dict(lg[it["c"].conId], stale=1) for it in items if it["c"].conId not in got and it["c"].conId in lg]
        missing = len(items) - len(rows) - len(stale)
        info[s] = dict(scope=len(items), fresh=len(rows), stale=len(stale), missing=missing)
        if not rows and open_us and s != "NQ":
            print(f"!! {s}: no usable quotes this cycle (holiday, missing subscription, or data not flowing)")
        all_rows += [dict(r, stale=0) for r in rows] + stale

    exp_by_key = {(s, it["c"].conId): it["exp_dt"] for s in active for it in state["uni"][s]["items"]}
    for r in all_rows:
        r["T"] = max((exp_by_key[(r["src"], r["conId"])] - now).total_seconds(), 3600) / (365 * 24 * 3600)
    if not all_rows:
        return None
    df, summary, per_src = compute_snapshot(all_rows, {s: spots[s] for s in active}, nq_spot)
    return df, summary, per_src, info


def one_cycle(ib: IB, con: sqlite3.Connection, state: dict, quiet: QuietFilter | None = None):
    t0 = time.time()
    quiet and quiet.counts.clear()
    res = run_cycle(ib, state, con)
    if res is None:
        print("No usable quotes this cycle.")
        set_status("noquotes", "這一輪沒有可用報價(可能休市,或市場資料沒進來)")
        return None
    df, s, per_src, info = res
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    save_snapshot(con, ts, df, s)
    set_status("recording", f"正常 · NQ {s['spot']:,.1f} · {s['n_contracts']} 個合約 · 來源 {s['sources']}",
               last_snapshot=ts, last_ok=time.time(), error="")
    if gex_levels is not None and EXPORT_LEVELS:
        try:
            gex_levels.maybe_export(con, ts, reexport_et=LEVELS_REEXPORT_ET)
        except Exception as e:                       # never let the level export stop the collector
            print(f"[levels] export failed: {e}")
    print(f"[{time.strftime('%H:%M:%S')}] NQ {s['spot']:,.1f} | contracts {s['n_contracts']} (stale {s['n_stale']}) | "
          f"sources {s['sources']} | took {time.time() - t0:.0f}s")
    for src, p in per_src.items():
        fo = f"{p['flip_oi']:,.0f}" if p["flip_oi"] else "n/a"
        fv = f"{p['flip_vol']:,.0f}" if p["flip_vol"] else "n/a"
        nat = f" (native {p['flip_oi'] / p['ratio']:,.2f})" if p["flip_oi"] and src != "NQ" else ""
        i = info.get(src, {})
        print(f"    {src:3s} net OI {p['net_oi'] / 1e6:+8,.0f}M flip {fo}{nat} | net vol {p['net_vol'] / 1e6:+8,.0f}M flip {fv} "
              f"| fresh {i.get('fresh', 0)} stale {i.get('stale', 0)} missing {i.get('missing', 0)}")
    if quiet and quiet.counts:
        print(f"    IBKR warnings hidden this cycle: {dict(quiet.counts)}")
    return df, s


def main():
    if "NQ" not in ENABLED:
        raise SystemExit("ENABLED must include 'NQ' (it defines the price coordinates).")
    con = init_db(DB_PATH)
    prune_old(con)
    quiet = QuietFilter()
    logging.getLogger("ib_async.wrapper").addFilter(quiet)
    ib = IB()
    threading.Thread(target=_status_heartbeat, daemon=True).start()
    set_status("starting", "收集器啟動中")
    ib.errorEvent += _on_ib_error
    connect(ib)
    ib.reqMarketDataType(MARKET_DATA_TYPE)
    state = dict(uni={}, oi_cache={}, last_good={}, fresh=set())
    n_old = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    print(f"Recording every cycle to {DB_PATH} ({n_old} snapshots already stored; replay them in the dashboard).\n"
          f"Stop with Ctrl+C.\n")
    try:
        while True:
            t0 = time.time()
            try:
                if not ib.isConnected():
                    print("Connection lost - reconnecting ...")
                    set_status("connecting", "與 IB Gateway 的連線中斷,重新連線中", link="lost", link_msg="API 連線已中斷")
                    connect(ib)
                    ib.reqMarketDataType(MARKET_DATA_TYPE)
                    state["uni"] = {}  # tickers from the old connection are dead
                    state["prebuild"] = {}
                one_cycle(ib, con, state, quiet)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # keep running; try again next cycle
                print(f"!! cycle failed: {e}")
                set_status("error", "這一輪收集失敗", error=str(e)[:200])
            ib.sleep(max(5.0, INTERVAL_SECS - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        ib.disconnect()
        con.close()


if __name__ == "__main__":
    main()
