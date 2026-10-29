import os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import thesis_stable as TS
# 1) 分數在 0 附近抖動:舊標籤狂翻、新標籤不動(中性)
random.seed(1)
ser = [(i * 180, random.uniform(-12, 12)) for i in range(60)]
old = TS.flips([TS.legacy_label(s) for _, s in ser]); r = TS.stabilize(ser); new = TS.flips([x["label"] for x in r])
assert old > 15 and new == 0 and r[-1]["label"] == "中性", (old, new)
# 2) 明確的多頭段落:要連續 K 個快照才進入,且不會被單一反向點翻掉
ser = [(i * 180, 40.0) for i in range(8)] + [(8 * 180, -30.0)] + [(i * 180, 40.0) for i in range(9, 14)]
r = TS.stabilize(ser); labs = [x["label"] for x in r]
assert labs[0] == "中性" and labs[1] == "偏多" and "偏空" not in labs, labs
# 3) 偏多不能直接翻偏空:一定先經過中性
ser = [(i * 180, 50.0) for i in range(6)] + [(i * 180, -50.0) for i in range(6, 20)]
labs = [x["label"] for x in TS.stabilize(ser)]
i = labs.index("偏空"); assert labs[i - 1] == "中性" and "偏多" in labs[:i], labs
# 4) since 在標籤改變時更新;None 分數被略過
r = TS.stabilize([(0, None), (180, 60), (360, 60), (540, 60)])
assert r[-1]["label"] == "偏多" and r[-1]["since"] == 360 and len(r) == 3, r
print("thesis_stable ok; flips old/new on noise:", old, new)
