import gzip, json, os, random, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import opt_hedge_check as H

T0 = 1_760_000_000 // 3600 * 3600


def mk_md(rnd, n_min=3000, bumps=()):
    md = {T0 + i * 60: rnd.gauss(0, 100) for i in range(n_min)}
    for t, dv in bumps:                       # 在 [t, t+300) 每分鐘加 dv/5
        for j in range(5):
            md[t // 60 * 60 + j * 60] += dv / 5
    return md


def mk_events(rnd, n=80):
    ts = sorted(rnd.sample(range(T0 + 1200, T0 + 3000 * 60 - 1200, 300), n))
    return [dict(t=t, cp=rnd.choice("CP")) for t in ts]

# 1) 沒有關聯 → 多數種子不應判顯著(5% 顯著水準下 20 個種子最多 3 個)
hits = 0
for s in range(20):
    rnd = random.Random(s)
    r = H.analyze(mk_events(rnd), mk_md(rnd), perm=500, seed=s)
    assert r["n_used"] >= 70, r
    hits += sum(1 for k in ("before", "during", "after") if r["win"][k]["p"] < 0.05)
assert hits <= 6, ("無關聯卻常顯著", hits)

# 2) 植入關聯:大單之後 Call 組期貨 +300、Put 組 -300 → after 要顯著,before 不顯著
rnd = random.Random(7)
ev = mk_events(rnd, 120)
bumps = [(e["t"], 300 if e["cp"] == "C" else -300) for e in ev]
r = H.analyze(ev, mk_md(rnd, bumps=bumps), perm=1000)
assert r["win"]["after"]["p"] < 0.01 and r["win"]["after"]["diff"] > 300, r["win"]["after"]
assert r["win"]["before"]["p"] > 0.01, r["win"]["before"]
txt = H.render(r)
assert "之後有明顯差距" in txt or "巧合機率" in txt

# 3) 缺資料:窗口不足的事件被排除,只有單邊也不會當掉
r = H.analyze([dict(t=T0 + 600, cp="C")], {}, perm=10)
assert r["n_used"] == 0 and "無法比較" in H.render(r) or "不到 30" in H.render(r)

# 4) 讀存檔(多段 gzip 串接、壞尾端、只取範圍內)
d = tempfile.mkdtemp()
day = __import__("time").strftime("%Y%m%d", __import__("time").gmtime(T0))
with open(os.path.join(d, day + ".jsonl.gz"), "ab") as f:
    for part in range(2):
        lines = "\n".join(json.dumps(dict(received_at=T0 + part * 600 + i * 10, delta_tr=float(i), delta=-1.0)) for i in range(60)) + "\n"
        f.write(gzip.compress(lines.encode()))
    f.write(b"\x1f\x8b\x08garbage")           # 壞尾端
md = H.load_minute_delta(d, T0, T0 + 3600)
assert len(md) >= 10 and all(v >= 0 for v in md.values()), md        # 取 delta_tr(>=0)而不是 delta(-1)
print("opt_hedge_check ok")
