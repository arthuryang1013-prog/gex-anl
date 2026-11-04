#!/usr/bin/env python3
"""abs_cum_backtest.py — 回測「累積吸收」訊號:亮綠/亮紅之後,±40 點誰先到(唯讀)。

做法:
  1. 用 scan_abs_cum 同一套門檻(W=30 / FLOW_MIN=500 / FLOW_REF=1200 / DEAD_PTS=10 /
     CONSIST=0.65)掃每分鐘存檔,找出每個「訊號段」(連續同方向亮燈的一段)。
  2. 每段取第一根當進場(你在圖上最早會看到它的那根),進場價用該分鐘現價(spot)。
  3. 往後看 1 分 K,判 ±40 點誰先到(和 thesis_outcome.py 同一套 first_touch):
     綠(看多)先漲 40 = 命中;紅(看空)先跌 40 = 命中;30 分沒到 = 逾時;
     同一根 K 棒上下都碰 = 無法判定(both);K 棒沒涵蓋 = 略過。
  4. 給命中率、損益兩平命中率(±40、來回成本 2 點 = 52.5%),以及「永遠做多」的基準。

用法(VPS 專案資料夾):
  python3 abs_cum_backtest.py                 # 預設 ±40 / 30 分 / 成本 2 點 / 只看美東 09:30–13:30 / 最近 30 天
  python3 abs_cum_backtest.py --all-hours     # 不分交易時段
  python3 abs_cum_backtest.py --days 10       # 只掃最近 10 天
  python3 abs_cum_backtest.py --show all      # 列出每一段訊號與結果

限制:訊號稀有(一天約 0~2 段),樣本少誤差極大;命中率要穩定高過兩平才有意義,
  樣本夠(連續多天、分出勝負 > 30)之前,結果只是線索,不是結論。
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import math
from zoneinfo import ZoneInfo

import thesis_outcome as TO
import scan_abs_cum as SC
import orderflow_window as OW

NY = ZoneInfo("America/New_York")
TW = ZoneInfo("Asia/Taipei")


def episodes(fires, gap_sec=120):
    """把連續(同方向、間隔 ≤ gap_sec 秒)的訊號併成一段,每段取第一根。"""
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
    ap.add_argument("--all-hours", action="store_true")
    ap.add_argument("--show", choices=["sum", "all"], default="sum")
    a = ap.parse_args()

    bars = TO.load_bars(a.db)
    if len(bars) < 200:
        raise SystemExit("bars 表資料太少(<200 根),先讓網站 K 棒來源跑夠")
    bt = [b[0] for b in bars]; bh = [b[1] for b in bars]; bl = [b[2] for b in bars]

    now = int(dt.datetime.now().timestamp())
    lo, hi = now - a.days * 86400, now
    minutes = OW.read_minutes(lo, hi)
    fires, nearmiss, stats = SC.scan(minutes)
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
    print(f"累積吸收回測 · ±{a.X:g} 點 / 最多 {a.hold} 分鐘 / 來回成本 {a.cost:g} 點 → 損益兩平命中率 {be * 100:.1f}%")
    print(f"掃描範圍(台北){dt.datetime.fromtimestamp(lo, TW):%m-%d %H:%M} ~ {dt.datetime.fromtimestamp(hi, TW):%m-%d %H:%M}")
    print(f"1 分 K {len(bars)} 根(紐約 {dt.datetime.fromtimestamp(bt[0], NY):%m-%d} ~ {dt.datetime.fromtimestamp(bt[-1], NY):%m-%d})")
    print(f"每分鐘有資料 {stats['n_min']} 分鐘;亮訊號分鐘 {stats['n_fire']}、併成 {len(eps)} 段;時段過濾+可判定後 {len(rows)} 段")
    if not rows:
        print("  沒有可判定的訊號段(可能:K 棒沒涵蓋那幾天、或時段過濾後為 0)。可加 --all-hours 或縮小 --days。")
        print("═" * 80)
        return

    if a.show == "all":
        print(f"  {'時間(台北)':<16} {'方向':<6} {'進場':>9} {'強度':>7} {'結果':<8}")
        for e, r in rows:
            tt = dt.datetime.fromtimestamp(e["t"], TW).strftime("%m-%d %H:%M:%S")
            res = {"up": "先到+40", "down": "先到-40", "timeout": "逾時", "both": "雙觸"}.get(r, r)
            print(f"  {tt:<16} {e['dir']:<6} {e['spot']:>9,.1f} {e['strength']:>+7.2f} {res:<8}")

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
        print("  沒有分出勝負的段(可能都在逾時/雙觸)。")

    print("─" * 80)
    if dec >= 30 and (hit / dec - be) > 0:
        print("  判讀:命中率暫時高過兩平,但還要連續多天站穩、樣本夠,才談得上有用。")
    else:
        print(f"  判讀:樣本只有 {dec} 段,誤差 ±~{98.0 / math.sqrt(max(dec, 1)):.0f}%,現在什麼都還看不出來。")
    print("  提醒:每段只取第一根當進場(去重);訊號稀有所以樣本少,這是硬限制,不是腳本沒跑全。")
    print("═" * 80)


if __name__ == "__main__":
    main()
