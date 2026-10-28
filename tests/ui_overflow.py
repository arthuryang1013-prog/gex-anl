"""檢查各分頁在手機 / 桌面寬度下有沒有橫向溢出(document 寬度 > 視窗寬度)。用法:python3 tests/ui_overflow.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_fixture as F
from playwright.sync_api import sync_playwright
F.serve(tempfile.mkdtemp(), 8793, "live")
bad = []
with sync_playwright() as p:
    b = p.chromium.launch()
    for w, h in ((390, 844), (768, 900), (1440, 900)):
        pg = b.new_page(viewport={"width": w, "height": h})
        pg.goto("http://127.0.0.1:8793/"); pg.wait_for_timeout(2500)
        for t in ("map", "table", "anly", "mentor", "review"):
            pg.click(f'button[data-tab="{t}"]'); pg.wait_for_timeout(900)
            sw = pg.evaluate("document.documentElement.scrollWidth")
            if sw > w + 1:
                bad.append(f"{w}px / {t}: 寬度 {sw}")
    b.close()
print("\n".join(bad) if bad else "no horizontal overflow")
sys.exit(1 if bad else 0)
