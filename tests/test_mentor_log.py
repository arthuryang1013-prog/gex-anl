import os, sys, sqlite3, tempfile, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zoneinfo import ZoneInfo
import mentor_log as ML
NY = ZoneInfo("America/New_York")
d = tempfile.mkdtemp(); log = d + "/m.sqlite"
b = sqlite3.connect(d + "/b.sqlite"); b.execute("CREATE TABLE bars (t INTEGER PRIMARY KEY,o,h,l,c,v)")
t0 = int(dt.datetime(2026, 10, 22, 9, 30, tzinfo=NY).timestamp())
px = 25000.0
for m in range(200):                      # 前 30 分鐘:緩漲,第 12 分鐘高點碰 +40;之後緩跌
    t = t0 + m * 60
    if m < 12: px += 3
    elif m < 60: px -= 1.5
    else: px -= 0.2
    b.execute("INSERT INTO bars VALUES (?,?,?,?,?,0)", (t, px, px + 1, px - 1, px))
b.commit()
# 25000 起漲 -> 第 12 分約 25036 + 1 ... 讓 +40 在第 14 分前碰到
b.execute("UPDATE bars SET h = 25041 WHERE t=?", (t0 + 13 * 60,)); b.commit()
T = t0 - 60                                   # 評論 t = 9:29,spot 25000
i1 = ML.log_comment("thesis", T, 25000.0, "偏多理由…", "偏多", 30, -10, "正 gamma:壓波動", 0.4, "中", 60, dict(flip=24990), path=log)
assert i1
assert ML.log_comment("thesis", T + 60, 25000.0, "太密", "偏多", 30, 5, "x", 0, "中", 60, path=log) is None       # 節流
i2 = ML.log_comment("thesis", T + 600, 25000.0, "偏空理由…", "偏空", -30, 20, "負 gamma:放大波動", -0.4, "中", 60, path=log)
i3 = ML.log_comment("ask", T + 900, 25000.0, "答…", question="現在?", path=log)
i4 = ML.log_comment("thesis", T + 3600*3, 25000.0, "遠在K棒之外", "偏多", 10, 10, "x", 0, "弱", 55, path=log)
assert ML.settle(b, log) >= 2
x = ML.daily("2026-10-22", "all", bars_con=b, path=log)
assert x["ok"]; tl = {r["id"]: r for r in x["timeline"]}
assert tl[i1]["verdict"] == "命中", tl[i1]
assert tl[i2]["verdict"] == "未命中", tl[i2]            # 偏空但先到上
assert tl[i3]["verdict"] == "問答"
assert tl[i4]["verdict"] == "待結算" and x["pending"] == 1
st = x["stats"]; assert st["thesis"]["hit"] == 1 and st["thesis"]["miss"] == 1 and st["thesis"]["rate"] == 50.0
assert st["split_dir"]["n"] == 3 or st["split_dir"]["n"] == 2
assert st["always_long"]["hit"] == 2 and st["always_short"]["hit"] == 0
# 規則文字與敘事快取
txt, ai = ML.narrative(x, None, path=log); assert not ai and "命中率" in txt
seen = []
txt, ai = ML.narrative(x, lambda p: (seen.append(p) or "AI 日結 #%d …" % i1), path=log)
assert ai and seen and f"#{i1}" in seen[0] and "不可自行編造" in seen[0]
x2 = ML.daily("2026-10-22", "all", bars_con=b, path=log); assert x2["summary"]["ai"]
txt2, _ = ML.narrative(x2, lambda p: 1/0, path=log); assert txt2 == txt          # 有 AI 快取 → 不再呼叫
# 範圍:盤中 09:30–13:30 只含 T 之後的(T 是 9:29 → 排除)
xs = ML.daily("2026-10-22", "session", bars_con=b, path=log); assert xs["n"] == 3, xs["n"]
assert ML.latest_day(log) == "2026-10-22"
print("mentor_log ok")
