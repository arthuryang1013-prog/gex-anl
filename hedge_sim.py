"""hedge_sim.py — NQ 期貨 + NQ 期貨選擇權的「模擬對沖」引擎(紙上交易,不連券商、不下真單)。

資料來源(都是現有資料,沒有新增收集):
  - nq_gex.sqlite 的 snapshots / contracts:每約 3 分鐘一張快照,每檔有履約價、IV、F、T、OI、成交量。
  - nq_gex.sqlite 的 bars:1 分 K(收盤價當作當時的 NQ 價格)。
重要限制(模擬與真實的差距,頁面上也會寫):
  1. 資料庫沒有存買賣報價,所以選擇權價格 = 用快照的 IV 代入 Black-76 算出的「理論價」,成交價 = 理論價 ± 假設的半個價差(可調)。
  2. IV 用「同履約價固定」(sticky strike):價格動了,該履約價的 IV 仍取最新快照裡同一檔的 IV,不另外模擬曲面移動。
  3. 到期一律以內含價值現金結算(真實的 NQ 選擇權會轉成期貨部位)。到期時刻取自快照的 T 推算,推不出來就用到期日美東 16:00。
  4. 沒有模擬保證金、強制平倉、提前履約、停損單、跳空成交與流動性不足。
  5. 手續費與滑價是估計值,請改成你券商的實際費用。
損益拆解:每個步驟用「步驟開始時的 Greeks」把選擇權損益拆成 Delta / Gamma / Theta / Vega / 其他(高階項與曲面變動),
  期貨對沖損益另算;成本 = 手續費 + 滑價(含選擇權半個價差)。總損益 = 各項加總(測試會逐步驗證與現金帳一致)。
"""
import bisect
import copy
import datetime as dt
import json
import math
import sqlite3
import threading
from zoneinfo import ZoneInfo

import hedge_pricing as HP

NY = ZoneInfo("America/New_York")
OPT_MULT = HP.NQ_MULT           # 選擇權每點 20 美元
OPT_TICK = 0.05
MIN_PRICE = 0.05
MAX_LEGS = 12
MAX_QTY = 200
MAX_STEPS_PER_CALL = 3000

DEFAULT_PARAMS = dict(
    fut_mult=20,            # 對沖用期貨:20 = NQ,2 = MNQ(微型)
    auto=False,             # 自動對沖開關
    threshold=0.5,          # 自動對沖門檻:淨 Delta 的絕對值(以 NQ 口數計)達到就對沖
    target=0.0,             # 對沖後想把淨 Delta 抵到多少(NQ 口數)
    cooldown_min=0,         # 兩次自動對沖之間至少隔幾分鐘
    opt_half_spread=1.0,    # 選擇權假設的半個買賣價差(點);買進 = 理論價 + 這個數,賣出 = 理論價 − 這個數
    opt_fee=2.0,            # 選擇權每口每邊手續費(美元,估計)
    fut_fee_nq=2.0,         # NQ 期貨每口每邊手續費(美元,估計)
    fut_fee_mnq=0.6,        # MNQ 期貨每口每邊手續費(美元,估計)
    fut_slip=0.5,           # 期貨市價單滑價(點,買貴賣便宜)
)
PARAM_RANGES = dict(
    fut_mult=(2, 20), threshold=(0.0, 50.0), target=(-50.0, 50.0), cooldown_min=(0, 600),
    opt_half_spread=(0.0, 50.0), opt_fee=(0.0, 50.0), fut_fee_nq=(0.0, 50.0), fut_fee_mnq=(0.0, 50.0), fut_slip=(0.0, 20.0),
)


class SimError(Exception):
    """給使用者看的錯誤(輸入不合法、沒有資料…)。"""


def _tick_up(x):
    return math.ceil(x / OPT_TICK - 1e-9) * OPT_TICK


def _tick_down(x):
    return math.floor(x / OPT_TICK + 1e-9) * OPT_TICK


def clean_params(p, base=None):
    out = dict(DEFAULT_PARAMS if base is None else base)
    for k, v in (p or {}).items():
        if k not in DEFAULT_PARAMS:
            continue
        if k == "auto":
            out[k] = bool(v)
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise SimError(f"參數 {k} 必須是數字")
        if not math.isfinite(v):
            raise SimError(f"參數 {k} 不是有效數字")
        lo, hi = PARAM_RANGES[k]
        if not lo <= v <= hi:
            raise SimError(f"參數 {k} 必須在 {lo} 到 {hi} 之間")
        out[k] = int(v) if k in ("fut_mult", "cooldown_min") else v
    if out["fut_mult"] not in (2, 20):
        raise SimError("fut_mult 只能是 20(NQ)或 2(MNQ)")
    return out


