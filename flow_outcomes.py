#!/usr/bin/env python3
"""flow_outcomes.py — 「訂單流事件」與「碰牆」的事後成績單。

每個事件(掃單/停損/冰山/吸收/力竭)發生當下,記下 GEX 背景(最近的牆/flip 是哪一條、離多遠);
每次價格碰到 GEX 牆或 flip,記下當時的訂單流;等 30 分鐘過去,用 1 分 K 算「之後發生了什麼」:
  - 最大有利 / 不利波動(點)、5 / 30 分鐘
  - ±40 點誰先到(跟你的交易方式一致:1:1)
所有想法(事件、碰牆)都用這份結果判定好不好,不靠直覺。

用法(VPS 專案資料夾):
  python3 flow_outcomes.py --loop        # 常駐,每 60 秒記錄 + 補結果(建議做成 systemd 服務,見 CLAUDE.md)
  python3 flow_outcomes.py --once        # 跑一次(測試或補資料)
  python3 flow_outcomes.py --report      # 白話成績單(預設只看美東 09:30–13:30;--all-hours 看全部)

資料寫進 flow_outcomes.sqlite(已 gitignore)。限制:
  - 結果用 1 分 K,事件發生那根 K 棒的後續不算(從下一根開始),所以第一分鐘內的走勢看不到。
  - 同一根 K 棒同時碰到 +40 和 -40 標成 both(先後不明),不算勝負。
  - 訂單流歷史檔只留 2 小時,碰牆的流量背景必須在 2 小時內記錄(常駐就沒問題)。
  - 樣本少時誤差很大;事件有重疊的 30 分鐘窗口,獨立樣本比筆數少。沒有連續幾週、足夠樣本前,不要調門檻或宣稱有效。
"""
import argparse, json, math, os, sqlite3, sys, time
import datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
NY = ZoneInfo("America/New_York")
OUT_DB = os.path.join(HERE, "flow_outcomes.sqlite")
X_PTS = 40.0                  # 跟交易方式一致:1:1、40 點
HOLD_MIN = 30                 # 結果觀察多久
TOUCH_TOL = 2.0               # 碰牆:K 棒範圍離牆 2 點內
TOUCH_DEBOUNCE = 15 * 60      # 同一條牆 15 分鐘內不重複記
EVENT_KINDS = ("sweep", "stop", "absorb", "iceberg", "exhaust")
LEVEL_SCOPES = ("all", "zero")            # 全部到期日 / 只看當天到期(0DTE)
LEVEL_SRCS = ("NQ", "QQQ", "NDX")


