#!/usr/bin/env python3
"""NQ / QQQ / NDX 三個來源比較:體量、散戶程度、對 NQ 走勢的反應。
用法:  python3 source_report.py            (在放 nq_gex.sqlite 和 gex_levels.py 的資料夾裡)
       python3 source_report.py --db 路徑  --X 40
全部是從你自己收集的資料算出來的描述;資料天數少的時候,第三部分只能當參考,不能當結論。"""
import argparse, datetime as dt, os, sqlite3, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gex_levels as GL

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="nq_gex.sqlite")
ap.add_argument("--X", type=float, default=40.0, help="反彈/穿越的判定點數(預設 40,你的停利點)")
ap.add_argument("--look", type=int, default=60, help="快照後最多等幾分鐘價格碰到關卡")
ap.add_argument("--after", type=int, default=30, help="碰到後觀察幾分鐘")
ap.add_argument("--max-frames", type=int, default=600)
a = ap.parse_args()
con = sqlite3.connect(a.db)
out = []
def P(s=""): print(s); out.append(s)

snaps = [r[0] for r in con.execute("SELECT ts FROM snapshots ORDER BY ts")]
if not snaps:
    sys.exit("資料庫裡沒有快照")
ep = lambda ts: int(dt.datetime.fromisoformat(ts).timestamp())
days = {}
for ts in snaps:
    d = dt.datetime.fromisoformat(ts).astimezone(GL.NY).date() if hasattr(GL, "NY") else ts[:10]
    days.setdefault(d, []).append(ts)
P(f"資料:{len(snaps)} 筆快照,{len(days)} 個交易日 ({min(days)} ~ {max(days)})")
P()

# ---------------- 1. 體量與散戶程度 ----------------
rows = []
for d, tss in sorted(days.items()):
    full = [t for t in tss if GL.frame_is_full(con, t)]   # 三個來源都有資料的快照(夜盤只有 NQ,不能拿來比)
    if not full: continue
    ts = full[-1]                                    # 當天最後一筆完整快照(成交量是當天累計)
    df, spot = GL.load_frame(con, ts)
    for s in GL.SOURCES:
        x = df[df["src"] == s]
        if x.empty: continue
        unit = x["mult"] * x["F"]                    # 一口合約對應的美元名目
        near = (x["strike_nq"] - spot).abs() <= spot * 0.005
        rows.append(dict(day=d, src=s, n=len(x), oi=x["oi"].sum(), oi_usd=(x["oi"] * unit).sum() / 1e9,
                         vol=x["vol"].sum(), vol_usd=(x["vol"] * unit).sum() / 1e9,
                         gex_abs=x["gex"].abs().sum() / 1e9, per_contract=float(unit.mean()) / 1e3,
                         z_vol=x.loc[x["dte"] == 0, "vol"].sum() / max(x["vol"].sum(), 1),
                         near_vol=x.loc[near, "vol"].sum() / max(x["vol"].sum(), 1)))
R = pd.DataFrame(rows)
g = R.groupby("src").mean(numeric_only=True).reindex(GL.SOURCES)
P(f"【1】體量與散戶程度(各日最後一筆「三個來源都齊」的快照的平均,共 {R.day.nunique()} 天)")
P(f"{'':6}{'一口名目':>10}{'未平倉口數':>12}{'未平倉名目':>12}{'成交口數':>10}{'成交名目':>10}{'GEX規模':>10}{'0DTE占成交':>11}{'價內外0.5%內':>12}")
P(f"{'來源':6}{'(千美元)':>10}{'':>12}{'(十億美元)':>12}{'':>10}{'(十億美元)':>10}{'(十億/1%)':>10}{'':>11}{'占成交':>12}")
for s, r in g.iterrows():
    P(f"{s:6}{r.per_contract:>10,.0f}{r.oi:>12,.0f}{r.oi_usd:>12,.1f}{r.vol:>10,.0f}{r.vol_usd:>10,.1f}{r.gex_abs:>10,.1f}{r.z_vol*100:>10.0f}%{r.near_vol*100:>11.0f}%")
P("說明:'一口名目' = 乘數 × 標的價,越小代表小資金越容易參與;'0DTE占成交' 與 '價平附近占成交' 越高,通常越偏短線投機。")
P("      這些是間接指標,不是直接量到「散戶」,因為沒有帳戶類型資料。")
P()

