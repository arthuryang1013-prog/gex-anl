#!/usr/bin/env python3
"""Delta 對帳:「MBO 推判的 Delta」vs「實際成交算的 Delta」,以及 imb 新舊算法。

資料來源:orderflow_archive/YYYYMMDD.jsonl.gz(接收器每天存的完整封包)。
需要新版 bridge(v2)才有 delta_tr / cvd_tr / cvd_mbo / imb_big 欄位;舊封包會被略過並說明。

看什麼:
 1) 方向一致率:每秒的 delta(MBO 推判,近 60 秒累計)與 delta_tr(成交,近 60 秒累計)正負號是否一致。
 2) 相關係數:兩者的相關(1 = 完全同步)。
 3) 量比:MBO 推判的總量 ÷ 成交總量(vol / vol_tr)。接近 1 才代表 MBO 推判沒有漏算或重複算。
 4) CVD 漂移:cvd_mbo 與 cvd_tr 的累計差,隨時間拉開多少口。
 5) imb(最近 N 檔)與 imb_big(量最大 N 檔,舊算法)的差異。
建議再手動對一次:在 Bookmap 畫面開 CVD 指標,記下某兩個時間點的 CVD 差,與本腳本同一段 cvd_tr 差比較。

用法(VPS 專案資料夾):  python3 orderflow_check.py [YYYYMMDD ...] [--dir orderflow_archive]
不給日期就用最新一天。
"""
import argparse, glob, gzip, json, math, os, sys


def load(path):
    out = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for ln in f:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def corr(x, y):
    n = len(x)
    if n < 3:
        return float("nan")
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x)); sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx == 0 or sy == 0:
        return float("nan")
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def report(day, recs):
    v2 = [r for r in recs if r.get("delta_tr") is not None]
    print(f"=== {day}:{len(recs)} 筆封包,其中新版欄位 {len(v2)} 筆 ===")
    if len(v2) < 30:
        print("  新版 bridge(v2)的資料不足 30 筆,無法對帳。請確認 Windows 那台已換上新版 bridge。\n")
        return
    d = [r["delta"] for r in v2]; t = [r["delta_tr"] for r in v2]
    both = [(a, b) for a, b in zip(d, t) if a != 0 and b != 0]
    same = sum(1 for a, b in both if (a > 0) == (b > 0))
    print(f"  1) 方向一致率:{100.0 * same / len(both):.1f}%(n={len(both)} 秒;兩邊都非 0 才算)" if both else "  1) 沒有兩邊都非 0 的秒數")
    print(f"  2) 相關係數(60 秒累計 Delta):{corr(d, t):.3f}")
    vm = sum(r.get("vol", 0) for r in v2); vt = sum(r.get("vol_tr", 0) for r in v2)
    print(f"  3) 量比 MBO推判/成交:{vm / vt:.2f}(1.00 = 一致;>1 表示推判多算,<1 表示漏算)" if vt else "  3) 成交量為 0,無法比較")
    c1 = v2[-1]["cvd_mbo"] - v2[0]["cvd_mbo"]; c2 = v2[-1]["cvd_tr"] - v2[0]["cvd_tr"]
    print(f"  4) 這段期間 CVD 變化:MBO推判 {c1:+,.0f} 口、成交 {c2:+,.0f} 口,差 {c1 - c2:+,.0f} 口")
    ib = [(r["imb"], r.get("imb_big")) for r in v2 if r.get("imb_big") is not None]
    if ib:
        print(f"  5) imb(最近N檔)與 imb_big(舊)相關 {corr([a for a, _ in ib], [b for _, b in ib]):.3f};"
              f"平均絕對差 {sum(abs(a - b) for a, b in ib) / len(ib):.3f}")
    pb = sum(r.get("pull_bid", 0) for r in v2); pa = sum(r.get("pull_ask", 0) for r in v2)
    print(f"  (參考)近價撤單合計 買盤 {pb:,.0f}、賣盤 {pa:,.0f} 口(60 秒窗口累加,重複計算,只能看相對大小);含被成交吃掉的委託")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("days", nargs="*")
    ap.add_argument("--dir", default="orderflow_archive")
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.dir, "*.jsonl.gz")))
    if not files:
        sys.exit(f"{a.dir} 裡沒有存檔")
    if a.days:
        files = [f for f in files if os.path.basename(f)[:8] in a.days]
    else:
        files = files[-1:]
    for f in files:
        report(os.path.basename(f)[:8], load(f))
    print("提醒:這是核對工具。方向一致率/量比差很多時,先查 bridge 的 MBO 判定(marketable 掛單)或 Bookmap 的 trades 方向;不要直接下結論。")


if __name__ == "__main__":
    main()
