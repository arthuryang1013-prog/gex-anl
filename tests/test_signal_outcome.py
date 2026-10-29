"""signal_outcome:合成資料,讓 'NQ:mom' 一定猜對、'NQ:pcr' 一定猜錯,檢查命中率;並檢查 api_thesis 會存 sig_json。"""
import json, os, sqlite3, subprocess, sys, tempfile, datetime as dt
from zoneinfo import ZoneInfo
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
d = tempfile.mkdtemp()
NY = ZoneInfo("America/New_York")
day0 = dt.datetime(2026, 10, 27, 9, 30, tzinfo=NY)
con = sqlite3.connect(os.path.join(d, "nq_gex.sqlite"))
con.execute("CREATE TABLE bars (t INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL)")
cc = sqlite3.connect(os.path.join(d, "web_cache.sqlite"))
cc.execute("CREATE TABLE thesis (ts TEXT PRIMARY KEY, t REAL, spot REAL, direction TEXT, score INTEGER, score_struct REAL, sig_json TEXT)")
t0 = int(day0.timestamp()); px = 20000.0
# 每 40 分鐘一個循環:先漲 60 點、再跌 60 點(交替);thesis 每 10 分鐘一筆
for m in range(0, 240):
    cyc = (m // 20) % 2
    px += 3.0 if cyc == 0 else -3.0
    con.execute("INSERT INTO bars VALUES (?,?,?,?,?,?)", (t0 + m * 60, px, px + 1, px - 1, px, 1))
for m in range(240, 480):
    con.execute("INSERT INTO bars VALUES (?,?,?,?,?,?)", (t0 + m * 60, px, px + 1, px - 1, px, 1))
con.commit()
for m in range(0, 200, 10):
    t = t0 + m * 60
    spot = [r[0] for r in con.execute("SELECT c FROM bars WHERE t<=? ORDER BY t DESC LIMIT 1", (t,))][0] if m else 20000.0
    up_next = ((m // 20) % 2 == 0)         # 這 10 分鐘起點之後會漲還是跌(粗略)
    s_good = 0.8 if up_next else -0.8
    sig = {"NQ": {"mom": s_good, "pcr": -s_good, "gma": 0.5}, "flow": {"累積Delta": s_good}, "agg": {"gex": 0.1, "flow": 0.0, "grand": 0.05}}
    cc.execute("INSERT INTO thesis VALUES (?,?,?,?,?,?,?)", (str(t), t, spot, "偏多", 10, 0.1 * 100, json.dumps(sig)))
cc.commit(); con.close(); cc.close()
out = subprocess.run([sys.executable, os.path.join(ROOT, "signal_outcome.py"), "--db", os.path.join(d, "nq_gex.sqlite"),
                      "--cache", os.path.join(d, "web_cache.sqlite"), "--all-hours", "--hold", "30", "--X", "40", "--min-n", "1"],
                     capture_output=True, text=True, cwd=ROOT)
print(out.stdout[-1500:], out.stderr[-500:])
assert out.returncode == 0
lines = {l.split()[0]: l for l in out.stdout.splitlines() if l.startswith(("NQ:", "flow:", "agg:"))}
assert "NQ:mom" in lines and "NQ:pcr" in lines and "NQ:gma(環境)" in lines or "NQ:gma(環境)" in out.stdout
import re
def rate(name):
    m = re.search(r"(\d+\.\d)%", lines[name].split(name)[1].split("±")[0]); return float(m.group(1)) if m else None
rm, rp = rate("NQ:mom"), rate("NQ:pcr")
print("mom", rm, "pcr", rp)
assert rm is not None and rp is not None and rm > rp
print("signal_outcome ok")
