#!/usr/bin/env python3
"""導師分數驗證:導師說「偏多/偏空」之後,價格先碰到 +40 還是 -40?

做法(三道柵欄 triple-barrier):
  每筆導師紀錄(thesis 表)當起點,往後看 1 分 K:價格先漲 40 點 → 「先到上」;先跌 40 點 → 「先到下」;
  30 分鐘內都沒到 → 「逾時」。同一根 K 棒同時碰到上下 → 「無法判定」(1 分 K 看不出誰先)。
  偏多的「命中」= 先到上;偏空的「命中」= 先到下。

怎麼看結果:
  停利 40、停損 40 的 1:1 交易,扣掉成本後,命中率要超過「損益兩平命中率」才賺:(40+成本)/80。
  所以表格同時給「命中率」和「損益兩平命中率」,以及「沒有導師、隨機做多」的基準線(多空先到比例)。
  相鄰的紀錄看的是重疊的 30 分鐘,所以真正獨立的樣本數大約是 n 的 1/3 以下,不要被 n 騙。

用法(在 VPS 專案資料夾):
  python3 thesis_outcome.py                      # 預設 ±40、30 分鐘、成本 2 點、只看台北盤中(美東 09:30–13:30)
  python3 thesis_outcome.py --X 40 --hold 30 --cost 2 --step 10 --all-hours
  python3 thesis_outcome.py --split              # 用「方向分數(不含 gamma 環境)」的正負當方向(導師分數拆分 patch 之後才有資料)
  python3 thesis_outcome.py --regime             # 另外依「環境」(regime_s 正/負 gamma)分組
"""
import argparse, datetime as dt, os, sqlite3, sys
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def load_bars(db):
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT t,h,l FROM bars ORDER BY t").fetchall()
    finally:
        con.close()
    return rows


