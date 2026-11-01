#!/usr/bin/env python3
"""把已經存在的每日完整存檔(orderflow_archive/*.jsonl.gz)補進兩份長期存檔,讓歷史回放看得到舊的日子:
  orderflow_events_archive/   事件(吸收/冰山/力竭/掃單/停損)
  orderflow_minutes_archive/  每分鐘彙總(訂單流動能副圖、動能條用)
只追加、可重複執行(已經有的事件 id 與分鐘不會重複寫)。receiver 正在跑也沒關係(它也是追加)。
用法:  python3 backfill_orderflow_archive.py [--dry-run]
"""
import argparse, glob, gzip, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orderflow_store as ST
from orderflow_minutes import minute_rows

HIST_KEEP = {"ts", "received_at", "idle_sec", "lag_sec", "delta", "delta_buy",
             "delta_sell", "vol", "imb", "big", "big_vol", "sweep_buy", "sweep_sell", "spot",
             "imb_big", "delta_tr", "vol_tr", "cvd_tr", "cvd_mbo", "pull_bid", "pull_ask",
             "stack_bid", "stack_ask"}
EV_KEEP = ("id", "t", "k", "s", "p", "v", "x")


def process_file(path, events_dir, minutes_dir, dry=False):
    evs, recs = {}, []
    bad = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(r, dict):
                    continue
                if r.get("received_at"):
                    recs.append({k: r[k] for k in HIST_KEEP if k in r})
                for e in r.get("events") or []:
                    if isinstance(e, dict) and e.get("id") and isinstance(e.get("t"), (int, float)) \
                            and isinstance(e.get("p"), (int, float)) and e.get("k") in ST.KINDS:
                        evs[str(e["id"])] = {k: e[k] for k in EV_KEEP if k in e}
    except (OSError, EOFError) as ex:
        print(f"  讀取中斷({ex}),已讀到的部分照常處理")
    # 事件:去掉各日已經存過的 id
    new_ev = []
    have = {}
    for e in evs.values():
        d = ST.day_of(e["t"])
        if d not in have:
            have[d] = ST.event_ids(events_dir, d)
        if e["id"] not in have[d]:
            new_ev.append(e)
    # 分鐘:沒有、或已有的那列包數比較少的才補
    rows = minute_rows(recs)
    new_min, cache = [], {}
    for r in rows:
        d = ST.day_of(r["t"])
        if d not in cache:
            cache[d] = {x.get("t"): x.get("n") or 0 for x in ST._read_jsonl(os.path.join(minutes_dir, d + ".jsonl"))}
        if r["t"] not in cache[d] or (r.get("n") or 0) > cache[d][r["t"]]:
            new_min.append(r)
    if not dry:
        ST.append_events(events_dir, new_ev)
        ST.append_minutes(minutes_dir, new_min)
    return len(evs), len(new_ev), len(rows), len(new_min), bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=os.path.join(HERE, "orderflow_archive"))
    ap.add_argument("--events-dir", default=os.path.join(HERE, "orderflow_events_archive"))
    ap.add_argument("--minutes-dir", default=os.path.join(HERE, "orderflow_minutes_archive"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.archive, "*.jsonl.gz")))
    if not files:
        print("找不到每日存檔:", a.archive); return
    tot = [0, 0, 0, 0]
    for fp in files:
        t0 = time.time()
        n_ev, add_ev, n_min, add_min, bad = process_file(fp, a.events_dir, a.minutes_dir, a.dry_run)
        for i, v in enumerate((n_ev, add_ev, n_min, add_min)):
            tot[i] += v
        print(f"{os.path.basename(fp)}: 事件 {n_ev} 筆(新增 {add_ev})· 分鐘 {n_min} 列(新增 {add_min})"
              + (f" · 壞行 {bad}" if bad else "") + f" · {time.time() - t0:.1f}s")
    print(f"合計:事件 新增 {tot[1]}/{tot[0]}、分鐘 新增 {tot[3]}/{tot[2]}" + (" (dry-run,沒有寫入)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