# ---------------- 2. 加權後各占多少 ----------------
tot = g["gex_abs"].sum()
P("【2】三者 GEX 規模占比(用來看「直接相加」時誰會蓋過誰)")
for s, r in g.iterrows():
    P(f"  {s:5} {r.gex_abs/tot*100:5.1f}%")
P()

# ---------------- 3. 關卡有沒有反應 ----------------
bars = None
if con.execute("SELECT name FROM sqlite_master WHERE name='bars'").fetchone():
    bars = pd.read_sql("SELECT t,o,h,l,c FROM bars ORDER BY t", con)
if bars is None or len(bars) < 500:
    P("【3】沒有足夠的 1 分 K(bars 表),無法檢查關卡反應。請先讓網站的 K 棒來源執行幾天。")
else:
    bt, bh, bl, bc = bars.t.values, bars.h.values, bars.l.values, bars.c.values
    step = max(1, len(snaps) // a.max_frames)
    ev = {}   # (src, kind, is_real) -> [bounce, break, neither]
    seen = set()   # 同一次碰觸只算一次(相鄰快照會看到同一根 K 棒)
    frames = snaps[::step]
    for ts in frames:
        e = ep(ts)
        i0 = np.searchsorted(bt, e, side="right") - 1
        if i0 < 0 or bt[i0] < e - 300 or i0 + a.look + a.after >= len(bt): continue
        df, spot = GL.load_frame(con, ts)
        if spot is None or df.empty: continue
        lv = GL.compute_levels(df, spot)
        for s in GL.SOURCES:
            for basis in ("OI",):
                d = lv[s][basis]["ALL"]
                for kind in ("call", "put", "flip"):
                    L = d.get(kind)
                    if L is None or abs(L - spot) < 10 or abs(L - spot) > 150: continue
                    for tag, LL in (("real", L), ("decoy+", L + 60), ("decoy-", L - 60)):
                        come = np.sign(spot - LL)   # +1 = 價格在關卡上方
                        j = None
                        for k in range(i0 + 1, i0 + 1 + a.look):
                            if bl[k] <= LL <= bh[k]: j = k; break
                        if j is None: continue
                        key = (s, kind, tag, j, round(LL / 5))
                        if key in seen: continue
                        seen.add(key)
                        res = 2
                        for k in range(j, min(len(bt), j + a.after + 1)):
                            away = come * (bc[k] - LL); thru = -come * (bc[k] - LL)
                            hi_away = come * ((bh[k] if come > 0 else bl[k]) - LL)
                            hi_thru = -come * ((bl[k] if come > 0 else bh[k]) - LL)
                            if hi_away >= a.X and hi_thru < a.X: res = 0; break
                            if hi_thru >= a.X and hi_away < a.X: res = 1; break
                            if hi_away >= a.X and hi_thru >= a.X: res = 2; break
                        ev.setdefault((s, kind, "real" if tag == "real" else "decoy"), []).append(res)
    P(f"【3】價格碰到關卡後 {a.after} 分鐘內,先走 {a.X:.0f} 點的方向(只看 OI、全部到期日)")
    P("   意思:價格從原本那一側碰到某個價位後,是先「被推回去 {0} 點」還是先「穿過去 {0} 點」。".format(int(a.X)))
    P("   對照組:把關卡往上、往下各挪 60 點當成假關卡,用同樣方法算。真關卡要明顯優於假關卡才算有用。")
    P(f"{'來源':6}{'種類':6}{'次數':>6}{'被推回':>9}{'穿過去':>9}{'都沒到':>9}   |  假關卡:{'次數':>6}{'被推回':>9}{'穿過去':>9}")
    for s in GL.SOURCES:
        for kind in ("call", "put", "flip"):
            r = ev.get((s, kind, "real"), []); d = ev.get((s, kind, "decoy"), [])
            f = lambda x, v: (sum(1 for y in x if y == v) / len(x) * 100) if x else float("nan")
            P(f"{s:6}{kind:6}{len(r):>6}{f(r,0):>8.0f}%{f(r,1):>8.0f}%{f(r,2):>8.0f}%   |  {'':8}{len(d):>6}{f(d,0):>8.0f}%{f(d,1):>8.0f}%")
    n_real = sum(len(v) for k, v in ev.items() if k[2] == "real")
    P(f"   總共 {n_real} 次碰觸。次數少於約 30 的列,百分比不穩定,不要據此下結論。")
open("source_report.txt", "w", encoding="utf-8").write("\n".join(out))
print("\n(已存成 source_report.txt)")
