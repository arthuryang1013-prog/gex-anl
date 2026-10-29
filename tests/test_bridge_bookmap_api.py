"""bridge 用到的 Bookmap 功能(設定面板/指標/訊息欄/廣播/診斷/id 正規化/OTC):用假的 bookmap 模組驗證。
假模組的函式名稱與參數,對照 bookmap 0.1.2 套件原始碼(不是只看說明文件)。"""
import os, sys, types
calls = []
fake = types.ModuleType("bookmap")
def _rec(name):
    def f(*a, **k): calls.append((name, a[1:], k))
    return f
for n in ("add_number_settings_parameter", "add_boolean_settings_parameter", "register_indicator", "add_point",
          "send_user_message", "subscribe_to_provider", "subscribe_to_generator", "subscribe_to_depth",
          "subscribe_to_mbo", "subscribe_to_trades"):
    setattr(fake, n, _rec(n))
sys.modules["bookmap"] = fake
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orderflow_bridge as b
b._post = lambda pkt: sent.append(pkt); sent = []
b._pips = 1.0; b._size_mult = 1.0
T = [1_700_000_000.0]
b.time = types.SimpleNamespace(time=lambda: T[0], sleep=lambda x: None)

# 訂閱:設定面板、指標、訊息欄、商品資訊
b.handle_subscribe_instrument("ADDON", "NQ", "NQZ6", False, 0.25, 1.0, 20.0,
                              {"mbo": True, "depth": True, "isDelayed": False, "exchangeUsedForSubscription": "CME"})
names = [c[0] for c in calls]
assert names.count("add_number_settings_parameter") == len(b._SETTINGS) and names.count("add_boolean_settings_parameter") == 2
assert all(c[2].get("reload_if_change") is False for c in calls if c[0].startswith("add_") and "settings" in c[0]), "改設定不該重啟 addon"
assert [c for c in calls if c[0] == "register_indicator"] and b._info["mult"] == 20.0 and b._info["exch"] == "CME"
assert any(c[0] == "send_user_message" for c in calls)
b._pips = 1.0; b._size_mult = 1.0

# 指標回應 → 之後每秒 add_point
b.handle_indicator_response("ADDON", 101, 7); b.handle_indicator_response("ADDON", 102, 8)
calls.clear(); b._tick()
assert [c for c in calls if c[0] == "add_point" and c[1][1] == 7], calls

# 設定改變:數字(含整數型別)、布林
b.handle_setting_change("ADDON", "NQ", "OF stop_min_aggr", "NUMBER", 5.0); assert b.STOP_MIN_AGGR == 5 and isinstance(b.STOP_MIN_AGGR, int)
b.handle_setting_change("ADDON", "NQ", "OF abs_dom", "NUMBER", 0.8); assert b.ABS_DOM == 0.8
b.handle_setting_change("ADDON", "NQ", "OF events_on", "BOOLEAN", False); assert b.EVENTS_ON is False
b.handle_setting_change("ADDON", "NQ", "OF events_on", "BOOLEAN", True)
b.handle_setting_change("ADDON", "NQ", "不認識", "NUMBER", 1.0)           # 不認識的名稱不丟例外

# id 正規化:Bookmap 沒有 id 時給的是字串,不是 None
for x in (None, "", " ", "null", "None", "nan", "0"):
    assert b._norm_id(x) is None, x
assert b._norm_id("12345") == "12345" and b._norm_id(777) == "777"
# 空字串 id 的成交:不該被當成同一張攻擊單(舊 bug)
b.events.clear(); b.secs.clear(); b._cur = None; b._cl = None; b._last_evt.clear(); b._agg_seq = 0
for i in range(4):
    b.handle_trades("A", "NQ", 100 + i, 6, False, True, True, True, "", ""); T[0] += 0.05
assert b.D["aggr_id"] == 0 and b.D["pass_id"] == 0 and b.D["samples"]["aggr"] == ["''"], b.D
# 有 id → 計入;passive id 對得上 MBO 追蹤中的委託 → pass_match
b.handle_mbo("A", "NQ", "BID_NEW", "P1", 90, 5)
b.handle_trades("A", "NQ", 90, 2, False, False, True, True, "X9", "P1")
assert b.D["aggr_id"] == 1 and b.D["pass_id"] == 1 and b.D["pass_match"] == 1, b.D
# OTC 成交不算 Delta
cvd0 = b.tr_buy - b.tr_sell
b.handle_trades("A", "NQ", 100, 50, True, True, True, True, "", ""); assert b.tr_buy - b.tr_sell == cvd0 and b.D["otc"] == 1
# handler 丟不出例外(Bookmap 會因此關掉 addon):餵垃圾也不能炸
b.handle_trades("A", "NQ", "abc", "x", False, True, True, True, None, None)
b.handle_mbo("A", "NQ", None, None, None, None); b.handle_depth("A", "NQ", True, "x", None)
b.handle_setting_change("A", "NQ", None, None, None); b.handle_bcast("A", None, object()); b.handle_bcast_status("A", None)

# 廣播:自動訂閱所有 provider/generator,記錄原始內容
calls.clear()
b.handle_bcast_status("ADDON", {"Stops&Icebergs": ["stops", "icebergs"], "Other": [{"name": "g1"}]})
assert ("subscribe_to_provider", ("Stops&Icebergs",), {}) in calls
assert ("subscribe_to_generator", ("Stops&Icebergs", "icebergs", False), {}) in calls and any(c[1][1:2] == ("g1",) for c in calls if c[0] == "subscribe_to_generator")
n = len(calls); b.handle_bcast_status("ADDON", {"Stops&Icebergs": ["stops", "icebergs"]}); assert len(calls) == n, "不重複訂閱"
b.handle_bcast("ADDON", "stops", {"price": 123, "size": 5})
d = b._diag_payload(); assert d["bc"]["counts"]["stops"] == 1 and any(r[1] == "stops" for r in d["bc"]["recent"]) and d["info"]["exch"] == "CME"
assert "recent" not in b._diag_payload()["bc"], "沒新內容就不重送"

# 事件 → Bookmap 訊息欄
b.events.clear(); b._last_evt.clear(); b._last_msg_t[0] = 0.0; calls.clear()
b._emit("stop", "buy", 100.0, 40, {"aggr": 5}, now=T[0])
assert [c for c in calls if c[0] == "send_user_message" and "停損" in c[1][1]], calls
b.MSG_ON = False; calls.clear(); b._last_msg_t[0] = 0.0; b._last_evt.clear(); b._emit("stop", "buy", 100.0, 40, now=T[0]); assert not calls
# 封包帶 diag / mult,且大小合理
sent.clear(); b._tick(); import json
p = sent[-1]; assert p["mult"] == 20.0 and "diag" in p and len(json.dumps(p)) < 20000, len(json.dumps(p))
print("bridge bookmap api ok")
