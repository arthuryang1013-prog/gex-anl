"""訂單流事件偵測(掃單/停損/冰山/吸收/力竭):用假行情驅動 bridge 的 handler,時間用假時鐘控制。
含「亂數行情誤報率」檢查:純隨機的成交流不該一直冒事件。"""
import os, sys, types, random
fake = types.ModuleType("bookmap"); sys.modules["bookmap"] = fake
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orderflow_bridge as b
b._post = lambda pkt: None
b._pips = 1.0; b._size_mult = 1.0
T = [1_700_000_000.0]
b.time = types.SimpleNamespace(time=lambda: T[0], sleep=lambda x: None)


def reset():
    b.events.clear(); b.secs.clear(); b.lvl_trades.clear(); b.lvl_vis.clear(); b._last_evt.clear()
    b.lvl_last_tr.clear(); b.lvl_refill.clear(); b.ord_stat.clear(); b._ice_cand.clear(); b._ice_hist.clear(); b._ice_hist_t = 0.0
    b._cur = None; b._cl = None; b._agg_seq = 0; b.orders.clear()
    b.bid_book.clear(); b.ask_book.clear(); b.best_bid = b.best_ask = None


def tr(price, size, buy, aggr=None, pas=None, start=True):
    b.handle_trades(None, "x", price, size, False, buy, start, True, aggr, pas)


def kinds():
    return [(e["k"], e["s"]) for e in b.events]


def settle(dt=1.0):
    T[0] += dt; b._detect_events(T[0])


def warm(price=1000, secs_=400, vol=2, seed=1):
    """先放一段平靜的、小量的、兩邊都有的成交,讓基準線有資料。"""
    rnd = random.Random(seed)
    for _ in range(secs_):
        tr(price + rnd.choice((0, 1)), vol, rnd.random() < 0.5); T[0] += 1.0

# ---- 掃單:一張攻擊單(同 aggressor id)連吃 4 個價位 ----
reset(); T[0] += 1000; warm(100)
for i in range(4):
    tr(100 + i, 6, True, aggr="A1", start=(i == 0)); T[0] += 0.05
settle()
assert ("sweep", "buy") in kinds() or ("stop", "buy") in kinds(), kinds()
# 只吃一個價位 → 不算
reset(); T[0] += 1000; tr(100, 30, True, aggr="A2"); settle(1.5); assert not b.events, kinds()
# 沒有 aggressor id、靠 is_execution_start 切批次:同一批 4 筆 → 一張攻擊單
reset(); T[0] += 1000
for i in range(4):
    tr(100 + i, 6, True, start=(i == 0)); T[0] += 0.05
settle(); assert b._cl is None and ("sweep", "buy") in kinds(), kinds()

# ---- 停損(推測):靜止一段後,5 張不同攻擊單 1.5 秒內往上衝穿過近期高點 ----
reset(); T[0] += 1000; warm(100)                            # 近期高點 101
for i in range(5):
    tr(101 + i, 8, True, aggr=f"S{i}"); T[0] += 0.3         # 101..105,40 口
settle()
assert ("stop", "buy") in kinds(), kinds()
ev = [e for e in b.events if e["k"] == "stop"][0]
assert ev["x"]["aggr"] == 5 and ev["x"]["brk"] >= 3 and ev["x"]["inf"] == 1, ev
# 往下跌破近期低點 → sell stop
reset(); T[0] += 1000; warm(100)
for i in range(5):
    tr(99 - i, 8, False, aggr=f"T{i}"); T[0] += 0.3
settle(); assert ("stop", "sell") in kinds(), kinds()
# 近期有更高的高點 110,這波只衝到 105 沒突破 → 不是停損
reset(); T[0] += 1000
for i in range(400):
    tr(90 + (i % 21), 2, i % 2 == 0); T[0] += 1.0
for i in range(5):
    tr(95 + i, 8, True, aggr=f"U{i}"); T[0] += 0.3
settle(); assert ("stop", "buy") not in kinds(), kinds()
# 只有一張攻擊單 → 是掃單不是停損
reset(); T[0] += 1000; warm(100)
for i in range(5):
    tr(101 + i, 8, True, aggr="ONE", start=(i == 0)); T[0] += 0.1
