import sys, os, math, random, datetime as dt, sqlite3, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nq_gex_collector as C
import gex_web_server as G
d = tempfile.mkdtemp(); db = d + "/nq_gex.sqlite"
con = C.init_db(db)
random.seed(2)
t0 = dt.datetime(2026, 10, 22, 14, 0, tzinfo=dt.timezone.utc)
nq = 25000.0
for i in range(60):
    ts = (t0 + dt.timedelta(minutes=3 * i)).isoformat(timespec="seconds"); nq += random.gauss(0, 6)
    con.execute("INSERT INTO snapshots (ts,spot) VALUES (?,?)", (ts, nq))
    for src, step, sc, mult in (("NQ", 25, 1.0, 20.0), ("QQQ", 1, nq / 610.0, 100.0), ("NDX", 50, nq / 25300.0, 20.0)):
        for dte in (0, 1):
            for k in range(-20, 21):
                kn = round(nq / step) * step + k * step
                stk = kn / (nq / (nq / sc if src != "NQ" else nq)) if False else (kn if src == "NQ" else kn * (1 / (nq / (nq if src == "NQ" else (610.0 if src == "QQQ" else 25300.0)))))
                for cp in "CP":
                    gex = (1 if cp == "C" else -1) * 1e6 * math.exp(-abs(k) / 6) * (1 + random.random() * .1)
                    con.execute("INSERT INTO contracts (ts,expiry,dte,cls,strike,cp,oi,iv,F,T,gamma,gex,stale,src,strike_nq,vol,gex_vol,mult,q) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (ts, "20261023", dte, "x", stk, cp, 1000, 0.2, nq, 0.003, 0.001, gex, 0, src, kn, 50, gex * .1, mult, 0.04))
con.commit(); con.close()
G.DB_PATH = db
print([n for n in dir(G) if "DB" in n.upper() and n.isupper()])
G.CACHE_PATH = d + "/web_cache.sqlite"
G.load_config = lambda: {"auto_ai_summary": False}
G.ORDERFLOW_PATH = d + "/none.json"
con = G.connect(db)
try:
    r = G.api_thesis(con, {})
except Exception:
    import traceback; traceback.print_exc(); sys.exit(1)
print(r.get("ok"), r.get("error"))
if r.get("ok"):
    print("score", r["score"], "dir", r["direction"], "| score_dir", r["score_dir"], r["direction_dir"], r["strength_dir"], "| regime", r["regime"])
    print([ (s["id"], s.get("kind")) for s in r["signals"]])
    c = sqlite3.connect(G.CACHE_PATH); print(c.execute("select score, score_dir, regime_s from thesis").fetchall())
