"""用「真的」bookmap 套件(pip install bookmap)的訊息解析,把偽造的伺服器訊息餵給 bridge 的 handler,
確認參數順序/型別對得上、沒有 id 時的原始值、廣播/設定變更的呼叫方式。沒裝 bookmap 套件就跳過。"""
import os, sys
try:
    import bookmap as bm
    from bookmap import bookmap as bmi
except Exception:
    print("skip: 沒有 bookmap 套件(pip install bookmap)"); sys.exit(0)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orderflow_bridge as b
b._post = lambda pkt: None
S = bmi.FIELD_SEPARATOR
addon = {}
bm.add_depth_handler(addon, b.handle_depth); bm.add_mbo_handler(addon, b.handle_mbo); bm.add_trades_handler(addon, b.handle_trades)
bm.add_on_setting_change_handler(addon, b.handle_setting_change); bm.add_indicator_response_handler(addon, b.handle_indicator_response)
bm.add_broadcasting_provider_status_handler(addon, b.handle_bcast_status); bm.add_broadcasting_handler(addon, b.handle_bcast)
b._pips = 1.0; b._size_mult = 1.0
def feed(*tokens): bmi._process_event(addon, S.join(tokens))
feed(bmi.DEPTH, "NQ", "1", "100", "7")
feed(bmi.MBO, "NQ", "BID_NEW", "o1", "99", "5")
feed(bmi.TRADE, "NQ", "100.0", "3", "0", "1", "1", "1", "", "")              # 沒 id(假設 Bookmap 送空字串)
feed(bmi.TRADE, "NQ", "101.0", "4", "0", "0", "1", "1", "A1", "o1")            # 有 id
feed(bmi.TRADE, "NQ", "101.0", "9", "1", "0", "1", "1", "A2", "o1")            # OTC
assert b.D["trades"] == 3 and b.D["otc"] == 1 and b.D["aggr_id"] == 1 and b.D["pass_id"] == 1 and b.D["pass_match"] == 1, b.D
assert b.D["samples"]["aggr"][0] == "''" and b.D["mbo"] == 1 and b.D["depth"] == 1 and b.D["err"] == 0, b.D
assert b.tr_buy == 3 and b.tr_sell == 4, (b.tr_buy, b.tr_sell)
feed(bmi.ON_SETTINGS_PARAMETER_CHANGED, "NQ", "OF stop_min_vol", "NUMBER", "35.0"); assert b.STOP_MIN_VOL == 35
feed(bmi.ON_SETTINGS_PARAMETER_CHANGED, "NQ", "OF msg_on", "BOOLEAN", "false"); assert b.MSG_ON is False
b._ind_req[101] = "OF CVD(trades)"
feed(bmi.INDICATOR_RESPONSE, "101", "5"); assert b._ind["OF CVD(trades)"] == 5
b._bc["subs"].clear()
feed(bmi.BROADCASTING, "stops", '{"p": 1}'); assert b._bc["counts"]["stops"] == 1
assert b.D["err"] == 0
print("real bookmap package parse ok")
