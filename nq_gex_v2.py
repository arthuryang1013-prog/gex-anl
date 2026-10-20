"""
nq_gex_v2.py -- GEX snapshot in NQ price coordinates from three option sources via IBKR:
    NQ  = CME NQ futures options  (multiplier 20,  gamma: Black-76)
    QQQ = QQQ ETF options         (multiplier 100, gamma: Black-Scholes with dividend yield)
    NDX = Nasdaq-100 index options(multiplier 100, gamma: Black-Scholes with dividend yield)

Changes vs. v1
  * QQQ and NDX added. Their strikes are mapped to NQ points with  NQ_spot / source_spot.
  * Port is auto-detected (tries 4002, 4001, 7497, 7496) -- no more editing the port by hand.
  * Expiry time comes from IBKR contract details when available (else 16:00 ET, and it says so).
  * Output is produced twice: all expiries, and excluding 0DTE (expiring today, New York date).
  * Diagnostics: why contracts were skipped, whether data was live or delayed, expiry table.

Install:  pip install ib_async numpy pandas scipy plotly
Run:      python3 nq_gex_v2.py

MODEL ASSUMPTIONS (change them on purpose)
  - Dealers long calls / short puts (sign +1 / -1). A common convention, NOT a verified fact.
  - Profile curves hold each contract's IV fixed while price moves (sticky-strike).
  - OI is the previous day's figure (published once per day).
  - QQQ/NDX -> NQ mapping uses one proportional ratio (ignores futures basis / carry drift).
  - Summing the three sources into one "COMBINED" number is a modelling choice, not a standard.
  - Dividend yields below are rough placeholders; set your own.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from ib_async import IB, Future, FuturesOption, Index, Option, Stock
from scipy.stats import norm

# ----------------------------- config -----------------------------
HOST = "127.0.0.1"
PORT_OVERRIDE = 4001                      # e.g. 4002 to force one port
PORTS_TO_TRY = [4002, 4001, 7497, 7496]   # Gateway paper/live, TWS paper/live
CLIENT_ID = 11
MARKET_DATA_TYPE = 3     # 3 = live if you are subscribed, otherwise delayed (to my knowledge); 1 = live only
ENABLED = ["NQ", "QQQ", "NDX"]            # NQ must stay (it defines the price coordinates)
SRC = {
    #        multiplier  strike step  band around spot  max days to expiry  dividend yield (None = futures option)
    "NQ":  dict(mult=20,  step=25, band=0.04, max_dte=7, q=None),
    "QQQ": dict(mult=100, step=1,  band=0.04, max_dte=7, q=0.006),
    "NDX": dict(mult=100, step=50, band=0.04, max_dte=7, q=0.007),
}
RISK_FREE = 0.04
SIGN = {"C": +1, "P": -1}
BATCH = 80                # concurrent market-data lines per batch (IBKR minimum allocation is 100)
SETTLE_SECS = 6
RETRY_SKIPPED = True      # one more attempt, with a longer wait, for contracts that came back incomplete
RETRY_SETTLE_SECS = 10
BIN = 25                  # NQ points per bucket in the strike chart
NY = ZoneInfo("America/New_York")
OUT_PREFIX = "gex_v2"


# ----------------------------- math -----------------------------
def bs_gamma(S, K, T, iv, r, q):
    """Black-Scholes gamma with continuous yield q. With q == r it equals Black-76 gamma on a future."""
    S, K, T, iv, r, q = (np.asarray(x, dtype=float) for x in (S, K, T, iv, r, q))
    sd = iv * np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * iv**2) * T) / sd
    return np.exp(-q * T) * norm.pdf(d1) / (S * sd)


def expiry_dt(expiry: str, detail):
    """Expiry timestamp. IBKR's lastTradeTime + timeZoneId if both parse, else 16:00 ET (flagged)."""
    d = dt.datetime.strptime(expiry, "%Y%m%d")
    lt = (getattr(detail, "lastTradeTime", "") or "").strip()
    tzid = (getattr(detail, "timeZoneId", "") or "").strip()
    if lt and tzid:
        try:
            t = dt.datetime.strptime(lt[:8], "%H:%M:%S")
            return d.replace(hour=t.hour, minute=t.minute, tzinfo=ZoneInfo(tzid)), "ibkr"
        except Exception:
            pass
    return d.replace(hour=16, tzinfo=NY), "fallback_16ET"


