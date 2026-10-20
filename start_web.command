#!/bin/bash
# Starts the NQ GEX web site (reads nq_gex.sqlite in this folder). Close the window or press Ctrl+C to stop.
cd "$(dirname "$0")"
echo "啟動 GEX 網站…(瀏覽器會自動開啟 http://127.0.0.1:8765)"
python3 gex_web_server.py
echo
read -p "已停止,按 Enter 關閉視窗"