# ----------------------------- 市場資料(資料庫版) -----------------------------
def _iso(epoch):
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).isoformat(timespec="seconds")


def _epoch(ts):
    return int(dt.datetime.fromisoformat(ts).timestamp())


class DbMarket:
    """從 nq_gex.sqlite 讀 NQ 選擇權快照與 1 分 K。只讀。測試時可用同介面的假物件取代。"""

    def __init__(self, db_path, connect=None):
        self.db_path = db_path
        self._connect = connect or (lambda p: sqlite3.connect(p, timeout=10))
        self._rows_cache = {}
        self._lock = threading.Lock()

    def _con(self):
        c = self._connect(self.db_path)
        try:
            c.execute("PRAGMA busy_timeout=8000")
        except Exception:
            pass
        return c

    def snap_at(self, t):
        """最新一張 snap_t <= t 的 NQ 快照 → dict(snap_t, spot, rows{(expiry,strike,cp): row})。沒有就回 None。"""
        con = self._con()
        try:
            r = con.execute("SELECT ts, spot FROM snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (_iso(t),)).fetchone()
            if not r or r[1] is None:
                return None
            ts, spot = r
            with self._lock:
                rows = self._rows_cache.get(ts)
            if rows is None:
                cols = {x[1] for x in con.execute("PRAGMA table_info(contracts)")}
                srcw = "COALESCE(src,'NQ')='NQ'" if "src" in cols else "1=1"
                volc = "vol" if "vol" in cols else "NULL"
                rows = {}
                for ex, dte, k, cp, oi, iv, F, T, stale, vol in con.execute(
                        f"SELECT expiry, dte, strike, cp, oi, iv, F, T, stale, {volc} FROM contracts WHERE ts=? AND {srcw}", (ts,)):
                    if k is None or cp not in ("C", "P"):
                        continue
                    rows[(str(ex), float(k), cp)] = dict(expiry=str(ex), dte=dte, strike=float(k), cp=cp, oi=oi, iv=iv, F=F, T=T,
                                                         stale=int(stale or 0), vol=vol)
                with self._lock:
                    if len(self._rows_cache) > 40:
                        self._rows_cache.clear()
                    self._rows_cache[ts] = rows
            return dict(snap_t=_epoch(ts), spot=float(spot), rows=rows)
        finally:
            con.close()

    def bars(self, lo, hi, limit=None):
        """[(bar_start, close)],lo <= bar_start < hi,依時間排序。"""
        con = self._con()
        try:
            q = "SELECT t, c FROM bars WHERE t >= ? AND t < ? AND c IS NOT NULL ORDER BY t"
            if limit:
                q += f" LIMIT {int(limit)}"
            return [(int(t), float(c)) for t, c in con.execute(q, (int(lo), int(hi)))]
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()

    def bar_close_before(self, t):
        """收盤時間(bar_start+60) <= t 的最後一根 K 棒 → (收盤時間, 收盤價) 或 None。"""
        con = self._con()
        try:
            r = con.execute("SELECT t, c FROM bars WHERE t <= ? AND c IS NOT NULL ORDER BY t DESC LIMIT 1", (int(t) - 60,)).fetchone()
            return (int(r[0]) + 60, float(r[1])) if r else None
        except sqlite3.OperationalError:
            return None
        finally:
            con.close()

    def data_range(self):
        con = self._con()
        try:
            s = con.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
            try:
                b = con.execute("SELECT MIN(t), MAX(t) FROM bars").fetchone()
            except sqlite3.OperationalError:
                b = (None, None)
            return dict(snap_lo=_epoch(s[0]) if s and s[0] else None, snap_hi=_epoch(s[1]) if s and s[1] else None,
                        bar_lo=int(b[0]) if b and b[0] else None, bar_hi=int(b[1]) + 60 if b and b[1] else None)
        finally:
            con.close()


def expiry_epoch_from(row, snap_t):
    """某檔合約的到期時刻(epoch 秒)。快照的 T 若沒被收集器的 1 小時下限截斷,就由 T 反推;否則用到期日美東 16:00。"""
    T = row.get("T")
    if T is not None and T > HP.MIN_T * 1.001:
        return int(round(snap_t + T * HP.YEAR_SEC))
    d = dt.datetime.strptime(str(row["expiry"]), "%Y%m%d").replace(hour=16, tzinfo=NY)
    return int(d.timestamp())


def chain_view(market, t, expiry=None, width=None):
    """選擇權鏈(給下單面板):該時刻最新快照裡,每個到期日、每個履約價的 Call/Put 理論價與 Greeks。"""
    snap = market.snap_at(t)
    if not snap:
        raise SimError("這個時間點之前沒有選擇權快照")
    last = market.bar_close_before(t)
    F_ref = last[1] if last else snap["spot"]
    exps = {}
    for (ex, k, cp), r in snap["rows"].items():
        e = exps.setdefault(ex, dict(expiry=ex, dte=r["dte"], n=0))
        e["n"] += 1
    expiries = sorted(exps.values(), key=lambda e: e["expiry"])
    out = dict(snap_t=snap["snap_t"], spot=snap["spot"], ref=F_ref, expiries=expiries, strikes=[])
    if expiry is None and expiries:
        expiry = expiries[0]["expiry"]
    out["expiry"] = expiry
    if expiry is None:
        return out
    w = width or 800.0
    by_k = {}
    for (ex, k, cp), r in snap["rows"].items():
        if ex != expiry or abs(k - F_ref) > w:
            continue
        exp_ep = expiry_epoch_from(r, snap["snap_t"])
        T = max((exp_ep - t) / HP.YEAR_SEC, 60 / HP.YEAR_SEC)
        iv = r["iv"]
        Fk = (r["F"] if r["F"] else snap["spot"]) + (F_ref - snap["spot"])
        g = HP.greeks(Fk, k, T, iv, cp) if iv and iv > 0 else None
        d = by_k.setdefault(k, dict(strike=k))
        d[cp] = dict(iv=iv, oi=r["oi"], vol=r["vol"], stale=r["stale"],
                     price=(round(g["price"], 2) if g else None), delta=(round(g["delta"], 4) if g else None),
                     gamma=(round(g["gamma"], 6) if g else None), theta=(round(g["theta"], 2) if g else None),
                     vega=(round(g["vega"], 2) if g else None))
        out["expiry_epoch"] = exp_ep
    out["strikes"] = [by_k[k] for k in sorted(by_k)]
    return out


# ----------------------------- 模擬器 -----------------------------
def new_state():
    return dict(clock_t=None, last_c=None, cash_opt=0.0, fut_qty=0, fut_avg=0.0, fut_realized=0.0, fees=0.0,
                slip_opt=0.0, slip_fut=0.0, cum=dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, resid=0.0, fut=0.0),
                legs=[], next_leg_id=1, n_hedges=0, n_auto=0, last_auto_t=0, iv_stale_steps=0, steps=0, no_data_steps=0)


