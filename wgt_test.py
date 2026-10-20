#!/usr/bin/env python3
"""加權合計的 Flip / 牆 穩不穩?  (1) 權重稍微改一點,線會動多少  (2) 相鄰兩個快照之間,線跳多少(跟單一來源比)
用法: python3 wgt_test.py [--db nq_gex.sqlite]   (放在 gex anl 資料夾,需要 gex_web_server.py 和 gex_levels.py)"""
import argparse, itertools, os, sqlite3, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gex_levels as GL
import gex_web_server as W
ap = argparse.ArgumentParser(); ap.add_argument("--db", default="nq_gex.sqlite"); ap.add_argument("--frames", type=int, default=120)
a = ap.parse_args()
con = sqlite3.connect(a.db)
full = [r[0] for r in con.execute("SELECT ts FROM snapshots ORDER BY ts") if GL.frame_is_full(con, r[0])]
frames = full[::max(1, len(full) // a.frames)]
gcol, wcol = GL.BASES["OI"]
BASE = {"NQ": 20.0, "QQQ": 35.0, "NDX": 45.0}
VARS = {"基準 20/35/45": BASE}
for s in BASE:
    for d in (-10, 10):
        w = dict(BASE); w[s] = max(0.0, w[s] + d); VARS[f"{s}{d:+d}"] = w
def levels(df, spot, w, norm):
    sub = W.weighted_frame(df, w, norm, gcol) if w else df
    sub = sub.copy()
    sub["lv_sum"] = np.round(sub["strike_nq"] / 25.0) * 25.0
    wl = GL._walls(sub, spot, gcol, "lv_sum")
    return wl["call"], wl["put"], GL._flip(sub, spot, wcol)
rows = []   # per frame: dict name -> (call,put,flip)
for ts in frames:
    df, spot = GL.load_frame(con, ts)
    if spot is None or df.empty: continue
    r = {"spot": spot}
    for s in BASE:
        sub = df[df["src"] == s].copy(); sub["lv_src"] = np.round(sub["strike_nq"] * 4) / 4
        wl = GL._walls(sub, spot, gcol, "lv_src"); r[s] = (wl["call"], wl["put"], GL._flip(sub, spot, wcol))
    for norm in (True, False):
        for name, w in VARS.items(): r[(name, norm)] = levels(df, spot, w, norm)
    rows.append(r)
def d(a, b, i):
    return abs(a[i] - b[i]) if a[i] is not None and b[i] is not None else np.nan
def stat(v):
    v = np.array(v, dtype=float); miss = int(np.isnan(v).sum()); v = v[~np.isnan(v)]
    return (f"{np.median(v):6.1f} / {v.max():6.1f}" if len(v) else "   —  /   —  ") + f"  (算不出 {miss} 次)"
print(f"用 {len(rows)} 個完整快照。數字 = 點數(中位數 / 最大)\n")
for norm in (True, False):
    print("■ " + ("先把三個來源縮到同樣規模(目前預設)" if norm else "不縮放,直接依金額加權"))
    for i, lab in ((2, "Flip"), (0, "Call牆"), (1, "Put牆")):
        ch = []
        for r in rows:
            b = r[("基準 20/35/45", norm)]
            for name in VARS:
                if name.startswith("基準"): continue
                ch.append(d(b, r[(name, norm)], i))
        print(f"  權重動 10 個百分點時 {lab:5} 移動:{stat(ch)}")
    for i, lab in ((2, "Flip"),):
        jit = [d(rows[k][("基準 20/35/45", norm)], rows[k + 1][("基準 20/35/45", norm)], i) for k in range(len(rows) - 1)]
        print(f"  相鄰快照之間 加權{lab} 跳動:{stat(jit)}")
    print()
print("■ 對照:單一來源 Flip 在相鄰快照之間的跳動")
for s in BASE:
    jit = [d(rows[k][s], rows[k + 1][s], 2) for k in range(len(rows) - 1)]
    print(f"  {s:4}: {stat(jit)}")
inside = 0; tot = 0
for r in rows:
    f = [r[s][2] for s in BASE if r[s][2] is not None]; w = r[("基準 20/35/45", True)][2]
    if len(f) == 3 and w is not None:
        tot += 1; inside += (min(f) <= w <= max(f))
print(f"\n加權 Flip 落在三個單一來源 Flip 之間的比例:{inside}/{tot}(不在之間代表加權後曲線形狀變了,不是簡單的平均)")
