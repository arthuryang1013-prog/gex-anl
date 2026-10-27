# -*- coding: utf-8 -*-
"""orderflow_bridge.py — 跑在 Bookmap 裡(Python addon,Windows 那台)。

把 NQ 的 MBO / 深度匯總成幾個小數字(Delta / 訂單簿不平衡 / 大單 / 掃單),
每秒 POST 一小包 JSON 到雲端 VPS 的 orderflow_receiver.py(port 8899)。

用法:
  1. 改下面 MAC_IP 成雲端 VPS 的公網 IP(167.99.66.102)。
  2. 把整份貼進 Bookmap 的程式編輯器(它裡面是 Python),按 Build。
  3. Settings → Configure addons → 加入 Build 出來的 JAR。
"""
import bookmap as bm
import json
import threading
import time
import urllib.request
from collections import deque

# ===== 設定區(要改的只有 MAC_IP) =====
MAC_IP = "167.99.66.102"   # ← 雲端 VPS 公網 IP
MAC_PORT = 8899
TOKEN = ""                 # ← 填跟 VPS config.json 的 orderflow_token 同一串(留空 = 不帶 token)
INGEST_URL = ""            # ← 之後若改走 https(例如 Caddy 轉發),填完整網址;留空 = 用上面的 IP:埠
SEND_EVERY = 1.0           # 每秒送一次
WINDOW_SEC = 60.0          # 統計窗口:數字 = 最近 60 秒的累計
TOP_N = 10                 # 訂單簿不平衡取前 10 檔
BIG_MIN = 20               # 單筆 >= 20 口算大單
SWEEP_GAP = 0.5            # 同方向連續攻擊單間隔 <= 0.5 秒算掃單
BOOK_RANGE_PTS = 100.0     # 掛單簿範圍:現價 ± 100 點
BOOK_MAX_LEVELS = 120      # 單邊最多送幾檔(防封包過大)
TRADE_WINDOW_SEC = 60.0    # 成交熱區統計窗口(近 60 秒)
NEAR_PTS = 5.0             # 「靠近成交價」的範圍:離最佳買/賣價 5 點內的掛單,才算撤單/加單事件
ORDER_CAP = 200000         # 追蹤中的委託上限(防記憶體無限成長)
# ================================

_lock = threading.Lock()
_pips = 0.25
_size_mult = 1.0

best_bid = None            # 最高買價(int,×pips)
best_ask = None            # 最低賣價(int,×pips)
bid_book = {}              # 買盤: 價 -> 量
ask_book = {}              # 賣盤: 價 -> 量

delta_buy = 0.0            # 累計主動買量(口,單調遞增)
delta_sell = 0.0           # 累計主動賣量(口)
big_buy = 0.0              # 大單主動買量
big_sell = 0.0             # 大單主動賣量
big_vol = 0.0              # 大單總量
sweep_buy = 0              # 掃單次數(買方)
sweep_sell = 0             # 掃單次數(賣方)
last_agg_side = None       # 上一次攻擊單方向 'b' / 's'
last_agg_time = 0.0
last_event_time = 0.0      # 最後一筆事件時間(算停頓秒數)

# --- v2 新增 ---
tr_buy = 0.0               # 由「實際成交」(trades 事件)算的主動買量,累計
tr_sell = 0.0              # 由「實際成交」算的主動賣量,累計
pull_bid = 0.0             # 買盤近價撤掉的量(撤單 + 縮量),累計
pull_ask = 0.0
stack_bid = 0.0            # 買盤近價新增的量(新掛 + 加量),累計
stack_ask = 0.0
orders = {}                # order_id -> [is_bid, 價(int), 量(口)];用來看 CANCEL/REPLACE 動了多少

samples = deque()          # 每秒快照,用來算「近 60 秒」窗口
trade_log = deque()        # 近 60 秒的成交紀錄:(時間, 買/賣, 價, 口數)


