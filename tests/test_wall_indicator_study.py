"""wall_indicator_study:合成 80 次碰牆,『守牆方事件多 → 守住』是人為植入的關聯,其餘指標是隨機雜訊。
驗證:碰牆偵測、守住/穿越標記、無偷看未來、事件功能上線前不當成 0、統計能抓到植入的關聯且不誤報雜訊。"""
import os, sys, random, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wall_indicator_study as W
import orderflow_store as ST
from flow_outcomes import outcome

rng = random.Random(7)
T0 = 1_700_000_040 // 60 * 60
L = 1000.0
bars, minutes, events, truth = [], {}, [], []
t = T0
for ep in range(80):
    bounce = rng.random() < 0.5
    truth.append(bounce)
    seg = [(985, 986, 984, 985)] * 10
    seg += [(990, 991, 989, 990), (995, 1001, 994, 998)]               # 第 11 根碰牆
    for i in range(1, 8):                                               # 之後 7 分鐘走到 ±45
        p = 998 + (-1 if bounce else 1) * min(45, i * 7)
        seg.append((p, p + 1, p - 1, p))
    seg += [(985, 986, 984, 985)] * 41
    for i, (o, h, l, c) in enumerate(seg):
        bars.append((t + i * 60, o, h, l, c))
    b0 = t + 11 * 60
    for m in range(b0 - 300, b0 + 60, 60):                              # 碰牆前 5 分鐘的指標:全部是雜訊
        minutes[m] = dict(t=m, n=60, delta_tr=rng.uniform(-50, 50), vol_tr=rng.uniform(100, 900), imb=rng.uniform(-1, 1),
                          big_vol=rng.uniform(0, 60), sweep_buy=rng.uniform(0, 20), sweep_sell=rng.uniform(0, 20))
    # 植入:守住時 80% 有 ≥1 個「守牆方」事件(承接買方 = 偏多;從下方推牆時守牆方是偏空,即 sell 側吸收),穿越時 20%
    if rng.random() < (0.8 if bounce else 0.2):
        events.append(dict(id=f"d{ep}", t=b0 + 10, k="absorb", s="sell", p=L, v=100, x={}))
    t += len(seg) * 60
events.insert(0, dict(id="first", t=T0 - 3600, k="iceberg", s="buy", p=900, v=50, x={}))     # 事件功能最早時間點

lv = lambda tt: (T0, 990.0, {"NQ|all": {"call": L, "put": None, "flip": None, "call_a": None, "put_b": None}})
tc = W.find_touches(bars, lv, lambda n: n.startswith("NQ|all|"), all_hours=True)
assert len(tc) == 80 and all(x["push"] == 1 and x["name"] == "NQ|all|call" for x in tc), len(tc)
ys = [W.label(bars, x) for x in tc]
assert [bool(y) for y in ys] == truth, "守住/穿越標記要對得上植入的走勢"

# 沒有偷看未來:把碰牆之後的分鐘資料改成極端值,特徵不變
f1 = W.features(minutes, events, T0 - 3600, tc[0]["t"], 1)
m2 = dict(minutes); m2[tc[0]["t"] + 60] = dict(t=tc[0]["t"] + 60, n=60, delta_tr=9999, vol_tr=9999)
assert W.features(m2, events, T0 - 3600, tc[0]["t"], 1) == f1, "碰牆之後的資料不能影響特徵"
# 事件功能上線之前:ev_* 要是 None,不是 0;分鐘資料不足:指標 None
assert W.features(minutes, events, T0 + 10**9, tc[0]["t"], 1)["ev_defend"] is None
assert W.features({}, events, None, tc[0]["t"], 1)["push_delta"] is None
# 推進方向:向下推時符號反轉
a, b = W.features(minutes, [], None, tc[3]["t"], 1), W.features(minutes, [], None, tc[3]["t"], -1)
assert abs(a["push_delta"] + b["push_delta"]) < 1e-9 and a["vol_tr"] == b["vol_tr"]

samples = [(W.features(minutes, events, events[0]["t"], x["t"], x["push"]), y) for x, y in zip(tc, ys)]
res = W.analyze(samples, perm=2000)
d = res["per"]["ev_defend"]["split"]
assert d and d["p"] < 0.01 and d["diff"] > 0.3, d                      # 植入的關聯要抓得到:守牆方事件多 → 守住率高
# 純雜訊不該極端顯著。(雜訊指標偶爾會碰巧 p<5%:另用 40 組亂數驗證過,200 次檢定有 10 次 = 5.0%,跟理論一致,所以這裡用 0.1% 當門檻)
for k in ("push_delta", "vol_tr", "push_imb", "big_vol", "push_sweep"):
    r = res["per"][k]["split"]
    assert r is None or r["p"] > 0.001, (k, r)
assert res["per"]["ev_push"]["n"] == 80
out = W.render(res)
assert "守牆方事件數" in out and "提醒" in out
# 樣本不足時不硬判
assert "沒有可用" in W.render(W.analyze([]))
few = W.analyze(samples[:8]); assert all("split" not in v or v["split"] is None for v in few["per"].values())
print(out)
print("OK")
