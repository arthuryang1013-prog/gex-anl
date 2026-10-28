import io, os, sys, contextlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gex_web_server as G

H = lambda **k: {kk.replace("_", "-"): v for kk, v in k.items()}
class Hd(dict):
    def get(self, k, d=None): return dict.get(self, k, d)

# 外面直接連進來:偽造的 X-Forwarded-For / X-Real-IP 一律不信
assert G._client_ip(Hd({"X-Forwarded-For": "9.9.9.9"}), ("1.2.3.4", 5555)) == "1.2.3.4"
assert G._client_ip(Hd({"X-Real-IP": "9.9.9.9"}), ("1.2.3.4", 5555)) == "1.2.3.4"
# 本機代理轉進來:相信表頭;沒有表頭就用連線來源
assert G._client_ip(Hd({"X-Forwarded-For": "8.8.8.8, 7.7.7.7"}), ("127.0.0.1", 1)) == "7.7.7.7"
assert G._client_ip(Hd({"X-Real-IP": "6.6.6.6"}), ("::1", 1, 0, 0)) == "6.6.6.6"
assert G._client_ip(Hd({}), ("127.0.0.1", 1)) == "127.0.0.1"
# 偽造表頭無法逃過鎖定:同一個真實 IP 換不同假表頭,仍然累計
G._login_fails.clear()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    for i in range(G.LOGIN_MAX_FAILS):
        ip = G._client_ip(Hd({"X-Forwarded-For": f"10.0.0.{i}"}), ("1.2.3.4", 1))
        G._login_fail(ip, "login頁")
assert G._login_blocked("1.2.3.4")
out = buf.getvalue()
assert out.count("[login] 失敗 ip=1.2.3.4") == G.LOGIN_MAX_FAILS and "暫時鎖定" in out
assert "pass" not in out.lower().replace("passw", "")  # 不記密碼
G._login_ok("1.2.3.4"); assert not G._login_blocked("1.2.3.4")
print("login hardening ok")
