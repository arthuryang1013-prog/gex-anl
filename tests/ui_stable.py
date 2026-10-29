"""盤面指標穩定標籤:塞入 2 小時的合成結構分數(前段在 0 附近晃、後段穩定偏空),檢查曲線、翻轉次數對照、標籤。"""
import os, random, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ui_fixture as F
from playwright.sync_api import sync_playwright
out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(); os.makedirs(out, exist_ok=True)
d = tempfile.mkdtemp(); F.serve(d, 8804, "live")
import gex_web_server as G
random.seed(5); now = time.time(); errs = []
for i in range(40):
    t = now - (40 - i) * 180
    sc = random.uniform(-14, 14) if i < 22 else -30 + random.uniform(-6, 6)
    G._thesis_save(f"2026-10-28T{i:02d}:00:00+00:00", t, 29700.0, "偏多" if sc >= 0 else "偏空", int(sc), 50, "弱", dict(direction="偏多", score=1),
                   None, None, None, None, None, score_dir=int(sc), regime_s=0.1, score_struct=sc)
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, (w, h) in (("desktop", (1440, 900)), ("mobile", (390, 844))):
        pg = b.new_page(viewport={"width": w, "height": h}); pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:8804/"); pg.wait_for_timeout(3000)
        pg.click('button[data-tab="mentor"]'); pg.wait_for_timeout(1500)
        txt = pg.inner_text('#mStable'); mdir = pg.inner_text('#mDir')
        print(name, "| 標籤:", mdir.replace("\n", " "), "|", txt[:120].replace("\n", " "))
        assert "舊標籤" not in txt and "平滑後" in txt and pg.query_selector('#mStable svg'), txt
        pg.query_selector('#mentorCard').screenshot(path=os.path.join(out, f"stable-{name}.png"))
    b.close()
print("錯誤:", errs or "無"); assert not errs; print("OK")
