#!/usr/bin/env python3
"""選擇權大單 vs 期貨主動買賣:有沒有可用的方向線索?(研究用,不改任何資料)

問題:選擇權大單(網站上的 surge 事件)出現時,NQ 期貨的主動買賣差(delta)是不是系統性偏向某一邊?
  想法(推論,不是事實):莊家賣出選擇權後會用期貨對沖,所以大單前後期貨 delta 可能有痕跡。
  方法:把大單事件分成 Call 與 Put 兩組,看每組事件「前 5 分鐘 / 這 5 分鐘 / 後 5 分鐘」期貨 delta
  的平均與「delta 為正」的比例,再用打亂 Call/Put 標籤 5000 次(排列檢定)問:兩組的差距會不會只是巧合?
  「前 5 分鐘」是對照組:對沖不可能發生在大單之前,若前面就有差距,代表只是價格走勢帶動(追價),不是對沖。

限制(請一起看):
  * 選擇權資料是每 ~5 分鐘一張快照,事件只知道落在哪個 5 分鐘內,對不準秒級。
  * 期貨 delta 取自 orderflow 存檔(orderflow_archive/*.jsonl.gz)每分鐘最後一筆的 60 秒滾動值(delta_tr 優先)。
  * 沒有買賣身分;就算有差距也只是統計關聯,不能證明誰買誰賣。
  * 事件數少時結果不可靠;預設只看美東 09:30–13:30。
用法:python3 opt_hedge_check.py [--days 10] [--src NQ] [--all-hours]
"""
import argparse, glob, gzip, json, math, os, random, sys, time
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from orderflow_minutes import minute_rows
from flow_outcomes import in_session, wilson

STEP = 300   # 5 分鐘


