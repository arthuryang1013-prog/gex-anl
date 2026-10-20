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
SEND_EVERY = 1.0           # 每秒送一次
WINDOW_SEC = 60.0          # 統計窗口:數字 = 最近 60 秒的累計
TOP_N = 10                 # 訂單簿不平衡取前 10 檔
BIG_MIN = 20               # 單筆 >= 20 口算大單
SWEEP_GAP = 0.5            # 同方向連續攻擊單間隔 <= 0.5 秒算掃單
BOOK_RANGE_PTS = 100.0     # 掛單簿範圍:現價 ± 100 點
BOOK_MAX_LEVELS = 120      # 單邊最多送幾檔(防封包過大)
TRADE_WINDOW_SEC = 60.0    # 成交熱區統計窗口(近 60 秒)
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

samples = deque()          # 每秒快照,用來算「近 60 秒」窗口
trade_log = deque()        # 近 60 秒的成交紀錄:(時間, 買/賣, 價, 口數)


def handle_subscribe_instrument(addon, alias, full_name, is_crypto, pips, size_multiplier, instrument_multiplier, supported_features):
    global _pips, _size_mult
    _pips = pips or 0.25
    _size_mult = size_multiplier or 1.0
    print(f"[orderflow_bridge] 訂閱 {full_name} pips={_pips} size_mult={_size_mult}", flush=True)
    bm.subscribe_to_depth(addon, alias, 1)
    bm.subscribe_to_mbo(addon, alias, 2)


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


def handle_mbo(addon, alias, event_type, order_id, price_level, size_level):
    global last_event_time
    with _lock:
        last_event_time = time.time()
        if event_type == "REPLACE" or event_type == "CANCEL":
            return
        try:
            size_cts = float(size_level) / _size_mult
        except Exception:
            size_cts = float(size_level)
        if event_type == "ASK_NEW":
            # 有人把賣單掛在買價或更低 → 主動賣(打買盤)
            if best_bid is not None and price_level <= best_bid:
                _on_aggressor("s", size_cts, price_level * _pips)
        elif event_type == "BID_NEW":
            # 有人把買單掛在賣價或更高 → 主動買(打賣盤)
            if best_ask is not None and price_level >= best_ask:
                _on_aggressor("b", size_cts, price_level * _pips)


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


def _top_imbalance():
    """前 TOP_N 檔的訂單簿不平衡,範圍 [-1,1];正 = 買方掛單厚。"""
    bids = sorted(bid_book.values(), reverse=True)[:TOP_N]
    asks = sorted(ask_book.values(), reverse=True)[:TOP_N]
    b = sum(bids)
    a = sum(asks)
    if b + a <= 0:
        return 0.0
    return (b - a) / float(b + a)


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
                    bv=big_vol, sb=sweep_buy, ss=sweep_sell)
        idle = (now - last_event_time) if last_event_time else 0.0
        imb = _top_imbalance()
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
        req = urllib.request.Request(
            f"http://{MAC_IP}:{MAC_PORT}/", data=data,
            headers={"Content-Type": "application/json"})
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
    bm.start_addon(addon, handle_subscribe_instrument, handle_unsubscribe_instrument)
    threading.Thread(target=_bg_loop, daemon=True).start()
    bm.wait_until_addon_is_turned_off(addon)
