"""對沖模擬頁:用假網站跑真瀏覽器。用法:python3 tests/ui_hedge.py [輸出資料夾]"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_fixture as F
from playwright.sync_api import sync_playwright
out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(); os.makedirs(out, exist_ok=True)
d = tempfile.mkdtemp(); F.serve(d, 8796, "none"); errs = []
answers = ["1", "", "回放測試"]      # 模式=回放、起點留空…
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    def on_dialog(dlg):
        dlg.accept(answers.pop(0) if dlg.type == "prompt" and answers else None) if dlg.type == "prompt" else dlg.accept()
    pg.on("dialog", on_dialog)
    pg.goto("http://127.0.0.1:8796/"); pg.wait_for_timeout(1500)
    pg.click('#tabs [data-tab="hedge"]'); pg.wait_for_timeout(800)
    assert pg.is_visible("#view-hedge") and not pg.is_visible("#view-map")
    # 建回放帳戶:起點在假資料裡取 bar_lo + 60 分(預設起點的 09:30 ET 可能不在假資料範圍內)
    info = pg.evaluate("fetch('/api/hedge_accounts').then(r=>r.json())")
    answers[:] = ["1", __import__("datetime").datetime.fromtimestamp(info["range"]["bar_lo"] + 3600, __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "回放測試"]
    pg.click("#hgNew"); pg.wait_for_selector("#hgChain td.px", timeout=8000)
    assert pg.is_visible("#hgReplayCtl"), "回放控制列沒出現"
    # 套用跨式 → 送出
    pg.select_option("#hgPreset", "longStraddle"); pg.fill("#hgQty", "2"); pg.click("#hgApply")
    assert pg.locator("#hgBuilder .hg-leg").count() == 2
    pg.click("#hgSubmit"); pg.wait_for_selector("#hgLegs tr td button[data-close]", timeout=8000)
    assert pg.locator("#hgLegs button[data-close]").count() == 2
    # 價差與鐵蝴蝶:腿數
    for key, n in (("bullCall", 2), ("shortFly", 4), ("shortStrangle", 2), ("sellP", 1)):
        pg.select_option("#hgPreset", key); pg.click("#hgApply"); assert pg.locator("#hgBuilder .hg-leg").count() == n, key
    pg.click("#hgClear")
    pg.select_option("#hgPreset", "buyC"); pg.fill("#hgQty", "4"); pg.click("#hgApply"); pg.click("#hgSubmit"); pg.wait_for_timeout(1000)
    assert pg.locator("#hgLegs button[data-close]").count() == 3
    # 開自動對沖、門檻 0.3,快轉 30 分
    pg.fill("#hgThr", "0.3"); pg.check("#hgAuto"); pg.wait_for_timeout(600)
    pg.click('[data-hgstep="30"]'); pg.wait_for_timeout(1500)
    st = pg.evaluate("fetch('/api/hedge_state?id='+document.querySelector('#hgAcct').value).then(r=>r.json())")
    assert st["steps"] >= 25 and st["params"]["auto"] and len(st["points"]) >= 25, (st["steps"], st["params"])
    assert abs(st["comp"]["total"] - st["equity"]) < 0.05
    # 手動對沖按鈕與播放
    pg.click("#hgHedge"); pg.wait_for_timeout(800)
    assert pg.evaluate("document.querySelector('#hgCmpOut') !== null")
    pg.click("#hgPlay"); pg.wait_for_timeout(2600); pg.click("#hgPlay")
    pg.wait_for_timeout(500)
    print("rows", pg.locator("#hgTrades tr").count(), "kpi", pg.locator("#hgCards .hg-kpi").count(), errs); pg.screenshot(path=os.path.join(out, "dbg.png"))
    assert pg.locator("#hgTrades tr").count() > 3 and pg.locator("#hgCards .hg-kpi").count() == 9
    # 比較
    pg.click("#hgCmp"); pg.wait_for_selector("#hgCmpOut table", timeout=20000)
    assert pg.locator("#hgCmpOut table tr").count() == 6
    pg.screenshot(path=os.path.join(out, "hedge.png"))
    # 重整後帳戶仍在
    pg.reload(); pg.wait_for_timeout(1200); pg.click('#tabs [data-tab="hedge"]'); pg.wait_for_selector("#hgLegs", timeout=5000); pg.wait_for_timeout(1200)
    assert "回放測試" in pg.inner_text("#hgAcct") and pg.locator("#hgLegs button[data-close]").count() == 3
    # 切走再回來不報錯
    pg.click('#tabs [data-tab="map"]'); pg.wait_for_timeout(300); pg.click('#tabs [data-tab="hedge"]'); pg.wait_for_timeout(600)
    print("錯誤:", errs or "無"); assert not errs
    b.close()
print("OK", out)
