"""訂單流事件偵測(吸收/冰山/力竭/掃單):用假行情驅動 bridge 的 handler,時間用假時鐘控制。"""
import os, sys, types
fake = types.ModuleType("bookmap"); sys.modules["bookmap"] = fake
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orderflow_bridge as b
b._post = lambda pkt: None
b._pips = 1.0; b._size_mult = 1.0            # 為了好算:價格單位 = 點、口數不換算
T = [1_700_000_000.0]
b.time = types.SimpleNamespace(time=lambda: T[0], sleep=lambda x: None)


def reset():
    b.events.clear(); b.secs.clear(); b.lvl_trades.clear(); b.lvl_vis.clear(); b._last_evt.clear(); b._burst = None
    b.bid_book.clear(); b.ask_book.clear(); b.best_bid = b.best_ask = None


def tr(price, size, buy):
    b.handle_trades(None, "x", price, size, False, buy, True, True)


def kinds():
    return [(e["k"], e["s"]) for e in b.events]

# ---- 掃單:同方向 0.1 秒內連吃 4 個價位 ----
reset()
for i in range(4):
    tr(100 + i, 6, True); T[0] += 0.1
T[0] += 1.0; b._detect_events(T[0])
assert ("sweep", "buy") in kinds(), kinds()
ev = [e for e in b.events if e["k"] == "sweep"][0]
assert ev["v"] == 24 and ev["x"]["lv"] == 4 and ev["p"] == 103.0, ev
# 只吃一個價位 → 不算掃單
reset(); tr(100, 30, True); T[0] += 1.5; b._detect_events(T[0]); assert not b.events, kinds()

# ---- 吸收:20 秒內賣方主動 200 口,價格幾乎沒動 ----
reset(); T[0] += 1000
for i in range(25):
    tr(1000 + (i % 2) * 1, 8, False); T[0] += 0.8          # 25 筆 ×8 = 200 口,價 1000/1001 來回
b._detect_events(T[0])
assert ("absorb", "buy") in kinds(), kinds()
# 同樣的量但價格一路下殺 → 不是吸收
reset(); T[0] += 1000
for i in range(25):
    tr(1000 - i, 8, False); T[0] += 0.8
b._detect_events(T[0]); assert ("absorb", "buy") not in kinds(), kinds()

# ---- 冰山:買盤 100 價位看得見最多 5 口,卻被主動賣打掉 60 口(10 筆)且價位守住 ----
reset(); T[0] += 1000
b.handle_depth(None, "x", True, 100, 5); b.handle_depth(None, "x", False, 101, 5)
for i in range(10):
    tr(100, 6, False); T[0] += 2.0
b._detect_events(T[0])
assert ("iceberg", "buy") in kinds(), kinds()
# 看得見 100 口、被打掉 60 口 → 只是正常被吃,不是冰山
reset(); T[0] += 1000
b.handle_depth(None, "x", True, 100, 100); b.handle_depth(None, "x", False, 101, 5)
for i in range(10):
    tr(100, 6, False); T[0] += 2.0
b._detect_events(T[0]); assert ("iceberg", "buy") not in kinds(), kinds()
# 價位被打穿(最佳買價掉到 99)→ 不算守住
reset(); T[0] += 1000
b.handle_depth(None, "x", True, 100, 5); b.handle_depth(None, "x", False, 101, 5)
for i in range(10):
    tr(100, 6, False); T[0] += 2.0
b.handle_depth(None, "x", True, 100, 0); b.handle_depth(None, "x", True, 99, 5)
b._detect_events(T[0]); assert ("iceberg", "buy") not in kinds(), kinds()

# ---- 力竭:先 35 秒買方猛推 +6 點(淨買 100 口),最後 15 秒幾乎沒成交 ----
reset(); T[0] += 1000
t0 = T[0]
for i in range(35):
    tr(2000 + int(i * 6 / 35), 3, True); T[0] += 1.0       # 105 口買、價 2000→2005
T[0] += 15.0                                               # 之後 15 秒沒有成交
b._detect_events(T[0])
assert ("exhaust", "buy") in kinds(), kinds()
# 推完還在持續買 → 沒力竭
reset(); T[0] += 1000
for i in range(50):
    tr(2000 + int(i * 8 / 50), 3, True); T[0] += 1.0
b._detect_events(T[0]); assert ("exhaust", "buy") not in kinds(), kinds()

# ---- 冷卻:同類事件不連發;封包帶 events、舊事件會被清掉 ----
reset(); T[0] += 1000
for _ in range(3):
    for i in range(4):
        tr(100 + i, 6, True); T[0] += 0.1
    T[0] += 1.0; b._detect_events(T[0])
assert sum(1 for e in b.events if e["k"] == "sweep") == 1, kinds()
b._tick(); T[0] += 400; b._detect_events(T[0]); assert not b.events
b.EVENTS_ON = False; reset(); tr(100, 10, True); assert not b.secs
print("orderflow events ok")
