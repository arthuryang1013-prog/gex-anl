"""導師圖像化:熱圖右側的情境圖有畫出來(取畫面像素檢查)、導師頁盤面圖有內容、可用圖層關掉。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_fixture as F
from playwright.sync_api import sync_playwright
d = tempfile.mkdtemp(); F.serve(d, 8802, "live"); errs = []
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8802/"); pg.wait_for_timeout(5000)
    # 情境區在最新 K 線右側:該區若有繪製,與關掉圖層後的畫面不同
    pg.click('#tbToggle'); pg.wait_for_timeout(1500)   # 先展開工具列,版面固定後再比對
    pg.screenshot(path=os.path.join(d, "on.png"))
    pg.click('#layers button[data-v="mscene"]'); pg.wait_for_timeout(800)
    pg.screenshot(path=os.path.join(d, "off.png"))
    from PIL import Image, ImageChops
    a, c = Image.open(os.path.join(d, "on.png")).convert("RGB"), Image.open(os.path.join(d, "off.png")).convert("RGB")
    diff = ImageChops.difference(a.crop((20, 230, 1015, 600)), c.crop((20, 230, 1015, 600))).getbbox()
    print("情境圖差異區域:", diff)
    pg.click('button[data-tab="mentor"]'); pg.wait_for_timeout(1500)
    n = pg.evaluate("document.getElementById('mLadder').innerHTML.length")
    print("盤面圖長度:", n, "| 錯誤:", errs or "無")
    assert diff and n > 500 and not errs
    b.close()
print("OK")
