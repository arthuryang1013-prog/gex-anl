# -*- coding: utf-8 -*-
"""mentor_log.py — 導師評論存檔、自動結算、每日總結。

做三件事:
 1) log_comment():把每則導師評論(文字 + 當時的分數/環境/關卡/輸入摘要)寫進 mentor_log.sqlite,永久保存。
 2) settle():用 1 分 K 判定「評論之後 30 分鐘內,先碰 +40 還是 -40」。對錯由程式判定,不靠 AI。
 3) daily():某個美東日的統計(命中率、對照永遠做多/做空、新舊分數對照)+ 逐則時間線 + 日結敘事。

資料庫放獨立檔案 mentor_log.sqlite(不放 web_cache.sqlite,因為那個會定期清舊快取)。已在 .gitignore 的 *.sqlite。
"""
import datetime as dt
import json
import os
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "mentor_log.sqlite")
NY = ZoneInfo("America/New_York")
TW = ZoneInfo("Asia/Taipei")
X_PTS = 40.0
HOLD_MIN = 30
COST_PTS = 2.0
THESIS_MIN_GAP = 300          # 自動產生的導師結論,最多每 5 分鐘存一則(網頁每次重新整理都會算一次,不能全存)
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mentor_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t REAL NOT NULL, kind TEXT NOT NULL,            -- kind: thesis(規則版自動結論) / ask(問導師) / ai(AI 導師原始數據版)
  spot REAL, direction TEXT, score INTEGER, score_dir INTEGER, regime TEXT, regime_s REAL,
  strength TEXT, confidence INTEGER,
  flip REAL, call_wall REAL, put_wall REAL, band_up REAL, band_dn REAL,
  text TEXT, question TEXT, inputs TEXT, ai INTEGER DEFAULT 0,
  settled INTEGER DEFAULT 0, outcome TEXT, out_t REAL, mfe REAL, mae REAL
);
CREATE INDEX IF NOT EXISTS idx_ml_t ON mentor_log(t);
CREATE TABLE IF NOT EXISTS daily_summary (
  day TEXT PRIMARY KEY, text TEXT, created REAL, n_comments INTEGER, ai INTEGER
);
"""


def _con(path=None):
    c = sqlite3.connect(path or LOG_PATH, timeout=10)
    c.execute("PRAGMA busy_timeout=8000")
    c.executescript(_SCHEMA)
    return c


def log_comment(kind, t, spot, text, direction=None, score=None, score_dir=None, regime=None, regime_s=None,
                strength=None, confidence=None, levels=None, question=None, inputs=None, ai=False, path=None):
    """寫一則評論;回傳 id。自動結論太密時略過(回傳 None)。任何錯誤都吞掉,不能影響網站。"""
    try:
        lv = levels or {}
        with _lock:
            c = _con(path)
            try:
                if kind == "thesis":
                    r = c.execute("SELECT MAX(t) FROM mentor_log WHERE kind='thesis'").fetchone()
                    if r and r[0] is not None and 0 <= t - r[0] < THESIS_MIN_GAP:
                        return None
                cur = c.execute(
                    "INSERT INTO mentor_log (t,kind,spot,direction,score,score_dir,regime,regime_s,strength,confidence,"
                    "flip,call_wall,put_wall,band_up,band_dn,text,question,inputs,ai) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t, kind, spot, direction, score, score_dir, regime, regime_s, strength, confidence,
                     lv.get("flip"), lv.get("call_wall"), lv.get("put_wall"), lv.get("band_up"), lv.get("band_dn"),
                     text, question, json.dumps(inputs, ensure_ascii=False) if inputs is not None else None, 1 if ai else 0))
                c.commit()
                return cur.lastrowid
            finally:
                c.close()
    except Exception:
        return None


def first_touch(bars, t0, spot, X=X_PTS, hold=HOLD_MIN):
    """bars: [(t,h,l)] 依時間排序。回傳 (outcome, out_t, mfe_up, mae_down);outcome ∈ up/down/both/timeout/None(K 棒還沒走完)。
    mfe_up / mae_down 是窗口內最大向上 / 向下的點數(相對 spot)。"""
    end = t0 + hold * 60
    hi = lo = None
    last_t = None
    for (t, h, l) in bars:
        if t <= t0:
            continue
        if t > end:
            break
        last_t = t
        hi = h if hi is None else max(hi, h)
        lo = l if lo is None else min(lo, l)
        up, dn = h >= spot + X, l <= spot - X
        if up and dn:
            return "both", t, hi - spot, spot - lo
        if up:
            return "up", t, hi - spot, spot - lo
        if dn:
            return "down", t, hi - spot, spot - lo
    if last_t is None or last_t < end - 120:
        return None, None, None, None
    return "timeout", last_t, (hi - spot) if hi is not None else None, (spot - lo) if lo is not None else None


def settle(bars_con, path=None, X=X_PTS, hold=HOLD_MIN):
    """把還沒結算、且 30 分鐘已過的評論結算掉。回傳新結算筆數。bars_con = 有 bars 表的連線。"""
    try:
        mx = bars_con.execute("SELECT MAX(t) FROM bars").fetchone()[0]
    except Exception:
        return 0
    if mx is None:
        return 0
    n = 0
    with _lock:
        c = _con(path)
        try:
            rows = c.execute("SELECT id, t, spot FROM mentor_log WHERE settled=0 AND spot IS NOT NULL AND t <= ? ORDER BY t",
                             (mx - hold * 60,)).fetchall()
            for (i, t0, spot) in rows:
                bars = bars_con.execute("SELECT t,h,l FROM bars WHERE t > ? AND t <= ? ORDER BY t", (t0, t0 + hold * 60 + 60)).fetchall()
                out, ot, mfe, mae = first_touch(bars, t0, spot, X, hold)
                if out is None:
                    continue
                c.execute("UPDATE mentor_log SET settled=1, outcome=?, out_t=?, mfe=?, mae=? WHERE id=?", (out, ot, mfe, mae, i))
                n += 1
            c.commit()
        finally:
            c.close()
    return n


def verdict(direction, outcome):
    """命中 / 未命中 / 逾時 / 同棒(無法判定) / 無方向 / 待結算"""
    if outcome is None:
        return "待結算"
    if direction not in ("偏多", "偏空"):
        return "無方向"
    if outcome == "timeout":
        return "逾時"
    if outcome == "both":
        return "同棒"
    return "命中" if (direction == "偏多") == (outcome == "up") else "未命中"


def _group(rows, dir_of):
    """rows: dict 列;dir_of(row) -> 該列要用的方向。回傳 n / hit / miss / timeout / both / 命中率。"""
    g = dict(n=0, hit=0, miss=0, timeout=0, both=0)
    for r in rows:
        v = verdict(dir_of(r), r["outcome"])
        if v in ("待結算", "無方向"):
            continue
        g["n"] += 1
        if v == "命中":
            g["hit"] += 1
        elif v == "未命中":
            g["miss"] += 1
        elif v == "逾時":
            g["timeout"] += 1
        else:
            g["both"] += 1
    dec = g["hit"] + g["miss"]
    g["rate"] = round(100.0 * g["hit"] / dec, 1) if dec else None
    g["decided"] = dec
    return g


def _ny_bounds(day):
    d = dt.date.fromisoformat(day)
    a = dt.datetime(d.year, d.month, d.day, tzinfo=NY)
    return a.timestamp(), (a + dt.timedelta(days=1)).timestamp()


def latest_day(path=None):
    c = _con(path)
    try:
        r = c.execute("SELECT MAX(t) FROM mentor_log").fetchone()
    finally:
        c.close()
    if not r or r[0] is None:
        return None
    return dt.datetime.fromtimestamp(r[0], NY).date().isoformat()


def load_day(day, scope="session", path=None):
    lo, hi = _ny_bounds(day)
    c = _con(path)
    try:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("SELECT * FROM mentor_log WHERE t >= ? AND t < ? ORDER BY t", (lo, hi))]
    finally:
        c.close()
    if scope == "session":       # 使用者交易時段:美東 09:30–13:30
        def ok(r):
            tt = dt.datetime.fromtimestamp(r["t"], NY).time()
            return dt.time(9, 30) <= tt <= dt.time(13, 30)
        rows = [r for r in rows if ok(r)]
    return rows


def daily(day=None, scope="session", bars_con=None, path=None):
    """某美東日的結算統計 + 時間線(不含敘事)。"""
    if bars_con is not None:
        settle(bars_con, path)
    day = day or latest_day(path)
    if not day:
        return dict(ok=False, error="還沒有任何導師紀錄(從現在開始,每則評論都會被存下來)")
    rows = load_day(day, scope, path)
    be = round(100.0 * (X_PTS + COST_PTS) / (2 * X_PTS), 1)
    th = [r for r in rows if r["kind"] == "thesis"]
    ai = [r for r in rows if r["kind"] == "ai"]
    sign_dir = lambda r: ("偏多" if (r["score_dir"] or 0) >= 0 else "偏空") if r["score_dir"] is not None else None
    stats = dict(
        thesis=_group(th, lambda r: r["direction"]),
        long=_group([r for r in th if r["direction"] == "偏多"], lambda r: r["direction"]),
        short=_group([r for r in th if r["direction"] == "偏空"], lambda r: r["direction"]),
        split_dir=_group([r for r in th if r["score_dir"] is not None], sign_dir),
        legacy_same_rows=_group([r for r in th if r["score_dir"] is not None], lambda r: r["direction"]),
        always_long=_group(th, lambda r: "偏多"),
        always_short=_group(th, lambda r: "偏空"),
        by_regime={},
        ai=_group(ai, lambda r: r["direction"]),
        ai_long=_group([r for r in ai if r["direction"] == "偏多"], lambda r: r["direction"]),
        ai_short=_group([r for r in ai if r["direction"] == "偏空"], lambda r: r["direction"]),
        ai_always_long=_group(ai, lambda r: "偏多"),
        ai_always_short=_group(ai, lambda r: "偏空"),
        breakeven=be,
    )
    regs = sorted({r["regime"] for r in th if r["regime"]})
    for g in regs:
        stats["by_regime"][g] = _group([r for r in th if r["regime"] == g], lambda r: r["direction"])
    pending = sum(1 for r in th if not r["settled"])
    timeline = []
    for r in rows:
        v = verdict(r["direction"], r["outcome"]) if r["kind"] in ("thesis", "ai") else "問答"
        extra = {}
        if r["kind"] == "ai":
            try:
                iv = (json.loads(r["inputs"]) if r["inputs"] else {}).get("verdict") or {}
            except Exception:
                iv = {}
            extra = dict(ai_levels=iv.get("key_levels") or [], invalid_price=iv.get("invalid_price"), one_line=iv.get("one_line"))
        txt = (r["text"] or "").strip()
        timeline.append(dict(
            id=r["id"], t=r["t"], tw=dt.datetime.fromtimestamp(r["t"], TW).strftime("%H:%M"),
            ny=dt.datetime.fromtimestamp(r["t"], NY).strftime("%H:%M"),
            kind=r["kind"], spot=r["spot"], direction=r["direction"], score=r["score"], score_dir=r["score_dir"],
            regime=r["regime"], strength=r["strength"], confidence=r["confidence"], question=r["question"],
            text=txt, verdict=v, outcome=r["outcome"], mfe=None if r["mfe"] is None else round(r["mfe"], 1),
            mae=None if r["mae"] is None else round(r["mae"], 1), settled=bool(r["settled"]), **extra))
    c = _con(path)
    try:
        s = c.execute("SELECT text, created, n_comments, ai FROM daily_summary WHERE day=?", (day,)).fetchone()
    finally:
        c.close()
    n_all = len(rows) if scope == "all" else len(load_day(day, "all", path))
    return dict(ok=True, day=day, scope=scope, n=len(rows), n_all=n_all, n_thesis=len(th), pending=pending, X=X_PTS, hold=HOLD_MIN,
                cost=COST_PTS, stats=stats, timeline=timeline,
                summary=dict(text=s[0], created=s[1], n_comments=s[2], ai=bool(s[3])) if s else None)


_OUT_WORDS = {"up": "先漲到 +40", "down": "先跌到 -40", "both": "同一分鐘內上下都碰到(順序不明)", "timeout": "30 分鐘內沒碰到任何一邊"}


def _facts(d, limit=60):
    tl = d["timeline"]
    if len(tl) > limit:        # 太多就等距抽樣,避免 prompt 過長
        step = len(tl) / float(limit)
        tl = [tl[int(i * step)] for i in range(limit)]
    lines = []
    for x in tl:
        head = f"#{x['id']} 台北{x['tw']} NQ {x['spot']:.0f}" if x["spot"] else f"#{x['id']} 台北{x['tw']}"
        if x["kind"] == "ask":
            lines.append(f"{head} [問導師] 問:{(x['question'] or '')[:60]};答要點:{x['text'][:110]}")
            continue
        if x["kind"] == "ai":
            res = ("→ " + _OUT_WORDS.get(x["outcome"], "") + f"(判定:{x['verdict']})") if x["outcome"] else "→ 尚未結算"
            lines.append(f"{head} [AI導師] {x['direction'] or '觀望'}(信心 {x['confidence'] if x['confidence'] is not None else '—'});{(x.get('one_line') or x['text'])[:110]} {res}")
            continue
        res = ("→ " + _OUT_WORDS.get(x["outcome"], "") + f"(判定:{x['verdict']}" + (f",最大順向 {x['mfe']:.0f}/逆向 {x['mae']:.0f} 點" if x["mfe"] is not None else "") + ")") if x["outcome"] else "→ 尚未結算"
        lines.append(f"{head} {x['direction']}({x['score']:+d}),環境:{x['regime'] or '—'};{x['text'][:110]} {res}")
    return lines


def rule_text(d):
    """沒有 AI 時的日結:純規則,照時間順序。"""
    s = d["stats"]["thesis"]
    L = [f"{d['day']}(美東){'盤中 09:30–13:30' if d['scope'] == 'session' else '全天'}:導師共存下 {d['n']} 則評論,其中自動結論 {d['n_thesis']} 則,已結算 {s['n']} 則。"]
    if s["decided"]:
        L.append(f"在 30 分鐘內分出勝負(先 +40 或先 -40)的有 {s['decided']} 則,方向命中 {s['hit']}、未命中 {s['miss']},命中率 {s['rate']}%;"
                 f"1:1 扣成本的損益兩平約 {d['stats']['breakeven']}%。逾時 {s['timeout']}、同棒 {s['both']}。")
        al, as_ = d["stats"]["always_long"], d["stats"]["always_short"]
        L.append(f"對照:同樣時間點永遠做多命中率 {al['rate']}%、永遠做空 {as_['rate']}%(扣除大盤當天漂移的影響要看這個)。")
    else:
        L.append("目前沒有分出勝負的結論(可能還在等 30 分鐘,或當天波動不夠)。")
    L.append("樣本提醒:相鄰結論的 30 分鐘窗口重疊,獨立樣本遠少於則數;單日結果只能當描述,不能當評分。")
    return "\n".join(L)


def narrative(d, summarize, force=False, path=None):
    """日結敘事:有快取就用快取;否則(summarize 可用時)一次 LLM 呼叫串成意識流,失敗則用規則文字。
    對錯、命中率都是程式算好的事實,LLM 只能照事實串,不能改數字。"""
    day = d["day"]
    if d.get("summary") and not force and (d["summary"]["ai"] or not summarize):
        return d["summary"]["text"], d["summary"]["ai"]
    text, ai = None, False
    if summarize and d["timeline"]:
        s = d["stats"]["thesis"]
        prompt = (
            "【任務】以下是你(導師)今天在盤中依序留下的評論,以及程式用 1 分 K 判定的結果(先碰 +40 或先碰 -40,30 分鐘為限)。"
            "請寫一篇「當日總結」:用第一人稱、意識流的口吻,依時間順序把一天的判斷串成一條有因果的思路——"
            "當時為什麼這樣看、後來市場怎麼走、哪裡看對、哪裡看錯、錯在哪個假設。\n"
            "【硬性規則】1) 每提到一則評論都要標註編號,例如 #12,讓讀者能對回原文;2) 數字與對錯只能用下面提供的,不可自行編造或改寫結果;"
            "3) 沒結算的就說還沒結算;4) 要誠實指出:逾時與同棒不是對錯、樣本重疊所以獨立樣本很少、單日不能證明什麼;"
            "5) 最後用兩三句說『明天要更注意什麼』,不要喊單;6) 純文字、不要 markdown,繁體中文。\n\n"
            f"【當日統計】{day} 自動結論已結算 {s['n']} 則,分出勝負 {s['decided']} 則,命中 {s['hit']}、未命中 {s['miss']}、逾時 {s['timeout']}、同棒 {s['both']},"
            f"命中率 {s['rate']}%(損益兩平約 {d['stats']['breakeven']}%);永遠做多基準 {d['stats']['always_long']['rate']}%、永遠做空基準 {d['stats']['always_short']['rate']}%。\n\n"
            "【逐則評論與結果】\n" + "\n".join(_facts(d)))
        try:
            text = summarize(prompt)
        except Exception:
            text = None
        ai = bool(text)
    if not text:
        text, ai = rule_text(d), False
    try:
        with _lock:
            c = _con(path)
            try:
                c.execute("INSERT OR REPLACE INTO daily_summary (day,text,created,n_comments,ai) VALUES (?,?,?,?,?)",
                          (day, text, time.time(), d["n"], 1 if ai else 0))
                c.commit()
            finally:
                c.close()
    except Exception:
        pass
    return text, ai
