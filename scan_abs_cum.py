#!/usr/bin/env python3
"""scan_abs_cum.py — 掃一段時間的每分鐘存檔,找出前端「累積吸收」訊號何時亮、當時數字(唯讀)。

前端「累積吸收」分頁(web/app.js `buildAbsorbCum`)是純前端算、沒有落地紀錄,要回頭查
「哪天幾點亮過、亮得對不對」,就用這支掃存檔重算一遍。門檻跟前端一致:
  W=30 分 / FLOW_MIN=300 口 / FLOW_REF=1200 / DEAD_PTS=10 點 / CONSIST=0.65,方向預設翻轉(延續假設)。

差異提醒(近似,不是 bug):前端「價格變動」用的是 1 分 K 收盤(bars_process 抓 IBKR),
這裡只有 Bridge 現價(spot),兩者可能差 1~3 點;所以「何時亮、亮綠還亮紅」會一致,
「動幾點」是近似值。

用法(時間都是台北):
  python3 scan_abs_cum.py --date 2026-11-04               # 掃一整天
  python3 scan_abs_cum.py --lo 1793807700 --hi 1793810100  # epoch(UTC),跨午夜用這個
  python3 scan_abs_cum.py --date 2026-11-04 --show all     # 連「差一點就亮」的近失誤也印

輸出:
  - 每個亮訊號的分鐘:時間、30 分累積 Delta、30 分價格變動、同號分鐘佔比、看多/看空、強度。
  - 若有 --show all,另列「累積量夠但被一致性/價格門檻擋掉」的近失誤(看是不是太嚴)。
"""
from __future__ import annotations

import argparse
import datetime as dt
from zoneinfo import ZoneInfo

TW = ZoneInfo("Asia/Taipei")

import orderflow_window as OW

W = 30          # 滾動窗口(分鐘)
FLOW_MIN = 300  # 窗口累積 Delta 至少 ±300 口(2026-11-04 從 500 放寬,讓訊號多一點)
FLOW_REF = 1200 # 強度滿格刻度
DEAD_PTS = 10   # 價格死區(點)
CONSIST = 0.65  # 單邊一致性:同號分鐘佔比下限
FLIP = True     # 方向翻轉(延續假設):累積賣壓→看空、累積買壓→看多。15 段 80% 走反的 post-hoc 假設,未驗證


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def scan(minutes, w=W, flow_min=FLOW_MIN, flow_ref=FLOW_REF, dead_pts=DEAD_PTS, consist=CONSIST, flip=FLIP):
    """輸入 read_minutes() 的結果(未排序也可),回傳 (fires, nearmiss, stats)。

    fires / nearmiss 都是 list of dict:time(epoch 秒)、cum、move、consist、dir、strength、why。
    stats 是 {n_min, n_fire, n_near}。門檻可覆寫(回測想試不同鬆緊度用)。
    """
    # 整理成 (t, delta, spot),delta 為 None 的照前端「整分鐘跳過」。
    ms = []
    for m in minutes:
        t = m.get("t")
        if t is None:
            continue
        tr = m.get("delta_tr")
        d = _fnum(tr) if tr is not None else _fnum(m.get("delta"))
        s = _fnum(m.get("spot"))
        ms.append((t, d, s))
    ms.sort(key=lambda x: x[0])

    fires, nearmiss = [], []
    a = 0  # 窗口起點指標(向前滑動)
    for i, (t, d, s) in enumerate(ms):
        if s is None:
            continue  # 沒有現價,算不了價格變動,跳過(前端這格沒 bar close 也會跳)
        w0 = t - w * 60
        while a < len(ms) and ms[a][0] < w0:
            a += 1
        # 窗口起點現價:窗口內第一個有 spot 的分鐘
        s0 = None
        for k in range(a, i + 1):
            if ms[k][2] is not None:
                s0 = ms[k][2]
                break
        if s0 is None:
            continue
        cum, neg, pos, n = 0.0, 0, 0, 0
        for k in range(a, i + 1):
            dk = ms[k][1]
            if dk is None:
                continue
            cum += dk
            if dk < 0:
                neg += 1
            elif dk > 0:
                pos += 1
            n += 1
        if n == 0:
            continue
        move = s - s0
        frac = (neg / n) if cum < 0 else (pos / n) if cum > 0 else 0.0
        rec = {"t": t, "cum": cum, "move": move, "consist": frac, "spot": s}

        if cum <= -flow_min and move > -dead_pts and neg / n >= consist:
            rec.update(dir="看多", strength=min(1.0, -cum / flow_ref))
        elif cum >= flow_min and move < dead_pts and pos / n >= consist:
            rec.update(dir="看空", strength=-min(1.0, cum / flow_ref))
        elif (cum <= -flow_min and move > -dead_pts) or (cum >= flow_min and move < dead_pts):
            # 舊規則(不含一致性)會亮,但同號佔比不足被擋掉 → 近失誤(看是不是太嚴)
            rec.update(dir="—", strength=0.0, why="同號佔比不足")
            nearmiss.append(rec)
            continue
        else:
            continue
        if flip:
            rec["dir"] = "看空" if rec["dir"] == "看多" else "看多"
            rec["strength"] = -rec["strength"]
        fires.append(rec)
    return fires, nearmiss, {"n_min": len(ms), "n_fire": len(fires), "n_near": len(nearmiss)}


