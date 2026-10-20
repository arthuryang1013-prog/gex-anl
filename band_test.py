#!/usr/bin/env python3
"""如果只抓「近價位、短天期」的合約,算出來的牆 / Flip / 淨 GEX 會差多少?
拿你已經存好的完整快照,假裝只有一部分合約,重算後跟完整版比較。  用法: python3 band_test.py [--db nq_gex.sqlite]"""
import argparse, os, sqlite3, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gex_levels as GL
ap = argparse.ArgumentParser(); ap.add_argument("--db", default="nq_gex.sqlite"); ap.add_argument("--frames", type=int, default=120)
a = ap.parse_args()
con = sqlite3.connect(a.db)
full = [r[0] for r in con.execute("SELECT ts FROM snapshots ORDER BY ts") if GL.frame_is_full(con, r[0])]
if not full: sys.exit("沒有三個來源都齊的快照")
step = max(1, len(full) // a.frames); frames = full[::step]
CASES = [("±2% 且 0~2 天到期", 0.02, 2), ("±1.5% 且 0~2 天到期", 0.015, 2), ("±2% 且 0~7 天(只縮價格範圍)", 0.02, 7), ("±3% 且 0~7 天", 0.03, 7)]
res = {c[0]: {s: dict(call=[], put=[], flip=[], net=[], n=[]) for s in ("NQ", "QQQ", "NDX")} for c in CASES}
for ts in frames:
    df, spot = GL.load_frame(con, ts)
    if spot is None or df.empty: continue
    base = GL.compute_levels(df, spot)
    for name, band, maxd in CASES:
        sub = df[((df["strike_nq"] - spot).abs() <= spot * band) & (df["dte"] <= maxd)]
        lv = GL.compute_levels(sub, spot)
        for s in ("NQ", "QQQ", "NDX"):
            b, c = base[s]["OI"]["ALL"], lv[s]["OI"]["ALL"]
            for k in ("call", "put", "flip"):
                if b[k] is not None and c[k] is not None: res[name][s][k].append(abs(b[k] - c[k]))
                elif b[k] is not None: res[name][s][k].append(np.nan)    # 完整版有、縮小版找不到
            nb = df[df["src"] == s]["gex"].sum(); nc = sub[sub["src"] == s]["gex"].sum()
            if nb: res[name][s]["net"].append(abs(nc - nb) / abs(nb) * 100)
            res[name][s]["n"].append(len(sub[sub["src"] == s]) / max(1, (df["src"] == s).sum()) * 100)
print(f"用 {len(frames)} 個完整快照比較(OI、全部到期日)。數字 = 和完整版差多少點(中位數 / 最差 / 找不到的次數)\n")
for name, *_ in CASES:
    print(f"■ 只抓 {name}")
    for s in ("NQ", "QQQ", "NDX"):
        r = res[name][s]; line = f"  {s:4} 合約數剩 {np.nanmedian(r['n']):3.0f}% | "
        for k, lab in (("call", "Call牆"), ("put", "Put牆"), ("flip", "Flip")):
            v = np.array(r[k], dtype=float); miss = int(np.isnan(v).sum()); v = v[~np.isnan(v)]
            line += f"{lab} {np.median(v) if len(v) else float('nan'):5.1f}/{v.max() if len(v) else float('nan'):5.1f}/{miss}次  "
        line += f"| 淨GEX 差 {np.median(r['net']):.0f}%(最差 {np.max(r['net']):.0f}%)"
        print(line)
    print()
print("看法:牆與 Flip 差的點數要跟你交易的 40 點比;只差幾點算沒影響。淨 GEX 的差是金額大小,不影響位置。")
