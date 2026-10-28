import json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gex_web_server as G
d = tempfile.mkdtemp(); G.ORDERFLOW_PATH = d + "/of.json"; G.load_config = lambda: {"orderflow_max_age_sec": 8}
assert G._orderflow_status() == {"connected": False}                                   # 從沒收過
json.dump({"received_at": time.time(), "lag_sec": 0.5}, open(G.ORDERFLOW_PATH, "w"))
s = G._orderflow_status(); assert s["connected"] and s["available"] and not s["stale"] and s["age_sec"] < 3
json.dump({"received_at": time.time() - 100, "lag_sec": 0.5}, open(G.ORDERFLOW_PATH, "w"))
s = G._orderflow_status(); assert s["connected"] and not s["available"] and s["stale"] and s["age_sec"] > 90
json.dump({"received_at": time.time(), "lag_sec": 99}, open(G.ORDERFLOW_PATH, "w"))
s = G._orderflow_status(); assert not s["available"]                                    # 延遲過高也算不可用
assert "delta" not in s and "imb" not in s                                              # 不外洩任何數值訊號
print("status orderflow ok")