settle(); assert ("stop", "buy") not in kinds() and ("sweep", "buy") in kinds(), kinds()
# 資料不足 120 秒 → 不做停損推測
reset(); T[0] += 1000; tr(100, 2, True); T[0] += 10
for i in range(5):
    tr(101 + i, 8, True, aggr=f"V{i}"); T[0] += 0.3
settle(); assert ("stop", "buy") not in kinds(), kinds()

# ---- 吸收:要先有 5 分鐘基準;20 秒內賣方主動 200 口、價不動 → 買方承接 ----
reset(); T[0] += 1000
for i in range(25):
    tr(1000 + (i % 2) * 1, 8, False); T[0] += 0.8
b._detect_events(T[0]); assert ("absorb", "buy") not in kinds(), "基準不足不該發: %s" % kinds()
reset(); T[0] += 1000; warm(1000)
for i in range(25):
    tr(1000 + (i % 2) * 1, 8, False); T[0] += 0.8
b._detect_events(T[0]); assert ("absorb", "buy") in kinds(), kinds()
assert [e for e in b.events if e["k"] == "absorb"][0]["x"]["conf"] == 1
# 價格一路下殺 → 不是吸收
reset(); T[0] += 1000; warm(1000)
for i in range(25):
    tr(1000 - i, 8, False); T[0] += 0.8
b._detect_events(T[0]); assert ("absorb", "buy") not in kinds(), kinds()
# 賣方主動只佔一半 → 沒有單邊優勢
reset(); T[0] += 1000; warm(1000)
for i in range(25):
    tr(1000 + (i % 2), 8, False); tr(1000 + (i % 2), 8, True); T[0] += 0.8
b._detect_events(T[0]); assert ("absorb", "buy") not in kinds(), kinds()

# ---- 冰山(價位統計):買盤 100 價位看得見最多 5 口,被主動賣打掉 96 口(16 筆)且守住 ----
reset(); T[0] += 1000
b.handle_depth(None, "x", True, 100, 5); b.handle_depth(None, "x", False, 101, 5)
for i in range(16):
    tr(100, 6, False); T[0] += 2.0
b._detect_events(T[0])
assert ("iceberg", "buy") in kinds(), kinds()
e = [e for e in b.events if e["k"] == "iceberg"][0]; assert e["x"]["conf"] == 1, e      # 沒補單證據
# 看得見 100 口、被打掉 96 口 → 正常被吃
reset(); T[0] += 1000
b.handle_depth(None, "x", True, 100, 100); b.handle_depth(None, "x", False, 101, 5)
for i in range(16):
    tr(100, 6, False); T[0] += 2.0
b._detect_events(T[0]); assert ("iceberg", "buy") not in kinds(), kinds()
# 不知道看得見的掛量 → 不判冰山
reset(); T[0] += 1000
for i in range(16):
    tr(100, 6, False); T[0] += 2.0
b._detect_events(T[0]); assert ("iceberg", "buy") not in kinds(), kinds()
# 價位被打穿 → 不算守住
reset(); T[0] += 1000
b.handle_depth(None, "x", True, 100, 5); b.handle_depth(None, "x", False, 101, 5)
for i in range(16):
    tr(100, 6, False); T[0] += 2.0
b.handle_depth(None, "x", True, 100, 0); b.handle_depth(None, "x", True, 99, 5)
b._detect_events(T[0]); assert ("iceberg", "buy") not in kinds(), kinds()
# 補單證據:每次被打後 0.3 秒內同價位出現新掛單 → conf=2
reset(); T[0] += 1000
b.handle_depth(None, "x", True, 100, 5); b.handle_depth(None, "x", False, 101, 5)
for i in range(12):
    tr(100, 6, False); T[0] += 0.3
    b.handle_mbo(None, "x", "BID_NEW", f"N{i}", 100, 5); T[0] += 1.0
