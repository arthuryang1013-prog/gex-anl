#!/usr/bin/env python3
"""Mac 端 orderflow 接收器。

Windows 的 Bookmap addon 每秒會把一小包 orderflow 數字 POST 到這裡,
我們把它原子寫入 orderflow.json(供 GEX web server 讀取),
同時累積一份歷史檔 orderflow_history.jsonl(保留最近 2 小時),
讓「導師」能讀到至少 1 小時的 orderflow 演變。

用法(在 Mac 終端機):
    python3 orderflow_receiver.py
第一次啟動 macOS 可能跳「允許傳入連線」,要按允許,Windows 才連得進來。
"""
import atexit
import gzip
import hmac
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "orderflow.json")
HIST = os.path.join(HERE, "orderflow_history.jsonl")
CONFIG_PATH = os.path.join(HERE, "config.json")
ARCHIVE_DIR = os.path.join(HERE, "orderflow_archive")   # 每日完整存檔(已加進 .gitignore)
PORT = 8899
MAX_BODY = 1_000_000           # 單一請求最大 1 MB(正常一包約 4~5 KB),防止被塞爆記憶體
ARCHIVE_FLUSH_N = 60           # 每累積 60 包(約 1 分鐘)壓縮寫一次
ARCHIVE_KEEP_DAYS = 30         # 存檔保留天數,0 = 不刪
HIST_WINDOW_SECS = 2 * 3600   # 保留 2 小時(滿足「至少 1 小時」的需求)
PRUNE_EVERY_SECS = 60          # 每 60 秒才整理一次歷史檔,避免每秒重寫

# 歷史檔只存「彙總數字」;掛單簿(book_bid/book_ask)與成交熱區(trades)很大,
# 每秒都存會把檔案塞爆,而且歷史分析用不到,所以不進歷史檔(只留在 orderflow.json 最新一包)。
HIST_KEEP = {"ts", "received_at", "idle_sec", "lag_sec", "delta", "delta_buy",
             "delta_sell", "vol", "imb", "big", "big_vol", "sweep_buy", "sweep_sell", "spot",
             "imb_big", "delta_tr", "vol_tr", "cvd_tr", "cvd_mbo", "pull_bid", "pull_ask",
             "stack_bid", "stack_ask"}

_hist_lock = threading.Lock()
_state = {"last_prune": 0.0}


def _append_history(rec):
    """把一筆 orderflow 紀錄追加到歷史檔,並定期修剪成只保留最近 2 小時。"""
    try:
        with _hist_lock:
            with open(HIST, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            now = time.time()
            if now - _state["last_prune"] >= PRUNE_EVERY_SECS:
                _state["last_prune"] = now
                _prune(now)
    except Exception:
        pass


def _prune(now):
    """把歷史檔修剪成只保留 received_at >= now - HIST_WINDOW_SECS 的行。"""
    try:
        with open(HIST, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return
    cutoff = now - HIST_WINDOW_SECS
    keep = []
    for ln in lines:
        try:
            if json.loads(ln).get("received_at", 0.0) >= cutoff:
                keep.append(ln)
        except Exception:
            continue
    if len(keep) < len(lines):
        tmp = HIST + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, HIST)
        except Exception:
            pass


def _load_token():
    """token 來源:環境變數 ORDERFLOW_TOKEN,否則 config.json 的 orderflow_token。
    每次請求都重新讀(檔案很小),所以改 config.json 不用重啟接收器。
    沒設定 = 空字串 = 不檢查(跟舊版行為一致,方便分階段上線)。"""
    t = (os.environ.get("ORDERFLOW_TOKEN") or "").strip()
    if t:
        return t
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("orderflow_token") or "").strip()
    except Exception:
        return ""


def _token_ok(headers):
    want = _load_token()
    if not want:
        return True
    auth = (headers.get("Authorization") or "").strip()
    got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # 常數時間比較,避免被時間差猜出 token
    return hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8"))