class Sim:
    """一個模擬帳戶。provider 要有 snap_at(t) / bars(lo,hi) / bar_close_before(t)。
    所有會改變帳戶的動作都把成交寫進 self.new_trades、每個推進步驟寫進 self.new_points,由外面的儲存層負責落地。"""

    def __init__(self, market, params=None, state=None):
        self.m = market
        self.params = clean_params(params)
        self.s = copy.deepcopy(state) if state else new_state()
        self.new_trades = []
        self.new_points = []

    # ---------- 工具 ----------
    @property
    def ratio(self):
        """1 口對沖期貨相當於幾口 NQ(NQ=1、MNQ=0.1)。"""
        return self.params["fut_mult"] / HP.NQ_MULT

    def _fut_fee(self):
        return self.params["fut_fee_nq"] if self.params["fut_mult"] == 20 else self.params["fut_fee_mnq"]

    def _log(self, kind, **kw):
        rec = dict(t=self.s["clock_t"], kind=kind, **kw)
        self.new_trades.append(rec)
        return rec

    def start(self, start_t):
        """設定起點。起點取 start_t 以前最後一根收盤的 K 棒(這樣一開始就有價格)。"""
        bc = self.m.bar_close_before(int(start_t))
        if not bc:
            raise SimError("這個時間點之前沒有 1 分 K 資料,無法開始")
        snap = self.m.snap_at(bc[0])
        if not snap:
            raise SimError("這個時間點之前沒有選擇權快照,無法開始")
        self.s["clock_t"], self.s["last_c"] = bc
        return bc

    def _need_started(self):
        if self.s["clock_t"] is None:
            raise SimError("帳戶還沒開始(沒有起點)")

    def _leg_market(self, leg, t, c, snap):
        """某腿在時刻 t、NQ 價格 c 的 (F, iv, T, stale 旗標)。找不到同一檔就沿用上一次的 IV。"""
        row = snap["rows"].get((leg["expiry"], leg["strike"], leg["cp"])) if snap else None
        used_stale = False
        if row and row["iv"] and row["iv"] > 0:
            iv = row["iv"]
            F = (row["F"] if row["F"] else snap["spot"]) + (c - snap["spot"])
            if row["stale"]:
                used_stale = True
        else:
            iv = leg["iv0"]
            F = leg["F0"] + (c - leg.get("c0", c))
            used_stale = True
        T = max((leg["expiry_epoch"] - t) / HP.YEAR_SEC, 60 / HP.YEAR_SEC)
        return F, iv, T, used_stale

    def _greeks_now(self, leg, t, c, snap):
        F, iv, T, stale = self._leg_market(leg, t, c, snap)
        g = HP.greeks(F, leg["strike"], T, iv, leg["cp"])
        return F, iv, g, stale

    # ---------- 淨部位 ----------
    def totals(self, t=None, c=None):
        """目前淨 Delta(NQ 口數)與各項 Greeks 加總(美元)。用步驟結束時存的 g0。"""
        d = sum(l["qty"] * l["g0"]["delta"] for l in self.s["legs"])
        opt_delta = d
        fut_delta = self.s["fut_qty"] * self.ratio
        gamma = sum(l["qty"] * l["g0"]["gamma"] for l in self.s["legs"])           # 每動 1 點,Delta(口)變多少
        theta = sum(l["qty"] * l["g0"]["theta"] * OPT_MULT for l in self.s["legs"])  # 美元/日曆日
        vega = sum(l["qty"] * l["g0"]["vega"] * OPT_MULT for l in self.s["legs"])    # 美元/波動率 1 點
        mtm = sum(l["qty"] * l["g0"]["price"] * OPT_MULT for l in self.s["legs"])
        return dict(net_delta=opt_delta + fut_delta, opt_delta=opt_delta, fut_delta=fut_delta, gamma=gamma, theta=theta, vega=vega, opt_mtm=mtm)

    def equity(self):
        """用現金帳算的總損益(跟拆解法互相驗證)。"""
        s, c = self.s, self.s["last_c"]
        fut_unreal = s["fut_qty"] * self.params["fut_mult"] * (c - s["fut_avg"]) if s["fut_qty"] else 0.0
        return s["cash_opt"] + self.totals()["opt_mtm"] + s["fut_realized"] + fut_unreal - s["fees"]

    def components(self):
        s = self.s
        cum = dict(s["cum"])
        cum["cost"] = -(s["fees"] + s["slip_opt"] + s["slip_fut"])
        cum["total"] = cum["delta"] + cum["gamma"] + cum["theta"] + cum["vega"] + cum["resid"] + cum["fut"] + cum["cost"]
        return cum

    # ---------- 期貨成交 ----------
    def _fut_trade(self, units, c, kind, note=""):
        """在 NQ 價格 c 成交 units 口期貨(正=買,負=賣)。成交價含滑價,更新均價與已實現損益、手續費。"""
        if units == 0:
            return None
        s, p = self.s, self.params
        slip = p["fut_slip"]
        fill = c + (slip if units > 0 else -slip)
        mult = p["fut_mult"]
        q0 = s["fut_qty"]
        q1 = q0 + units
        if q0 == 0 or (q0 > 0) == (units > 0):
            s["fut_avg"] = (s["fut_avg"] * abs(q0) + fill * abs(units)) / abs(q1)
        else:
            closed = min(abs(units), abs(q0))
            s["fut_realized"] += closed * mult * (fill - s["fut_avg"]) * (1 if q0 > 0 else -1)
            if abs(units) > abs(q0):          # 反向超過:剩下的是新倉
                s["fut_avg"] = fill
            elif q1 == 0:
                s["fut_avg"] = 0.0
        s["fut_qty"] = q1
        fee = abs(units) * self._fut_fee()
        s["fees"] += fee
        s["slip_fut"] += abs(units) * mult * slip
        return self._log("fut", note=note or kind, units=units, price=round(fill, 2), ref=round(c, 2), fee=round(fee, 2),
                         slip_cost=round(abs(units) * mult * slip, 2), fut_after=q1, instrument="NQ" if mult == 20 else "MNQ")

    def hedge_now(self, target=None, units=None, kind="manual"):
        """把淨 Delta 抵到 target(NQ 口數;預設用參數 target)。units 指定就直接買賣那麼多口。回傳成交紀錄或 None。"""
        self._need_started()
        s, c = self.s, self.s["last_c"]
        if units is None:
            tgt = self.params["target"] if target is None else float(target)
            net = self.totals()["net_delta"]
            units = int(round((tgt - net) / self.ratio))
        units = int(units)
        if units == 0:
            return None
        if abs(units) * self.ratio > 200:
            raise SimError("單次對沖口數太大")
        rec = self._fut_trade(units, c, kind, note=("手動對沖" if kind == "manual" else "自動對沖" if kind == "auto" else kind))
        s["n_hedges"] += 1
        if kind == "auto":
            s["n_auto"] += 1
            s["last_auto_t"] = s["clock_t"]
        return rec

    # ---------- 選擇權成交 ----------
    def _opt_fill(self, model, qty):
        hs = self.params["opt_half_spread"]
        if qty > 0:
            return max(MIN_PRICE, _tick_up(model + hs))
        return max(MIN_PRICE, _tick_down(model - hs))

    def open_legs(self, legs, group=None, note=""):
        """legs: [{expiry, strike, cp, qty}](qty 正 = 買進、負 = 賣出),在目前時刻、目前價格,以理論價 ± 半個價差成交。"""
        self._need_started()
        s = self.s
        if not legs or not isinstance(legs, list):
            raise SimError("沒有要建立的腿")
        if len(s["legs"]) + len(legs) > MAX_LEGS:
            raise SimError(f"最多同時持有 {MAX_LEGS} 條腿")
        t, c = s["clock_t"], s["last_c"]
        snap = self.m.snap_at(t)
        if not snap:
            raise SimError("目前時間點之前沒有選擇權快照")
        prepared = []
        for L in legs:
            try:
                ex, k, cp, q = str(L["expiry"]), float(L["strike"]), str(L["cp"]).upper()[:1], int(L["qty"])
            except (KeyError, TypeError, ValueError):
                raise SimError("每條腿都要有 expiry、strike、cp、qty")
            if cp not in ("C", "P") or q == 0 or abs(q) > MAX_QTY:
                raise SimError(f"腿的內容不合法:{L}")
            row = snap["rows"].get((ex, k, cp))
            if not row or not row["iv"] or row["iv"] <= 0:
                raise SimError(f"最新快照裡沒有這一檔(或沒有 IV):{ex} {k:g} {cp}")
            exp_ep = expiry_epoch_from(row, snap["snap_t"])
            if exp_ep <= t:
                raise SimError(f"這個到期日已經到期:{ex}")
            prepared.append((ex, k, cp, q, row, exp_ep))
        gid = group or f"g{s['next_leg_id']}"
        for ex, k, cp, q, row, exp_ep in prepared:
            leg = dict(id=s["next_leg_id"], expiry=ex, strike=k, cp=cp, qty=q, expiry_epoch=exp_ep, group=gid, open_t=t)
            s["next_leg_id"] += 1
            leg.update(F0=0.0, iv0=row["iv"], t0=t, c0=c)
            F, iv, g, stale = self._greeks_now(dict(leg, F0=(row["F"] or snap["spot"]) + (c - snap["spot"])), t, c, snap)
            leg.update(F0=F, iv0=iv, g0=g, c0=c)
            fill = self._opt_fill(g["price"], q)
            slip_pts = (fill - g["price"]) if q > 0 else (g["price"] - fill)
            fee = abs(q) * self.params["opt_fee"]
            s["cash_opt"] -= q * OPT_MULT * fill
            s["fees"] += fee
            s["slip_opt"] += slip_pts * abs(q) * OPT_MULT
            leg["open_price"] = fill
            leg["open_model"] = g["price"]
            s["legs"].append(leg)
            self._log("opt_open", note=note or "建倉", group=gid, leg_id=leg["id"], expiry=ex, strike=k, cp=cp, qty=q,
                      price=round(fill, 2), model=round(g["price"], 2), iv=round(iv, 4), fee=round(fee, 2),
                      slip_cost=round(slip_pts * abs(q) * OPT_MULT, 2), delta=round(g["delta"], 4))
        return gid

    def close_legs(self, leg_ids=None, group=None, note="平倉"):
        self._need_started()
        s, t, c = self.s, self.s["clock_t"], self.s["last_c"]
        sel = [l for l in s["legs"] if (leg_ids and l["id"] in leg_ids) or (group and l["group"] == group)]
        if not sel:
            raise SimError("找不到要平倉的腿")
        snap = self.m.snap_at(t)
        for leg in sel:
            F, iv, g, stale = self._greeks_now(leg, t, c, snap)
            q = leg["qty"]
            fill = self._opt_fill(g["price"], -q)
            slip_pts = (fill - g["price"]) if -q > 0 else (g["price"] - fill)
            fee = abs(q) * self.params["opt_fee"]
            s["cash_opt"] += q * OPT_MULT * fill
            s["fees"] += fee
            s["slip_opt"] += slip_pts * abs(q) * OPT_MULT
            pnl = q * OPT_MULT * (fill - leg["open_price"])
            s["legs"].remove(leg)
            self._log("opt_close", note=note, group=leg["group"], leg_id=leg["id"], expiry=leg["expiry"], strike=leg["strike"], cp=leg["cp"],
                      qty=-q, price=round(fill, 2), model=round(g["price"], 2), fee=round(fee, 2),
                      slip_cost=round(slip_pts * abs(q) * OPT_MULT, 2), leg_pnl=round(pnl - fee, 2))
        return len(sel)

    # ---------- 推進時間 ----------
    def _step(self, bar_start, c):
        """處理一根 K 棒(狀態時間 = bar_start + 60,價格 = 收盤)。回傳 True 表示有處理。"""
        s, p = self.s, self.params
        t1 = bar_start + 60
        t0, c0 = s["clock_t"], s["last_c"]
        snap = self.m.snap_at(t1)
        if not snap:
            s["no_data_steps"] += 1
            return False
        dtd = (t1 - t0) / 86400.0
        stale_any = False
        expired = []
        for leg in list(s["legs"]):
            g0, F0, iv0 = leg["g0"], leg["F0"], leg["iv0"]
            if t1 >= leg["expiry_epoch"]:
                F1 = F0 + (c - c0)
                inner = HP.intrinsic(F1, leg["strike"], leg["cp"])
                dV = inner - g0["price"]
                dF = F1 - F0
                parts = dict(delta=g0["delta"] * dF, gamma=0.5 * g0["gamma"] * dF * dF, theta=g0["theta"] * dtd, vega=0.0)
                parts["resid"] = dV - sum(parts.values())
                expired.append((leg, inner, parts))
                continue
            F1, iv1, T1, stale = self._leg_market(leg, t1, c, snap)
            g1 = HP.greeks(F1, leg["strike"], T1, iv1, leg["cp"])
            dF, dIv = F1 - F0, (iv1 - iv0) * 100.0
            dV = g1["price"] - g0["price"]
            parts = dict(delta=g0["delta"] * dF, gamma=0.5 * g0["gamma"] * dF * dF, theta=g0["theta"] * dtd, vega=g0["vega"] * dIv)
            parts["resid"] = dV - sum(parts.values())
            for k, v in parts.items():
                s["cum"][k] += leg["qty"] * OPT_MULT * v
            leg.update(F0=F1, iv0=iv1, g0=g1, c0=c, t0=t1)
            stale_any = stale_any or stale
        for leg, inner, parts in expired:
            for k, v in parts.items():
                s["cum"][k] += leg["qty"] * OPT_MULT * v
            s["cash_opt"] += leg["qty"] * OPT_MULT * inner
            s["legs"].remove(leg)
            s["clock_t"], s["last_c"] = t1, c
            self._log("opt_expire", note="到期以內含價值現金結算", group=leg["group"], leg_id=leg["id"], expiry=leg["expiry"],
                      strike=leg["strike"], cp=leg["cp"], qty=leg["qty"], price=round(inner, 2),
                      leg_pnl=round(leg["qty"] * OPT_MULT * (inner - leg["open_price"]), 2))
        s["cum"]["fut"] += s["fut_qty"] * p["fut_mult"] * (c - c0)
        s["clock_t"], s["last_c"] = t1, c
        s["steps"] += 1
        if stale_any:
            s["iv_stale_steps"] += 1
        # 自動對沖
        if p["auto"] and (s["legs"] or s["fut_qty"]):
            net = self.totals()["net_delta"]
            cool = (t1 - s["last_auto_t"]) >= p["cooldown_min"] * 60
            if abs(net - p["target"]) >= p["threshold"] and cool and s["legs"]:
                self.hedge_now(kind="auto")
        self._point(snap)
        return True

    def _point(self, snap):
        s = self.s
        tot = self.totals()
        comp = self.components()
        eq = self.equity()
        self.new_points.append(dict(
            t=s["clock_t"], c=round(s["last_c"], 2), equity=round(eq, 2), net_delta=round(tot["net_delta"], 4),
            opt_delta=round(tot["opt_delta"], 4), fut_qty=s["fut_qty"], gamma=round(tot["gamma"], 6), theta=round(tot["theta"], 2),
            vega=round(tot["vega"], 2), opt_mtm=round(tot["opt_mtm"], 2), n_legs=len(s["legs"]),
            comp={k: round(v, 2) for k, v in comp.items()}, snap_age=int(s["clock_t"] - snap["snap_t"]), hedges=s["n_hedges"]))

    def advance(self, until_t, max_steps=MAX_STEPS_PER_CALL):
        """把時間推進到 until_t(含):逐根處理 K 棒。回傳 (處理幾根, 是否因為沒有更多資料而停下)。"""
        self._need_started()
        s = self.s
        n = 0
        bars = self.m.bars(s["clock_t"] - 60 + 1, int(until_t) - 60 + 1, limit=max_steps + 1)
        bars = [b for b in bars if b[0] + 60 > s["clock_t"]]
        at_end = len(bars) <= max_steps
        for bs, c in bars[:max_steps]:
            if self._step(bs, c):
                n += 1
        return n, at_end and (not bars or True)

    # ---------- 給畫面看的視圖 ----------
    def view(self):
        s, t, c = self.s, self.s["clock_t"], self.s["last_c"]
        legs = []
        snap = self.m.snap_at(t) if t else None
        for l in s["legs"]:
            g = l["g0"]
            legs.append(dict(id=l["id"], group=l["group"], expiry=l["expiry"], strike=l["strike"], cp=l["cp"], qty=l["qty"],
                             open_price=round(l["open_price"], 2), price=round(g["price"], 2), iv=round(l["iv0"], 4),
                             delta=round(g["delta"], 4), gamma=round(g["gamma"], 6), theta=round(g["theta"], 2), vega=round(g["vega"], 2),
                             pnl=round(l["qty"] * OPT_MULT * (g["price"] - l["open_price"]), 2),
                             mins_left=max(0, int((l["expiry_epoch"] - t) / 60)) if t else None))
        tot = self.totals() if t else {}
        fut_unreal = s["fut_qty"] * self.params["fut_mult"] * (c - s["fut_avg"]) if (s["fut_qty"] and c) else 0.0
        return dict(clock_t=t, price=c, params=self.params, legs=legs, totals={k: round(v, 4) for k, v in tot.items()},
                    fut=dict(qty=s["fut_qty"], avg=round(s["fut_avg"], 2), unreal=round(fut_unreal, 2), realized=round(s["fut_realized"], 2),
                             instrument="NQ" if self.params["fut_mult"] == 20 else "MNQ"),
                    equity=round(self.equity(), 2) if t else 0.0, comp={k: round(v, 2) for k, v in self.components().items()},
                    fees=round(s["fees"], 2), slip_opt=round(s["slip_opt"], 2), slip_fut=round(s["slip_fut"], 2),
                    n_hedges=s["n_hedges"], n_auto=s["n_auto"], steps=s["steps"], iv_stale_steps=s["iv_stale_steps"],
                    no_data_steps=s["no_data_steps"], snap_age=(int(t - snap["snap_t"]) if snap else None))


