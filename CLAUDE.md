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
- **orderflow 逐秒欄位是「近 60 秒滾動累計」**(bridge `WINDOW_SEC=60`,每秒送一次),不是增量。歷史檔逐秒加總會放大約 60 倍;一律用 `orderflow_minutes.minute_rows()`(每分鐘取最後一筆)。已修:訂單流動能副圖、AI 導師每分鐘表、導師 orderflow 摘要;這是讀程式碼發現的,還沒用真資料核對量級。
- **訊號存檔與逐訊號驗證**:網站每次算盤面指標,會把各訊號的值(-1~+1)存進 `web_cache.sqlite` 的 `thesis.sig_json`;部署後累積幾天,用 `signal_outcome.py`(±40 點 / 30 分鐘)看每個訊號的命中率。舊紀錄沒有這欄,無法回補。樣本少時誤差很大,多訊號同時比較會有偶然高分,**沒有連續幾天、足夠樣本前,不要調權重或宣稱某訊號有效**。
- **訂單流事件(掃單 / 停損 / 冰山 / 吸收 / 力竭)**:`orderflow_bridge.py` 用成交與掛單簿(MBO)偵測,封包多一個 `events`;`orderflow_receiver.py` 去重後存 `orderflow_events.jsonl`(留 2 天,已 gitignore);`/api/flowevents` 供熱圖「訂單流事件」圖層畫在對應時間與價位。概念參考 Bookmap「Stops & Icebergs」與 ATAS MBO 指標的**公開說明**(兩家都沒公開精確演算法與預設數值,所以不是複製,門檻是我們自己的初始值):
  - 掃單:同方向攻擊單(用 `aggressor_order_id` 分張;沒有 id 就用 `is_execution_start` 切批次)在 0.5 秒間隔內併成一波,吃穿 ≥3 價位、≥15 口、≤3 秒。
  - **停損是「推測」**:Bookmap/ATAS 用 MBO 分出停損單觸發的成交,但 Python API 沒有這個旗標。這裡只用特徵猜:一波由 ≥3 張不同攻擊單組成、≥25 口、≥2 點、≤2.5 秒,且突破前 5 分鐘高/低點(資料不到 120 秒不判)。事件帶 `x.inf=1`,畫面標「停損(推測)」。命中這些條件的掃單歸為停損,不重複算掃單。
  - 冰山:(一)`passive_order_id` 對上 MBO 的同一張委託,被打掉量 ≥3 倍它的顯示量且補過量 → `x.conf=2, by=id`;(二)價位統計:同價位 60 秒被打掉量 ≥ max(40, 近 10 分鐘各價位成交量平均+2 標準差)且 ≥3 倍看得見的掛量、價位守住;成交後 1 秒內同價位又出現新掛單 ≥2 次 → `conf=2`(補單證據),沒有補單證據要 1.5 倍門檻、`conf=1`。不知道看得見的掛量就不判。
  - 吸收:20 秒窗口單邊主動量 ≥ max(120, 近 10 分鐘各窗口平均+2 標準差)、佔窗口 65% 以上、價格往攻擊方向 ≤1 點、價格範圍 ≤3 點;前面不到 5 分鐘資料不發;同價附近有 `conf=2` 冰山則 `conf=2`。
  - 力竭:推進段(60~15 秒前)淨量 ≥80、推進 ≥4 點,最後三分之一的量 ≤ 最前三分之一的 0.7 倍,最近 15 秒量萎縮且不創新高/低。
  - **基準線只用 bridge 啟動後的滾動 10 分鐘,不是 Bookmap 那種「同時段」基準**;bridge 剛啟動 5~10 分鐘內吸收/冰山自適應門檻不完整。
  - `aggressor_order_id` / `passive_order_id` 依 Bookmap 文件只在「某些 MBO 情況」提供,**實際有沒有給、冰山能不能用委託 id 對上,要看真資料**;沒有 id 時退回價位/批次統計(可信度較低)。
  - **Bookmap 套件的事實(讀 bookmap 0.1.2 原始碼確認,不是只看說明文件)**:handler 丟例外會讓整個 addon 被關掉(所以 bridge 所有 handler 都吞例外);成交的 `aggressor_order_id`/`passive_order_id` 沒有時是 `str(...)` 不是 None(`_norm_id` 把空字串/null/none/nan/0 視為沒有);設定欄位 `reload_if_change=False` 才不會改設定就重啟;`subscribe_to_bars` 原始碼註解寫 "does not work";成交事件沒有時間戳,bridge 用本機時間。bridge 另外用了:設定面板(門檻可在 Bookmap 內調,名稱前綴 `OF `)、副圖指標 5 條(`OF CVD(trades)`、`OF Delta 60s`、`OF CVD(MBO)`、`OF Imbalance`、`OF Big delta 60s`;CVD 可跟 Bookmap 原生 CVD 對照),每條與每種事件(sweep/stop/iceberg/absorb/exhaust)在設定面板都有開關(`OF ind_*`、`OF ev_*`;關事件 = 不偵測不送),指標註冊時 `is_modifiable=True` 讓 Bookmap 自己的面板也能改色/顯示隱藏、訊息欄(事件觸發寫一行)、廣播(自動訂閱其他 addon 的所有 generator,先只記原始內容)、OTC 成交旗標(不算 Delta)、商品資訊(`mult`、MBO/延遲/交易所特徵)。**這些呼叫只對照過套件原始碼、用假/真套件的訊息解析測過;Bookmap 實際 UI 怎麼顯示、指標副圖的數值單位(是否要換成價格)都沒在真 Bookmap 驗證過。**
  - 診斷:封包 `diag`(只在 `orderflow.json`,不進每日存檔);VPS 上跑 `python3 orderflow_diag.py` 用白話看 id 有沒有給、MBO id 與成交 id 對不對得上、有沒有收到廣播。
  - 測試:`tests/test_orderflow_events.py`(假行情正反例 + 4 組隨機行情 1 小時零誤報)。**只證明邏輯與不會被噪音觸發,不證明抓得到真實事件、更不證明有預測力**;門檻要看過真實盤面再調。更新 bridge 要把新版整份重貼進 Windows 的 Bookmap(保留 `MAC_IP`、`TOKEN`)並重建,盤外做;receiver 與 web 要 `systemctl restart orderflow-receiver gex-web`。
