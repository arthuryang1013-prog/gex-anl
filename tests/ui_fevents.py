"""訂單流事件(吸收/冰山/力竭/掃單)畫在熱圖上:圖例、事件符號、hover 說明;切換圖層;手機不溢出。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(); os.makedirs(out, exist_ok=True)
import ui_fixture as F
from playwright.sync_api import sync_playwright
d = tempfile.mkdtemp(); F.serve(d, 8813, "live")
errs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900}); pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8813/"); pg.wait_for_timeout(3500)
    n = pg.evaluate("window.__feN ? window.__feN() : -1")
    chip = pg.evaluate("document.querySelector('#layers button[data-v=flmark]').textContent")
    print("圖層鈕:", chip); assert chip == "訂單流事件"
    print("圖例顯示:", pg.evaluate("!document.getElementById('feLegend').hidden")); assert pg.evaluate("!document.getElementById('feLegend').hidden")
    pg.screenshot(path=os.path.join(out, "fe-desktop.png"))
    # hover:找一個命中點
    pos = pg.evaluate("(() => { const h = window.__feHits(); return h.length ? { n: h.length, x: h[0].x, y: h[0].y } : null })()")
    print("可 hover 的事件數:", pos and pos["n"]); assert pos and pos["n"] >= 4
    box = pg.evaluate("(() => { const r = document.querySelector('#chart').getBoundingClientRect(); return [r.left, r.top] })()")
    pg.mouse.move(box[0] + pos["x"], box[1] + pos["y"]); pg.wait_for_timeout(400)
    tip = pg.evaluate("window.__feTip.hidden ? '' : window.__feTip.innerText"); print("tooltip:", tip.replace("\n", " | ")[:200]); assert "推測" in tip or "線索" in tip
    pg.screenshot(path=os.path.join(out, "fe-hover.png"))
    # 關掉圖層
    pg.click('#tbToggle') if pg.query_selector('#tbToggle') and not pg.is_visible('#layers button[data-v=flmark]') else None
    pg.click('#layers button[data-v=flmark]'); pg.wait_for_timeout(500)
    assert pg.evaluate("document.getElementById('feLegend').hidden"); print("關閉後圖例隱藏")
    pg2 = b.new_page(viewport={"width": 390, "height": 844}); pg2.on("pageerror", lambda e: errs.append(str(e)))
    pg2.goto("http://127.0.0.1:8813/"); pg2.wait_for_timeout(3000)
    ow = pg2.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"); print("手機橫向溢出:", ow); assert ow <= 1
    pg2.screenshot(path=os.path.join(out, "fe-mobile.png"))
    b.close()
print("錯誤:", errs or "無"); assert not errs; print("OK")
