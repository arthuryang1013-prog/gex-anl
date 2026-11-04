#!/usr/bin/env python3
"""absorb_div_backtest.py — 回測「吸收背離」訊號:亮綠/亮紅之後,±40 點誰先到(唯讀)。

前端「吸收背離」(web/app.js `buildAbsorb`、absSeries 虛線)是「每分鐘」的背離:
  r = 該分鐘實際成交淨量 / 成交量(主動性比率,±1),對上該分 K 的收−開(點)。
  r < 0(賣方主動)且價格沒跌(m > -0.5)→ 看多吸收(綠);
  r > 0(買方主動)且價格沒漲(m < +0.5)→ 看空吸收(紅)。
  這是「吸收 = 反轉」的方向假設,目前前端就是這個方向、還沒翻過。

這支就是來回答「這個方向到底對不對」:掃每分鐘存檔 + 1 分 K,找出每個「吸收背離訊號段」,
每段取第一根當進場,往後看 ±40 點誰先到(和 thesis_outcome 同一套 first_touch)。

用法(VPS 專案資料夾):
  python3 absorb_div_backtest.py                    # 預設 ±40 / 30 分 / 成本 2 點 / 只看美東 09:30–13:30 / 最近 30 天
  python3 absorb_div_backtest.py --all-hours --show all
  python3 absorb_div_backtest.py --flip             # 測「反轉」假設(和累積吸收一樣翻轉方向)
  python3 absorb_div_backtest.py --r-min 0.1        # 把 |r| 門檻從 0.05 拉高(訊號更純、更少)

限制:方向還沒翻過,所以這是測「原始假設」;但資料一樣只有 10-27 起、8 天,樣本少、多數在凌晨,
  結果只是線索,不是結論。
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import math
import sqlite3
from zoneinfo import ZoneInfo

import thesis_outcome as TO
import orderflow_window as OW

NY = ZoneInfo("America/New_York")
TW = ZoneInfo("Asia/Taipei")

R_MIN = 0.05   # |r| 門檻,前端 buildAbsorb 同值
DEAD = 0.5     # 價格死區(點):該分 K 收−開 的 |m| 門檻


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_bars(db):
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT t, o, h, l, c FROM bars ORDER BY t").fetchall()
    finally:
        con.close()
    return rows


def detect(db, lo, hi, r_min=R_MIN, dead=DEAD, flip=False):
    """掃每分鐘存檔 + 1 分 K,回傳 (bars, fires)。

    fires 都是 dict:t、dir(看多/看空)、spot(該分 K 收盤)、strength、r、m。
    """
    bars = load_bars(db)
    if len(bars) < 200:
        raise SystemExit("bars 表資料太少(<200 根),先讓網站 K 棒來源跑夠")
    bt = [b[0] for b in bars]
    bar_oc = {b[0]: (b[1], b[4]) for b in bars}  # t -> (o, c)

    minutes = OW.read_minutes(lo, hi)
    mt = []
    for m in minutes:
        t = m.get("t")
        if t is None:
            continue
        tr = m.get("delta_tr")
        use_tr = tr is not None
        d = _fnum(tr) if use_tr else _fnum(m.get("delta"))
        v = _fnum(m.get("vol_tr")) if use_tr else _fnum(m.get("vol"))
        if d is None or not v:
            continue
        mt.append((t, d, v))
    mt.sort(key=lambda x: x[0])

    fires, aligned = [], 0
    for (t, d, v) in mt:
        i = bisect.bisect_left(bt, t)
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(bt):
                tj = bt[j]
                if abs(tj - t) <= 60 and (best is None or abs(tj - t) < abs(best - t)):
                    best = tj
        if best is None:
            continue
        aligned += 1
        o, c = bar_oc[best]
        if o is None or c is None:
            continue
        r = d / v
        if abs(r) < r_min:
            continue
        mchg = c - o
        if r < 0 and mchg > -dead:
            dr, st = "看多", abs(r)
        elif r > 0 and mchg < dead:
            dr, st = "看空", -abs(r)
        else:
            continue
        if flip:
            dr = "看空" if dr == "看多" else "看多"
            st = -st
        fires.append({"t": t, "dir": dr, "spot": c, "strength": st, "r": r, "m": mchg})
    return bars, fires, aligned, len(mt)


def episodes(fires, gap_sec=120):
    eps = []
    for f in fires:
        if eps and f["dir"] == eps[-1]["dir"] and f["t"] - eps[-1]["end"] <= gap_sec:
            eps[-1]["end"] = f["t"]
            continue
        eps.append({"t": f["t"], "dir": f["dir"], "spot": f["spot"], "strength": f["strength"], "end": f["t"]})
    return eps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="nq_gex.sqlite", help="有 bars 表的資料庫")
    ap.add_argument("--days", type=int, default=30, help="往回掃幾天")
    ap.add_argument("--X", type=float, default=40.0)
    ap.add_argument("--hold", type=int, default=30, help="最多等幾分鐘")
    ap.add_argument("--cost", type=float, default=2.0, help="來回成本(點)")
    ap.add_argument("--r-min", type=float, default=R_MIN, help="|r| 門檻(預設 0.05)")
    ap.add_argument("--dead", type=float, default=DEAD, help="價格死區點數(預設 0.5)")
    ap.add_argument("--flip", action="store_true", help="翻轉方向(測「背離=延續」假設)")
    ap.add_argument("--all-hours", action="store_true")
    ap.add_argument("--show", choices=["sum", "all"], default="sum")
    a = ap.parse_args()

    now = int(dt.datetime.now().timestamp())
    lo, hi = now - a.days * 86400, now
    bars, fires, aligned, n_min = detect(a.db, lo, hi, r_min=a.r_min, dead=a.dead, flip=a.flip)
    bt = [b[0] for b in bars]; bh = [b[2] for b in bars]; bl = [b[3] for b in bars]
    eps = episodes(fires)

    rows = []
    for e in eps:
        t = e["t"]
        loc = dt.datetime.fromtimestamp(t, NY)
        if not a.all_hours and not (dt.time(9, 30) <= loc.time() <= dt.time(13, 30) and loc.weekday() < 5):
            continue
        if e["spot"] is None:
            continue
        i0 = bisect.bisect_right(bt, t)
        r = TO.first_touch(bt, bh, bl, i0, t, e["spot"], a.X, a.hold)
        if r is None:
            continue
        rows.append((e, r))

    be = (a.X + a.cost) / (2 * a.X)
    print("═" * 80)
    print(f"吸收背離回測 · ±{a.X:g} 點 / 最多 {a.hold} 分鐘 / 來回成本 {a.cost:g} 點 → 損益兩平命中率 {be * 100:.1f}%")
    print(f"訊號門檻:|r| ≥ {a.r_min:g} / 價格死區 {a.dead:g} 點(收−開)/ 方向{'翻轉(延續假設)' if a.flip else '原始(吸收=反轉)'}")
    print(f"掃描範圍(台北){dt.datetime.fromtimestamp(lo, TW):%m-%d %H:%M} ~ {dt.datetime.fromtimestamp(hi, TW):%m-%d %H:%M}")
    print(f"1 分 K {len(bars)} 根(紐約 {dt.datetime.fromtimestamp(bt[0], NY):%m-%d} ~ {dt.datetime.fromtimestamp(bt[-1], NY):%m-%d})")
    print(f"每分鐘有資料 {n_min} 分鐘;對上 K 棒 {aligned} 分鐘;亮訊號分鐘 {len(fires)}、併成 {len(eps)} 段;時段過濾+可判定後 {len(rows)} 段")
    if not rows:
        print("  沒有可判定的訊號段(可能:K 棒沒涵蓋那幾天、或時段過濾後為 0)。可加 --all-hours。")
        print("═" * 80)
        return

    if a.show == "all":
        print(f"  {'時間(台北)':<16} {'方向':<6} {'進場':>9} {'r':>7} {'結果':<8}")
        for e, r in rows:
            tt = dt.datetime.fromtimestamp(e["t"], TW).strftime("%m-%d %H:%M:%S")
            res = {"up": "先到+40", "down": "先到-40", "timeout": "逾時", "both": "雙觸"}.get(r, r)
            print(f"  {tt:<16} {e['dir']:<6} {e['spot']:>9,.1f} {e['r']:>+7.3f} {res:<8}")

    hit = dec = n_up = n_dn = n_to = n_bo = 0
    for e, r in rows:
        if r == "up":
            n_up += 1
        elif r == "down":
            n_dn += 1
        elif r == "timeout":
            n_to += 1
        elif r == "both":
            n_bo += 1
        if r not in ("up", "down"):
            continue
        dec += 1
        if (e["dir"] == "看多" and r == "up") or (e["dir"] == "看空" and r == "down"):
            hit += 1

    print("─" * 80)
    print(f"  分出勝負 {dec} 段(逾時 {n_to}、雙觸 {n_bo}):猜對 {hit} 段")
    if dec:
        rate = hit / dec
        ci = 98.0 / math.sqrt(dec)
        print(f"  命中率 {rate * 100:.1f}% ±{ci:.0f}%(95% 誤差) · 損益兩平 {be * 100:.1f}% · 差 {(rate - be) * 100:+.1f}%")
        base = n_up / (n_up + n_dn) if (n_up + n_dn) else 0
        print(f"  基準(這幾段永遠做多):先漲 40 有 {n_up}、先跌 40 有 {n_dn} → {base * 100:.1f}%")
    else:
        print("  沒有分出勝負的段。")

    print("─" * 80)
    print(f"  判讀:樣本 {dec} 段,誤差 ±~{98.0 / math.sqrt(max(dec, 1)):.0f}%,現在什麼都還看不出來。")
    print("  提醒:每段取第一根當進場;相鄰段看的是重疊的 30 分鐘,獨立樣本比 n 少很多。")
    print("═" * 80)


if __name__ == "__main__":
    main()
