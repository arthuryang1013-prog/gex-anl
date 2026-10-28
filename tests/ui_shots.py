"""用假資料啟動網站並截圖(桌面 + 手機寬度),同時收集瀏覽器 console 錯誤。
用法:python3 tests/ui_shots.py <輸出資料夾> [live|stale|none]    需要 playwright(chromium)。"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_fixture as F
from playwright.sync_api import sync_playwright

out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
mode = sys.argv[2] if len(sys.argv) > 2 else "live"
tabs = (sys.argv[3] if len(sys.argv) > 3 else "map,mentor").split(",")
os.makedirs(out, exist_ok=True)
d = tempfile.mkdtemp(); port = 8791
F.serve(d, port, mode)
errs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, (w, h) in (("desktop", (1440, 900)), ("mobile", (390, 844))):
        pg = b.new_page(viewport={"width": w, "height": h})
        pg.on("console", lambda m: errs.append(f"[{name}] console {m.type}: {m.text}") if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(f"[{name}] pageerror: {e}"))
        pg.goto(f"http://127.0.0.1:{port}/"); pg.wait_for_timeout(2500)
        for t in tabs:
            pg.click(f'button[data-tab="{t}"]'); pg.wait_for_timeout(1800)
            pg.screenshot(path=os.path.join(out, f"{mode}-{name}-{t}.png"), full_page=(t != "map"))
    b.close()
print("\n".join(errs) if errs else "no console errors")
