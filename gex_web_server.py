"""
gex_web_server.py -- local web site for the NQ GEX monitor (READ-ONLY on nq_gex.sqlite).

What it does
  * serves the web page in the "web" folder  (candles, GEX heat map, walls / flip, side profile, replay)
  * answers small JSON requests from the page (/api/...), reading the SQLite file the collector writes
  * optionally keeps 1-minute NQ candles up to date from IBKR (table "bars" in the same file, own client id 31,
    read-only).  Without IBKR candles it falls back to a line made from the snapshot prices.

Run:      python3 gex_web_server.py            then open  http://127.0.0.1:8765
Options:  --port 8765   --db path/to/nq_gex.sqlite   --no-bars (do not connect to IBKR)   --no-open (do not open the browser)
Needs:    the packages the collector already needs (numpy, pandas); ib_async only for the candles.
Nothing is sent anywhere: the server listens on 127.0.0.1 only.

MODEL ASSUMPTIONS (same as the collector / dashboard): call positive, put negative (a convention, not a verified fact);
QQQ and NDX strikes are mapped to NQ points with the price ratio read at the same instant; flips hold IV fixed while
price moves (sticky-strike).  Volume has no buyer / seller split.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import datetime as dt
import hashlib
import json
import math
import mimetypes
import multiprocessing
import os
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import gex_levels as GL
import deepseek_client as DC

try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    HERE = os.getcwd()
WEB_DIR = os.path.join(HERE, "web")
DB_PATH = os.path.join(HERE, "nq_gex.sqlite")
CACHE_PATH = os.path.join(HERE, "web_cache.sqlite")
NY = ZoneInfo("America/New_York")
PORTS_TO_TRY = [4001, 4002, 7497, 7496]
BARS_CLIENT_ID = 31
BARS_HISTORY = "4 D"
MAX_COLS = 400            # heat-map columns sent to the page (snapshots are thinned evenly above this)
MAX_LEVEL_FRAMES = 120    # snapshots for which wall / flip lines are computed (thinned evenly above this)
BAND = 0.05               # heat map covers price +/- 5 %

STATUS = {"bars": "none", "bars_err": ""}
_cache_lock = threading.Lock()
_ERRORS = collections.deque(maxlen=100)   # (dict) 最近 100 筆伺服器錯誤,供前端「錯誤紀錄」面板顯示

# orderflow + AI 設定檔與常數(DeepSeek key 填在 config.json,熱讀取,不用重開 server)
CONFIG_PATH = os.path.join(HERE, "config.json")
ORDERFLOW_PATH = os.path.join(HERE, "orderflow.json")
ORDERFLOW_HISTORY_PATH = os.path.join(HERE, "orderflow_history.jsonl")
DEFAULT_ORDERFLOW_MAX_AGE = 60.0   # orderflow.json 超過幾秒沒更新就視為斷線
DEFAULT_ORDERFLOW_MAX_LAG = 30.0   # Bookmap 事件延遲超過幾秒就標記為「過時,不可信」
FLOW_WEIGHT = 0.30                 # 有 orderflow 時它在總結論的權重;GEX 三市場佔 1 - FLOW_WEIGHT
TTS_VOICE_DEFAULT = "zh-TW-YunJheNeural"   # 導師聲音(男生·允哲);可在 config.json 用 tts_voice 改
TTS_DIRNAME = "tts"                        # 語音 mp3 快取放 web 資料夾底下的子資料夾


def load_config() -> dict:
    """讀 config.json(不存在或壞掉就回傳空 dict,呼叫端用預設值)。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


# ----------------------------- helpers -----------------------------
def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).isoformat(timespec="seconds")


def epoch_of(ts: str) -> int:
    return int(dt.datetime.fromisoformat(ts).timestamp())


