"""basis_check.py --session / --detail 的合成資料測試:盤前比例漂移(NQ 動、NDX 沒動),盤中穩定。"""
import datetime as dt, os, sqlite3, subprocess, sys, tempfile
from zoneinfo import ZoneInfo
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import nq_gex_collector as C
d = tempfile.mkdtemp(); db = os.path.join(d, "x.sqlite")
con = C.init_db(db); NY = ZoneInfo("America/New_York")
day = dt.date(2026, 10, 28)
for hm in range(8 * 60, 16 * 60, 10):
    t = dt.datetime(day.year, day.month, day.day, hm // 60, hm % 60, tzinfo=NY).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    ndx = 29700.0
    nq = 29900.0 + (-120 if hm < 570 else 0) + (hm % 7)       # 盤前 NQ 低 120 點但 NDX 指數沒更新 → 比例被拉低
    con.execute("INSERT INTO snapshots (ts,spot) VALUES (?,?)", (t, nq))
    for k in range(5):
        con.execute("INSERT INTO contracts (ts,expiry,dte,cls,strike,cp,oi,iv,F,T,gamma,gex,stale,src,strike_nq,vol,gex_vol,mult,q) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t, "20261030", 0, "x", 29700 + k * 50, "C", 1, .2, ndx, .003, .001, 1.0, 0, "NDX", (29700 + k * 50) * nq / ndx, 1, 1.0, 100.0, .04))
con.commit(); con.close()
run = lambda *a: subprocess.run([sys.executable, os.path.join(HERE, "basis_check.py"), "--db", db, *a], capture_output=True, text=True)
full = run().stdout
ses = run("--session").stdout
det = run("--detail", "2026-10-28").stdout
import re
def drift(txt):
    m = re.search(r"2026-10-28\s+NDX\s+\d+\s+\S+~\S+\s+([\d.]+)", txt); return float(m.group(1))
assert drift(full) > 100 and drift(ses) < 15, (drift(full), drift(ses))
assert "08:00" in det and "09:30" in det and "-1" in det, det
print("full", drift(full), "session", drift(ses)); print(det); print("OK")