def handle_subscribe_instrument(addon, alias, full_name, is_crypto, pips, size_multiplier, instrument_multiplier, supported_features):
    global _pips, _size_mult
    _pips = pips or 0.25
    _size_mult = size_multiplier or 1.0
    print(f"[orderflow_bridge] 訂閱 {full_name} pips={_pips} size_mult={_size_mult}", flush=True)
    bm.subscribe_to_depth(addon, alias, 1)
    bm.subscribe_to_mbo(addon, alias, 2)
    bm.subscribe_to_trades(addon, alias, 3)


def handle_unsubscribe_instrument(addon, alias):
    print("[orderflow_bridge] 取消訂閱", flush=True)


def _on_aggressor(side, size_cts, price_real=None):
    """記錄一筆攻擊單(跨越價差的市價單),順便判斷是不是大單/掃單,並記下成交價(算成交熱區)。"""
    global delta_buy, delta_sell, big_buy, big_sell, big_vol, sweep_buy, sweep_sell, last_agg_side, last_agg_time
    now = time.time()
    if side == "b":
        delta_buy += size_cts
        if size_cts >= BIG_MIN:
            big_buy += size_cts
            big_vol += size_cts
    else:
        delta_sell += size_cts
        if size_cts >= BIG_MIN:
            big_sell += size_cts
            big_vol += size_cts
    if last_agg_side == side and (now - last_agg_time) <= SWEEP_GAP:
        if side == "b":
            sweep_buy += 1
        else:
            sweep_sell += 1
    last_agg_side = side
    last_agg_time = now
    if price_real is not None:
        trade_log.append((now, side, price_real, size_cts))


def _near(is_bid, price_level):
    """這個價位是否離該側最佳價 NEAR_PTS 點內(買盤看 best_bid,賣盤看 best_ask)。"""
    ref = best_bid if is_bid else best_ask
    if ref is None:
        return False
    return abs(price_level - ref) * _pips <= NEAR_PTS


def _track(kind, is_bid, price_level, size_cts):
    """kind: 'pull' 或 'stack'。只記近價事件。"""
    global pull_bid, pull_ask, stack_bid, stack_ask
    if size_cts <= 0 or not _near(is_bid, price_level):
        return
    if kind == "pull":
        if is_bid:
            pull_bid += size_cts
        else:
            pull_ask += size_cts
    else:
        if is_bid:
            stack_bid += size_cts
        else:
            stack_ask += size_cts


def handle_mbo(addon, alias, event_type, order_id, price_level, size_level):
    global last_event_time
    with _lock:
        last_event_time = time.time()
        try:
            size_cts = float(size_level) / _size_mult
        except Exception:
            size_cts = float(size_level)
        if event_type == "CANCEL":
            o = orders.pop(order_id, None)
            if o is not None:
                # 注意:被成交吃掉的委託也可能以 CANCEL 出現,無法與主動撤單區分(見 README/說明)
                _track("pull", o[0], o[1], o[2])
            return
        if event_type == "REPLACE":
            o = orders.get(order_id)
            if o is not None:
                if price_level != o[1]:
                    # 改價 = 舊位置撤掉、新位置加上
                    _track("pull", o[0], o[1], o[2])
                    _track("stack", o[0], price_level, size_cts)
                elif size_cts < o[2]:
                    _track("pull", o[0], o[1], o[2] - size_cts)
                elif size_cts > o[2]:
                    _track("stack", o[0], o[1], size_cts - o[2])
                o[1] = price_level
                o[2] = size_cts
            return
        if event_type == "ASK_NEW":
            # 有人把賣單掛在買價或更低 → 主動賣(打買盤)
            if best_bid is not None and price_level <= best_bid:
                _on_aggressor("s", size_cts, price_level * _pips)
            else:
                _remember(order_id, False, price_level, size_cts)
        elif event_type == "BID_NEW":
            # 有人把買單掛在賣價或更高 → 主動買(打賣盤)
            if best_ask is not None and price_level >= best_ask:
                _on_aggressor("b", size_cts, price_level * _pips)
            else:
                _remember(order_id, True, price_level, size_cts)


