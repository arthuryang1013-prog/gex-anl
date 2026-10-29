# -*- coding: utf-8 -*-
"""orderflow_bridge.py — 跑在 Bookmap 裡(Python addon,Windows 那台)。

把 NQ 的 MBO / 深度匯總成幾個小數字(Delta / 訂單簿不平衡 / 大單 / 掃單),
每秒 POST 一小包 JSON 到雲端 VPS 的 orderflow_receiver.py(port 8899)。
另外偵測「吸收 / 冰山 / 力竭 / 掃單」事件(封包的 events 欄位;門檻在設定區,EVENTS_ON=False 可整個關掉)。

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
SWEEP_GAP = 0.5            # 同方向連續攻擊單間隔 <= 0.5 秒算掃單(也用在事件偵測)
BOOK_RANGE_PTS = 100.0     # 掛單簿範圍:現價 ± 100 點
BOOK_MAX_LEVELS = 120      # 單邊最多送幾檔(防封包過大)
TRADE_WINDOW_SEC = 60.0    # 成交熱區統計窗口(近 60 秒)
NEAR_PTS = 5.0             # 「靠近成交價」的範圍:離最佳買/賣價 5 點內的掛單,才算撤單/加單事件
ORDER_CAP = 200000         # 追蹤中的委託上限(防記憶體無限成長)
# --- 訂單流事件偵測(掃單 / 停損 / 冰山 / 吸收 / 力竭)。概念仿 Bookmap「Stops & Icebergs」與 ATAS MBO 指標,
#     但兩家都沒公開精確演算法與預設數值,所以下面的門檻是我們自己的初始值,還沒用真資料調過,偵測結果只當線索 ---
EVENTS_ON = True           # 設 False 就完全不偵測、不送 events
EVT_KEEP_SEC = 150.0       # 每包帶最近這麼多秒內偵測到的事件(接收端會去重)
SWEEP_MIN_LEVELS = 3       # 掃單:這一波至少吃穿幾個價位
SWEEP_MIN_VOL = 15         # 掃單:這一波至少幾口
SWEEP_MAX_SEC = 3.0        # 掃單:整波不超過幾秒
STOP_MIN_AGGR = 3          # 停損推測:這一波至少由幾張不同攻擊單組成(停損觸發會同時湧出多張市價單)
STOP_MIN_VOL = 25          # 停損推測:至少幾口
STOP_MIN_PTS = 2.0         # 停損推測:至少穿過幾點
STOP_MAX_SEC = 2.5         # 停損推測:整波不超過幾秒(很快)
STOP_LOOKBACK = 300        # 停損推測:往前看多少秒找「近期高/低點」,需突破它才算(停損通常擺在高低點外面)
ICE_WIN = 60               # 冰山:觀察窗口(秒)
ICE_MIN_VOL = 40           # 冰山:同價位被打掉至少幾口(下限;實際門檻 = max(它, 近 10 分鐘各價位成交量平均 + ICE_SD_K 個標準差))
ICE_SD_K = 2.0
ICE_FACTOR = 3.0           # 冰山:且成交量 >= 該價位看得見的最大掛量的幾倍
ICE_MIN_TRADES = 4         # 冰山:至少幾筆成交
ICE_REFILL_SEC = 1.0       # 冰山:成交後這麼多秒內同價位又出現新掛單 = 一次「補單」
ICE_MIN_REFILL = 2         # 冰山:補單次數 >= 這個值 → 可信度 2(有補單證據);沒有補單證據的要 1.5 倍門檻,可信度 1
ABS_WIN = 20               # 吸收:觀察窗口(秒)
ABS_MIN_VOL = 120          # 吸收:窗口內單邊主動量至少幾口
ABS_SD_K = 2.0             # 吸收:且要高過過去 10 分鐘「各窗口單邊主動量」平均 + 幾個標準差
ABS_DOM = 0.65             # 吸收:該方向要佔窗口總成交量的幾成以上
ABS_MOVE_PTS = 1.0         # 吸收:價格往攻擊方向最多只動這麼多點(動不了 = 被吸收)
ABS_RANGE_PTS = 3.0        # 吸收:窗口內成交價範圍不超過這麼多點
EXH_PUSH_VOL = 80          # 力竭:前一段推進(60~15 秒前)的淨主動量至少幾口
EXH_RISE_PTS = 4.0         # 力竭:前一段價格推進至少幾點
EXH_TAIL_RATIO = 0.4       # 力竭:最近 15 秒的主動量(換算成同時間長度)降到推進段平均的幾成以下
EXH_LEG_RATIO = 0.7        # 力竭:推進段最後三分之一的量要小於最前三分之一的這個比例(後勁遞減)
EVT_COOLDOWN = {"sweep": 3.0, "stop": 10.0, "absorb": 45.0, "iceberg": 90.0, "exhaust": 60.0}   # 同類事件至少隔幾秒
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

events = deque()           # 偵測到的事件:dict(id, t, k, s, p, v, x)
secs = deque()             # 每秒彙總的成交:[秒, 買量, 賣量, 首價, 末價, 高, 低, 價×量合計](約 12 分鐘)
lvl_trades = {}            # (被動方是買盤?, 價位int) -> deque[(時間, 口數)]:冰山用
lvl_vis = {}               # (是買盤?, 價位int) -> [看得見的最大掛量, 時間]
lvl_last_tr = {}           # (被動方是買盤?, 價位int) -> 最近一次成交時間
lvl_refill = {}            # (是買盤?, 價位int) -> deque[補單時間]:成交後立刻又在同價位出現新掛單
ord_stat = {}              # 被動委託 id -> [是買盤, 價位, 起始量, 最大量, 已成交量, 成交筆數, 首次成交, 最近成交, 補量次數]
_ice_cand = set()          # 最近有成交的被動委託 id(只掃這些)
_ice_hist = deque()        # (時間, 某價位 60 秒成交量):算冰山自適應門檻
_ice_hist_t = 0.0
_cur = None                # 目前這張攻擊單(同一 aggressor id 或同一批成交)
_agg_seq = 0               # 沒有 aggressor id 時,用 is_execution_start 切批次
_cl = None                 # 目前這一波同方向攻擊單(掃單/停損用)
_last_evt = {}             # 事件冷卻:(kind, side/價位) -> 時間
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
                if size_cts > o[2]:
                    try:
                        _note_resize(order_id, size_cts, time.time())
                    except Exception:
                        pass
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
    try:
        _note_new_order(order_id, is_bid, price_level, size_cts, time.time())
    except Exception:
        pass


# =========================== 訂單流事件偵測 ===========================
# 全部是用成交(trades)與掛單簿(MBO)推出來的「線索」,不是事實,也沒驗證過有預測力。概念參考 Bookmap / ATAS 的公開說明:
#   sweep   掃單:同方向的攻擊單在極短時間內吃穿多個價位(單張攻擊單跨價位,或連續幾張)
#   stop    停損(推測):掃單的特例——由多張攻擊單同時湧出、很快、而且突破近期高/低點。
#           Bookmap/ATAS 說是用 MBO 把「停損單觸發的成交」分出來,但 Python API 沒給這個旗標,所以這裡只能用上面的特徵推測。
#   iceberg 冰山:同一價位被打掉的量遠超過看得見的掛量。x.conf=2 表示有補單證據(同一張委託被打掉量遠超它自己的顯示量,
#           或成交後立刻在同價位又出現新掛單);x.conf=1 表示只有成交量證據。
#   absorb  吸收:一邊大量主動成交(高過近 10 分鐘的平均 + 標準差、佔窗口 65% 以上),價格卻推不動。x.conf=2 = 同價附近同時有冰山證據。
#   exhaust 力竭:一波推進後勁遞減(最後三分之一的量小於最前三分之一),最近 15 秒量萎縮、不再創新高/新低。
# side:sweep/stop/exhaust 以「主動方」表示('buy' 買方主動 / 'sell' 賣方主動);
#       absorb/iceberg 以「承接方」表示('buy' 買方在承接 / 'sell' 賣方在承接)。
# 門檻是我們自己的初始值(Bookmap/ATAS 都沒公開精確演算法或預設值),要用真資料看過再調。


def _emit(kind, side, price, vol, x=None, key=None, now=None):
    """記錄一個事件;同類事件有冷卻時間,避免連發。"""
    now = now or time.time()
    ck = (kind, key if key is not None else side)
    if now - _last_evt.get(ck, 0.0) < EVT_COOLDOWN.get(kind, 30.0):
        return False
    _last_evt[ck] = now
    ev = {"id": f"{int(now * 10)}-{kind}-{side}-{int(round(price * 4))}", "t": round(now, 2), "k": kind, "s": side,
          "p": round(float(price), 2), "v": int(round(vol))}
    if x:
        ev["x"] = x
    events.append(ev)
    return True


def _sec_add(now, side, price, sz):
    sec = int(now)
    if not secs or secs[-1][0] != sec:
        secs.append([sec, 0.0, 0.0, price, price, price, price, 0.0])
    b = secs[-1]
    if side == "b":
        b[1] += sz
    else:
        b[2] += sz
    b[4] = price
    b[5] = max(b[5], price)
    b[6] = min(b[6], price)
    b[7] += price * sz
    while secs and secs[0][0] < sec - 720:
        secs.popleft()


def _prior_extreme(t0, buy):
    """t0 之前 STOP_LOOKBACK 秒(不含最後 1 秒)的最高/最低成交價;資料不到 120 秒回 None。"""
    rows = [r for r in secs if t0 - STOP_LOOKBACK <= r[0] < t0 - 1]
    if not rows or rows[-1][0] - rows[0][0] < 120:
        return None
    return max(r[5] for r in rows) if buy else min(r[6] for r in rows)


def _cluster_end():
    """結算一波同方向攻擊單:先判斷是不是停損(推測),不是再看是不是掃單。"""
    global _cl
    c = _cl
    _cl = None
    if not c:
        return
    buy = c["side"] == "b"
    dur = c["t1"] - c["t0"]
    nlv = len(c["lv"])
    rng = c["hi"] - c["lo"]
    side = "buy" if buy else "sell"
    if nlv < SWEEP_MIN_LEVELS or c["vol"] < SWEEP_MIN_VOL or dur > SWEEP_MAX_SEC:
        return
    brk = None
    if c["aggr"] >= STOP_MIN_AGGR and c["vol"] >= STOP_MIN_VOL and rng >= STOP_MIN_PTS and dur <= STOP_MAX_SEC:
        ext = _prior_extreme(c["t0"], buy)
        if ext is not None:
            brk = (c["hi"] - ext) if buy else (ext - c["lo"])
            # 起點要在近期高/低點附近(不是離很遠才衝到),而且確實突破它
            near = (ext - c["p0"]) if buy else (c["p0"] - ext)
            if brk > 0 and near <= 2.0:
                _emit("stop", side, c["last"], c["vol"],
                      {"aggr": c["aggr"], "lv": nlv, "p0": round(c["p0"], 2), "brk": round(brk, 2), "inf": 1},
                      key=c["side"], now=c["t1"])
                return
    _emit("sweep", side, c["last"], c["vol"],
          {"aggr": c["aggr"], "lv": nlv, "p0": round(c["p0"], 2)}, key=c["side"], now=c["t1"])


def _exec_end():
    """一張攻擊單結束:併進目前這一波,或另起一波。"""
    global _cur, _cl
    e = _cur
    _cur = None
    if not e:
        return
    if _cl and (_cl["side"] != e["side"] or e["t0"] - _cl["t1"] > SWEEP_GAP):
        _cluster_end()
    if not _cl:
        _cl = {"side": e["side"], "t0": e["t0"], "t1": e["t1"], "p0": e["p0"], "last": e["last"], "vol": 0.0,
               "lv": set(), "aggr": 0, "hi": e["hi"], "lo": e["lo"]}
    _cl["t1"] = e["t1"]
    _cl["last"] = e["last"]
    _cl["vol"] += e["vol"]
    _cl["lv"] |= e["lv"]
    _cl["aggr"] += 1
    _cl["hi"] = max(_cl["hi"], e["hi"])
    _cl["lo"] = min(_cl["lo"], e["lo"])


def _on_trade_event(now, side, price_level, price_real, sz, is_bid_aggr, start=False, aggr_id=None, pass_id=None):
    """handle_trades 呼叫:更新每秒彙總、攻擊單/掃單波段、冰山的同價位與委託累計。"""
    global _cur, _agg_seq
    if not EVENTS_ON:
        return
    _sec_add(now, side, price_real, sz)
    # --- 攻擊單分組:有 aggressor id 就用它;沒有就用 is_execution_start 切批次 ---
    if aggr_id is not None:
        key = ("id", str(aggr_id))
    else:
        if start or _cur is None:
            _agg_seq += 1
        key = ("seq", _agg_seq)
    if _cur and (_cur["key"] != key or _cur["side"] != side or now - _cur["t1"] > SWEEP_GAP):
        _exec_end()
    if not _cur:
        _cur = {"key": key, "side": side, "t0": now, "t1": now, "p0": price_real, "last": price_real,
                "vol": 0.0, "lv": set(), "hi": price_real, "lo": price_real}
    _cur["t1"] = now
    _cur["last"] = price_real
    _cur["vol"] += sz
    _cur["lv"].add(price_level)
    _cur["hi"] = max(_cur["hi"], price_real)
    _cur["lo"] = min(_cur["lo"], price_real)
    # --- 冰山:主動買打的是賣盤(被動方 = 賣),主動賣打的是買盤(被動方 = 買) ---
    lk = (not is_bid_aggr, price_level)
    dq = lvl_trades.get(lk)
    if dq is None:
        if len(lvl_trades) > 4000:
            lvl_trades.clear()
        dq = lvl_trades[lk] = deque()
    dq.append((now, sz))
    lvl_last_tr[lk] = now
    if len(lvl_last_tr) > 8000:
        lvl_last_tr.clear()
    if pass_id is not None:
        pid = str(pass_id)
        st = ord_stat.get(pid)
        if st is not None:
            st[4] += sz
            st[5] += 1
            if st[6] is None:
                st[6] = now
            st[7] = now
            _ice_cand.add(pid)


def _note_new_order(order_id, is_bid, price_level, size_cts, now):
    """handle_mbo 呼叫:新掛的被動單。記起始量;若剛在同價位成交過,算一次「補單」。"""
    if not EVENTS_ON:
        return
    pid = str(order_id)
    if len(ord_stat) > 60000:
        for k in list(ord_stat)[:20000]:
            ord_stat.pop(k, None)
            _ice_cand.discard(k)
    ord_stat[pid] = [is_bid, price_level, size_cts, size_cts, 0.0, 0, None, None, 0]
    k = (is_bid, price_level)
    lt = lvl_last_tr.get(k)
    if lt is not None and now - lt <= ICE_REFILL_SEC:
        dq = lvl_refill.get(k)
        if dq is None:
            if len(lvl_refill) > 4000:
                lvl_refill.clear()
            dq = lvl_refill[k] = deque()
        dq.append(now)


def _note_resize(order_id, new_size, now):
    """handle_mbo 呼叫:委託改量。成交過之後量又變大 = 這張單補量(顯示量重新補滿)。"""
    if not EVENTS_ON:
        return
    st = ord_stat.get(str(order_id))
    if st is None:
        return
    if st[5] > 0 and new_size > st[3] * 0.5 and st[7] is not None and now - st[7] <= 5.0:
        st[8] += 1
    st[3] = max(st[3], new_size)


def _win(now, a, b):
    """每秒彙總裡 [now-a, now-b) 這段:回傳 (買量, 賣量, 首價, 末價, 高, 低, 價量合計);沒有成交回 None。"""
    rows = [r for r in secs if now - a <= r[0] < now - b]
    if not rows:
        return None
    return (sum(r[1] for r in rows), sum(r[2] for r in rows), rows[0][3], rows[-1][4],
            max(r[5] for r in rows), min(r[6] for r in rows), sum(r[7] for r in rows))


def _abs_threshold(now, side_idx):
    """吸收門檻:過去 10 分鐘(不含最近 ABS_WIN 秒)各個 ABS_WIN 秒窗口的單邊主動量,平均 + ABS_SD_K 個標準差;
    資料不到 5 分鐘回 None(這時只用 ABS_MIN_VOL 的下限,並且不發吸收)。"""
    rows = [r for r in secs if now - 620 <= r[0] < now - ABS_WIN]
    if len(rows) < 150:
        return None
    t_lo = rows[0][0] // ABS_WIN
    t_hi = rows[-1][0] // ABS_WIN
    n = int(t_hi - t_lo + 1)
    if n < 8:
        return None
    sums = [0.0] * n
    for r in rows:
        sums[int(r[0] // ABS_WIN - t_lo)] += r[side_idx]
    m = sum(sums) / n
    sd = (sum((x - m) ** 2 for x in sums) / n) ** 0.5
    return max(ABS_MIN_VOL, m + ABS_SD_K * sd)


def _detect_absorb(now):
    w = _win(now, ABS_WIN, 0)
    if not w:
        return
    bv, sv, p_first, p_last, hi, lo, pv = w
    if hi - lo > ABS_RANGE_PTS:
        return
    tot = bv + sv
    for idx, vol, side_aggr in ((2, sv, "s"), (1, bv, "b")):
        thr = _abs_threshold(now, idx)
        if thr is None or vol < thr or vol < ABS_DOM * tot:
            continue
        moved = (p_last - p_first) if side_aggr == "b" else (p_first - p_last)     # 往攻擊方向走了幾點
        if moved > ABS_MOVE_PTS:
            continue
        vwap = pv / tot if tot else p_last
        # 賣方主動被吸收 = 買方在承接('buy');買方主動被吸收 = 賣方在承接('sell')
        side = "buy" if side_aggr == "s" else "sell"
        conf = 1
        for e in events:
            if e["k"] == "iceberg" and (e.get("x") or {}).get("conf") == 2 and e["s"] == side and now - e["t"] <= ABS_WIN + 10 and abs(e["p"] - vwap) <= 1.5:
                conf = 2
                break
        _emit("absorb", side, vwap, vol,
              {"win": ABS_WIN, "moved": round(moved, 2), "thr": round(thr, 1), "conf": conf}, now=now)


def _ice_threshold(now):
    """冰山成交量門檻:每分鐘取一次「各價位 60 秒成交量」樣本,近 10 分鐘 平均 + ICE_SD_K×標準差;樣本不足就用 1.5×下限。"""
    global _ice_hist_t
    if now - _ice_hist_t >= 60:
        _ice_hist_t = now
        for k, dq in lvl_trades.items():
            v = sum(x[1] for x in dq if x[0] >= now - ICE_WIN)
            if v > 0:
                _ice_hist.append((now, v))
    while _ice_hist and _ice_hist[0][0] < now - 600:
        _ice_hist.popleft()
    if len(_ice_hist) < 30:
        return ICE_MIN_VOL * 1.5
    vals = [x[1] for x in _ice_hist]
    m = sum(vals) / len(vals)
    sd = (sum((x - m) ** 2 for x in vals) / len(vals)) ** 0.5
    return max(ICE_MIN_VOL, m + ICE_SD_K * sd)


def _held(passive_bid, pl):
    """價位被打穿(對面最佳價越過了它)就不算「守住」。"""
    ref = best_bid if passive_bid else best_ask
    return not (ref is not None and ((passive_bid and ref < pl) or ((not passive_bid) and ref > pl)))


def _detect_iceberg(now):
    thr = _ice_threshold(now)
    done = set()
    # (一)有委託 id 的證據:同一張被動單被打掉的量遠超它自己的顯示量
    for pid in list(_ice_cand):
        st = ord_stat.get(pid)
        if st is None or st[7] is None or now - st[7] > ICE_WIN:
            _ice_cand.discard(pid)
            continue
        is_bid, pl, init, mx, ex, nf, t0, t1, rf = st
        if ex >= max(ICE_MIN_VOL, ICE_FACTOR * mx) and nf >= 3 and (rf >= 1 or ex >= ICE_FACTOR * max(init, mx)) and _held(is_bid, pl):
            if _emit("iceberg", "buy" if is_bid else "sell", pl * _pips, ex,
                     {"vis": round(mx, 1), "n": nf, "refill": rf, "conf": 2, "by": "id"}, key=(is_bid, pl), now=now):
                done.add((is_bid, pl))
    # (二)用價位統計:同價位 60 秒內被打掉的量遠超看得見的掛量;有補單證據可信度 2,否則要 1.5 倍門檻、可信度 1
    cut = now - ICE_WIN
    for key in list(lvl_trades):
        dq = lvl_trades[key]
        while dq and dq[0][0] < cut:
            dq.popleft()
        if not dq:
            del lvl_trades[key]
            continue
        if key in done:
            continue
        vol = sum(x[1] for x in dq)
        if vol < thr or len(dq) < ICE_MIN_TRADES:
            continue
        vis = lvl_vis.get(key)
        vis_max = vis[0] if vis and now - vis[1] <= ICE_WIN * 2 else 0.0
        if vis_max <= 0 or vol < ICE_FACTOR * vis_max:      # 不知道看得見的掛量就沒辦法說「藏了多少」
            continue
        rq = lvl_refill.get(key)
        while rq and rq[0] < cut:
            rq.popleft()
        refills = len(rq) if rq else 0
        conf = 2 if refills >= ICE_MIN_REFILL else 1
        if conf == 1 and vol < 1.5 * thr:
            continue
        passive_bid, pl = key
        if not _held(passive_bid, pl):
            continue
        _emit("iceberg", "buy" if passive_bid else "sell", pl * _pips, vol,
              {"vis": round(vis_max, 1), "n": len(dq), "refill": refills, "conf": conf, "by": "lvl"}, key=key, now=now)


def _detect_exhaust(now):
    push = _win(now, 60, 15)
    tail = _win(now, 15, 0)
    if not push:
        return
    pb, ps, p0, p1, phi, plo, _ = push
    tb, ts_, _, t_last, thi, tlo, _ = tail if tail else (0.0, 0.0, p1, p1, p1, p1, 0.0)
    legs = [_win(now, 60 - 15 * i, 45 - 15 * i) for i in range(3)]      # 推進段的前/中/後三分之一
    if not legs[0] or not legs[2]:
        return
    scale = 15.0 / 45.0
    # 買方力竭:推進段淨買、價格上推,後勁遞減,最近 15 秒買量萎縮且沒再創高
    if (pb - ps >= EXH_PUSH_VOL and phi - p0 >= EXH_RISE_PTS and tb <= EXH_TAIL_RATIO * pb * scale
            and legs[2][0] <= EXH_LEG_RATIO * legs[0][0] and thi <= phi + 0.25):
        _emit("exhaust", "buy", phi, pb - ps, {"rise": round(phi - p0, 2), "tail": round(tb, 1)}, now=now)
    elif (ps - pb >= EXH_PUSH_VOL and p0 - plo >= EXH_RISE_PTS and ts_ <= EXH_TAIL_RATIO * ps * scale
            and legs[2][1] <= EXH_LEG_RATIO * legs[0][1] and tlo >= plo - 0.25):
        _emit("exhaust", "sell", plo, ps - pb, {"fall": round(p0 - plo, 2), "tail": round(ts_, 1)}, now=now)


def _detect_events(now):
    """每秒呼叫一次(在 _lock 內)。任何偵測器出錯都只跳過那個,不影響主流程。"""
    if not EVENTS_ON:
        return
    try:
        if _cur and now - _cur["t1"] > SWEEP_GAP:
            _exec_end()
        if _cl and now - _cl["t1"] > SWEEP_GAP:
            _cluster_end()
    except Exception:
        pass
    for fn in (_detect_iceberg, _detect_absorb, _detect_exhaust):
        try:
            fn(now)
        except Exception:
            pass
    while events and events[0]["t"] < now - EVT_KEEP_SEC:
        events.popleft()


def _events_payload():
    return list(events)


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
        try:
            _on_trade_event(time.time(), "b" if is_bid else "s", price_level, price_level * _pips, sz, bool(is_bid),
                            start=bool(is_execution_start), aggr_id=aggressor_order_id, pass_id=passive_order_id)
        except Exception:
            pass


def handle_depth(addon, alias, is_bid, price_level, size_level):
    global best_bid, best_ask, last_event_time
    with _lock:
        last_event_time = time.time()
        if EVENTS_ON and size_level > 0:
            try:
                _k = (bool(is_bid), price_level)
                _sz = float(size_level) / _size_mult
                _v = lvl_vis.get(_k)
                _t = time.time()
                if _v is None or _t - _v[1] > ICE_WIN * 2 or _sz > _v[0]:
                    lvl_vis[_k] = [_sz, _t]
                if len(lvl_vis) > 20000:
                    lvl_vis.clear()
            except Exception:
                pass
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
        try:
            _detect_events(now)
            ev_out = _events_payload()
        except Exception:
            ev_out = []
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
        "events": ev_out,
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
