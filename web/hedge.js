/* 對沖模擬頁 -- 獨立檔案(不依賴 app.js 的內部變數),只提供 window.hedgeTab(on)。
   資料全部來自 /api/hedge_*(GET)與 /api/hedge/<action>(POST JSON)。模擬帳戶存在伺服器的 hedge_sim.sqlite。 */
(() => {
'use strict';
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const LW = window.LightweightCharts;
const KEY = 'hedge.v1';
const H = { accts: [], info: null, id: null, st: null, chain: null, builder: [], timer: null, playing: false, busy: false, charts: {}, hidden: {} };
const COMP = [['total', '總損益', '#e9edf4'], ['delta', 'Delta', '#7d9bd9'], ['gamma', 'Gamma', '#39d98a'], ['theta', 'Theta', '#ff5c6c'],
  ['vega', 'Vega', '#d58cff'], ['resid', '其他', '#8a93a4'], ['fut', '期貨對沖', '#e7a44c'], ['cost', '成本', '#ff9d5c']];

function tz() { try { return (JSON.parse(localStorage.getItem('gexweb.v1')) || {}).tz || 'Asia/Taipei'; } catch (e) { return 'Asia/Taipei'; } }
function fmtT(t) { if (!t) return '—'; return new Intl.DateTimeFormat('zh-TW', { timeZone: tz(), month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(t * 1000)); }
const n0 = (v) => (v == null ? '—' : Math.round(v).toLocaleString('en-US'));
const n2 = (v) => (v == null ? '—' : Number(v).toFixed(2));
const sg = (v, d = 0) => (v == null ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }));
const cls = (v) => (v > 0.005 ? 'hg-up' : v < -0.005 ? 'hg-dn' : '');
function toast(m) { const t = $('#toast'); if (!t) return alert(m); t.textContent = m; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => { t.hidden = true; }, 6000); }

