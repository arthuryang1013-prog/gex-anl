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
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "orderflow.json")
HIST = os.path.join(HERE, "orderflow_history.jsonl")
PORT = 8899
HIST_WINDOW_SECS = 2 * 3600   # 保留 2 小時(滿足「至少 1 小時」的需求)
PRUNE_EVERY_SECS = 60          # 每 60 秒才整理一次歷史檔,避免每秒重寫

# 歷史檔只存「彙總數字」;掛單簿(book_bid/book_ask)與成交熱區(trades)很大,
# 每秒都存會把檔案塞爆,而且歷史分析用不到,所以不進歷史檔(只留在 orderflow.json 最新一包)。
HIST_KEEP = {"ts", "received_at", "idle_sec", "lag_sec", "delta", "delta_buy",
             "delta_sell", "vol", "imb", "big", "big_vol", "sweep_buy", "sweep_sell", "spot"}

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
        self._send(200, {"ok": True, "port": PORT, "out": OUT, "hist": HIST})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._send(400, {"ok": False, "error": f"bad json: {e}"})
        data["received_at"] = time.time()
        tmp = OUT + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, OUT)
        except Exception as e:
            return self._send(500, {"ok": False, "error": str(e)})
        _append_history({k: data[k] for k in HIST_KEEP if k in data})
        self._send(200, {"ok": True, "ts": data.get("ts")})


if __name__ == "__main__":
    print(f"orderflow 接收器啟動: http://0.0.0.0:{PORT}  →  {OUT}")
    print(f"歷史檔:{HIST}(保留 {HIST_WINDOW_SECS // 3600} 小時)")
    print("Windows 的 Bookmap addon 會把數字 POST 到這裡。關閉:按 Ctrl+C")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
