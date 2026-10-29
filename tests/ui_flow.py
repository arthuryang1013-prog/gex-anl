"""Bookmap 訂單流:盤面指標頁的區塊(讀數表、每分鐘圖、掛單牆)、熱圖掛單牆圖層、副圖成交 Delta 線。假資料。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ui_fixture as F
from playwright.sync_api import sync_playwright
out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(); os.makedirs(out, exist_ok=True)
d = tempfile.mkdtemp(); F.serve(d, 8805, "live")
errs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900}); pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8805/"); pg.wait_for_timeout(3500)
    pg.evaluate("window.__bkw && window.__bkw.redraw()")
    n_of = pg.evaluate("(() => { const c = document.querySelector('#layers button[data-v=bkwall]'); return c ? c.classList.contains('on') : null })()")
    print("掛單牆圖層鈕:", n_of); assert n_of is True
    pg.screenshot(path=os.path.join(out, "flow-heat.png"))
    # 副圖:訂單流動能
    pg.click('#subtabs button[data-v="sq"]'); pg.wait_for_timeout(1500)
    print("副圖標題:", pg.inner_text('#subtitle')[:60]); assert "實際成交" in pg.inner_text('#subtitle')
    pg.screenshot(path=os.path.join(out, "flow-sub.png"))
    # 盤面指標頁
    pg.click('button[data-tab="mentor"]'); pg.wait_for_timeout(2000)
    txt = pg.inner_text('#mFlowBox'); print(txt[:400].replace("\n", " | "))
    assert "連線中" in txt and "Delta(實際成交)" in txt and "買方掛單牆" in txt and "方向一致" in txt, txt
    assert pg.query_selector('#mFlowBox svg.ofSvg')
    # 量級:假資料每秒約 4 口買 + 4 口賣,60 秒窗口 vol 約 400;若逐秒加總會是 ~24000
    import re
    m = re.search(r"總量 ([\d,]+) 口", txt); vol = int(m.group(1).replace(",", "")); print("近60秒總量", vol); assert vol < 2000
    pg.query_selector('#mFlowBox').screenshot(path=os.path.join(out, "flow-panel.png"))
    pg2 = b.new_page(viewport={"width": 390, "height": 844}); pg2.on("pageerror", lambda e: errs.append(str(e)))
    pg2.goto("http://127.0.0.1:8805/"); pg2.wait_for_timeout(2500); pg2.click('button[data-tab="mentor"]'); pg2.wait_for_timeout(2000)
    ow = pg2.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"); print("手機橫向溢出:", ow); assert ow <= 1
    pg2.query_selector('#mFlowBox').screenshot(path=os.path.join(out, "flow-panel-mobile.png"))
    b.close()
print("錯誤:", errs or "無"); assert not errs; print("OK")