def _tw(t):
    return dt.datetime.fromtimestamp(t, TW).strftime("%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description="掃每分鐘存檔,找「累積吸收」訊號何時亮(台北時間)")
    ap.add_argument("--date", default=None, help="台北日期 YYYY-MM-DD(預設今天)")
    ap.add_argument("--from", dest="frm", default=None, help="起 HH:MM(台北時間,不跨午夜)")
    ap.add_argument("--to", dest="to", default=None, help="訖 HH:MM(台北時間,不跨午夜)")
    ap.add_argument("--lo", type=int, default=None, help="直接給 epoch 秒(UTC),覆蓋上面選項")
    ap.add_argument("--hi", type=int, default=None, help="直接給 epoch 秒(UTC),覆蓋上面選項")
    ap.add_argument("--show", choices=["fire", "all"], default="fire", help="fire=只印亮訊號;all=連近失誤也印")
    ap.add_argument("--w", type=float, default=W, help="滾動窗口分鐘(預設 30)")
    args = ap.parse_args()

    if args.lo is not None and args.hi is not None:
        lo, hi = args.lo, args.hi
    else:
        day = args.date or dt.datetime.now(TW).strftime("%Y-%m-%d")
        try:
            d = dt.datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit(f"日期格式不對:{day}(要 YYYY-MM-DD)")

        def hhmm(s, dflt):
            if not s:
                return dflt
            h, m = s.split(":")
            return dt.datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=TW)

        lo = int(hhmm(args.frm, dt.datetime(d.year, d.month, d.day, 0, 0, tzinfo=TW)).timestamp())
        hi = int(hhmm(args.to, dt.datetime(d.year, d.month, d.day, 23, 59, tzinfo=TW)).timestamp())
    if lo > hi:
        raise SystemExit("起 > 訖,檢查一下時間順序。(跨午夜請用 --lo/--hi epoch 秒)")

    minutes = OW.read_minutes(lo, hi)
    fires, nearmiss, stats = scan(minutes, w=args.w)

    print("═" * 80)
    print(f"累積吸收掃描 · {_tw(lo)} ~ {_tw(hi)}(台北)· 共 {stats['n_min']} 分鐘有資料")
    if stats["n_min"] == 0:
        print("  這段沒有訂單流資料(Bookmap 沒送、或時間不對)。訊號不可能出現在沒資料的時段。")
        print("═" * 80)
        return

    print(f"  {'時間':<16} {'累積Δ':>9} {'價格變動':>9} {'同號%':>6} {'方向':<6} {'強度':>7}")
    if not fires:
        print("  這段沒有任何「累積吸收」訊號亮過。")
    for r in fires:
        print(f"  {_tw(r['t']):<16} {r['cum']:>+9.0f} {r['move']:>+9.1f} {r['consist'] * 100:>5.0f}% {r['dir']:<6} {r['strength']:>+7.2f}")

    if args.show == "all" and nearmiss:
        print("  ── 近失誤(累積≥300 且價格變動≤10,但被「同號≥65%」擋掉)──")
        for r in nearmiss:
            print(f"  {_tw(r['t']):<16} {r['cum']:>+9.0f} {r['move']:>+9.1f} {r['consist'] * 100:>5.0f}%  (擋掉:{r['why']})")

    print("─" * 80)
    print(f"  總結:亮訊號 {stats['n_fire']} 個、被一致性擋掉的近失誤 {stats['n_near']} 個。")
    print("  價格變動是「現價 vs 約30分前現價」的近似(前端用 1 分 K 收盤,可能差 1~3 點)。")
    print("═" * 80)
    print("把上面整段複製貼給我,我幫你判讀(那次綠色誤報到底在哪、亮得對不對)。")


if __name__ == "__main__":
    main()
