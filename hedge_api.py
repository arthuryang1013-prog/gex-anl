"""hedge_api.py — 「對沖模擬」頁面的 API 層(gex_web_server 只負責轉送,邏輯都在這裡,方便單獨測試)。

GET  /api/hedge_accounts            帳戶清單 + 資料可回放的範圍 + 預設起點
GET  /api/hedge_state?id=           帳戶畫面資料(持倉、Greeks、損益拆解、曲線、成交)。即時帳戶會先補算到最新一根 K 棒
GET  /api/hedge_chain?id=&expiry=   該帳戶「現在」的選擇權鏈(理論價、Delta、Gamma、Theta、Vega、IV)
POST /api/hedge/<action>            create / delete / reset / open / close / hedge / params / step / compare(要登入、要 JSON)
"""
import datetime as dt
import threading
import time
from zoneinfo import ZoneInfo

import hedge_sim as HS

NY = ZoneInfo("America/New_York")


def _default_start(rng):
    """資料範圍內最後一個美東 09:30。沒有 K 棒就回 None。"""
    hi = rng.get("bar_hi")
    if not hi:
        return None
    d = dt.datetime.fromtimestamp(hi - 60, NY)
    start = d.replace(hour=9, minute=30, second=0, microsecond=0)
    if start > d:
        start -= dt.timedelta(days=1)
    t = int(start.timestamp())
    return max(t, rng.get("bar_lo") or t)


class HedgeApi:
    def __init__(self, db_path, store_path, market=None):
        self.market = market or HS.DbMarket(db_path)
        self.store = HS.Store(store_path)
        self._locks = {}
        self._glock = threading.Lock()

    def _lock(self, aid):
        with self._glock:
            return self._locks.setdefault(int(aid), threading.RLock())

    @staticmethod
    def _aid(q):
        try:
            return int(q.get("id"))
        except (TypeError, ValueError):
            raise HS.SimError("缺少帳戶 id")

    def _sim(self, aid):
        acc = self.store.load(aid)
        sim = HS.Sim(self.market, acc["params"], acc["state"])
        return acc, sim

    def _catch_up(self, acc, sim):
        """即時帳戶:補算到資料庫裡最新的 K 棒。"""
        if acc["mode"] == "live" and sim.s["clock_t"] is not None:
            hi = self.market.data_range().get("bar_hi")
            if hi and hi > sim.s["clock_t"]:
                sim.advance(hi)

    # ---------------- GET ----------------
    def get(self, name, q):
        if name == "hedge_accounts":
            rng = self.market.data_range()
            return dict(accounts=self.store.list(), range=rng, default_start=_default_start(rng), now=int(time.time()),
                        limits=dict(max_legs=HS.MAX_LEGS, max_qty=HS.MAX_QTY), defaults=HS.DEFAULT_PARAMS)
        aid = self._aid(q)
        with self._lock(aid):
            acc, sim = self._sim(aid)
            if name == "hedge_state":
                self._catch_up(acc, sim)
                if sim.new_points or sim.new_trades:
                    self.store.save(aid, sim)
                v = sim.view()
                v.update(id=aid, name=acc["name"], mode=acc["mode"], start_t=acc["start_t"],
                         points=self.store.points(aid), trades=self.store.trades(aid, 200), range=self.market.data_range())
                return v
            if name == "hedge_chain":
                self._catch_up(acc, sim)
                if sim.new_points:
                    self.store.save(aid, sim)
                if sim.s["clock_t"] is None:
                    raise HS.SimError("帳戶還沒開始")
                w = q.get("width")
                return HS.chain_view(self.market, sim.s["clock_t"], q.get("expiry") or None, float(w) if w else None)
        raise HS.SimError("unknown")

    # ---------------- POST ----------------
    def post(self, action, body):
        body = body if isinstance(body, dict) else {}
        if action == "create":
            mode = body.get("mode")
            if mode not in ("replay", "live"):
                raise HS.SimError("mode 要是 replay 或 live")
            params = HS.clean_params(body.get("params"))
            rng = self.market.data_range()
            if mode == "live":
                start = rng.get("bar_hi")
                if not start:
                    raise HS.SimError("資料庫沒有 K 棒")
            else:
                try:
                    start = int(body.get("start_t") or _default_start(rng))
                except (TypeError, ValueError):
                    raise HS.SimError("start_t 不合法")
            sim = HS.Sim(self.market, params)
            sim.start(start)
            name = str(body.get("name") or ("即時模擬" if mode == "live" else "歷史回放"))[:40]
            aid = self.store.create(name, mode, sim.s["clock_t"], sim.params, sim.s, int(time.time()))
            self.store.save(aid, sim)
            return dict(id=aid)
        if action == "compare":
            try:
                a, b = int(body["start_t"]), int(body["end_t"])
            except (KeyError, TypeError, ValueError):
                raise HS.SimError("start_t / end_t 不合法")
            return dict(results=HS.compare(self.market, body.get("legs") or [], a, b, body.get("configs") or [],
                                           HS.clean_params(body.get("params"))))
        aid = self._aid(body)
        with self._lock(aid):
            if action == "delete":
                self.store.delete(aid)
                return dict(ok=True)
            acc, sim = self._sim(aid)
            if action == "reset":
                rng = self.market.data_range()
                start = int(body.get("start_t") or acc["start_t"])
                if acc["mode"] == "live":
                    start = rng.get("bar_hi") or start
                sim = HS.Sim(self.market, sim.params)
                sim.start(start)
                self.store.reset(aid, sim.s, sim.s["clock_t"])
                return dict(ok=True)
            self._catch_up(acc, sim)
            if action == "open":
                sim.open_legs(body.get("legs"), note=str(body.get("note") or "")[:40])
            elif action == "close":
                ids = body.get("leg_ids")
                sim.close_legs(leg_ids=[int(i) for i in ids] if isinstance(ids, list) else None, group=body.get("group"))
            elif action == "hedge":
                if sim.hedge_now(target=body.get("target"), units=body.get("units")) is None:
                    self.store.save(aid, sim, sim.params)
                    return dict(ok=True, note="淨 Delta 已經在目標附近(不到 1 口),不需要對沖")
            elif action == "params":
                sim.params = HS.clean_params(body.get("params"), base=sim.params)
            elif action == "step":
                if acc["mode"] == "live":
                    raise HS.SimError("即時帳戶由資料推進,不能手動快轉")
                mins = max(1, min(int(body.get("minutes") or 1), 1440))
                until = sim.s["clock_t"] + mins * 60
                if body.get("until_close"):
                    d = dt.datetime.fromtimestamp(sim.s["clock_t"], NY)
                    until = int(d.replace(hour=16, minute=0, second=0, microsecond=0).timestamp())
                    if until <= sim.s["clock_t"]:
                        until = sim.s["clock_t"] + 60
                n, _ = sim.advance(until)
                if n == 0:
                    self.store.save(aid, sim)
                    return dict(ok=True, advanced=0, note="已經到資料的最後了(沒有更新的 K 棒)")
            else:
                raise HS.SimError("unknown")
            self.store.save(aid, sim, sim.params)
            return dict(ok=True)
