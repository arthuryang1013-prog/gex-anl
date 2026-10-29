"""flow_outcomes:事件的 GEX 背景、事後 ±40 結果、碰牆記錄、白話報表。用假 K 棒/假事件/假的牆來源。"""
import os, sys, json, sqlite3, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import flow_outcomes as F

d = tempfile.mkdtemp()
bars_db, ev_path, hist = os.path.join(d, "b.sqlite"), os.path.join(d, "ev.jsonl"), os.path.join(d, "h.jsonl")
T0 = 1_700_000_000 // 60 * 60            # 某個整分鐘
con = sqlite3.connect(bars_db); con.execute("CREATE TABLE bars (t INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL)")


def put_bars(fn, start, n):
    for i in range(n):
        o, h, l, c = fn(i)
        con.execute("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,0)", (start + i * 60, o, h, l, c))
    con.commit()

# 價格:先在 1000 附近,事件後 10 分鐘一路漲到 +45,之後回落
def path(i):
    p = 1000 + (0 if i < 5 else min(45, (i - 4) * 5))
    return p, p + 1, p - 1, p
put_bars(path, T0, 60)
levels = lambda t: (T0, 1000.0, {"NQ|all": {"call": 1002.0, "put": 950.0, "flip": 1100.0, "call_a": None, "put_b": None}})

# outcome() 本身
bars = con.execute("SELECT t,o,h,l,c FROM bars ORDER BY t").fetchall()
o = F.outcome(bars, T0 + 5 * 60 + 10, 1005.0)
assert o and o["first40"] == "up" and o["mfe30"] >= 40 and o["mae30"] < 41, o
assert F.outcome(bars, T0 + 55 * 60, 1000.0) is None, "還沒走完 30 分鐘不給結果"
# 同根碰到 ±40 → both
put_bars(lambda i: (1000, 1045, 955, 1000), T0 + 200 * 60, 40)
b2 = con.execute("SELECT t,o,h,l,c FROM bars WHERE t>=?", (T0 + 200 * 60,)).fetchall()
assert F.outcome(b2, T0 + 199 * 60 + 30, 1000.0)["first40"] == "both"

# 事件:近牆(離 call 牆 2 點)的買方吸收,偏多
evs = [dict(id="e1", t=T0 + 5 * 60 + 5, k="absorb", s="buy", p=1000.0, v=200, x={"conf": 2}),
       dict(id="e2", t=T0 + 6 * 60, k="exhaust", s="buy", p=1030.0, v=90, x={}),          # 買方力竭 → 偏空
       dict(id="e3", t=T0 + 7 * 60, k="stop", s="sell", p=1010.0, v=50, x={"inf": 1})]    # 賣方停損 → 順勢偏空
open(ev_path, "w").write("\n".join(json.dumps(e) for e in evs) + "\n")
c = F.init_db(os.path.join(d, "out.sqlite"))
now = T0 + 120 * 60
assert F.ingest_events(c, ev_path, levels, now=now) == 3 and F.ingest_events(c, ev_path, levels, now=now) == 0
r = c.execute("SELECT near_name, near_dist, bias, conf FROM ev WHERE id='e1'").fetchone()
assert r == ("NQ|all|call", 2.0, 1, 2), r
assert [x[0] for x in c.execute("SELECT bias FROM ev ORDER BY t")] == [1, -1, -1]
assert F.fill_outcomes(c, bars_db, now=now) == 3
assert c.execute("SELECT first40 FROM ev WHERE id='e1'").fetchone()[0] == "up"
# 沒有 GEX 背景也要能記
c2 = F.init_db(os.path.join(d, "out2.sqlite")); F.ingest_events(c2, ev_path, lambda t: None, now=now)
assert c2.execute("SELECT COUNT(*), SUM(near_name IS NULL) FROM ev").fetchone() == (3, 3)

# 碰牆:價格從 1000 附近爬到 call 牆 1002 → 上一根收盤離牆 > 2 點才算新碰觸
bars_db2 = os.path.join(d, "b2.sqlite"); con2 = sqlite3.connect(bars_db2)
con2.execute("CREATE TABLE bars (t INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL)")
for i in range(70):
    p = 990 + (i if i < 12 else 12)                                   # 990→1002 後停在 1002
    con2.execute("INSERT INTO bars VALUES (?,?,?,?,?,0)", (T0 + i * 60, p, p + 1, p - 1, p)); 
con2.commit()
json.dump(dict(received_at=T0 + 11 * 60 + 30, delta_tr=-30.0, vol_tr=120.0, imb=0.1), open(hist, "w")); open(hist, "a").write("\n")
now2 = T0 + 200 * 60
lv2 = lambda t: (T0, 1000.0, {"NQ|all": {"call": 1004.0, "put": None, "flip": None, "call_a": None, "put_b": None}})
n = F.ingest_touches(c, bars_db2, lv2, hist, ev_path, now=now2, lookback_h=10)
assert n == 1, n                                                       # 只記一次(停在牆邊不重複)
t = c.execute("SELECT level_name, approach, bounce FROM touch").fetchone(); assert t == ("NQ|all|call", "below", -1), t
assert F.ingest_touches(c, bars_db2, lv2, hist, ev_path, now=now2, lookback_h=10) == 0
F.fill_outcomes(c, bars_db2, now=now2)

# 報表:白話、不炸;有勝負欄位
txt = F.report(c, all_hours=True)
assert "先賺 40 點" in txt and "absorb" in txt and "碰牆" in txt, txt
print(txt[:400])
assert F.bias_of("exhaust", "sell") == 1 and F.bias_of("sweep", "sell") == -1 and F.bias_of("iceberg", "buy") == 1
lo, hi = F.wilson(7, 10); assert 0.39 < lo < 0.40 and 0.89 < hi < 0.93, (lo, hi)
print("flow_outcomes ok")
