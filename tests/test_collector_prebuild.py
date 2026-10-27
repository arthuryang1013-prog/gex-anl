"""用假的 IBKR 連線測收集器的『合約清單快取 + 開盤前預建』邏輯。
只測邏輯:真實 IBKR 的回應速度、OI 開盤前是否已更新、NDX 盤前報價是否存在,都必須盤外用真連線驗證。"""
import datetime as dt, os, sys, tempfile, types, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nq_gex_collector as C
from ib_async import Future, FuturesOption, Option

d = tempfile.mkdtemp()
C.CACHE_PATH = d + "/universe_cache.json"; C.STATUS_PATH = d + "/st.json"; C.DB_PATH = d + "/db.sqlite"
C.PREBUILD_RETRY_SECS = 0
DAY = dt.date(2026, 10, 28)                       # 星期三
clock = {"t": dt.datetime(2026, 10, 28, 8, 5, tzinfo=C.NY)}
C._now_ny = lambda: clock["t"]
PX = {"NQ": 25000.0, "QQQ": 610.0, "NDX": 25300.0}
STEP = {"NQ": 25, "QQQ": 1, "NDX": 50}


class Tk:
    def __init__(s, ib, c, generic):
        s.c, s.close = c, None
        s.volume = 10.0; s.callOpenInterest = s.putOpenInterest = None; s.modelGreeks = None
        key = "NQ" if c.secType in ("FUT", "FOP") else ("QQQ" if c.symbol == "QQQ" else "NDX")
        s.px = PX[key]
        if c.secType in ("FUT", "STK", "IND"):
            s._mp = s.px
        else:
            s._mp = float("nan")
            if generic == "101" and not ib.oi_missing:
                s.callOpenInterest = s.putOpenInterest = 500.0
            if ib.open:
                s.modelGreeks = types.SimpleNamespace(impliedVol=0.2, undPrice=s.px)

    def marketPrice(s): return s._mp


class FakeIB:
    def __init__(s): s.open = False; s.oi_missing = False; s.calls = []; s.fail = set(); s.nid = 1000

    def reqContractDetails(s, c):
        s.calls.append(("cd", type(c).__name__, getattr(c, "symbol", None)))
        if isinstance(c, Future):
            return [types.SimpleNamespace(contract=Future("NQ", "20261218", conId=1, exchange="CME", localSymbol="NQZ6"))]
        key = "NQ" if isinstance(c, FuturesOption) else c.symbol
        out = []
        for k in range(-60, 61):
            st = round(PX[key] / STEP[key]) * STEP[key] + k * STEP[key]
            for r in "CP":
                s.nid += 1
                cls = FuturesOption if key == "NQ" else Option
                cc = cls(symbol=("NQ" if key == "NQ" else c.symbol), lastTradeDateOrContractMonth=c.lastTradeDateOrContractMonth,
                         strike=float(st), right=r, exchange=c.exchange, multiplier="20" if key == "NQ" else "100",
                         currency="USD", tradingClass=c.tradingClass, localSymbol=f"{key}{st}{r}", conId=s.nid)
                out.append(types.SimpleNamespace(contract=cc, lastTradeTime="", timeZoneId=""))
        return out

    def qualifyContracts(s, c):
        if c.symbol in s.fail:
            raise RuntimeError("boom")
        c.conId = {"QQQ": 2, "NDX": 3}[c.symbol]

    def reqSecDefOptParams(s, sym, ex, st, cid):
        return [types.SimpleNamespace(exchange="CME" if sym == "NQ" else "SMART",
                                      tradingClass={"NQ": "Q1A", "QQQ": "QQQ", "NDX": "NDX"}[sym],
                                      expirations=["20261028", "20261029"])]

    def reqMktData(s, c, generic="", *a): return Tk(s, c, generic)
    def sleep(s, x): pass
    def cancelMktData(s, c): pass


def opt_cd(ib): return sum(1 for c in ib.calls if c[0] == "cd" and c[1] in ("FuturesOption", "Option"))