def load_minute_delta(arch_dir, lo, hi):
    """{分鐘起點 epoch: delta}。從每日存檔讀,只取 [lo-1h, hi+1h]。"""
    recs = []
    d0 = time.strftime("%Y%m%d", time.gmtime(lo - 3600))
    d1 = time.strftime("%Y%m%d", time.gmtime(hi + 3600))
    for fn in sorted(glob.glob(os.path.join(arch_dir, "*.jsonl.gz"))):
        day = os.path.basename(fn)[:8]
        if not (d0 <= day <= d1):
            continue
        try:
            with gzip.open(fn, "rt", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    ra = r.get("received_at")
                    if ra and lo - 3600 <= ra <= hi + 3600:
                        recs.append({k: r.get(k) for k in ("received_at", "delta_tr", "delta")})
        except (OSError, EOFError):
            continue                     # 檔案正在寫入或壞掉的尾端:跳過
    out = {}
    for row in minute_rows(recs):
        v = row.get("delta_tr")
        if v is None:
            v = row.get("delta")
        if v is not None:
            out[row["t"]] = v
    return out


def window_sum(md, a, b, need=4):
    """[a,b) 內分鐘 delta 加總;分鐘數不足 need 回 None。"""
    vals = [md[t] for t in range(int(a // 60) * 60, int(b), 60) if t in md]
    return sum(vals) if len(vals) >= need else None


def analyze(events, md, perm=5000, seed=1):
    """events: [{t, cp}];回傳 dict。"""
    rows = []
    for e in events:
        w = {k: window_sum(md, e["t"] + a, e["t"] + b) for k, (a, b) in
             dict(before=(-2 * STEP, -STEP), during=(-STEP, 0), after=(0, STEP)).items()}
        if all(v is not None for v in w.values()):
            rows.append(dict(cp=e["cp"], **w))
    res = dict(n_events=len(events), n_used=len(rows), win={})
    for k in ("before", "during", "after"):
        c = [r[k] for r in rows if r["cp"] == "C"]
        p = [r[k] for r in rows if r["cp"] == "P"]
        if not c or not p:
            res["win"][k] = None
            continue
        diff = sum(c) / len(c) - sum(p) / len(p)
        allv = [r[k] for r in rows]
        labels = [r["cp"] == "C" for r in rows]
        rnd = random.Random(seed)
        ge = 0
        for _ in range(perm):
            rnd.shuffle(labels)
            cc = [v for v, l in zip(allv, labels) if l]
            pp = [v for v, l in zip(allv, labels) if not l]
            d = sum(cc) / len(cc) - sum(pp) / len(pp)
            if abs(d) >= abs(diff):
                ge += 1
        res["win"][k] = dict(nC=len(c), nP=len(p), meanC=sum(c) / len(c), meanP=sum(p) / len(p), diff=diff,
                             posC=sum(1 for v in c if v > 0), posP=sum(1 for v in p if v > 0),
                             p=(ge + 1) / (perm + 1))
    return res


NAME = dict(before="大單前 5 分鐘(對照)", during="大單這 5 分鐘", after="大單後 5 分鐘")


def render(res):
    L = [f"選擇權大單事件 {res['n_events']} 筆,其中期貨資料完整可用 {res['n_used']} 筆。"]
    if res["n_used"] < 30:
        L.append("⚠ 可用事件不到 30 筆,以下數字誤差很大,只能當參考。")
    L.append("看法:Call 大單與 Put 大單之後,期貨主動買減賣(delta)有沒有明顯不同。")
    for k in ("before", "during", "after"):
        w = res["win"].get(k)
        if not w:
            L.append(f"  {NAME[k]}:Call 或 Put 其中一組沒有事件,無法比較")
            continue
        lc = wilson(w["posC"], w["nC"]); lp = wilson(w["posP"], w["nP"])
        L.append(f"  {NAME[k]}:Call 組 {w['nC']} 筆,平均 delta {w['meanC']:+.0f} 口,delta 為正的比例 {w['posC']/w['nC']:.0%}"
                 f"(95% 範圍 {lc[0]:.0%}~{lc[1]:.0%});Put 組 {w['nP']} 筆,平均 {w['meanP']:+.0f} 口,為正 {w['posP']/w['nP']:.0%}"
                 f"(95% 範圍 {lp[0]:.0%}~{lp[1]:.0%});差 {w['diff']:+.0f} 口,巧合機率 p={w['p']:.2f}")
    b, d, a = (res["win"].get(k) for k in ("before", "during", "after"))
    L.append("")
    L.append("怎麼讀:")
    L.append("  * p 越小代表『Call 組跟 Put 組的差距只是巧合』越不可能;一般要 p<0.05 才值得注意,而且要重複在不同日子成立。")
    L.append("  * 只有『大單這 5 分鐘』或『之後』有差、『之前』沒差,才比較像對沖痕跡;『之前』就有差,代表只是價格走勢帶動追價。")
    if a and b and a["p"] < 0.05 and b["p"] >= 0.05:
        L.append("  * 本次:之後有明顯差距而之前沒有。仍不證明誰買誰賣,且需要在更多天重複確認。")
    elif a and a["p"] >= 0.05 and (not d or d["p"] >= 0.05):
        L.append("  * 本次:看不出 Call 與 Put 大單後期貨 delta 有系統性差異(也可能是樣本太少或 5 分鐘太粗)。")
    else:
        L.append("  * 本次結果混雜,請對照上面各窗口的 p 值自行判斷,不要只挑好看的一項。")
    return "\n".join(L)


def fetch_events(db, lo, hi, src, all_hours):
    import gex_web_server as G
    con = G.connect(db)
    seen, out = set(), []
    t = lo
    while t < hi:
        t2 = min(hi, t + 3 * 3600)           # api_events 只回最後 80 筆,所以切成 3 小時一段
        try:
            evs = G.api_events(con, {"from": t, "to": t2, "src": src}).get("events", [])
        except Exception as ex:
            print("讀取事件失敗:", ex, file=sys.stderr); evs = []
        for e in evs:
            k = (e["t"], e["strike"], e["cp"])
            if k in seen or e["cp"] not in ("C", "P"):
                continue
            seen.add(k)
            if all_hours or in_session(e["t"]):
                out.append(dict(t=int(e["t"]), cp=e["cp"], dvol=e.get("dvol")))
        t = t2
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--src", default="NQ")
    ap.add_argument("--all-hours", action="store_true")
    ap.add_argument("--db", default=os.path.join(HERE, "nq_gex.sqlite"))
    ap.add_argument("--arch", default=os.path.join(HERE, "orderflow_archive"))
    ap.add_argument("--perm", type=int, default=5000)
    a = ap.parse_args()
    hi = int(time.time()); lo = hi - a.days * 86400
    evs = fetch_events(a.db, lo, hi, a.src, a.all_hours)
    md = load_minute_delta(a.arch, lo, hi)
    print(f"來源 {a.src},最近 {a.days} 天,{'全部時段' if a.all_hours else '美東 09:30–13:30'};期貨 delta 分鐘資料 {len(md)} 筆")
    print(render(analyze(evs, md, perm=a.perm)))


if __name__ == "__main__":
    main()
