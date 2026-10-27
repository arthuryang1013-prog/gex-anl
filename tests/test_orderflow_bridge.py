import sys, types, importlib
fake = types.ModuleType("bookmap"); sys.modules["bookmap"] = fake
import os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orderflow_bridge as b
b._post = lambda pkt: sent.append(pkt); sent = []
b._pips = 0.25; b._size_mult = 1.0
b._tick()  # 建立窗口基準
# 簿:買 100..(離現價近) 小量,遠處一檔超大量
for i in range(20): b.handle_depth(None,"x",True, 400-i, 1)   # 買 400,399,...(×pips)
for i in range(20): b.handle_depth(None,"x",False,401+i, 3)
b.handle_depth(None,"x",True, 300, 1000)   # 遠處大單
assert b.best_bid==400 and b.best_ask==401
imb=b._top_imbalance(); big=b._top_imbalance_big()
assert abs(imb-(10-30)/40)<1e-9, imb          # 最近10檔: 買10 賣30
assert big>0.5, big                            # 舊算法被遠處 1000 帶偏
# 撤單/加單
b.handle_mbo(None,"x","BID_NEW","o1",399,5);  assert b.stack_bid==5
b.handle_mbo(None,"x","REPLACE","o1",399,2);  assert b.pull_bid==3
b.handle_mbo(None,"x","REPLACE","o1",399,6);  assert b.stack_bid==9
b.handle_mbo(None,"x","CANCEL","o1",399,0);   assert b.pull_bid==9 and "o1" not in b.orders
b.handle_mbo(None,"x","BID_NEW","far",100,50);assert b.stack_bid==9   # 遠離現價不計
b.handle_mbo(None,"x","CANCEL","far",100,0);  assert b.pull_bid==9
b.handle_mbo(None,"x","CANCEL","unknown",1,0) # 不認識的 id 不當機
# 改價
b.handle_mbo(None,"x","ASK_NEW","a1",405,4); b.handle_mbo(None,"x","REPLACE","a1",410,4)
assert b.pull_ask==4 and b.stack_ask==8
b._tick() if False else None
# 主動單(MBO 推判) + 成交
b.handle_mbo(None,"x","BID_NEW","m1",401,7); assert b.delta_buy==7 and "m1" not in b.orders
b.handle_trades(None,"x",401,6,False,True,True,True); b.handle_trades(None,"x",400,2,False,False,True,True)
assert (b.tr_buy,b.tr_sell)==(6,2)
b._tick(); p=sent[-1]
for k in ("imb","imb_big","delta","delta_tr","cvd_tr","cvd_mbo","pull_bid","stack_ask"): assert k in p,k
assert p["delta_tr"]==4 and p["delta"]==7, p
# 委託上限
b.ORDER_CAP=10
for i in range(30): b.handle_mbo(None,"x","BID_NEW",f"z{i}",390,1)
assert len(b.orders)<=10
print("bridge ok")