# ----------------------------- 儲存(sqlite) -----------------------------
class Store:
    """帳戶、成交、每步的曲線點存在獨立的 hedge_sim.sqlite(*.sqlite 已被 gitignore)。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        with self._con() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, mode TEXT,
                    created INTEGER, start_t INTEGER, params TEXT, state TEXT);
                CREATE TABLE IF NOT EXISTS trades (account_id INTEGER, seq INTEGER, t INTEGER, js TEXT);
                CREATE INDEX IF NOT EXISTS idx_trades ON trades(account_id, seq);
                CREATE TABLE IF NOT EXISTS points (account_id INTEGER, t INTEGER, js TEXT);
                CREATE INDEX IF NOT EXISTS idx_points ON points(account_id, t);
            """)

    def _con(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("PRAGMA busy_timeout=8000")
        return c

    def list(self):
        with self._con() as c:
            return [dict(id=r[0], name=r[1], mode=r[2], created=r[3], start_t=r[4], clock_t=json.loads(r[5]).get("clock_t"))
                    for r in c.execute("SELECT id, name, mode, created, start_t, state FROM accounts ORDER BY id DESC")]

    def create(self, name, mode, start_t, params, state, now):
        with self.lock, self._con() as c:
            cur = c.execute("INSERT INTO accounts (name, mode, created, start_t, params, state) VALUES (?,?,?,?,?,?)",
                            (name, mode, int(now), int(start_t), json.dumps(params), json.dumps(state)))
            return cur.lastrowid

    def load(self, aid):
        with self._con() as c:
            r = c.execute("SELECT id, name, mode, created, start_t, params, state FROM accounts WHERE id=?", (int(aid),)).fetchone()
        if not r:
            raise SimError("找不到這個帳戶")
        return dict(id=r[0], name=r[1], mode=r[2], created=r[3], start_t=r[4], params=json.loads(r[5]), state=json.loads(r[6]))

    def save(self, aid, sim, params=None):
        with self.lock, self._con() as c:
            c.execute("UPDATE accounts SET state=?, params=? WHERE id=?", (json.dumps(sim.s), json.dumps(params or sim.params), int(aid)))
            seq = (c.execute("SELECT COALESCE(MAX(seq),0) FROM trades WHERE account_id=?", (int(aid),)).fetchone()[0])
            for i, tr in enumerate(sim.new_trades, 1):
                c.execute("INSERT INTO trades VALUES (?,?,?,?)", (int(aid), seq + i, tr.get("t") or 0, json.dumps(tr)))
            for pt in sim.new_points:
                c.execute("INSERT INTO points VALUES (?,?,?)", (int(aid), pt["t"], json.dumps(pt)))
        sim.new_trades, sim.new_points = [], []

    def delete(self, aid):
        with self.lock, self._con() as c:
            for tb in ("trades", "points"):
                c.execute(f"DELETE FROM {tb} WHERE account_id=?", (int(aid),))
            c.execute("DELETE FROM accounts WHERE id=?", (int(aid),))

    def reset(self, aid, state, start_t):
        with self.lock, self._con() as c:
            for tb in ("trades", "points"):
                c.execute(f"DELETE FROM {tb} WHERE account_id=?", (int(aid),))
            c.execute("UPDATE accounts SET state=?, start_t=? WHERE id=?", (json.dumps(state), int(start_t), int(aid)))

    def trades(self, aid, limit=300):
        with self._con() as c:
            rows = c.execute("SELECT js FROM trades WHERE account_id=? ORDER BY seq DESC LIMIT ?", (int(aid), int(limit))).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def points(self, aid, max_points=600):
        with self._con() as c:
            rows = c.execute("SELECT js FROM points WHERE account_id=? ORDER BY t", (int(aid),)).fetchall()
        pts = [json.loads(r[0]) for r in rows]
        if len(pts) > max_points:
            step = (len(pts) - 1) / (max_points - 1)
            keep = sorted({int(round(i * step)) for i in range(max_points)})
            pts = [pts[i] for i in keep]
        return pts


# ----------------------------- 對沖門檻比較(不存檔) -----------------------------
def compare(market, legs, start_t, end_t, configs, base_params=None, max_points=240):
    """同一組部位、同一段時間,用不同的對沖設定各跑一次,回傳比較表。
    configs: [{label, auto(bool), threshold, cooldown_min, ...任何 DEFAULT_PARAMS 的覆蓋值}]。每個設定都從零帳戶開始。"""
    if end_t <= start_t:
        raise SimError("結束時間必須晚於開始時間")
    if end_t - start_t > 3 * 86400:
        raise SimError("比較的區間最長 3 天")
    if not configs or len(configs) > 8:
        raise SimError("比較設定要 1 到 8 組")
    out = []
    for cfg in configs:
        cfg = dict(cfg)
        label = str(cfg.pop("label", "")) or ("自動 ±%g 口" % cfg.get("threshold", 0.5) if cfg.get("auto") else "不對沖")
        sim = Sim(market, clean_params({**(base_params or {}), **cfg}))
        sim.start(start_t)
        sim.open_legs(legs, group="cmp", note="比較用建倉")
        sim.advance(end_t)
        pts = sim.new_points
        eqs = [0.0] + [p["equity"] for p in pts]
        peak, mdd = eqs[0], 0.0
        for e in eqs:
            peak = max(peak, e)
            mdd = max(mdd, peak - e)
        diffs = [eqs[i + 1] - eqs[i] for i in range(len(eqs) - 1)]
        mean = sum(diffs) / len(diffs) if diffs else 0.0
        sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)) if len(diffs) > 1 else 0.0
        comp = sim.components()
        thin = pts if len(pts) <= max_points else [pts[int(round(i * (len(pts) - 1) / (max_points - 1)))] for i in range(max_points)]
        out.append(dict(label=label, auto=sim.params["auto"], threshold=sim.params["threshold"], pnl=round(sim.equity(), 2),
                        comp={k: round(v, 2) for k, v in comp.items()}, n_hedges=sim.s["n_hedges"], fees=round(sim.s["fees"], 2),
                        slip=round(sim.s["slip_opt"] + sim.s["slip_fut"], 2), max_drawdown=round(mdd, 2), step_sd=round(sd, 2),
                        steps=sim.s["steps"], end_t=sim.s["clock_t"], open_legs_left=len(sim.s["legs"]),
                        curve=[[p["t"], p["equity"]] for p in thin]))
    return out