def load_thesis(cache_db, split):
    """回傳 [(t, spot, 方向, 分數, regime_s)]。split=True 時用 score_dir 的正負當方向(舊紀錄沒有此欄 → 略過)。"""
    con = sqlite3.connect(cache_db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(thesis)")]
        if split and "score_dir" not in cols:
            sys.exit("thesis 表還沒有 score_dir 欄位(要先部署『導師分數拆成方向與環境』那個更新,並累積資料)")
        sd = "score_dir" if "score_dir" in cols else "NULL"
        rg = "regime_s" if "regime_s" in cols else "NULL"
        rows = con.execute(f"SELECT t, spot, direction, score, {sd}, {rg} FROM thesis WHERE t IS NOT NULL ORDER BY t").fetchall()
    finally:
        con.close()
    out = []
    for (t, spot, d, sc, sdir, rs) in rows:
        if split:
            if sdir is None:
                continue
            d, sc = ("偏多" if sdir >= 0 else "偏空"), sdir
        out.append((t, spot, d, sc, rs))
    return out


def first_touch(bars_t, bars_h, bars_l, i0, t0, spot, X, hold):
    """從 t0 之後的第一根 K 棒起算。回傳 'up' / 'down' / 'timeout' / 'both' / None(資料不足)。"""
    end = t0 + hold * 60
    n = len(bars_t)
    i = i0
    last_t = None
    while i < n and bars_t[i] <= end:
        last_t = bars_t[i]
        up = bars_h[i] >= spot + X
        dn = bars_l[i] <= spot - X
        if up and dn:
            return "both"
        if up:
            return "up"
        if dn:
            return "down"
        i += 1
    if last_t is None or last_t < end - 120:      # 後面的 K 棒還沒走完/缺資料
        return None
    return "timeout"


def pct(a, b):
    return f"{100.0 * a / b:5.1f}%" if b else "   -  "


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="nq_gex.sqlite", help="有 bars 表的資料庫")
    ap.add_argument("--cache", default="web_cache.sqlite", help="有 thesis 表的資料庫")
    ap.add_argument("--X", type=float, default=40.0)
    ap.add_argument("--hold", type=int, default=30, help="最多等幾分鐘")
    ap.add_argument("--cost", type=float, default=2.0, help="來回成本(點)")
    ap.add_argument("--step", type=int, default=10, help="每隔幾分鐘最多取一筆紀錄(減少重疊)")
    ap.add_argument("--split", action="store_true", help="用方向分數(不含 gamma 環境)的正負當方向")
    ap.add_argument("--regime", action="store_true", help="另外依 gamma 環境分組")
    ap.add_argument("--all-hours", action="store_true", help="不限美東 09:30–13:30")
    a = ap.parse_args()

    bars = load_bars(a.db)
    if len(bars) < 200:
        sys.exit("bars 表資料太少(<200 根),先讓網站的 K 棒來源多跑幾天")
    bt = [b[0] for b in bars]; bh = [b[1] for b in bars]; bl = [b[2] for b in bars]
    rows = load_thesis(a.cache, a.split)
    print(f"導師紀錄 {len(rows)} 筆;1 分 K {len(bars)} 根 ({dt.datetime.fromtimestamp(bt[0], NY):%Y-%m-%d} ~ {dt.datetime.fromtimestamp(bt[-1], NY):%Y-%m-%d})")

    import bisect
    picked, last = [], -1e18
    for (t, spot, d, sc, rs) in rows:
        if t - last < a.step * 60:
            continue
        loc = dt.datetime.fromtimestamp(t, NY)
        if not a.all_hours and not (dt.time(9, 30) <= loc.time() <= dt.time(13, 30) and loc.weekday() < 5):
            continue
        if spot is None or sc is None or d not in ("偏多", "偏空"):
            continue
        i0 = bisect.bisect_right(bt, t)
        r = first_touch(bt, bh, bl, i0, t, spot, a.X, a.hold)
        if r is None:
            continue
        picked.append((t, d, sc, r, rs)); last = t
    if not picked:
        sys.exit("沒有可判定的紀錄(時段過濾後為 0,或 K 棒沒涵蓋)。可加 --all-hours 看看")

    be = (a.X + a.cost) / (2 * a.X)
    print(f"判定點數 ±{a.X:g}、最多等 {a.hold} 分鐘、成本 {a.cost:g} 點 → 損益兩平命中率 {be*100:.1f}%")
    print(f"取樣:每 {a.step} 分鐘最多 1 筆,共 {len(picked)} 筆(重疊 → 獨立樣本約 {max(1, int(len(picked) * a.step / a.hold))} 以下)\n")

    def table(title, groups):
        print(f"【{title}】")
        print(f"{'組別':16}{'n':>5}{'先到上':>8}{'先到下':>8}{'逾時':>7}{'同棒':>6}{'分出勝負':>9}{'命中率*':>9}{'vs兩平':>9}")
        for name, items in groups:
            n = len(items)
            if not n:
                continue
            up = sum(1 for x in items if x[3] == "up"); dn = sum(1 for x in items if x[3] == "down")
            to = sum(1 for x in items if x[3] == "timeout"); bo = sum(1 for x in items if x[3] == "both")
            hit = sum(1 for x in items if (x[1] == "偏多" and x[3] == "up") or (x[1] == "偏空" and x[3] == "down"))
            dec = sum(1 for x in items if x[3] in ("up", "down"))
            rate = hit / dec if dec else float("nan")
            diff = (rate - be) * 100 if dec else float("nan")
            print(f"{name:16}{n:>5}{pct(up, n):>8}{pct(dn, n):>8}{pct(to, n):>7}{pct(bo, n):>6}{dec:>9}{rate*100:>8.1f}%{diff:>+8.1f}%")
        print()

    table("全部", [("全部", picked), ("偏多", [x for x in picked if x[1] == "偏多"]), ("偏空", [x for x in picked if x[1] == "偏空"])])
    bins = [(0, 15), (15, 30), (30, 50), (50, 101)]
    table("依分數強度(|分數|)", [(f"|{lo}|~|{hi}|", [x for x in picked if lo <= abs(x[2]) < hi]) for lo, hi in bins])

    if a.regime:
        known = [x for x in picked if x[4] is not None]
        table("依 gamma 環境(正=壓波動 / 負=放大波動 / 中性)",
              [("正 gamma(≥+0.25)", [x for x in known if x[4] >= 0.25]),
               ("中性", [x for x in known if -0.25 < x[4] < 0.25]),
               ("負 gamma(≤-0.25)", [x for x in known if x[4] <= -0.25])])
    # 基準線:不看導師,隨機做多 / 做空會怎樣
    base = [(x[0], "偏多", x[2], x[3], x[4]) for x in picked]
    base_s = [(x[0], "偏空", x[2], x[3], x[4]) for x in picked]
    table("基準線(不看導師,同樣時間點全做多 / 全做空)", [("永遠做多", base), ("永遠做空", base_s)])

    print("*命中率 = 命中次數 ÷ (先到上 + 先到下);逾時與同棒不算進分母(實際交易逾時會平倉,結果接近 0,此處未計入)。")
    print("看法:'vs兩平' 為正且樣本夠多,才有「導師方向有用」的證據;和「永遠做多」基準比較,可以扣掉大盤漂移的影響。")
    print("      這是歷史描述,不是預測保證;樣本少的時候(獨立樣本 < 100)只能當參考。")


if __name__ == "__main__":
    main()