# ----------------------------- IBKR helpers -----------------------------
def connect(ib: IB) -> int:
    for p in ([PORT_OVERRIDE] if PORT_OVERRIDE else PORTS_TO_TRY):
        try:
            ib.connect(HOST, p, clientId=CLIENT_ID, readonly=True, timeout=5)
            print(f"Connected to IBKR on port {p}")
            return p
        except Exception:
            ib.disconnect()
    raise SystemExit(f"Could not connect on ports {PORTS_TO_TRY}. Is TWS / IB Gateway logged in with the API enabled?")


def get_underlying(ib: IB, src: str):
    if src == "NQ":
        details = ib.reqContractDetails(Future("NQ", exchange="CME"))
        today = dt.datetime.now(NY).strftime("%Y%m%d")
        cands = sorted((d.contract for d in details if d.contract.lastTradeDateOrContractMonth[:8] >= today),
                       key=lambda c: c.lastTradeDateOrContractMonth)
        return cands[0], "FUT"
    c = Stock("QQQ", "SMART", "USD") if src == "QQQ" else Index("NDX", "NASDAQ", "USD")
    ib.qualifyContracts(c)
    return c, ("STK" if src == "QQQ" else "IND")


def last_price(ib: IB, contract):
    t = ib.reqMktData(contract, "", False, False)
    ib.sleep(3)
    px = t.marketPrice()
    if not px or math.isnan(px):
        px = t.close
    ib.cancelMktData(contract)
    return float(px) if px and not math.isnan(px) else None


def list_contracts(ib: IB, src: str, und, sectype: str, spot: float):
    cfg = SRC[src]
    chains = ib.reqSecDefOptParams(und.symbol, "CME" if src == "NQ" else "", sectype, und.conId)
    if src == "NQ":
        chains = [c for c in chains if c.exchange == "CME"]
    else:
        chains = [c for c in chains if c.exchange == "SMART"] or list(chains)
    today = dt.datetime.now(NY).date()
    lo, hi = spot * (1 - cfg["band"]), spot * (1 + cfg["band"])
    out, seen = [], set()
    for ch in chains:
        for exp in sorted(ch.expirations):
            dte = (dt.datetime.strptime(exp, "%Y%m%d").date() - today).days
            if not 0 <= dte <= cfg["max_dte"] or (ch.tradingClass, exp) in seen:
                continue
            seen.add((ch.tradingClass, exp))
            if src == "NQ":
                probe = FuturesOption(symbol="NQ", lastTradeDateOrContractMonth=exp, exchange="CME",
                                      tradingClass=ch.tradingClass)
            else:
                probe = Option(symbol=und.symbol, lastTradeDateOrContractMonth=exp, exchange="SMART",
                               currency="USD", tradingClass=ch.tradingClass)
            for d in ib.reqContractDetails(probe):
                c = d.contract
                if lo <= c.strike <= hi and round(c.strike % cfg["step"], 6) == 0:
                    out.append((c, d))
    return out


def pull(ib: IB, items, src: str, settle: float):
    cfg = SRC[src]
    now = dt.datetime.now(NY)
    today = now.date()
    rows, skipped, dtypes = [], [], Counter()
    for i in range(0, len(items), BATCH):
        batch = items[i : i + BATCH]
        tickers = [ib.reqMktData(c, "101", False, False) for c, _ in batch]  # 101 = option open interest
        ib.sleep(settle)
        for (c, d), t in zip(batch, tickers):
            g = t.modelGreeks
            oi = t.callOpenInterest if c.right == "C" else t.putOpenInterest
            if g is None:
                why = "no_greeks"
            elif not g.impliedVol:
                why = "no_iv"
            elif not g.undPrice:
                why = "no_underlying"
            elif oi is None or math.isnan(oi):
                why = "no_oi"
            else:
                why = None
            if why:
                skipped.append((c, d, why))
                continue
            dtypes[getattr(t, "marketDataType", None)] += 1
            exp = c.lastTradeDateOrContractMonth
            exp_dt, exp_src = expiry_dt(exp, d)
            T = max((exp_dt - now).total_seconds(), 3600) / (365 * 24 * 3600)  # floor 1h
            rows.append(dict(
                src=src, expiry=exp, exp_dt=exp_dt.isoformat(), exp_src=exp_src,
                dte=(dt.datetime.strptime(exp, "%Y%m%d").date() - today).days, cls=c.tradingClass,
                strike=c.strike, right=c.right, iv=g.impliedVol, F=g.undPrice, oi=float(oi),
                ibkr_gamma=g.gamma, mult=cfg["mult"], q=RISK_FREE if cfg["q"] is None else cfg["q"], T=T,
            ))
        for c, _ in batch:
            ib.cancelMktData(c)
        print(f"  batch {i // BATCH + 1}/{math.ceil(len(items) / BATCH)}: kept {len(rows)}, skipped {len(skipped)}")
    return rows, skipped, dtypes


