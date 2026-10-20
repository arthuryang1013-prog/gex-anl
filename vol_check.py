import sqlite3, sys, pandas as pd
con = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "nq_gex.sqlite")
ts = pd.read_sql("SELECT DISTINCT ts FROM contracts ORDER BY ts", con)["ts"]
last = ts.groupby(ts.str[:10]).last()
for d, t in last.items():
    df = pd.read_sql("SELECT src, dte, strike, cp, oi, vol FROM contracts WHERE ts=?", con, params=(t,))
    print(d, t[11:16], {s: (int(x.oi.sum()), int(x.vol.sum()), int(x.vol.max())) for s, x in df.groupby("src")})
q = pd.read_sql("SELECT ts, strike, cp, dte, oi, vol FROM contracts WHERE src='QQQ' ORDER BY vol DESC LIMIT 8", con)
print("QQQ 成交量最大的 8 筆:"); print(q.to_string())