# ---- 每日完整存檔:orderflow_archive/YYYYMMDD.jsonl.gz(UTC 日期) ----
# 一包一行 JSON(含掛單簿與成交熱區),累積一批才壓縮寫入。
# 多段 gzip 串接起來仍是合法的 .gz,讀取時 gzip.open 會自動串起來。
_arch_lock = threading.Lock()
_arch_buf = []          # [(日期字串, json 字串)]
_arch_state = {"last_day": ""}


def _archive_flush_locked():
    if not _arch_buf:
        return
    by_day = {}
    for day, line in _arch_buf:
        by_day.setdefault(day, []).append(line)
    _arch_buf.clear()
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        for day, lines in by_day.items():
            blob = ("\n".join(lines) + "\n").encode("utf-8")
            with open(os.path.join(ARCHIVE_DIR, day + ".jsonl.gz"), "ab") as f:
                f.write(gzip.compress(blob, compresslevel=6))
    except Exception:
        pass   # 存檔失敗不能影響即時資料


def _archive_add(rec):
    try:
        t = rec.get("received_at") or time.time()
        day = time.strftime("%Y%m%d", time.gmtime(t))
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with _arch_lock:
            _arch_buf.append((day, line))
            if len(_arch_buf) >= ARCHIVE_FLUSH_N:
                _archive_flush_locked()
            if day != _arch_state["last_day"]:
                _arch_state["last_day"] = day
                _archive_prune(t)
    except Exception:
        pass


def _archive_prune(now):
    """日期換日時,刪掉超過保留天數的舊存檔。"""
    if not ARCHIVE_KEEP_DAYS:
        return
    try:
        cutoff = time.strftime("%Y%m%d", time.gmtime(now - ARCHIVE_KEEP_DAYS * 86400))
        for name in os.listdir(ARCHIVE_DIR):
            if name.endswith(".jsonl.gz") and name[:8].isdigit() and name[:8] < cutoff:
                os.remove(os.path.join(ARCHIVE_DIR, name))
    except Exception:
        pass


def _archive_flush():
    with _arch_lock:
        _archive_flush_locked()


atexit.register(_archive_flush)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安靜
        pass

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except OSError:
            pass

    def do_GET(self):
        # 只回「活著」,不再洩漏伺服器上的檔案路徑
        self._send(200, {"ok": True})

    def do_POST(self):
        if not _token_ok(self.headers):
            return self._send(401, {"ok": False, "error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._send(400, {"ok": False, "error": "bad Content-Length"})
        if n <= 0 or n > MAX_BODY:
            return self._send(413, {"ok": False, "error": "bad body size"})
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(400, {"ok": False, "error": "bad json"})
        if not isinstance(data, dict):
            return self._send(400, {"ok": False, "error": "body must be a JSON object"})
        data["received_at"] = time.time()
        tmp = OUT + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, OUT)
        except Exception as e:
            return self._send(500, {"ok": False, "error": str(e)})
        _append_history({k: data[k] for k in HIST_KEEP if k in data})
        _archive_add(data)
        self._send(200, {"ok": True, "ts": data.get("ts")})


def _periodic_flush():
    """流量停了也要把緩衝區寫出去,不然最後一小段會留在記憶體裡。"""
    while True:
        time.sleep(30)
        _archive_flush()


if __name__ == "__main__":
    print(f"orderflow 接收器啟動: http://0.0.0.0:{PORT}  →  {OUT}")
    print(f"歷史檔:{HIST}(保留 {HIST_WINDOW_SECS // 3600} 小時)")
    print(f"每日完整存檔:{ARCHIVE_DIR}(保留 {ARCHIVE_KEEP_DAYS} 天)")
    if _load_token():
        print("token 驗證:已啟用(沒帶正確 token 的請求會被拒絕)")
    else:
        print("警告:token 驗證未啟用(config.json 沒有 orderflow_token),任何人連得到都能寫入")
    print("Windows 的 Bookmap addon 會把數字 POST 到這裡。關閉:按 Ctrl+C")
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))   # systemd 停止時也要走 atexit 把存檔寫完
    threading.Thread(target=_periodic_flush, daemon=True).start()
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