def pull_with_retry(ib: IB, items, src: str):
    rows, skipped, dtypes = pull(ib, items, src, SETTLE_SECS)
    if skipped and RETRY_SKIPPED:
        print(f"  retrying {len(skipped)} incomplete contracts with a longer wait ...")
        rows2, skipped, dt2 = pull(ib, [(c, d) for c, d, _ in skipped], src, RETRY_SETTLE_SECS)
        rows += rows2
        dtypes += dt2
    return rows, Counter(w for *_, w in skipped), dtypes


# ----------------------------- GEX -----------------------------
def prepare(df: pd.DataFrame, spots: dict, nq_spot: float) -> pd.DataFrame:
    df = df.copy()
    df["sign"] = df["right"].map(SIGN)
    df["gamma"] = bs_gamma(df["F"], df["strike"], df["T"], df["iv"], RISK_FREE, df["q"])
    df["gex"] = df["sign"] * df["gamma"] * df["oi"] * df["mult"] * df["F"] ** 2 * 0.01  # $ per 1% move
    df["strike_nq"] = df["strike"] * df["src"].map(lambda s: nq_spot / spots[s])
    df["bin"] = (df["strike_nq"] / BIN).round() * BIN
    return df


def profile(df: pd.DataFrame, grid: np.ndarray, nq_spot: float) -> np.ndarray:
    if df.empty:
        return np.zeros_like(grid)
    Fg = df["F"].values[:, None] * (grid[None, :] / nq_spot)  # each underlying moves proportionally
    g = bs_gamma(Fg, df["strike"].values[:, None], df["T"].values[:, None], df["iv"].values[:, None],
                 RISK_FREE, df["q"].values[:, None])
    return (df["sign"].values[:, None] * g * df["oi"].values[:, None] * df["mult"].values[:, None]
            * Fg**2 * 0.01).sum(axis=0)


def find_flip(grid, prof, ref):
    flips = []
    for k in np.where(np.sign(prof[:-1]) * np.sign(prof[1:]) < 0)[0]:
        x0, x1, y0, y1 = grid[k], grid[k + 1], prof[k], prof[k + 1]
        flips.append(x0 - y0 * (x1 - x0) / (y1 - y0))
    return min(flips, key=lambda x: abs(x - ref)) if flips else None


def summarize(df: pd.DataFrame, grid, nq_spot, label: str):
    print(f"\n=== {label} ===")
    rows = []
    parts = [(s, df[df["src"] == s]) for s in ENABLED if s in set(df["src"])] + [("COMBINED", df)]
    for name, sub in parts:
        if sub.empty:
            continue
        flip = find_flip(grid, profile(sub, grid, nq_spot), nq_spot)
        calls = sub[sub["right"] == "C"].groupby("bin")["gex"].sum()
        puts = sub[sub["right"] == "P"].groupby("bin")["gex"].sum()
        rows.append(dict(source=name, contracts=len(sub), net_gex_M=sub["gex"].sum() / 1e6, flip_nq=flip,
                         call_wall_nq=calls.idxmax() if len(calls) else None,
                         put_wall_nq=puts.idxmin() if len(puts) else None))
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:,.1f}"))


