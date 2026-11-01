"""事件與每分鐘彙總的長期保存:接收→存檔→讀取、重送去重、回補舊存檔、API 合併。"""
import gzip, json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orderflow_store as ST
import backfill_orderflow_archive as BF
from orderflow_minutes import minute_rows

T0 = 1_761_000_000 // 86400 * 86400 + 3600          # 某個 UTC 日 01:00
d = tempfile.mkdtemp(); ev_dir = os.path.join(d, "ev"); mn_dir = os.path.join(d, "mn"); ar_dir = os.path.join(d, "ar"); os.makedirs(ar_dir)

# 1) 事件:跨 UTC 日分檔、重送去重、種類/區間過濾
e1 = dict(id="a1", t=T0 + 10, k="iceberg", s="buy", p=100.0, v=50, x=dict(conf=2))
e2 = dict(id="a2", t=T0 + 86400 + 5, k="sweep", s="sell", p=101.0, v=30)
ST.append_events(ev_dir, [e1, e2, e1])                                 # e1 重複寫入
assert sorted(os.listdir(ev_dir)) == [ST.day_of(T0) + ".jsonl", ST.day_of(T0 + 86400) + ".jsonl"]
got = ST.read_events(ev_dir, T0, T0 + 2 * 86400)
assert [e["id"] for e in got] == ["a1", "a2"], got                     # 讀取時去重、依時間排序
assert [e["id"] for e in ST.read_events(ev_dir, T0, T0 + 20)] == ["a1"]
open(os.path.join(ev_dir, ST.day_of(T0) + ".jsonl"), "a").write("壞行\n{\"t\":1}\n")   # 壞行/缺欄位不影響
assert len(ST.read_events(ev_dir, T0, T0 + 2 * 86400)) == 2

# 2) 每分鐘彙總:跟 minute_rows 一致;分鐘換了才寫;結束 flush 寫最後一列
recs = [dict(received_at=T0 + i, delta=float(i % 7), delta_tr=float(i % 5), vol=10.0, cvd_tr=float(i), imb=0.1, spot=100.0 + i * 0.01) for i in range(0, 190)]
agg = ST.MinuteAgg(mn_dir)
for r in recs:
    agg.add(r)
rows_before_flush = ST.read_minutes(mn_dir, T0 - 60, T0 + 400)
agg.flush()
rows = ST.read_minutes(mn_dir, T0 - 60, T0 + 400)
exp = {r["t"]: r for r in minute_rows(recs)}
assert set(rows) == set(exp) and len(rows) >= 3 and len(rows) > len(rows_before_flush)
for t in exp:
    for k in ("delta", "delta_tr", "cvd_tr", "n"):
        assert rows[t][k] == exp[t][k], (t, k, rows[t][k], exp[t][k])
# 重啟後同一分鐘又寫一次較少包的列 → 讀取時取 n 大的
ST.append_minutes(mn_dir, [dict(t=min(exp), n=1, delta=999.0)])
assert ST.read_minutes(mn_dir, T0 - 60, T0 + 400)[min(exp)]["delta"] != 999.0
# 流量停了:flush_if_stale
agg2 = ST.MinuteAgg(os.path.join(d, "mn2")); agg2.add(dict(received_at=T0 + 5, delta=1.0))
agg2.flush_if_stale(T0 + 30); assert not ST.read_minutes(os.path.join(d, "mn2"), T0 - 60, T0 + 400)
agg2.flush_if_stale(T0 + 200); assert len(ST.read_minutes(os.path.join(d, "mn2"), T0 - 60, T0 + 400)) == 1

# 3) 回補舊的每日完整存檔(含 diag 之外的整包、事件每包重送)
ev_dir2, mn_dir2 = os.path.join(d, "ev2"), os.path.join(d, "mn2b")
with gzip.open(os.path.join(ar_dir, ST.day_of(T0) + ".jsonl.gz"), "wt", encoding="utf-8") as f:
    for i in range(0, 180):
        pk = dict(received_at=T0 + i, delta=float(i % 7), delta_tr=1.0, vol=5.0, cvd_tr=float(i), book_bid=[[1, 2]], events=[e1] if i >= 10 else [])
        f.write(json.dumps(pk) + "\n")
    f.write("不是 json\n")
r = BF.process_file(os.path.join(ar_dir, ST.day_of(T0) + ".jsonl.gz"), ev_dir2, mn_dir2)
assert r[0] == 1 and r[1] == 1 and r[2] == 3 and r[3] == 3 and r[4] == 1, r        # 事件去重成 1、分鐘 3 列、1 壞行
r2 = BF.process_file(os.path.join(ar_dir, ST.day_of(T0) + ".jsonl.gz"), ev_dir2, mn_dir2)
assert r2[1] == 0 and r2[3] == 0, r2                                              # 可重複執行,不會重複寫
assert len(ST.read_events(ev_dir2, T0, T0 + 99)) == 1

# 4) API:長期存檔 + 即時檔合併、去重、不會因為即時檔不存在而回空
import gex_web_server as G
G.ORDERFLOW_EVENTS_ARCHIVE_DIR = ev_dir
G.ORDERFLOW_EVENTS_PATH = os.path.join(d, "live_events.jsonl")
open(G.ORDERFLOW_EVENTS_PATH, "w").write(json.dumps(e1) + "\n" + json.dumps(dict(id="live1", t=T0 + 20, k="exhaust", s="buy", p=100.5, v=60)) + "\n")
out = G.api_flowevents(None, dict(**{"from": T0, "to": T0 + 2 * 86400}))
assert sorted(e["id"] for e in out["events"]) == ["a1", "a2", "live1"], out
G.ORDERFLOW_MINUTES_DIR = mn_dir
G.ORDERFLOW_HISTORY_PATH = os.path.join(d, "no_such_history.jsonl")                # 即時檔不存在
fm = G.api_flowmom(None, dict(**{"from": T0 - 60, "to": T0 + 400}))
assert len(fm["times"]) == len(exp) and fm["times"] == sorted(fm["times"]), fm
assert abs(fm["rows"][0][1] - (exp[min(exp)]["delta_tr"])) < 1e-6, "動能副圖要優先用實際成交的 delta_tr"
assert fm["src"] == ["tr"] * len(fm["times"])
# 舊版 bridge 沒有 delta_tr 的日子:退回 delta;同一段時間可以新舊混在一起
old_t = T0 - 3 * 3600 // 60 * 60
ST.append_minutes(mn_dir, [dict(t=old_t, n=60, delta=7.0, delta_buy=8.0, delta_sell=1.0), dict(t=old_t + 60, n=60, delta=-3.0, delta_tr=-9.0)])
fm2 = G.api_flowmom(None, dict(**{"from": old_t - 60, "to": T0 + 400}))
assert fm2["src"][:2] == ["mbo", "tr"] and fm2["rows"][0][1] == 7.0 and fm2["rows"][1][1] == -9.0, fm2
assert abs(fm2["rows"][1][0] - (7.0 - 9.0)) < 1e-6, "累積線要把兩種來源接起來"
print("orderflow archive ok")
