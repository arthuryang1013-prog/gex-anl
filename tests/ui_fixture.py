"""UI 截圖用的假資料網站:合成資料庫(快照 + 合約 + 1 分 K)、訂單流 json、導師存檔,然後啟動真正的 gex_web_server。
不需要 IBKR / Bookmap / 金鑰。用法見 tests/ui_shots.py。"""
import datetime as dt, json, math, os, random, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build(d, of_mode="live"):
    import nq_gex_collector as C
    random.seed(3)
    db = os.path.join(d, "nq_gex.sqlite")
    con = C.init_db(db)
    now = int(time.time())
    t0 = now - 4 * 3600
    px = 29800.0
    con.execute("CREATE TABLE IF NOT EXISTS bars (t INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL)")
    bars = []
    for m in range(4 * 60 + 1):
        t = t0 + m * 60 - (t0 % 60)
        o = px; px += random.gauss(0, 5) + 0.3 * math.sin(m / 25.0); h = max(o, px) + random.random() * 3; l = min(o, px) - random.random() * 3
        bars.append((t, o, h, l, px, 100))
    con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?)", bars)
    spot_last = bars[-1][4]
    snap_ts = []
    for i, k in enumerate(range(0, 4 * 3600, 180)):
        t = t0 + k
        nq = [b for b in bars if b[0] <= t][-1][4]
        ts = dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds")
        snap_ts.append((ts, nq))
        con.execute("INSERT OR REPLACE INTO snapshots (ts,spot) VALUES (?,?)", (ts, nq))
        for src, step, base, mult in (("NQ", 25, nq, 20.0), ("QQQ", 1, nq / 48.5, 100.0), ("NDX", 50, nq - 280, 100.0)):
            for dte in (0, 1, 2):
                for kk in range(-24, 25):
                    kn = round(nq / 25) * 25 + kk * 25
                    stk = kn if src == "NQ" else round((kn - (nq - base) * (0 if src == "QQQ" else 1)) / (48.5 if src == "QQQ" else 1))
                    for cp in "CP":
                        # call 牆在 spot 上方約 +85,put 牆在下方約 -110,製造可讀的關卡
                        peak = 85 if cp == "C" else -110
                        w = math.exp(-((kn - nq - peak) / 90.0) ** 2) * (1.0 if dte == 0 else 0.6)
                        gex = (1 if cp == "C" else -1) * 4e6 * w * (1 + random.random() * .1) + (6e5 if (cp == "C") == (kk > 0) else -2e5) * (0.5 if dte else 1)
                        con.execute("INSERT INTO contracts (ts,expiry,dte,cls,strike,cp,oi,iv,F,T,gamma,gex,stale,src,strike_nq,vol,gex_vol,mult,q) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (ts, "20261030", dte, "x", stk, cp, 800, 0.2, nq, 0.003 + dte * 0.004, 0.001, gex, 0, src, kn, 40, gex * .2, mult, 0.04))
    con.commit(); con.close()
    # 導師存檔:幾則已結算的評論(讓復盤/標記有東西可看)
    import mentor_log as ML
    rows = [(150, "偏多", 34, -8, "正 gamma:壓波動"), (120, "偏空", -28, 12, "負 gamma:放大波動"), (90, "偏多", 22, 25, "正 gamma:壓波動"),
            (60, "偏多", 41, 33, "負 gamma:放大波動"), (30, "偏空", -19, -22, "正 gamma:壓波動")]
    for mins, dr, sc, sd, rg in rows:
        t = now - mins * 60
        spot = [b for b in bars if b[0] <= t][-1][4]
        ML.log_comment("thesis", t, spot, f"{dr}:示範評論,現價 {spot:.0f}。(假資料)", direction=dr, score=sc, score_dir=sd, regime=rg,
                       regime_s=0.4 if "正" in rg else -0.4, strength="中", confidence=62, path=os.path.join(d, "mentor_log.sqlite"))
    # 訂單流
    of = os.path.join(d, "orderflow.json")
    # 歷史:模擬 bridge 的「近 60 秒滾動累計」(每秒一筆),delta_tr 與 delta 大致同向但不相同
    hist_path = os.path.join(d, "orderflow_history.jsonl")
    rnd = random.Random(7)
    win, cvd_tr, cvd_mbo, recs = [], 0.0, 0.0, []
    for sec in range(90 * 60, 0, -1):
        t = now - sec
        trend = math.sin(sec / 400.0)
        b, sl = max(0.0, rnd.gauss(4 + 2 * trend, 2)), max(0.0, rnd.gauss(4 - 2 * trend, 2))
        mb = b * 0.8 + rnd.gauss(0, 0.5)
        ms_ = sl * 0.8 + rnd.gauss(0, 0.5)
        win.append((t, b, sl, mb, ms_))
        while win and win[0][0] <= t - 60:
            win.pop(0)
        cvd_tr += b - sl; cvd_mbo += mb - ms_
        sb = sum(x[1] for x in win); ss = sum(x[2] for x in win)
        recs.append(dict(ts=t, received_at=t, idle_sec=0.3, lag_sec=0.3, delta=round(sum(x[3] - x[4] for x in win), 1),
                         vol=round(sum(x[3] + x[4] for x in win), 1), delta_tr=round(sb - ss, 1), vol_tr=round(sb + ss, 1),
                         cvd_tr=round(cvd_tr, 1), cvd_mbo=round(cvd_mbo, 1), imb=round(0.2 * trend + rnd.gauss(0, .05), 3),
                         imb_big=round(-0.1 * trend, 3), big=round(rnd.gauss(0, 3), 1), big_vol=30.0, sweep_buy=int(rnd.random() < .1),
                         sweep_sell=int(rnd.random() < .1), pull_bid=round(rnd.random() * 30, 1), pull_ask=round(rnd.random() * 30, 1),
                         stack_bid=round(rnd.random() * 40, 1), stack_ask=round(rnd.random() * 40, 1), spot=spot_last))
    with open(hist_path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    last = recs[-1]
    book_bid = [[round(spot_last - 0.25 * k, 2), int(30 + 200 * math.exp(-((k - 40) / 6.0) ** 2) + 120 * math.exp(-((k - 14) / 3.0) ** 2))] for k in range(1, 400, 4)]
    book_ask = [[round(spot_last + 0.25 * k, 2), int(30 + 260 * math.exp(-((k - 55) / 6.0) ** 2) + 90 * math.exp(-((k - 22) / 3.0) ** 2))] for k in range(1, 400, 4)]
    pkt = dict(last, ts=now, received_at=now, book_bid=book_bid, book_ask=book_ask,
               trades=[[round(spot_last + 1.0, 2), 5.0, 40.0], [round(spot_last - 2.0, 2), 30.0, 3.0]])
    if of_mode == "none":
        pass
    else:
        json.dump(pkt, open(of, "w"))
    return db, of, pkt


def serve(d, port, of_mode="live"):
    """在背景執行緒啟動網站;of_mode: live(每秒更新)/ stale(送了一次就停)/ none(從沒收過)。"""
    db, of, pkt = build(d, of_mode)
    import gex_web_server as G
    import mentor_log as ML
    G.CACHE_PATH = os.path.join(d, "web_cache.sqlite")
    G.ORDERFLOW_PATH = of
    G.ORDERFLOW_HISTORY_PATH = os.path.join(d, "orderflow_history.jsonl")
    ML.LOG_PATH = os.path.join(d, "mentor_log.sqlite")
    G.load_config = lambda: {"orderflow_max_age_sec": 8}
    if of_mode == "stale":
        pkt["received_at"] = time.time() - 125; json.dump(pkt, open(of, "w"))

    def feed():
        while True:
            pkt["received_at"] = time.time(); pkt["ts"] = time.time()
            json.dump(pkt, open(of, "w")); time.sleep(1)
    if of_mode == "live":
        threading.Thread(target=feed, daemon=True).start()
    sys.argv = ["gex_web_server.py", "--port", str(port), "--db", db, "--no-bars", "--no-open"]
    threading.Thread(target=G.main, daemon=True).start()
    for _ in range(50):
        try:
            import urllib.request; urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1); break
        except Exception:
            time.sleep(0.3)
    return db
