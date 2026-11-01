#!/usr/bin/env python3
"""wall_indicator_study.py — 價格碰到 GEX 牆/flip 時,Bookmap 指標跟「牆守住 or 被穿越」的關係(唯讀研究,不寫任何資料)。

做法(全部用歷史資料重放,不需要常駐):
  1. 用 1 分 K 找出每次碰牆(K 棒範圍離牆 2 點內、上一根收盤不貼牆、同一條牆 15 分鐘內只算一次)。
  2. 結果:碰牆後 ±40 點(離牆算)誰先到。往「來的方向」反向走 40 點先到 = 守住(bounce);
     往前穿過去 40 點先到 = 穿越(break);兩邊同根 K 棒或 30 分鐘內都沒到 = 不算。
  3. 特徵:碰牆前 5 分鐘(到碰牆那根收完)的 Bookmap 指標,只用碰牆當下已知的資料,不偷看之後。
     「推進方向」= 價格往牆的方向(從下方碰牆 = 向上推)。帶符號的指標以「推向牆」為正。
  4. 看兩件事:(a) 每個指標高的那一半 vs 低的那一半,守住機率差多少、巧合機率(打亂標籤)多大;
               (b) 指標彼此之間的關係(等級相關,看是不是在講同一件事)。

限制(重要):
  - 每分鐘彙總從 2026-10-27 起才有;訂單流事件從 2026-10-30 台北 00:33 起才有。樣本很少,結果只是線索。
  - 同時比較很多指標,偶然出現「看起來有關」的機率很高;不要只挑好看的一項,也不要據此調偵測門檻。
  - 重疊的碰牆(同一波行情碰多條牆)不是獨立樣本。
用法(VPS 專案資料夾):  python3 wall_indicator_study.py [--days 10] [--src NQ] [--scope all] [--all-hours]
"""
import argparse, math, os, random, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orderflow_store as ST
from flow_outcomes import (TOUCH_TOL, TOUCH_DEBOUNCE, flat_levels, outcome, in_session, wilson,
                           load_bars, bias_of, make_levels_provider)

