"""對沖模擬 HTTP API 測試:用 ui_fixture 的假網站(真的 gex_web_server)。"""
import json, os, sys, tempfile, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_fixture as F
PORT = 8797
d = tempfile.mkdtemp(); F.serve(d, PORT, "none")
B = f"http://127.0.0.1:{PORT}"

def req(path, body=None, headers=None, raw=False):
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    r = urllib.request.Request(B + path, data=(json.dumps(body).encode() if body is not None else None), headers=h)
    try:
        with urllib.request.urlopen(r, timeout=20) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

st, acc = req("/api/hedge_accounts"); assert st == 200 and acc["range"]["bar_hi"] and acc["accounts"] == [], acc
# 即時帳戶
st, r = req("/api/hedge/create", dict(mode="live", name="t", params=dict(auto=True, threshold=0.4))); assert st == 200, r
aid = r["id"]
st, ch = req(f"/api/hedge_chain?id={aid}"); assert st == 200 and ch["strikes"], ch
atm = min(ch["strikes"], key=lambda s: abs(s["strike"] - ch["ref"]))
ex = ch["expiry"]
legs = [dict(expiry=ex, strike=atm["strike"], cp="C", qty=3), dict(expiry=ex, strike=atm["strike"], cp="P", qty=3)]
st, r = req("/api/hedge/open", dict(id=aid, legs=legs)); assert st == 200, r
st, s = req(f"/api/hedge_state?id={aid}"); assert st == 200 and len(s["legs"]) == 2 and s["trades"], s
assert abs(s["comp"]["total"] - s["equity"]) < 0.05
st, r = req("/api/hedge/step", dict(id=aid, minutes=5)); assert st == 400, (st, r)      # 即時帳戶不能快轉
st, r = req("/api/hedge/hedge", dict(id=aid, units=2)); assert st == 200
st, s = req(f"/api/hedge_state?id={aid}"); assert s["fut"]["qty"] == 2
# 回放帳戶:快轉
st, r = req("/api/hedge/create", dict(mode="replay", start_t=acc["range"]["bar_lo"] + 600, name="r")); assert st == 200, r
rid = r["id"]
st, ch2 = req(f"/api/hedge_chain?id={rid}"); legs2 = [dict(expiry=ch2["expiry"], strike=min(ch2["strikes"], key=lambda s: abs(s["strike"] - ch2["ref"]))["strike"], cp="C", qty=1)]
assert req("/api/hedge/open", dict(id=rid, legs=legs2))[0] == 200
st, r = req("/api/hedge/step", dict(id=rid, minutes=30)); assert st == 200, r
st, s = req(f"/api/hedge_state?id={rid}"); assert s["steps"] >= 25 and len(s["points"]) >= 25, s["steps"]
assert abs(s["comp"]["total"] - s["equity"]) < 0.05
# 防護與錯誤
assert req("/api/hedge/open", dict(id=rid, legs=[]))[0] == 400
assert req("/api/hedge/open", dict(id=9999, legs=legs2))[0] == 400
assert req("/api/hedge/nope", dict(id=rid))[0] == 400
assert req("/api/hedge/open", dict(id=rid, legs=legs2), headers={"Content-Type": "text/plain"})[0] == 415
assert req("/api/hedge/open", dict(id=rid, legs=legs2), headers={"Origin": "http://evil.example"})[0] == 403
assert req("/api/hedge_state")[0] == 400
assert req("/api/login_nope", dict(a=1))[0] == 405
st, r = req("/api/hedge/compare", dict(start_t=acc["range"]["bar_lo"] + 600, end_t=acc["range"]["bar_lo"] + 3600, legs=legs2,
        configs=[dict(label="a"), dict(label="b", auto=True, threshold=0.1)])); assert st == 200 and len(r["results"]) == 2, r
assert req("/api/hedge/reset", dict(id=rid))[0] == 200
st, s = req(f"/api/hedge_state?id={rid}"); assert s["legs"] == [] and s["steps"] == 0
assert req("/api/hedge/delete", dict(id=rid))[0] == 200 and req(f"/api/hedge_state?id={rid}")[0] == 400
print("OK")
