"""把 orderflow_history.jsonl 的逐秒紀錄整理成「每分鐘一列」。

重要:orderflow_bridge 每秒送的 delta / vol / big / big_vol / sweep / pull / stack 都是「最近 60 秒的滾動累計」
(WINDOW_SEC = 60),不是這一秒的增量。所以同一筆成交會出現在連續約 60 筆紀錄裡。
把逐秒欄位直接加總會把量放大約 60 倍。正確做法:每分鐘取「該分鐘最後一筆」的滾動值(它剛好涵蓋這一分鐘),
再把分鐘值相加才是累積量。cvd_tr / cvd_mbo 本來就是從 bridge 啟動起的累計,取分鐘最後值即可。
imb / imb_big 是瞬時比值,取分鐘平均。
"""

ROLL_KEYS = ("delta", "delta_buy", "delta_sell", "delta_tr", "vol", "vol_tr", "big", "big_vol",
             "sweep_buy", "sweep_sell", "pull_bid", "pull_ask", "stack_bid", "stack_ask")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def minute_rows(recs, step=60):
    """recs: 歷史紀錄 list(需有 received_at)。回傳依時間排序的 list of dict:
    t(分鐘起點 epoch 秒)、n(該分鐘紀錄筆數)、滾動欄位(分鐘最後一筆的值,沒有就是 None)、
    cvd_tr / cvd_mbo(分鐘最後值)、imb / imb_big(分鐘平均)、spot(分鐘最後值)。"""
    g = {}
    for r in recs:
        ra = _num(r.get("received_at"))
        if ra:
            g.setdefault(int(ra // step) * step, []).append((ra, r))
    out = []
    for t in sorted(g):
        rs = [x[1] for x in sorted(g[t], key=lambda x: x[0])]
        row = dict(t=t, n=len(rs))
        for k in ROLL_KEYS:
            row[k] = None
            for r in reversed(rs):
                v = _num(r.get(k))
                if v is not None:
                    row[k] = v
                    break
        for k in ("cvd_tr", "cvd_mbo", "spot"):
            row[k] = None
            for r in reversed(rs):
                v = _num(r.get(k))
                if v is not None:
                    row[k] = v
                    break
        for k in ("imb", "imb_big"):
            vs = [v for v in (_num(r.get(k)) for r in rs) if v is not None]
            row[k] = (sum(vs) / len(vs)) if vs else None
        out.append(row)
    return out
