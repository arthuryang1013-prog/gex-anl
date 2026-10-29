#!/usr/bin/env python3
"""盤面指標「每個訊號」的 ±40 驗證:這個訊號說偏多 / 偏空之後,價格先碰到 +40 還是 -40?

和 thesis_outcome.py 同一套判法(三道柵欄:±X 點、最多等 hold 分鐘),但把總分拆開,
一個訊號一個訊號看。要有資料:網站部署「訊號存檔」那個更新之後,thesis 表才會存每個訊號的值(sig_json),
舊紀錄沒有,只能從部署後開始累積。

怎麼看:
  - 「命中率」= 訊號說多後先漲 40、說空後先跌 40 的比例(只算分出勝負的)。1:1 停利停損、扣成本後,
    要高過「損益兩平命中率」才賺。「±」是大概的誤差範圍(95%):樣本少時,誤差大到什麼都看不出來。
  - 相鄰紀錄的 30 分鐘重疊,獨立樣本比 n 少很多。
  - 同時比較很多個訊號,運氣好的幾個會看起來有效(多重比較)。要有效,應該連續幾天都高過兩平、而且樣本夠多。
  - gma / gma0 是「環境」訊號(gamma 正負),不是方向,放在表裡只是對照。

用法(VPS 專案資料夾):
  python3 signal_outcome.py                 # 預設 ±40、30 分鐘、成本 2 點、只看美東 09:30–13:30、|s| ≥ 0.15 才算有表態
  python3 signal_outcome.py --thr 0.3 --all-hours
"""
import argparse, bisect, datetime as dt, json, math, sqlite3, sys
from zoneinfo import ZoneInfo
import thesis_outcome as TO

NY = ZoneInfo("America/New_York")
REGIME_IDS = ("gma", "gma0")


def load_rows(cache_db):
    con = sqlite3.connect(cache_db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(thesis)")]
        if "sig_json" not in cols:
            sys.exit("thesis 表還沒有 sig_json 欄位:要先部署『訊號存檔』更新,等盤中累積幾天資料。")
        rows = con.execute("SELECT t, spot, score, sig_json FROM thesis WHERE t IS NOT NULL AND sig_json IS NOT NULL ORDER BY t").fetchall()
    finally:
        con.close()
    out = []
    for t, spot, score, sj in rows:
        try:
            d = json.loads(sj)
        except Exception:
            continue
        sig = {}
        for k in ("NQ", "QQQ", "NDX"):
            for sid, v in (d.get(k) or {}).items():
                sig[f"{k}:{sid}"] = v
        for n, v in (d.get("flow") or {}).items():
            sig[f"flow:{n}"] = v
        for n, v in (d.get("agg") or {}).items():
            sig[f"agg:{n}"] = v
        out.append((t, spot, sig))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="nq_gex.sqlite")
    ap.add_argument("--cache", default="web_cache.sqlite")
    ap.add_argument("--X", type=float, default=40.0)
    ap.add_argument("--hold", type=int, default=30)
    ap.add_argument("--cost", type=float, default=2.0)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--thr", type=float, default=0.15, help="|s| 要超過這個值才算「有表態」")
    ap.add_argument("--min-n", type=int, default=20, help="分出勝負少於這個數的訊號只列出、不評論")
    ap.add_argument("--all-hours", action="store_true")
    a = ap.parse_args()

    bars = TO.load_bars(a.db)
    if len(bars) < 200:
        sys.exit("bars 表資料太少(<200 根)")
    bt = [b[0] for b in bars]; bh = [b[1] for b in bars]; bl = [b[2] for b in bars]
    rows = load_rows(a.cache)
    print(f"有訊號存檔的紀錄 {len(rows)} 筆")
    picked, last = [], -1e18
    for t, spot, sig in rows:
        if t - last < a.step * 60:
            continue
        loc = dt.datetime.fromtimestamp(t, NY)
        if not a.all_hours and not (dt.time(9, 30) <= loc.time() <= dt.time(13, 30) and loc.weekday() < 5):
            continue
        if spot is None:
            continue
        r = TO.first_touch(bt, bh, bl, bisect.bisect_right(bt, t), t, spot, a.X, a.hold)
        if r is None:
            continue
        picked.append((t, sig, r)); last = t
    if not picked:
        sys.exit("沒有可判定的紀錄(時段過濾後為 0,或 K 棒沒涵蓋)。可加 --all-hours")
    be = (a.X + a.cost) / (2 * a.X)
    days = len({dt.datetime.fromtimestamp(p[0], NY).date() for p in picked})
    print(f"判定 ±{a.X:g} 點 / 最多 {a.hold} 分鐘 / 成本 {a.cost:g} 點 → 損益兩平命中率 {be*100:.1f}%;取樣 {len(picked)} 筆,跨 {days} 天")
    up = sum(1 for p in picked if p[2] == "up"); dn = sum(1 for p in picked if p[2] == "down")
    print(f"基準:這段期間先漲 40 的有 {up} 次、先跌 40 的有 {dn} 次(永遠做多命中率 {100*up/max(1,up+dn):.1f}%)\n")

    names = sorted({k for p in picked for k in p[1]})
    res = []
    for k in names:
        hit = dec = n = 0
        for t, sig, r in picked:
            s = sig.get(k)
            if s is None or abs(s) < a.thr:
                continue
            n += 1
            if r in ("up", "down"):
                dec += 1
                if (s > 0 and r == "up") or (s < 0 and r == "down"):
                    hit += 1
        res.append((k, n, dec, hit))
    print(f"{'訊號':26}{'有表態':>7}{'分出勝負':>9}{'猜對':>6}{'命中率':>8}{'±':>6}{'vs兩平':>8}")
    for k, n, dec, hit in res:
        tag = "(環境)" if k.split(":")[-1] in REGIME_IDS else ""
        if dec == 0:
            print(f"{k + tag:26}{n:>7}{dec:>9}")
            continue
        rate = hit / dec
        ci = 98.0 / math.sqrt(dec)
        note = "" if dec >= a.min_n else "  (樣本太少)"
        print(f"{k + tag:26}{n:>7}{dec:>9}{hit:>6}{rate*100:>7.1f}%{ci:>5.0f}%{(rate - be)*100:>+7.1f}%{note}")
    print("\n提醒:「±」是 95% 誤差範圍;命中率減掉兩平後,若還沒超過 ± 就是看不出差別。多個訊號同時比較,偶然的高分很常見。")


if __name__ == "__main__":
    main()
