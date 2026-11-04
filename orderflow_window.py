#!/usr/bin/env python3
"""orderflow_window.py — 看「任意一段歷史時間」的每分鐘訂單流與事件(唯讀)。

flow_snapshot.py 只看「最近 N 分鐘」;要回看某一段(例如某晚盤整的 22:48~23:45),
需要指定日期與起訖時間。這個腳本只讀不寫,不碰任何服務,也不動資料檔。

在 VPS 專案資料夾直接跑(時間一律是台北時間):
  python3 orderflow_window.py --date 2026-11-03 --from 22:48 --to 23:45
  # 或直接給 epoch 秒(UTC,不是台北):
  python3 orderflow_window.py --lo 1793796000 --hi 1793800000
  # 省略 --from/--to = 看那一天整天;省略 --date = 今天。

輸出:
  - 每分鐘:台北時間、實際成交 Delta(delta_tr)、成交量(vol_tr)、Bridge 現價(spot)、
    累積 Delta、主動性比率 r = delta/vol(就是我前端「吸收背離」用的那個 r)。
  - 這段現價的最高-最低(看是不是盤整)、合計淨量。
  - 這段內的吸收/冰山/掃單/停損/力竭事件。

讀法重點:如果這段「累積 Delta 持續偏一邊、但現價範圍很小、且每分鐘 r 都小小的(<0.05)」,
那就是「盤整中的單邊暗流」——我現在那條吸收背離線會因為 r 太小而整段沒訊號。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from collections import Counter
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
TW = ZoneInfo("Asia/Taipei")

import orderflow_minutes as OM
import orderflow_store as OST

# 跟 gex_web_server.py 同一套路徑(HERE = 專案資料夾)。
ORDERFLOW_HISTORY_PATH = os.path.join(HERE, "orderflow_history.jsonl")
ORDERFLOW_EVENTS_PATH = os.path.join(HERE, "orderflow_events.jsonl")
ORDERFLOW_EVENTS_ARCHIVE_DIR = os.path.join(HERE, "orderflow_events_archive")
ORDERFLOW_MINUTES_DIR = os.path.join(HERE, "orderflow_minutes_archive")

KIND_NAME = {"sweep": "掃單", "stop": "停損(推測)", "absorb": "吸收", "iceberg": "冰山", "exhaust": "力竭"}


def _fnum(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def read_minutes(lo, hi):
    """跟 api_flowmom 同法:即時檔重算 + 長期存檔,每分鐘取 n 大者。"""
    recs = []
    try:
        with open(ORDERFLOW_HISTORY_PATH, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                ra = r.get("received_at") or 0
                if lo <= ra <= hi:
                    recs.append(r)
    except Exception:
        pass
    merged = {}
    try:
        merged = OST.read_minutes(ORDERFLOW_MINUTES_DIR, lo, hi)
    except Exception:
        merged = {}
    for m in (OM.minute_rows(recs, 60) if recs else []):
        old = merged.get(m["t"])
        if old is None or (m.get("n") or 0) >= (old.get("n") or 0):
            merged[m["t"]] = m
    return [merged[t] for t in sorted(merged)]


def read_events(lo, hi):
    out, seen = [], set()
    try:
        for e in OST.read_events(ORDERFLOW_EVENTS_ARCHIVE_DIR, lo, hi):
            if e.get("id"):
                seen.add(e["id"])
            out.append(e)
    except Exception:
        pass
    try:
        with open(ORDERFLOW_EVENTS_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8_000_000))
            raw = f.read().decode("utf-8", "ignore").splitlines()
    except Exception:
        raw = []
    for ln in raw:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        t = e.get("t")
        if isinstance(t, (int, float)) and lo <= t <= hi and e.get("k") in OST.KINDS:
            if e.get("id"):
                if e["id"] in seen:
                    continue
                seen.add(e["id"])
            out.append(e)
    out.sort(key=lambda e: e["t"])
    return out


def tw(t):
    return dt.datetime.fromtimestamp(t, TW).strftime("%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description="看一段歷史時間的每分鐘訂單流與事件(台北時間)")
    ap.add_argument("--date", default=None, help="台北日期 YYYY-MM-DD(預設今天)")
    ap.add_argument("--from", dest="frm", default=None, help="起 HH:MM(台北時間)")
    ap.add_argument("--to", dest="to", default=None, help="訖 HH:MM(台北時間)")
    ap.add_argument("--lo", type=int, default=None, help="直接給 epoch 秒(UTC),覆蓋上面選項")
    ap.add_argument("--hi", type=int, default=None, help="直接給 epoch 秒(UTC),覆蓋上面選項")
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
        raise SystemExit("起 > 訖,檢查一下時間順序。")

    minutes = read_minutes(lo, hi)
    events = read_events(lo, hi)

    print("═" * 72)
    print(f"每分鐘訂單流 · {tw(lo)} ~ {tw(hi)}(台北)· 共 {len(minutes)} 分鐘有資料")
    if not minutes:
        print("  這段沒有訂單流資料。可能:日期不對、Bookmap 沒在送、或早於長期存檔開始日(分鐘存檔自 2026-10-27 起)。")
    else:
        print(f"  {'時間':<13} {'Delta實':>7} {'成交量':>7} {'淨補單':>7} {'失衡':>6} {'現價BM':>10} {'累積Δ':>8} {'r':>7}")
        cum = 0.0
        spots = []
        n_tr = n_mbo = 0
        t_stack_bid = t_stack_ask = t_pull_bid = t_pull_ask = t_big_vol = t_vol = 0.0
        imbs = []
        for m in minutes:
            tr = m.get("delta_tr")
            use_tr = tr is not None
            d = _fnum(tr) if use_tr else _fnum(m.get("delta"))
            v = _fnum(m.get("vol_tr")) if use_tr else _fnum(m.get("vol"))
            src = "tr" if use_tr else "mbo"
            if src == "tr":
                n_tr += 1
            else:
                n_mbo += 1
            if d is not None:
                cum += d
            r = (d / v) if (d is not None and v) else None
            sb = _fnum(m.get("stack_bid")); sa = _fnum(m.get("stack_ask"))
            ns = (sb - sa) if (sb is not None and sa is not None) else None
            if sb is not None: t_stack_bid += sb
            if sa is not None: t_stack_ask += sa
            pb = _fnum(m.get("pull_bid")); pa = _fnum(m.get("pull_ask"))
            if pb is not None: t_pull_bid += pb
            if pa is not None: t_pull_ask += pa
            ib = _fnum(m.get("imb"))
            if ib is not None: imbs.append(ib)
            bv = _fnum(m.get("big_vol"))
            if bv is not None: t_big_vol += bv
            if v is not None: t_vol += v
            sm = _fnum(m.get("spot"))
            if sm is not None:
                spots.append(sm)
            ds = f"{d:+.1f}" if d is not None else "—"
            vs = f"{v:.0f}" if v is not None else "—"
            nss = f"{ns:+.1f}" if ns is not None else "—"
            ibs = f"{ib:+.2f}" if ib is not None else "—"
            cs = f"{cum:+.1f}"
            ss = f"{sm:,.2f}" if sm is not None else "—"
            rs = f"{r:+.3f}" if r is not None else "—"
            print(f"  {tw(m['t']):<13} {ds:>7} {vs:>7} {nss:>7} {ibs:>6} {ss:>10} {cs:>8} {rs:>7}")
        print("─" * 72)
        print(f"  合計淨量(累積 Delta){cum:+.1f} 口 → {'淨買(偏多累積)' if cum >= 0 else '淨賣(偏空累積)'}")
        if spots:
            print(f"  現價(Bridge)這段最高 {max(spots):,.2f} / 最低 {min(spots):,.2f} → 振幅 {max(spots) - min(spots):.1f} 點")
        print(f"  資料來源:實際成交(tr){n_tr} 分鐘、退回 MBO {n_mbo} 分鐘(後者可信度較低)")
        print("  ── 掛單簿 / 補單(「小量一直補」的暗流證據)──")
        print(f"  近價補單:買 {t_stack_bid:.0f} / 賣 {t_stack_ask:.0f} → 淨補單 {t_stack_bid - t_stack_ask:+.0f} 口(正 = 買方一直補)")
        print(f"  近價撤單:買 {t_pull_bid:.0f} / 賣 {t_pull_ask:.0f}")
        avg_imb = (sum(imbs) / len(imbs)) if imbs else None
        print(f"  平均掛單失衡 imb:{avg_imb:+.2f} (正 = 買方掛單厚)" if avg_imb is not None else "  平均掛單失衡 imb:—")
        if t_vol > 0:
            share = t_big_vol / t_vol
            verdict = "很小(符合「看不到大單」的小量暗流)" if share < 0.1 else "不低,可能不是純小量暗流"
            print(f"  大單量合計 {t_big_vol:.0f} 口,佔總成交 {share * 100:.0f}% → {verdict}")
        big_r = [abs((_fnum(m.get('delta_tr') if m.get('delta_tr') is not None else m.get('delta')) or 0) / (_fnum(m.get('vol_tr') if m.get('vol_tr') is not None else m.get('vol')) or 1))
                 for m in minutes if _fnum(m.get('vol_tr') if m.get('vol_tr') is not None else m.get('vol'))]
        if big_r:
            over = sum(1 for x in big_r if x >= 0.05)
            print(f"  r 的 |絕對值| ≥ 0.05(前端吸收背離的門檻)的分鐘數:{over}/{len(big_r)} → 若幾乎全 <0.05,那條線整段靜默是正常的")
        print("  讀法:淨補單買方持續為正 + 累積 Delta 偏負 + 現價振幅小 + 大單佔比低 = 「小量一直補、價格下不去」的看多暗流(反之亦然)。")

    print("\n【事件】")
    if not events:
        print("  這段沒有偵測到任何訂單流事件。")
    else:
        print(f"  {'時間':<14} {'種類':<7} {'承接方':<8} {'價位':>10} {'conf':>6}")
        for e in events:
            side = e.get("s")
            side_s = ("買(下方接)" if side == "buy" else "賣(上方接)") if side in ("buy", "sell") else "—"
            conf = (e.get("x") or {}).get("conf")
            p = _fnum(e.get("p"))
            print(f"  {tw(e['t']):<14} {KIND_NAME.get(e['k'], e['k']):<7} {side_s:<8} {p if p is not None else '—':>10} {conf if conf is not None else '—':>6}")
        c = Counter(e["k"] for e in events)
        print("  → " + "、".join(f"{KIND_NAME.get(k, k)} {v} 次" for k, v in sorted(c.items())))

    print("═" * 72)
    print("把上面整段複製貼給我,我幫你判讀(是不是盤整中的單邊暗流)。")


if __name__ == "__main__":
    main()
