"""surge 大單的「價內外分類」與「訂單流上下文」邏輯測試(合成資料,只證明邏輯,不證明真資料表現)。

測三件事:
1. _money_bucket:call/put 的價內外分類與價外點數門檻(價內 ≤ -15 / 價平 ±15 / 價外 < 100 / 深度價外 ≥ 100)。
2. _surge_frame 的 JOIN 有沒有把 snapshots.spot 帶進來(當作 nq_spot)。
3. api_events 端到端:合成兩張快照製造一次 surge,確認事件帶 money / m / flow,且 flow 的 delta_tr 加總、附近事件數正確。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import gex_web_server as G


def test_money_bucket():
    # call:價外 = strike > spot;put 相反。回 (bucket, 價外點數 m)
    assert G._money_bucket(28300, 28400, "C") == ("價內", -100)
    assert G._money_bucket(28400, 28400, "C") == ("價平", 0)
    assert G._money_bucket(28450, 28400, "C") == ("價外", 50)
    assert G._money_bucket(28550, 28400, "C") == ("深度價外", 150)
    # put 鏡像
    assert G._money_bucket(28300, 28400, "P") == ("深度價外", 100)
    assert G._money_bucket(28400, 28400, "P") == ("價平", 0)
    assert G._money_bucket(28450, 28400, "P") == ("價內", -50)
    # 缺 spot → None
    assert G._money_bucket(28400, None, "C") == (None, None)
    # 邊界:±15、±100(價平 = -15 < m < 15;價外 = 15 ≤ m < 100)
    assert G._money_bucket(28414, 28400, "C") == ("價平", 14)
    assert G._money_bucket(28415, 28400, "C") == ("價外", 15)
    assert G._money_bucket(28500, 28400, "C") == ("深度價外", 100)


def _mk_db(path):
    con = G.connect(path)
    con.executescript("""
      CREATE TABLE snapshots (ts TEXT PRIMARY KEY, spot REAL);
      CREATE TABLE contracts (ts TEXT, expiry TEXT, dte INT, cls TEXT, strike REAL, cp TEXT, oi REAL, iv REAL,
        F REAL, T REAL, gamma REAL, gex REAL, stale INT, src TEXT, strike_nq REAL, vol REAL, gex_vol REAL, mult REAL, q REAL);
    """)
    return con


def _insert_contracts(con, ts, vols):
    for i, v in enumerate(vols):
        strike = 28200 + i * 25
        con.execute("INSERT INTO contracts (ts,expiry,dte,cls,strike,cp,oi,iv,F,T,gamma,gex,stale,src,strike_nq,vol,gex_vol,mult,q) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, "20261106", 1, "x", strike, "C", 800, 0.2, 28400, 0.004, 0.001, 0.0, 0, "NQ", strike, v, 0.0, 20.0, 0.04))


def test_surge_frame_join():
    d = tempfile.mkdtemp()
    con = _mk_db(os.path.join(d, "t.sqlite"))
    con.execute("INSERT INTO snapshots (ts,spot) VALUES (?,?)", (G.iso(1750000200), 28400.0))
    _insert_contracts(con, G.iso(1750000200), [10.0] * 3)
    con.commit()
    df = G._surge_frame(con, G.iso(1750000200))
    assert "nq_spot" in df.columns
    assert float(df["nq_spot"].iloc[0]) == 28400.0
    con.close()


def test_api_events_money_flow():
    t0 = 1750000200            # 300 的整數倍
    t1 = t0 + 300
    spot = 28400.0
    d = tempfile.mkdtemp()
    con = _mk_db(os.path.join(d, "t.sqlite"))
    con.execute("INSERT INTO snapshots (ts,spot) VALUES (?,?)", (G.iso(t0), spot))
    con.execute("INSERT INTO snapshots (ts,spot) VALUES (?,?)", (G.iso(t1), spot))
    n = 21
    # 快照 1:全部 vol=10;快照 2:20 檔 dvol=5、1 檔(最遠價外)dvol=50 → 觸發 surge
    _insert_contracts(con, G.iso(t0), [10.0] * n)
    vols2 = [15.0] * (n - 1) + [60.0]
    _insert_contracts(con, G.iso(t1), vols2)
    con.commit()

    # 訂單流:大單前 5 分鐘每分鐘 delta_tr=+10、之後 5 分鐘每分鐘 -5;附近一個掃單
    hist = os.path.join(d, "orderflow_history.jsonl")
    with open(hist, "w") as f:
        for k, dv in [(t1 - 300, 10.0), (t1 - 240, 10.0), (t1 - 180, 10.0), (t1 - 120, 10.0), (t1 - 60, 10.0),
                      (t1, -5.0), (t1 + 60, -5.0), (t1 + 120, -5.0), (t1 + 180, -5.0), (t1 + 240, -5.0)]:
            f.write(json.dumps(dict(received_at=k, delta_tr=dv, vol_tr=20.0)) + "\n")
    evf = os.path.join(d, "orderflow_events.jsonl")
    with open(evf, "w") as f:
        f.write(json.dumps(dict(id="s1", t=t1 + 60, k="sweep", s="buy", p=28400.0, v=50)) + "\n")

    # 指向臨時檔,測完還原
    saved = (G.ORDERFLOW_HISTORY_PATH, G.ORDERFLOW_EVENTS_PATH, G.ORDERFLOW_MINUTES_DIR, G.ORDERFLOW_EVENTS_ARCHIVE_DIR)
    G.ORDERFLOW_HISTORY_PATH = hist
    G.ORDERFLOW_EVENTS_PATH = evf
    G.ORDERFLOW_MINUTES_DIR = os.path.join(d, "empty_min")
    G.ORDERFLOW_EVENTS_ARCHIVE_DIR = os.path.join(d, "empty_ev")
    try:
        # 多來源(NQ,QQQ)是刻意要抓一個回歸:內層把變數 m 從 DataFrame 蓋成 int,下一個來源會 TypeError。
        # 所以這裡傳 "NQ,QQQ",舊程式會炸,修好後應該通過。
        events = G.api_events(con, {"from": t0, "to": t1 + 60, "src": "NQ,QQQ"}).get("events", [])
        assert events, "應該至少偵測到一次 surge"
        e = events[0]
        assert e["money"] == "深度價外"
        assert e["m"] == 300
        assert e["flow"] is not None
        assert e["flow"]["dur"] == 50 and e["flow"]["dur_n"] == 5      # 大單前 5 分鐘 +10×5
        assert e["flow"]["aft"] == -25 and e["flow"]["aft_n"] == 5     # 之後 5 分鐘 -5×5
        assert e["flow"]["ev"] == {"sweep": 1}
    finally:
        (G.ORDERFLOW_HISTORY_PATH, G.ORDERFLOW_EVENTS_PATH, G.ORDERFLOW_MINUTES_DIR, G.ORDERFLOW_EVENTS_ARCHIVE_DIR) = saved
    con.close()


if __name__ == "__main__":
    test_money_bucket()
    test_surge_frame_join()
    test_api_events_money_flow()
    print("OK")
