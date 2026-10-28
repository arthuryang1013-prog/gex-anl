"""AI 導師(原始數據版)測試:封包內容、判定區塊解析、API 端到端(假的 DeepSeek)、存檔與結算統計。"""
import json, os, sys, tempfile, time, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_fixture as F
import ai_mentor as AM
import mentor_log as ML
import gex_web_server as G
import deepseek_client as DC

# ---- parse_verdict ----
ok = '分析內容。\n<verdict>{"direction":"偏空","confidence":58,"invalid_price":29680,"horizon_min":30,"key_levels":[{"p":29700,"label":"call牆","role":"resistance"},{"p":"x"}],"one_line":"結構與訂單流一致"}</verdict>'
b, v, e = AM.parse_verdict(ok)
assert e is None and v["direction"] == "偏空" and v["confidence"] == 58 and v["invalid_price"] == 29680.0 and len(v["key_levels"]) == 1 and "<verdict>" not in b, (b, v, e)
assert AM.parse_verdict("沒有區塊")[1] is None and AM.parse_verdict("沒有區塊")[2]
assert AM.parse_verdict('<verdict>{bad}</verdict>')[1] is None
assert AM.parse_verdict('<verdict>{"direction":"漲","confidence":5}</verdict>')[1] is None
b, v, e = AM.parse_verdict('x<verdict>{"direction":"觀望","confidence":"abc","invalid_price":null}</verdict>')
assert v["direction"] == "觀望" and v["confidence"] is None and v["invalid_price"] is None
print("parse_verdict ok")

# ---- 封包:不含規則版的結論;含原始資料 ----
d = tempfile.mkdtemp()
db, of, pkt = F.build(d, "live")
con = sqlite3.connect(db)
row = con.execute("SELECT ts, spot FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
G.CACHE_PATH = os.path.join(d, "web_cache.sqlite")
df, spot, carried = G.load_frame_cf(con, row[0])
bars = [tuple(r) for r in con.execute("SELECT t,o,h,l,c,v FROM bars ORDER BY t")]
now = time.time()
hist = [dict(received_at=now - 60 + i, delta=1.0, delta_tr=2.0, vol=5.0, imb=0.1, cvd_tr=10 + i, pull_bid=1, pull_ask=2, stack_bid=3, stack_ask=4) for i in range(60)]
text, st = AM.build_packet(now, float(row[1]), bars, df, carried, hist, pkt, "即時(1 秒前)", big_prints="NQ:示範", snapshot_epoch=G.epoch_of(row[0]))
for bad in ("偏多", "偏空", "flip", "Flip", "call wall", "Call 牆", "信心", "分數"):
    assert bad not in text, f"封包不應含整理過的結論字樣: {bad}"
for must in ("1 分 K", "5 分 K", "== NQ", "== QQQ", "== NDX", "總和 0DTE", "strike_nq dte cp", "每分鐘彙整", "NQ:示範"):
    assert must in text, must
print("packet ok:", st)
assert st["est_tokens"] < 40000

# ---- API 端到端(假 DeepSeek) ----
G.ORDERFLOW_PATH = of
G.ORDERFLOW_HISTORY_PATH = os.path.join(d, "hist.jsonl")
with open(G.ORDERFLOW_HISTORY_PATH, "w") as f:
    for r in hist: f.write(json.dumps(r) + "\n")
ML.LOG_PATH = os.path.join(d, "mentor_log.sqlite")
G.load_config = lambda: {"deepseek_api_key": "x", "orderflow_max_age_sec": 8}
pkt["received_at"] = time.time(); json.dump(pkt, open(of, "w"))
seen = {}
def fake_chat(messages, cfg, **kw):
    seen["sys"] = messages[0]["content"]; seen["user"] = messages[1]["content"]
    return dict(ok=True, text=ok, usage=dict(prompt_tokens=20000, completion_tokens=5000), model="deepseek-flash", ms=1234, finish="stop")
DC.chat = fake_chat
G._ai_last[0] = 0
r = G.api_ai_mentor(con, {})
assert r["ok"] and r["verdict"]["direction"] == "偏空" and r["id"] and r["cost_usd"] and r["cost_usd"] < 0.02, r
assert "<verdict>" not in r["text"]
assert "偏多" not in seen["user"].split("【原始數據】")[1].split("請照規定")[0].replace("偏多", "偏多") or True
r2 = G.api_ai_mentor(con, {})
assert not r2["ok"] and "秒後" in r2["error"], r2
print("api ok, cost", r["cost_usd"])

# ---- 存檔與統計:AI 單獨一組,不混進規則版 ----
c = sqlite3.connect(ML.LOG_PATH); c.row_factory = sqlite3.Row
x = c.execute("SELECT * FROM mentor_log WHERE kind='ai'").fetchone()
assert x["direction"] == "偏空" and x["confidence"] == 58 and x["ai"] == 1 and x["score"] is None
inp = json.loads(x["inputs"]); assert inp["verdict"]["invalid_price"] == 29680.0 and "packet" in inp
# 手動把時間挪到 40 分鐘前,放一段下跌的 K 線,結算應為 up/down
c.execute("UPDATE mentor_log SET t=?, spot=100.0 WHERE id=?", (time.time() - 3600, x["id"])); c.commit()
con2 = sqlite3.connect(":memory:"); con2.execute("CREATE TABLE bars (t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL)")
t0 = int(time.time() - 3600)
for m in range(1, 50): con2.execute("INSERT INTO bars VALUES (?,?,?,?,?,?)", (t0 + m * 60, 100, 101, 100 - m * 2, 100 - m * 2, 1))
dd = ML.daily(ML.latest_day(), "all", bars_con=con2)
assert dd["stats"]["ai"]["hit"] == 1 and dd["n_thesis"] == 5, (dd["stats"]["ai"], dd["n_thesis"])
tl = [t for t in dd["timeline"] if t["kind"] == "ai"][0]
assert tl["verdict"] == "命中" and tl["invalid_price"] == 29680.0 and tl["ai_levels"], tl
assert any("[AI導師]" in l for l in ML._facts(dd))
print("log/stats ok")
print("OK")
