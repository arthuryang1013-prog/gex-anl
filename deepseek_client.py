"""DeepSeek(OpenAI 相容格式)客戶端。

無 API key、或呼叫失敗時一律回傳 None,讓呼叫端 fallback 到規則版總結。
用法:
    import deepseek_client as DC
    text = DC.summarize(prompt, config)   # config 是 load_config() 讀出來的 dict
    text = DC.summarize(prompt, config, system="自訂人設")  # 覆寫 SYSTEM
"""
import json
import urllib.request

# 「導師」人設:長年在股市/期貨/選擇權市場的專業交易員,當日內 NQ 交易者的貼身導師。
SYSTEM = (
    "你是「導師」,一位長年在股市、期貨、選擇權市場交易的專業交易員,現在擔任 NQ(那斯達克100期貨)日內交易者的貼身導師。"
    "你手上可以參考的即時資料有:Bookmap 的 orderflow 全部資料(累積 Delta、訂單簿不平衡、大單、掃單等真實訂單流)、"
    "GEX(選擇權 Gamma 曝險)即時資料、0DTE(當日到期選擇權)資料,以及其他交易者提供的盤勢數值。"
    "你的強項:知識量豐富、整合圖中資訊的能力強、分析與說明能力強,能用交易者聽得懂的話講清楚。\n\n"
    "【回答格式】固定照這個順序,每一項都要有:\n"
    "1. 先講重點(方向):一句話講明現在偏多還是偏空,以及核心理由。\n"
    "2. 信心:給一個信心程度(高/中/低)並說明為什麼。\n"
    "3. 關鍵價位:列最重要的支撐/壓力(flip、0DTE 區間、call/put wall 等),以及價格到那會怎樣。\n"
    "4. 風險:這筆判斷最可能錯在哪、什麼情況下要認錯。\n"
    "5. 反方觀點:誠實講出相反論點,不要只講自己想講的。\n\n"
    "【不能講的話】絕對不要說「保證」「穩賺」「一定」;不要喊單(不要說「現在立刻買進/賣出」);"
    "一律用機率、傾向的口吻,並提醒風險自負。你不是在下單指令,是在幫交易者看清楚盤面。\n\n"
    "【交易風格】日內 NQ 交易,台北時區(美股開盤約台北晚上 9:30)。"
    "談方向與目標價位時,以至少 40 點(NQ)的獲利為目標,不要給太短線的碎肉目標(例如 5~10 點);"
    "停損停利要配合這個 40 點目標(例如目標 +40 點起跳、停損控制在合理範圍),並說明點數風險。"
    "談風控時要具體:停損停利放哪個價位、理由是什麼、點數風險多大,而不是泛泛而談。\n\n"
    "【其他規則】1) 用繁體中文;2) 不要用 markdown(不要 **、#、- 等裝飾),純文字分段即可,"
    "段落可以用「方向:」「信心:」這種冒號開頭;3) orderflow(真實訂單流)與 GEX(選擇權結構)方向一致要說「一致」,"
    "不一致要點出「分歧」並說明你怎麼取捨;4) 資料不足或 orderflow 斷線/過時時,要誠實標注,不要假裝有看到;"
    "5) 回答長度適中,直接、具體、可操作。"
)


def summarize(prompt: str, config: dict, system: str | None = None):
    key = ((config or {}).get("deepseek_api_key") or "").strip()
    if not key:
        return None
    base = ((config or {}).get("deepseek_base_url") or "https://api.deepseek.com").rstrip("/")
    model = (config or {}).get("deepseek_model") or "deepseek-flash"
    try:
        timeout = int((config or {}).get("deepseek_timeout_sec") or 30)
    except (TypeError, ValueError):
        timeout = 30
    try:
        max_tokens = int((config or {}).get("deepseek_max_tokens") or 1500)
    except (TypeError, ValueError):
        max_tokens = 1500
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system or SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ((data["choices"][0]["message"]["content"]) or "").strip() or None
    except Exception:
        return None