def _remember(order_id, is_bid, price_level, size_cts):
    """記下掛在簿上的委託;近價新掛算「加單」。"""
    if len(orders) >= ORDER_CAP:
        try:
            orders.pop(next(iter(orders)))   # 丟最舊的(dict 保持插入順序)
        except Exception:
            orders.clear()
    orders[order_id] = [is_bid, price_level, size_cts]
    _track("stack", is_bid, price_level, size_cts)


def handle_trades(addon, alias, price_level, size_level, is_otc, is_bid, is_execution_start,
                  is_execution_end, aggressor_order_id=None, passive_order_id=None):
    """實際成交。is_bid=True 代表買方主動(依官方文件);用來跟 MBO 推判的 Delta 對照。"""
    global tr_buy, tr_sell
    with _lock:
        try:
            sz = float(size_level) / _size_mult
        except Exception:
            sz = float(size_level)
        if is_bid:
            tr_buy += sz
        else:
            tr_sell += sz


def handle_depth(addon, alias, is_bid, price_level, size_level):
    global best_bid, best_ask, last_event_time
    with _lock:
        last_event_time = time.time()
        if is_bid:
            if size_level > 0:
                bid_book[price_level] = size_level
                if best_bid is None or price_level > best_bid:
                    best_bid = price_level
            else:
                bid_book.pop(price_level, None)
                if price_level == best_bid:
                    best_bid = max(bid_book) if bid_book else None
        else:
            if size_level > 0:
                ask_book[price_level] = size_level
                if best_ask is None or price_level < best_ask:
                    best_ask = price_level
            else:
                ask_book.pop(price_level, None)
                if price_level == best_ask:
                    best_ask = min(ask_book) if ask_book else None


def _imb(bids, asks):
    b = sum(bids)
    a = sum(asks)
    if b + a <= 0:
        return 0.0
    return (b - a) / float(b + a)


def _top_imbalance():
    """離現價最近的 TOP_N 檔的訂單簿不平衡,範圍 [-1,1];正 = 買方掛單厚。"""
    bp = sorted(bid_book, reverse=True)[:TOP_N]     # 最高的買價 = 最靠近現價
    ap = sorted(ask_book)[:TOP_N]                   # 最低的賣價 = 最靠近現價
    return _imb([bid_book[p] for p in bp], [ask_book[p] for p in ap])


def _top_imbalance_big():
    """舊算法(量最大的 N 檔,不管離現價多遠),保留作為對照欄位 imb_big。"""
    return _imb(sorted(bid_book.values(), reverse=True)[:TOP_N],
                sorted(ask_book.values(), reverse=True)[:TOP_N])


def _book_snapshot():
    """現價 ±BOOK_RANGE_PTS 點的掛單簿(買賣各一列 [價格, 口數]),近的排前面。"""
    bids, asks = [], []
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
        rng = int(round(BOOK_RANGE_PTS / _pips))
        for p in sorted(bid_book, reverse=True):
            if p < mid - rng:
                break
            s = bid_book[p]
            if s > 0:
                bids.append([round(p * _pips, 2), round(float(s) / _size_mult, 1)])
                if len(bids) >= BOOK_MAX_LEVELS:
                    break
        for p in sorted(ask_book):
            if p > mid + rng:
                break
            s = ask_book[p]
            if s > 0:
                asks.append([round(p * _pips, 2), round(float(s) / _size_mult, 1)])
                if len(asks) >= BOOK_MAX_LEVELS:
                    break
    return bids, asks


def _trade_snapshot(now):
    """近 TRADE_WINDOW_SEC 秒內,成交熱區(每個價位的買/賣主動量),量大的排前面。"""
    while trade_log and trade_log[0][0] < now - TRADE_WINDOW_SEC:
        trade_log.popleft()
    buy, sell = {}, {}
    for (_t, side, p, sz) in trade_log:
        key = round(p, 2)
        d = buy if side == "b" else sell
        d[key] = d.get(key, 0.0) + sz
    rows = []
    for p in set(buy) | set(sell):
        bv = buy.get(p, 0.0)
        sv = sell.get(p, 0.0)
        rows.append([round(p, 2), round(bv, 1), round(sv, 1)])
    rows.sort(key=lambda r: -(r[1] + r[2]))
    return rows[:16]