# ----------------------------- 資料庫 -----------------------------
def init_db(path=OUT_DB):
    c = sqlite3.connect(path, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS ev (
        id TEXT PRIMARY KEY, t REAL, kind TEXT, side TEXT, price REAL, vol REAL, conf INTEGER, x_json TEXT,
        spot REAL, lv_json TEXT, near_name TEXT, near_dist REAL, bias INTEGER,
        mfe5 REAL, mae5 REAL, mfe30 REAL, mae30 REAL, first40 TEXT, filled INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS touch (
        id TEXT PRIMARY KEY, t REAL, level_name TEXT, level REAL, approach TEXT, spot REAL,
        flow_json TEXT, bounce INTEGER,
        mfe5 REAL, mae5 REAL, mfe30 REAL, mae30 REAL, first40 TEXT, filled INTEGER DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS ev_filled ON ev(filled, t)")
    c.execute("CREATE INDEX IF NOT EXISTS tc_filled ON touch(filled, t)")
    c.commit()
    return c


def bias_of(kind, side):
    """事件「暗示」的方向:+1 偏多、-1 偏空。sweep/stop 看主動方(順勢);absorb/iceberg 看承接方;exhaust 是反向(買方力竭 → 偏空)。"""
    b = 1 if side == "buy" else -1
    return -b if kind == "exhaust" else b


# ----------------------------- GEX 背景 -----------------------------
def flat_levels(lv):
    """lv = {"NQ|all": {call, put, flip, call_a, put_b}, ...} → {"NQ|all|call": 價, ...}(去掉 None)"""
    out = {}
    for k, d in (lv or {}).items():
        for n, v in (d or {}).items():
            if v is not None:
                out[f"{k}|{n}"] = float(v)
    return out


def nearest_level(price, flat):
    best = None
    for n, v in flat.items():
        d = abs(v - price)
        if best is None or d < best[1]:
            best = (n, d, v)
    return best


def make_levels_provider():
    """預設的 GEX 牆來源:用網站程式既有的 levels_for_ts(有磁碟快取)。回傳 f(epoch)→(snapshot_epoch, spot, lv)。"""
    import gex_web_server as G
    con = G.connect(G.DB_PATH)

    def f(t):
        r = con.execute("SELECT ts, spot FROM snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (G.iso(int(t)),)).fetchone()
        if not r:
            return None
        ep = G.epoch_of(r[0])
        if t - ep > 2 * 3600:                    # 最近的快照超過 2 小時前,背景不可靠
            return None
        cache = G._cache_con()
        try:
            lv = {}
            for sc in LEVEL_SCOPES:
                got = G.levels_for_ts(con, cache, r[0], list(LEVEL_SRCS), "oi", sc)
                for src, v in got.items():
                    lv[f"{src}|{sc}"] = dict(zip(("call", "put", "flip", "call_a", "put_b"), v))
        finally:
            cache.close()
        return ep, (float(r[1]) if r[1] is not None else None), lv
    return f


# ----------------------------- 事件 -----------------------------
def read_events(path, since):
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8_000_000))
            for ln in f.read().decode("utf-8", "ignore").splitlines():
                try:
                    e = json.loads(ln)
                except Exception:
                    continue
                if e.get("k") in EVENT_KINDS and isinstance(e.get("t"), (int, float)) and e["t"] >= since:
                    out.append(e)
    except FileNotFoundError:
        pass
    return out


def ingest_events(c, events_path, levels_fn, now=None):
    now = now or time.time()
    last = c.execute("SELECT MAX(t) FROM ev").fetchone()[0] or 0
    n = 0
    for e in read_events(events_path, max(last - 300, now - 3 * 86400)):
        if c.execute("SELECT 1 FROM ev WHERE id=?", (e["id"],)).fetchone():
            continue
        lv = None
        try:
            lv = levels_fn(e["t"])
        except Exception:
            lv = None
        spot, flat, near = None, {}, None
        lvj = None
        if lv:
            _ep, spot, lvd = lv
            flat = flat_levels(lvd)
            near = nearest_level(e["p"], flat)
            lvj = json.dumps(lvd)
        x = e.get("x") or {}
        c.execute("INSERT OR IGNORE INTO ev (id,t,kind,side,price,vol,conf,x_json,spot,lv_json,near_name,near_dist,bias) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (e["id"], e["t"], e["k"], e["s"], e["p"], e.get("v"), x.get("conf"), json.dumps(x), spot, lvj,
                  near[0] if near else None, near[1] if near else None, bias_of(e["k"], e["s"])))
        n += 1
    c.commit()
    return n


# ----------------------------- 結果(用 1 分 K) -----------------------------
def load_bars(bars_db, lo, hi):
    c = sqlite3.connect(bars_db, timeout=10)
    try:
        return c.execute("SELECT t,o,h,l,c FROM bars WHERE t>=? AND t<=? ORDER BY t", (int(lo), int(hi))).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def outcome(bars, t0, p0, hold_min=HOLD_MIN, x=X_PTS):
    """從事件下一根完整 1 分 K 開始往後看。回傳 dict 或 None(資料還沒走完 / 缺 K 棒)。
    mfe = 最大上漲(點,往上為正)、mae = 最大下跌(點,正數);first40 = 'up' / 'dn' / 'both' / 'none'。"""
    start = (int(t0) // 60 + 1) * 60
    end = start + hold_min * 60
    seq = [b for b in bars if start <= b[0] < end]
    if len(seq) < hold_min * 0.8 or (bars and bars[-1][0] < end - 60):      # 還沒走完或缺太多
        return None
    res = dict(mfe5=None, mae5=None, mfe30=None, mae30=None, first40="none")
    hi = lo = None
    done = False
    for b in seq:
        hi = b[2] if hi is None else max(hi, b[2])
        lo = b[3] if lo is None else min(lo, b[3])
        if b[0] < start + 5 * 60:
            res["mfe5"], res["mae5"] = max(0.0, hi - p0), max(0.0, p0 - lo)
        if not done:
            up, dn = b[2] - p0 >= x, p0 - b[3] >= x
            if up and dn:
                res["first40"], done = "both", True
            elif up:
                res["first40"], done = "up", True
            elif dn:
                res["first40"], done = "dn", True
    res["mfe30"], res["mae30"] = max(0.0, hi - p0), max(0.0, p0 - lo)
    return res


def fill_outcomes(c, bars_db, now=None):
    now = now or time.time()
    n = 0
    for table, ts_col, pcol in (("ev", "t", "price"), ("touch", "t", "level")):
        rows = c.execute(f"SELECT id, t, {pcol} FROM {table} WHERE filled=0 AND t <= ?", (now - (HOLD_MIN + 2) * 60,)).fetchall()
        if not rows:
            continue
        bars = load_bars(bars_db, min(r[1] for r in rows), max(r[1] for r in rows) + (HOLD_MIN + 3) * 60)
        for rid, t0, p0 in rows:
            if now - t0 > 3 * 86400 and not bars:
                c.execute(f"UPDATE {table} SET filled=-1 WHERE id=?", (rid,))      # 資料太舊且沒有 K 棒:放棄
                continue
            o = outcome(bars, t0, p0)
            if o:
                c.execute(f"UPDATE {table} SET mfe5=?, mae5=?, mfe30=?, mae30=?, first40=?, filled=1 WHERE id=?",
                          (o["mfe5"], o["mae5"], o["mfe30"], o["mae30"], o["first40"], rid))
                n += 1
    c.commit()
    return n


# ----------------------------- 碰牆 -----------------------------
def flow_context(hist_path, events, t, rows_fn=None):
    """碰牆那一分鐘的訂單流:Delta(實際成交)、成交量、前後 90 秒內的事件統計。"""
    ctx = {}
    try:
        import orderflow_minutes as OM
        recs = []
        with open(hist_path, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if t - 180 <= (r.get("received_at") or 0) <= t + 120:
                    recs.append(r)
        mr = OM.minute_rows(recs)
        m = [r for r in mr if r["t"] == int(t) // 60 * 60]
        if m:
            ctx.update(delta_tr=m[0].get("delta_tr"), vol_tr=m[0].get("vol_tr"), imb=m[0].get("imb"))
    except Exception:
        pass
    cnt = {}
    for e in events:
        if t - 90 <= e["t"] <= t + 90:
            k = f"{e['k']}:{e['s']}"
            cnt[k] = cnt.get(k, 0) + 1
    ctx["events"] = cnt
    return ctx


def ingest_touches(c, bars_db, levels_fn, hist_path, events_path, now=None, lookback_h=3):
    now = now or time.time()
    bars = load_bars(bars_db, now - lookback_h * 3600, now)
    evs = read_events(events_path, now - lookback_h * 3600 - 300)
    n = 0
    for i in range(1, len(bars)):
        b, pb = bars[i], bars[i - 1]
        if b[0] + 60 > now:                     # 還沒收完的 K 棒不算
            continue
        lv = None
        try:
            lv = levels_fn(b[0])
        except Exception:
            lv = None
        if not lv:
            continue
        _ep, spot, lvd = lv
        for name, L in flat_levels(lvd).items():
            if not (b[3] - TOUCH_TOL <= L <= b[2] + TOUCH_TOL):
                continue
            if abs(pb[4] - L) <= TOUCH_TOL:      # 上一根收盤就已經貼著牆:不是新的碰觸
                continue
            appr = "below" if pb[4] < L else "above"
            last = c.execute("SELECT MAX(t) FROM touch WHERE level_name=? AND ABS(level-?)<=?", (name, L, TOUCH_TOL)).fetchone()[0]
            if last and b[0] - last < TOUCH_DEBOUNCE:
                continue
            tid = f"{name}-{round(L)}-{b[0]}"
            flow = flow_context(hist_path, evs, b[0] + 30)
            c.execute("INSERT OR IGNORE INTO touch (id,t,level_name,level,approach,spot,flow_json,bounce) VALUES (?,?,?,?,?,?,?,?)",
                      (tid, float(b[0]), name, L, appr, spot, json.dumps(flow), -1 if appr == "below" else 1))
            n += 1
    c.commit()
    return n


# ----------------------------- 報表 -----------------------------
def wilson(w, n, z=1.96):
    if n <= 0:
        return (0.0, 1.0)
    p = w / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, cen - h), min(1.0, cen + h))


def in_session(t):
    d = dt.datetime.fromtimestamp(t, NY)
    return d.weekday() < 5 and dt.time(9, 30) <= d.time() < dt.time(13, 30)


def _dist_bucket(d):
    if d is None:
        return "沒有 GEX 背景"
    return "貼著牆(3 點內)" if d <= 3 else "靠近牆(3–10 點)" if d <= 10 else "離牆遠(10 點以上)"


def _line(label, w, l, none, both, mfe, mae):
    n = w + l
    if n == 0:
        return f"  {label}:還沒有分出勝負的樣本(沒走到 ±40:{none},同根都碰到:{both})"
    lo, hi = wilson(w, n)
    return (f"  {label}:先賺 40 點 {w} 次、先賠 40 點 {l} 次 → 先賺的比例 {100 * w / n:.0f}%(誤差約 {100 * lo:.0f}–{100 * hi:.0f}%)"
            f";30 分鐘內沒到 ±40 的有 {none} 次" + (f";平均最有利 {mfe:.0f} 點、最不利 {mae:.0f} 點" if mfe is not None else ""))


def report(c, all_hours=False):
    L = []
    rows = c.execute("SELECT kind, side, conf, near_dist, bias, first40, mfe30, mae30, t FROM ev WHERE filled=1").fetchall()
    if not all_hours:
        rows = [r for r in rows if in_session(r[8])]
    L.append(f"【訂單流事件的事後成績】共 {len(rows)} 筆已走完 30 分鐘的事件" + ("" if all_hours else "(只看美東 09:30–13:30)"))
    L.append("讀法:事件各自「暗示」一個方向(掃單/停損=順著主動方;吸收/冰山=承接方;力竭=反向)。")
    L.append("「先賺 40 點」= 30 分鐘內價格先往暗示方向走 40 點;「先賠 40 點」= 先往反方向走 40 點。1:1 的話,先賺的比例要明顯高過 50%(還要扣成本)才有用。")
    if not rows:
        L.append("  (還沒有資料。事件要先累積,且要等 30 分鐘後才有結果。)")
    kinds = {}
    for r in rows:
        kinds.setdefault(r[0], []).append(r)
    for k in EVENT_KINDS:
        rs = kinds.get(k)
        if not rs:
            continue
        L.append(f"\n▶ {k}:{len(rs)} 筆")
        groups = [("全部", rs)] + [(b, [r for r in rs if _dist_bucket(r[3]) == b]) for b in ("貼著牆(3 點內)", "靠近牆(3–10 點)", "離牆遠(10 點以上)", "沒有 GEX 背景")]
        if k in ("iceberg", "absorb"):
            groups += [("可信度 2(有證據)", [r for r in rs if r[2] == 2])]
        for lab, g in groups:
            if not g:
                continue
            w = sum(1 for r in g if (r[5] == "up" and r[4] > 0) or (r[5] == "dn" and r[4] < 0))
            l = sum(1 for r in g if (r[5] == "dn" and r[4] > 0) or (r[5] == "up" and r[4] < 0))
            none = sum(1 for r in g if r[5] == "none")
            both = sum(1 for r in g if r[5] == "both")
            mfe = [(r[6] if r[4] > 0 else r[7]) for r in g if r[6] is not None]
            mae = [(r[7] if r[4] > 0 else r[6]) for r in g if r[6] is not None]
            L.append(_line(lab, w, l, none, both, sum(mfe) / len(mfe) if mfe else None, sum(mae) / len(mae) if mae else None))
    rows = c.execute("SELECT level_name, approach, bounce, first40, t FROM touch WHERE filled=1").fetchall()
    if not all_hours:
        rows = [r for r in rows if in_session(r[4])]
    L.append(f"\n【碰牆的事後成績】共 {len(rows)} 次已走完 30 分鐘的碰觸")
    L.append("「彈回」= 碰到牆後,價格先往來的方向反彈 40 點;「穿過」= 先往穿過牆的方向走 40 點。")
    by = {}
    for r in rows:
        grp = r[0].split("|")[2] if r[0].count("|") >= 2 else r[0]
        by.setdefault(("全部牆", ""), []).append(r)
        by.setdefault((grp, ""), []).append(r)
    for (lab, _), g in sorted(by.items()):
        w = sum(1 for r in g if (r[3] == "up" and r[2] > 0) or (r[3] == "dn" and r[2] < 0))
        l = sum(1 for r in g if (r[3] == "dn" and r[2] > 0) or (r[3] == "up" and r[2] < 0))
        none = sum(1 for r in g if r[3] == "none")
        n = w + l
        if n == 0:
            L.append(f"  {lab}:{len(g)} 次,還沒有分出勝負的")
            continue
        lo, hi = wilson(w, n)
        L.append(f"  {lab}:彈回 {w} 次、穿過 {l} 次 → 彈回比例 {100 * w / n:.0f}%(誤差約 {100 * lo:.0f}–{100 * hi:.0f}%);30 分鐘沒到 ±40 的 {none} 次")
    L.append("\n注意:樣本少時誤差很大;相鄰事件的 30 分鐘窗口重疊,獨立樣本比筆數少;同時看很多組,運氣好的幾組會看起來有效。")
    return "\n".join(L)


# ----------------------------- 主程式 -----------------------------
def run_once(c, levels_fn, bars_db, events_path, hist_path, now=None):
    a = ingest_events(c, events_path, levels_fn, now)
    b = ingest_touches(c, bars_db, levels_fn, hist_path, events_path, now)
    d = fill_outcomes(c, bars_db, now)
    return a, b, d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--all-hours", action="store_true")
    ap.add_argument("--db", default=os.path.join(HERE, "nq_gex.sqlite"))
    ap.add_argument("--events", default=os.path.join(HERE, "orderflow_events.jsonl"))
    ap.add_argument("--hist", default=os.path.join(HERE, "orderflow_history.jsonl"))
    ap.add_argument("--out", default=OUT_DB)
    a = ap.parse_args()
    c = init_db(a.out)
    if a.report:
        print(report(c, a.all_hours))
        return
    levels_fn = make_levels_provider()
    if a.once or not a.loop:
        print("新事件 %d、新碰牆 %d、補上結果 %d" % run_once(c, levels_fn, a.db, a.events, a.hist))
        return
    while True:
        try:
            run_once(c, levels_fn, a.db, a.events, a.hist)
        except Exception as e:
            print("flow_outcomes 錯誤:", e, file=sys.stderr, flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