b._detect_events(T[0])
e = [e for e in b.events if e["k"] == "iceberg"]; assert e and e[0]["x"]["conf"] == 2 and e[0]["x"]["refill"] >= 2, b.events
# 委託 id 證據:同一張買單(id=ICE)顯示 5 口,被打掉 60 口且補量 → by=id
reset(); T[0] += 1000
b.handle_depth(None, "x", False, 101, 5)
b.handle_mbo(None, "x", "BID_NEW", "ICE", 100, 5)
b.handle_depth(None, "x", True, 100, 5)
for i in range(12):
    tr(100, 5, False, aggr=f"X{i}", pas="ICE"); T[0] += 0.4
    b.handle_mbo(None, "x", "REPLACE", "ICE", 100, 5); T[0] += 0.4
b._detect_events(T[0])
e = [e for e in b.events if e["k"] == "iceberg"]; assert e and e[0]["x"]["by"] == "id" and e[0]["x"]["conf"] == 2, b.events
# 吸收 + 同價附近有補單證據的冰山 → 吸收 conf=2
reset(); T[0] += 1000; warm(1000)
b.handle_depth(None, "x", True, 1000, 5); b.handle_depth(None, "x", False, 1002, 5)
for i in range(25):
    tr(1000, 8, False); T[0] += 0.8
    b.handle_mbo(None, "x", "BID_NEW", f"M{i}", 1000, 5); T[0] += 0.1
b._detect_events(T[0])
assert ("iceberg", "buy") in kinds(), kinds()
assert [e for e in b.events if e["k"] == "absorb"][0]["x"]["conf"] == 2, b.events

# ---- 力竭:買方猛推 +6 點,量遞減,最後 15 秒幾乎沒成交 ----
reset(); T[0] += 1000
for i in range(45):
    tr(2000 + int(i * 6 / 45), 4 if i < 15 else 3 if i < 30 else 1, True); T[0] += 1.0
T[0] += 15.0
b._detect_events(T[0])
assert ("exhaust", "buy") in kinds(), kinds()
# 推完還在持續買 → 沒力竭
reset(); T[0] += 1000
for i in range(60):
    tr(2000 + int(i * 8 / 60), 3, True); T[0] += 1.0
b._detect_events(T[0]); assert ("exhaust", "buy") not in kinds(), kinds()
# 量越推越大(後勁沒有遞減)→ 即使最後停了也不算力竭
reset(); T[0] += 1000
for i in range(45):
    tr(2000 + int(i * 6 / 45), 1 if i < 15 else 3 if i < 30 else 6, True); T[0] += 1.0
T[0] += 15.0; b._detect_events(T[0]); assert ("exhaust", "buy") not in kinds(), kinds()

# ---- 冷卻、清理、關閉 ----
reset(); T[0] += 1000
for _ in range(3):
    for i in range(4):
        tr(100 + i, 6, True, aggr="C"); T[0] += 0.05
    settle()
assert sum(1 for e in b.events if e["k"] in ("sweep", "stop")) == 1, kinds()
b._tick(); T[0] += 400; b._detect_events(T[0]); assert not b.events
b.EVENTS_ON = False; reset(); tr(100, 10, True); assert not b.secs
b.EVENTS_ON = True

# ---- 亂數行情誤報率:1 小時純隨機成交(小量、隨機漫步、隨機方向),不該一直冒事件 ----
for seed in (1, 2, 3, 7):
    reset(); T[0] += 1000
    rnd = random.Random(seed); px = 1000; seen = {}
    b.handle_depth(None, "x", True, px - 1, 20); b.handle_depth(None, "x", False, px + 1, 20)
    for sec in range(3600):
        for j in range(rnd.choice((0, 1, 1, 2, 3))):
            px += rnd.choice((-1, 0, 0, 1)) * (1 if rnd.random() < .5 else 0)
            tr(px, rnd.choice((1, 1, 2, 3, 5)), rnd.random() < 0.5, aggr=f"R{sec}{j}")
        T[0] += 1.0
        b._detect_events(T[0])
        for e in b.events:
            seen[e["id"]] = e["k"]
    cnt = {}
    for k in seen.values(): cnt[k] = cnt.get(k, 0) + 1
    print(f"亂數行情 seed={seed} 1 小時誤報:", cnt or "無")
    assert sum(cnt.values()) <= 6, cnt           # 純噪音 1 小時超過 6 個事件 → 門檻太鬆
print("orderflow events ok")
