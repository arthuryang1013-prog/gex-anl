#!/bin/bash
# One click: starts the collector (records option data from IB Gateway),
# the orderflow receiver (receives Bookmap data from Windows) and the web site.
# Close this window or press Ctrl+C to stop ALL of them.
cd "$(dirname "$0")"

COLLECTOR_PID=""
RECEIVER_PID=""

if pgrep -f "[n]q_gex_collector.py" >/dev/null; then
  echo "收集程式已經在跑了,這次不重複啟動。"
else
  echo "啟動收集程式…(需要 IB Gateway 已開啟並登入,連接埠 4001)"
  python3 nq_gex_collector.py >> collector.log 2>&1 &
  COLLECTOR_PID=$!
  sleep 3
  if ! kill -0 "$COLLECTOR_PID" 2>/dev/null; then
    echo
    echo "收集程式一啟動就停了。最後幾行訊息:"
    tail -n 15 collector.log
    echo
    echo "多半是 IB Gateway 還沒登入。登入後再雙擊這個檔案一次。"
    read -p "按 Enter 關閉視窗"
    exit 1
  fi
  echo "收集程式已啟動(紀錄寫在 collector.log)。"
fi

if pgrep -f "[o]rderflow_receiver.py" >/dev/null; then
  echo "orderflow 接收器已經在跑了,這次不重複啟動。"
else
  echo "啟動 orderflow 接收器…(接收 Windows Bookmap 傳來的訂單流,連接埠 8899)"
  python3 orderflow_receiver.py >> receiver.log 2>&1 &
  RECEIVER_PID=$!
  sleep 1
  if ! kill -0 "$RECEIVER_PID" 2>/dev/null; then
    echo
    echo "orderflow 接收器一啟動就停了。最後幾行訊息:"
    tail -n 15 receiver.log
    echo
    echo "(如果前面跳「允許傳入連線」的視窗,要按允許,Windows 才連得進來。)"
  else
    echo "orderflow 接收器已啟動(紀錄寫在 receiver.log)。"
  fi
fi

cleanup() {
  if [ -n "$COLLECTOR_PID" ]; then
    kill "$COLLECTOR_PID" 2>/dev/null
    echo "已停止收集程式。"
  fi
  if [ -n "$RECEIVER_PID" ]; then
    kill "$RECEIVER_PID" 2>/dev/null
    echo "已停止 orderflow 接收器。"
  fi
}
trap cleanup EXIT INT TERM

echo "啟動 GEX 網站…(瀏覽器會自動開啟 http://127.0.0.1:8765)"
python3 gex_web_server.py
echo
read -p "已停止,按 Enter 關閉視窗"