- **`orderflow_bridge.py` 的括號續行不要縮排比起點深**:貼進 Bookmap 編輯器時,它的 Python 摺疊解析器(RSyntaxTextArea PythonFoldParser 3.3.0)遇到「續行縮排比下一行深」會 NullPointerException(2026-10-30 AA/AB 版貼上就碰到)。改完 bridge 後跑 `python3 tests/flatten_bridge.py orderflow_bridge.py` 再跑 `tests/test_bridge_fold_safe.py`(用該版本演算法的移植版模擬,已證實能重現這次的當機);這是模擬,**沒在真的 Bookmap 編輯器驗證過**。
- **事件 / 碰牆的事後成績單**:`flow_outcomes.py`(常駐,範例服務檔 `flow-outcomes.service.example`)把每個訂單流事件記下當時的 GEX 背景(最近的牆/flip 是哪條、離多遠,來源 NQ/QQQ/NDX × 全部到期/0DTE),每次價格碰到牆或 flip 記下當時訂單流,30 分鐘後用 1 分 K 補「±40 點誰先到」與最大有利/不利波動,存 `flow_outcomes.sqlite`(已被 `*.sqlite` 忽略)。`python3 flow_outcomes.py --report` 出白話成績單(預設只看美東 09:30–13:30)。事件的「暗示方向」:掃單/停損=主動方、吸收/冰山=承接方、力竭=反向。限制:結果從事件的下一根完整 1 分 K 開始算;同根同時碰 ±40 標 both;碰牆的流量背景只能取 2 小時內的歷史檔,所以要常駐;**只用假資料測過邏輯,沒有真資料成績前,不要調偵測門檻、不要宣稱任何事件/牆有預測力;樣本少誤差很大**。
- 熱圖 K 線上的導師標記只標 AI 導師的結論;規則版(盤面指標)不再標 #。
- **K 棒由 gex-web 另開的程序抓**(`bars_process`,IB clientId 31),不是收集器。2026-10-29 發生過:IBKR 夜間重置時即時訂閱死了(Error 10182)但 API 連線沒斷,程序一直以為連著,K 棒停了約 10 小時。已加看門狗(開市時間內 `BARS_STALE_SEC` 秒沒更新或收到 10182 就重連);**只用假的 ib_async 測過**。救急:`systemctl restart gex-web`(會重新訂閱並補 4 天歷史),不影響收集器。
- 規則版導師已改稱「盤面指標」:價格動量權重最大(3),方向容易跟著走勢跑,只當參考。
- 導師評論現在會存進 `mentor_log.sqlite`(永久保留,不要放進會被定期清理的 web_cache),日結由 `/api/daily` 產生;對錯由程式用 1 分 K 判定,AI 只負責串成文字。

## 溝通偏好

- 一律用繁體中文。
- 明確區分:已驗證的事實、邏輯推論、推測。講資料來源。
- 統計結果用白話,並用交易結果表達(例如「停利先到的機率」),不要只丟專有名詞。
- 不要奉承;有疑慮就直接說。