WIN_BEFORE = 300          # 碰牆前看 5 分鐘
NEED_MIN = 3              # 這 5 分鐘至少要有 3 列分鐘資料才算有指標
NAMES = {
    "push_delta": "實際成交 Delta(推向牆為正)",
    "vol_tr": "成交量(活躍度)",
    "push_imb": "掛單失衡(推向牆為正)",
    "big_vol": "大單量",
    "push_sweep": "掃單量淨值(推向牆為正)",
    "ev_defend": "守牆方事件數(承接/冰山/力竭等指向守牆)",
    "ev_push": "推牆方事件數(掃單/停損等順推進方向)",
}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def features(minutes, events, ev_start, b0, push):
    """b0=碰牆那根 K 棒起點;push=+1 價格向上推(從下方碰牆)/ -1 向下。回傳 dict(缺資料的欄位是 None)。"""
    lo, hi = b0 - WIN_BEFORE, b0 + 60
    rows = [minutes[t] for t in range(lo // 60 * 60, hi, 60) if t in minutes]
    f = {k: None for k in NAMES}
    if len(rows) >= NEED_MIN:
        def s(key):
            vs = [_f(r.get(key)) for r in rows]
            vs = [v for v in vs if v is not None]
            return sum(vs) if len(vs) >= NEED_MIN else None
        d = s("delta_tr")
        f["push_delta"] = None if d is None else push * d
        f["vol_tr"] = s("vol_tr")
        im = [_f(r.get("imb")) for r in rows]
        im = [v for v in im if v is not None]
        f["push_imb"] = push * sum(im) / len(im) if im else None
        f["big_vol"] = s("big_vol")
        sb, ss = s("sweep_buy"), s("sweep_sell")
        f["push_sweep"] = None if sb is None or ss is None else push * (sb - ss)
    if ev_start is not None and lo >= ev_start:           # 事件功能上線之前沒有事件,不是「0 個事件」
        dfn = psh = 0
        for e in events:
            if lo <= e["t"] < hi:
                b = bias_of(e["k"], e["s"])
                if b == push:
                    psh += 1
                else:
                    dfn += 1
        f["ev_defend"], f["ev_push"] = dfn, psh
    return f


def find_touches(bars, levels_fn, names_ok, all_hours=False):
    """重放 1 分 K。levels_fn(t)→(epoch, spot, lv) 或 None;牆每 5 分鐘更新一次(跟快照頻率相近)。"""
    out, last_touch, lv_cache = [], {}, {}
    for i in range(1, len(bars)):
        b, pb = bars[i], bars[i - 1]
        if not all_hours and not in_session(b[0]):
            continue
        slot = b[0] // 300
        if slot not in lv_cache:
            try:
                lv_cache[slot] = levels_fn(b[0])
            except Exception:
                lv_cache[slot] = None
        lv = lv_cache[slot]
        if not lv:
            continue
        for name, L in flat_levels(lv[2]).items():
            if not names_ok(name):
                continue
            if not (b[3] - TOUCH_TOL <= L <= b[2] + TOUCH_TOL) or abs(pb[4] - L) <= TOUCH_TOL:
                continue
            key = (name, round(L))
            if key in last_touch and b[0] - last_touch[key] < TOUCH_DEBOUNCE:
                continue
            last_touch[key] = b[0]
            out.append(dict(t=b[0], name=name, level=L, push=1 if pb[4] < L else -1))
    return out


def label(bars, tc):
    """守住=1 / 穿越=0 / None(同根都碰到、30 分鐘都沒到、資料沒走完)。"""
    o = outcome(bars, tc["t"] + 30, tc["level"])
    if not o or o["first40"] not in ("up", "dn"):
        return None
    went_with_push = (o["first40"] == "up") == (tc["push"] == 1)       # 往推進方向穿過去
    return 0 if went_with_push else 1


# ----------------------------- 統計 -----------------------------
def ranks(v):
    idx = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and v[idx[j + 1]] == v[idx[i]]:
            j += 1
        for k in range(i, j + 1):
            r[idx[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(a, b):
    if len(a) < 5:
        return None
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else None


def split_test(vals, y, perm=5000, seed=1):
    """以中位數分「高/低」兩半,比較守住率;打亂標籤算巧合機率。樣本不足回 None。"""
    # 門檻取「讓高/低兩半最接近各一半」的值;只看指標本身、不看結果,所以不會偏袒任何一邊(事件數這種很多 0/1 的指標也能分)
    cand = sorted(set(vals))[:-1]
    if not cand:
        return None
    med = min(cand, key=lambda c: abs(sum(1 for v in vals if v > c) - len(vals) / 2))
    hi = [i for i, v in enumerate(vals) if v > med]
    lo = [i for i, v in enumerate(vals) if v <= med]
    if len(hi) < 5 or len(lo) < 5:
        return None
    rate = lambda ix: sum(y[i] for i in ix) / len(ix)
    obs = rate(hi) - rate(lo)
    rng, ys, c = random.Random(seed), list(y), 0
    for _ in range(perm):
        rng.shuffle(ys)
        if abs(sum(ys[i] for i in hi) / len(hi) - sum(ys[i] for i in lo) / len(lo)) >= abs(obs) - 1e-12:
            c += 1
    return dict(med=med, n_hi=len(hi), n_lo=len(lo), r_hi=rate(hi), r_lo=rate(lo), diff=obs, p=(c + 1) / (perm + 1),
                w_hi=sum(y[i] for i in hi), w_lo=sum(y[i] for i in lo))


def analyze(samples, perm=5000):
    """samples: list of (features dict, 守住 0/1)。"""
    res = dict(n=len(samples), hold=sum(s[1] for s in samples), per={}, corr=[])
    for k in NAMES:
        pairs = [(s[0][k], s[1]) for s in samples if s[0].get(k) is not None]
        if len(pairs) < 12:
            res["per"][k] = dict(n=len(pairs))
            continue
        vals, y = [p[0] for p in pairs], [p[1] for p in pairs]
        r = split_test(vals, y, perm)
        res["per"][k] = dict(n=len(pairs), rho=spearman(vals, y), split=r)
    keys = list(NAMES)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            pr = [(s[0][keys[i]], s[0][keys[j]]) for s in samples if s[0].get(keys[i]) is not None and s[0].get(keys[j]) is not None]
            if len(pr) >= 12:
                r = spearman([p[0] for p in pr], [p[1] for p in pr])
                if r is not None:
                    res["corr"].append((keys[i], keys[j], r, len(pr)))
    res["corr"].sort(key=lambda x: -abs(x[2]))
    return res


def render(res):
    n, h = res["n"], res["hold"]
    L = []
    if n == 0:
        return "沒有可用的碰牆樣本(資料不足,或碰牆後 30 分鐘內都沒走到 ±40 點)。"
    lo_, hi_ = wilson(h, n)
    L.append(f"可用的碰牆 {n} 次:牆守住(先反向走 40 點){h} 次 = {h / n:.0%};穿越 {n - h} 次。95% 信賴區間 {lo_:.0%}–{hi_:.0%}。")
    L.append("")
    L.append("【每個指標:高的那一半 vs 低的那一半,碰牆後牆守住的機率】")
    shown = 0
    for k, d in res["per"].items():
        if "split" not in d or d["split"] is None:
            why = f"樣本不足({d['n']} 筆)" if d["n"] < 12 else f"{d['n']} 筆裡大多數數值相同,切不出夠大的高/低兩半"
            L.append(f"  - {NAMES[k]}:{why},不判斷。")
            continue
        s = d["split"]
        shown += 1
        flag = "  ← 巧合機率低於 5%,但見下方提醒" if s["p"] < 0.05 else ""
        L.append(f"  - {NAMES[k]}:高的一半({s['n_hi']} 次)守住 {s['r_hi']:.0%},低的一半({s['n_lo']} 次)守住 {s['r_lo']:.0%},差 {s['diff'] * 100:+.0f} 個百分點;"
                 f"打亂標籤後這麼大的差距出現的機率 {s['p']:.0%}{flag}")
    L.append("")
    L.append("【指標彼此的關係(等級相關,-1~+1;絕對值大 = 常一起升降,可能在講同一件事)】")
    top = [c for c in res["corr"] if abs(c[2]) >= 0.3][:8]
    if not top:
        L.append("  沒有任何兩個指標的相關達到 0.3(或樣本不足)。")
    for a, b, r, m in top:
        L.append(f"  - {NAMES[a]} 與 {NAMES[b]}:{r:+.2f}({m} 次)")
    L.append("")
    L.append(f"提醒:同時比較了 {shown} 個指標,即使全部沒有關係,偶然也會有一兩個巧合機率低於 5%;")
    L.append("樣本很少且重疊碰牆不是獨立樣本。這份結果只是線索,不能拿來調偵測門檻,也不能說某個指標有預測力。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--src", default="NQ")
    ap.add_argument("--scope", default="all", help="all=全部到期日;zero=只看當天到期")
    ap.add_argument("--all-hours", action="store_true")
    ap.add_argument("--db", default=os.path.join(HERE, "nq_gex.sqlite"))
    ap.add_argument("--minutes", default=os.path.join(HERE, "orderflow_minutes_archive"))
    ap.add_argument("--events", default=os.path.join(HERE, "orderflow_events_archive"))
    ap.add_argument("--perm", type=int, default=5000)
    a = ap.parse_args()
    hi = int(time.time()); lo = hi - a.days * 86400
    bars = load_bars(a.db, lo - 3600, hi)
    minutes = ST.read_minutes(a.minutes, lo - 3600, hi)
    events = ST.read_events(a.events, lo - 3600, hi)
    ev_start = min((e["t"] for e in events), default=None)
    prefix = f"{a.src}|{a.scope}|"
    touches = find_touches(bars, make_levels_provider(), lambda n: n.startswith(prefix), a.all_hours)
    samples, skipped = [], 0
    for tc in touches:
        y = label(bars, tc)
        if y is None:
            skipped += 1
            continue
        samples.append((features(minutes, events, ev_start, tc["t"], tc["push"]), y))
    print(f"來源 {a.src}({a.scope}),最近 {a.days} 天,{'全部時段' if a.all_hours else '美東 09:30–13:30'}")
    print(f"K 棒 {len(bars)} 根、分鐘彙總 {len(minutes)} 列、事件 {len(events)} 筆;碰牆 {len(touches)} 次,"
          f"其中 {skipped} 次沒有明確結果(30 分鐘內沒走到 ±40 點 / 兩邊同根 / 資料沒走完)被排除")
    print(render(analyze(samples, a.perm)))


if __name__ == "__main__":
    main()
