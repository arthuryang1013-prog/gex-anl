#!/usr/bin/env python3
"""orderflow_diag.py — 看 bridge 回報的診斷(讀 orderflow.json 的 diag 欄位),用白話回答:
  Bookmap 有沒有給成交的 id?資料是不是 MBO?有沒有收到其他 addon 的廣播(例如 Bookmap 內建 Stops & Icebergs)?
用法:python3 orderflow_diag.py [orderflow.json 路徑]"""
import json, os, sys, time

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "orderflow.json")
try:
    p = json.load(open(path, encoding="utf-8"))
except Exception as e:
    sys.exit(f"讀不到 {path}:{e}")
age = time.time() - (p.get("received_at") or 0)
d = p.get("diag")
print(f"最後一包:{age:.0f} 秒前" + ("(bridge 可能停了)" if age > 30 else ""))
if not d:
    sys.exit("封包沒有 diag 欄位:Bookmap 裡跑的還是舊版 bridge,需要重貼。")
info = d.get("info") or {}
print(f"商品:{info.get('name')}  契約乘數:{info.get('mult')}(每點美元)")
print(f"Bookmap 回報(這幾項是 True/False 旗標,不是名稱):支援 MBO={info.get('mbo')}  支援深度={info.get('depth')}  延遲資料={info.get('delayed')}  "
      f"exchangeUsedForSubscription={info.get('exch')}  typeUsedForSubscription={info.get('typ')}  marketMode={info.get('mkt')}")
n = d.get("trades", 0) or 0
def pct(x): return f"{100.0 * x / n:.1f}%" if n else "—"
print(f"\n成交筆數(自 bridge 啟動):{n}   場外(OTC)成交:{d.get('otc')}")
print(f"  帶攻擊單 id 的成交:{d.get('aggr_id')}({pct(d.get('aggr_id', 0))})")
print(f"  帶被動單 id 的成交:{d.get('pass_id')}({pct(d.get('pass_id', 0))})   其中對得上我們追蹤中的 MBO 委託:{d.get('pass_match')}")
if d.get("pass_id"):
    print(f"    對不上的:簿上有這張單但沒追蹤 {d.get('pass_live_untracked', '—')}、簿上也沒有(可能是 bridge 啟動前就掛著,或已被撤) {d.get('pass_miss', '—')};對不上的 id 樣本 {d['samples'].get('miss')}")
    print(f"    目前簿上追蹤 {d.get('ord_live')} 張委託、統計中 {d.get('ord_stat')} 張")
print(f"  id 原始值樣本:攻擊 {d['samples'].get('aggr')}  被動 {d['samples'].get('pass')}  MBO 委託 id {d['samples'].get('mbo')}")
print(f"MBO 事件:{d.get('mbo')}  {d.get('mbo_types')}   深度事件:{d.get('depth')}   handler 內部錯誤:{d.get('err')}")
if n and not d.get("aggr_id"):
    print("→ 成交沒有帶攻擊單 id:掃單/停損改用「成交批次」分組,冰山只能靠價位統計(可信度較低)。")
elif n and d.get("pass_id") and d.get("pass_match", 0) < 0.5 * d["pass_id"]:
    print("→ 有被動單 id,但多半對不上 MBO 追蹤的委託:冰山的「同一張委託」證據用不上(可能是 id 格式不同,看上面樣本與 MBO id)。")
bc = d.get("bc") or {}
print(f"\n廣播:訂閱了 {bc.get('subs')} 個來源/generator;收到計數:{bc.get('counts')}")
print(f"  來源清單:{bc.get('status')}")
for r in bc.get("recent") or []:
    print("  ", time.strftime("%H:%M:%S", time.localtime(r[0])), r[1], r[2])
if not bc.get("status"):
    print("  (沒有收到任何廣播來源:Bookmap 內建的 Stops & Icebergs 不是以廣播方式提供,或沒開啟。)")
