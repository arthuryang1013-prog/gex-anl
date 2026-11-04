"""hedge_pricing(Black-76):用有限差分、put-call parity、與收集器既有 gamma 公式交叉驗證。"""
import os, sys, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hedge_pricing as H

R = H.RISK_FREE
cases = [(29800.0, 29800.0, 1 / 365, 0.20), (29800.0, 29900.0, 0.01, 0.18), (29800.0, 29500.0, 0.05, 0.25),
         (29800.0, 30500.0, 0.003, 0.30), (29800.0, 29810.0, 3 / 24 / 365, 0.15)]

for F, K, T, iv in cases:
    for cp in "CP":
        g = H.greeks(F, K, T, iv, cp)
        # 價格一致
        assert abs(g["price"] - H.price(F, K, T, iv, cp)) < 1e-9
        # Delta / Gamma:對 F 做有限差分
        h = F * 1e-5
        up, dn = H.price(F + h, K, T, iv, cp), H.price(F - h, K, T, iv, cp)
        d_num = (up - dn) / (2 * h)
        g_num = (up - 2 * g["price"] + dn) / (h * h)
        assert abs(d_num - g["delta"]) < 1e-5, (F, K, T, iv, cp, d_num, g["delta"])
        assert abs(g_num - g["gamma"]) < max(1e-7, abs(g["gamma"]) * 1e-3), (F, K, T, iv, cp, g_num, g["gamma"])
        # Vega:對 iv 做有限差分,每 1 個波動率點
        dv = 1e-5
        v_num = (H.price(F, K, T, iv + dv, cp) - H.price(F, K, T, iv - dv, cp)) / (2 * dv) * 0.01
        assert abs(v_num - g["vega"]) < max(1e-6, abs(g["vega"]) * 1e-4), (F, K, T, iv, cp, v_num, g["vega"])
        # Theta:時間走 1/365/10 年,每日曆日的價格變化
        dt_ = 1 / 365 / 10
        if T > dt_ * 2:
            # price(T-dt) - price(T+dt) = -2dt·∂V/∂T;除以 2dt 得 Theta(每年),再除以 365 換成每日曆日
            th_num = (H.price(F, K, T - dt_, iv, cp) - H.price(F, K, T + dt_, iv, cp)) / (2 * dt_) / 365
            assert abs(th_num - g["theta"]) < max(1e-4, abs(g["theta"]) * 2e-3), (F, K, T, iv, cp, th_num, g["theta"])

# put-call parity:C - P = e^{-rT}(F - K)
for F, K, T, iv in cases:
    c, p = H.price(F, K, T, iv, "C"), H.price(F, K, T, iv, "P")
    assert abs((c - p) - math.exp(-R * T) * (F - K)) < 1e-7

# Call Delta - Put Delta = e^{-rT};Call、Put 的 Gamma / Vega 相同
for F, K, T, iv in cases:
    c, p = H.greeks(F, K, T, iv, "C"), H.greeks(F, K, T, iv, "P")
    assert abs((c["delta"] - p["delta"]) - math.exp(-R * T)) < 1e-9
    assert abs(c["gamma"] - p["gamma"]) < 1e-12 and abs(c["vega"] - p["vega"]) < 1e-9

# 與收集器已用的 gamma 公式一致(它存進資料庫的 gamma 欄位就是這個):q = r 時等於 Black-76
try:
    import nq_gex_collector as C
    for F, K, T, iv in cases:
        a = float(C.bs_gamma(F, K, T, iv, R, R))
        b = H.greeks(F, K, T, iv, "C")["gamma"]
        assert abs(a - b) < 1e-12 + abs(a) * 1e-9, (a, b)
    print("與收集器 bs_gamma 一致")
except ImportError as e:
    print("略過收集器對照(缺套件):", e)

# 邊界:到期、IV 缺失、價內價外
assert H.price(100, 90, 0, 0.2, "C") == 10 and H.price(100, 90, 0, 0.2, "P") == 0
g = H.greeks(100, 90, 0, 0.2, "C"); assert g["delta"] == 1 and g["gamma"] == 0 and g["theta"] == 0
g = H.greeks(100, 90, 0, 0.2, "P"); assert g["delta"] == 0
g = H.greeks(100, 110, 0, 0.2, "P"); assert g["delta"] == -1
assert H.price(100, 100, 0.01, 0, "C") == 0.0          # iv=0 → 內含價值
assert H.price(100, 100, 0.01, None, "C") == 0.0
try:
    H.price(100, 100, 0.01, 0.2, "X"); raise SystemExit("應該丟錯")
except ValueError:
    pass
# ATM 一日選擇權的量級合理性(不是驗證,只是擋明顯單位錯誤):F=29800, iv=20%, 1 天 → 約 0.4*F*iv*sqrt(T) ≈ 130 點
p = H.price(29800, 29800, 1 / 365, 0.20, "C")
assert 100 < p < 160, p
print("OK")
