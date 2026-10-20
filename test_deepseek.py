"""測試 DeepSeek 金鑰能不能用、連不連得上。
用法:在 Mac 的「終端機」執行
    python3 test_deepseek.py
然後把畫面上印出來的東西全部貼給我。
"""
import json
import urllib.request
import urllib.error

cfg = json.load(open("config.json"))
key = (cfg.get("deepseek_api_key") or "").strip()
base = (cfg.get("deepseek_base_url") or "https://api.deepseek.com").rstrip("/")
model = cfg.get("deepseek_model") or "deepseek-flash"
timeout = int(cfg.get("deepseek_timeout_sec") or 30)

print("=== 設定 ===")
print("key:", (key[:6] + "..." + key[-4:]) if key else "(空)")
print("url:", base + "/chat/completions")
print("model:", model)
print()

if not key:
    print("結果:金鑰是空的,請先填 config.json")
    raise SystemExit

body = json.dumps({
    "model": model,
    "messages": [
        {"role": "system", "content": "你是測試員,只回兩個字:成功"},
        {"role": "user", "content": "測試連線"},
    ],
    "temperature": 0.0,
    "max_tokens": 10,
}).encode("utf-8")

req = urllib.request.Request(
    base + "/chat/completions",
    data=body,
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
)

try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    print("結果:成功!")
    print("回覆:", (data["choices"][0]["message"]["content"] or "").strip())
except urllib.error.HTTPError as e:
    print("結果:HTTP 錯誤,狀態碼", e.code)
    try:
        print("伺服器回覆:", e.read().decode("utf-8"))
    except Exception:
        pass
except urllib.error.URLError as e:
    print("結果:網路連線失敗(連不上伺服器)")
    print("原因:", e.reason)
except Exception as e:
    print("結果:其他錯誤")
    print("原因:", type(e).__name__, e)
