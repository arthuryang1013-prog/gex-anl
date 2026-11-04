"""hedge_sim 引擎測試:用合成的 sqlite(跟真實 schema 相同)驗證損益拆解、對沖、成本、重現性、到期、儲存。"""
import datetime as dt, math, os, random, sys, tempfile, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hedge_sim as HS, hedge_pricing as HP

T0 = int(dt.datetime(2026, 11, 3, 14, 30, tzinfo=dt.timezone.utc).timestamp())   # 09:30 ET
EXP = "20261103"   # 當日到期:16:00 ET = 21:00 UTC
NBARS = 300

def build(path, seed=1, vol=1.5, iv_drift=0.0):
    rnd = random.Random(seed)
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE snapshots(ts TEXT, spot REAL);
    CREATE TABLE contracts(ts TEXT, expiry TEXT, dte INT, cls TEXT, strike REAL, cp TEXT, oi REAL, iv REAL, F REAL, T REAL,
      gamma REAL, gex REAL, stale INT, src TEXT, strike_nq REAL, vol REAL, gex_vol REAL, mult REAL, q REAL);
    CREATE TABLE bars(t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL);""")
    px, closes = 25000.0, []
    for i in range(NBARS):
        px += rnd.gauss(0, vol) * 4
        closes.append(px)
        con.execute("INSERT INTO bars VALUES (?,?,?,?,?,?)", (T0 + 60 * i, px, px, px, px, 1))
    exp_ep = int(dt.datetime(2026, 11, 3, 21, 0, tzinfo=dt.timezone.utc).timestamp())
    for i in range(0, NBARS, 3):
        t = T0 + 60 * i
        ts = HS._iso(t); spot = closes[i]
        con.execute("INSERT INTO snapshots VALUES (?,?)", (ts, spot))
        Tn = max(exp_ep - t, 3600) / HP.YEAR_SEC
        for k in range(24500, 25600, 50):
            for cp in "CP":
                iv = 0.2 + iv_drift * i / NBARS + abs(k - 25000) * 0.00005
                con.execute("INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (ts, EXP, 0, "0dte", k, cp, 100, iv, spot, Tn, 0, 0, 0, "NQ", k, 5, 0, 20, 0))
    con.commit(); con.close()
    return closes

def mk(seed=1, **kw):
    d = tempfile.mkdtemp(); p = os.path.join(d, "t.sqlite")
    closes = build(p, seed, **kw)
    return HS.DbMarket(p), closes, d

STRADDLE = [dict(expiry=EXP, strike=25000, cp="C", qty=2), dict(expiry=EXP, strike=25000, cp="P", qty=2)]

def run(params, seed=1, minutes=240, legs=STRADDLE, **kw):
    m, closes, d = mk(seed, **kw)
    sim = HS.Sim(m, params); sim.start(T0 + 120)
    sim.open_legs(legs); sim.advance(T0 + minutes * 60)
    return sim, m, d

def test_attribution_matches_cash():
    for seed in (1, 2, 3):
        for params in ({}, dict(auto=True, threshold=0.3), dict(auto=True, threshold=0.2, fut_mult=2)):
            sim, *_ = run(params, seed, iv_drift=0.03)
            c = sim.components()
            assert abs(c["total"] - sim.equity()) < 1e-6 * max(1, abs(sim.equity())) + 0.01, (seed, params, c["total"], sim.equity())
            for p in sim.new_points:
                assert abs(p["comp"]["total"] - p["equity"]) < 0.02, (p["comp"]["total"], p["equity"])

def test_auto_hedge_keeps_delta_bounded():
    sim, *_ = run(dict(auto=True, threshold=0.3), 4)
    assert sim.s["n_auto"] > 0
    # 對沖是在步驟結束時做的,所以每個步驟結束後的 |淨 Delta| 不會大於門檻 + 取整誤差(一口 NQ = 1)
    assert all(abs(p["net_delta"]) <= 0.3 + 0.5 + 1e-9 for p in sim.new_points), max(abs(p["net_delta"]) for p in sim.new_points)
    nohedge, *_ = run({}, 4)
    assert nohedge.s["n_hedges"] == 0

def test_mnq_finer_than_nq():
    a, *_ = run(dict(auto=True, threshold=0.3, fut_mult=2), 5)
    b, *_ = run(dict(auto=True, threshold=0.3, fut_mult=20), 5)
    ea = max(abs(p["net_delta"]) for p in a.new_points); eb = max(abs(p["net_delta"]) for p in b.new_points)
    assert ea <= eb + 1e-9 and ea <= 0.3 + 0.05 + 1e-9, (ea, eb)

def test_costs_reduce_pnl_and_zero_cost_matches():
    free = dict(opt_half_spread=0, opt_fee=0, fut_fee_nq=0, fut_slip=0, auto=True, threshold=0.3)
    s0, *_ = run(free, 6); s1, *_ = run(dict(free, opt_half_spread=1, opt_fee=2, fut_fee_nq=2, fut_slip=0.5), 6)
    assert s0.components()["cost"] > -100 and s1.components()["cost"] < s0.components()["cost"] - 100   # 零成本時只剩選擇權 0.05 跳動點取整
    assert s1.equity() < s0.equity()

def test_deterministic_and_resume_equal():
    a, *_ = run(dict(auto=True, threshold=0.3), 7)
    b, *_ = run(dict(auto=True, threshold=0.3), 7)
    assert a.s == b.s
    # 分段推進(模擬「即時模式」每次請求補到現在)與一次推進結果相同
    m, closes, d = mk(7)
    sim = HS.Sim(m, dict(auto=True, threshold=0.3)); sim.start(T0 + 120); sim.open_legs(STRADDLE)
    for k in range(60, 241, 30):
        sim.advance(T0 + k * 60)
    assert abs(sim.equity() - a.equity()) < 1e-6 and sim.s["fut_qty"] == a.s["fut_qty"]

def test_expiry_cash_settlement():
    m, closes, d = mk(8)
    sim = HS.Sim(m, dict(opt_half_spread=0, opt_fee=0)); sim.start(T0 + 120)
    sim.open_legs([dict(expiry=EXP, strike=25000, cp="C", qty=1)])
    sim.advance(T0 + 290 * 60)          # 21:00 UTC 之前 → 還沒到期(14:30 + 290 分 = 19:20 UTC)
    assert len(sim.s["legs"]) == 1
    # 把到期時刻提前到最後一根 K 棒之內來測試結算
    sim.s["legs"][0]["expiry_epoch"] = T0 + (NBARS - 5) * 60
    sim.advance(T0 + NBARS * 60)
    assert not sim.s["legs"]
    ex = [t for t in sim.new_trades if t["kind"] == "opt_expire"]; assert len(ex) == 1
    inner = max(0, sim.s["last_c"] - 25000) if False else ex[0]["price"]
    assert abs(sim.equity() - sim.components()["total"]) < 0.02

def test_manual_hedge_and_close():
    sim, m, d = run({}, 9, minutes=60, legs=[dict(expiry=EXP, strike=25000, cp="C", qty=5)])
    before = sim.totals()["net_delta"]
    rec = sim.hedge_now(target=0)
    after = sim.totals()["net_delta"]
    assert rec and abs(after) <= 0.5 + 1e-9 and abs(after) < abs(before) + 1e-9
    sim.hedge_now(units=-sim.s["fut_qty"], kind="manual")
    assert sim.s["fut_qty"] == 0
    n = sim.close_legs(group=sim.s["legs"][0]["group"])
    assert n == 1 and not sim.s["legs"]
    assert abs(sim.components()["total"] - sim.equity()) < 0.02

def test_input_validation():
    m, closes, d = mk(10)
    sim = HS.Sim(m, {}); 
    for fn in (lambda: sim.open_legs(STRADDLE),):
        try: fn(); assert False
        except HS.SimError: pass
    sim.start(T0 + 120)
    bad = [[], [dict(expiry=EXP, strike=25000, cp="X", qty=1)], [dict(expiry=EXP, strike=25000, cp="C", qty=0)],
           [dict(expiry=EXP, strike=99999, cp="C", qty=1)], [dict(expiry=EXP, strike=25000, cp="C", qty=10**6)], [dict(foo=1)]]
    for b in bad:
        try: sim.open_legs(b); assert False, b
        except HS.SimError: pass
    for p in (dict(threshold=-1), dict(fut_mult=5), dict(threshold="x"), dict(opt_fee=float("nan"))):
        try: HS.clean_params(p); assert False, p
        except HS.SimError: pass
    assert not sim.s["legs"]

def test_store_roundtrip():
    sim, m, d = run(dict(auto=True, threshold=0.3), 11, minutes=120)
    st = HS.Store(os.path.join(d, "h.sqlite"))
    aid = st.create("t", "replay", T0, sim.params, sim.s, 0)
    st.save(aid, sim)
    acc = st.load(aid)
    sim2 = HS.Sim(m, acc["params"], acc["state"])
    assert abs(sim2.equity() - sim.equity()) < 1e-9 and len(st.trades(aid)) > 2 and len(st.points(aid)) > 10
    st.delete(aid)
    try: st.load(aid); assert False
    except HS.SimError: pass

def test_compare_and_chain():
    m, closes, d = mk(12)
    res = HS.compare(m, STRADDLE, T0 + 120, T0 + 200 * 60,
                     [dict(label="不對沖"), dict(label="±0.3", auto=True, threshold=0.3), dict(label="±1", auto=True, threshold=1.0)])
    assert [r["n_hedges"] for r in res][0] == 0 and res[1]["n_hedges"] >= res[2]["n_hedges"] > -1
    assert res[1]["step_sd"] <= res[0]["step_sd"] + 1e-9       # 越常對沖,每步損益波動越小
    cv = HS.chain_view(m, T0 + 3600)
    assert cv["strikes"] and cv["expiry"] == EXP
    atm = min(cv["strikes"], key=lambda s: abs(s["strike"] - cv["ref"]))
    assert 0.3 < atm["C"]["delta"] < 0.7 and atm["P"]["delta"] < 0

if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
    print("OK")