async function get(name, params = {}) {
  const r = await fetch(`/api/${name}?${new URLSearchParams(params)}`, { cache: 'no-store' });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error(j.error || r.status);
  return j;
}
async function post(action, body = {}) {
  const r = await fetch(`/api/hedge/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error(j.error || r.status);
  return j;
}
async function guarded(fn) { try { return await fn(); } catch (e) { toast('對沖模擬:' + e.message); return null; } }

/* ---------------- 帳戶 ---------------- */
async function loadAccts(keep) {
  H.info = await get('hedge_accounts');
  H.accts = H.info.accounts;
  let saved = null; try { saved = (JSON.parse(localStorage.getItem(KEY)) || {}).id; } catch (e) { /* ignore */ }
  if (!keep || !H.accts.some((a) => a.id === H.id)) H.id = H.accts.some((a) => a.id === saved) ? saved : (H.accts[0] || {}).id || null;
  $('#hgAcct').innerHTML = H.accts.map((a) => `<option value="${a.id}">${esc(a.name)} · ${a.mode === 'live' ? '即時' : '回放'} #${a.id}</option>`).join('') || '<option value="">（沒有帳戶）</option>';
  if (H.id) $('#hgAcct').value = H.id;
  if (H.id) await refresh(true); else { H.st = null; H.chain = null; render(); }
}
function remember() { try { localStorage.setItem(KEY, JSON.stringify({ id: H.id })); } catch (e) { /* ignore */ } }

async function newAcct() {
  const mode = prompt('模式:輸入 1 = 歷史回放,2 = 即時模擬', '1');
  if (mode !== '1' && mode !== '2') return;
  const body = { mode: mode === '1' ? 'replay' : 'live' };
  if (mode === '1') {
    const def = H.info.default_start ? new Date(H.info.default_start * 1000).toISOString().slice(0, 16) + 'Z' : '';
    const txt = prompt('回放起點(UTC,例如 2026-11-03T14:30Z = 美東 09:30;留空 = 最近一個交易日 09:30 ET)', def);
    if (txt === null) return;
    if (txt.trim()) { const t = Date.parse(txt.trim()); if (!isFinite(t)) return toast('時間格式不對'); body.start_t = Math.floor(t / 1000); }
  }
  const name = prompt('帳戶名稱', mode === '1' ? '歷史回放' : '即時模擬'); if (name === null) return;
  body.name = name;
  const r = await guarded(() => post('create', body)); if (!r) return;
  H.id = r.id; remember(); await loadAccts(true);
}

/* ---------------- 狀態與畫面 ---------------- */
async function refresh(withChain) {
  if (!H.id || H.busy) return;
  H.busy = true;
  try {
    H.st = await get('hedge_state', { id: H.id });
    if (withChain || !H.chain) H.chain = await get('hedge_chain', { id: H.id, expiry: ($('#hgExp').value || '') });
  } catch (e) { toast('對沖模擬:' + e.message); stopPlay(); } finally { H.busy = false; }
  render();
}
function render() {
  const st = H.st;
  $('#hgMode').textContent = st ? (st.mode === 'live' ? '即時模擬' : '歷史回放') : '';
  $('#hgMode').className = 'hg-badge' + (st && st.mode === 'live' ? ' live' : '');
  $('#hgClock').textContent = st ? `${fmtT(st.clock_t)} · NQ ${n2(st.price)}` + (st.snap_age != null ? ` · 快照落後 ${Math.round(st.snap_age / 60)} 分` : '') : '請先建立帳戶';
  $('#hgReplayCtl').hidden = !(st && st.mode === 'replay');
  if (st) fillParams(st.params);
  renderChain(); renderBuilder(); renderCards(); renderLegs(); renderTrades(); renderCharts();
}
function fillParams(p) {
  if (document.activeElement && document.activeElement.closest && document.activeElement.closest('.hg-card') && ['INPUT', 'SELECT'].includes(document.activeElement.tagName)
    && ['hgThr', 'hgTgt', 'hgCool', 'hgHs', 'hgOptFee', 'hgNqFee', 'hgMnqFee', 'hgSlip'].includes(document.activeElement.id)) return;
  $('#hgAuto').checked = !!p.auto; $('#hgThr').value = p.threshold; $('#hgTgt').value = p.target; $('#hgCool').value = p.cooldown_min;
  $('#hgInst').value = String(p.fut_mult); $('#hgHs').value = p.opt_half_spread; $('#hgOptFee').value = p.opt_fee;
  $('#hgNqFee').value = p.fut_fee_nq; $('#hgMnqFee').value = p.fut_fee_mnq; $('#hgSlip').value = p.fut_slip;
}
function renderCards() {
  const st = H.st; if (!st) { $('#hgCards').innerHTML = ''; return; }
  const t = st.totals || {}, c = st.comp || {};
  const k = (name, val, sub, cl) => `<div class="hg-kpi"><div class="k">${name}</div><div class="v ${cl || ''}">${val}</div><div class="s">${sub || ''}</div></div>`;
  $('#hgCards').innerHTML = [
    k('總損益', '$' + sg(st.equity, 0), `已含成本 $${n0(-c.cost)}`, cls(st.equity)),
    k('淨 Delta(口 NQ)', sg(t.net_delta, 2), `選擇權 ${sg(t.opt_delta, 2)} + 期貨 ${sg(t.fut_delta, 2)}`, ''),
    k('NQ 動 10 點約損益', '$' + sg((t.net_delta || 0) * 20 * 10, 0), '只看 Delta,不含 Gamma', cls(t.net_delta)),
    k('Gamma(Δ/點)', sg(t.gamma, 4), '每動 1 點,Delta 變這麼多口', ''),
    k('Theta($/日)', '$' + sg(t.theta, 0), '每過一個日曆日', cls(t.theta)),
    k('Vega($/IV 1點)', '$' + sg(t.vega, 0), 'IV 升 1 個百分點', cls(t.vega)),
    k('期貨部位', `${sg(st.fut.qty, 0)} ${st.fut.instrument}`, `均價 ${n2(st.fut.avg)} · 未實現 $${sg(st.fut.unreal, 0)}`, ''),
    k('對沖次數', `${st.n_hedges}`, `自動 ${st.n_auto} · 手動 ${st.n_hedges - st.n_auto}`, ''),
    k('成本合計', '$' + n0(st.fees + st.slip_opt + st.slip_fut), `手續費 ${n0(st.fees)} · 滑價 ${n0(st.slip_opt + st.slip_fut)}`, '')].join('');
}
function renderLegs() {
  const st = H.st, el = $('#hgLegs'); if (!st) { el.innerHTML = ''; return; }
  if (!st.legs.length) { el.innerHTML = '<tr><td class="hg-hint">目前沒有選擇權部位</td></tr>'; return; }
  el.innerHTML = '<tr><th>腿</th><th>開倉</th><th>現價</th><th>損益$</th><th>Δ</th><th>Γ</th><th>Θ/日</th><th>Vega</th><th>IV</th><th>剩餘</th><th></th></tr>' +
    st.legs.map((l) => `<tr><td>${l.qty > 0 ? '買' : '賣'} ${Math.abs(l.qty)} ${l.cp === 'C' ? 'Call' : 'Put'} ${l.strike} <span class="hg-hint">${esc(l.expiry)}</span></td>
      <td>${n2(l.open_price)}</td><td>${n2(l.price)}</td><td class="${cls(l.pnl)}">${sg(l.pnl, 0)}</td><td>${sg(l.delta * l.qty, 2)}</td><td>${(l.gamma * l.qty).toFixed(4)}</td>
      <td>${sg(l.theta * l.qty * 20, 0)}</td><td>${sg(l.vega * l.qty * 20, 0)}</td><td>${(l.iv * 100).toFixed(1)}%</td>
      <td>${l.mins_left == null ? '' : l.mins_left >= 120 ? Math.round(l.mins_left / 60) + '時' : l.mins_left + '分'}</td>
      <td><button class="ghost" data-close="${l.id}">平</button></td></tr>`).join('');
}
function renderTrades() {
  const st = H.st, el = $('#hgTrades'); if (!st) { el.innerHTML = ''; return; }
  const rows = [...st.trades].reverse();
  el.innerHTML = '<tr><th>時間</th><th>動作</th><th>內容</th><th>價</th><th>成本$</th></tr>' + rows.map((r) => {
    let what = '';
    if (r.kind === 'fut') what = `${r.units > 0 ? '買' : '賣'} ${Math.abs(r.units)} ${r.instrument}`;
    else what = `${r.qty > 0 ? '買' : '賣'} ${Math.abs(r.qty)} ${r.cp === 'C' ? 'Call' : 'Put'} ${r.strike}`;
    const cost = (r.fee || 0) + (r.slip_cost || 0);
    return `<tr><td>${fmtT(r.t)}</td><td>${esc(r.note || r.kind)}</td><td>${what}</td><td>${n2(r.price)}</td><td>${cost ? n0(cost) : ''}${r.leg_pnl != null ? ` <span class="${cls(r.leg_pnl)}">(${sg(r.leg_pnl, 0)})</span>` : ''}</td></tr>`;
  }).join('') || '<tr><td class="hg-hint">還沒有成交</td></tr>';
}

/* ---------------- 選擇權鏈與下單區 ---------------- */
function renderChain() {
  const ch = H.chain, el = $('#hgChain'); if (!ch) { el.innerHTML = ''; return; }
  const sel = $('#hgExp'), cur = sel.value;
  sel.innerHTML = (ch.expiries || []).map((e) => `<option value="${esc(e.expiry)}">${esc(e.expiry)}(${e.dte}DTE)</option>`).join('');
  sel.value = ch.expiry || cur;
  const ks = ch.strikes || [];
  let ai = 0; ks.forEach((s, i) => { if (Math.abs(s.strike - ch.ref) < Math.abs(ks[ai].strike - ch.ref)) ai = i; });
  const lo = Math.max(0, ai - 10), hi = Math.min(ks.length, ai + 11);
  const cell = (s, cp) => { const x = s[cp]; return x && x.price != null ? `<td class="px" data-add="${s.strike}|${cp}">${n2(x.price)}</td><td>${sg(x.delta, 2)}</td>` : '<td></td><td></td>'; };
  el.innerHTML = '<tr><th>Call 價</th><th>Δ</th><th style="text-align:center">履約價</th><th>Put 價</th><th>Δ</th></tr>' +
    ks.slice(lo, hi).map((s, i) => `<tr class="${lo + i === ai ? 'atm' : ''}">${cell(s, 'C')}<td class="k">${s.strike}</td>${(() => { const x = s.P; return x && x.price != null ? `<td class="px" data-add="${s.strike}|P">${n2(x.price)}</td><td>${sg(x.delta, 2)}</td>` : '<td></td><td></td>'; })()}</tr>`).join('');
}
function strikes() { return (H.chain && H.chain.strikes ? H.chain.strikes.map((s) => s.strike) : []); }
function nearest(target) { const ks = strikes(); if (!ks.length) return null; return ks.reduce((a, b) => (Math.abs(b - target) < Math.abs(a - target) ? b : a)); }
function atm() { return H.chain ? nearest(H.chain.ref) : null; }
const PRESETS = {
  buyC: ['買進 Call', (a, w) => [['C', a, 1]]], sellC: ['賣出 Call', (a, w) => [['C', a, -1]]],
  buyP: ['買進 Put', (a, w) => [['P', a, 1]]], sellP: ['賣出 Put', (a, w) => [['P', a, -1]]],
  longStraddle: ['買跨式 Straddle', (a, w) => [['C', a, 1], ['P', a, 1]]], shortStraddle: ['賣跨式 Straddle', (a, w) => [['C', a, -1], ['P', a, -1]]],
  longStrangle: ['買勒式 Strangle(±間距)', (a, w) => [['C', a + w, 1], ['P', a - w, 1]]], shortStrangle: ['賣勒式 Strangle(±間距)', (a, w) => [['C', a + w, -1], ['P', a - w, -1]]],
  bullCall: ['多頭價差(買 ATM Call、賣 +間距 Call)', (a, w) => [['C', a, 1], ['C', a + w, -1]]], bearPut: ['空頭價差(買 ATM Put、賣 −間距 Put)', (a, w) => [['P', a, 1], ['P', a - w, -1]]],
  bearCall: ['賣出 Call 價差(賣 ATM Call、買 +間距 Call)', (a, w) => [['C', a, -1], ['C', a + w, 1]]], bullPut: ['賣出 Put 價差(賣 ATM Put、買 −間距 Put)', (a, w) => [['P', a, -1], ['P', a - w, 1]]],
  shortFly: ['賣鐵蝴蝶(賣 ATM 跨式、買 ±間距 保險)', (a, w) => [['C', a, -1], ['P', a, -1], ['C', a + w, 1], ['P', a - w, 1]]],
  longFly: ['買鐵蝴蝶', (a, w) => [['C', a, 1], ['P', a, 1], ['C', a + w, -1], ['P', a - w, -1]]],
  shortCondor: ['賣鐵禿鷹(賣 ±間距、買 ±2×間距)', (a, w) => [['C', a + w, -1], ['C', a + 2 * w, 1], ['P', a - w, -1], ['P', a - 2 * w, 1]]],
};
function applyPreset() {
  if (!H.chain) return toast('還沒有選擇權鏈');
  const [, fn] = PRESETS[$('#hgPreset').value], w = Math.max(0, Number($('#hgWidth').value) || 0), q = Math.max(1, Math.min(200, Math.floor(Number($('#hgQty').value) || 1))), a = atm();
  H.builder = fn(a, w).map(([cp, k, side]) => ({ cp, strike: nearest(k), qty: side * q }));
  renderBuilder();
}
function renderBuilder() {
  const el = $('#hgBuilder');
  el.innerHTML = H.builder.map((l, i) => `<div class="hg-leg"><button class="ghost ${l.qty > 0 ? 'buy' : 'sell'}" data-flip="${i}">${l.qty > 0 ? '買' : '賣'}</button>
    <input type="number" data-qty="${i}" value="${Math.abs(l.qty)}" min="1" max="200" style="width:60px"> 口 ${l.cp === 'C' ? 'Call' : 'Put'} ${l.strike}
    <button class="ghost" data-del="${i}">✕</button></div>`).join('');
  let net = 0, ok = true;
  H.builder.forEach((l) => { const row = ((H.chain || {}).strikes || []).find((s) => s.strike === l.strike); const x = row && row[l.cp]; if (!x || x.price == null) ok = false; else net += l.qty * x.price * 20; });
  $('#hgPrev').textContent = H.builder.length ? (ok ? `理論價淨${net >= 0 ? '支出' : '收入'}約 $${n0(Math.abs(net))}(未含半價差與手續費)` : '有腿找不到價格') : '';
}

/* ---------------- 圖 ---------------- */
function mkChart(id, fmt) {
  if (H.charts[id]) return H.charts[id];
  const el = $('#' + id); if (!el || !LW) return null;
  const c = LW.createChart(el, { autoSize: true, layout: { background: { color: '#0b0f16' }, textColor: '#8a93a4', attributionLogo: false }, grid: { vertLines: { color: 'rgba(255,255,255,.04)' }, horzLines: { color: 'rgba(255,255,255,.04)' } },
    rightPriceScale: { borderVisible: false }, timeScale: { timeVisible: true, borderVisible: false, secondsVisible: false, tickMarkFormatter: (t) => fmtT(t) },
    localization: { locale: 'zh-TW', timeFormatter: (t) => fmtT(t), priceFormatter: fmt || ((v) => v.toFixed(0)) } });
  H.charts[id] = { c, series: {} };
  return H.charts[id];
}
function setLine(ch, key, color, data, opts = {}) {
  let s = ch.series[key];
  if (!s) { s = ch.series[key] = ch.c.addSeries(LW.LineSeries, { color, lineWidth: opts.w || 1.5, priceLineVisible: false, lastValueVisible: !!opts.last, ...opts }); }
  s.setData(data); return s;
}
function renderCharts() {
  const st = H.st; if (!st || !LW) return;
  $('#hgLegend').innerHTML = COMP.map(([k, n, col]) => `<button class="${H.hidden[k] ? '' : 'on'}" data-leg="${k}"><i style="background:${col}"></i>${n}</button>`).join('');
  const pts = []; let last = -1; for (const p of st.points) if (p.t > last) { pts.push(p); last = p.t; }
  const eq = mkChart('hgEq'); if (eq) {
    for (const [k, , col] of COMP) {
      const s = setLine(eq, k, col, pts.map((p) => ({ time: p.t, value: (p.comp || {})[k] ?? 0 })), { w: k === 'total' ? 2.4 : 1.2, last: k === 'total' });
      s.applyOptions({ visible: !H.hidden[k] });
    }
    if (pts.length && !renderCharts.fit) { eq.c.timeScale().fitContent(); renderCharts.fit = 1; }
  }
  const dl = mkChart('hgDl', (v) => v.toFixed(1)); if (dl) {
    setLine(dl, 'nd', '#7d9bd9', pts.map((p) => ({ time: p.t, value: p.net_delta })), { w: 1.8, last: true });
    setLine(dl, 'od', '#8a93a4', pts.map((p) => ({ time: p.t, value: p.opt_delta })), { w: 1 });
    const p = st.params; dl.lines = dl.lines || [];
    dl.lines.forEach((l) => dl.series.nd.removePriceLine(l)); dl.lines = [];
    if (p.auto) for (const v of [p.target + p.threshold, p.target - p.threshold]) dl.lines.push(dl.series.nd.createPriceLine({ price: v, color: '#e7a44c', lineStyle: 2, lineWidth: 1, axisLabelVisible: false }));
  }
}

/* ---------------- 動作 ---------------- */
async function act(action, body, quiet) {
  const r = await guarded(() => post(action, { id: H.id, ...body })); if (!r) return null;
  if (r.note && !quiet) toast(r.note);
  renderCharts.fit = 0; await refresh(true); return r;
}
function collectParams() {
  return { auto: $('#hgAuto').checked, threshold: Number($('#hgThr').value), target: Number($('#hgTgt').value), cooldown_min: Number($('#hgCool').value),
    fut_mult: Number($('#hgInst').value), opt_half_spread: Number($('#hgHs').value), opt_fee: Number($('#hgOptFee').value),
    fut_fee_nq: Number($('#hgNqFee').value), fut_fee_mnq: Number($('#hgMnqFee').value), fut_slip: Number($('#hgSlip').value) };
}
async function step(mins, untilClose) {
  if (!H.id || H.busy) return;
  const r = await guarded(() => post('step', { id: H.id, minutes: mins, until_close: !!untilClose })); if (!r) return stopPlay();
  if (r.advanced === 0) { stopPlay(); toast(r.note || '到資料盡頭了'); }
  await refresh(false);
}
function stopPlay() { H.playing = false; $('#hgPlay').textContent = '▶ 播放'; }
function togglePlay() {
  if (H.playing) return stopPlay();
  H.playing = true; $('#hgPlay').textContent = '⏸ 暫停';
}
let tickN = 0;
function tick() {
  if (!H.id || !H.st) return;
  tickN += 1;
  if (H.st.mode === 'live') { if (tickN % 10 === 0) refresh(false); }       // 即時帳戶每 10 秒補算一次(資料每分鐘才更新)
  else if (H.playing && !H.stepping) { H.stepping = true; step(Number($('#hgSpeed').value) || 5).finally(() => { H.stepping = false; }); }
}

async function compare() {
  const st = H.st; if (!st) return;
  let legs;
  if ($('#hgCmpSrc').value === 'open') legs = st.legs.map((l) => ({ expiry: l.expiry, strike: l.strike, cp: l.cp, qty: l.qty }));
  else legs = H.builder.map((l) => ({ expiry: H.chain.expiry, strike: l.strike, cp: l.cp, qty: l.qty }));
  if (!legs.length) return toast('沒有可比較的部位(先下單,或在下單區組好腿)');
  const mins = Number($('#hgCmpMin').value);
  const cfgs = [{ label: '不對沖', auto: false }, ...[0.25, 0.5, 1, 2].map((t) => ({ label: `自動 ±${t}`, auto: true, threshold: t }))];
  $('#hgCmpOut').textContent = '計算中…';
  const r = await guarded(() => post('compare', { start_t: st.clock_t, end_t: st.clock_t + mins * 60, legs, configs: cfgs, params: collectParams() }));
  if (!r) { $('#hgCmpOut').textContent = '比較失敗'; return; }
  const rs = r.results;
  $('#hgCmpOut').innerHTML = '<table class="hg-t"><tr><th>設定</th><th>總損益$</th><th>對沖次數</th><th>成本$</th><th>每分損益波動$</th><th>最大回撤$</th></tr>' +
    rs.map((x) => `<tr><td>${esc(x.label)}</td><td class="${cls(x.pnl)}">${sg(x.pnl, 0)}</td><td>${x.n_hedges}</td><td>${n0(x.fees + x.slip)}</td><td>${n0(x.step_sd)}</td><td>${n0(x.max_drawdown)}</td></tr>`).join('') +
    `</table><div class="hg-hint">這段區間只有一條價格路徑,結果是「這一次」的比較,不是統計結論。波動越小 = 越接近 Delta 中性,但手續費與滑價越高;損益高低跟運氣(那段行情)關係很大。</div>`;
  $('#hgCmpChart').hidden = false;
  const ch = mkChart('hgCmpChart'); if (!ch) return;
  const cols = ['#8a93a4', '#7d9bd9', '#39d98a', '#e7a44c', '#d58cff'];
  for (const k of Object.keys(ch.series)) { ch.c.removeSeries(ch.series[k]); delete ch.series[k]; }
  rs.forEach((x, i) => { const d = []; let l = -1; for (const [t, v] of x.curve) if (t > l) { d.push({ time: t, value: v }); l = t; } setLine(ch, 'c' + i, cols[i % 5], d, { w: 1.6 }); });
  ch.c.timeScale().fitContent();
}

/* ---------------- 事件綁定 ---------------- */
let wired = false;
function wire() {
  if (wired) return; wired = true;
  $('#hgPreset').innerHTML = Object.entries(PRESETS).map(([k, v]) => `<option value="${k}">${v[0]}</option>`).join('');
  $('#hgPreset').value = 'longStraddle';
  $('#hgAcct').onchange = async (e) => { H.id = Number(e.target.value) || null; remember(); stopPlay(); renderCharts.fit = 0; await refresh(true); };
  $('#hgNew').onclick = newAcct;
  $('#hgExp').onchange = async () => { if (H.id) { H.chain = await guarded(() => get('hedge_chain', { id: H.id, expiry: $('#hgExp').value })); render(); } };
  $('#hgApply').onclick = applyPreset;
  $('#hgClear').onclick = () => { H.builder = []; renderBuilder(); };
  $('#hgChain').onclick = (e) => { const td = e.target.closest('[data-add]'); if (!td) return; const [k, cp] = td.dataset.add.split('|'); H.builder.push({ cp, strike: Number(k), qty: 1 }); renderBuilder(); };
  $('#hgBuilder').onclick = (e) => { const f = e.target.closest('[data-flip]'), d = e.target.closest('[data-del]');
    if (f) { H.builder[+f.dataset.flip].qty *= -1; renderBuilder(); } if (d) { H.builder.splice(+d.dataset.del, 1); renderBuilder(); } };
  $('#hgBuilder').onchange = (e) => { const q = e.target.closest('[data-qty]'); if (!q) return; const i = +q.dataset.qty, sgn = H.builder[i].qty < 0 ? -1 : 1; H.builder[i].qty = sgn * Math.max(1, Math.min(200, Math.floor(Number(q.value) || 1))); renderBuilder(); };
  $('#hgSubmit').onclick = async () => {
    if (!H.builder.length) return toast('先在上面選腿或套用組合');
    const legs = H.builder.map((l) => ({ expiry: H.chain.expiry, strike: l.strike, cp: l.cp, qty: l.qty }));
    if (await act('open', { legs })) { H.builder = []; renderBuilder(); }
  };
  $('#hgLegs').onclick = (e) => { const b = e.target.closest('[data-close]'); if (b) act('close', { leg_ids: [Number(b.dataset.close)] }); };
  $('#hgSaveP').onclick = () => act('params', { params: collectParams() });
  $('#hgAuto').onchange = () => act('params', { params: collectParams() });
  $('#hgInst').onchange = () => act('params', { params: collectParams() });
  $('#hgHedge').onclick = async () => { await act('params', { params: collectParams() }, true); await act('hedge', {}); };
  $('#hgFlat').onclick = () => { if (H.st && H.st.fut.qty) act('hedge', { units: -H.st.fut.qty }); };
  $('#hgPlay').onclick = togglePlay;
  document.querySelectorAll('[data-hgstep]').forEach((b) => { b.onclick = () => step(Number(b.dataset.hgstep)); });
  $('#hgToClose').onclick = () => step(1, true);
  $('#hgReset').onclick = () => { if (H.id && confirm('清掉這個帳戶的持倉、成交與曲線,回到起點?')) { stopPlay(); act('reset', {}); } };
  $('#hgDel').onclick = async () => { if (H.id && confirm('刪除這個帳戶?')) { stopPlay(); const id = H.id; H.id = null; await guarded(() => post('delete', { id })); await loadAccts(false); } };
  $('#hgLegend').onclick = (e) => { const b = e.target.closest('[data-leg]'); if (b) { H.hidden[b.dataset.leg] = !H.hidden[b.dataset.leg]; renderCharts(); } };
  $('#hgCmp').onclick = compare;
}

window.hedgeTab = (on) => {
  if (!on) { stopPlay(); clearInterval(H.timer); H.timer = null; return; }
  wire();
  guarded(() => loadAccts(true));
  clearInterval(H.timer); H.timer = setInterval(tick, 1000);
  setTimeout(() => { for (const k of Object.keys(H.charts)) H.charts[k].c.timeScale().fitContent(); }, 300);
};
})();
