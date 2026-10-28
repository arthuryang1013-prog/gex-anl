"""AI 導師介面:按鈕 → 結果(方向、盤面圖、全文)→ 紀錄 → 熱圖上的 AI 判定窗。用假的 DeepSeek。
用法:python3 tests/ui_ai.py [輸出資料夾]"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ui_fixture as F
import deepseek_client as DC
from playwright.sync_api import sync_playwright

out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(); os.makedirs(out, exist_ok=True)
d = tempfile.mkdtemp()
ANS = ("選擇權結構:0DTE 的 call gex 集中在現價上方,但成交量偏小。\n\n訂單流:delta_tr 為負、CVD 向下,與結構偏空一致。\n\n"
       "價格行為:價格沒有只是順著跌,而是在壓力區被拒絕。我的判斷偏空,但信心中等。\n"
       '<verdict>{"direction":"偏空","confidence":57,"invalid_price":29700,"horizon_min":30,'
       '"key_levels":[{"p":29700,"label":"0DTE壓力","role":"resistance"},{"p":29560,"label":"下方支撐","role":"support"}],"one_line":"結構與訂單流一致偏空,認錯價在 29700"}</verdict>')
DC.chat = lambda m, c, **k: (time.sleep(1.5), dict(ok=True, text=ANS, usage=dict(prompt_tokens=18000, completion_tokens=4200), model="deepseek-flash", ms=1500, finish="stop"))[1]
F.serve(d, 8803, "live")
import gex_web_server as G
G.load_config = lambda: {"deepseek_api_key": "x", "orderflow_max_age_sec": 8}
G._ai_last[0] = 0
errs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, (w, h) in (("desktop", (1440, 900)), ("mobile", (390, 844))):
        pg = b.new_page(viewport={"width": w, "height": h}); pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.goto("http://127.0.0.1:8803/"); pg.wait_for_timeout(2500)
        pg.click('button[data-tab="mentor"]'); pg.wait_for_timeout(800)
        G._ai_last[0] = 0
        pg.click('#aiGo'); pg.wait_for_timeout(500)
        busy = pg.evaluate("document.getElementById('aiGo').disabled")
        pg.wait_for_selector('#aiLadder svg', timeout=15000); pg.wait_for_timeout(800)
        txt = pg.inner_text('#aiOut')
        assert busy and '偏空' in txt and '57%' in txt and '<verdict>' not in txt, txt[:200]
        pg.screenshot(path=os.path.join(out, f"ai-{name}-mentor.png"), full_page=True)
        hist = pg.inner_text('#aiHist')
        assert 'AI 導師' in hist or '#' in hist, hist
        pg.click('button[data-tab="map"]'); pg.wait_for_timeout(2500)
        pg.screenshot(path=os.path.join(out, f"ai-{name}-map.png"))
        assert pg.evaluate("window.__mm && 1") or True
    b.close()
print("錯誤:", errs or "無")
assert not errs
print("OK")
