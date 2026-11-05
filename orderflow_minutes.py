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


def vpin_impact(mrows, window=30, min_rows=5):
    """從 minute_rows 的每分鐘彙總算兩個訂單流微觀結構量(實驗,未驗證):

      vpin  : 成交量分桶的「毒流」|Σdelta_tr| / Σvol_tr,近似 Easley 的 VPIN。0~1,
              高 = 有一段單邊資金持續主導(買賣不平衡被隱藏在大成交量裡),波動或將放大。
              delta_tr 是實際成交的淨買賣(買正賣負),所以 |Σdelta_tr|/Σvol_tr 量的是「主動性失衡」。
      impact: 每分鐘 |Δspot| / vol_tr 的中位數(點/口),近似 Kyle's Lambda。高 = 簿子薄、每口衝擊大。

    只取最後 window 分鐘;資料不足回 (None, None, n)。delta_tr / vol_tr 是 bridge「近 60 秒滾動累計」
    在每分鐘末的快照,連續分鐘約略不重疊,可當作該分鐘的淨量與量。"""
    rows = [m for m in mrows if (m.get("vol_tr") or 0) > 0 and m.get("delta_tr") is not None]
    rows = rows[-window:]
    n = len(rows)
    if n < min_rows:
        return None, None, n
    # VPIN:以「成交量」分桶(不是時間),桶大小 = 窗口總量 / 25,下限 200 口
    tot_v = sum(m["vol_tr"] for m in rows)
    bucket = max(200.0, tot_v / 25.0)
    bs, cd, cv = [], 0.0, 0.0
    for m in rows:
        cd += m["delta_tr"]
        cv += m["vol_tr"]
        if cv >= bucket:
            bs.append(abs(cd) / cv)
            cd = cv = 0.0
    if bs:
        vpin = sum(bs) / len(bs)
    else:
        vpin = sum(abs(m["delta_tr"]) / m["vol_tr"] for m in rows) / n
    # impact:每分鐘 |價格變動| / 成交量,取中位數(對 outlier 較穩)
    im = []
    for i in range(1, n):
        s0, s1 = rows[i - 1].get("spot"), rows[i].get("spot")
        v = rows[i]["vol_tr"]
        if s0 and s1 and v > 0:
            im.append(abs(s1 - s0) / v)
    impact = sorted(im)[len(im) // 2] if im else None
    return vpin, impact, n
