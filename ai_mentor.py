# -*- coding: utf-8 -*-
"""ai_mentor.py — 「AI 導師(原始數據版)」。

和舊的「問導師」不同:這裡**不給**規則版的方向、分數、flip、牆、信心這類整理過的結論,
只餵原始數據(各履約價的 OI / 成交量 / gamma、1 分 K、訂單流每分鐘歷史、大單),
讓 AI 自己整理、自己下判斷。回答最後必須附一個固定格式的 <verdict> 區塊(方向 / 判錯價位 / 關鍵價位),
由程式解析後存進 mentor_log(kind='ai'),並用跟規則版同一套 ±40 點 / 30 分鐘規則自動判對錯,
之後可以和規則版、永遠做多、永遠做空並排比較。

本模組不連資料庫、不連網路:資料由呼叫端(gex_web_server)準備好傳進來,方便用合成資料測試。
"""
import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
TW = ZoneInfo("Asia/Taipei")

NEAR_PTS = 250          # 只列現價 ±250 點(NQ 座標)內的履約價
MAX_DTE_RAW = 5         # 原始列只列 5 天內到期;更遠的只給總和
MAX_ROWS_PER_SRC = 150  # 每個來源最多列幾列(取 |gex| 最大的,再依履約價排序)
BARS_1M = 90            # 最近 90 根 1 分 K
BARS_5M_HOURS = 5       # 更早的 5 小時用 5 分 K

SYSTEM_RAW = (
    "你是一位 NQ(那斯達克100期貨)日內交易的分析師,服務對象是只做紐約盤(美東 09:30–13:30)、"
    "以 1 分 K 操作、目標約 40 點、停損約 40 點(1:1)的交易者。\n"
    "下面給你的是**原始數據**,沒有任何人先幫你下結論:請你自己整理、自己判斷。\n\n"
    "【工作方式】\n"
    "1) 先分開看三件事,各自寫一小段:\n"
    "   A. 選擇權結構(NQ / QQQ / NDX 的 OI、成交量、gamma 在哪些履約價集中;0DTE 與其他到期日分開看)。\n"
    "   B. 訂單流(Bookmap:成交 Delta、CVD、掛單簿不平衡、掃單、近價撤單/加單)。\n"
    "   C. 價格行為(1 分 K 與 5 分 K 的結構、相對關鍵價位的位置)。\n"
    "2) 再回答:這三件事彼此一致還是矛盾?矛盾時你信哪個、為什麼?\n"
    "3) 不要只描述最近幾根 K 棒然後順勢下結論;價格是用來檢驗結構判斷的,不是結論本身。"
    "如果你的判斷其實只是「剛剛在漲所以偏多」,請直接承認並降低信心或選擇觀望。\n"
    "4) 要承擔判斷:給出方向,或明確說「觀望」並說明什麼條件出現才會有方向。\n\n"
    "【資料說明與限制,請照實引用】\n"
    "- GEX 正負號是慣例:call 記正、put 記負,不是已驗證的事實(不知道莊家真實部位)。\n"
    "- OI 是前一日數字;成交量沒有買賣方向,也分不出莊家 / 客戶。\n"
    "- QQQ / NDX 的履約價是用價格比例換算成 NQ 座標(strike_nq),忽略基差變動,有誤差。\n"
    "- 訂單流欄位 delta_tr / cvd_tr 是新加的「實際成交」版本,尚未和 Bookmap CVD 手動核對;delta(MBO 推判)可能低估。"
    "pull_* / stack_* 是近價撤單/加單量,被成交吃掉的委託也可能被算成撤單。請在引用時帶著這些保留。\n"
    "- 若資料標示過時、缺漏或沿用舊值,要在回答裡說明它對你判斷的影響。\n"
    "- 你可能算錯大表格裡的加總;給你的「總和」是程式算好的,個別履約價的結論請引用具體數字。\n\n"
    "【輸出規則】用繁體中文;不要 markdown 符號(不要 **、#、-);不要說「保證」「一定」;不喊單。"
    "篇幅控制在約 500–900 字。\n\n"
    "【最後一定要附一個判定區塊】,格式固定,放在回答最後一行,單行 JSON,不要換行:\n"
    "<verdict>{\"direction\":\"偏多\" 或 \"偏空\" 或 \"觀望\",\"confidence\":0到100的整數,"
    "\"invalid_price\":你認錯的價位(NQ),\"horizon_min\":30,"
    "\"key_levels\":[{\"p\":價位,\"label\":\"短標籤\",\"role\":\"support\" 或 \"resistance\" 或 \"pivot\"}(最多 8 個)],"
    "\"one_line\":\"一句話理由\"}</verdict>\n"
    "direction 是觀望時 invalid_price 可為 null。判定窗是 30 分鐘內先碰順向 40 點還是反向 40 點,"
    "請讓你的 direction 對應這個窗口,不是對全天的看法。"
)