def _tick():
    now = time.time()
    with _lock:
        snap = dict(t=now, db=delta_buy, ds=delta_sell, bb=big_buy, bs=big_sell,
                    bv=big_vol, sb=sweep_buy, ss=sweep_sell,
                    tb=tr_buy, ts=tr_sell, pb=pull_bid, pa=pull_ask, kb=stack_bid, ka=stack_ask)
        idle = (now - last_event_time) if last_event_time else 0.0
        imb = _top_imbalance()
        imb_big = _top_imbalance_big()
        spot = None
        if best_bid is not None and best_ask is not None:
            spot = (best_bid + best_ask) / 2.0 * _pips
        book_bid, book_ask = _book_snapshot()
        trades = _trade_snapshot(now)
    samples.append(snap)
    while samples and samples[0]["t"] < now - WINDOW_SEC:
        samples.popleft()
    base = samples[0] if samples else snap
    pkt = {
        "ts": now,
        "idle_sec": round(idle, 2),
        "lag_sec": round(idle, 2),   # 事件停頓秒數(不是真的市場延遲;Bookmap 卡頓我們測不到)
        "delta": round((snap["db"] - base["db"]) - (snap["ds"] - base["ds"]), 1),
        "delta_buy": round(snap["db"] - base["db"], 1),
        "delta_sell": round(snap["ds"] - base["ds"], 1),
        "vol": round((snap["db"] - base["db"]) + (snap["ds"] - base["ds"]), 1),
        "imb": round(imb, 4),
        "imb_big": round(imb_big, 4),
        # v2:實際成交算的 Delta(跟 delta 對照)、累計 CVD、近價撤單/加單量
        "delta_tr": round((snap["tb"] - base["tb"]) - (snap["ts"] - base["ts"]), 1),
        "vol_tr": round((snap["tb"] - base["tb"]) + (snap["ts"] - base["ts"]), 1),
        "cvd_tr": round(snap["tb"] - snap["ts"], 1),
        "cvd_mbo": round(snap["db"] - snap["ds"], 1),
        "pull_bid": round(snap["pb"] - base["pb"], 1),
        "pull_ask": round(snap["pa"] - base["pa"], 1),
        "stack_bid": round(snap["kb"] - base["kb"], 1),
        "stack_ask": round(snap["ka"] - base["ka"], 1),
        "big": round((snap["bb"] - base["bb"]) - (snap["bs"] - base["bs"]), 1),
        "big_vol": round(snap["bv"] - base["bv"], 1),
        "sweep_buy": int(snap["sb"] - base["sb"]),
        "sweep_sell": int(snap["ss"] - base["ss"]),
        "spot": spot,
        "book_bid": book_bid,
        "book_ask": book_ask,
        "trades": trades,
    }
    _post(pkt)


def _post(pkt):
    try:
        data = json.dumps(pkt).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if TOKEN:
            headers["Authorization"] = "Bearer " + TOKEN
        req = urllib.request.Request(
            INGEST_URL or f"http://{MAC_IP}:{MAC_PORT}/", data=data, headers=headers)
        urllib.request.urlopen(req, timeout=1.5)
    except Exception:
        pass  # Mac 沒開或網路不通就跳過,下秒再試


def _bg_loop():
    while True:
        time.sleep(SEND_EVERY)
        try:
            _tick()
        except Exception:
            pass


if __name__ == "__main__":
    addon = bm.create_addon()
    bm.add_depth_handler(addon, handle_depth)
    bm.add_mbo_handler(addon, handle_mbo)
    bm.add_trades_handler(addon, handle_trades)
    bm.start_addon(addon, handle_subscribe_instrument, handle_unsubscribe_instrument)
    threading.Thread(target=_bg_loop, daemon=True).start()
    bm.wait_until_addon_is_turned_off(addon)
