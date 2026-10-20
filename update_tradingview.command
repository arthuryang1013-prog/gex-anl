#!/bin/bash
# 一鍵:更新全部歷史價位 -> 重建 Pine 腳本 -> 複製到剪貼簿 -> 打開 TradingView。
# 之後你只要在 Pine 編輯器按 Cmd+A、Cmd+V、Cmd+S。

cd "$(dirname "$0")" || exit 1

echo "1/3 從資料庫補齊每天的價位,並重建 nq_gex_levels.pine ..."
if ! python3 gex_levels.py --backfill; then
  echo
  echo "出錯了(上面是原因)。按 Enter 關閉。"
  read -r
  exit 1
fi

echo
echo "2/3 複製到剪貼簿 ..."
pbcopy < nq_gex_levels.pine
osascript -e 'display notification "已複製。到 Pine 編輯器:Cmd+A、Cmd+V、Cmd+S" with title "NQ GEX 價位"' >/dev/null 2>&1

echo "3/3 打開 TradingView ..."
if ! open -a "TradingView" 2>/dev/null; then
  echo "(找不到名為 TradingView 的桌面 App,請自己切換到它)"
fi

echo
echo "完成。接下來在 TradingView:"
echo "  1. 打開下方的 Pine 編輯器(點圖表下方「Pine 編輯器」)"
echo "  2. 點進程式碼區 -> Cmd+A(全選)-> Cmd+V(貼上)-> Cmd+S(儲存)"
echo "  3. 若指標已在圖上,儲存後它會自動更新;沒有的話按「加到圖表」"
echo
echo "按 Enter 關閉此視窗。"
read -r
