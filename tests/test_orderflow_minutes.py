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
