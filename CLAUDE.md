# gex-anl 專案說明(給 Claude Code 讀)

這是一個 NQ(那斯達克 100 期貨)日內交易用的 GEX + orderflow 工具。使用者自己做自己用,多台電腦開發,**GitHub 是唯一的真實來源**。
這個 repo 是公開的:**不要在任何檔案、commit 訊息裡放金鑰、密碼、token、IP 位址、帳號資料。**

## 開工與收工(多台電腦同步,最重要)

1. 開始工作前先執行 `git fetch`,再 `git status -sb`。如果顯示落後(behind),先 `git pull` 再動手。
   本機有沒提交的修改時,先告訴使用者,不要自己 stash 或覆蓋。
2. 收工前把改動提交,commit 訊息用繁體中文,寫清楚「改了什麼、為什麼」。
3. **`git push` 之前一定先問使用者。** 不要自己推送,不要推到 main 以外你沒被要求的分支。
4. 不同機器不要同時改同一個檔案;衝突時把衝突內容貼給使用者,不要自己硬合併。

## 絕對不要提交的東西(已在 .gitignore)

`config.json`(金鑰)、`*.sqlite`(資料庫)、`orderflow.json`、`orderflow_history.jsonl`、`orderflow_archive/`、`collector_status.json`、`*.log`、`*.bak*`。
新增會產生資料或含秘密的檔案時,先加進 `.gitignore`。

## 架構(資料怎麼流)

- `nq_gex_collector.py`:連 IB Gateway(本機 4001,唯讀),收 NQ / QQQ / NDX 選擇權資料,寫入 `nq_gex.sqlite`;同時寫 `collector_status.json`(給網站顯示連線狀況)。
- `gex_levels.py`:算牆(call/put wall)與 flip 等關卡,收集器和網站共用。
- `gex_web_server.py` + `web/`:網站(讀同一個 SQLite 檔),含「導師」(DeepSeek 寫總結,`deepseek_client.py`)。
- `orderflow_bridge.py`:**貼進 Windows 那台 Bookmap 的 Python 編輯器執行**,每秒 POST 一包 orderflow 到伺服器。它不能讀 `config.json`,設定要直接改檔案開頭的常數。
- `orderflow_receiver.py`:收 bridge 的資料(埠 8899),寫 `orderflow.json`、歷史檔與每日壓縮存檔。
- 正式環境在雲端 VPS(Linux,systemd 服務:`gex-collector`、`gex-web`、`ibgateway`、`orderflow-receiver`)。`*.command` 是 Mac 本機一鍵啟動用。

## 重要慣例(不要悄悄改)

- GEX 正負號是**慣例**:call 為正、put 為負。這不是已驗證的事實,不要在文字或註解裡寫成事實。
- QQQ / NDX 以價格比例換算成 NQ 座標,忽略基差變動(已知限制)。
- OI 是前一日數字;成交量沒有買賣方向,也沒有莊家 / 客戶區分。
- gamma 計算目前在多個檔案重複,**改公式要全部一起改**,並且告訴使用者。

## 交易時段限制(很重要)

使用者只交易紐約盤 09:30–13:30 ET,台北時間約 21:30–01:30(美國冬令時間 11/1 後變 22:30–02:30)。
- **盤中不要重啟收集器或 Bookmap addon。** 收集器重啟後,QQQ/NDX 要重建合約清單,約 20–30 分鐘沒有新快照。
- 部署、重啟都排在盤外。需要重啟時,先確認現在是不是盤中。

## 驗證怎麼做

- 改完先 `python3 -m py_compile <檔案>`。
- 沒有 IB Gateway 時,無法測真正的收集;用假的連線或模擬的狀態檔測邏輯,並**說明哪些是沒測到的**。
- 分析腳本(用資料庫的歷史快照):`band_test.py`、`wgt_test.py`、`source_report.py`(含「真關卡 vs 假關卡」對照)。

## 已知問題(動手前先看)

- `orderflow_bridge.py` 的訂單簿失衡 `imb` 取的是「量最大的 N 檔」,不是「離現價最近的 N 檔」,與註解不符。
- Delta 是由 MBO 掛單事件推判主動方,未用實際成交(trades)對照過,需與 Bookmap 的 CVD 核對。
- bridge 目前忽略 CANCEL / REPLACE 事件(看不到撤單與拉單)。
- 導師的方向分數是人工加權,尚未用實際結果(先到 +40 還是 −40)驗證。

## 溝通偏好

- 一律用繁體中文。
- 明確區分:已驗證的事實、邏輯推論、推測。講資料來源。
- 統計結果用白話,並用交易結果表達(例如「停利先到的機率」),不要只丟專有名詞。
- 不要奉承;有疑慮就直接說。
