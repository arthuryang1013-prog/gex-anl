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
    ctx = b.new_context(viewport={"width": 1440, "height": 900}); pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)))
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
    # 圖例個別開關:關掉「停損」→ 畫面上不再有停損,其他不變;再點恢復;設定會存起來
    kinds = lambda: pg.evaluate("window.__feHits().map(h => h.e.k)")
    k0 = kinds(); assert "stop" in k0 and "absorb" in k0, k0
    pg.click('#feLegend .feG[data-k=stop]'); pg.wait_for_timeout(300)
    k1 = kinds(); print("關掉停損後種類:", sorted(set(k1))); assert "stop" not in k1 and k1.count("absorb") == k0.count("absorb")
    assert pg.evaluate("document.querySelector('#feLegend .feG[data-k=stop]').classList.contains('off')")
    assert pg.evaluate("JSON.parse(localStorage.getItem('gexweb.v1')).feKinds.stop") is False
    for k in ("absorb", "iceberg", "exhaust", "sweep"): pg.click(f'#feLegend .feG[data-k={k}]')
    pg.wait_for_timeout(300); assert kinds() == [], "全部關掉應該一個都不畫"
    assert pg.evaluate("!document.getElementById('feLegend').hidden"), "全關時圖例還要在,才能再打開"
    pg.screenshot(path=os.path.join(out, "fe-legend-off.png"))
    for k in ("stop", "absorb", "iceberg", "exhaust", "sweep"): pg.click(f'#feLegend .feG[data-k={k}]')
    pg.wait_for_timeout(300); assert sorted(kinds()) == sorted(k0), "全部打開要回到原樣"
    # 重新整理後設定保留(關掉冰山)
    pg.click('#feLegend .feG[data-k=iceberg]'); old = pg; pg = ctx.new_page(); old.close(); pg.on("pageerror", lambda e: errs.append(str(e))); pg.goto("http://127.0.0.1:8813/"); pg.wait_for_timeout(3500)
    assert "iceberg" not in kinds() and pg.evaluate("document.querySelector('#feLegend .feG[data-k=iceberg]').classList.contains('off')"), "重新整理後開關要保留"
    pg.click('#feLegend .feG[data-k=iceberg]'); pg.wait_for_timeout(200)
    print("圖例個別開關 OK")
    # 冰山太多時:前 N% 篩選 + 相近合併(只影響畫面)
    base = pg.evaluate("(() => { const h = window.__feHits()[0].e; return { t: h.t, p: h.p } })()")
    pg.evaluate("""(b) => { const ev = []; 
      for (let i = 0; i < 20; i++) ev.push({ k: 'iceberg', s: 'buy', t: b.t + i * 30, p: b.p + 0.25 * (i % 3), v: 30, x: { vis: 5, n: 6, conf: 2, by: 'lvl' } });
      for (let i = 0; i < 20; i++) ev.push({ k: 'iceberg', s: 'sell', t: b.t + i * 600, p: b.p + 20 + 5 * i, v: 10 + i, x: { vis: 5, n: 6, conf: 2, by: 'lvl' } });
      window.__feSet(ev); }""", base)
    ice = lambda: [e for e in pg.evaluate("window.__feShown()") if e["k"] == "iceberg"]
    pg.select_option('#feLegend select[data-top=iceberg]', '100'); pg.wait_for_timeout(200)
    a = ice(); print("合併開、全部:", len(a)); assert len(a) == 21 and max(e.get("cl", 1) for e in a) == 20, [e.get("cl") for e in a]
    pg.click('#feLegend .feG[data-opt=merge]'); pg.wait_for_timeout(200)
    a = ice(); print("合併關、全部:", len(a)); assert len(a) == 40
    pg.click('#feLegend .feG[data-opt=merge]'); pg.select_option('#feLegend select[data-top=iceberg]', '25'); pg.wait_for_timeout(200)
    a = ice(); print("合併開、前25%:", len(a)); assert len(a) == 1 and a[0]["cl"] == 20
    txt = pg.evaluate("document.querySelector('#feLegend .feG[data-k=iceberg] span').textContent"); print("圖例:", txt); assert "1/40" in txt
    assert pg.evaluate("JSON.parse(localStorage.getItem('gexweb.v1')).feTop.iceberg") == 25
    pg.select_option('#feLegend select[data-top=iceberg]', '100')
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
