"""K 棒看門狗:即時訂閱死了(收到 10182 或開市時間內太久沒更新)時,_bars_session 要丟出例外,讓外層重連。用假的 ib_async。"""
import asyncio, datetime as dt, os, sqlite3, sys, tempfile, types
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NY = ZoneInfo("America/New_York")


class Ev:
    def __init__(self): self.h = []
    def __iadd__(self, f): self.h.append(f); return self
    def fire(self, *a): [f(*a) for f in self.h]


class Bars(list):
    def __init__(self, rows): super().__init__(rows); self.updateEvent = Ev()


class Bar:
    def __init__(self, t): self.date = dt.datetime.fromtimestamp(t, dt.timezone.utc); self.open = self.high = self.low = self.close = 100.0; self.volume = 5


class Det:
    def __init__(self): self.contract = types.SimpleNamespace(lastTradeDateOrContractMonth="20261218", localSymbol="NQZ6")


class IB:
    inst = None
    def __init__(self): self.errorEvent = Ev(); self._c = False; IB.inst = self
    async def connectAsync(self, *a, **k): self._c = True
    def isConnected(self): return self._c
    def disconnect(self): self._c = False
    async def reqContractDetailsAsync(self, *a, **k): return [Det()]
    async def reqHistoricalDataAsync(self, *a, **k):
        self.bars = Bars([Bar(1_700_000_000 + 60 * i) for i in range(3)]); return self.bars


mod = types.ModuleType("ib_async"); mod.IB = IB; mod.Future = lambda *a, **k: None; sys.modules["ib_async"] = mod
import gex_web_server as G

# 1) 休市時間判斷
def ny(y, m, d, h, mi): return dt.datetime(y, m, d, h, mi, tzinfo=NY)
assert G.bars_market_open(ny(2026, 10, 29, 9, 56))            # 週四盤中
assert not G.bars_market_open(ny(2026, 10, 29, 17, 30))       # 每日休市 17:00–18:00
assert G.bars_market_open(ny(2026, 10, 29, 18, 0))
assert not G.bars_market_open(ny(2026, 10, 30, 17, 1))        # 週五 17:00 後
assert not G.bars_market_open(ny(2026, 10, 31, 12, 0))        # 週六
assert not G.bars_market_open(ny(2026, 11, 1, 17, 59))        # 週日 18:00 前
assert G.bars_market_open(ny(2026, 11, 1, 18, 0))
assert G.bars_market_open(ny(2026, 10, 29, 0, 5))             # 夜間重置後仍開市(00:00 ET)

d = tempfile.mkdtemp(); db = os.path.join(d, "t.sqlite")
c = sqlite3.connect(db); c.execute("CREATE TABLE bars (t INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL)"); c.commit(); c.close()
G._meta_set = lambda *a, **k: None

async def run(coro, timeout=8):
    return await asyncio.wait_for(coro, timeout)

# 2) 收到 10182 → 丟例外
async def t_err():
    task = asyncio.ensure_future(G._bars_session(db)); await asyncio.sleep(0.5)
    IB.inst.errorEvent.fire(4, 10182, "Failed to request live updates (disconnected).", None)
    try:
        await run(task); return None
    except RuntimeError as e: return str(e)
r = asyncio.run(t_err()); print("10182 →", r); assert r and "10182" in r

# 3) 開市時間內太久沒更新 → 丟例外;休市時不丟
G.BARS_STALE_SEC = 1
async def t_stale(open_):
    G.bars_market_open = lambda now: open_
    task = asyncio.ensure_future(G._bars_session(db))
    await asyncio.sleep(4.5)
    if open_:
        try: await run(task, 3); return None
        except RuntimeError as e: return str(e)
    alive = not task.done(); IB.inst.disconnect(); await asyncio.sleep(2.5); return alive
r = asyncio.run(t_stale(True)); print("stale →", r); assert r and "no bar update" in r
alive = asyncio.run(t_stale(False)); print("休市時不重連:", alive); assert alive is True

# 4) 有更新就不會誤判
async def t_ok():
    G.BARS_STALE_SEC = 5
    G.bars_market_open = lambda now: True
    task = asyncio.ensure_future(G._bars_session(db)); await asyncio.sleep(0.5)
    for _ in range(5):
        await asyncio.sleep(0.8); IB.inst.bars.updateEvent.fire(IB.inst.bars, True)
    alive = not task.done(); IB.inst.disconnect(); await asyncio.sleep(2.5); return alive
assert asyncio.run(t_ok()) is True
print("bars watchdog ok")
