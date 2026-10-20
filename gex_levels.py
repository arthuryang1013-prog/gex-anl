"""
gex_levels.py -- turn ONE snapshot of nq_gex.sqlite into price levels (Call wall, Put wall, Flip) for NQ,
ready to paste into TradingView.  READ-ONLY on the database.

Levels are produced for every combination of
    source  : NQ, QQQ, NDX, SUM (the three together)          (QQQ / NDX are mapped into NQ points)
    basis   : OI (previous-day open interest)  /  VOL (today's volume so far)
    scope   : ALL (all expiries)  /  EX0 (without 0DTE)
    level   : CALL / PUT  (the single largest, anywhere)
              CALLA / PUTB (largest Call at or above the price / largest Put at or below it)
              FLIP  (zero-gamma flip, sticky-strike profile, closest to price)

Files written next to this script
    levels_today.txt   one line for the TradingView indicator (nq_gex_levels.pine) -- paste it into its settings
    levels_today.json  the same numbers in readable form (the dashboard shows them)
    levels_archive/    a copy of every export

The collector calls maybe_export() after every snapshot, so the files are made by themselves at the first complete frame
after the US open.  You can also run it by hand:
    python3 gex_levels.py                    first complete frame of today (New York date)
    python3 gex_levels.py --latest           the newest frame
    python3 gex_levels.py --at 22:30         the frame closest to 22:30 Taipei time today
    python3 gex_levels.py --date 2026-10-13  first complete frame of that New York date

MODEL ASSUMPTIONS (same as the dashboard): call positive / put negative (a convention, not a verified fact); QQQ and NDX
strikes are mapped to NQ points with the price ratio read at the same instant; flips hold IV fixed while price moves.
The VOL levels use today's running volume: right after the open it is only a few minutes of trading, so treat the VOL
levels of the first frame as noise and re-export later (e.g. --latest at 10:30 New York time).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY, TW = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei")
try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    HERE = os.getcwd()
DB_PATH = os.path.join(HERE, "nq_gex.sqlite")
OUT_TXT = os.path.join(HERE, "levels_today.txt")
OUT_JSON = os.path.join(HERE, "levels_today.json")
ARCHIVE = os.path.join(HERE, "levels_archive")
STATE_FILE = os.path.join(HERE, "levels_state.json")
OUT_HIST = os.path.join(HERE, "levels_history.txt")      # one line per New York date (newest export of that day), oldest first
HIST_DAYS = 400
RISK_FREE = 0.04
SOURCES = ["NQ", "QQQ", "NDX"]
ALL_SRC = SOURCES + ["SUM"]
BASES = {"OI": ("gex", "oi"), "VOL": ("gex_vol", "vol")}      # label -> (signed weight column, raw weight column)
SCOPES = ("ALL", "EX0")
KINDS = [("call", "CALL"), ("put", "PUT"), ("flip", "FLIP"), ("call_above", "CALLA"), ("put_below", "PUTB")]
US_OPEN, US_CLOSE = dt.time(9, 30), dt.time(16, 15)
MIN_CONTRACTS = 50                                           # a source needs this many contracts to count as "present"


# ----------------------------- maths -----------------------------
def _flip(sub: pd.DataFrame, nq_spot: float, wcol: str):
    """Zero-gamma flip in NQ points: the sign change of the net-GEX profile closest to the price."""
    if sub.empty or float(sub[wcol].abs().sum()) == 0.0:
        return None
    grid = np.linspace(nq_spot * 0.96, nq_spot * 1.04, 241)
    Fg = sub["F"].values[:, None] * (grid[None, :] / nq_spot)
    K, T, iv, q = (sub[k].values[:, None] for k in ("strike", "T", "iv", "q"))
    sd = iv * np.sqrt(T)
    d1 = (np.log(Fg / K) + (RISK_FREE - q + 0.5 * iv**2) * T) / sd
    gamma = np.exp(-q * T) * np.exp(-0.5 * d1**2) / np.sqrt(2 * np.pi) / (Fg * sd)
    sign = np.where(sub["cp"].values == "C", 1.0, -1.0)[:, None]
    prof = (sign * gamma * sub[wcol].values[:, None] * sub["mult"].values[:, None] * Fg**2 * 0.01).sum(axis=0)
    flips = []
    for k in np.where(np.sign(prof[:-1]) * np.sign(prof[1:]) < 0)[0]:
        x0, x1, y0, y1 = grid[k], grid[k + 1], prof[k], prof[k + 1]
        flips.append(x0 - y0 * (x1 - x0) / (y1 - y0))
    return float(min(flips, key=lambda x: abs(x - nq_spot))) if flips else None


def _walls(sub: pd.DataFrame, nq_spot: float, gcol: str, level_col: str):
    out = dict(call=None, put=None, call_above=None, put_below=None)
    if sub.empty:
        return out
    calls = sub[sub["cp"] == "C"].groupby(level_col)[gcol].sum()
    puts = sub[sub["cp"] == "P"].groupby(level_col)[gcol].sum()
    if len(calls) and calls.max() > 0:
        out["call"] = float(calls.idxmax())
        up = calls[calls.index >= nq_spot]
        if len(up) and up.max() > 0:
            out["call_above"] = float(up.idxmax())
    if len(puts) and puts.min() < 0:
        out["put"] = float(puts.idxmin())
        dn = puts[puts.index <= nq_spot]
        if len(dn) and dn.min() < 0:
            out["put_below"] = float(dn.idxmin())
    return out


def compute_levels(df: pd.DataFrame, nq_spot: float) -> dict:
    """df = the contracts of one snapshot. Returns levels[src][basis][scope] = {call, put, flip, call_above, put_below, n}."""
    df = df.copy()
    df["lv_src"] = np.round(df["strike_nq"] * 4.0) / 4.0              # NQ tick
    df["lv_sum"] = np.round(df["strike_nq"] / 25.0) * 25.0            # 25-point buckets when the sources are added up
    res = {}
    for src in ALL_SRC:
        sub_src = df if src == "SUM" else df[df["src"] == src]
        res[src] = {}
        for basis, (gcol, wcol) in BASES.items():
            res[src][basis] = {}
            for scope in SCOPES:
                sub = sub_src if scope == "ALL" else sub_src[sub_src["dte"] > 0]
                lv = _walls(sub, nq_spot, gcol, "lv_sum" if src == "SUM" else "lv_src")
                lv["flip"] = _flip(sub, nq_spot, wcol)
                lv["n"] = int(len(sub))
                res[src][basis][scope] = lv
    return res


def to_string(levels: dict, meta: dict, compact: bool = False) -> str:
    """One line for the Pine indicator:  META|date|time|price;SRC|BASIS|SCOPE|KIND|price;..."""
    parts = [f"META|{meta['ny_date']}|{meta['tw_time']}|{meta['nq_spot']:.2f}"]
    for src in ALL_SRC:
        for basis in BASES:
            for scope in SCOPES:
                for key, code in KINDS:
                    if compact and key in ("call_above", "put_below"):
                        continue
                    v = levels[src][basis][scope].get(key)
                    if v is not None:
                        parts.append(f"{src}|{basis}|{scope}|{code}|{v:.2f}")
    return ";".join(parts)


def format_table(levels: dict, basis: str = "OI", scope: str = "ALL") -> str:
    rows = [f"{'':4s} {'Call wall':>10s} {'Put wall':>10s} {'Flip':>10s} {'Call(上方)':>11s} {'Put(下方)':>10s}   [{basis} | {scope}]"]
    for src in ALL_SRC:
        lv = levels[src][basis][scope]
        f = lambda v: "-" if v is None else f"{v:,.2f}"
        rows.append(f"{src:4s} {f(lv['call']):>10s} {f(lv['put']):>10s} {f(lv['flip']):>10s} {f(lv['call_above']):>11s} {f(lv['put_below']):>10s}")
    return "\n".join(rows)


# ----------------------------- frames -----------------------------
def _cols(con: sqlite3.Connection) -> set:
    return {r[1] for r in con.execute("PRAGMA table_info(contracts)")}


def load_frame(con: sqlite3.Connection, ts: str):
    have = _cols(con)
    sel = (["ts", "dte", "strike", "cp", "oi", "iv", "F", "T", "gex"]
           + [(f"COALESCE({n}, {d}) AS {n}" if n in have else f"{d} AS {n}")
              for n, d in (("src", "'NQ'"), ("strike_nq", "strike"), ("vol", "0.0"), ("gex_vol", "0.0"), ("mult", "20.0"), ("q", str(RISK_FREE)))])
    df = pd.read_sql(f"SELECT {', '.join(sel)} FROM contracts WHERE ts = ?", con, params=(ts,))
    bad = (df["vol"] >= 2_000_000_000) | (df["vol"] < 0)      # IBKR's 'no value' marker 2147483647 that older collector versions stored
    if bad.any():
        df.loc[bad, ["vol", "gex_vol"]] = 0.0
    spot = con.execute("SELECT spot FROM snapshots WHERE ts = ?", (ts,)).fetchone()
    return df, (float(spot[0]) if spot else None)


def _ny_day_bounds(ny_date: dt.date):
    a = pd.Timestamp(ny_date.isoformat(), tz=NY)
    return a.tz_convert("UTC").isoformat(timespec="seconds"), (a + pd.Timedelta(days=1)).tz_convert("UTC").isoformat(timespec="seconds")


def frame_is_full(con: sqlite3.Connection, ts: str, min_contracts: int = MIN_CONTRACTS) -> bool:
    if "src" not in _cols(con):
        return False
    c = dict(con.execute("SELECT src, COUNT(*) FROM contracts WHERE ts = ? GROUP BY src", (ts,)).fetchall())
    return all(c.get(s, 0) >= min_contracts for s in SOURCES)


def frames_of_day(con: sqlite3.Connection, ny_date: dt.date) -> list:
    """Snapshot timestamps inside the US options session of that New York date."""
    lo, hi = _ny_day_bounds(ny_date)
    out = []
    for (t,) in con.execute("SELECT ts FROM snapshots WHERE ts >= ? AND ts < ? ORDER BY ts", (lo, hi)):
        tn = pd.Timestamp(t).tz_convert(NY).time()
        if US_OPEN <= tn <= US_CLOSE:
            out.append(t)
    return out


def pick_first_full_frame(con: sqlite3.Connection, ny_date: dt.date, min_contracts: int = MIN_CONTRACTS):
    for t in frames_of_day(con, ny_date):
        if frame_is_full(con, t, min_contracts):
            return t
    return None


def pick_closest(con: sqlite3.Connection, ny_date: dt.date, hhmm_tw: str):
    frames = frames_of_day(con, ny_date)
    if not frames:
        return None
    h, m = (int(x) for x in hhmm_tw.split(":"))
    target = pd.Timestamp(dt.datetime.combine(dt.datetime.now(TW).date(), dt.time(h, m)), tz=TW)
    return min(frames, key=lambda t: abs((pd.Timestamp(t) - target).total_seconds()))


# ----------------------------- export -----------------------------
def export_frame(con: sqlite3.Connection, ts: str, tag: str = "manual", compact: bool = True) -> dict:
    df, spot = load_frame(con, ts)
    if df.empty or spot is None:
        raise RuntimeError(f"沒有 {ts} 這一幀的資料")
    levels = compute_levels(df, spot)
    t_ny, t_tw = pd.Timestamp(ts).tz_convert(NY), pd.Timestamp(ts).tz_convert(TW)
    meta = dict(ts=ts, ny_date=t_ny.date().isoformat(), tw_time=t_tw.strftime("%H:%M"), tw_datetime=t_tw.strftime("%Y-%m-%d %H:%M"),
                nq_spot=spot, tag=tag, sources_in_frame={s: int((df["src"] == s).sum()) for s in SOURCES},
                generated=dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S"))
    line = to_string(levels, meta, compact)
    os.makedirs(ARCHIVE, exist_ok=True)
    stem = f"levels_{meta['ny_date']}_{t_tw.strftime('%H%M')}_{tag}"
    payload = dict(meta=meta, levels=levels, string=line)
    for path, text in ((OUT_TXT, line), (OUT_JSON, json.dumps(payload, ensure_ascii=False, indent=1)),
                       (os.path.join(ARCHIVE, stem + ".txt"), line), (os.path.join(ARCHIVE, stem + ".json"), json.dumps(payload, ensure_ascii=False, indent=1))):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + ("\n" if path.endswith(".txt") else ""))
    update_history(line, meta['ny_date'])
    try:
        write_pine()
    except Exception:
        pass
    return payload


def update_history(line: str, ny_date: str):
    """Keep one line per New York date in levels_history.txt (a re-export of the same day replaces that day's line)."""
    try:
        old = []
        if os.path.exists(OUT_HIST):
            with open(OUT_HIST, encoding="utf-8") as f:
                old = [x.strip() for x in f if x.strip()]
        keep = [x for x in old if not x.startswith(f"META|{ny_date}|")]
        keep.append(line)
        keep.sort(key=lambda x: x.split("|", 2)[1] if x.startswith("META|") else "")
        with open(OUT_HIST, "w", encoding="utf-8") as f:
            f.write("\n".join(keep[-HIST_DAYS:]) + "\n")
    except Exception:
        pass


# ----------------------------- Pine generator (all history, no 10-day limit) -----------------------------
PINE_KINDS = ("CALL", "PUT", "FLIP")
SERIES = [(s, b, sc, k) for s in ALL_SRC for b in BASES for sc in SCOPES for k in PINE_KINDS]   # 4*2*2*3 = 48 series, fixed order
PINE_OUT = os.path.join(HERE, "nq_gex_levels.pine")


def _parse_line(line: str):
    parts = line.strip().split(";")
    m = parts[0].split("|")
    if len(m) < 4 or m[0] != "META":
        return None
    vals = {}
    for it in parts[1:]:
        f = it.split("|")
        if len(f) == 5:
            vals[(f[0], f[1], f[2], f[3])] = f[4]
    return m[1], vals


def write_pine(hist_path: str = OUT_HIST, out_path: str = PINE_OUT, with_vol: bool = True):
    """Build ONE Pine script with every day of levels_history.txt embedded.  Plots (not drawing objects) are used, so there
    is no 500-line limit; a day's levels exist only during that day's session, so old days never block new bars."""
    days = []
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as f:
            for ln in f:
                r = _parse_line(ln)
                if r:
                    days.append(r)
    days.sort(key=lambda x: x[0])
    rows = []
    for d, vals in days:
        key = d.replace("-", "")
        cells = [vals.get(SERIES[i], "") for i in range(len(SERIES))]
        rows.append(f"{key}," + ",".join(cells))
    push = "\n".join(f'    array.push(rows, "{r}")' for r in rows) or '    array.push(rows, "")'
    plots = []
    for setno in (1, 2):
        for si, s in enumerate(ALL_SRC):
            for ki, k in enumerate(PINE_KINDS):
                if k == "FLIP":
                    continue          # flips are drawn as dashed line.new segments (a plot cannot be dashed)
                width = "lineW + 1" if k == "FLIP" else "lineW"
                tr = "0" if setno == 1 else "55"
                show = f"show{s}" if setno == 1 else f"show2 and show{s}"
                shownk = {"CALL": "showCall", "PUT": "showPut", "FLIP": "showFlip"}[k]
                plots.append(
                    f'plot(({show} and {shownk}) ? sv({si}, {ki}, {setno}) : na, "{s} {k} 組{setno}", '
                    f'color=color.new(c{s}, {tr}), linewidth={width}, style=plot.style_linebr, display=display.pane)')
    pine = PINE_TEMPLATE.replace("//DATA//", push).replace("//PLOTS//", "\n".join(plots)).replace("//NSER//", str(len(SERIES))).replace("//NDAYS//", str(len(rows)))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(pine)
    return len(rows), len(pine)


PINE_TEMPLATE = """//@version=5
// 由 gex_levels.py 自動產生,內含全部歷史(//NDAYS// 天)。每天:整份複製貼進 Pine 編輯器 -> 儲存 / 更新圖表。
// 線用 plot 畫(不是 line.new),沒有 500 條上限;每天的價位只出現在當天時段,不會往右延伸。
indicator("NQ GEX Levels (history)", overlay=true, max_labels_count=500, max_lines_count=500)

sess      = input.session("0930-1615", "線的時段(紐約時間)", group="時段", tooltip="每天的價位只畫在這個時段。請維持在同一個紐約日曆日內。僅支援日內週期圖(1m~4h)。")
basis1    = input.string("OI", "第一組:資料", options=["OI", "VOL"], group="顯示", tooltip="OI=前一日留倉量;VOL=今日成交量(開盤第一波只有幾分鐘成交量,雜訊大)")
scope1    = input.string("ALL", "第一組:範圍", options=["ALL", "EX0"], group="顯示", tooltip="ALL=含 0DTE;EX0=排除 0DTE")
show2     = input.bool(false, "同時顯示第二組(半透明)", group="第二組")
basis2    = input.string("OI", "第二組:資料", options=["OI", "VOL"], group="第二組")
scope2    = input.string("EX0", "第二組:範圍", options=["ALL", "EX0"], group="第二組")
showNQ    = input.bool(true,  "NQ",  group="來源", inline="s")
showQQQ   = input.bool(true,  "QQQ", group="來源", inline="s")
showNDX   = input.bool(true,  "NDX", group="來源", inline="s")
showSUM   = input.bool(false, "三者合計", group="來源", inline="s")
showCall  = input.bool(true, "Call wall", group="價位種類", inline="k")
showPut   = input.bool(true, "Put wall",  group="價位種類", inline="k")
showFlip  = input.bool(true, "Flip(虛線)", group="價位種類", inline="k")
cNQ  = input.color(color.new(#f5a623, 0), "NQ 顏色",  group="顏色")
cQQQ = input.color(color.new(#4a90e2, 0), "QQQ 顏色", group="顏色")
cNDX = input.color(color.new(#7ed321, 0), "NDX 顏色", group="顏色")
cSUM = input.color(color.new(#bd10e0, 0), "合計 顏色", group="顏色")
lineW = input.int(1, "線寬", minval=1, maxval=3, group="樣式")
labelDays = input.int(999, "標籤顯示最近幾天(0=不顯示)", minval=0, maxval=999, group="樣式", tooltip="在每天線的起點標出 來源/種類/價位。預設每天都標。TradingView 一個腳本最多 500 個標籤,超過時最舊的會被自動刪掉;想圖乾淨可改小。")

var float[] dates = array.new_float()
var float[] vals  = array.new_float()
var int NS = //NSER//

if barstate.isfirst
    rows = array.new_string()
//DATA//
    for r in rows
        if str.length(r) > 8
            f = str.split(r, ",")
            if array.size(f) == NS + 1
                array.push(dates, str.tonumber(array.get(f, 0)))
                for j = 1 to NS
                    c = array.get(f, j)
                    array.push(vals, str.length(c) > 0 ? str.tonumber(c) : na)

nyKey = year(time, "America/New_York") * 10000 + month(time, "America/New_York") * 100 + dayofmonth(time, "America/New_York")
inSess = not na(time(timeframe.period, sess, "America/New_York"))
idx = inSess ? array.binary_search(dates, nyKey) : -1

sv(si, ki, setno) =>
    bs = setno == 1 ? basis1 : basis2
    sc = setno == 1 ? scope1 : scope2
    off = ((si * 2 + (bs == "VOL" ? 1 : 0)) * 2 + (sc == "EX0" ? 1 : 0)) * 3 + ki
    idx >= 0 ? array.get(vals, idx * NS + off) : na

srcNames = array.from("NQ", "QQQ", "NDX", "SUM")
kindNames = array.from("Call", "Put", "Flip")
srcOnA = array.from(showNQ, showQQQ, showNDX, showSUM)
kindOnA = array.from(showCall, showPut, showFlip)
srcColA = array.from(cNQ, cQQQ, cNDX, cSUM)
newSess = idx >= 0 and idx != nz(idx[1], -1)
endH = int(str.tonumber(str.substring(sess, 5, 7)))
endM = int(str.tonumber(str.substring(sess, 7, 9)))
if newSess and showFlip
    tEnd = timestamp("America/New_York", year(time, "America/New_York"), month(time, "America/New_York"), dayofmonth(time, "America/New_York"), endH, endM)
    for setno = 1 to 2
        if setno == 1 or show2
            for si = 0 to 3
                if array.get(srcOnA, si)
                    fv = sv(si, 2, setno)
                    if not na(fv)
                        line.new(time, fv, tEnd, fv, xloc=xloc.bar_time, color=color.new(array.get(srcColA, si), setno == 1 ? 0 : 55), style=line.style_dashed, width=lineW + 1)

if newSess and labelDays > 0 and idx >= array.size(dates) - labelDays
    for setno = 1 to 2
        if setno == 1 or show2
            for si = 0 to 3
                for ki = 0 to 2
                    if array.get(srcOnA, si) and array.get(kindOnA, ki)
                        v = sv(si, ki, setno)
                        if not na(v)
                            txt = array.get(srcNames, si) + " " + array.get(kindNames, ki) + (setno == 2 ? "②" : "") + " " + str.tostring(v, "#.##")
                            label.new(bar_index, v, txt, style=label.style_none, textcolor=array.get(srcColA, si), size=size.small)

//PLOTS//
"""


def _notify(title: str, text: str):
    if sys.platform != "darwin":
        return
    try:
        esc = lambda s: s.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{esc(text)}" with title "{esc(title)}"'], timeout=10, capture_output=True)
    except Exception:
        pass


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
            return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def _save_state(s: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:
        pass


def maybe_export(con: sqlite3.Connection, ts_now: str, reexport_et=(), notify: bool = True, log=print):
    """Called by the collector after each snapshot. Exports once at the first complete frame after the US open
    (all three sources present) and, optionally, again at the given New York times ("10:30", ...)."""
    now_ny = pd.Timestamp(ts_now).tz_convert(NY)
    if now_ny.weekday() >= 5 or not (US_OPEN <= now_ny.time() <= US_CLOSE):
        return None
    day = now_ny.date().isoformat()
    state = _load_state()
    if state.get("date") != day:
        state = dict(date=day, first_done=False, done_times=[])
    out = None
    if not state["first_done"]:
        if frame_is_full(con, ts_now):
            first = pick_first_full_frame(con, now_ny.date()) or ts_now
            out = export_frame(con, first, tag="first")
            state["first_done"] = True
            note = f"今日第一波價位已產生(資料時間 {out['meta']['tw_time']} 台北)"
        else:
            return None
    else:
        due = [t for t in reexport_et if t not in state["done_times"] and now_ny.strftime("%H:%M") >= t]
        if not due:
            return None
        out = export_frame(con, ts_now, tag=due[-1].replace(":", ""))
        state["done_times"] += due
        note = f"價位已更新({due[-1]} 紐約時間,資料 {out['meta']['tw_time']} 台北)"
    _save_state(state)
    log(f"[levels] {note} -> {OUT_TXT}")
    if notify:
        _notify("NQ GEX 價位", note + "。到儀表板複製字串,貼進 TradingView 指標。")
    return out


# ----------------------------- command line -----------------------------
def main():
    ap = argparse.ArgumentParser(description="Export Call wall / Put wall / Flip levels for TradingView")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--date", help="New York date YYYY-MM-DD (default: today in New York)")
    ap.add_argument("--latest", action="store_true", help="use the newest frame of that date")
    ap.add_argument("--at", metavar="HH:MM", help="use the frame closest to this Taipei time (today)")
    ap.add_argument("--backfill", action="store_true", help="add the first complete frame of EVERY past day in the database to levels_history.txt, then rebuild nq_gex_levels.pine")
    ap.add_argument("--pine", action="store_true", help="only rebuild nq_gex_levels.pine from levels_history.txt")
    ap.add_argument("--no-vol", action="store_true", help="leave the VOL series out of the Pine script (shorter script)")
    ap.add_argument("--full", action="store_true", help="also include the 'largest Call above price / largest Put below price' variants (string ~2000 chars instead of ~1300)")
    args = ap.parse_args()
    if args.pine:
        n, size = write_pine(with_vol=not args.no_vol)
        print(f"已用 levels_history.txt 的 {n} 天重建 {PINE_OUT}({size:,} 字元)")
        return
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    if args.backfill:
        add = 0
        for (t0,) in con.execute("SELECT MIN(ts) FROM snapshots"):
            pass
        days = sorted({pd.Timestamp(t).tz_convert(NY).date() for (t,) in con.execute("SELECT ts FROM snapshots")})
        for d in days:
            if d.weekday() >= 5:
                continue
            ts = pick_first_full_frame(con, d)
            if not ts:
                continue
            df, spot = load_frame(con, ts)
            if df.empty or spot is None:
                continue
            lv = compute_levels(df, spot)
            t_ny, t_tw = pd.Timestamp(ts).tz_convert(NY), pd.Timestamp(ts).tz_convert(TW)
            meta = dict(ny_date=t_ny.date().isoformat(), tw_time=t_tw.strftime("%H:%M"), nq_spot=spot)
            update_history(to_string(lv, meta, True), meta["ny_date"])
            add += 1
        n, size = write_pine(with_vol=not args.no_vol)
        print(f"補進 {add} 天;levels_history.txt 現有 {n} 天;已重建 {PINE_OUT}({size:,} 字元)")
        if size > 90000:
            print("警告:腳本接近 TradingView 的長度上限(我記得約 10 萬字元,未驗證),可加 --no-vol 縮短。")
        return
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(NY).date()
    if args.latest:
        fr = frames_of_day(con, day)
        ts = fr[-1] if fr else None
    elif args.at:
        ts = pick_closest(con, day, args.at)
    else:
        ts = pick_first_full_frame(con, day)
    if not ts:
        sys.exit(f"找不到 {day} 的快照。(第一波需要 NQ、QQQ、NDX 三個來源各至少 {MIN_CONTRACTS} 個合約,美股選擇權 21:30 開盤後約 5-10 分鐘才會有。)")
    con.close()
    con = sqlite3.connect(args.db)
    out = export_frame(con, ts, tag="manual" if (args.latest or args.at) else "first", compact=not args.full)
    m = out["meta"]
    print(f"資料時間 {m['tw_datetime']}(台北)|NQ {m['nq_spot']:,.2f}|各來源合約數 {m['sources_in_frame']}")
    for b in BASES:
        for sc in SCOPES:
            print("\n" + format_table(out["levels"], b, sc))
    print(f"\n字串長度 {len(out['string'])} 字元,已寫入 {OUT_TXT}")


if __name__ == "__main__":
    main()
