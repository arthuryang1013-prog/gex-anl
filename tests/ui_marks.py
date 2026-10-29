"""導師標記測試:假資料網站載入後,檢查 K 線上有標記、點開能看到原文。用法:python3 tests/ui_marks.py <輸出資料夾>"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_fixture as F
from playwright.sync_api import sync_playwright
out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(); os.makedirs(out, exist_ok=True)
F.SEED_AI = True
d = tempfile.mkdtemp(); port = 8792
F.serve(d, port, "live")
errs = []
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(f"http://127.0.0.1:{port}/"); pg.wait_for_timeout(3000)
    n = pg.evaluate("__mm.items.length")
    print("markers:", n)
    assert n > 0, "no markers"
    kinds = pg.evaluate("__mm.items.map(m => m.x.kind)"); print("kinds:", kinds); assert set(kinds) == {"ai"}, kinds   # 規則版(thesis)不標
    # 以程式模擬點擊第一個標記所在的 K 棒
    pg.evaluate("""()=>{const m=__mm.items[0];const x=__mm.chart.timeScale().timeToCoordinate(m.time);
      const r=document.querySelector('#chart, .chartWrap, body').getBoundingClientRect();window.__x=x;}""")
    box = pg.evaluate("(()=>{const c=document.querySelector('canvas');const r=c.getBoundingClientRect();return [r.left,r.top]})()")
    x = pg.evaluate("window.__x"); y = pg.evaluate("(()=>{const m=__mm.items[0];const b=__mm.D.bars.find(b=>b.time===m.time);return __mm.candle.priceToCoordinate(b.close)})()")
    pg.mouse.click(box[0] + x, box[1] + y); pg.wait_for_timeout(500)
    vis = pg.evaluate("(()=>{const e=document.getElementById('mmPop');return !!e && !e.hidden && e.textContent.length>10})()")
    print("popup visible:", vis)
    pg.screenshot(path=os.path.join(out, "marks.png"))
    # 關閉圖層
    pg.click('#tbToggle'); pg.click('#layers button[data-v="mmarks"]'); pg.wait_for_timeout(800)
    print("after toggle off:", pg.evaluate("__mm.items.length"))
    b.close()
print("errors:", errs or "none")
assert vis and not errs
