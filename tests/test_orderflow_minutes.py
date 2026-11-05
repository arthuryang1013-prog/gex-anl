"""orderflow_minutes:bridge 的 delta 是「近 60 秒滾動累計」,逐秒加總會放大約 60 倍;每分鐘取最後一筆才對。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orderflow_minutes as OM

# 每秒成交 +1 口買,持續 180 秒;bridge 每秒的 delta = 最近 60 秒累計 = min(秒數, 60)
base = 1_700_000_040          # 對齊分鐘
recs = []
cvd = 0.0
for s in range(180):
    cvd += 1
    recs.append(dict(received_at=base + s, delta=float(min(s + 1, 60)), vol=float(min(s + 1, 60)), cvd_tr=cvd, imb=0.1 * (s % 2), spot=100.0 + s))
rows = OM.minute_rows(recs)
assert len(rows) == 3, len(rows)
naive = sum(r["delta"] for r in recs)
assert naive > 9000                                  # 逐秒加總:實際只有 180 口,卻得到約 10 倍以上的錯誤值
fixed = sum(r["delta"] for r in rows)
assert fixed == 180.0, fixed                         # 每分鐘最後一筆:60+60+60 = 實際 180 口
assert rows[-1]["cvd_tr"] == 180.0 and rows[-1]["spot"] == 100.0 + 179
assert abs(rows[0]["imb"] - 0.05) < 1e-9
assert rows[0]["delta_tr"] is None                   # 舊紀錄沒有的欄位 → None
print("orderflow_minutes ok (逐秒加總 %d → 修正後 %d)" % (naive, fixed))

# vpin_impact:單邊主導 → vpin 高;平衡 → vpin 低;價格衝擊 = 中位數 |Δspot|/vol
def mk(delta_tr, vol_tr, spot):
    return dict(delta_tr=float(delta_tr), vol_tr=float(vol_tr), spot=float(spot))

rows1 = [mk(100.0, 100.0, 100.0 + i) for i in range(30)]          # 全部買 → vpin ≈ 1
v1, im1, n1 = OM.vpin_impact(rows1)
assert v1 is not None and v1 > 0.95, v1

rows2 = [mk(100.0 if i % 2 == 0 else -100.0, 200.0, 100.0 + (i % 2)) for i in range(30)]   # 買賣相抵 → vpin ≈ 0
v2, _, _ = OM.vpin_impact(rows2)
assert v2 is not None and v2 < 0.5, v2

rows3 = [mk(0.0, 1000.0, 100.0 + 10 * i) for i in range(30)]       # 每分鐘 +10 點 / 1000 口 → 0.01 點/口
_, im3, _ = OM.vpin_impact(rows3)
assert im3 is not None and abs(im3 - 0.01) < 1e-9, im3

assert OM.vpin_impact([]) == (None, None, 0)
assert OM.vpin_impact([mk(10.0, 100.0, 100.0)]) == (None, None, 1)   # 不到 min_rows
print("vpin/impact ok")
