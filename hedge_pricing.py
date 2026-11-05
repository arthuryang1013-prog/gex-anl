"""hedge_pricing.py — NQ 期貨選擇權的理論價格與 Greeks(Black-76),給「對沖模擬」用。

為什麼需要:資料庫(contracts 表)存了每檔的履約價、IV、F(期貨價)、T(到期年數),但沒有存買賣報價,
所以模擬倉的選擇權價格只能「用 IV 反推理論價」,不是真實可成交的價格。這是模擬的核心限制。

約定:
  - 價格單位是「NQ 點」(1 點 = 20 美元 × 張數)。Delta 是「相當於幾口 NQ 期貨」。
  - Gamma:每動 1 點 NQ,Delta 變多少(口)。
  - Theta:每過 1 個日曆日,價格變多少點(通常為負 = 買方付出的時間價值)。
  - Vega:IV 每變 1 個波動率點(例如 20% → 21%),價格變多少點。
  - r 預設 0.04,跟收集器的 RISK_FREE 一致;期貨選擇權折現用 exp(-rT)。
  - T 用「日曆年」(秒數 / (365×24×3600)),跟收集器一致;收集器對很近到期的合約把 T 下限設在 1 小時,
    這裡不重複套用,呼叫端要自己決定(模擬引擎在推進時間時用同一個下限)。
只用標準函式庫(math),不依賴 numpy / scipy。
"""
import math

RISK_FREE = 0.04
YEAR_SEC = 365 * 24 * 3600
MIN_T = 3600 / YEAR_SEC          # 與收集器相同:T 至少 1 小時
NQ_MULT = 20.0                   # 美元/點(NQ 期貨與 NQ 選擇權)
MNQ_MULT = 2.0                   # 美元/點(微型 MNQ 期貨)


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _sign(cp):
    c = str(cp).upper()[:1]
    if c == "C":
        return 1
    if c == "P":
        return -1
    raise ValueError(f"cp 必須是 C 或 P:{cp!r}")


def intrinsic(F, K, cp):
    return max(0.0, _sign(cp) * (F - K))


def price(F, K, T, iv, cp, r=RISK_FREE):
    """Black-76 理論價(點)。T<=0 或 iv<=0 時回傳內含價值。"""
    s = _sign(cp)
    if T <= 0 or iv is None or iv <= 0 or F <= 0 or K <= 0:
        return intrinsic(F, K, cp)
    sd = iv * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * iv * iv * T) / sd
    d2 = d1 - sd
    disc = math.exp(-r * T)
    return disc * s * (F * _ncdf(s * d1) - K * _ncdf(s * d2))


def greeks(F, K, T, iv, cp, r=RISK_FREE):
    """回傳 dict(price, delta, gamma, theta, vega)。單位見檔頭。T<=0 或 iv<=0 時只剩內含價值與階梯 Delta。"""
    s = _sign(cp)
    if T <= 0 or iv is None or iv <= 0 or F <= 0 or K <= 0:
        itm = (F > K) if s == 1 else (F < K)
        return dict(price=intrinsic(F, K, cp), delta=(s if itm else 0.0), gamma=0.0, theta=0.0, vega=0.0)
    rt = math.sqrt(T)
    sd = iv * rt
    d1 = (math.log(F / K) + 0.5 * iv * iv * T) / sd
    d2 = d1 - sd
    disc = math.exp(-r * T)
    pv = disc * s * (F * _ncdf(s * d1) - K * _ncdf(s * d2))
    delta = disc * s * _ncdf(s * d1)
    gamma = disc * _npdf(d1) / (F * sd)
    vega_1 = disc * F * _npdf(d1) * rt                       # 每 1.00(=100 個波動率點)的價格變化
    # ∂V/∂T = -r V + e^{-rT} F φ(d1) σ / (2√T);Theta = -∂V/∂T(隨日曆時間流逝),再換成每日曆日
    theta_year = r * pv - disc * F * _npdf(d1) * iv / (2.0 * rt)
    return dict(price=pv, delta=delta, gamma=gamma, theta=theta_year / 365.0, vega=vega_1 * 0.01)


def bs_yield_greeks(S, K, T, iv, cp, r=RISK_FREE, q=0.0):
    """Black-Scholes(含連續殖利率 q)的 Greeks——與收集器 nq_gex_collector.bs_gamma 同一個模型
    (q == r 時等同 Black-76;NQ 的 q=r=0.04,QQQ/NDX 用各自較小的 q)。給「vanna / charm 環境」訊號用,
    不影響上面 greeks()(那套是 Black-76、給對沖模擬用)。

    回傳 dict(delta, gamma, vega, vanna, charm),單位與 greeks() 一致:
      - vanna = ∂Δ/∂σ(每 1 個波動率點,即 σ 變 0.01)。Call / Put 相同。
      - charm = ∂Δ/∂T(每 1 個日曆日):「到期日增加一天」時 Delta 的變化量;隨時間流逝(T 每天減 1),
        Delta 實際漂移方向 = −charm/天。Call / Put 不同(差一個 q 的載體項)。
    這些是二階量,與 GEX 同屬「慣例 / 未驗證」,不當方向事實。T<=0 或 iv<=0 時各項回 0。
    """
    s = _sign(cp)
    if T <= 0 or iv is None or iv <= 0 or S <= 0 or K <= 0:
        return dict(delta=0.0, gamma=0.0, vega=0.0, vanna=0.0, charm=0.0)
    rt = math.sqrt(T)
    sd = iv * rt
    d1 = (math.log(S / K) + (r - q + 0.5 * iv * iv) * T) / sd
    d2 = d1 - sd
    eq = math.exp(-q * T)
    phi = _npdf(d1)
    nd1 = _ncdf(s * d1)
    delta = s * eq * nd1
    gamma = eq * phi / (S * sd)
    vega_1 = S * eq * phi * rt                          # 每 1.00(100 個波動率點)
    # ∂Δ/∂σ = e^{-qT} φ(d1) · ∂d1/∂σ,而 ∂d1/∂σ = −d2/σ → vanna = −e^{-qT} φ(d1) d2 / σ
    vanna_1 = -eq * phi * d2 / iv                       # 每 1.00 σ
    # ∂Δ/∂T(每年)= e^{-qT}·[ φ(d1)·∂d1/∂T − s·q·N(s·d1) ],其中 ∂d1/∂T = −d2/(2T) + (r−q)/(σ√T)
    ddelta_dT = eq * (phi * (-d2 / (2.0 * T) + (r - q) / sd) - s * q * nd1)
    return dict(delta=delta, gamma=gamma, vega=vega_1 * 0.01,
                vanna=vanna_1 * 0.01, charm=ddelta_dT / 365.0)
