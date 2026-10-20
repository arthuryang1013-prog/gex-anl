#!/usr/bin/env python3
"""清掉資料庫裡 IBKR 的「沒有成交量」標記(2147483647)。會先備份資料庫。用法: python3 fix_bad_volume.py [nq_gex.sqlite]"""
import shutil, sqlite3, sys, time
db = sys.argv[1] if len(sys.argv) > 1 else "nq_gex.sqlite"
bak = f"{db}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(db, bak); print("已備份到", bak)
con = sqlite3.connect(db)
n = con.execute("SELECT COUNT(*) FROM contracts WHERE vol >= 2000000000 OR vol < 0").fetchone()[0]
con.execute("UPDATE contracts SET vol = 0, gex_vol = 0 WHERE vol >= 2000000000 OR vol < 0")
con.commit(); print(f"已修正 {n} 筆")
try:
    con.execute("DELETE FROM lv"); con.commit()
except Exception:
    pass
