#!/usr/bin/env python3
"""基差檢查:QQQ / NDX 換算成 NQ 座標時用的「價格比例」,一天內會漂移多少點?用「比例法」vs「差額法」差多少?

背景:程式把 QQQ/NDX 的履約價乘上 (NQ現價 ÷ 該來源現價) 換成 NQ 點位(比例法),假設比例整天不變。
實際上 NQ 與 NDX 的差(基差)會隨利率、股息、到期日改變,盤中也會因為兩邊報價時間差而跳動。
本腳本只量「這個假設錯得多嚴重」,不改任何東西。

用法(VPS 專案資料夾):  python3 basis_check.py [--db nq_gex.sqlite] [--days 10]
輸出白話:每天每個來源,比例的漂移相當於 NQ 幾點;離現價 ±100/±300 點的牆,比例法與差額法會差幾點。
"""
import argparse, datetime as dt, os, sqlite3, sys
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

NY = ZoneInfo("America/New_York")
ap = argparse.ArgumentParser()
ap.add_argument("--db", default="nq_gex.sqlite")
ap.add_argument("--days", type=int, default=10)
a = ap.parse_args()
con = sqlite3.connect(a.db)

df = pd.read_sql(
    "SELECT c.ts AS ts, c.src AS src, c.dte AS dte, c.strike AS strike, c.strike_nq AS strike_nq, c.F AS F, s.spot AS spot "
    "FROM contracts c JOIN snapshots s ON s.ts = c.ts WHERE c.src IN ('QQQ','NDX') AND c.strike > 0 AND c.strike_nq > 0", con)
if df.empty:
    sys.exit("資料庫裡沒有 QQQ/NDX 合約(需要新版收集器產生的 src / strike_nq 欄位)")
df["ratio"] = df["strike_nq"] / df["strike"]
g = df.groupby(["src", "ts"]).agg(spot=("spot", "first"), ratio=("ratio", "median"),
                                  F=("F", lambda x: float(np.median(x))), n=("strike", "size")).reset_index()
g["day"] = pd.to_datetime(g["ts"], utc=True).dt.tz_convert(NY).dt.date
days = sorted(g["day"].unique())[-a.days:]
print(f"QQQ/NDX → NQ 換算比例的日內漂移(最近 {len(days)} 個美東日)")
print("說明:'漂移' = 當天比例最大值與最小值的差,換成 NQ 點數(在現價附近)。越小代表「比例不變」的假設越安全。\n")
print(f"{'日期':12}{'來源':6}{'快照數':>7}{'比例範圍':>22}{'漂移(NQ點)':>12}")
for src in ("NDX", "QQQ"):
    for d in days:
        x = g[(g.src == src) & (g.day == d)]
        if len(x) < 3:
            continue
        lo, hi = x.ratio.min(), x.ratio.max()
        drift = (hi - lo) / x.ratio.mean() * x.spot.mean()
        print(f"{str(d):12}{src:6}{len(x):>7}{lo:>12.5f}~{hi:<9.5f}{drift:>12.1f}")
print()

x = g[g.src == "NDX"].dropna(subset=["F"])
if len(x):
    print("比例法 vs 差額法(只對 NDX 有意義,因為單位跟 NQ 同為『點』):")
    print("  比例法:NQ位置 = 履約價 × (NQ現價 ÷ NDX現價)    差額法:NQ位置 = 履約價 + (NQ現價 − NDX現價)")
    r = x.iloc[-1]
    ndx = r.F
    ratio = r.spot / ndx
    print(f"  以最近一筆快照({r.ts})為例:NQ {r.spot:,.1f}、NDX 遠期價 {ndx:,.1f}、差 {r.spot-ndx:+,.1f} 點、比例 {ratio:.5f}")
    print(f"  {'牆離現價':>10}{'比例法':>12}{'差額法':>12}{'兩者差':>10}")
    for off in (-300, -100, 100, 300):
        k = ndx + off
        p_ratio, p_off = k * ratio, k + (r.spot - ndx)
        print(f"  {off:>+9}點{p_ratio:>12,.1f}{p_off:>12,.1f}{p_ratio-p_off:>+10.1f}")
    print("  兩種方法在現價附近一致,離現價越遠差越大;若差距小於你的停利 40 點的一小部分,換算方式不是主要誤差來源。")
    print("  註:這裡用 NDX 遠期價 F 當 NDX 現價近似(資料庫沒存 NDX 指數現價),有利率/股息的小偏差。")
print("\n限制:這是描述性量測;不能由此推出哪個方法『比較準』——要看牆的實際反應,可用 source_report.py 的真/假關卡對照。")