def _hm(epoch, tz):
    return dt.datetime.fromtimestamp(epoch, tz).strftime("%H:%M")


def _f(x, nd=1):
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _bars_section(bars, now):
    """bars: [(t,o,h,l,c,v)] 依時間排序(1 分 K)。前面較早的壓成 5 分 K。時間用紐約時間。"""
    if not bars:
        return ["(沒有 K 線資料)"]
    last = bars[-BARS_1M:]
    cut = last[0][0]
    older = [b for b in bars if b[0] < cut and b[0] >= cut - BARS_5M_HOURS * 3600]
    out = []
    if older:
        out.append(f"-- 5 分 K(紐約時間,之前 {BARS_5M_HOURS} 小時):time o h l c vol")
        g = {}
        for b in older:
            g.setdefault(b[0] // 300 * 300, []).append(b)
        for k in sorted(g):
            r = g[k]
            out.append(f"{_hm(k, NY)} {_f(r[0][1])} {_f(max(x[2] for x in r))} {_f(min(x[3] for x in r))} {_f(r[-1][4])} {_f(sum((x[5] or 0) for x in r), 0)}")
    out.append(f"-- 1 分 K(紐約時間,最近 {len(last)} 根):time o h l c vol")
    for b in last:
        out.append(f"{_hm(b[0], NY)} {_f(b[1])} {_f(b[2])} {_f(b[3])} {_f(b[4])} {_f(b[5] or 0, 0)}")
    return out


def _options_section(df, spot, carried):
    """df: pandas DataFrame,欄位 src, expiry, dte, cp, strike_nq, oi, vol, gex, gex_vol, iv。"""
    out = []
    if df is None or len(df) == 0:
        return ["(沒有選擇權資料)"]
    for src in ("NQ", "QQQ", "NDX"):
        sub = df[df["src"] == src]
        if len(sub) == 0:
            out.append(f"== {src}:沒有資料 ==")
            continue
        note = ""
        if carried and src in carried:
            note = f"(注意:沿用 {_hm(carried[src], NY)} 紐約時間的舊快照,非即時)"
        out.append(f"== {src}{note} ==")
        # 總和(程式算好的算術,不是判斷)
        for name, m in (("0DTE", sub["dte"] == 0), ("1–5 天", (sub["dte"] >= 1) & (sub["dte"] <= MAX_DTE_RAW)), (f">{MAX_DTE_RAW} 天", sub["dte"] > MAX_DTE_RAW)):
            s2 = sub[m]
            if len(s2) == 0:
                continue
            c, p = s2[s2["cp"] == "C"], s2[s2["cp"] == "P"]
            out.append(f"總和 {name}:OI-gex Call {_f(c['gex'].sum() / 1e6, 1)}M / Put {_f(p['gex'].sum() / 1e6, 1)}M;"
                       f"成交量-gex Call {_f(c['gex_vol'].sum() / 1e6, 1)}M / Put {_f(p['gex_vol'].sum() / 1e6, 1)}M;"
                       f"成交量 Call {_f(c['vol'].sum(), 0)} / Put {_f(p['vol'].sum(), 0)};OI Call {_f(c['oi'].sum(), 0)} / Put {_f(p['oi'].sum(), 0)}")
        near = sub[(sub["strike_nq"] - spot).abs() <= NEAR_PTS]
        near = near[near["dte"] <= MAX_DTE_RAW]
        if len(near) > MAX_ROWS_PER_SRC:
            near = near.assign(_a=near["gex"].abs()).nlargest(MAX_ROWS_PER_SRC, "_a")
        near = near.sort_values(["strike_nq", "dte", "cp"])
        out.append(f"逐列(現價 ±{NEAR_PTS} 點、{MAX_DTE_RAW} 天內到期;最多 {MAX_ROWS_PER_SRC} 列,取 |gex| 大者):"
                   "strike_nq dte cp oi vol gex_M(oi) gex_vol_M iv")
        for r in near.itertuples():
            out.append(f"{_f(r.strike_nq, 0)} {int(r.dte)} {r.cp} {_f(r.oi, 0)} {_f(r.vol, 0)} {_f(r.gex / 1e6, 2)} {_f(r.gex_vol / 1e6, 2)} {_f(r.iv, 3)}")
    return out


def _orderflow_section(hist, now_pkt, status):
    out = []
    out.append(f"狀態:{status}")
    if now_pkt:
        keys = ("delta", "delta_tr", "cvd_tr", "cvd_mbo", "vol", "vol_tr", "imb", "imb_big", "big", "big_vol", "sweep_buy", "sweep_sell",
                "pull_bid", "pull_ask", "stack_bid", "stack_ask")
        out.append("最新一筆:" + " ".join(f"{k}={_f(now_pkt.get(k), 2)}" for k in keys if now_pkt.get(k) is not None))
        for side, key in (("買方掛單簿(價,量)", "book_bid"), ("賣方掛單簿(價,量)", "book_ask")):
            items = sorted(now_pkt.get(key) or [], key=lambda x: -(float(x[1]) if len(x) > 1 else 0))[:8]
            if items:
                out.append(f"{side}最大幾檔:" + " ".join(f"{_f(i[0], 2)}:{_f(i[1], 0)}" for i in items))
    if hist:
        import orderflow_minutes as OM
        rows = OM.minute_rows(hist)
        out.append("每分鐘彙整(紐約時間;bridge 的 delta/vol/pull/stack 是「近 60 秒滾動累計」,這裡取每分鐘最後一筆,約等於該分鐘的量;"
                   "cvd_tr 為累計值取分鐘最後值,imb 取分鐘平均):"
                   "time delta delta_tr vol cvd_tr imb pull_b pull_a stack_b stack_a")
        for r in rows[-60:]:
            out.append(f"{_hm(r['t'], NY)} {_f(r['delta'], 0)} {_f(r['delta_tr'], 0)} {_f(r['vol'], 0)} "
                       f"{_f(r['cvd_tr'], 0)} {_f(r['imb'], 2)} "
                       f"{_f(r['pull_bid'], 0)} {_f(r['pull_ask'], 0)} {_f(r['stack_bid'], 0)} {_f(r['stack_ask'], 0)}")
    else:
        out.append("(沒有訂單流歷史)")
    return out


def build_packet(now, spot, bars, df, carried, of_hist, of_now, of_status, big_prints=None, snapshot_epoch=None):
    """組成給 AI 的原始數據文字。回傳 (text, stats)。"""
    L = []
    L.append(f"【時間】台北 {_hm(now, TW)} / 紐約 {_hm(now, NY)}({dt.datetime.fromtimestamp(now, NY).strftime('%Y-%m-%d %a')});"
             f"NQ 現價 {_f(spot, 2)}。選擇權快照時間(紐約){_hm(snapshot_epoch, NY) if snapshot_epoch else '?'}。")
    L.append("【K 線】")
    L += _bars_section(bars, now)
    L.append("【選擇權原始資料】(gex 單位:美元/每 1% 價格變動,以百萬計;call 正、put 負為慣例)")
    L += _options_section(df, spot, carried)
    if big_prints:
        L.append("【今日大單紀錄】(近 30 分鐘 0DTE 大量成交,系統偵測)")
        L.append(big_prints)
    L.append("【Bookmap 訂單流原始資料】")
    L += _orderflow_section(of_hist, of_now, of_status)
    text = "\n".join(L)
    return text, dict(chars=len(text), lines=len(L), est_tokens=int(len(text) / 2.2))


_V = re.compile(r"<verdict>(.*?)</verdict>", re.S | re.I)


def parse_verdict(answer):
    """從 AI 回答抓 <verdict>{json}</verdict>。回傳 (去掉判定區塊的正文, verdict dict 或 None, 錯誤說明或 None)。"""
    if not answer:
        return "", None, "空回應"
    m = _V.search(answer)
    body = _V.sub("", answer).strip()
    if not m:
        return body, None, "回答裡沒有 <verdict> 判定區塊"
    try:
        v = json.loads(m.group(1).strip())
    except Exception as e:
        return body, None, f"<verdict> 不是合法 JSON:{e}"
    d = v.get("direction")
    if d not in ("偏多", "偏空", "觀望"):
        return body, None, f"direction 不合規:{d!r}"
    try:
        conf = int(v.get("confidence"))
    except (TypeError, ValueError):
        conf = None
    inv = v.get("invalid_price")
    try:
        inv = float(inv) if inv is not None else None
    except (TypeError, ValueError):
        inv = None
    lv = []
    for x in (v.get("key_levels") or [])[:8]:
        try:
            lv.append(dict(p=float(x["p"]), label=str(x.get("label") or "")[:14], role=x.get("role") if x.get("role") in ("support", "resistance", "pivot") else "pivot"))
        except Exception:
            continue
    return body, dict(direction=d, confidence=max(0, min(100, conf)) if conf is not None else None, invalid_price=inv,
                      horizon_min=v.get("horizon_min") or 30, key_levels=lv, one_line=str(v.get("one_line") or "")[:200]), None


# 估價:flash 非尖峰價(第三方整理頁 2026-10-18 的數字,未對照官方文件);其他模型不估
_PRICE = {"deepseek-flash": (0.15, 0.60)}


def est_cost_usd(model, usage):
    pr = _PRICE.get(model)
    if not pr or not usage:
        return None
    try:
        return round((usage.get("prompt_tokens", 0) * pr[0] + usage.get("completion_tokens", 0) * pr[1]) / 1e6, 4)
    except Exception:
        return None
