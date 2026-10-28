"""副圖可拖曳調高度、可收合。用法:python3 tests/ui_subpane.py [輸出資料夾]"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_fixture as F
from playwright.sync_api import sync_playwright
out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(); os.makedirs(out, exist_ok=True)
d = tempfile.mkdtemp(); F.serve(d, 8795, "live"); errs = []
H = lambda pg, sel: round(pg.evaluate(f"document.querySelector('{sel}').getBoundingClientRect().height"))
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8795/"); pg.wait_for_timeout(2500)
    s0, c0 = H(pg, '#subwrap'), H(pg, '#chart')
    r = pg.evaluate("(()=>{const r=document.getElementById('subResize').getBoundingClientRect();return [r.left+r.width/2,r.top+4]})()")
    pg.mouse.move(*r); pg.mouse.down(); pg.mouse.move(r[0], r[1] - 120, steps=6); pg.mouse.up(); pg.wait_for_timeout(600)
    s1, c1 = H(pg, '#subwrap'), H(pg, '#chart')
    pg.click('#subFold'); pg.wait_for_timeout(600)
    s2, c2 = H(pg, '#subwrap'), H(pg, '#chart')
    pg.screenshot(path=os.path.join(out, "folded.png"))
    pg.click('#subFold'); pg.wait_for_timeout(600)
    s3 = H(pg, '#subwrap')
    pg.reload(); pg.wait_for_timeout(2000); s4 = H(pg, '#subwrap')
    print("默認", s0, c0, "| 拖高", s1, c1, "| 收合", s2, c2, "| 展開", s3, "| 重整後", s4, "| 錯誤", errs or "無")
    assert s1 > s0 + 80 and c1 < c0 and s2 <= 32 and c2 > c1 and s3 == s1 and s4 == s1 and not errs
    b.close()
print("OK")