def fresh_state(): return dict(uni={}, oi_cache={}, last_good={}, fresh=set())


con = C.init_db(C.DB_PATH)
ib = FakeIB(); state = fresh_state()

# 1) 08:05 盤前:NQ 建好 + 預建 QQQ(一次只建一個來源)並預抓 OI
C.run_cycle(ib, state, con)
assert "QQQ" in state["uni"] and "NDX" not in state["uni"], list(state["uni"])
assert state["oi_last"].get("QQQ"), "QQQ OI 應已預抓"
assert set(json.load(open(C.CACHE_PATH))) == {"NQ", "QQQ"}
# 2) 08:10:再建 NDX
clock["t"] = dt.datetime(2026, 10, 28, 8, 10, tzinfo=C.NY)
C.run_cycle(ib, state, con)
assert "NDX" in state["uni"] and state["oi_last"].get("NDX")
built_calls = opt_cd(ib); assert built_calls > 0
# 3) 09:35 開盤:零重建、第一輪就有 3 個來源、QQQ/NDX 不走慢速 OI 流程
ib.open = True; clock["t"] = dt.datetime(2026, 10, 28, 9, 35, tzinfo=C.NY)
pulls = []; orig = C.pull
C.pull = lambda ib_, items, settle, oi_cache, want_oi=True: (pulls.append((items[0]["src"], want_oi)), orig(ib_, items, settle, oi_cache, want_oi))[1]
res = C.run_cycle(ib, state, con)
C.pull = orig
assert res is not None and opt_cd(ib) == built_calls, "開盤不該再向 IBKR 查合約清單"
df, summary, per_src, info = res
assert set(info) == {"NQ", "QQQ", "NDX"} and all(i["fresh"] > 0 for i in info.values()), info
assert all(not w for (s_, w) in pulls if s_ in ("QQQ", "NDX")), pulls
# 4) 同一天重啟:全部讀檔,不查合約清單
ib2 = FakeIB(); ib2.open = True; st2 = fresh_state()
C.run_cycle(ib2, st2, con)
assert opt_cd(ib2) == 0, ib2.calls
assert {"NQ", "QQQ", "NDX"} <= set(st2["uni"])
# 5) 日期變了 / 現價漂移太多 → 不用快取
assert C.cache_load("QQQ", DAY + dt.timedelta(days=1), 610.0) is None
assert C.cache_load("QQQ", DAY, 610.0 * 1.03) is None
assert C.cache_load("QQQ", DAY, 610.0 * 1.005) is not None
# 6) 預建失敗不影響 NQ;稍後重試成功
os.remove(C.CACHE_PATH)
ib3 = FakeIB(); ib3.open = True; ib3.fail = {"QQQ"}; st3 = fresh_state()
clock["t"] = dt.datetime(2026, 10, 28, 8, 20, tzinfo=C.NY)
res = C.run_cycle(ib3, st3, con); assert res is not None and "QQQ" not in st3["uni"]
ib3.fail = set()
C.run_cycle(ib3, st3, con); assert "QQQ" in st3["uni"]
# 7) 預抓拿不到 OI → 不能把 oi_last 設成新的(否則開盤整輪合約都 incomplete)
ib4 = FakeIB(); ib4.oi_missing = True; st4 = fresh_state()
os.remove(C.CACHE_PATH)
C.run_cycle(ib4, st4, con)
assert "QQQ" in st4["uni"] and not st4.get("oi_last", {}).get("QQQ")
# 8) 週末、或 08:00 之前:不預建
for t in (dt.datetime(2026, 10, 31, 8, 30, tzinfo=C.NY), dt.datetime(2026, 10, 28, 7, 30, tzinfo=C.NY)):
    clock["t"] = t; ib5 = FakeIB(); st5 = fresh_state()
    try:
        C.run_cycle(ib5, st5, con)
    except Exception:
        pass
    assert "QQQ" not in st5["uni"], t
print("collector prebuild ok")