def clean(o):
    """Make an object JSON safe (NaN / inf -> null, numpy -> python)."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if hasattr(o, "item"):
        return clean(o.item())
    return o


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA busy_timeout=8000")
    return con


def thin(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1)
    keep = sorted({int(round(i * step)) for i in range(n)})
    return [items[i] for i in keep]


def has_table(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def exprs(con) -> dict:
    have = GL._cols(con)
    return dict(
        src="COALESCE(src,'NQ')" if "src" in have else "'NQ'",
        sk="COALESCE(strike_nq,strike)" if "strike_nq" in have else "strike",
        gvol="COALESCE(gex_vol,0)" if "gex_vol" in have else "0",
    )


def where_parts(con, srcs: list, scope: str):
    e = exprs(con)
    w, args = [], []
    if srcs and set(srcs) != {"NQ", "QQQ", "NDX"}:
        w.append(f"{e['src']} IN ({','.join('?' * len(srcs))})")
        args += srcs
    if scope == "ex0":
        w.append("dte > 0")
    elif scope == "zero":
        w.append("dte = 0")
    return e, w, args


def snapshot_list(con, lo: int, hi: int) -> list:
    rows = con.execute("SELECT ts, spot FROM snapshots WHERE ts >= ? AND ts <= ? ORDER BY ts", (iso(lo), iso(hi))).fetchall()
    return [(epoch_of(t), t, float(s)) for t, s in rows if s is not None]


def bucket_thin(snaps: list, span: int, secs: int | None = None) -> list:
    """Keep the first snapshot of every 5 / 15 / 30 minute bucket (bucket size depends on the span).  Buckets are anchored
    to the clock, not to the range, so the same frames come back for different ranges and the disk cache keeps working."""
    secs = secs or (300 if span <= 12 * 3600 else 900 if span <= 3 * 86400 else 1800)
    out, seen = [], set()
    for item in snaps:
        k = item[0] // secs
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


# ----------------------------- API: data -----------------------------
def api_days(con, q):
    out = {}
    for (t,) in con.execute("SELECT ts FROM snapshots ORDER BY ts"):
        e = epoch_of(t)
        d = dt.datetime.fromtimestamp(e, NY).date().isoformat()
        o = out.setdefault(d, dict(date=d, first=e, last=e, n=0))
        o["last"], o["n"] = e, o["n"] + 1
    return dict(days=list(out.values()))


def api_heat(con, q):
    lo, hi = int(q["from"]), int(q["to"])
    srcs = [s for s in q.get("src", "NQ,QQQ,NDX").split(",") if s]
    basis, scope, bucket = q.get("basis", "oi"), q.get("scope", "all"), max(1, int(float(q.get("bucket", 25))))
    snaps = thin(snapshot_list(con, lo, hi), int(q.get("maxcols", MAX_COLS)))
    if not snaps:
        return dict(bucket=bucket, times=[], spots=[], cols=[])
    e, w, args = where_parts(con, srcs, scope)
    wcol = "gex" if basis == "oi" else e["gvol"]
    ts_list = [s[1] for s in snaps]
    sql = (f"SELECT c.ts, CAST(ROUND({e['sk']}/{bucket}.0)*{bucket} AS INTEGER) AS bin, "
           f"SUM(CASE WHEN cp='C' THEN {wcol} ELSE 0 END), SUM(CASE WHEN cp='P' THEN {wcol} ELSE 0 END) "
           f"FROM contracts c JOIN snapshots s ON s.ts = c.ts "
           f"WHERE c.ts IN ({','.join('?' * len(ts_list))}) AND ABS({e['sk']} - s.spot) <= s.spot * {BAND} "
           + "".join(f"AND {x} " for x in w) + "GROUP BY c.ts, bin ORDER BY c.ts, bin")
    by = {}
    for ts, b, c, p in con.execute(sql, ts_list + args):
        by.setdefault(ts, []).extend([int(b), round((c or 0.0) / 1e6, 2), round((p or 0.0) / 1e6, 2)])
    return dict(bucket=bucket, times=[s[0] for s in snaps], spots=[s[2] for s in snaps], cols=[by.get(s[1], []) for s in snaps])


def api_net(con, q):
    lo, hi = int(q["from"]), int(q["to"])
    srcs = [s for s in q.get("src", "NQ,QQQ,NDX").split(",") if s]
    basis, scope = q.get("basis", "oi"), q.get("scope", "all")
    snaps = thin(snapshot_list(con, lo, hi), int(q.get("maxcols", MAX_COLS)))
    if not snaps:
        return dict(data=[])
    e, w, args = where_parts(con, srcs, scope)
    wcol = "gex" if basis == "oi" else e["gvol"]
    ts_list = [s[1] for s in snaps]
    sql = (f"SELECT ts, SUM({wcol}) FROM contracts WHERE ts IN ({','.join('?' * len(ts_list))}) "
           + "".join(f"AND {x} " for x in w) + "GROUP BY ts")
    got = dict(con.execute(sql, ts_list + args).fetchall())
    return dict(data=[[s[0], round((got.get(s[1]) or 0.0) / 1e6, 2)] for s in snaps if s[1] in got])


def api_net3(con, q):
    """Three net-GEX series at once, same $M scale: all, 0DTE only (dte=0), without 0DTE (dte>0)."""
    lo, hi = int(q["from"]), int(q["to"])
    srcs = [s for s in q.get("src", "NQ,QQQ,NDX").split(",") if s]
    basis = q.get("basis", "oi")
    snaps = thin(snapshot_list(con, lo, hi), int(q.get("maxcols", MAX_COLS)))
    if not snaps:
        return dict(all=[], zero=[], ex0=[])
    e, w, args = where_parts(con, srcs, "all")
    wcol = "gex" if basis == "oi" else e["gvol"]
    ts_list = [s[1] for s in snaps]
    sql = (f"SELECT ts, SUM({wcol}), SUM(CASE WHEN dte=0 THEN {wcol} ELSE 0 END), SUM(CASE WHEN dte>0 THEN {wcol} ELSE 0 END) "
           f"FROM contracts WHERE ts IN ({','.join('?' * len(ts_list))}) "
           + "".join(f"AND {x} " for x in w) + "GROUP BY ts")
    got = {}
    for ts, a, z, x in con.execute(sql, ts_list + args):
        got[ts] = (round((a or 0.0) / 1e6, 2), round((z or 0.0) / 1e6, 2), round((x or 0.0) / 1e6, 2))
    out_a, out_z, out_x = [], [], []
    for s in snaps:
        if s[1] in got:
            a, z, x = got[s[1]]
            out_a.append([s[0], a]); out_z.append([s[0], z]); out_x.append([s[0], x])
    return dict(all=out_a, zero=out_z, ex0=out_x)


def _atm_rows(con, snaps, src, want_iv=True):
    """One pass: fetch the ATM-relevant columns for one source over the snapshots we need."""
    ts_list = [s[1] for s in snaps]
    qm = ",".join("?" * len(ts_list))
    e = exprs(con)
    cols = f"{e['sk']}, cp, iv, dte" if want_iv else f"{e['sk']}, cp, dte"
    sql = f"SELECT ts, {cols} FROM contracts WHERE ts IN ({qm}) AND {e['src']}=?" + (" AND iv>0" if want_iv else "")
    rows = con.execute(sql, ts_list + [src]).fetchall()
    by = {}
    for r in rows:
        ts, sk, cp = r[0], float(r[1]), r[2]
        iv = float(r[3]) if want_iv else None
        dte = int(r[4] or 0)
        by.setdefault(ts, []).append((sk, cp, iv, dte))
    return by


def api_band(con, q):
    """±1σ expected-move band over time: spot ± spot × ATM_IV × √(1/365).  ATM IV = mean call/put IV at the strike
    nearest spot, from the nearest non-0DTE expiry (0DTE IV is distorted by the intraday expiry clock).  Returns thinned
    series [epoch, spot, up, dn, atm_iv_pct, dte]."""
    lo, hi = int(q["from"]), int(q["to"])
    src = (q.get("src", "NQ").split(",") or ["NQ"])[0]
    snaps = thin(snapshot_list(con, lo, hi), int(q.get("maxcols", MAX_COLS)))
    if not snaps:
        return dict(series=[], src=src, formula="±1σ = spot × ATM IV × √(1/365)")
    by = _atm_rows(con, snaps, src, want_iv=True)
    out = []
    for s in snaps:
        rows = by.get(s[1]); spot = s[2]
        if not rows:
            continue
        non0 = [r for r in rows if r[3] > 0] or rows
        min_dte = min(r[3] for r in non0)
        pool = [r for r in non0 if r[3] == min_dte] or non0
        best = min(pool, key=lambda r: abs(r[0] - spot))
        ivs = [r[2] for r in pool if abs(r[0] - best[0]) < 0.01]
        if not ivs:
            continue
        atm = sum(ivs) / len(ivs)
        em = spot * atm * math.sqrt(1.0 / 365.0)
        out.append([s[0], round(spot, 1), round(spot + em, 1), round(spot - em, 1), round(atm * 100, 1), min_dte])
    return dict(series=out, src=src, formula="±1σ = spot × ATM IV × √(1/365)")


def api_band0(con, q):
    """0DTE 日內預期區間 over time:spot ± spot × ATM_IV(0DTE) × √(T),T = 0DTE 到期(4pm ET)前的年化時間。
    比整日 ±1σ 貼近當下,且隨收盤逼近收窄。Returns [epoch, spot, up, dn, iv0_pct, hours_left]."""
    lo, hi = int(q["from"]), int(q["to"])
    src = (q.get("src", "NQ").split(",") or ["NQ"])[0]
    snaps = thin(snapshot_list(con, lo, hi), int(q.get("maxcols", MAX_COLS)))
    if not snaps:
        return dict(series=[], src=src, formula="±1σ(0DTE) = spot × 0DTE ATM IV × √T")
    e = exprs(con)
    ts_list = [s[1] for s in snaps]
    qm = ",".join("?" * len(ts_list))
    rows = con.execute(
        f"SELECT ts, {e['sk']}, iv, T FROM contracts WHERE ts IN ({qm}) AND {e['src']}=? AND dte=0 AND iv>0 AND T>0",
        ts_list + [src]).fetchall()
    by = {}
    for r in rows:
        by.setdefault(r[0], []).append((float(r[1]), float(r[2]), float(r[3])))
    out = []
    for s in snaps:
        rs = by.get(s[1]); spot = s[2]
        if not rs:
            continue
        best = min(rs, key=lambda r: abs(r[0] - spot))
        pool = [r for r in rs if abs(r[0] - best[0]) < 0.01]
        ivs = [r[1] for r in pool]
        if not ivs:
            continue
        iv0 = sum(ivs) / len(ivs)
        T = best[2]
        em = spot * iv0 * math.sqrt(T) if T > 0 else 0.0
        out.append([s[0], round(spot, 1), round(spot + em, 1), round(spot - em, 1), round(iv0 * 100, 1), round(T * 365.0 * 24.0, 1)])
    return dict(series=out, src=src, formula="±1σ(0DTE) = spot × 0DTE ATM IV × √T")


def api_skew(con, q):
    """25Δ risk-reversal skew (front expiry) over time, in vol points: IV(25Δ put) − IV(25Δ call).  Positive = puts
    rich (fear premium), negative = calls rich.  Uses Black-76 delta with the row's own iv/T/q/F."""
    lo, hi = int(q["from"]), int(q["to"])
    src = (q.get("src", "NQ").split(",") or ["NQ"])[0]
    snaps = thin(snapshot_list(con, lo, hi), int(q.get("maxcols", MAX_COLS)))
    if not snaps:
        return dict(series=[], src=src)
    ts_list = [s[1] for s in snaps]
    qm = ",".join("?" * len(ts_list))
    e = exprs(con)
    rows = con.execute(
        f"SELECT ts, {e['sk']}, cp, iv, T, q, F, dte FROM contracts WHERE ts IN ({qm}) AND {e['src']}=? AND iv>0",
        ts_list + [src]).fetchall()
    by = {}
    for r in rows:
        by.setdefault(r[0], []).append(
            (float(r[1]), r[2], float(r[3]), float(r[4] or 0), float(r[5] or 0), float(r[6] or 0), int(r[7] or 0)))
    def cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    def delta(F, K, iv, T, q, cp):
        if iv <= 0 or T <= 0 or F <= 0:
            return None
        d1 = (math.log(F / K) + (GL.RISK_FREE - q + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        cd = math.exp(-q * T) * cdf(d1)
        return cd if cp == "C" else cd - math.exp(-q * T)
    out = []
    for s in snaps:
        rows_t = by.get(s[1])
        if not rows_t:
            continue
        min_dte = min(r[6] for r in rows_t)
        pool = [r for r in rows_t if r[6] == min_dte]
        puts, calls = [], []
        for sk, cp, iv, T, q, F, dte in pool:
            d = delta(F, sk, iv, T, q, cp)
            if d is None:
                continue
            (calls if cp == "C" else puts).append((abs(d), iv))
        if len(puts) < 2 or len(calls) < 2:
            continue
        p25 = min(puts, key=lambda x: abs(x[0] - 0.25))[1]
        c25 = min(calls, key=lambda x: abs(x[0] - 0.25))[1]
        out.append([s[0], round((p25 - c25) * 100, 2)])
    return dict(series=out, src=src)


def api_pcr(con, q):
    """Put/Call ratio (in contract units) over time: OI and volume basis, all expiries and 0DTE only."""
    lo, hi = int(q["from"]), int(q["to"])
    srcs = [s for s in q.get("src", "NQ,QQQ,NDX").split(",") if s]
    snaps = thin(snapshot_list(con, lo, hi), int(q.get("maxcols", MAX_COLS)))
    if not snaps:
        return dict(series=dict(oi=[], vol=[], oi0=[], vol0=[]))
    e, w, args = where_parts(con, srcs, "all")
    ts_list = [s[1] for s in snaps]
    qm = ",".join("?" * len(ts_list))
    sql = ("SELECT ts, "
           "SUM(CASE WHEN cp='P' THEN oi ELSE 0 END), SUM(CASE WHEN cp='C' THEN oi ELSE 0 END), "
           "SUM(CASE WHEN cp='P' THEN vol ELSE 0 END), SUM(CASE WHEN cp='C' THEN vol ELSE 0 END), "
           "SUM(CASE WHEN cp='P' AND dte=0 THEN oi ELSE 0 END), SUM(CASE WHEN cp='C' AND dte=0 THEN oi ELSE 0 END), "
           "SUM(CASE WHEN cp='P' AND dte=0 THEN vol ELSE 0 END), SUM(CASE WHEN cp='C' AND dte=0 THEN vol ELSE 0 END) "
           f"FROM contracts WHERE ts IN ({qm}) " + "".join(f"AND {x} " for x in w) + "GROUP BY ts")
    got = {}
    for ts, po, co, pv, cv, po0, co0, pv0, cv0 in con.execute(sql, ts_list + args):
        got[ts] = (po, co, pv, cv, po0, co0, pv0, cv0)
    def ratio(p, c):
        return round(p / c, 4) if c else None
    oi, vol, oi0, vol0 = [], [], [], []
    for s in snaps:
        v = got.get(s[1])
        if not v:
            continue
        po, co, pv, cv, po0, co0, pv0, cv0 = v
        oi.append([s[0], ratio(po, co)]); vol.append([s[0], ratio(pv, cv)])
        oi0.append([s[0], ratio(po0, co0)]); vol0.append([s[0], ratio(pv0, cv0)])
    return dict(series=dict(oi=oi, vol=vol, oi0=oi0, vol0=vol0))


def api_flow(con, q):
    """Most-active strikes in the latest cycle: source volume added between the two latest snapshots, ranked by it."""
    src = (q.get("src", "NQ").split(",") or ["NQ"])[0]
    n = max(1, min(40, int(q.get("n", 15))))
    ts_list = [r[0] for r in con.execute("SELECT ts FROM snapshots ORDER BY ts DESC LIMIT 2").fetchall()]
    if len(ts_list) < 2:
        return dict(t=None, prev_t=None, rows=[], src=src)
    latest, prev = ts_list[0], ts_list[1]
    e = exprs(con)
    sk = e["sk"]
    def strike_data(ts):
        rows = con.execute(
            f"SELECT {sk}, cp, SUM(vol), SUM(oi) FROM contracts WHERE ts=? AND {e['src']}=? GROUP BY {sk}, cp",
            (ts, src)).fetchall()
        vd, od = {}, {}
        for strike, cp, v, o in rows:
            key = (int(round(float(strike))), cp)
            vd[key] = float(v or 0); od[key] = float(o or 0)
        return vd, od
    av, ao = strike_data(latest); bv, _bo = strike_data(prev)
    strikes = sorted({k[0] for k in set(av) | set(bv)})
    agg = []
    for s in strikes:
        cdv = max(0, av.get((s, "C"), 0) - bv.get((s, "C"), 0))
        pdv = max(0, av.get((s, "P"), 0) - bv.get((s, "P"), 0))
        dv = cdv + pdv
        if dv <= 0:
            continue
        agg.append(dict(strike=s, call_dv=round(cdv), put_dv=round(pdv), dv=round(dv),
                        call_vol=round(av.get((s, "C"), 0)), put_vol=round(av.get((s, "P"), 0)),
                        oi=round(ao.get((s, "C"), 0) + ao.get((s, "P"), 0))))
    agg.sort(key=lambda x: -x["dv"])
    return dict(t=epoch_of(latest), prev_t=epoch_of(prev), src=src, rows=agg[:n])


def _cache_con():
    c = sqlite3.connect(CACHE_PATH, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS lv (ts TEXT, src TEXT, basis TEXT, scope TEXT, call REAL, put REAL, flip REAL, "
              "call_a REAL, put_b REAL, PRIMARY KEY (ts, src, basis, scope))")
    c.execute("CREATE TABLE IF NOT EXISTS hx (k TEXT, ts TEXT, blob TEXT, PRIMARY KEY (k, ts))")
    c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    r = c.execute("SELECT v FROM meta WHERE k='ver'").fetchone()
    if not r or r[0] != "cf1":          # carry-forward of QQQ/NDX changed what a frame contains -> old cached numbers are stale
        c.execute("DELETE FROM lv")
        c.execute("DELETE FROM hx")
        c.execute("INSERT OR REPLACE INTO meta VALUES ('ver','cf1')")
        c.commit()
    return c


_cf_memo = {}
CF_MAX_AGE = 4 * 86400


def load_frame_cf(con, ts):
    """Like GL.load_frame, but when this snapshot has no QQQ / NDX rows (overnight: they are only polled during the US
    session) the rows of the most recent snapshot that has them are added, so weighted / QQQ / NDX levels and heat maps
    still show the last US-session picture.  Returns (df, spot, carried) with carried = {src: epoch of the borrowed frame}."""
    df, spot = GL.load_frame(con, ts)
    carried = {}
    if "src" not in GL._cols(con):
        return df, spot, carried
    have = set(df["src"].unique())
    parts = [df]
    for s in ("QQQ", "NDX"):
        if s in have:
            continue
        k = (s, ts)
        if k not in _cf_memo:
            r = con.execute("SELECT MAX(ts) FROM contracts WHERE src=? AND ts<?", (s, ts)).fetchone()
            prev = r[0] if r and r[0] else None
            if prev and epoch_of(ts) - epoch_of(prev) > CF_MAX_AGE:
                prev = None
            _cf_memo[k] = prev
        prev = _cf_memo[k]
        if not prev:
            continue
        d2, _ = GL.load_frame(con, prev)
        d2 = d2[d2["src"] == s]
        if len(d2):
            parts.append(d2)
            carried[s] = epoch_of(prev)
    if len(parts) > 1:
        df = pd.concat(parts, ignore_index=True)
    return df, spot, carried


def parse_weights(q) -> tuple:
    """w=NQ:50,QQQ:20,NDX:30  norm=1  ->  (weights dict, normalise flag, cache key)."""
    w = {"NQ": 20.0, "QQQ": 35.0, "NDX": 45.0}
    for part in (q.get("w") or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                if k in w:
                    w[k] = max(0.0, float(v))
            except ValueError:
                pass
    norm = str(q.get("norm", "1")) not in ("0", "false", "")
    return w, norm, "WGT|%g-%g-%g|%d" % (w["NQ"], w["QQQ"], w["NDX"], 1 if norm else 0)


def weighted_frame(df, w: dict, norm: bool, gcol: str):
    """Blend the three sources into one book: every contract's weights (oi, vol, gex, gex_vol) are multiplied by the source's
    importance weight; with norm the source is first scaled to the same size (total |GEX| = 1) so that a big book such as NDX
    does not win just by being big."""
    parts = []
    tot_w = sum(w.values()) or 1.0
    for src, imp in w.items():
        sub = df[df["src"] == src]
        if sub.empty or imp <= 0:
            continue
        size = float(sub[gcol].abs().sum()) if norm else 1.0
        if size <= 0:
            continue
        f = (imp / tot_w) / size * (1e9 if norm else 1.0)
        sub = sub.copy()
        for c in ("oi", "vol", "gex", "gex_vol"):
            sub[c] = sub[c] * f
        parts.append(sub)
    return pd.concat(parts) if parts else df.iloc[0:0]


def levels_for_ts(con, cache, ts: str, srcs: list, basis: str, scope: str) -> dict:
    """{src: (call, put, flip, call_above, put_below) or None}.  Cached on disk: a flip costs a few ms per source."""
    got = {}
    for r in cache.execute("SELECT src, call, put, flip, call_a, put_b FROM lv WHERE ts=? AND basis=? AND scope=?",
                           (ts, basis.upper(), scope.upper())):
        got[r[0]] = r[1:]
    miss = [s for s in srcs if s not in got]
    if miss:
        df, spot, _c = load_frame_cf(con, ts)
        gcol, wcol = GL.BASES[basis.upper()]
        df["lv_src"] = (df["strike_nq"] * 4.0).round() / 4.0
        df["lv_sum"] = (df["strike_nq"] / 25.0).round() * 25.0
        sc = scope.upper()
        rows = []
        for s in miss:
            wts = None
            if s.startswith("WGT|"):
                _p = s.split("|")
                _v = _p[1].split("-")
                wts = ({"NQ": float(_v[0]), "QQQ": float(_v[1]), "NDX": float(_v[2])}, _p[2] == "1")
                sub = df
            else:
                sub = df if s == "SUM" else df[df["src"] == s]
            if sc == "EX0":
                sub = sub[sub["dte"] > 0]
            elif sc == "ZERO":
                sub = sub[sub["dte"] == 0]
            if wts:
                sub = weighted_frame(sub, wts[0], wts[1], gcol)
            if spot is None or len(sub) < 10:
                val = (None,) * 5
            else:
                wl = GL._walls(sub, spot, gcol, "lv_sum" if (s == "SUM" or wts) else "lv_src")
                val = (wl["call"], wl["put"], GL._flip(sub, spot, wcol), wl["call_above"], wl["put_below"])
            got[s] = val
            rows.append((ts, s, basis.upper(), sc) + val)
        with _cache_lock:
            cache.executemany("INSERT OR REPLACE INTO lv VALUES (?,?,?,?,?,?,?,?,?)", rows)
            cache.commit()
    return got


def api_levels(con, q):
    lo, hi = int(q["from"]), int(q["to"])
    srcs = [s for s in q.get("src", "NQ,QQQ,NDX").split(",") if s]
    basis, scope = q.get("basis", "oi"), q.get("scope", "all")
    snaps = bucket_thin(snapshot_list(con, lo, hi), hi - lo)
    _w, _n, wkey = parse_weights(q)
    internal = {s: (wkey if s == "WGT" else s) for s in srcs}
    cache = _cache_con()
    try:
        out = {s: dict(t=[], call=[], put=[], flip=[]) for s in srcs}
        for e, ts, _spot in snaps:
            got = levels_for_ts(con, cache, ts, list(internal.values()), basis, scope)
            for s in srcs:
                v = got.get(internal[s])
                if v is None or all(x is None for x in v):
                    continue
                o = out[s]
                o["t"].append(e)
                o["call"].append(v[0])
                o["put"].append(v[1])
                o["flip"].append(v[2])
        return dict(series=out)
    finally:
        cache.close()


_status_cache = {}


def api_status(con, q):
    row = con.execute("SELECT ts, spot FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    out = dict(now=int(time.time()), latest=None, bars=dict(STATUS))
    if has_table(con, "bars_meta"):
        m = dict(con.execute("SELECT k, v FROM bars_meta").fetchall())
        out["bars"] = dict(bars=m.get("state", "none"), bars_err=m.get("err", ""), contract=m.get("contract", ""))
    if con and has_table(con, "bars"):
        r = con.execute("SELECT MAX(t) FROM bars").fetchone()
        out["bars"]["last_t"] = r[0] if r else None
    if not row:
        return out
    ts = row[0]
    out["latest"], out["spot"] = epoch_of(ts), float(row[1])
    if ts not in _status_cache:
        df, spot, carried = load_frame_cf(con, ts)
        lv = GL.compute_levels(df, spot) if len(df) else {}
        per = {}
        for s in GL.SOURCES:
            sub = df[df["src"] == s]
            if len(sub) == 0:
                continue
            ratio = float((sub["strike_nq"] / sub["strike"]).median())
            per[s] = dict(n=int(len(sub)), ratio=ratio, spot=spot / ratio if ratio else None,
                          net_oi=float(sub["gex"].sum()), net_vol=float(sub["gex_vol"].sum()),
                          net_oi_ex0=float(sub[sub["dte"] > 0]["gex"].sum()), net_vol_ex0=float(sub[sub["dte"] > 0]["gex_vol"].sum()))
        zero = {}
        cache = _cache_con()
        try:
            for b in ("OI", "VOL"):
                got = levels_for_ts(con, cache, ts, GL.ALL_SRC, b.lower(), "zero")
                for s_, v in got.items():
                    zero.setdefault(s_, {})[b] = dict(call=v[0], put=v[1], flip=v[2])
        finally:
            cache.close()
        _status_cache.clear()
        _status_cache[ts] = dict(levels=lv, levels_zero=zero, sources=per, carried=carried)
    out.update(_status_cache[ts])
    return out


def api_bars(con, q):
    lo, hi, tf = int(q["from"]), int(q["to"]), max(1, int(q.get("tf", 1)))
    rows = []
    if has_table(con, "bars"):
        rows = [r for r in con.execute("SELECT t,o,h,l,c,v FROM bars WHERE t >= ? AND t <= ? ORDER BY t", (lo - tf * 60, hi)).fetchall()
                if r[1] is not None and r[2] is not None and r[3] is not None and r[4] is not None]
    if rows:
        df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v"])
        if tf > 1:
            df["g"] = (df["t"] // (60 * tf)) * 60 * tf
            df = df.groupby("g").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index().rename(columns={"g": "t"})
        df = df[df["t"] >= lo]
        out = [[int(r.t), r.o, r.h, r.l, r.c, r.v] for r in df.itertuples()]
        return dict(kind="candles", tf=tf, data=out)
    snaps = snapshot_list(con, lo, hi)
    return dict(kind="ticks", tf=tf, data=[[e, s] for e, _t, s in snaps])


def api_history(con, q):
    """One row per New York date from levels_history.txt (written by gex_levels.py)."""
    days = []
    if os.path.exists(GL.OUT_HIST):
        with open(GL.OUT_HIST, encoding="utf-8") as f:
            for ln in f:
                r = GL._parse_line(ln)
                if not r:
                    continue
                meta = ln.split(";")[0].split("|")
                lv = {"|".join(k): float(v) for k, v in r[1].items()}
                days.append(dict(date=r[0], tw_time=meta[2], spot=float(meta[3]), levels=lv))
    return dict(days=days)



# ----------------------------- extra heat maps: GEX pressure profile and charm -----------------------------
def _frame(con, ts, srcs, scope):
    df, spot, _c = load_frame_cf(con, ts)
    if srcs and set(srcs) != set(GL.SOURCES):
        df = df[df["src"].isin(srcs)]
    if scope == "ex0":
        df = df[df["dte"] > 0]
    elif scope == "zero":
        df = df[df["dte"] == 0]
    return df, spot


def _profile_col(df, spot, bucket, wcol):
    """Net GEX if the NQ price were at each price level (same sticky-strike curve the flip is read from), $M per 1 %."""
    if df.empty or spot is None:
        return []
    g0 = math.ceil(spot * (1 - BAND) / bucket) * bucket
    g1 = math.floor(spot * (1 + BAND) / bucket) * bucket
    grid = np.arange(g0, g1 + bucket, bucket, dtype=float)
    prof = np.zeros(len(grid))
    cp = np.where(df["cp"].values == "C", 1.0, -1.0)
    Fv, Kv, Tv, iv, qv, wv, mv = (df[k].values.astype(float) for k in ("F", "strike", "T", "iv", "q", wcol, "mult"))
    for a in range(0, len(df), 400):
        sl = slice(a, a + 400)
        Fg = Fv[sl, None] * (grid[None, :] / spot)
        sd = (iv[sl] * np.sqrt(Tv[sl]))[:, None]
        with np.errstate(all="ignore"):
            d1 = (np.log(Fg / Kv[sl, None]) + (GL.RISK_FREE - qv[sl, None] + 0.5 * iv[sl, None] ** 2) * Tv[sl, None]) / sd
            g = np.exp(-qv[sl, None] * Tv[sl, None]) * np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * np.pi) / (Fg * sd)
            part = cp[sl, None] * g * wv[sl, None] * mv[sl, None] * Fg ** 2 * 0.01
        prof += np.nan_to_num(part, nan=0.0, posinf=0.0, neginf=0.0).sum(axis=0)
    prof /= 1e6
    out = []
    for x, v in zip(grid, prof):
        out += [int(x), round(max(v, 0.0), 2), round(min(v, 0.0), 2)]
    return out


def _charm_exposure(df, wcol):
    """Dollar delta that the book gains per calendar day from the passage of time (Black-Scholes charm), call +/put - convention."""
    from scipy.special import ndtr
    F, K, T, iv, q, w, m = (df[k].values.astype(float) for k in ("F", "strike", "T", "iv", "q", wcol, "mult"))
    r = GL.RISK_FREE
    with np.errstate(all="ignore"):
        sd = iv * np.sqrt(T)
        d1 = (np.log(F / K) + (r - q + 0.5 * iv ** 2) * T) / sd
        d2 = d1 - sd
        pdf = np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * np.pi)
        term = np.exp(-q * T) * pdf * (2 * (r - q) * T - d2 * sd) / (2 * T * sd)
        is_c = df["cp"].values == "C"
        ch = np.where(is_c, q * np.exp(-q * T) * ndtr(d1) - term, -q * np.exp(-q * T) * ndtr(-d1) - term)
        expo = np.where(is_c, 1.0, -1.0) * ch / 365.0 * w * m * F
    return np.nan_to_num(expo, nan=0.0, posinf=0.0, neginf=0.0)


def _charm_col(df, spot, bucket, wcol):
    if df.empty or spot is None:
        return []
    e = _charm_exposure(df, wcol)
    bins = (np.round(df["strike_nq"].values / bucket) * bucket).astype(int)
    keep = np.abs(df["strike_nq"].values - spot) <= spot * BAND
    t = pd.DataFrame(dict(b=bins[keep], c=np.where(df["cp"].values[keep] == "C", e[keep], 0.0), p=np.where(df["cp"].values[keep] == "P", e[keep], 0.0)))
    g = t.groupby("b").sum().sort_index()
    out = []
    for b, row in g.iterrows():
        out += [int(b), round(row["c"] / 1e6, 3), round(row["p"] / 1e6, 3)]
    return out


def api_heat_x(con, q):
    lo, hi = int(q["from"]), int(q["to"])
    mode = q.get("mode", "profile")
    srcs = [s for s in q.get("src", "NQ,QQQ,NDX").split(",") if s]
    basis, scope, bucket = q.get("basis", "oi"), q.get("scope", "all"), max(1, int(float(q.get("bucket", 25))))
    snaps = bucket_thin(snapshot_list(con, lo, hi), hi - lo)
    key = "|".join([mode, ",".join(srcs), basis, scope, str(bucket)])
    wcol = "oi" if basis == "oi" else "vol"
    cache = _cache_con()
    try:
        got = {}
        ts_list = [s[1] for s in snaps]
        for i in range(0, len(ts_list), 500):
            part = ts_list[i:i + 500]
            for ts, blob in cache.execute(f"SELECT ts, blob FROM hx WHERE k=? AND ts IN ({','.join('?' * len(part))})", [key] + part):
                got[ts] = json.loads(blob)
        new = []
        for _e, ts, _sp in snaps:
            if ts in got:
                continue
            df, spot = _frame(con, ts, srcs, scope)
            col = _profile_col(df, spot, bucket, wcol) if mode == "profile" else _charm_col(df, spot, bucket, wcol)
            got[ts] = col
            new.append((key, ts, json.dumps(col, separators=(",", ":"))))
        if new:
            with _cache_lock:
                cache.executemany("INSERT OR REPLACE INTO hx VALUES (?,?,?)", new)
                cache.commit()
        return dict(bucket=bucket, times=[s[0] for s in snaps], spots=[s[2] for s in snaps], cols=[got.get(s[1], []) for s in snaps])
    finally:
        cache.close()


# ----------------------------- whole book -----------------------------
def api_book(con, q):
    """Per source: how the whole option book is tilted (gamma, charm, put/call volume).  No trade prints are used."""
    scope = q.get("scope", "all")
    t = int(q["t"]) if q.get("t") else None
    row = (con.execute("SELECT ts FROM snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (iso(t),)).fetchone() if t
           else con.execute("SELECT ts FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone())
    if not row:
        return dict(books={})
    ts = row[0]
    df, spot = _frame(con, ts, [], scope)
    books = {}
    for s in GL.SOURCES:
        sub = df[df["src"] == s]
        if len(sub) < 10:
            continue
        is_c = sub["cp"].values == "C"
        ch_oi, ch_vol = _charm_exposure(sub, "oi"), _charm_exposure(sub, "vol")
        books[s] = dict(
            n=int(len(sub)),
            gex_oi=[float(sub["gex"].sum()), float(sub["gex"].abs().sum())],
            gex_vol=[float(sub["gex_vol"].sum()), float(sub["gex_vol"].abs().sum())],
            charm_oi=[float(ch_oi.sum()), float(np.abs(ch_oi).sum())],
            charm_vol=[float(ch_vol.sum()), float(np.abs(ch_vol).sum())],
            call_vol=float(sub["vol"].values[is_c].sum()), put_vol=float(sub["vol"].values[~is_c].sum()),
            call_oi=float(sub["oi"].values[is_c].sum()), put_oi=float(sub["oi"].values[~is_c].sum()),
        )
    return dict(t=epoch_of(ts), scope=scope, books=books)


# ----------------------------- notable activity (volume surges) -----------------------------
def _surge_frame(con, ts):
    return pd.read_sql("SELECT src, expiry, strike, cp, dte, strike_nq, vol, gex_vol FROM contracts WHERE ts = ?", con, params=(ts,))


def api_events(con, q):
    lo, hi = int(q["from"]), int(q["to"])
    srcs = [s for s in q.get("src", "NQ,QQQ,NDX").split(",") if s]
    if "vol" not in GL._cols(con):
        return dict(events=[])
    snaps = bucket_thin(snapshot_list(con, lo - 600, hi), 0, secs=300)
    events, last_seen, prev, prev_e = [], {}, None, None
    for e, ts, _sp in snaps:
        cur = _surge_frame(con, ts)
        if prev is not None and len(cur):
            m = cur.merge(prev[["src", "expiry", "strike", "cp", "vol", "gex_vol"]], on=["src", "expiry", "strike", "cp"], how="left", suffixes=("", "_p"))
            m["dvol"] = (m["vol"] - m["vol_p"].fillna(0)).clip(lower=0)
            m["dgex"] = (m["gex_vol"] - m["gex_vol_p"].fillna(0)).abs() / 1e6
            for s in srcs:
                ms = m[m["src"] == s]
                pos = ms[ms["dvol"] > 0]
                if e < lo or len(pos) < 5:
                    continue
                typical = float(pos["dvol"].quantile(0.95))
                thr = max(30.0, 3.0 * typical)
                sub = ms[ms["dvol"] >= thr]
                tot = float(ms["dvol"].sum()) or 1.0
                for r in sub.sort_values("dvol", ascending=False).head(2).itertuples():
                    key = (s, r.expiry, r.strike, r.cp)
                    if e - last_seen.get(key, 0) < 1800:
                        continue
                    last_seen[key] = e
                    events.append(dict(type="surge", t=e, src=s, price=float(r.strike_nq), strike=float(r.strike), cp=r.cp, dte=int(r.dte),
                                       dvol=float(r.dvol), vol=float(r.vol), dgex=float(r.dgex), share=float(r.dvol) / tot,
                                       mins=round((e - prev_e) / 60.0, 1), mult_base=typical))
        prev, prev_e = cur, e
    return dict(events=events[-80:])


# ----------------------------- acceleration (gamma squeeze) tendency -----------------------------
SQ_STEP = 50.0      # NQ points: how far we look up / down the pressure curve


def _net_abs_at(df, spot, prices, wcol):
    """Net GEX ($M per 1 %) and gross |GEX| if the NQ price were at each of `prices` (same sticky-strike curve as the flip)."""
    if df.empty or spot is None:
        return [0.0] * len(prices), [0.0] * len(prices)
    grid = np.asarray(prices, dtype=float)
    net, gross = np.zeros(len(grid)), np.zeros(len(grid))
    cp = np.where(df["cp"].values == "C", 1.0, -1.0)
    Fv, Kv, Tv, iv, qv, wv, mv = (df[k].values.astype(float) for k in ("F", "strike", "T", "iv", "q", wcol, "mult"))
    for a in range(0, len(df), 400):
        sl = slice(a, a + 400)
        Fg = Fv[sl, None] * (grid[None, :] / spot)
        sd = (iv[sl] * np.sqrt(Tv[sl]))[:, None]
        with np.errstate(all="ignore"):
            d1 = (np.log(Fg / Kv[sl, None]) + (GL.RISK_FREE - qv[sl, None] + 0.5 * iv[sl, None] ** 2) * Tv[sl, None]) / sd
            g = np.exp(-qv[sl, None] * Tv[sl, None]) * np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * np.pi) / (Fg * sd)
            part = cp[sl, None] * g * wv[sl, None] * mv[sl, None] * Fg ** 2 * 0.01
        part = np.nan_to_num(part, nan=0.0, posinf=0.0, neginf=0.0)
        net += part.sum(axis=0)
        gross += np.abs(part).sum(axis=0)
    return list(net / 1e6), list(gross / 1e6)


def _squeeze_row(df, spot, wcol):
    """[net0, netUp, netDn, gross0, up_score, dn_score]  (scores 0-100, a heuristic, NOT a validated forecast).
    environment = how much of the book is net short gamma at the current price (net < 0);
    slope       = how much MORE negative the net gets 50 points further in that direction (feedback that feeds on itself)."""
    net, gross = _net_abs_at(df, spot, [spot, spot + SQ_STEP, spot - SQ_STEP], wcol)
    g0 = max(gross[0], 1e-9)
    env = min(1.0, max(0.0, -net[0] / g0))
    up = min(1.0, max(0.0, (net[0] - net[1]) / g0))
    dn = min(1.0, max(0.0, (net[0] - net[2]) / g0))
    return [round(net[0], 2), round(net[1], 2), round(net[2], 2), round(gross[0], 2),
            round(100 * (0.5 * env + 0.5 * up), 1), round(100 * (0.5 * env + 0.5 * dn), 1)]


def api_squeeze(con, q):
    lo, hi = int(q["from"]), int(q["to"])
    srcs = [s for s in q.get("src", "NQ,QQQ,NDX").split(",") if s]
    basis, scope = q.get("basis", "oi"), q.get("scope", "all")
    snaps = bucket_thin(snapshot_list(con, lo, hi), hi - lo)
    key = "sq1|" + "|".join([",".join(srcs), basis, scope])
    wcol = "oi" if basis == "oi" else "vol"
    cache = _cache_con()
    try:
        got = {}
        ts_list = [s[1] for s in snaps]
        for i in range(0, len(ts_list), 500):
            part = ts_list[i:i + 500]
            for ts, blob in cache.execute(f"SELECT ts, blob FROM hx WHERE k=? AND ts IN ({','.join('?' * len(part))})", [key] + part):
                got[ts] = json.loads(blob)
        new = []
        for _e, ts, _sp in snaps:
            if ts in got:
                continue
            df, spot = _frame(con, ts, srcs, scope)
            row = _squeeze_row(df, spot, wcol)
            got[ts] = row
            new.append((key, ts, json.dumps(row, separators=(",", ":"))))
        if new:
            with _cache_lock:
                cache.executemany("INSERT OR REPLACE INTO hx VALUES (?,?,?)", new)
                cache.commit()
        return dict(step=SQ_STEP, times=[s[0] for s in snaps], rows=[got.get(s[1]) for s in snaps])
    finally:
        cache.close()


def api_flowmom(con, q):
    """orderflow 動能(取代 gamma squeeze 的「加速風險」副圖):
    讀 orderflow_history.jsonl(每秒一筆),落 60 秒桶,回 times(epoch 秒)+ rows=[cum_delta, net_delta, buy, sell]。
    cum_delta=視窗內累積 Delta(線);net_delta=每分鐘主動買賣淨量(柱)。無資料回空。"""
    lo, hi = int(q["from"]), int(q["to"])
    step = 60
    recs = []
    try:
        with open(ORDERFLOW_HISTORY_PATH, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                ra = r.get("received_at") or 0
                if lo <= ra <= hi:
                    recs.append((ra, r))
    except Exception:
        return dict(step=step, times=[], rows=[])
    if not recs:
        return dict(step=step, times=[], rows=[])

    def fnum(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    recs.sort(key=lambda x: x[0])
    buckets = {}   # bucket 起始秒 -> [delta, buy, sell]
    for ra, r in recs:
        b = int(ra // step) * step
        d = buckets.setdefault(b, [0.0, 0.0, 0.0])
        d[0] += fnum(r.get("delta"))
        d[1] += fnum(r.get("delta_buy"))
        d[2] += fnum(r.get("delta_sell"))
    times, rows, cum = [], [], 0.0
    for b in sorted(buckets):
        d = buckets[b]
        cum += d[0]
        rows.append([round(cum, 1), round(d[0], 1), round(d[1], 1), round(d[2], 1)])
        times.append(b)
    return dict(step=step, times=times, rows=rows)


# ----------------------------- 0DTE gamma share -----------------------------
def api_dte(con, q):
    """Time series of what fraction of total |GEX| sits in 0DTE (dte = 0) contracts: [share %, gross0 $M, grossAll $M]."""
    lo, hi = int(q["from"]), int(q["to"])
    srcs = [s for s in q.get("src", "NQ,QQQ,NDX").split(",") if s]
    basis = q.get("basis", "oi")
    snaps = bucket_thin(snapshot_list(con, lo, hi), hi - lo)
    key = "dte|" + "|".join([",".join(srcs), basis])
    gcol = GL.BASES[basis.upper()][0]
    cache = _cache_con()
    try:
        got = {}
        ts_list = [s[1] for s in snaps]
        for i in range(0, len(ts_list), 500):
            part = ts_list[i:i + 500]
            for ts, blob in cache.execute(f"SELECT ts, blob FROM hx WHERE k=? AND ts IN ({','.join('?' * len(part))})", [key] + part):
                got[ts] = json.loads(blob)
        new = []
        for _e, ts, _sp in snaps:
            if ts in got:
                continue
            df, _spot = _frame(con, ts, srcs, "all")
            row = [0.0, 0.0, 0.0]
            if not df.empty:
                gall = float(df[gcol].abs().sum())
                g0 = float(df.loc[df["dte"] == 0, gcol].abs().sum())
                row = [round(100.0 * g0 / gall if gall > 0 else 0.0, 2), round(g0 / 1e6, 2), round(gall / 1e6, 2)]
            got[ts] = row
            new.append((key, ts, json.dumps(row, separators=(",", ":"))))
        if new:
            with _cache_lock:
                cache.executemany("INSERT OR REPLACE INTO hx VALUES (?,?,?)", new)
                cache.commit()
        return dict(times=[s[0] for s in snaps], rows=[got.get(s[1]) for s in snaps])
    finally:
        cache.close()


def _clamp(x, a=-1.0, b=1.0):
    if x is None:      # 空值 = 沒訊號,歸 0,不要讓它把整個導師分析炸掉
        return 0.0
    return a if x < a else b if x > b else x


def _fnum(x):
    if x is None:
        return "—"
    x = float(x)
    return str(int(round(x))) if abs(x - round(x)) < 1e-6 else f"{x:.1f}"


def _dte0_band(con, latest, src, spot):
    """0DTE 日內預期區間:spot ± spot × ATM_IV(0DTE) × √(T),T = 0DTE 到期(4pm ET)前的年化時間。
    隨收盤逼近自動收窄。回傳 dict(up, dn, iv, em, hours) 或 None。"""
    e = exprs(con)
    rows = con.execute(
        f"SELECT {e['sk']}, iv, T FROM contracts WHERE ts=? AND {e['src']}=? AND dte=0 AND iv>0 AND T>0",
        (latest, src)).fetchall()
    if not rows:
        return None
    best = min(rows, key=lambda r: abs(float(r[0]) - spot))
    pool = [r for r in rows if abs(float(r[0]) - float(best[0])) < 0.01]
    ivs = [float(r[1]) for r in pool]
    if not ivs:
        return None
    iv0 = sum(ivs) / len(ivs)
    T = float(best[2])
    em = spot * iv0 * math.sqrt(T) if T > 0 else 0.0
    return dict(up=round(spot + em, 1), dn=round(spot - em, 1), iv=round(iv0 * 100, 1),
                em=round(em, 1), hours=round(T * 365.0 * 24.0, 1))


def _dte0_share(con, latest, src):
    """0DTE 佔總 |GEX| 的比例(%),衡量 0DTE 對盤面的主導程度。"""
    e = exprs(con)
    r = con.execute(
        f"SELECT SUM(CASE WHEN dte=0 THEN ABS(gex) ELSE 0 END), SUM(ABS(gex)) "
        f"FROM contracts WHERE ts=? AND {e['src']}=?", (latest, src)).fetchone()
    if not r or not r[1]:
        return None
    return round(100.0 * float(r[0]) / float(r[1]), 1)


def _dte0_flow(con, src):
    """0DTE 最新兩次快照間、成交量增量最大的履約價(尾盤釘價/磁吸區的候選)。"""
    e = exprs(con)
    sk = e["sk"]
    ts_list = [r[0] for r in con.execute("SELECT ts FROM snapshots ORDER BY ts DESC LIMIT 2").fetchall()]
    if len(ts_list) < 2:
        return []
    latest, prev = ts_list[0], ts_list[1]

    def sd(ts):
        return con.execute(
            f"SELECT {sk}, cp, SUM(vol) FROM contracts WHERE ts=? AND {e['src']}=? AND dte=0 GROUP BY {sk}, cp",
            (ts, src)).fetchall()

    d = {}
    for strike, cp, v in sd(latest):
        d[(int(round(float(strike))), cp)] = float(v or 0)
    for strike, cp, v in sd(prev):
        k = (int(round(float(strike))), cp)
        d[k] = d.get(k, 0) - float(v or 0)
    agg = {}
    for (strike, cp), dv in d.items():
        if dv > 0:
            a = agg.setdefault(strike, dict(strike=strike, call=0, put=0))
            if cp == "C":
                a["call"] += round(dv)
            else:
                a["put"] += round(dv)
    out = [dict(strike=k, call=v["call"], put=v["put"], dv=v["call"] + v["put"]) for k, v in agg.items()]
    out.sort(key=lambda x: -x["dv"])
    return out[:6]


def _big_flow(con, latest, src):
    """大單成交(0DTE):最近 ~30 分鐘內 0DTE 大量成交(Call/Put)的彙總,用跟 api_events 相同的 surge 偵測
    (新增量 ≥ 30 口且 ≥ 同來源前 5% 的 3 倍),跨多個快照累加。上下一起開單(both)= 這段時間 Call 與 Put
    都出現過大量成交,常見於跨式/勒式、避險或吸收。注意:這是真實成交量,無買賣方向,只能看「量堆在哪」。"""
    e = exprs(con)
    t = epoch_of(latest)
    snaps = bucket_thin(snapshot_list(con, t - 2400, t), 0, secs=300)
    if len(snaps) < 2:
        return None
    cols = f"{e['sk']} AS strike, cp, vol, oi"
    frames = {}
    for _ep, ts, _sp in snaps:
        df = pd.read_sql(f"SELECT {cols} FROM contracts WHERE ts=? AND {e['src']}=? AND dte=0", con, params=(ts, src))
        if not df.empty:
            frames[ts] = df
    if len(frames) < 2:
        return None
    keys = sorted(frames.keys())
    call_dv = put_dv = 0.0
    top = []
    seen = set()
    # 未平倉量(OI)增減:視窗首幀 → 末幀。量放大 + OI 增 = 主力開新倉(建倉);量放大 + OI 減 = 平倉(獲利了結/解套)。
    oi_chg = frames[keys[-1]].merge(frames[keys[0]], on=["strike", "cp"], how="left", suffixes=("", "_f"))
    oi_chg["doi"] = oi_chg["oi"] - oi_chg["oi_f"].fillna(oi_chg["oi"])
    oi_map = {(round(float(r.strike), 1), r.cp): float(r.doi) for r in oi_chg.itertuples()}
    call_doi = float(oi_chg[oi_chg["cp"] == "C"]["doi"].sum())
    put_doi = float(oi_chg[oi_chg["cp"] == "P"]["doi"].sum())
    for i in range(1, len(keys)):
        a, b = frames[keys[i]], frames[keys[i - 1]]
        m = a.merge(b, on=["strike", "cp"], how="left", suffixes=("", "_p"))
        m["dvol"] = (m["vol"] - m["vol_p"].fillna(0)).clip(lower=0)
        pos = m[m["dvol"] > 0]
        if len(pos) < 5:
            continue
        thr = max(30.0, 3.0 * float(pos["dvol"].quantile(0.95)))
        big = m[m["dvol"] >= thr]
        for r in big.itertuples():
            key = (round(float(r.strike), 1), r.cp)
            if key in seen:
                continue
            seen.add(key)
            dvol = float(r.dvol)
            if r.cp == "C":
                call_dv += dvol
            else:
                put_dv += dvol
            top.append(dict(strike=round(float(r.strike), 1), cp=r.cp, dvol=int(dvol), doi=oi_map.get(key, 0.0)))
    top.sort(key=lambda x: -x["dvol"])
    top = top[:6]
    both = (call_dv > 0 and put_dv > 0)
    tot = call_dv + put_dv
    return dict(call=int(round(call_dv)), put=int(round(put_dv)), both=both,
                net=int(round(call_dv - put_dv)), skew=round((call_dv - put_dv) / tot, 2) if tot else 0.0,
                call_oi=int(round(call_doi)), put_oi=int(round(put_doi)),
                top=top, n=int(len(top)))


def _thesis_src(con, src, lo, t, latest, spot, snaps):
    """Score ONE source's signals -> (raw, signals, ctx)."""
    ret1 = ret4 = None
    if len(snaps) >= 2:
        last_spot = snaps[-1][2]

        def base_at(back):
            base = min(snaps, key=lambda s: abs(s[0] - (t - back)))
            if base[2] and last_spot and base[0] < snaps[-1][0]:
                return last_spot / base[2] - 1.0
            return None

        ret1 = base_at(3600)
        ret4 = base_at(4 * 3600)

    net_all = net_ex0 = net_zero = None
    n3 = api_net3(con, {"from": lo, "to": t, "src": src, "basis": "oi", "maxcols": 60})
    if n3.get("all"):
        net_all = n3["all"][-1][1]
    if n3.get("ex0"):
        net_ex0 = n3["ex0"][-1][1]
    if n3.get("zero"):
        net_zero = n3["zero"][-1][1]

    pcr_vol = pcr_vol0 = None
    pc = api_pcr(con, {"from": lo, "to": t, "src": src, "maxcols": 60})
    for k, arr in (pc.get("series") or {}).items():
        if arr:
            if k == "vol":
                pcr_vol = arr[-1][1]
            elif k == "vol0":
                pcr_vol0 = arr[-1][1]

    sk = None
    sks = (api_skew(con, {"from": lo, "to": t, "src": src, "maxcols": 60}).get("series")) or []
    if sks:
        sk = sks[-1][1]

    band_up = band_dn = atm_iv = dte = None
    bnd = (api_band(con, {"from": lo, "to": t, "src": src, "maxcols": 60}).get("series")) or []
    if bnd:
        band_up, band_dn, atm_iv, dte = bnd[-1][2], bnd[-1][3], bnd[-1][4], bnd[-1][5]

    call_dv = put_dv = 0
    for r in api_flow(con, {"src": src, "n": 15}).get("rows") or []:
        call_dv += r["call_dv"]
        put_dv += r["put_dv"]

    flip = call_wall = put_wall = None
    flip0 = call_wall0 = put_wall0 = None
    cache = _cache_con()
    try:
        v = levels_for_ts(con, cache, latest, [src], "oi", "all").get(src)
        if v:
            call_wall, put_wall, flip = (None if x is None else float(x) for x in v[:3])
        v0 = levels_for_ts(con, cache, latest, [src], "oi", "zero").get(src)
        if v0:
            call_wall0, put_wall0, flip0 = (None if x is None else float(x) for x in v0[:3])
    finally:
        cache.close()
    share0 = _dte0_share(con, latest, src)
    band0 = _dte0_band(con, latest, src, spot)
    flow0 = _dte0_flow(con, src)
    big0 = _big_flow(con, latest, src)

    signals = []

    def add(sid, name, value, s, weight, text):
        s = _clamp(s)
        if s == 0:
            s = 0.0
        signals.append(dict(id=sid, name=name, value=value, s=round(s, 3), weight=weight, text=text))

    if ret4 is not None:
        sm = _clamp(0.6 * _clamp(ret4 / 0.015) + 0.4 * _clamp((ret1 or 0.0) / 0.004))
        val = f"4小時 {ret4*100:+.2f}%" + (f" / 1小時 {ret1*100:+.2f}%" if ret1 is not None else "")
        add("mom", "價格動量", val, sm, 3, "近期價格往上 = 趨勢偏多;往下 = 偏空。所有訊號裡最直覺、最有實證的一條。")
    if net_all is not None or flip is not None:
        gs = []
        if net_all is not None:
            gs.append(_clamp(net_all / 1000.0))
        if flip is not None:
            gs.append(_clamp((spot - flip) / (spot * 0.01)))
        ge = _clamp(sum(gs) / len(gs))
        val = f"淨GEX(含0DTE) {_fnum(net_all)}M"
        if flip is not None:
            val += f" · 現價{'高於' if spot > flip else '低於'} flip {_fnum(flip)}"
        add("gma", "Gamma 環境", val, ge, 2, "正 Gamma(現價在 flip 之上、淨GEX為正)= 盤勢較穩、易均值回歸;負 Gamma = 波動放大。改含 0DTE,反映當下真實的 gamma 環境。")
    if net_zero is not None or flip0 is not None:
        gs0 = []
        if net_zero is not None:
            gs0.append(_clamp(net_zero / 500.0))
        if flip0 is not None:
            gs0.append(_clamp((spot - flip0) / (spot * 0.005)))
        ge0 = _clamp(sum(gs0) / len(gs0))
        val0 = f"0DTE 淨GEX {_fnum(net_zero)}M"
        if flip0 is not None:
            val0 += f" · 現價{'高於' if spot > flip0 else '低於'} 0DTE flip {_fnum(flip0)}"
        add("gma0", "0DTE Gamma", val0, ge0, 2, "0DTE 是日內主導力量:0DTE 正 gamma = 易被釘價、區間震;0DTE 負 gamma = 接近/穿越 flip 時波動放大。")
    if pcr_vol is not None:
        add("pcr", "Put/Call 比", f"成交量 P/C = {pcr_vol}", _clamp((1.0 - pcr_vol) / 0.5), 2, ">1 表示 Put 較熱 = 避險買盤較強(偏空);<1 表示 Call 較熱(偏多)。成交量只看「熱度比例」,不含買賣方向。")
    if pcr_vol0 is not None:
        add("pcr0", "0DTE P/C", f"0DTE 成交量 P/C = {pcr_vol0}", _clamp((1.0 - pcr_vol0) / 0.5), 1, "0DTE 的 Put/Call 熱度,反映當下最即時的避險/追價情緒。")
    if sk is not None:
        add("skw", "IV 偏斜", f"25Δ 風險逆轉 = {sk:+} vol點", _clamp(-sk / 5.0), 1, "正值 = OTM Put 較貴(買保護、防禦情緒,偏空);負值 = Call 較貴(追漲,偏多)。")
    if band_up and band_dn and band_up > band_dn:
        mid = (band_up + band_dn) / 2.0
        half = (band_up - band_dn) / 2.0 or 1.0
        add("bnd", "預期波動帶", f"現價 {_fnum(spot)} 於 {_fnum(band_dn)}–{_fnum(band_up)}", _clamp(-(spot - mid) / half), 1, "貼近今日預期震幅上緣 = 短線偏過熱(易回);貼近下緣 = 偏超跌(易彈)。")
    tot_dv = call_dv + put_dv
    if tot_dv > 0:
        add("flw", "成交增量", f"Call 增量 {call_dv} / Put 增量 {put_dv}", _clamp((call_dv - put_dv) / float(tot_dv)), 2, "最活躍履約價裡,Call 成交量增加較多 = 短線偏多;Put 較多 = 偏空。這是「熱度增量」,不是方向訂單流。")
    if big0 and big0.get("n"):
        both = big0["both"]
        s_big = _clamp(big0["skew"]) * (0.5 if both else 1.0)
        val_big = f"大單 Call {big0['call']} / Put {big0['put']}" + (" · 上下同開" if both else "")
        add("big", "大單成交", val_big, s_big, 2, "0DTE 大量成交的 Call/Put 差:Call 大單較多 = 短線偏多;Put 較多 = 偏空。上下同開(both)= 跨式/勒式或避險/吸收,方向不明但波動會放大。無買賣方向,僅看量堆在哪。")

    raw = 0.0
    if signals:
        tw = sum(x["weight"] for x in signals)
        raw = sum(x["s"] * x["weight"] for x in signals) / tw
    return raw, signals, dict(ret1=ret1, ret4=ret4, net_all=net_all, net_ex0=net_ex0, net_zero=net_zero,
                              pcr_vol=pcr_vol, pcr_vol0=pcr_vol0, sk=sk,
                              band_up=band_up, band_dn=band_dn, atm_iv=atm_iv, dte=dte, call_dv=call_dv, put_dv=put_dv,
                              flip=flip, call_wall=call_wall, put_wall=put_wall,
                              flip0=flip0, call_wall0=call_wall0, put_wall0=put_wall0, share0=share0, band0=band0, flow0=flow0, big0=big0)


def _structure(con, latest, spot, sigma):
    """0DTE 參與者結構:NQ/QQQ/NDX 三個市場的 0DTE 成交量增量與未平倉量,按 價內/價外/深價外 分層讀情緒。
    所有履約價都用 strike_nq 換算成 NQ 等效價,所以能放在同一把尺(現價 spot)下比較。"""
    ts_list = [r[0] for r in con.execute("SELECT ts FROM snapshots ORDER BY ts DESC LIMIT 2").fetchall()]
    e = exprs(con)
    srcs = ["NQ", "QQQ", "NDX"]

    def strike_data(ts, src):
        rows = con.execute(f"SELECT {e['sk']}, cp, SUM(vol), SUM(oi) FROM contracts WHERE ts=? AND {e['src']}=? AND dte=0 GROUP BY {e['sk']}, cp", (ts, src)).fetchall()
        vd, od = {}, {}
        for strike, cp, v, o in rows:
            key = (float(strike), cp)
            vd[key] = float(v or 0)
            od[key] = float(o or 0)
        return vd, od

    def bucket_of(K, cp):
        if cp == "C":
            return 0 if K < spot else 1 if K <= spot + sigma else 2
        return 0 if K > spot else 1 if K >= spot - sigma else 2

    def one(src):
        av, ao = strike_data(ts_list[0], src) if ts_list else ({}, {})
        bv, _bo = strike_data(ts_list[1], src) if len(ts_list) > 1 else ({}, {})
        buckets = [dict(label="價內", call_dv=0, put_dv=0, call_oi=0, put_oi=0),
                   dict(label="價外", call_dv=0, put_dv=0, call_oi=0, put_oi=0),
                   dict(label="深價外", call_dv=0, put_dv=0, call_oi=0, put_oi=0)]
        for (K, cp), v in av.items():
            b = buckets[bucket_of(K, cp)]
            dv = max(0.0, v - bv.get((K, cp), 0.0))
            if cp == "C":
                b["call_dv"] += dv
                b["call_oi"] += ao.get((K, cp), 0.0)
            else:
                b["put_dv"] += dv
                b["put_oi"] += ao.get((K, cp), 0.0)
        for b in buckets:
            for k in ("call_dv", "put_dv", "call_oi", "put_oi"):
                b[k] = int(round(b[k]))
        tot_cd = sum(b["call_dv"] for b in buckets)
        tot_pd = sum(b["put_dv"] for b in buckets)
        o, d, itm = buckets[1], buckets[2], buckets[0]
        notes = []
        if tot_pd > tot_cd * 1.3:
            notes.append("Put 成交量增量明顯多於 Call,避險/防禦買盤較強")
        if tot_cd > tot_pd * 1.3:
            notes.append("Call 成交量增量明顯多於 Put,追漲情緒較強")
        if d["put_dv"] >= d["call_dv"] and d["put_dv"] > 0:
            notes.append("深價外 Put 有量,有人在買便宜保險防尾端下跌(恐慌型避險)")
        if d["call_dv"] > d["put_dv"] and d["call_dv"] > 0:
            notes.append("深價外 Call 有量,樂透單/追高(FOMO)")
        if itm["call_dv"] > itm["put_dv"] and itm["call_dv"] > 0:
            notes.append("價內 Call 有量,較像大戶調倉/看多,而非散戶追價")
        if itm["put_dv"] > itm["call_dv"] and itm["put_dv"] > 0:
            notes.append("價內 Put 有量,較像機構對沖下檔風險")
        if tot_pd > tot_cd * 1.5:
            verdict = "偏恐慌/避險"
        elif tot_cd > tot_pd * 1.5:
            verdict = "偏追漲(FOMO)"
        else:
            verdict = "多空相當,情緒中性"
        return dict(src=src, verdict=verdict, buckets=buckets, notes=notes, call_dv=int(tot_cd), put_dv=int(tot_pd))

    return dict(sigma=round(sigma, 1), markets=[one(s) for s in srcs])


def _dealer_stance(spot, flip, net_g):
    if flip is None:
        return "做市商對沖立場:0DTE flip 資料不足,無法判斷"
    if spot > flip:
        return (f"做市商目前是正 gamma(現價 {_fnum(spot)} 在 0DTE flip {_fnum(flip)} 之上,0DTE 淨GEX {_fnum(net_g)}M):"
                f"對沖時「買低賣高」,下跌到支撐會承接、上漲到壓力會賣出,傾向把價格拉回 flip,波動被壓住")
    return (f"做市商目前是負 gamma(現價 {_fnum(spot)} 在 0DTE flip {_fnum(flip)} 之下,0DTE 淨GEX {_fnum(net_g)}M):"
            f"對沖時「追漲殺跌」,價格離 flip 越遠對沖力道越大,波動被放大,趨勢一啟動容易加速")


def _load_orderflow(cfg):
    """讀 orderflow.json(Windows addon 每秒 POST 過來的小包)。回傳 dict 或 None(檔案不存在)。"""
    max_age = cfg.get("orderflow_max_age_sec", DEFAULT_ORDERFLOW_MAX_AGE)
    max_lag = cfg.get("orderflow_max_lag_sec", DEFAULT_ORDERFLOW_MAX_LAG)
    try:
        with open(ORDERFLOW_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    now = time.time()
    received = data.get("received_at")
    age = (now - received) if received else None
    lag = data.get("lag_sec") if data.get("lag_sec") is not None else data.get("idle_sec")
    laggy = lag is not None and lag > max_lag
    available = (age is not None and age <= max_age) and not laggy
    stale = bool(age is not None and not available)
    return dict(data=data, age_sec=age, lag_sec=lag, stale=stale, available=available)


def _flow_signal(name, value, weight, text):
    return dict(name=name, value=value, s=_clamp(value), weight=weight, text=text)


def _orderflow_signals(cfg):
    """把 orderflow.json 轉成一組訊號(與 GEX 訊號同格式),供加權與顯示。永遠回傳 dict(不會是 None)。"""
    of = _load_orderflow(cfg)
    if of is None:
        return dict(available=False, stale=False, connected=False, age_sec=None, lag_sec=None,
                    signals=[], raw=0.0, spot=None)
    if not of["available"]:
        return dict(available=False, stale=of["stale"], connected=True, age_sec=of["age_sec"],
                    lag_sec=of["lag_sec"], signals=[], raw=0.0, spot=of["data"].get("spot"))
    d = of["data"]
    vol = float(d.get("vol") or 0)
    delta = float(d.get("delta") or 0)
    imb = float(d.get("imb") or 0)
    big_vol = float(d.get("big_vol") or 0)
    big = float(d.get("big") or 0)
    sw_buy = int(d.get("sweep_buy") or 0)
    sw_sell = int(d.get("sweep_sell") or 0)
    sweeps = sw_buy + sw_sell

    sigs = []
    if vol > 0:
        s = _clamp(delta / vol)
        sigs.append(_flow_signal("累積Delta", int(round(delta)), 2,
                                 f"窗口內累積Delta {delta:+.0f} 口 / 總成交 {vol:.0f} 口:{'買盤主動(偏多)' if s > 0 else '賣盤主動(偏空)'}"))
    if abs(imb) > 0.001:
        sigs.append(_flow_signal("訂單簿不平衡", round(imb, 3), 1,
                                 f"訂單簿不平衡 {imb:+.3f}:{'買方掛單較厚' if imb > 0 else '賣方掛單較厚'}"))
    if big_vol > 0:
        s = _clamp(big / big_vol)
        sigs.append(_flow_signal("大單淨量", int(round(big)), 2,
                                 f"大單淨量 {big:+.0f} 口 / 大單總量 {big_vol:.0f} 口:{'大單偏買' if s > 0 else '大單偏賣'}"))
    if sweeps > 0:
        net = sw_buy - sw_sell
        s = _clamp(net / float(sweeps))
        sigs.append(_flow_signal("掃單", sweeps, 1,
                                 f"掃單 {sw_buy} 買 / {sw_sell} 賣:{'買方掃單較多(偏多)' if s > 0 else '賣方掃單較多(偏空)'}"))
    tot_w = sum(x["weight"] for x in sigs)
    raw = sum(x["s"] * x["weight"] for x in sigs) / tot_w if tot_w else 0.0
    return dict(available=True, stale=False, connected=True, age_sec=of["age_sec"], lag_sec=of["lag_sec"],
                signals=sigs, raw=raw, spot=d.get("spot"),
                delta=delta, vol=vol, imb=imb, big=big, big_vol=big_vol,
                sweep_buy=sw_buy, sweep_sell=sw_sell)


def _orderflow_sentence(flow):
    """一行中文訂單流摘要,塞進規則版總結。"""
    if not flow or not flow.get("available"):
        if flow and flow.get("stale"):
            return "orderflow 目前斷線或過時(Bookmap 延遲偏高),本段暫不採信、僅用 GEX 結構推測。"
        if flow and flow.get("connected"):
            return "orderflow 已連線但資料過時,本段暫不採信、僅用 GEX 結構推測。"
        return "orderflow 尚未連線(Windows 那台還沒送資料),本段僅用 GEX 結構推測。"
    head = f"訂單流(約 {int(flow['age_sec'])} 秒前):"
    parts = []
    if flow.get("vol"):
        parts.append(f"累積Delta {flow['delta']:+.0f} 口 / 總成交 {flow['vol']:.0f} 口")
    if flow.get("imb"):
        parts.append(f"不平衡 {flow['imb']:+.3f}")
    if flow.get("big_vol"):
        parts.append(f"大單 {flow['big']:+.0f} / {flow['big_vol']:.0f} 口")
    if flow.get("sweep_buy") or flow.get("sweep_sell"):
        parts.append(f"掃單 {flow['sweep_buy']} 買 / {flow['sweep_sell']} 賣")
    if flow.get("stale"):
        parts.append("(⚠ Bookmap 延遲中,數據可能落後,謹慎)")
    return head + "、".join(parts) + "。"


def _orderflow_history(cfg, window_secs=3600):
    """把過去 window_secs 的 orderflow 歷史(orderflow_history.jsonl)彙整成一句中文摘要。
    歷史檔由 orderflow_receiver.py 每秒累積。無檔案/資料不足回 None。"""
    try:
        with open(ORDERFLOW_HISTORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return None
    now = time.time()
    cutoff = now - window_secs
    recs = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if rec.get("received_at", 0.0) >= cutoff:
            recs.append(rec)
    if not recs:
        return None
    recs.sort(key=lambda r: r.get("received_at", 0.0))
    n = len(recs)
    span_min = max(1, int((recs[-1].get("received_at", 0.0) - recs[0].get("received_at", 0.0)) / 60))

    def f(r, k):
        try:
            return float(r.get(k) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    delta_sum = sum(f(r, "delta") for r in recs)
    vol_sum = sum(f(r, "vol") for r in recs)
    big_sum = sum(f(r, "big") for r in recs)
    big_vol_sum = sum(f(r, "big_vol") for r in recs)
    sw_buy = int(sum(f(r, "sweep_buy") for r in recs))
    sw_sell = int(sum(f(r, "sweep_sell") for r in recs))
    spots = []
    for r in recs:
        v = f(r, "spot")
        if v > 0:
            spots.append(v)
    spot_first = spots[0] if spots else None
    spot_last = spots[-1] if spots else None
    spot_hi = max(spots) if spots else None
    spot_lo = min(spots) if spots else None

    parts = [f"orderflow 過去 {span_min} 分鐘(約 {n} 筆)"]
    if vol_sum > 0:
        side = "買方主動(偏多)" if delta_sum > 0 else "賣方主動(偏空)" if delta_sum < 0 else "多空打平"
        parts.append(f"累積Delta {delta_sum:+.0f} 口({side}) · 總成交 {vol_sum:.0f} 口")
    else:
        parts.append("這段時間幾乎沒有成交(清淡)")
    if big_vol_sum > 0:
        bside = "大單偏買" if big_sum > 0 else "大單偏賣" if big_sum < 0 else "大單多空打平"
        parts.append(f"大單淨 {big_sum:+.0f} 口 / 總量 {big_vol_sum:.0f} 口({bside})")
    if sw_buy or sw_sell:
        parts.append(f"掃單 買{sw_buy}/賣{sw_sell}")
    if spot_first is not None and spot_last is not None:
        chg = spot_last - spot_first
        if spot_hi is not None and spot_lo is not None:
            parts.append(f"現價 {spot_first:.1f} → {spot_last:.1f}({chg:+.1f};高 {spot_hi:.1f}/低 {spot_lo:.1f})")
        else:
            parts.append(f"現價 {spot_first:.1f} → {spot_last:.1f}({chg:+.1f})")
    if vol_sum > 0 and delta_sum != 0:
        t10 = now - 600
        delta_recent = sum(f(r, "delta") for r in recs if r.get("received_at", 0.0) >= t10)
        if delta_recent * delta_sum < 0:
            parts.append(f"近10分鐘 Delta {delta_recent:+.0f} 口(與前段方向相反,留意轉向)")
        elif abs(delta_recent / delta_sum) > 0.6:
            parts.append(f"近10分鐘 Delta {delta_recent:+.0f} 口(近段動能集中)")
    return "、".join(parts) + "。"


def _orderflow_book(cfg):
    """讀 orderflow.json 的掛單簿(book_bid/book_ask,現價 ±100 點)與成交熱區(trades),
    彙整成一段「流動性地圖」給導師看:哪裡有掛單牆、哪裡成交密集。無資料回 None。"""
    of = _load_orderflow(cfg)
    if of is None or not of["available"]:
        return None
    d = of["data"]
    bids = d.get("book_bid") or []
    asks = d.get("book_ask") or []
    trades = d.get("trades") or []
    if not bids and not asks and not trades:
        return None
    parts = []

    def _wall(items, n=6):
        out = []
        for it in sorted(items, key=lambda x: -(float(x[1]) if len(x) > 1 else 0.0))[:n]:
            try:
                p = _fnum(float(it[0]))
                s = int(round(float(it[1])))
            except (TypeError, ValueError):
                continue
            out.append(f"{p}({s}口)")
        return out

    if bids:
        parts.append("買方掛單牆(下方支撐):" + "、".join(_wall(bids)))
    if asks:
        parts.append("賣方掛單牆(上方壓力):" + "、".join(_wall(asks)))
    if trades:
        tz = []
        for it in trades[:6]:
            try:
                p, bv, sv = float(it[0]), float(it[1]), float(it[2])
            except (TypeError, ValueError):
                continue
            tz.append(f"{_fnum(p)}(買{int(round(bv))}/賣{int(round(sv))})")
        if tz:
            parts.append("近60秒成交熱區:" + "、".join(tz))
    tot_b = sum(float(x[1]) for x in bids if len(x) > 1)
    tot_a = sum(float(x[1]) for x in asks if len(x) > 1)
    if tot_b + tot_a > 0:
        parts.append(f"100點內買掛單 {int(round(tot_b))} 口 vs 賣掛單 {int(round(tot_a))} 口")
    return "orderflow 掛單簿(±100點):" + ";".join(parts) + "。"


def _gex_big_prints(con, latest):
    """GEX 熱圖大單(近 30 分鐘 0DTE 大量成交),讓導師拿來跟 Bookmap orderflow 交叉比對。"""
    lines = []
    for src in ("NQ", "QQQ", "NDX"):
        try:
            bf = _big_flow(con, latest, src)
        except Exception:
            continue
        if not bf or bf.get("n", 0) == 0:
            continue
        def _oi_tag(x):
            doi = x.get("doi", 0.0)
            if doi > 5:
                return f"增倉+{int(round(doi))}"
            if doi < -5:
                return f"減倉{int(round(abs(doi)))}"
            return "持平"
        top = "、".join(f"{x['strike']}{'C' if x['cp'] == 'C' else 'P'}(量{x['dvol']},{_oi_tag(x)})" for x in bf["top"])
        both = "上下同開" if bf.get("both") else "單邊"
        co, po = int(bf.get("call_oi", 0)), int(bf.get("put_oi", 0))
        oi = f";0DTE未平倉增減:Call {co:+d}/Put {po:+d}" if (co or po) else ""
        lines.append(f"{src}:Call {bf['call']}/Put {bf['put']}({both});大單價位 {top}{oi}")
    if not lines:
        return None
    return "GEX 熱圖大單(近30分鐘0DTE):" + ";".join(lines) + "。"


def _parse_mentor(answer):
    """從導師的文字回答抓:方向、信心、以及「站上/跌破」的方向箭頭價位。
    直接讀他已經寫出來的文字(正則),不強迫 AI 另吐 JSON。抓不到就回空。"""
    import re
    out = dict(direction=None, confidence=None, arrows=[])
    if not answer:
        return out
    # 方向:「方向:」後面可能還夾字(如「方向:結構上偏空」),所以容許中間穿插少許文字
    m = re.search(r"方向[:：]?.{0,12}?(偏多|偏空)", answer)
    if m:
        out["direction"] = m.group(1)
    m = re.search(r"信心[:：]?\s*(高|中|低)", answer)
    if m:
        out["confidence"] = m.group(1)
    seen = set()

    def add(p, d):
        try:
            p = round(float(p), 1)
        except (TypeError, ValueError):
            return
        k = (p, d)
        if k in seen:
            return
        seen.add(k)
        out["arrows"].append(dict(p=p, dir=d))

    # 方向性動詞;數字可能在動詞「前」或「後」,中間可夾少許填充字(如「破了看 29780」「29900 都站上」)
    UP_V = r"(?:站上|站穩|突破|翻多)"
    DN_V = r"(?:跌破|失守|破了)"
    num = r"(\d{4,}(?:\.\d+)?)"
    for mm in re.finditer(UP_V + r"[^\d。\n]{0,6}?" + num, answer):
        add(mm.group(1), "up")
    for mm in re.finditer(DN_V + r"[^\d。\n]{0,6}?" + num, answer):
        add(mm.group(1), "dn")
    for mm in re.finditer(num + r"[^\d。\n]{0,6}?" + UP_V, answer):
        add(mm.group(1), "up")
    for mm in re.finditer(num + r"[^\d。\n]{0,6}?" + DN_V, answer):
        add(mm.group(1), "dn")
    out["arrows"] = out["arrows"][:8]
    return out


def _book_walls(cfg, max_walls=3, cluster_pts=2.0):
    """從 orderflow.json 的掛單簿,把相鄰價位聚成「牆」,回傳最厚的前幾堵買牆/賣牆。
    回傳 [{lo, hi, side, size}]。無資料回 []。"""
    of = _load_orderflow(cfg)
    if of is None or not of["available"]:
        return []
    d = of["data"]

    def cluster(levels):
        walls = []
        for it in levels:
            try:
                p, s = float(it[0]), float(it[1])
            except (TypeError, ValueError, IndexError):
                continue
            if s <= 0:
                continue
            if walls and abs(p - walls[-1]["hi"]) <= cluster_pts:
                w = walls[-1]
                w["lo"] = min(w["lo"], p)
                w["hi"] = max(w["hi"], p)
                w["size"] += s
            else:
                walls.append(dict(lo=p, hi=p, size=s))
        walls.sort(key=lambda w: -w["size"])
        return walls[:max_walls]

    out = []
    for w in cluster(d.get("book_bid") or []):
        w["side"] = "buy"
        out.append(w)
    for w in cluster(d.get("book_ask") or []):
        w["side"] = "sell"
        out.append(w)
    return out


def _mentor_overlay(cfg, nq, answer):
    """組一個「圖層」給前端畫在熱圖上:方向/信心、關鍵價位、掛單牆、方向箭頭。"""
    pm = _parse_mentor(answer)
    levels = []

    def add(p, kind, label):
        if p is None:
            return
        levels.append(dict(p=round(float(p), 1), kind=kind, label=label))

    add(nq.get("flip0"), "flip", "0DTE flip")
    cw = nq.get("call_wall0")
    pw = nq.get("put_wall0")
    if cw is not None and pw is not None and abs(cw - pw) < 1e-6:
        add(cw, "wall", "call/put wall")
    else:
        add(cw, "call", "call wall")
        add(pw, "put", "put wall")
    b0 = nq.get("band0") or {}
    add(b0.get("up"), "band0", "0DTE 日內高")
    add(b0.get("dn"), "band0", "0DTE 日內低")
    add(nq.get("band_up"), "band", "波動帶上緣")
    add(nq.get("band_dn"), "band", "波動帶下緣")
    walls = _book_walls(cfg)
    # 兜底:若抓到方向但 flip 附近沒有箭頭,就在 flip 補一支「站上翻多/跌破翻空」的方向箭頭,
    # 確保導師講的方向永遠有圖可看(正則抓不到的修辭還是能模擬出來)。
    flip = nq.get("flip0")
    if flip is not None and pm["direction"] and not any(abs(a["p"] - flip) < 0.6 for a in pm["arrows"]):
        pm["arrows"].append(dict(p=round(float(flip), 1), dir=("up" if pm["direction"] == "偏多" else "dn")))
    return dict(direction=pm["direction"], confidence=pm["confidence"],
                levels=levels, walls=walls, arrows=pm["arrows"])


def _ai_narrative(cfg, direction, score, confidence, strength, caveat, per, weights, short, spot, nq, flow):
    """組一段繁體中文 prompt 丟給 DeepSeek 寫總結;無 key 或失敗回傳 None。"""
    lines = [f"現價 NQ {_fnum(spot)}。"]
    lines.append(f"GEX 三市場加權結論:「{direction}」,分數 {score:+d},信心度 {confidence}%,強度 {strength}。")
    for s in ("NQ", "QQQ", "NDX"):
        lines.append(f"{s}:{per[s]['direction']}({per[s]['score']:+d})")
    top = sorted(per["NQ"]["signals"], key=lambda x: -abs(x["s"] * x["weight"]))[:6]
    lines.append("關鍵 GEX 訊號:" + ";".join(f"{x['name']}={x['value']}({x['text']})" for x in top))
    if flow and flow.get("available"):
        lines.append("真實 orderflow 訊號:" + ";".join(
            f"{x['name']}={x['value']}({x['text']})" for x in flow["signals"]))
    else:
        lines.append("真實 orderflow:目前無資料或過時,請以 GEX 結構推測為主,並誠實標注缺少訂單流。")
    if nq.get("flip0") is not None:
        lines.append(f"0DTE flip {_fnum(nq['flip0'])},日內區間 {_fnum((nq.get('band0') or {}).get('dn'))}–{_fnum((nq.get('band0') or {}).get('up'))}。")
    lines.append(f"5分鐘短線:「{short['direction']}」({short['score']:+d}),屬雜訊等級。")
    if caveat:
        lines.append(f"補充提醒:{caveat}。")
    lines.append("請照規定格式簡短回答(先講重點方向,再信心/關鍵價位/風險/反方觀點,每項一到兩句)。")
    return DC.summarize("\n".join(lines), cfg)


def _speak(text: str, voice: str):
    """用 edge-tts(微軟神經語音)把文字合成 mp3,回傳相對 URL(如 /tts/xxx.mp3);任何失敗回 None。"""
    try:
        import edge_tts
    except Exception:
        return None
    t = (text or "").replace("**", "").replace("#", "").strip()
    if not t:
        return None
    h = hashlib.md5(t.encode("utf-8")).hexdigest()[:16]
    rel = TTS_DIRNAME + "/" + h + ".mp3"
    path = os.path.join(WEB_DIR, rel)
    if os.path.isfile(path):
        return "/" + rel
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        asyncio.run(edge_tts.Communicate(t, voice=voice).save(path))
    except Exception:
        return None
    return "/" + rel if os.path.isfile(path) else None


def _trader_view(nq, spot, flow=None):
    """NQ 期貨交易者視角:用 0DTE 結構反推 Bookmap 上應該看到的現象 + 當下觀察重點/情境應對。
    bookmap/watch 是結構推測(從 gamma/成交量/IV 推的);real 若有值,則是 Windows 端 Bookmap 送來的真實 orderflow 讀數。"""
    share0 = nq.get("share0")
    flip0 = nq.get("flip0")
    net_zero = nq.get("net_zero")
    call_wall0 = nq.get("call_wall0")
    put_wall0 = nq.get("put_wall0")
    band0 = nq.get("band0") or {}
    flow0 = nq.get("flow0") or []
    pcr_vol0 = nq.get("pcr_vol0")
    big0 = nq.get("big0") or {}

    regime = ("0DTE 主導" if share0 is not None and share0 >= 60
              else "0DTE 中等" if share0 is not None and share0 >= 35
              else "0DTE 較淡" if share0 is not None else "0DTE 資料不足")
    pos_gamma = flip0 is not None and net_zero is not None and spot > flip0
    gamma_sign = "正 gamma" if pos_gamma else "負 gamma"

    bookmap = []
    if call_wall0:
        bookmap.append(f"上方約 {_fnum(call_wall0)} 有 0DTE Call gamma 牆,那附近容易出現賣壓與承接,是短線壓力、也是流動性堆積區")
    if put_wall0:
        bookmap.append(f"下方約 {_fnum(put_wall0)} 有 0DTE Put gamma 牆,那附近容易出現買盤與吸收,是短線支撐、也是流動性堆積區")
    if pos_gamma:
        bookmap.append(f"做市商是正 gamma(現價在 0DTE flip 之上),會逆勢「買低賣高」:Bookmap 上你會看到接近牆的價位掛單變厚、波動被壓、價差相對穩定")
    else:
        bookmap.append(f"做市商是負 gamma(現價在 0DTE flip 之下),會順勢「追漲殺跌」:Bookmap 上你會看到接近 flip 時掛單變薄、掃單變快、價差容易變寬")
    if share0 is not None and share0 >= 60:
        bookmap.append(f"今天 0DTE 佔比高達 {share0}%,盤面由 0DTE 主導:越接近收盤,對沖與展期越密集,價格容易被吸向最大 OI/成交量的履約價")
    if flow0:
        top = flow0[0]
        bookmap.append(f"0DTE 目前最熱的履約價在 {top['strike']}(Call 增量 {top['call']} / Put 增量 {top['put']}),這是當下多空交戰、最容易被「釘」住的磁吸區")
    if big0.get("n"):
        top0 = big0["top"][0] if big0.get("top") else None
        loc = f" @{_fnum(top0['strike'])}" if top0 else ""
        if big0.get("both"):
            bookmap.append(f"0DTE 大單上下同開(Call {big0['call']} / Put {big0['put']} 口{loc}):Bookmap 上會看到 Call 與 Put 同時堆量,像跨式/勒式或避險/吸收,價差容易變寬、掃單變快")
        else:
            side = "Call" if big0["skew"] > 0 else "Put"
            bookmap.append(f"0DTE 大單集中在 {side}(Call {big0['call']} / Put {big0['put']} 口{loc}):Bookmap 上那附近掛單最厚,是短線流動性最足、也最容易被掃的位置")

    watch = []
    if flip0 is not None:
        rel = "站上" if spot > flip0 else ("跌破" if spot < flip0 else "觸及")
        watch.append(f"盯住 0DTE flip {_fnum(flip0)}:現在{rel}它,一旦穿越,做市商對沖會從逆勢轉成順勢,波動放大、動能延續,接著往下一檔 gamma 牆看")
    if band0.get("up") and band0.get("dn"):
        watch.append(f"0DTE 日內預期區間 {_fnum(band0['dn'])}–{_fnum(band0['up'])}(±{_fnum(band0['em'])} 點,剩約 {band0.get('hours')} 小時收盤):到區間邊緣時 0DTE 賣壓/買盤會放大,容易見壓力或支撐")
    if pcr_vol0 is not None:
        mood = ">1,避險買盤偏重、短線偏防禦" if pcr_vol0 > 1 else "<1,追價買盤偏重、短線偏積極"
        watch.append(f"0DTE P/C = {pcr_vol0}({mood}),可對照盤中是否出現反常的單邊堆量")
    if big0.get("both"):
        watch.append(f"大單上下同開(跨式/勒式或避險):方向先不定,等 0DTE flip {_fnum(flip0) if flip0 is not None else ''} 突破才表態;在突破前波動會放大,追單要防兩邊掃損")

    narrative = "。".join(bookmap) + ("。" if bookmap else "")
    if watch:
        narrative += "當下觀察重點:" + "".join(watch)

    # 真實 orderflow(若 Windows 端有送資料進來)
    real = []
    flow_available = bool(flow and flow.get("available"))
    if flow_available and flow.get("signals"):
        for x in flow["signals"]:
            real.append(f"{x['name']} {x['value']} —— {x['text']}")
    elif flow and flow.get("stale"):
        real.append("⚠ orderflow 斷線或過時(Bookmap 延遲偏高),今天先只看 GEX 結構推測")
    return dict(regime=regime, gamma_sign=gamma_sign, pos_gamma=pos_gamma, share0=share0,
                flip0=flip0, net_zero=net_zero, band=band0, flow0=flow0, pcr_vol0=pcr_vol0,
                bookmap=bookmap, watch=watch, real=real, flow_available=flow_available,
                flow_lag_sec=(flow or {}).get("lag_sec"), narrative=narrative)


def _short_lean(nq, spot):
    comps = []
    cd, pd = nq["call_dv"], nq["put_dv"]
    if cd + pd > 0:
        comps.append(_clamp((cd - pd) / float(cd + pd)) * 0.4)
    if nq["ret1"] is not None:
        comps.append(_clamp(nq["ret1"] / 0.003) * 0.35)
    if nq["pcr_vol0"] is not None:
        comps.append(_clamp((1.0 - nq["pcr_vol0"]) / 0.4) * 0.15)
    bu, bd = nq["band_up"], nq["band_dn"]
    if bu and bd and bu > bd:
        mid = (bu + bd) / 2.0
        half = (bu - bd) / 2.0 or 1.0
        comps.append(_clamp(-(spot - mid) / half) * 0.10)
    raw = sum(comps) / len(comps) if comps else 0.0
    return dict(direction=("偏多" if raw >= 0 else "偏空"), score=int(round(raw * 100)),
                note="極低信心,五分鐘屬雜訊等級,僅供參考、勿當下單依據")


def _narrative(direction, score, confidence, strength, caveat, per, weights, short, spot, nq):
    """短時間總結:以 0DTE 的變化為主。參與者結構/策略足跡/做市商對沖的細節放到下方各區塊,不塞進總結。"""
    L = []
    share0 = nq.get("share0")
    flip0 = nq.get("flip0")
    net_zero = nq.get("net_zero")
    band0 = nq.get("band0") or {}
    flow0 = nq.get("flow0") or []
    call_wall0 = nq.get("call_wall0")
    put_wall0 = nq.get("put_wall0")
    if share0 is not None:
        L.append(f"今天 0DTE {'主導' if share0 >= 60 else '中等' if share0 >= 35 else '較淡'}(佔比 {share0}%)。")
    if flip0 is not None:
        rel = "之上" if spot > flip0 else "之下"
        L.append(f"現價 {_fnum(spot)} 在 0DTE flip {_fnum(flip0)} {rel}(0DTE 淨GEX {_fnum(net_zero)}M),呈「{'正' if spot > flip0 else '負'} gamma」。")
    big0 = nq.get("big0") or {}
    if big0.get("n"):
        top0 = big0["top"][0] if big0.get("top") else None
        loc = f" @{_fnum(top0['strike'])}" if top0 else ""
        if big0.get("both"):
            L.append(f"0DTE 大單上下同開(Call {big0['call']} / Put {big0['put']} 口{loc}),像跨式/勒式或避險,方向不明、但波動會放大。")
        else:
            side = "Call" if big0["skew"] > 0 else "Put"
            L.append(f"0DTE 大單集中在 {side}(Call {big0['call']} / Put {big0['put']} 口{loc}),短線偏{'多' if big0['skew'] > 0 else '空'}。")
    elif flow0:
        top = flow0[0]
        L.append(f"0DTE 成交量集中在 {top['strike']}(Call 增量 {top['call']} / Put 增量 {top['put']})。")
    parts = "、".join(f"{s} {per[s]['direction']}({per[s]['score']:+d})" for s in ("NQ", "QQQ", "NDX"))
    L.append(f"把 NQ、QQQ、NDX 加權後({parts}),總結論傾向「{direction}」,分數 {score:+d}、信心度 {confidence}%。")
    if caveat:
        L.append(caveat + "。")
    L.append(f"未來五分鐘短線傾向「{short['direction']}」({short['score']:+d}),但{short['note']}。")
    s = f"關鍵價位:0DTE flip {_fnum(flip0)}"
    if band0.get("up") and band0.get("dn"):
        s += f",0DTE 日內區間 {_fnum(band0['dn'])}–{_fnum(band0['up'])}"
    if call_wall0 and put_wall0 and call_wall0 > spot > put_wall0:
        s += f",上方 0DTE call 牆 {_fnum(call_wall0)}、下方 0DTE put 牆 {_fnum(put_wall0)}"
    s += "。"
    L.append(s)
    return "".join(L)


_THESIS_KEYS = ("ts", "t", "spot", "direction", "score", "confidence", "strength", "short_dir", "short_score",
                "flip", "call_wall", "put_wall", "band_up", "band_dn", "created")


def _thesis_read(t):
    """回傳 (上一次論點 dict 或 None, 滾動戰績 dict)。用「上一段預測當前因,驗證結果」串前因後果。"""
    with _cache_lock:
        c = _cache_con()
        try:
            c.execute("""CREATE TABLE IF NOT EXISTS thesis (
                ts TEXT PRIMARY KEY, t REAL, spot REAL, direction TEXT, score INTEGER,
                confidence INTEGER, strength TEXT, short_dir TEXT, short_score INTEGER,
                flip REAL, call_wall REAL, put_wall REAL, band_up REAL, band_dn REAL,
                created REAL)""")
            rows = c.execute("SELECT ts, t, spot, direction, score, confidence, strength, short_dir, short_score, "
                             "flip, call_wall, put_wall, band_up, band_dn, created FROM thesis ORDER BY t ASC").fetchall()
        finally:
            c.close()
    prev = None
    for r in rows:
        if r[1] is not None and r[1] < t:
            prev = dict(zip(_THESIS_KEYS, r))
    hit = miss = und = 0
    for i in range(1, len(rows)):
        t0, s0, d0 = rows[i - 1][1], rows[i - 1][2], rows[i - 1][3]
        t1, s1 = rows[i][1], rows[i][2]
        if t1 is None or t0 is None or (t1 - t0) > 1800 or not s0 or not s1:
            continue
        move = s1 - s0
        if abs(move) <= abs(s0) * 0.0005:
            und += 1
        elif (d0 == "偏多" and move > 0) or (d0 == "偏空" and move < 0):
            hit += 1
        else:
            miss += 1
    return prev, dict(n=hit + miss + und, hit=hit, miss=miss, undecided=und)


def _thesis_save(ts, t, spot, direction, score, confidence, strength, short, flip, call_wall, put_wall, band_up, band_dn):
    with _cache_lock:
        c = _cache_con()
        try:
            c.execute("INSERT OR REPLACE INTO thesis (ts, t, spot, direction, score, confidence, strength, short_dir, short_score, flip, call_wall, put_wall, band_up, band_dn, created) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (ts, t, spot, direction, score, confidence, strength, short["direction"], short["score"],
                       flip, call_wall, put_wall, band_up, band_dn, time.time()))
            c.commit()
        finally:
            c.close()


def api_thesis(con, q):
    """「導師」:NQ/QQQ/NDX 各算方向分數再加權成總結論,並讀出參與者結構(價內/價外/深價外)、策略足跡(推測)、
    做市商對沖立場、短線(5分鐘)傾向,最後用書寫方式給出一段分析。"""
    row = con.execute("SELECT ts, spot FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    if not row:
        return dict(ok=False, error="還沒有快照")
    latest, spot = row[0], float(row[1])
    t = epoch_of(latest)
    lo = t - 6 * 3600
    snaps = snapshot_list(con, lo, t)
    prev, track = _thesis_read(t)
    cfg = load_config()
    flow = _orderflow_signals(cfg)

    weights = {"NQ": 0.45, "QQQ": 0.30, "NDX": 0.25}
    per = {}
    for s in ("NQ", "QQQ", "NDX"):
        raw, signals, ctx = _thesis_src(con, s, lo, t, latest, spot, snaps)
        per[s] = dict(raw=raw, score=int(round(raw * 100)), direction=("偏多" if raw >= 0 else "偏空"), signals=signals, ctx=ctx)

    tot_w = sum(weights[s] for s in per)
    gex = sum(per[s]["raw"] * weights[s] for s in per) / tot_w if tot_w else 0.0
    flow_w = FLOW_WEIGHT if flow.get("available") else 0.0
    gex_w = 1.0 - flow_w
    grand = (gex * gex_w + flow["raw"] * flow_w) if flow_w else gex
    score = int(round(grand * 100))
    direction = "偏多" if grand >= 0 else "偏空"
    confidence = min(90, int(round(50 + abs(grand) * 40)))
    strength = "強" if abs(grand) >= 0.5 else "中" if abs(grand) >= 0.25 else "弱"
    caveat = "三市場方向分歧、證據不足,方向僅供參考" if strength == "弱" else ""
    if flow.get("stale"):
        caveat = (caveat + ";" if caveat else "") + "orderflow 斷線或過時(Bookmap 延遲),本結論僅用 GEX 結構推測"
    elif not flow.get("connected"):
        caveat = (caveat + ";" if caveat else "") + "orderflow 尚未連線(Windows 那台還沒送資料),本結論僅用 GEX 結構推測"

    nq = per["NQ"]["ctx"]
    flip = nq["flip"]
    call_wall = nq["call_wall"]
    put_wall = nq["put_wall"]
    band_up = nq["band_up"]
    band_dn = nq["band_dn"]
    atm_iv = nq["atm_iv"]
    dte = nq["dte"]
    if band_up and band_up > spot:
        sigma = band_up - spot
    elif atm_iv:
        sigma = spot * atm_iv / 100.0 * math.sqrt(1.0 / 365.0)
    else:
        sigma = spot * 0.008

    participants = _structure(con, latest, spot, sigma)
    dealer = _dealer_stance(spot, nq["flip0"], nq["net_zero"])
    short = _short_lean(nq, spot)
    trader = _trader_view(nq, spot, flow)

    # 前因後果:把「上一段預測」當前因,用新快照驗證方向/點數/flip 穿越/5分鐘短線,並帶滾動戰績。
    verify = dict(has_prev=False, n=track["n"], hit=track["hit"], miss=track["miss"], undecided=track["undecided"])

    def _dir_hit(d, move, base):
        if not base:
            return "尚未分曉"
        if abs(move) <= abs(base) * 0.0005:
            return "尚未分曉"
        if (d == "偏多" and move > 0) or (d == "偏空" and move < 0):
            return "命中"
        return "未命中"

    if prev:
        move = spot - prev["spot"]
        elapsed_min = int(round((t - prev["t"]) / 60.0)) if prev["t"] else None
        dir_hit = _dir_hit(prev["direction"], move, prev["spot"])
        short_hit = _dir_hit(prev.get("short_dir") or "", move, prev["spot"])
        flip_cross = None
        if prev.get("flip") and prev["spot"]:
            if (prev["spot"] < prev["flip"] and spot >= prev["flip"]) or (prev["spot"] > prev["flip"] and spot <= prev["flip"]):
                flip_cross = f"已穿越 flip {_fnum(prev['flip'])}"
        verify.update(has_prev=True,
                      prev=dict(direction=prev["direction"], score=prev["score"], spot=round(prev["spot"], 1),
                                ts=prev["ts"], elapsed_min=elapsed_min),
                      direction_hit=dir_hit, short_hit=short_hit, move=round(move, 1),
                      flip_cross=flip_cross,
                      detail=(f"回顧上一段:約 {elapsed_min} 分鐘前結論是「{prev['direction']}」({prev['score']:+d}),"
                              f"當時現價約 {_fnum(prev['spot'])},現在來到 {_fnum(spot)}({move:+.1f} 點),方向判斷「{dir_hit}」。"))

    ai = _ai_narrative(cfg, direction, score, confidence, strength, caveat, per, weights, short, spot, nq, flow) if cfg.get("auto_ai_summary") else None
    if ai:
        narrative = ai
    else:
        narrative = _narrative(direction, score, confidence, strength, caveat, per, weights, short, spot, nq)
        narrative = _orderflow_sentence(flow) + narrative
    if verify["has_prev"]:
        narrative = verify["detail"] + "接著看現在的盤面。" + narrative

    def rnd(x, n=1):
        return None if x is None else round(float(x), n)

    _thesis_save(latest, t, spot, direction, score, confidence, strength, short, flip, call_wall, put_wall, band_up, band_dn)

    if flow.get("available"):
        disc_of = (f"訂單流(Delta/不平衡/大單/掃單)來自 Windows 端 Bookmap 真實 MBO 匯總,資料約 {int(flow['age_sec'])} 秒前"
                   + (",但 Bookmap 延遲偏高、可能落後" if flow.get("stale") else ""))
    elif flow.get("stale"):
        disc_of = "訂單流目前斷線或過時,本結論不含可信訂單流"
    else:
        disc_of = "訂單流尚未連線,本結論不含真實訂單流"
    return dict(ok=True, t=t, spot=round(spot, 1), direction=direction, score=score,
                confidence=confidence, strength=strength, caveat=caveat,
                sources=dict((s, dict(direction=per[s]["direction"], score=per[s]["score"])) for s in per),
                weights=dict((s, round(weights[s] * 100)) for s in weights),
                narrative=narrative, participants=participants, dealer=dealer, short=short, verify=verify, trader=trader,
                signals=sorted(per["NQ"]["signals"], key=lambda x: -abs(x["s"] * x["weight"])),
                orderflow=dict(available=flow["available"], stale=flow["stale"], connected=flow["connected"],
                               age_sec=flow["age_sec"], lag_sec=flow["lag_sec"], weight=int(round(flow_w * 100)),
                               raw=flow["raw"], spot=flow["spot"], signals=flow["signals"],
                               sentence=_orderflow_sentence(flow)),
                ai_narrative=bool(ai),
                key_levels=dict(flip=rnd(flip), call=rnd(call_wall), put=rnd(put_wall), band_up=rnd(band_up), band_dn=rnd(band_dn), atm_iv=rnd(atm_iv), dte=dte,
                                flip0=rnd(nq.get("flip0")), band0_up=rnd((nq.get("band0") or {}).get("up")), band0_dn=rnd((nq.get("band0") or {}).get("dn")), band0_em=rnd((nq.get("band0") or {}).get("em")), share0=rnd(nq.get("share0"))),
                disclaimer=("這是當下數據的機率傾向,不是保證。GEX 結構、參與者結構與「Bookmap 上應該會看到」皆為結構推測(從 gamma/成交量/IV 推的),不是真實 DOM;"
                            + disc_of + "。請自行控管風險。"))

def api_ask(con, q):
    """「問導師」:把使用者問題 + 最新盤勢(GEX 三市場 / 0DTE / orderflow / 做市商 / 短線)組成一包,丟給 DeepSeek 回答。"""
    question = (q.get("q") or "").strip()
    if not question:
        return dict(ok=False, error="問題是空的")
    row = con.execute("SELECT ts, spot FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    if not row:
        return dict(ok=False, error="還沒有快照")
    latest, spot = row[0], float(row[1])
    t = epoch_of(latest)
    lo = t - 6 * 3600
    snaps = snapshot_list(con, lo, t)
    cfg = load_config()
    flow = _orderflow_signals(cfg)

    per = {}
    for s in ("NQ", "QQQ", "NDX"):
        raw, signals, ctx = _thesis_src(con, s, lo, t, latest, spot, snaps)
        per[s] = dict(raw=raw, score=int(round(raw * 100)), direction=("偏多" if raw >= 0 else "偏空"), signals=signals, ctx=ctx)

    nq = per["NQ"]["ctx"]
    b0 = nq.get("band0") or {}
    ny = dt.datetime.fromtimestamp(t, NY).strftime("%H:%M")

    L = [f"現在:台北 {dt.datetime.fromtimestamp(t).strftime('%H:%M')}(紐約 {ny})。現價 NQ {_fnum(spot)}。"]
    L.append("GEX 三市場方向與分數:" + " ".join(f"{s}={per[s]['direction']}({per[s]['score']:+d})" for s in ("NQ", "QQQ", "NDX")))
    _w = {"NQ": 0.45, "QQQ": 0.30, "NDX": 0.25}
    _tw = sum(_w[s] for s in ("NQ", "QQQ", "NDX"))
    _gex = sum(per[s]["raw"] * _w[s] for s in ("NQ", "QQQ", "NDX")) / _tw if _tw else 0.0
    L.append(f"GEX 加權合計:「{'偏多' if _gex >= 0 else '偏空'}」分數 {int(round(_gex * 100)):+d}(權重 NQ45%/QQQ30%/NDX25%)")
    top = sorted(per["NQ"]["signals"], key=lambda x: -abs(x["s"] * x["weight"]))[:8]
    if top:
        L.append("NQ 關鍵 GEX 訊號:" + ";".join(f"{x['name']}={x['value']}" for x in top))
    L.append("0DTE:淨GEX " + _fnum(nq["net_zero"]) + "M · flip " + _fnum(nq["flip0"]) +
             " · 日內區間 " + _fnum(b0.get("dn")) + "–" + _fnum(b0.get("up")) +
             " · 佔比 " + _fnum(nq.get("share0")) + "% · call wall " + _fnum(nq["call_wall0"]) +
             " / put wall " + _fnum(nq["put_wall0"]))
    L.append("關鍵價位:flip " + _fnum(nq["flip"]) + " · call wall " + _fnum(nq["call_wall"]) +
             " / put wall " + _fnum(nq["put_wall"]) +
             " · 預期波動帶 " + _fnum(nq["band_dn"]) + "–" + _fnum(nq["band_up"]))
    L.append("orderflow:" + _orderflow_sentence(flow))
    _oh = _orderflow_history(cfg)
    if _oh:
        L.append(_oh)
    _ob = _orderflow_book(cfg)
    if _ob:
        L.append(_ob)
    _gb = _gex_big_prints(con, latest)
    if _gb:
        L.append(_gb)
    L.append("做市商:" + _dealer_stance(spot, nq["flip0"], nq["net_zero"]))
    L.append("5分鐘短線:「" + _short_lean(nq, spot)["direction"] + "」,極低信心雜訊。")

    prompt = ("【當下盤勢】\n" + "\n".join(L) + "\n\n【我的問題】\n" + question +
              "\n\n請照規定格式回答,並在最後補一段「主力意圖」:綜合選擇權大單佈單(含未平倉增減,判斷開新倉還是平倉)、"
              "NQ 期貨 orderflow(累積 Delta、大單、掃單、掛單牆)與 GEX 結構,判斷大戶/主力現在大概在做什麼、"
              "佈局偏多還是偏空,再說明這對後續方向代表什麼。")
    answer = DC.summarize(prompt, cfg)
    if not answer:
        return dict(ok=False, error="AI 沒回應(可能 config.json 還沒填 deepseek_api_key,或網路問題)")
    overlay = _mentor_overlay(cfg, nq, answer)
    return dict(ok=True, t=t, spot=round(spot, 1), question=question, answer=answer, overlay=overlay)


def api_speak(con, q):
    """把一段文字合成語音(edge-tts),回傳 mp3 網址。與 api_ask 分開,避免拖慢回答。"""
    text = (q.get("text") or "").strip()
    if not text:
        return dict(ok=False, error="沒有要唸的文字")
    cfg = load_config()
    voice = cfg.get("tts_voice") or TTS_VOICE_DEFAULT
    audio = _speak(text, voice)
    if not audio:
        return dict(ok=False, error="語音合成失敗(可能沒裝 edge-tts)")
    return dict(ok=True, audio=audio)


def api_errors(con, q):
    """最近幾筆伺服器錯誤(記憶體佇列),讓前端「錯誤紀錄」面板直接顯示,不用 SSH 看 log。"""
    return dict(errors=list(_ERRORS), now=int(time.time()))


ROUTES = dict(squeeze=api_squeeze, flowmom=api_flowmom, heatx=api_heat_x, book=api_book, events=api_events, history=api_history, days=api_days, heat=api_heat, net=api_net, levels=api_levels, status=api_status, bars=api_bars, dte=api_dte, net3=api_net3, band=api_band, band0=api_band0, pcr=api_pcr, skew=api_skew, flow=api_flow, thesis=api_thesis, ask=api_ask, speak=api_speak, errors=api_errors)


# ----------------------------- IBKR 1-minute candles (optional, own process) -----------------------------
# The candles are fetched by a separate PROCESS so that the GEX calculations of this server (numpy / pandas bursts) can never
# delay a candle update, and neither can the collector: all three only share the SQLite file.
def _meta_set(db_path: str, k: str, v: str):
    try:
        c = connect(db_path)
        c.execute("CREATE TABLE IF NOT EXISTS bars_meta (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT OR REPLACE INTO bars_meta VALUES (?,?)", (k, str(v)))
        c.commit()
        c.close()
    except Exception:
        pass


def bars_process(db_path: str):
    try:
        import ib_async  # noqa: F401
    except Exception as e:  # ib_async not installed
        _meta_set(db_path, "state", "off")
        _meta_set(db_path, "err", f"ib_async not available: {e}")
        return
    con = connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS bars (t INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL)")
    con.commit()
    con.close()
    while True:
        try:
            asyncio.run(_bars_session(db_path))
        except Exception as e:
            _meta_set(db_path, "state", "reconnecting")
            _meta_set(db_path, "err", str(e)[:200])
        time.sleep(15)


async def _bars_session(db_path: str):
    from ib_async import IB, Future
    ib = IB()
    for p in PORTS_TO_TRY:
        try:
            await ib.connectAsync("127.0.0.1", p, clientId=BARS_CLIENT_ID, timeout=5, readonly=True)
            break
        except Exception:
            continue
    else:
        raise RuntimeError("IB Gateway / TWS not reachable on 4001, 4002, 7497, 7496")
    con = None
    try:
        today = dt.datetime.now(NY).strftime("%Y%m%d")
        details = await ib.reqContractDetailsAsync(Future("NQ", exchange="CME"))
        c = sorted((d.contract for d in details if d.contract.lastTradeDateOrContractMonth[:8] >= today),
                   key=lambda c: c.lastTradeDateOrContractMonth)[0]   # same rule as the collector
        bars = await ib.reqHistoricalDataAsync(c, endDateTime="", durationStr=BARS_HISTORY, barSizeSetting="1 min",
                                               whatToShow="TRADES", useRTH=False, formatDate=2, keepUpToDate=True)
        con = connect(db_path)

        def save(rows):
            con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?)", rows)
            con.commit()

        def row(b):
            d = b.date
            t = int(d.timestamp()) if hasattr(d, "timestamp") else int(d)
            return (t, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))

        save([row(b) for b in bars])
        _meta_set(db_path, "state", "ibkr")
        _meta_set(db_path, "err", "")
        _meta_set(db_path, "contract", c.localSymbol)

        def on_update(bs, has_new):
            save([row(b) for b in bs[-2:]])

        bars.updateEvent += on_update
        while ib.isConnected():
            await asyncio.sleep(2)
    finally:
        if con is not None:
            con.close()
        if ib.isConnected():
            ib.disconnect()


# ----------------------------- HTTP -----------------------------
def _log_error(path, e):
    """Print the full error, keep a copy in gex_web.log, and remember it in memory so the page's
    「錯誤紀錄」panel can show it without anyone having to SSH into the server."""
    import traceback
    tb = traceback.format_exc()
    now = dt.datetime.now()
    msg = f"[{now.strftime('%m-%d %H:%M:%S')}] {path}\n{tb}"
    print(msg, flush=True)
    _ERRORS.append(dict(t=int(now.timestamp()), path=path, error=f"{type(e).__name__}: {e}", trace=tb))
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gex_web.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "GexWeb/1.0"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        """Basic Auth 檢查:config.json 有設定 auth_user / auth_pass 時才擋,沒設定就放行(熱讀取,免重開)。"""
        cfg = load_config()
        user = (cfg.get("auth_user") or "").strip()
        pw = (cfg.get("auth_pass") or "")
        if not user or not pw:
            return True
        ah = self.headers.get("Authorization", "")
        if not ah.startswith("Basic "):
            return False
        try:
            got = base64.b64decode(ah[6:]).decode("utf-8")
        except Exception:
            return False
        return got == f"{user}:{pw}"

    def do_GET(self):
        if not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="GEX"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            name = u.path[5:]
            fn = ROUTES.get(name)
            if not fn:
                return self._send(404, b'{"error":"unknown"}', "application/json")
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                con = connect(self.server.db_path)
                try:
                    body = json.dumps(clean(fn(con, q)), separators=(",", ":")).encode()
                finally:
                    con.close()
                return self._send(200, body, "application/json")
            except (BrokenPipeError, ConnectionResetError):
                return          # the browser cancelled the request (e.g. you changed a setting while it was loading): harmless
            except Exception as e:  # report to the page instead of dropping the connection
                _log_error(self.path, e)
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")
        rel = "index.html" if u.path in ("", "/") else u.path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR + os.sep) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--no-bars", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    if not os.path.isfile(a.db):
        raise SystemExit(f"找不到資料庫 {a.db}。請先執行收集器(start_gex.command),或用 --db 指定檔案。")
    global CACHE_PATH
    CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(a.db)), "web_cache.sqlite")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.db_path = a.db
    if not a.no_bars:
        multiprocessing.Process(target=bars_process, args=(a.db,), daemon=True).start()

    def warm():
        try:
            c = connect(a.db)
            row = c.execute("SELECT MAX(ts) FROM snapshots").fetchone()
            if row and row[0]:
                hi = epoch_of(row[0])
                for b in ("oi", "vol"):
                    api_levels(c, dict(**{"from": hi - 8 * 3600, "to": hi}, basis=b, scope="all"))
            c.close()
        except Exception:
            pass
    threading.Thread(target=warm, daemon=True).start()
    url = f"http://{a.host}:{a.port}"
    print(f"GEX 網站 v2(含 GEX 壓力/Charm 熱圖、整體部位、事件通知)已啟動:{url}    (關閉:按 Ctrl+C)")
    print(f"程式位置:{os.path.abspath(__file__)}")
    if not a.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("已停止。")
    except Exception as e:
        _log_error("serve_forever", e)
        print("伺服器因為上面的錯誤停止了。請把這段訊息(或 gex_web.log)貼給 Claude。", flush=True)


if __name__ == "__main__":
    main()