def plot(df: pd.DataFrame, grid, nq_spot, title: str, path: str):
    import plotly.graph_objects as go  # noqa: F401
    from plotly.subplots import make_subplots

    colors = {"NQ": "#3b6fd4", "QQQ": "#2e9e6b", "NDX": "#e09f3e"}
    fig = make_subplots(rows=2, cols=1, vertical_spacing=0.12,
                        subplot_titles=(f"Net GEX by strike bucket ({BIN} NQ pts), $ per 1% move", "Net GEX vs. NQ price"))
    for s in ENABLED:
        sub = df[df["src"] == s]
        if sub.empty:
            continue
        b = sub.groupby("bin")["gex"].sum()
        fig.add_bar(x=b.index, y=b.values, name=f"{s} net", marker_color=colors[s], row=1, col=1)
        fig.add_scatter(x=grid, y=profile(sub, grid, nq_spot), name=f"{s} profile",
                        line=dict(color=colors[s], width=1), row=2, col=1)
    comb = profile(df, grid, nq_spot)
    fig.add_scatter(x=grid, y=comb, name="Combined", line=dict(color="black", width=3), row=2, col=1)
    flip = find_flip(grid, comb, nq_spot)
    for r in (1, 2):
        fig.add_vline(x=nq_spot, line_dash="dash", line_color="gray", annotation_text="NQ", row=r, col=1)
        if flip:
            fig.add_vline(x=flip, line_dash="dot", line_color="orange", annotation_text="flip", row=r, col=1)
    fig.add_hline(y=0, line_color="lightgray", row=2, col=1)
    fig.update_layout(barmode="relative", height=850, template="plotly_white", title=title)
    fig.write_html(path)


# ----------------------------- main -----------------------------
def main():
    if "NQ" not in ENABLED:
        raise SystemExit("ENABLED must include 'NQ' (it defines the price coordinates).")
    ib = IB()
    connect(ib)
    ib.reqMarketDataType(MARKET_DATA_TYPE)
    all_rows, spots, diag = [], {}, []
    try:
        for src in ENABLED:
            print(f"\n--- {src} ---")
            try:
                und, sectype = get_underlying(ib, src)
                spot = last_price(ib, und)
                if spot is None:
                    raise RuntimeError("no underlying price (missing subscription, or market closed)")
                spots[src] = spot
                print(f"underlying {und.localSymbol or und.symbol}: {spot:,.2f}")
                items = list_contracts(ib, src, und, sectype, spot)
                print(f"contracts in scope: {len(items)}")
                rows, reasons, dtypes = pull_with_retry(ib, items, src)
                all_rows += rows
                diag.append((src, len(items), len(rows), dict(reasons), dict(dtypes)))
            except Exception as e:  # keep going with the other sources
                print(f"!! {src} skipped: {e}")
                if src == "NQ":
                    raise SystemExit("NQ price is required as the coordinate reference. Stopping.")
    finally:
        ib.disconnect()

    if not all_rows:
        raise SystemExit("No usable quotes from any source.")
    nq_spot = spots["NQ"]
    df = prepare(pd.DataFrame(all_rows), spots, nq_spot)
    df.to_csv(f"{OUT_PREFIX}.csv", index=False)
    grid = np.linspace(nq_spot * 0.96, nq_spot * 1.04, 241)

    print("\n=== Diagnostics ===")
    for src, n_scope, n_kept, reasons, dtypes in diag:
        print(f"{src}: spot {spots[src]:,.2f} | in scope {n_scope} | kept {n_kept} | still incomplete: {reasons} "
              f"| data type of kept quotes (1=live, 3=delayed): {dtypes}")
    exp_tbl = df.groupby(["src", "expiry"]).agg(expiry_time=("exp_dt", "first"), time_source=("exp_src", "first"),
                                                 contracts=("gex", "size"), net_gex_M=("gex", lambda s: s.sum() / 1e6))
    print("\nExpiry times used (time_source 'fallback_16ET' = IBKR did not supply one; 0DTE numbers are sensitive to this):")
    print(exp_tbl.to_string(float_format=lambda x: f"{x:,.1f}"))
    for s in df["src"].unique():
        sub = df[(df["src"] == s) & df["ibkr_gamma"].notna() & (df["ibkr_gamma"] > 0)]
        if len(sub):
            print(f"Sanity {s}: median(my gamma / IBKR gamma) = {(sub['gamma'] / sub['ibkr_gamma']).median():.3f}")

    summarize(df, grid, nq_spot, "ALL expiries")
    ex0 = df[df["dte"] > 0]
    summarize(ex0, grid, nq_spot, "EXCLUDING 0DTE (expiring today, New York date)")
    plot(df, grid, nq_spot, "GEX in NQ coordinates - all expiries", f"{OUT_PREFIX}_all.html")
    if not ex0.empty:
        plot(ex0, grid, nq_spot, "GEX in NQ coordinates - excluding 0DTE", f"{OUT_PREFIX}_ex0dte.html")
    print(f"\nSaved {OUT_PREFIX}.csv, {OUT_PREFIX}_all.html, {OUT_PREFIX}_ex0dte.html")


if __name__ == "__main__":
    main()
