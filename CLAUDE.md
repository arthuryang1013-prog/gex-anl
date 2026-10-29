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

`config.json`(金鑰)、`*.sqlite`(資料庫,含 `mentor_log.sqlite`)、`universe_cache.json`、`orderflow.json`、`orderflow_history.jsonl`、`orderflow_archive/`、`collector_status.json`、`*.log`、`*.bak*`。
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
- 分析腳本(用資料庫的歷史快照):`band_test.py`、`wgt_test.py`、`source_report.py`(含「真關卡 vs 假關卡」對照)、`thesis_outcome.py`(導師分數 ±40 驗證)、`orderflow_check.py`(Delta 對帳)、`basis_check.py`(基差漂移)。
- 自動測試在 `tests/`(假 bookmap、假 IBKR、合成資料);跑法:`python3 tests/test_xxx.py`。它們只證明邏輯,不證明真實環境表現。

## 已知問題(動手前先看)

以下四項已有修正(各自是獨立 patch,部署後請把這裡改成「已修」並記錄驗證結果):
- `imb`(訂單簿失衡)原本取「量最大的 N 檔」。新版 bridge 改取離現價最近 N 檔,舊算法保留為 `imb_big` 對照。
- Delta 原本只由 MBO 掛單事件推判。新版加 `delta_tr` / `cvd_tr`(實際成交),用 `orderflow_check.py` 對帳;**還沒有用真資料對帳過,也還沒跟 Bookmap CVD 手動核對**。
- CANCEL / REPLACE 原本被忽略。新版統計近價(5 點內)撤單 / 加單量(`pull_*` / `stack_*`);注意被成交吃掉的委託也可能以 CANCEL 出現,無法區分。
- 導師分數原本是人工加權且混入 gamma 環境訊號。新版另外給 `score_dir`(方向,不含 gamma)與 `regime`(環境),舊總分不變。用 `thesis_outcome.py` 驗證(先 +40 還是先 −40);**驗證結果出來前,不要宣稱任何一個分數有預測力**。

仍未解決:
- 開盤空窗:新版收集器有合約清單快取與盤前預建,但**只用假的 IBKR 連線測過邏輯**,真實耗時與盤前 OI / 報價是否可取得,要盤外實測。
- QQQ / NDX 換算 NQ 用單一價格比例(忽略基差變動)。`basis_check.py` 真資料實測(2026-10-15~28,美東 09:30–13:30 交易時段):NDX 比例整段漂移 9–17 NQ 點、QQQ 4–7 點,30 分鐘內的抖動 NDX 約 4–9 點、QQQ 約 1–5 點;**美東 15:30 之後(接近 16:00 現貨收盤)NDX/QQQ 的比例會失真(漂移 50–140 點,推測是現貨收盤後 NQ 還在動),交易時段外的 QQQ/NDX 牆不要信**。比例法與差額法在現價 ±300 點內只差約 2 點。結論:交易時段內不需要改換算方式。
- **AI 導師(原始數據版)**:`ai_mentor.py` + `/api/aimentor`,**手動觸發**(導師頁按鈕)。只餵原始數據(逐履約價 OI/量/gamma、1/5 分 K、訂單流每分鐘歷史、大單),不給規則版的方向/分數/flip/牆;回答最後必須附 `<verdict>` 判定區塊,由程式解析並存進 `mentor_log`(kind='ai'),用同一套 ±40 點 / 30 分鐘判對錯,和規則版、永遠做多/做空並排。每次呼叫約 1–3 萬 token 輸入,有 45 秒冷卻;逾時/輸出上限用 config 的 `ai_mentor_timeout_sec`、`ai_mentor_max_tokens`(預設 240 秒 / 8000)。**只用假的 DeepSeek 測過;AI 判斷好不好要靠累積的命中率,沒有驗證前不要宣稱有預測力。**
- **orderflow 逐秒欄位是「近 60 秒滾動累計」**(bridge `WINDOW_SEC=60`,每秒送一次),不是增量。歷史檔逐秒加總會放大約 60 倍;一律用 `orderflow_minutes.minute_rows()`(每分鐘取最後一筆)。已修:訂單流動能副圖、AI 導師每分鐘表、導師 orderflow 摘要;這是讀程式碼發現的,還沒用真資料核對量級。`/api/orderflow` 供盤面指標頁與熱圖掛單牆圖層使用。
- **訊號存檔與逐訊號驗證**:網站每次算盤面指標,會把各訊號的值(-1~+1)存進 `web_cache.sqlite` 的 `thesis.sig_json`;部署後累積幾天,用 `signal_outcome.py`(±40 點 / 30 分鐘)看每個訊號的命中率。舊紀錄沒有這欄,無法回補。樣本少時誤差很大,多訊號同時比較會有偶然高分,**沒有連續幾天、足夠樣本前,不要調權重或宣稱某訊號有效**。
- **訂單流事件(吸收 / 冰山 / 力竭 / 掃單)**:`orderflow_bridge.py` 用成交與掛單簿偵測,封包多一個 `events`;`orderflow_receiver.py` 去重後存 `orderflow_events.jsonl`(留 2 天,已 gitignore);`/api/flowevents` 供熱圖「訂單流事件」圖層畫在對應時間與價位(取代舊的左緣大單/掃單標記)。**偵測門檻(`ABS_*`、`ICE_*`、`EXH_*`、`SWEEP_*`)是初始猜測,只用假行情測過;要看過真實盤面再調,沒有驗證前不要宣稱有預測力。** 更新 bridge 要把新版整份重貼進 Windows 的 Bookmap 並重建,盤外做;receiver 要 `systemctl restart orderflow-receiver`。
- 熱圖 K 線上的導師標記只標 AI 導師的結論;規則版(盤面指標)不再標 #。
- 規則版導師已改稱「盤面指標」:價格動量權重最大(3),方向容易跟著走勢跑,只當參考。
- 導師評論現在會存進 `mentor_log.sqlite`(永久保留,不要放進會被定期清理的 web_cache),日結由 `/api/daily` 產生;對錯由程式用 1 分 K 判定,AI 只負責串成文字。

## 溝通偏好

- 一律用繁體中文。
- 明確區分:已驗證的事實、邏輯推論、推測。講資料來源。
- 統計結果用白話,並用交易結果表達(例如「停利先到的機率」),不要只丟專有名詞。
- 不要奉承;有疑慮就直接說。
