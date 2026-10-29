/* NQ GEX monitor -- front end.  No build step; talks to gex_web_server.py (/api/...).
   Charts: TradingView Lightweight Charts (Apache-2.0, see LWC-LICENSE.txt).  The heat map, side profile and cursor are
   drawn by two small "series primitives" on the same canvas as the candles, so they zoom and pan with them. */
(() => {
'use strict';
const LW = window.LightweightCharts;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const SRC_COL = { NQ: '#7d9bd9', QQQ: '#39d98a', NDX: '#e7a44c', SUM: '#d58cff', WGT: '#ff7ad9' };
const SRC_ORDER = ['NQ', 'QQQ', 'NDX', 'WGT'];
const sn = (s) => (s === 'WGT' ? '加權合計' : s);
const UP = '#2bc08b', DN = '#ff5c6c';
const KINDS = [['call', 'Call 牆'], ['put', 'Put 牆'], ['flip', 'Flip']];
const SCOPE_KEY = { all: 'ALL', ex0: 'EX0', zero: 'ZERO' };

/* ------------------------------ settings ------------------------------ */
const DEF = { hours: 8, day: null, tz: 'Asia/Taipei', srcs: ['NQ', 'QQQ', 'NDX'], basis: 'oi', scope: 'all', lens: 'abs',
  tf: 1, bucket: 25, heat: true, lines: true, profile: true, sub: true, alpha: 85, compress: 70, follow: true, candle: 'mono',
  heatMode: 'strike', heatSrc: 'ALL', band: true, bandW: 20, bandA: 45, wgt: { NQ: 20, QQQ: 35, NDX: 45 }, wver: 2, wnorm: true, mmarks: true, mscene: true, tbOpen: false, subH: 0, subFold: false, events: true, evTypes: { surge: true, touch: true, cross: true }, evSeen: 0,
  pal: { name: '預設', low: '#3752cd', mid: '#1c96c3', high: '#ffbc48', pos: '#2bc08b', neg: '#ff5c6c' },
  gridH: false, gridV: false, cross: 'normal', pmode: 0, auto: true, pline: true, llabels: true, stay: false, lw: 3, roff: 8, nosb: false,
  subMode: 'net', sqrib: false, emband: true, anly: 'pcr', anlyPcrBasis: 'vol', anlySkewSrc: 'NQ', anlyDivBasis: 'oi', anlyDivScope: 'all',
  flmark: true, feKinds: { sweep: true, stop: true, absorb: true, iceberg: true, exhaust: true }, sets: false, mmini: true, lineCol: { NQ: '#7d9bd9', QQQ: '#39d98a', NDX: '#e7a44c', WGT: '#ff7ad9' } };
const S = { ...DEF };
try { if (/[?&]reset\b/.test(location.search)) localStorage.removeItem('gexweb.v1'); } catch (e) { /* ignore */ }
try { const o = JSON.parse(localStorage.getItem('gexweb.v1') || '{}'); Object.assign(S, o); S.pal = { ...DEF.pal, ...(o.pal || {}) }; S.evTypes = { ...DEF.evTypes, ...(o.evTypes || {}) }; S.feKinds = { ...DEF.feKinds, ...(o.feKinds || {}) }; S.wgt = { ...DEF.wgt, ...(o.wgt || {}) }; S.lineCol = { ...DEF.lineCol, ...(o.lineCol || {}) }; if ((o.wver || 0) < 2) { S.wgt = { ...DEF.wgt }; S.wver = 2; } if (S.subMode !== 'net' && S.subMode !== 'sq') S.subMode = 'net'; if (!['pcr', 'skew', 'div'].includes(S.anly)) S.anly = 'pcr'; if (!['vol', 'oi'].includes(S.anlyPcrBasis)) S.anlyPcrBasis = 'vol'; if (!['NQ', 'QQQ', 'NDX'].includes(S.anlySkewSrc)) S.anlySkewSrc = 'NQ'; if (!['oi', 'vol'].includes(S.anlyDivBasis)) S.anlyDivBasis = 'oi'; if (!['all', 'ex0', 'zero'].includes(S.anlyDivScope)) S.anlyDivScope = 'all'; } catch (e) { /* storage may be blocked */ }
const save = () => { try { localStorage.setItem('gexweb.v1', JSON.stringify(S)); } catch (e) { /* ignore */ } };

/* ------------------------------ state ------------------------------ */
const D = { kind: 'candles', bars: [], barT: [], cols: [], vmax: {}, levels: {}, net3: null, status: null, days: [], last: null, lastLatest: null, token: 0, lvPts: {}, heatTok: 0, events: [], srvEvents: [], book: null, sq: null, sqTok: 0, band: null, bandUpPts: [], bandDnPts: [], band0: null, band0UpPts: [], band0DnPts: [], pcr: null, skew: null, div: null, flow: null, flowTs: null, anlyTs: null, thesis: null, thesisTs: null };
const R = { on: false, idx: 0, timer: null };   // replay
let hoverIdx = -1;                               // heat column under the mouse

/* ------------------------------ helpers ------------------------------ */
const nf = (d) => new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const f2 = nf(2), f0 = nf(0);
const fP = (p) => (p == null ? '—' : f2.format(p));
const fSigned = (v, d = 0) => (v == null ? '—' : (v >= 0 ? '+' : '−') + nf(d).format(Math.abs(v)));
const fM = (v) => {   // $M per 1 % move
  if (v == null) return '—';
  const a = Math.abs(v), s = v < 0 ? '−' : '+';
  return a >= 1000 ? `${s}${(a / 1000).toFixed(2)}B` : `${s}${a.toFixed(a >= 100 ? 0 : 1)}M`;
};
const dtfCache = {};
function dtf(key, opts) {
  const k = key + S.tz;
  return dtfCache[k] || (dtfCache[k] = new Intl.DateTimeFormat('en-GB', { timeZone: S.tz, hour12: false, ...opts }));
}
const hm = (t) => dtf('hm', { hour: '2-digit', minute: '2-digit' }).format(new Date(t * 1000));
const md = (t) => dtf('md', { month: '2-digit', day: '2-digit' }).format(new Date(t * 1000)).replace(/^(\d+)\/(\d+)$/, '$2/$1');
const mdhm = (t) => `${md(t)} ${hm(t)}`;
const hmIn = (t, tz) => new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour12: false, hour: '2-digit', minute: '2-digit' }).format(new Date(t * 1000));
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => { t.hidden = true; }, 5000);
}
async function api(name, params = {}) {
  const q = new URLSearchParams(params).toString();
  const r = await fetch(`/api/${name}?${q}`, { cache: 'no-store' });
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || r.status);
  return j;
}

/* ------------------------------ error reporting ------------------------------ */
const escHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const CLIENT_ERRORS = [];
let DSRV_ERRORS = [];
let errSeen = Math.floor(Date.now() / 1000);
function recordClientError(where, msg) {
  const text = String((msg && msg.message) || msg || '未知錯誤');
  CLIENT_ERRORS.push({ t: Math.floor(Date.now() / 1000), path: where, error: text.slice(0, 400), client: true });
  if (CLIENT_ERRORS.length > 50) CLIENT_ERRORS.shift();
  updateErrBadge();
}
window.addEventListener('error', (e) => recordClientError('前端', e.message));
window.addEventListener('unhandledrejection', (e) => recordClientError('前端(Promise)', e.reason));
function allErrors() {
  return [...DSRV_ERRORS.map((x) => ({ ...x, client: false })), ...CLIENT_ERRORS.map((x) => ({ ...x, client: true }))]
    .sort((a, b) => b.t - a.t);
}
function updateErrBadge() {
  const b = $('#errbadge'); if (!b) return;
  const n = allErrors().filter((x) => x.t > errSeen).length;
  b.hidden = n === 0; b.textContent = n > 99 ? '99+' : n;
}
function renderErrors() {
  const box = $('#errlist'); if (!box) return;
  const list = allErrors();
  box.innerHTML = list.length ? list.map((x) =>
    `<div class="errit"><div class="ett">${hm(x.t)} · ${x.client ? '前端' : '伺服器'} · <b>${escHtml(x.path)}</b></div><div class="emsg">${escHtml(x.error)}</div></div>`
  ).join('') : '<div class="sub" style="padding:14px">目前沒有錯誤紀錄。出問題時會自動記在這裡。</div>';
}
async function loadErrors() {
  try {
    const r = await api('errors', {});
    DSRV_ERRORS = (r.errors || []).slice().reverse();
  } catch (e) { /* ignore: 錯誤端點本身失敗就不用再往上報 */ }
  renderErrors(); updateErrBadge();
}
function lowerIdx(arr, t) {   // index of the last element <= t, or -1
  let lo = 0, hi = arr.length - 1, ans = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (arr[m] <= t) { ans = m; lo = m + 1; } else hi = m - 1; }
  return ans;
}
function snapIdx(t) {   // candle index a snapshot time belongs to (-1 = no candle close enough)
  const i = lowerIdx(D.barT, t);
  if (i < 0) return -1;
  return t - D.barT[i] > Math.max(S.tf, 1) * 60 * 4 && i === D.barT.length - 1 ? -1 : i;
}
const debounce = (fn, ms) => { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; };

/* ------------------------------ colour maps ------------------------------ */
function makeLUT(stops) {   // stops: [[pos, r, g, b, a], ...] -> 256 css colours
  const out = [];
  for (let i = 0; i < 256; i++) {
    const p = i / 255; let k = 0;
    while (k < stops.length - 2 && p > stops[k + 1][0]) k++;
    const [p0, r0, g0, b0, a0] = stops[k], [p1, r1, g1, b1, a1] = stops[k + 1];
    const u = Math.min(1, Math.max(0, (p - p0) / (p1 - p0 || 1)));
    out.push(`rgba(${Math.round(r0 + (r1 - r0) * u)},${Math.round(g0 + (g1 - g0) * u)},${Math.round(b0 + (b1 - b0) * u)},${(a0 + (a1 - a0) * u).toFixed(3)})`);
  }
  return out;
}
const hexRGB = (h) => { const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(h || ''); return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [128, 128, 128]; };
const lighten = (c, k) => c.map((v) => Math.round(v + (255 - v) * k));
const rgba = (h, a) => { const c = hexRGB(h); return `rgba(${c[0]},${c[1]},${c[2]},${a})`; };
const LUT = {};
function buildLUTs() {
  const lo = hexRGB(S.pal.low), mi = hexRGB(S.pal.mid), hi = hexRGB(S.pal.high), po = hexRGB(S.pal.pos), ne = hexRGB(S.pal.neg);
  LUT.abs = makeLUT([[0, ...lo, 0.0], [0.2, ...lo, 0.24], [0.55, ...mi, 0.48], [1, ...hi, 0.84]]);
  LUT.call = makeLUT([[0, ...po, 0.0], [0.3, ...po, 0.30], [0.7, ...po, 0.56], [1, ...lighten(po, 0.35), 0.82]]);
  LUT.put = makeLUT([[0, ...ne, 0.0], [0.3, ...ne, 0.32], [0.7, ...ne, 0.58], [1, ...lighten(ne, 0.35), 0.84]]);
  LUT.netPos = LUT.call; LUT.netNeg = LUT.put;
}
buildLUTs();
const PRESETS = {
  '預設': { low: '#3752cd', mid: '#1c96c3', high: '#ffbc48', pos: '#2bc08b', neg: '#ff5c6c' },
  '熱力(紫→紅→黃)': { low: '#4b1d91', mid: '#d8325a', high: '#ffd35c', pos: '#e7a44c', neg: '#7a5cff' },
  '冷色(藍→青→白)': { low: '#12306b', mid: '#2a7fbf', high: '#aef0ff', pos: '#5de0e6', neg: '#8f7bff' },
  '色盲友善(藍/橘)': { low: '#2b3a8f', mid: '#6a9fd8', high: '#f2a541', pos: '#f2a541', neg: '#4a90e2' },
  '黑白灰階': { low: '#2a2f3a', mid: '#8a93a6', high: '#ffffff', pos: '#e8ecf5', neg: '#6c7488' },
};

/* ------------------------------ charts ------------------------------ */
const roffEff = () => (S.mscene ? Math.max(S.roff, Math.ceil(30 / Math.max(S.tf, 1)) + 2) : S.roff);   // 導師情境要在最新 K 線右邊留 30 分鐘的位置
const baseOpts = () => ({
  autoSize: true,
  layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#8a93a4', fontFamily: 'ui-monospace,"SF Mono",Menlo,monospace', fontSize: 11, attributionLogo: true },
  grid: { vertLines: { visible: S.gridV, color: 'rgba(255,255,255,.04)' }, horzLines: { visible: S.gridH, color: 'rgba(255,255,255,.045)' } },
  rightPriceScale: { borderVisible: false, minimumWidth: 78, scaleMargins: { top: 0.06, bottom: 0.08 } },
  timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: roffEff(), rightBarStaysOnScroll: S.stay, barSpacing: 7, minBarSpacing: 0.4,
    tickMarkFormatter: (t, type) => (type <= 2 ? md(t) : hm(t)) },
  localization: { timeFormatter: (t) => mdhm(t), priceFormatter: (p) => f2.format(p) },
  crosshair: { mode: LW.CrosshairMode.Normal,
    vertLine: { color: 'rgba(255,255,255,.28)', style: 3, labelBackgroundColor: '#27344d' },
    horzLine: { color: 'rgba(255,255,255,.28)', style: 3, labelBackgroundColor: '#27344d' } },
  handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
  handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
});
const chart = LW.createChart($('#chart'), baseOpts());
const subChart = LW.createChart($('#subchart'), { ...baseOpts(), layout: { ...baseOpts().layout, attributionLogo: false }, localization: { timeFormatter: (t) => mdhm(t), priceFormatter: (v) => fM(v).replace('+', '') }, rightPriceScale: { borderVisible: false, minimumWidth: 78, scaleMargins: { top: 0.18, bottom: 0.12 } },
  timeScale: { ...baseOpts().timeScale, visible: false }, handleScale: { mouseWheel: false, pinch: false, axisPressedMouseMove: { time: false, price: true }, axisDoubleClickReset: { time: false, price: true } }, handleScroll: false, crosshair: { ...baseOpts().crosshair, horzLine: { visible: false, labelVisible: false } } });

const CANDLE = {
  mono: { upColor: '#eef2fb', downColor: '#0b0f16', borderUpColor: '#eef2fb', borderDownColor: '#c3cde2', wickUpColor: '#c3cde2', wickDownColor: '#c3cde2' },
  rg: { upColor: UP, downColor: DN, borderUpColor: UP, borderDownColor: DN, wickUpColor: UP, wickDownColor: DN },
};
const candle = chart.addSeries(LW.CandlestickSeries, { ...CANDLE[S.candle in CANDLE ? S.candle : 'mono'], priceLineColor: '#aab4c8', priceLineStyle: 2, lastValueVisible: true });
function applyCandleStyle() { if (D.kind === 'candles') candle.applyOptions(CANDLE[S.candle in CANDLE ? S.candle : 'mono']); }
const tickLine = chart.addSeries(LW.LineSeries, { color: '#9db4ff', lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
const netSeries = subChart.addSeries(LW.BaselineSeries, {
  baseValue: { type: 'price', price: 0 },
  topLineColor: UP, topFillColor1: 'rgba(38,201,139,.30)', topFillColor2: 'rgba(38,201,139,.02)',
  bottomLineColor: DN, bottomFillColor1: 'rgba(255,92,108,.02)', bottomFillColor2: 'rgba(255,92,108,.30)',
  lineWidth: 2, lastValueVisible: true, priceLineVisible: false,
  priceFormat: { type: 'custom', formatter: (v) => fM(v).replace('+', ''), minMove: 0.01 },
});

/* ---- orderflow 動能副圖:線=累積 Delta(綠正/紅負)· 柱=每分鐘主動買賣淨量(綠買/紅賣) ---- */
const flowCum = subChart.addSeries(LW.BaselineSeries, {
  baseValue: { type: 'price', price: 0 },
  topLineColor: UP, topFillColor1: 'rgba(38,201,139,.22)', topFillColor2: 'rgba(38,201,139,.02)',
  bottomLineColor: DN, bottomFillColor1: 'rgba(255,92,108,.02)', bottomFillColor2: 'rgba(255,92,108,.22)',
  lineWidth: 2, visible: false, lastValueVisible: true, priceLineVisible: false, title: '累積Δ',
  priceFormat: { type: 'custom', formatter: (v) => f0.format(v) + ' 口', minMove: 1 },
});
const spdSeries = subChart.addSeries(LW.HistogramSeries, { visible: false, priceScaleId: 'spd', lastValueVisible: false, priceLineVisible: false,
  priceFormat: { type: 'custom', formatter: (v) => f0.format(v) + ' 口/分', minMove: 1 } });
subChart.priceScale('spd').applyOptions({ scaleMargins: { top: 0.58, bottom: 0.03 }, borderVisible: false, visible: false });
const netZero = subChart.addSeries(LW.LineSeries, { color: '#e7a44c', lineWidth: 2, visible: false, lastValueVisible: true, priceLineVisible: false, title: '0DTE',
  priceFormat: { type: 'custom', formatter: (v) => fM(v).replace('+', ''), minMove: 0.01 } });
const netEx0 = subChart.addSeries(LW.LineSeries, { color: '#7d9bd9', lineWidth: 2, visible: false, lastValueVisible: true, priceLineVisible: false, title: '排除0DTE',
  priceFormat: { type: 'custom', formatter: (v) => fM(v).replace('+', ''), minMove: 0.01 } });
const subSeries = () => (S.subMode === 'sq' ? flowCum : netSeries);
const ORANGE = '#e7a44c';

/* ---- ±1σ expected-move band (two dashed amber lines + a faint fill channel behind the candles) ---- */
const bandUp = chart.addSeries(LW.LineSeries, { color: '#e7a44c', lineWidth: 1, lineStyle: 2, lineType: LW.LineType.WithSteps, visible: S.emband, lastValueVisible: S.emband, priceLineVisible: false, crosshairMarkerVisible: false, title: '±1σ 上沿', autoscaleInfoProvider: () => null,
  priceFormat: { type: 'custom', formatter: (v) => fP(v), minMove: 0.25 } });
const bandDn = chart.addSeries(LW.LineSeries, { color: '#e7a44c', lineWidth: 1, lineStyle: 2, lineType: LW.LineType.WithSteps, visible: S.emband, lastValueVisible: S.emband, priceLineVisible: false, crosshairMarkerVisible: false, title: '±1σ 下沿', autoscaleInfoProvider: () => null,
  priceFormat: { type: 'custom', formatter: (v) => fP(v), minMove: 0.25 } });
/* ---- 0DTE intraday band (two dashed teal lines; tighter and shrinking into the close) ---- */
const band0Up = chart.addSeries(LW.LineSeries, { color: '#5eead4', lineWidth: 1, lineStyle: 2, lineType: LW.LineType.WithSteps, visible: S.emband, lastValueVisible: S.emband, priceLineVisible: false, crosshairMarkerVisible: false, title: '0DTE 上沿', autoscaleInfoProvider: () => null,
  priceFormat: { type: 'custom', formatter: (v) => fP(v), minMove: 0.25 } });
const band0Dn = chart.addSeries(LW.LineSeries, { color: '#5eead4', lineWidth: 1, lineStyle: 2, lineType: LW.LineType.WithSteps, visible: S.emband, lastValueVisible: S.emband, priceLineVisible: false, crosshairMarkerVisible: false, title: '0DTE 下沿', autoscaleInfoProvider: () => null,
  priceFormat: { type: 'custom', formatter: (v) => fP(v), minMove: 0.25 } });
class EmbBandPrim {
  attached(p) { this.series = p.series; this.chart = p.chart; this.req = p.requestUpdate; }
  detached() {}
  updateAllViews() {}
  redraw() { if (this.req) this.req(); }
  paneViews() { return this._v || (this._v = [{ zOrder: () => 'bottom', renderer: () => ({ draw: (t) => this.draw(t) }) }]); }
  draw(target) {
    target.useBitmapCoordinateSpace((sc) => {
      if (!S.emband) return;
      const up = D.bandUpPts, dn = D.bandDnPts;
      if (!up || up.length < 2) return;
      const ctx = sc.context, hr = sc.horizontalPixelRatio, vr = sc.verticalPixelRatio, ts = this.chart.timeScale(), ser = this.series, W = sc.bitmapSize.width;
      for (let i = 0; i < up.length; i++) {
        const a = up[i]; if (a.value == null) continue;
        const b = dn[i]; if (!b || b.value == null) continue;
        let j = i + 1;
        while (j < up.length && (up[j].value == null || dn[j].value == null)) j++;
        const tEnd = j < up.length ? up[j].time : D.barT[D.barT.length - 1];
        const x0 = ts.timeToCoordinate(a.time), x1 = ts.timeToCoordinate(tEnd);
        const yU = ser.priceToCoordinate(a.value), yD = ser.priceToCoordinate(b.value);
        if (x0 == null || x1 == null || yU == null || yD == null || x1 <= x0) continue;
        if (x1 < -2 || x0 > W / hr + 2) continue;
        const top = Math.round(Math.min(yU, yD) * vr), h = Math.max(1, Math.round(Math.abs(yD - yU) * vr));
        ctx.fillStyle = 'rgba(231,164,76,.05)';
        ctx.fillRect(Math.round(x0 * hr), top, Math.max(1, Math.round((x1 - x0) * hr)), h);
      }
    });
  }
}
const embandPrim = new EmbBandPrim(); candle.attachPrimitive(embandPrim);

/* ---- analysis page charts (P/C ratio, IV skew, three-source divergence) ---- */
const anlyOpts = (pf) => ({
  autoSize: true,
  layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#8a93a4', fontFamily: 'ui-monospace,"SF Mono",Menlo,monospace', fontSize: 12, attributionLogo: false },
  grid: { vertLines: { visible: false, color: 'rgba(255,255,255,.04)' }, horzLines: { visible: true, color: 'rgba(255,255,255,.045)' } },
  rightPriceScale: { borderVisible: false, minimumWidth: 96, scaleMargins: { top: 0.08, bottom: 0.1 } },
  timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 6, rightBarStaysOnScroll: false, barSpacing: 8, minBarSpacing: 0.5,
    tickMarkFormatter: (t, type) => (type <= 2 ? md(t) : hm(t)) },
  localization: { timeFormatter: (t) => mdhm(t), priceFormatter: pf },
  crosshair: { mode: LW.CrosshairMode.Normal,
    vertLine: { color: 'rgba(255,255,255,.28)', style: 3, labelBackgroundColor: '#27344d' },
    horzLine: { color: 'rgba(255,255,255,.28)', style: 3, labelBackgroundColor: '#27344d' } },
  handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
  handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
});
const pcrChart = LW.createChart($('#pcrChart'), anlyOpts((v) => v.toFixed(2) + '×'));
const pcrAll = pcrChart.addSeries(LW.LineSeries, { color: '#7d9bd9', lineWidth: 2, lastValueVisible: true, priceLineVisible: false, title: '全部到期' });
const pcr0 = pcrChart.addSeries(LW.LineSeries, { color: '#e7a44c', lineWidth: 2, lineStyle: 2, lastValueVisible: true, priceLineVisible: false, title: '0DTE' });
pcrAll.createPriceLine({ price: 1, color: 'rgba(255,255,255,.25)', lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: 'P = C' });
const skewChart = LW.createChart($('#skewChart'), anlyOpts((v) => (v >= 0 ? '+' : '') + v.toFixed(1) + ' vol'));
const skewLine = skewChart.addSeries(LW.BaselineSeries, {
  baseValue: { type: 'price', price: 0 }, topLineColor: '#e7a44c', topFillColor1: 'rgba(231,164,76,.22)', topFillColor2: 'rgba(231,164,76,.02)',
  bottomLineColor: '#7d9bd9', bottomFillColor1: 'rgba(125,155,217,.02)', bottomFillColor2: 'rgba(125,155,217,.22)',
  lineWidth: 2, lastValueVisible: true, priceLineVisible: false, title: '25Δ 偏斜', priceFormat: { type: 'custom', formatter: (v) => (v >= 0 ? '+' : '') + v.toFixed(1) + ' vol', minMove: 0.1 } });
const divChart = LW.createChart($('#divChart'), anlyOpts((v) => fP(v)));
const divSpreadChart = LW.createChart($('#divSpreadChart'), anlyOpts((v) => f0.format(v) + ' 點'));
const divFlip = {};
for (const src of ['NQ', 'QQQ', 'NDX']) {
  divFlip[src] = divChart.addSeries(LW.LineSeries, { color: SRC_COL[src], lineWidth: 2, lineType: LW.LineType.WithSteps, lastValueVisible: true, priceLineVisible: false, title: src, priceFormat: { type: 'custom', formatter: (v) => fP(v), minMove: 0.5 } });
}
const divSpread = divSpreadChart.addSeries(LW.LineSeries, { color: '#e7a44c', lineWidth: 2, lineType: LW.LineType.WithSteps, lastValueVisible: true, priceLineVisible: false, title: '分歧度', priceFormat: { type: 'custom', formatter: (v) => f0.format(v) + ' 點', minMove: 0.5 } });

/* keep the two time axes locked together (both are built on the same candle grid) */
let syncing = false;
chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
  if (!r || syncing) return; syncing = true;
  try { subChart.timeScale().setVisibleLogicalRange(r); } finally { syncing = false; }
});

/* ---- heat map primitive (behind the candles) ---- */
function lensValue(col, j, lens) {
  const c = col.c[j], p = col.p[j];
  return lens === 'abs' ? c - p : lens === 'net' ? c + p : lens === 'call' ? c : -p;
}
function modeNames() {
  const m = S.heatMode, src = S.heatSrc === 'ALL' ? '合計' : S.heatSrc;
  if (m === 'profile') return { c: '正 GEX', p: '負 GEX', net: '淨 GEX(價格到此時)', abs: '壓力強度', title: `GEX 壓力 ${src}`, unit: '$ / 1%' };
  if (m === 'charm') return { c: 'Call 端', p: 'Put 端', net: '合計 Charm', abs: '強度', title: `Charm ${src}`, unit: 'Δ$ / 日' };
  return { c: 'Call', p: 'Put', net: 'Net', abs: '集中度', title: `履約價集中度 ${src}`, unit: '$ / 1%' };
}
function computeVmax() {
  for (const lens of ['abs', 'net', 'call', 'put']) {
    const vals = [];
    for (const col of D.cols) for (let j = 0; j < col.bins.length; j++) { const v = Math.abs(lensValue(col, j, lens)); if (v > 0) vals.push(v); }
    vals.sort((a, b) => a - b);
    D.vmax[lens] = vals.length ? vals[Math.min(vals.length - 1, Math.floor(vals.length * 0.985))] || 1 : 1;
  }
}
class HeatPrim {
  attached(p) { this.series = p.series; this.chart = p.chart; this.req = p.requestUpdate; }
  detached() {}
  updateAllViews() {}
  redraw() { if (this.req) this.req(); }
  paneViews() { return this._v || (this._v = [{ zOrder: () => 'bottom', renderer: () => ({ draw: (t) => this.draw(t) }) }]); }
  draw(target) {
    target.useBitmapCoordinateSpace((sc) => {
      const cols = D.cols; if (!cols.length || !D.barT.length) return;
      const ctx = sc.context, hr = sc.horizontalPixelRatio, vr = sc.verticalPixelRatio, W = sc.bitmapSize.width;
      const ts = this.chart.timeScale(), ser = this.series;
      const mid = Math.max(0, Math.min(D.barT.length - 2, D.barT.length >> 1));
      const xa = ts.timeToCoordinate(D.barT[mid]), xb = ts.timeToCoordinate(D.barT[mid + 1]);
      const barPx = xa != null && xb != null && xb > xa ? xb - xa : 6;
      const bucket = D.bucket || S.bucket, lens = S.lens, lut = lens === 'net' ? null : LUT[lens], vmax = D.vmax[lens] || 1;
      const ycache = new Map();
      const yOf = (price) => {
        let y = ycache.get(price);
        if (!y) { const a = ser.priceToCoordinate(price + bucket / 2), b = ser.priceToCoordinate(price - bucket / 2); y = [a, b]; ycache.set(price, y); }
        return y;
      };
      const gaps = []; for (let i = 1; i < cols.length; i++) gaps.push(cols[i].t - cols[i - 1].t);
      gaps.sort((p, q) => p - q);
      const medGap = gaps.length ? gaps[gaps.length >> 1] : 300, gapLimit = Math.max(medGap * 3, 1500);
      const gamma = S.compress / 100;
      if (S.heat) {
        ctx.save(); ctx.globalAlpha = S.alpha / 100;
        for (let i = 0; i < cols.length; i++) {
          const c = cols[i]; if (c.si < 0) continue;
          if (i + 1 < cols.length && cols[i + 1].si === c.si) continue;   // several snapshots in one candle: keep the newest
          const x0 = ts.timeToCoordinate(D.barT[c.si]); if (x0 == null) continue;
          let nx = null;
          for (let k = i + 1; k < cols.length; k++) if (cols[k].si > c.si) { nx = ts.timeToCoordinate(D.barT[cols[k].si]); break; }
          let nxT = null;
          for (let k = i + 1; k < cols.length; k++) if (cols[k].si > c.si) { nxT = cols[k].t; break; }
          /* a column lasts until the next one; only a real hole in the data (collector off, market closed) is left empty */
          const x1 = nx != null && nxT != null && nxT - c.t <= gapLimit ? nx : x0 + barPx * Math.max(1, Math.min(10, medGap / (Math.max(S.tf, 1) * 60)));
          if (x1 < -2 || x0 > W / hr + 2) continue;
          const px = Math.round(x0 * hr), pw = Math.max(1, Math.round(x1 * hr) - px);
          for (let j = 0; j < c.bins.length; j++) {
            const v = lensValue(c, j, lens), t = Math.min(1, Math.abs(v) / vmax) ** gamma;
            if (t < 0.03) continue;
            const y = yOf(c.bins[j]); if (y[0] == null || y[1] == null) continue;
            ctx.fillStyle = (lut || (v >= 0 ? LUT.netPos : LUT.netNeg))[Math.min(255, Math.round(t * 255))];
            ctx.fillRect(px, Math.round(y[0] * vr), pw, Math.max(1, Math.ceil((y[1] - y[0]) * vr) + 1));
          }
        }
        ctx.restore();
      }
      /* cursor column (replay) */
      const ci = cursorIdx();
      if (R.on && ci >= 0 && cols[ci] && cols[ci].si >= 0) {
        const x = ts.timeToCoordinate(D.barT[cols[ci].si]);
        if (x != null) { ctx.fillStyle = 'rgba(231,164,76,.85)'; ctx.fillRect(Math.round(x * hr), 0, Math.max(1, Math.round(hr)), sc.bitmapSize.height); }
      }
    });
  }
}
/* ---- side profile primitive (bars growing from the right edge) ---- */
class ProfilePrim {
  attached(p) { this.series = p.series; this.chart = p.chart; this.req = p.requestUpdate; }
  detached() {}
  updateAllViews() {}
  redraw() { if (this.req) this.req(); }
  paneViews() { return this._v || (this._v = [{ zOrder: () => 'normal', renderer: () => ({ draw: (t) => this.draw(t) }) }]); }
  draw(target) {
    target.useBitmapCoordinateSpace((sc) => {
      if (!S.profile) return;
      const ci = cursorIdx(); const col = D.cols[ci]; if (!col || !col.bins.length) return;
      const ctx = sc.context, hr = sc.horizontalPixelRatio, vr = sc.verticalPixelRatio, W = sc.bitmapSize.width;
      const ser = this.series, bucket = D.bucket || S.bucket;
      let m = 0; for (let j = 0; j < col.bins.length; j++) m = Math.max(m, col.c[j], -col.p[j]);
      if (m <= 0) return;
      const maxW = Math.min(W * 0.2, 230 * hr), edge = W - 4 * hr;
      ctx.fillStyle = 'rgba(8,11,17,.32)'; ctx.fillRect(edge - maxW - 6 * hr, 0, maxW + 10 * hr, sc.bitmapSize.height);
      let bc = -1, bp = -1, vc = 0, vp = 0;
      for (let j = 0; j < col.bins.length; j++) {
        const a = ser.priceToCoordinate(col.bins[j] + bucket / 2), b = ser.priceToCoordinate(col.bins[j] - bucket / 2);
        if (a == null || b == null) continue;
        const top = a * vr, h = Math.max(2, (b - a) * vr - 1), half = h / 2;
        const wc = (col.c[j] / m) * maxW, wp = (-col.p[j] / m) * maxW;
        ctx.fillStyle = rgba(S.pal.pos, 0.78); if (wc > 0.5) ctx.fillRect(edge - wc, top, wc, half);
        ctx.fillStyle = rgba(S.pal.neg, 0.78); if (wp > 0.5) ctx.fillRect(edge - wp, top + half, wp, Math.max(1, h - half));
        if (col.c[j] > vc) { vc = col.c[j]; bc = j; }
        if (-col.p[j] > vp) { vp = -col.p[j]; bp = j; }
      }
      ctx.font = `${11 * hr}px ui-monospace,Menlo,monospace`; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      const lab = (j, txt, color) => {
        const y = ser.priceToCoordinate(col.bins[j]); if (y == null) return;
        const w = ctx.measureText(txt).width + 10 * hr;
        ctx.fillStyle = 'rgba(10,13,19,.82)'; ctx.fillRect(edge - maxW - w - 6 * hr, y * vr - 9 * hr, w, 18 * hr);
        ctx.fillStyle = color; ctx.fillText(txt, edge - maxW - 11 * hr, y * vr);
      };
      const N = modeNames();
      if (bc >= 0) lab(bc, `${N.c} ${f0.format(col.bins[bc])}  ${fM(col.c[bc])}`, '#7ee8b8');
      if (bp >= 0) lab(bp, `${N.p} ${f0.format(col.bins[bp])}  ${fM(col.p[bp])}`, '#ff9aa5');
      ctx.fillStyle = 'rgba(160,172,194,.95)'; ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';
      ctx.fillText(`側邊分布 · ${N.title} · ${hm(col.t)}`, edge - 4 * hr, sc.bitmapSize.height - 6 * vr);
    });
  }
}
/* ---- support / resistance bands: a soft shaded zone around every wall and flip line ---- */
class BandPrim {
  attached(p) { this.series = p.series; this.chart = p.chart; this.req = p.requestUpdate; }
  detached() {}
  updateAllViews() {}
  redraw() { if (this.req) this.req(); }
  paneViews() { return this._v || (this._v = [{ zOrder: () => 'bottom', renderer: () => ({ draw: (t) => this.draw(t) }) }]); }
  draw(target) {
    target.useBitmapCoordinateSpace((sc) => {
      if (!S.band || !S.lines) return;
      const ctx = sc.context, hr = sc.horizontalPixelRatio, vr = sc.verticalPixelRatio, ts = this.chart.timeScale(), ser = this.series;
      const W = S.bandW, A = S.bandA / 100, lastT = D.barT[D.barT.length - 1];
      for (const key in D.lvPts) {
        const [src, kind] = key.split('-'); if (!S.srcs.includes(src)) continue;
        const col = kind === 'call' ? S.pal.pos : kind === 'put' ? S.pal.neg : '#ffd166', c = hexRGB(col);
        const pts = D.lvPts[key];
        let i = 0;
        while (i < pts.length) {
          if (pts[i].value == null) { i++; continue; }
          let j = i;
          while (j + 1 < pts.length && pts[j + 1].value === pts[i].value) j++;
          const tEnd = j + 1 < pts.length ? pts[j + 1].time : lastT;
          const x0 = ts.timeToCoordinate(pts[i].time), x1 = ts.timeToCoordinate(tEnd);
          const ya = ser.priceToCoordinate(pts[i].value + W), yb = ser.priceToCoordinate(pts[i].value - W), ym = ser.priceToCoordinate(pts[i].value);
          i = j + 1;
          if (x0 == null || x1 == null || ya == null || yb == null || ym == null || x1 <= x0) continue;
          const top = ya * vr, h = Math.max(2, (yb - ya) * vr), g = ctx.createLinearGradient(0, top, 0, top + h);
          const a = (kind === 'flip' ? A * 0.8 : A), rgb = `${c[0]},${c[1]},${c[2]}`;
          g.addColorStop(0, `rgba(${rgb},0)`); g.addColorStop(0.5, `rgba(${rgb},${a})`); g.addColorStop(1, `rgba(${rgb},0)`);
          ctx.fillStyle = g; ctx.fillRect(Math.round(x0 * hr), top, Math.max(1, Math.round((x1 - x0) * hr)), h);
        }
      }
    });
  }
}
/* ---- 導師 overlay:掛單牆(色帶)+方向箭頭(站上/跌破)+方向信心徽章 ---- */
let mentorOv = null;
let mentorLines = [];   // createPriceLine 建立的價位線,方便整組清掉
class MentorPrim {
  attached(p) { this.series = p.series; this.chart = p.chart; this.req = p.requestUpdate; }
  detached() {}
  updateAllViews() {}
  redraw() { if (this.req) this.req(); }
  paneViews() { return this._v || (this._v = [{ zOrder: () => 'normal', renderer: () => ({ draw: (t) => this.draw(t) }) }]); }
  draw(target) {
    target.useBitmapCoordinateSpace((sc) => {
      const ov = mentorOv; if (!ov) return;
      const ctx = sc.context, hr = sc.horizontalPixelRatio, vr = sc.verticalPixelRatio, W = sc.bitmapSize.width;
      const ser = this.series, ts = this.chart.timeScale();
      const bt = D.barT; if (!bt || bt.length < 2) return;
      const x0 = ts.timeToCoordinate(bt[0]), x1 = ts.timeToCoordinate(bt[bt.length - 1]);
      if (x0 == null || x1 == null) return;
      const L = Math.round(Math.min(x0, x1) * hr), R = Math.round(Math.max(x0, x1) * hr);
      // 掛單牆:半透明色帶(綠=買牆,紅=賣牆)
      for (const w of (ov.walls || [])) {
        const ya = ser.priceToCoordinate(w.hi), yb = ser.priceToCoordinate(w.lo);
        if (ya == null || yb == null) continue;
        const top = Math.round(Math.min(ya, yb) * vr), h = Math.max(2, Math.round(Math.abs(yb - ya) * vr));
        const col = w.side === 'buy' ? UP : DN;
        ctx.fillStyle = rgba(col, 0.18);
        ctx.fillRect(L, top, R - L, h);
        ctx.font = `${11 * hr}px ui-monospace,Menlo,monospace`;
        ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
        ctx.fillStyle = rgba(col, 0.95);
        ctx.fillText(w.side === 'buy' ? '買牆' : '賣牆', L + 5 * hr, top + h / 2);
      }
      // 方向箭頭(右緣):站上=綠▲,跌破=紅▼
      for (const a of (ov.arrows || [])) {
        const y = ser.priceToCoordinate(a.p);
        if (y == null) continue;
        const yy = y * vr, col = a.dir === 'up' ? UP : DN, s = 6 * hr, ex = W - 4 * hr;
        ctx.fillStyle = col;
        ctx.beginPath();
        if (a.dir === 'up') { ctx.moveTo(ex, yy - s); ctx.lineTo(ex - s, yy + s); ctx.lineTo(ex + s, yy + s); }
        else { ctx.moveTo(ex, yy + s); ctx.lineTo(ex - s, yy - s); ctx.lineTo(ex + s, yy - s); }
        ctx.closePath(); ctx.fill();
      }
      // 方向+信心徽章(左上角)
      if (ov.direction) {
        const col = ov.direction === '偏多' ? UP : DN;
        const txt = `${ov.direction}${ov.confidence ? ' · 信心' + ov.confidence : ''}`;
        ctx.font = `bold ${13 * hr}px ui-monospace,Menlo,monospace`;
        const w = ctx.measureText(txt).width + 16 * hr, bx = 10 * hr, by = 10 * vr, bh = 22 * vr;
        ctx.fillStyle = 'rgba(8,11,17,.85)';
        ctx.fillRect(bx, by, w, bh);
        ctx.fillStyle = col;
        ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
        ctx.fillText(txt, bx + 8 * hr, by + bh / 2);
      }
    });
  }
}
const heatPrim = new HeatPrim(), profPrim = new ProfilePrim(), bandPrim = new BandPrim(), mentorPrim = new MentorPrim();
candle.attachPrimitive(heatPrim);
candle.attachPrimitive(bandPrim);
candle.attachPrimitive(profPrim);
candle.attachPrimitive(mentorPrim);
/* ---- AI 判定窗:畫出 AI 導師最近一次判斷(方向箭頭 + 順向/反向 40 點區 + 它說的判錯價位),從它說話的那根 K 線開始 ---- */
const SCENE_X = 40, SCENE_MIN = 30;   // 跟驗證用的判定窗一致:±40 點 / 30 分鐘
class ScenePrim {
  attached(p) { this.series = p.series; this.chart = p.chart; this.req = p.requestUpdate; }
  detached() {}
  updateAllViews() {}
  redraw() { if (this.req) this.req(); }
  paneViews() { return this._v || (this._v = [{ zOrder: () => 'top', renderer: () => ({ draw: (t) => this.draw(t) }) }]); }
  draw(target) {
    target.useBitmapCoordinateSpace((sc) => {
      const A = D.ai;
      if (!S.mscene || R.on || S.day || !A || !D.barT.length) return;
      const dirUp = A.direction === '偏多', dirDn = A.direction === '偏空';
      if (!dirUp && !dirDn) return;
      const spot = A.spot; if (spot == null) return;
      const ctx = sc.context, hr = sc.horizontalPixelRatio, vr = sc.verticalPixelRatio;
      const ts = this.chart.timeScale(), ser = this.series;
      const bi = lowerIdx(D.barT, A.t); if (bi < 0) return;
      const xl = ts.timeToCoordinate(D.barT[bi]); if (xl == null) return;
      const sp = ts.options().barSpacing || 6;
      const maxW = Math.max(0, ts.width() - xl - 8);
      const w = Math.min(maxW, Math.max(40, SCENE_MIN / Math.max(S.tf, 1) * sp));
      if (w < 24) return;
      const sg = dirUp ? 1 : -1, col = dirUp ? UP : DN, opp = dirUp ? DN : UP;
      const y0 = ser.priceToCoordinate(spot), yT = ser.priceToCoordinate(spot + sg * SCENE_X), yS = ser.priceToCoordinate(spot - sg * SCENE_X);
      if (y0 == null || yT == null || yS == null) return;
      const X = Math.round(xl * hr), W = Math.round(w * hr);
      const rect = (ya, yb, c, a2) => { const top = Math.min(ya, yb) * vr, hh = Math.abs(yb - ya) * vr; ctx.fillStyle = rgba(c, a2); ctx.fillRect(X, top, W, hh); };
      rect(y0, yT, col, 0.16); rect(y0, yS, opp, 0.12);
      ctx.lineWidth = 1.4 * hr;
      ctx.setLineDash([]); ctx.strokeStyle = col; ctx.beginPath(); ctx.moveTo(X, yT * vr); ctx.lineTo(X + W, yT * vr); ctx.stroke();
      ctx.setLineDash([5 * hr, 4 * hr]); ctx.strokeStyle = opp; ctx.beginPath(); ctx.moveTo(X, yS * vr); ctx.lineTo(X + W, yS * vr); ctx.stroke();
      // AI 自己說的判錯價位(不一定等於 ±40)
      if (A.invalid != null) {
        const yi = ser.priceToCoordinate(A.invalid);
        if (yi != null) {
          ctx.setLineDash([2 * hr, 3 * hr]); ctx.strokeStyle = '#ffd166'; ctx.lineWidth = 1.2 * hr;
          ctx.beginPath(); ctx.moveTo(X, yi * vr); ctx.lineTo(X + W, yi * vr); ctx.stroke();
          ctx.fillStyle = '#ffd166'; ctx.font = `${10.5 * hr}px ui-monospace,Menlo,monospace`; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
          ctx.fillText(`AI認錯價 ${A.invalid.toFixed(0)}`, X + 4 * hr, yi * vr - 9 * vr);
        }
      }
      ctx.setLineDash([]);
      const ax = X + W * 0.5, ay0 = y0 * vr, ay1 = (y0 + (yT - y0) * 0.9) * vr, hs = 6 * hr;
      ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 2 * hr;
      ctx.beginPath(); ctx.moveTo(ax, ay0); ctx.lineTo(ax, ay1); ctx.stroke();
      const base = ay1 + (dirUp ? hs * 1.6 : -hs * 1.6);
      ctx.beginPath(); ctx.moveTo(ax, ay1); ctx.lineTo(ax - hs, base); ctx.lineTo(ax + hs, base); ctx.closePath(); ctx.fill();
      ctx.font = `${11 * hr}px ui-monospace,Menlo,monospace`; ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
      ctx.fillStyle = col; ctx.fillText(`順向 ${(spot + sg * SCENE_X).toFixed(0)}`, X + 4 * hr, yT * vr + (dirUp ? 9 : -9) * vr);
      ctx.fillStyle = opp; ctx.fillText(`反向 ${(spot - sg * SCENE_X).toFixed(0)}`, X + 4 * hr, yS * vr + (dirUp ? -9 : 9) * vr);
      ctx.fillStyle = 'rgba(200,210,225,.9)';
      ctx.fillText(`AI ${A.direction}${A.confidence != null ? ' ' + A.confidence + '%' : ''} · ${hmIn(A.t, 'Asia/Taipei')}`, X + 4 * hr, y0 * vr + (dirUp ? 14 : -14) * vr);
    });
  }
}
const scenePrim = new ScenePrim(); candle.attachPrimitive(scenePrim); window.__scene = scenePrim;

function redraw() { heatPrim.redraw(); bandPrim.redraw(); profPrim.redraw(); embandPrim.redraw(); mentorPrim.redraw(); scenePrim.redraw(); if (window.__flm) window.__flm.redraw(); if (window.__sqr) window.__sqr.redraw(); }

function cursorIdx() {
  if (R.on) return R.idx;
  if (S.follow && hoverIdx >= 0) return hoverIdx;
  return D.cols.length - 1;
}

/* ------------------------------ level lines ------------------------------ */
let lineSeries = {};   // 'NQ-call' -> series
function rebuildLineSeries() {
  for (const k in lineSeries) chart.removeSeries(lineSeries[k]);
  lineSeries = {};
  for (const src of S.srcs) for (const [kind, name] of KINDS) {
    lineSeries[`${src}-${kind}`] = chart.addSeries(LW.LineSeries, {
      color: (S.lineCol && S.lineCol[src]) || SRC_COL[src], lineWidth: lw(`${src}-${kind}`), lineStyle: kind === 'call' ? 0 : kind === 'put' ? 1 : 3,
      lineType: LW.LineType.WithSteps, title: S.llabels ? `${sn(src)} ${name}` : '', lastValueVisible: S.llabels, priceLineVisible: false,
      crosshairMarkerVisible: false, autoscaleInfoProvider: () => null, visible: S.lines,
    });
  }
}
function latestLevel(src, basis = S.basis, scope = S.scope) {
  if (src === 'WGT') { const o = D.levels.WGT; if (!o || !o.t.length) return null; const i = o.t.length - 1; return { call: o.call[i], put: o.put[i], flip: o.flip[i] }; }
  const st = D.status; if (!st) return null;
  const B = basis.toUpperCase();
  if (scope === 'zero') { const z = st.levels_zero && st.levels_zero[src] && st.levels_zero[src][B]; return z || null; }
  const v = st.levels && st.levels[src] && st.levels[src][B] && st.levels[src][B][SCOPE_KEY[scope]];
  return v || null;
}
function applyLevels() {
  for (const src of S.srcs) {
    const ser = D.levels[src]; if (!ser) continue;
    const lastT = D.barT[D.barT.length - 1];
    for (const [kind] of KINDS) {
      const line = lineSeries[`${src}-${kind}`]; if (!line) continue;
      const map = new Map();
      ser.t.forEach((t, i) => {
        const si = snapIdx(t); if (si < 0) return;
        map.set(D.barT[si], ser[kind][i]);
      });
      const lv = latestLevel(src);
      if (lv && lastT != null && D.status && D.status.latest && Math.abs(D.status.latest - lastT) < 3 * 3600) map.set(lastT, lv[kind]);
      const pts = [...map.entries()].sort((a, b) => a[0] - b[0]).map(([time, v]) => (v == null ? { time } : { time, value: v }));
      line.setData(pts);
      D.lvPts[`${src}-${kind}`] = pts;
    }
  }
}

/* ------------------------------ data loading ------------------------------ */
function rangeNow() {
  const st = D.status || {};
  const hiData = Math.max(st.latest || 0, st.bars && st.bars.last_t || 0) || st.now || Math.floor(Date.now() / 1000);
  if (S.day) {
    const d = D.days.find((x) => x.date === S.day);
    if (d) return { lo: d.first - 1800, hi: d.last + 900 };
  }
  return { lo: hiData - S.hours * 3600, hi: hiData + 900 };
}
async function loadAll(fit) {
  const token = ++D.token;
  const { lo, hi } = rangeNow();
  const src = S.srcs.join(',');
  const real = S.srcs.filter((x) => x !== 'WGT'); if (!real.length) real.push('NQ', 'QQQ', 'NDX');
  try {
    const [bars, net3] = await Promise.all([
      api('bars', { from: lo, to: hi, tf: S.tf }),
      real.length ? api('net3', { from: lo, to: hi, src: real.join(','), basis: S.basis }) : Promise.resolve({ all: [], zero: [], ex0: [] }),
    ]);
    if (token !== D.token) return;
    D.kind = bars.kind;
    if (bars.kind === 'candles') {
      D.bars = bars.data.filter((r) => r[1] != null && r[2] != null && r[3] != null && r[4] != null)
        .map(([t, o, h, l, c, v]) => ({ time: t, open: o, high: h, low: l, close: c, vol: v || 0 }));
      tickLine.setData([]);
      applyCandleStyle();
    } else {
      D.bars = bars.data.filter((r) => r[1] != null)
        .map(([t, s]) => ({ time: t, open: s, high: s, low: s, close: s }));
      tickLine.setData(D.bars.map((b) => ({ time: b.time, value: b.close })));
      const none = 'rgba(0,0,0,0)';
      candle.applyOptions({ upColor: none, downColor: none, borderUpColor: none, borderDownColor: none, wickUpColor: none, wickDownColor: none });
    }
    D.barT = D.bars.map((b) => b.time);
    candle.setData(D.bars);
    D.last = D.bars.length ? D.bars[D.bars.length - 1].close : (D.status && D.status.spot) || null;
    D.cols.forEach((c) => { c.si = snapIdx(c.t); });
    D.net3 = net3;
    applyNet(); applySq(); loadMentorMarks();
    rebuildLineSeries();
    applyLevelsFromCache();
    if (fit) { chart.timeScale().fitContent(); }
    $('#empty').hidden = true;
    redraw(); renderSide(); updateReplayBounds(); updateLegend();
    loadHeat(); loadLevels(token); loadBand(token); loadBand0(token); loadBook(); loadEvents(true); loadSq();
  } catch (e) {
    toast('讀取資料失敗:' + e.message);
  }
}
let busyN = 0;
function busy(d, text) { busyN = Math.max(0, busyN + d); const b = $('#busy'); b.hidden = busyN === 0; if (text) b.lastChild.textContent = text; }
const heatSrcs = () => (S.heatSrc === 'ALL' ? ['NQ', 'QQQ', 'NDX'] : [S.heatSrc]);
async function loadHeat() {
  const tok = ++D.heatTok, { lo, hi } = rangeNow();
  const base = { from: lo, to: hi, src: heatSrcs().join(','), basis: S.basis, scope: S.scope, bucket: S.bucket };
  const slow = S.heatMode !== 'strike';
  busy(1, slow ? '計算 ' + modeNames().title + '(第一次較慢,之後會快取)…' : '讀取熱圖…');
  try {
    const h = S.heatMode === 'strike' ? await api('heat', base) : await api('heatx', { ...base, mode: S.heatMode });
    if (tok !== D.heatTok) return;
    buildCols(h); redraw(); updateReplayBounds(); renderSide(); updateLegend();
  } catch (e) { if (tok === D.heatTok) toast('熱圖讀取失敗:' + e.message); } finally { busy(-1); }
}
async function loadLevels(token) {
  if (!S.srcs.length) return;
  const { lo, hi } = rangeNow();
  busy(1, '計算牆與 flip…');
  try {
    const r = await api('levels', { from: lo, to: hi, src: S.srcs.join(','), basis: S.basis, scope: S.scope, ...wParams() });
    if (token !== D.token) return;
    D.levels = r.series; D.levelsKey = levelsKey();
    applyLevels(); renderSide(); rebuildClientEvents();
  } catch (e) { toast('牆 / flip 計算失敗:' + e.message); } finally { busy(-1); }
}
const wParams = () => ({ w: `NQ:${S.wgt.NQ},QQQ:${S.wgt.QQQ},NDX:${S.wgt.NDX}`, norm: S.wnorm ? 1 : 0 });
const levelsKey = () => [S.srcs.join(','), S.basis, S.scope, S.srcs.includes('WGT') ? JSON.stringify(wParams()) : ''].join('|');
function applyLevelsFromCache() { if (D.levelsKey === levelsKey()) applyLevels(); else D.levels = {}; }
function buildCols(h) {
  D.bucket = h.bucket;
  D.cols = h.times.map((t, i) => {
    const f = h.cols[i], n = f.length / 3, bins = new Int32Array(n), c = new Float32Array(n), p = new Float32Array(n);
    for (let j = 0; j < n; j++) { bins[j] = f[3 * j]; c[j] = f[3 * j + 1]; p[j] = f[3 * j + 2]; }
    return { t, spot: h.spots[i], bins, c, p, si: snapIdx(t) };
  });
  computeVmax();
}
function applyNet() {
  const map = (arr) => {
    const pts = []; let k = -1;
    for (let i = 0; i < D.barT.length; i++) {
      const t = D.barT[i];
      while (k + 1 < arr.length && arr[k + 1][0] <= t) k++;
      if (k >= 0 && t - arr[k][0] < 1200 && arr[k][1] != null) pts.push({ time: t, value: arr[k][1] }); else pts.push({ time: t });
    }
    return pts;
  };
  const R = D.net3 || { all: [], zero: [], ex0: [] };
  netSeries.setData(map(R.all));
  netZero.setData(map(R.zero));
  netEx0.setData(map(R.ex0));
  const names = { oi: '未平倉量', vol: '成交量' };
  $('#subtitle').textContent = `Net GEX · 粗帶=全部(綠正/紅負) · 橘=0DTE · 藍=排除0DTE · ${(S.srcs.filter((x) => x !== 'WGT').join('+') || 'NQ+QQQ+NDX')} · ${names[S.basis]}  ($ / 1% 變動)`;
}
/* ---- ±1σ expected-move band ---- */
async function loadBand(token) {
  const { lo, hi } = rangeNow();
  const real = S.srcs.filter((x) => x !== 'WGT'); const src = real[0] || 'NQ';
  try {
    const r = await api('band', { from: lo, to: hi, src });
    if (token !== D.token) return;
    D.band = r; applyBand(); redraw();
  } catch (e) { toast('波動帶計算失敗:' + e.message); }
}
function applyBand() {
  const R = D.band;
  if (!R || !R.series || !R.series.length) { bandUp.setData([]); bandDn.setData([]); D.bandUpPts = []; D.bandDnPts = []; return; }
  const map = (idx) => {
    const pts = []; let k = -1;
    for (let i = 0; i < D.barT.length; i++) {
      const t = D.barT[i];
      while (k + 1 < R.series.length && R.series[k + 1][0] <= t) k++;
      if (k >= 0 && t - R.series[k][0] < 1200) pts.push({ time: t, value: R.series[k][idx] }); else pts.push({ time: t });
    }
    return pts;
  };
  D.bandUpPts = map(2); D.bandDnPts = map(3);
  bandUp.setData(D.bandUpPts); bandDn.setData(D.bandDnPts);
}
async function loadBand0(token) {
  const { lo, hi } = rangeNow();
  const real = S.srcs.filter((x) => x !== 'WGT'); const src = real[0] || 'NQ';
  try {
    const r = await api('band0', { from: lo, to: hi, src });
    if (token !== D.token) return;
    D.band0 = r; applyBand0(); redraw();
  } catch (e) { toast('0DTE 區間計算失敗:' + e.message); }
}
function applyBand0() {
  const R = D.band0;
  if (!R || !R.series || !R.series.length) { band0Up.setData([]); band0Dn.setData([]); D.band0UpPts = []; D.band0DnPts = []; return; }
  const map = (idx) => {
    const pts = []; let k = -1;
    for (let i = 0; i < D.barT.length; i++) {
      const t = D.barT[i];
      while (k + 1 < R.series.length && R.series[k + 1][0] <= t) k++;
      if (k >= 0 && t - R.series[k][0] < 1200) pts.push({ time: t, value: R.series[k][idx] }); else pts.push({ time: t });
    }
    return pts;
  };
  D.band0UpPts = map(2); D.band0DnPts = map(3);
  band0Up.setData(D.band0UpPts); band0Dn.setData(D.band0DnPts);
}
/* ---- analysis page ---- */
function anlyVis() {
  $('#pcrChart').hidden = S.anly !== 'pcr';
  $('#skewChart').hidden = S.anly !== 'skew';
  $('#divCol').hidden = S.anly !== 'div';
  $('#anlyPcrBasisGrp').hidden = S.anly !== 'pcr';
  $('#anlySkewSrcGrp').hidden = S.anly !== 'skew';
  $('#anlyDivBasisGrp').hidden = S.anly !== 'div';
  $('#anlyDivScopeGrp').hidden = S.anly !== 'div';
}
function anlyMeta() {
  if (S.anly === 'pcr') {
    const b = S.anlyPcrBasis === 'vol' ? '成交量' : '未平倉量';
    $('#anlyTitle').textContent = 'Put/Call 比';
    $('#anlySub').textContent = `以「${b}」為基準的口數比(Put ÷ Call)。大於 1 = Put 較多。藍實線 = 全部到期;橘虛線 = 0DTE。來源:NQ + QQQ + NDX 合計。`;
    $('#anlyNote').innerHTML = '<b>怎麼讀:</b>成交量 P/C 比是盤中情緒的即時溫度計,持續大於 1 通常反映避險買盤(put 需求)較旺;未平倉量 P/C 比則反映隔夜留倉的結構。成交量沒有買賣方向,所以這裡只有「熱度比例」,不是方向訊號。';
  } else if (S.anly === 'skew') {
    $('#anlyTitle').textContent = 'IV 偏斜(Skew)';
    $('#anlySub').textContent = `25Δ 風險逆轉:近月履約價中,25Δ Put 隱波 − 25Δ Call 隱波(單位 vol 點)。來源:${S.anlySkewSrc}。`;
    $('#anlyNote').innerHTML = '<b>怎麼讀:</b>正值(橘)代表 OTM Put 比 OTM Call 貴,市場願意多付錢買下檔保護 = 避險情緒;負值(藍)代表上檔 Call 較貴 = 追漲情緒。這是 IV 曲線的形狀,與價格本身無關。';
  } else {
    const b = S.anlyDivBasis === 'oi' ? '未平倉量' : '成交量', sc = { all: '全部到期', ex0: '排除 0DTE', zero: '只看 0DTE' }[S.anlyDivScope];
    $('#anlyTitle').textContent = '三來源分歧度';
    $('#anlySub').textContent = `NQ / QQQ / NDX 三個來源各自的 Flip 位置(上圖),以及三者相差的點數(下圖,分歧度 = 最高 Flip − 最低 Flip)。${b} · ${sc}。`;
    $('#anlyNote').innerHTML = '<b>怎麼讀:</b>三個來源的 Flip 越接近 = 市場對「轉折點」共識越強;分歧度擴大代表各市場(NQ 期貨 / QQQ ETF / NDX 指數)的部位結構不一致,關卡的可信度降低,行情更容易在區間內來回。';
  }
}
const cleanSeries = (arr) => arr.filter((x) => x && x[1] != null).map(([t, v]) => ({ time: t, value: v }));
function applyPcr() {
  const R = D.pcr; if (!R || !R.series) { pcrAll.setData([]); pcr0.setData([]); return; }
  const key = S.anlyPcrBasis === 'vol' ? 'vol' : 'oi', key0 = S.anlyPcrBasis === 'vol' ? 'vol0' : 'oi0';
  pcrAll.setData(cleanSeries(R.series[key])); pcr0.setData(cleanSeries(R.series[key0]));
}
function applySkew() {
  const R = D.skew; if (!R || !R.series) { skewLine.setData([]); return; }
  skewLine.setData(cleanSeries(R.series));
}
function applyDiv() {
  const R = D.div || {};
  const vals = {}, times = new Set();
  for (const src of ['NQ', 'QQQ', 'NDX']) {
    const s = R[src]; vals[src] = {};
    if (s) s.t.forEach((t, i) => { times.add(t); vals[src][t] = s.flip[i]; });
  }
  const ts = [...times].sort((a, b) => a - b);
  for (const src of ['NQ', 'QQQ', 'NDX']) divFlip[src].setData(ts.map((t) => ({ time: t, value: vals[src][t] })).filter((x) => x.value != null));
  const spread = [];
  for (const t of ts) {
    const f = ['NQ', 'QQQ', 'NDX'].map((s) => vals[s][t]).filter((v) => v != null);
    if (f.length >= 2) spread.push({ time: t, value: Math.round(Math.max(...f) - Math.min(...f)) });
  }
  divSpread.setData(spread);
}
async function loadAnalysis() {
  if ($('#view-anly').classList.contains('hidden')) return;
  const { lo, hi } = rangeNow();
  try {
    if (S.anly === 'pcr') { D.pcr = await api('pcr', { from: lo, to: hi }); applyPcr(); }
    else if (S.anly === 'skew') { D.skew = await api('skew', { from: lo, to: hi, src: S.anlySkewSrc }); applySkew(); }
    else { const r = await api('levels', { from: lo, to: hi, src: 'NQ,QQQ,NDX', basis: S.anlyDivBasis, scope: S.anlyDivScope }); D.div = r.series; applyDiv(); }
    D.anlyTs = D.status ? D.status.latest : null;
  } catch (e) { toast('分析資料讀取失敗:' + e.message); }
}
function fitAnly() {
  if (S.anly === 'pcr') pcrChart.timeScale().fitContent();
  else if (S.anly === 'skew') skewChart.timeScale().fitContent();
  else { divChart.timeScale().fitContent(); divSpreadChart.timeScale().fitContent(); }
}
function setAnly(m) {
  S.anly = m; save(); syncUI(); anlyVis(); anlyMeta(); loadAnalysis();
  setTimeout(fitAnly, 60);
}
/* ---- mentor / thesis ---- */
async function loadThesis() {
  try {
    D.thesis = await api('thesis', {});
    D.thesisTs = D.status ? D.status.latest : null;
  } catch (e) {
    toast('導師分析失敗:' + e.message);
    D.thesis = { ok: false, error: e.message };
  }
  renderThesis();
}
function renderThesis() {
  const T = D.thesis;
  renderMmini();
  const dir = $('#mDir'), conf = $('#mConf'), arc = $('#mConfArc');
  if (!T || !T.ok) {
    dir.className = 'mdir'; dir.innerHTML = '<span class="arr">—</span><b>等待資料</b><em></em>';
    conf.textContent = '—'; arc.style.strokeDashoffset = '188.5';
    $('#mGaugeMark').style.left = '50%';
    $('#mMeta').textContent = T && T.error ? `讀取失敗:${T.error}` : '';
    { const el = $('#mSplit'); if (el) el.innerHTML = ''; }
    { const hl = $('#mHeadline'); if (hl) hl.textContent = ''; }
    $('#mNarrative').textContent = '正在等待最新快照…';
    $('#mSrcs').innerHTML = ''; $('#mParts').innerHTML = '';
    $('#mDealer').textContent = ''; $('#mShort').innerHTML = '';
    $('#mKeys').innerHTML = ''; $('#mSigs').innerHTML = ''; $('#mDisclaimer').textContent = '';
    $('#mGauge').innerHTML = '';
    $('#mVerify').hidden = true; $('#mVerify').innerHTML = '';
    $('#mTrader').innerHTML = '';
    return;
  }
  const up = T.direction === '偏多';
  const str = T.strength === '強' ? '訊號強' : T.strength === '中' ? '訊號中' : '訊號弱';
  dir.className = 'mdir ' + (up ? 'up' : 'dn');
  dir.innerHTML = `<span class="arr">${up ? '↑' : '↓'}</span><b>${T.direction}</b><em>${str}</em>`;
  conf.textContent = T.confidence + '%';
  const circ = 2 * Math.PI * 30;
  arc.style.strokeDasharray = circ;
  arc.style.strokeDashoffset = circ * (1 - T.confidence / 100);
  arc.style.stroke = up ? 'var(--up)' : 'var(--dn)';
  $('#mGaugeMark').style.left = (50 + T.score / 2) + '%';
  { const hl = $('#mHeadline'); if (hl) hl.textContent = T.headline || ''; }
  renderLadder($('#mLadder'), { spot: T.spot, levels: thesisLevels(T), dir: T.direction, conf: T.confidence });
  { const rg = T.regime || {}; const el = $('#mSplit');
    if (el) el.innerHTML = T.score_dir == null ? '' :
      `<b>拆開看</b>:方向(不含 gamma 環境)<b class="${T.direction_dir === '偏多' ? 'upc' : 'dnc'}">${T.direction_dir} ${T.score_dir > 0 ? '+' : ''}${T.score_dir}</b>`
      + ` ｜ 環境 <b>${rg.label || '—'}</b><span class="msplitT">${rg.text || ''}</span>`; }
  $('#mMeta').innerHTML = `現價 ${fP(T.spot)} · 三市場加權 · 快照 ${mdhm(T.t)}` + (T.caveat ? ` · <b class="warn">${T.caveat}</b>` : '');

  const V = T.verify || {};
  if (V.has_prev) {
    const p = V.prev || {};
    const hitCls = V.direction_hit === '命中' ? 'hit' : V.direction_hit === '未命中' ? 'miss' : 'und';
    const moveTxt = V.move != null ? `${V.move > 0 ? '+' : ''}${V.move}` : '—';
    let vh = `<div class="mvbadge ${hitCls}">${V.direction_hit}</div>`;
    vh += `<div class="mvtxt"><b>上一段驗證</b><span>約 ${p.elapsed_min != null ? p.elapsed_min : '—'} 分鐘前:${p.direction}(${p.score > 0 ? '+' : ''}${p.score}) @ ${fP(p.spot)} → 現價 ${fP(T.spot)}(${moveTxt} 點)</span>`;
    if (V.flip_cross) vh += `<span class="dim">${V.flip_cross}</span>`;
    vh += `</div>`;
    if (V.n != null) {
      const vtot = Math.max(1, (V.hit || 0) + (V.miss || 0) + (V.undecided || 0));
      vh += `<div class="mvtrack"><b>戰績</b>
        <div class="mhitbar"><i class="h" style="width:${((V.hit || 0) / vtot) * 100}%"></i><i class="m" style="width:${((V.miss || 0) / vtot) * 100}%"></i><i class="u" style="width:${((V.undecided || 0) / vtot) * 100}%"></i></div>
        <span class="hit">命中 ${V.hit}</span><span class="miss">未中 ${V.miss}</span><span class="und">未分曉 ${V.undecided}</span><em class="dim">共 ${V.n} 筆</em></div>`;
    }
    $('#mVerify').innerHTML = vh;
    $('#mVerify').hidden = false;
  } else {
    $('#mVerify').hidden = true;
  }

  $('#mNarrative').textContent = T.narrative || '';
  $('#mDisclaimer').textContent = T.disclaimer;

  const W = T.weights || {};
  $('#mSrcs').innerHTML = ['NQ', 'QQQ', 'NDX'].map((s) => {
    const d = (T.sources || {})[s] || {};
    const u = d.direction === '偏多';
    return `<div class="msrc ${u ? 'up' : 'dn'}"><span class="msrcN" style="color:${SRC_COL[s]}">${s}</span><span class="msrcD">${u ? '↑' : '↓'} ${d.direction}</span><b>${d.score > 0 ? '+' : ''}${d.score}</b><em>權重 ${W[s] || 0}%</em></div>`;
  }).join('');

  const TR = T.trader || {};
  const OF = T.orderflow || {};
  if (TR.regime) {
    const gPos = TR.pos_gamma;
    let th = `<div class="mthead"><span class="mregime ${gPos ? 'up' : 'dn'}">${TR.regime}</span><span class="mgamma ${gPos ? 'up' : 'dn'}">做市商 ${TR.gamma_sign}</span>${TR.share0 != null ? `<span class="mtheadx">0DTE 佔比 <b>${TR.share0}%</b></span>` : ''}</div>`;
    if (OF.sentence) {
      th += `<div class="mof"><b class="${OF.available ? 'up' : 'warn'}">訂單流</b><span class="dim">${OF.sentence}</span></div>`;
    }
    if (TR.real && TR.real.length) {
      th += `<div class="mbookmap mreal"><b>真實 orderflow 讀數(Bookmap)</b><ul>${TR.real.map((x) => `<li>${x}</li>`).join('')}</ul></div>`;
    }
    if (TR.bookmap && TR.bookmap.length) {
      th += `<div class="mbookmap"><b>Bookmap 上應該會看到(結構推測)</b><ul>${TR.bookmap.map((x) => `<li>${x}</li>`).join('')}</ul></div>`;
    }
    if (TR.watch && TR.watch.length) {
      th += `<div class="mwatch"><b>當下觀察重點</b><ul>${TR.watch.map((x) => `<li>${x}</li>`).join('')}</ul></div>`;
    }
    $('#mTrader').innerHTML = th;
  } else {
    $('#mTrader').innerHTML = '<span class="dim">資料不足</span>';
  }

  const P = T.participants || {};
  let ph = P.sigma != null ? `<div class="mpverdict">分層基準:1σ ≈ <b>${fP(P.sigma)} 點</b>(價內 / 價外 / 深價外)</div>` : '';
  (P.markets || []).forEach((mk) => {
    const up = mk.verdict === '偏追漲(FOMO)';
    const dn = mk.verdict === '偏恐慌/避險';
    const cls = up ? 'up' : (dn ? 'dn' : '');
    ph += `<div class="mpmarket"><div class="mpmhead"><span class="mpsrc">${mk.src}</span><span class="mpverdict ${cls}">${mk.verdict || '—'}</span></div>`;
    (mk.buckets || []).forEach((b) => {
      const cd = b.call_dv || 0, pd = b.put_dv || 0, tot = cd + pd || 1;
      ph += `<div class="mpbarRow"><span class="mpbarLbl">${b.label}</span>
        <div class="mpbar"><i class="mpbarCall" style="width:${(cd / tot) * 100}%"></i><i class="mpbarPut" style="width:${(pd / tot) * 100}%"></i></div>
        <span class="mpbarNum">C ${cd} / P ${pd} · 未平倉 C${b.call_oi}/P${b.put_oi}</span></div>`;
    });
    if (mk.notes && mk.notes.length) ph += `<ul class="mpnotes">${mk.notes.map((n) => `<li>${n}</li>`).join('')}</ul>`;
    ph += `</div>`;
  });
  $('#mParts').innerHTML = ph || '<span class="dim">資料不足</span>';

  const klD = T.key_levels || {};
  const TRD = T.trader || {};
  let dh = '';
  if (klD.flip0 != null && T.spot != null) {
    const above = T.spot > klD.flip0;
    dh = `<div class="mdealerGauge ${above ? 'up' : 'dn'}">
      <div class="mdgTrack"><i class="mdgFlip"></i><i class="mdgSpot" style="left:${above ? '64%' : '36%'}"></i></div>
      <div class="mdgLbl"><span>負 gamma · 追漲殺跌</span><span>0DTE flip ${fP(klD.flip0)}</span><span>正 gamma · 買低賣高</span></div>
      <div class="mdgState">現價在 flip0 ${above ? '上方' : '下方'} → 做市商 ${TRD.gamma_sign || (above ? '正 gamma' : '負 gamma')}</div>
    </div>`;
  }
  $('#mDealer').innerHTML = dh + (T.dealer ? `<div class="mdealText">${T.dealer}</div>` : '');

  const SH = T.short || {};
  const shUp = SH.direction === '偏多';
  const shPct = Math.max(2, Math.min(98, 50 + (SH.score || 0) / 2));
  $('#mShort').innerHTML = `<div class="mshortRow"><b class="${shUp ? 'up' : 'dn'}">${shUp ? '↑' : '↓'} ${SH.direction}</b><span>分數 ${SH.score > 0 ? '+' : ''}${SH.score}</span></div>
    <div class="mshortTrack"><i class="mid"></i><i class="mark ${shUp ? 'up' : 'dn'}" style="left:${shPct}%"></i></div>
    <em class="dim">${SH.note}</em>`;

  const kl = T.key_levels || {};
  const lvls = [];
  if (kl.band_up != null) lvls.push(['±1σ 上沿', kl.band_up, 'band']);
  if (kl.band0_up != null) lvls.push(['0DTE 上沿', kl.band0_up, 'band0']);
  if (kl.call != null) lvls.push(['壓力', kl.call, 'res']);
  if (kl.flip != null) lvls.push(['Flip', kl.flip, 'flip']);
  if (kl.flip0 != null) lvls.push(['0DTE flip', kl.flip0, 'dte0']);
  if (kl.put != null) lvls.push(['支撐', kl.put, 'sup']);
  if (kl.band0_dn != null) lvls.push(['0DTE 下沿', kl.band0_dn, 'band0']);
  if (kl.band_dn != null) lvls.push(['±1σ 下沿', kl.band_dn, 'band']);
  if (T.spot != null) lvls.push(['現價', T.spot, 'spot']);
  lvls.sort((a, b) => (b[1] == null ? -1 : b[1]) - (a[1] == null ? -1 : a[1]));
  $('#mKeys').innerHTML = `<div class="mladder">` + lvls.map(([name, v, cls]) =>
    `<div class="mladderRow ${cls}" ${cls === 'spot' ? '' : `data-strike="${v}"`}><span>${name}</span><b>${fP(v)}</b></div>`).join('') + `</div>`;
  $$('#mKeys .mladderRow[data-strike]').forEach((k) => { k.onclick = () => jumpToStrike(+k.dataset.strike); });
  renderGauge(T);

  const sigHtml = (T.signals || []).map((s) => {
    const sUp = s.s >= 0, w = Math.round(Math.min(100, Math.abs(s.s) * 100));
    return `<div class="msig ${sUp ? 'up' : 'dn'}">
      <div class="msigTop"><span class="msigName">${s.name}</span><span class="msigVal">${s.value}</span><span class="msigVote">${sUp ? '偏多' : '偏空'}</span></div>
      <div class="msigBar"><i style="width:${w}%;background:${sUp ? 'var(--up)' : 'var(--dn)'}"></i></div>
      <div class="msigText">${s.text}</div>
    </div>`;
  }).join('');
  let ofHtml = '';
  if (OF.signals && OF.signals.length) {
    ofHtml = `<div class="msigOfHead dim">真實訂單流 · 權重 ${OF.weight}% · 約 ${Math.round(OF.age_sec)} 秒前</div>` +
      OF.signals.map((s) => {
        const sUp = s.s >= 0, w = Math.round(Math.min(100, Math.abs(s.s) * 100));
        return `<div class="msig ${sUp ? 'up' : 'dn'}">
          <div class="msigTop"><span class="msigName">${s.name}</span><span class="msigVal">${s.value}</span><span class="msigVote">${sUp ? '偏多' : '偏空'}</span></div>
          <div class="msigBar"><i style="width:${w}%;background:${sUp ? 'var(--up)' : 'var(--dn)'}"></i></div>
          <div class="msigText">${s.text}</div>
        </div>`;
      }).join('');
  }
  $('#mSigs').innerHTML = sigHtml + ofHtml;
}
function renderGauge(T) {
  const g = T.gauge || [];
  const el = $('#mGauge');
  if (!g.length) { el.innerHTML = '<span class="dim">儀表板資料還沒好</span>'; return; }
  el.innerHTML = g.map((t) => {
    const cls = t.dir === 'up' ? 'up' : t.dir === 'dn' ? 'dn' : 'flat';
    const mag = t.score == null ? null : Math.max(0, Math.min(1, Math.abs(t.score)));
    let fill = '';
    if (mag == null || mag < 0.08) {
      fill = '<i class="gfill flat"></i>';
    } else {
      const h = (mag * 50).toFixed(1);
      fill = t.dir === 'up'
        ? `<i class="gfill up" style="height:${h}%"></i>`
        : `<i class="gfill dn" style="height:${h}%"></i>`;
    }
    return `<div class="gcol" title="${t.note || ''}">
      <div class="gval ${cls}">${t.val || '—'}</div>
      <div class="gbar">${fill}</div>
      <div class="gname">${t.name}</div>
    </div>`;
  }).join('');
}
/* ---- 導師精簡重點版(浮在熱圖旁,可拖動、可關閉) ---- */
function renderMmini() {
  const box = $('#mmini');
  const T = D.thesis;
  if (!T || !T.ok || !S.mmini) { box.hidden = true; return; }
  const up = T.direction === '偏多';
  const kl = T.key_levels || {};
  const bits = [];
  if (kl.flip0 != null) bits.push(`0DTE flip ${fP(kl.flip0)}`);
  if (kl.put != null) bits.push(`支 ${fP(kl.put)}`);
  if (kl.call != null) bits.push(`壓 ${fP(kl.call)}`);
  $('#mminiBody').innerHTML = `<div class="mminiDir ${up ? 'up' : 'dn'}">${up ? '↑' : '↓'} ${T.direction} · 信心 ${T.confidence}%</div>
    <div class="mminiNarr">${(T.narrative || '').slice(0, 140)}</div>
    <div class="mminiKeys">${bits.join(' · ')}</div>`;
  box.hidden = false;
}
$('#mminiX').onclick = () => { S.mmini = false; save(); $('#mmini').hidden = true; };
(function () {
  const mm = $('#mmini');
  mm.addEventListener('mousedown', (e) => {
    if (e.target.closest('button')) return;
    const w = $('.chartwrap');
    const sx = e.clientX, sy = e.clientY, ox = mm.offsetLeft, oy = mm.offsetTop;
    const mv = (ev) => {
      mm.style.left = Math.max(0, Math.min(w.clientWidth - mm.offsetWidth, ox + ev.clientX - sx)) + 'px';
      mm.style.top = Math.max(0, Math.min(w.clientHeight - mm.offsetHeight, oy + ev.clientY - sy)) + 'px';
    };
    const up = () => { window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', mv); window.addEventListener('mouseup', up);
  });
})();
/* ---- orderflow momentum ---- */
async function loadSq() {
  if (S.subMode !== 'sq' && !S.sqrib) { return; }
  const tok = ++D.sqTok, { lo, hi } = rangeNow();
  try {
    const r = await api('flowmom', { from: lo, to: hi });
    if (tok !== D.sqTok) return;
    D.sq = r; applySq(); redraw();
  } catch (e) { toast('訂單流動能計算失敗:' + e.message); }
}
function applySq() {
  const sq = S.subMode === 'sq';
  subChart.applyOptions({ localization: { priceFormatter: sq ? (v) => f0.format(v) + ' 口' : (v) => fM(v).replace('+', '') } });
  netSeries.applyOptions({ visible: !sq });
  netZero.applyOptions({ visible: !sq });
  netEx0.applyOptions({ visible: !sq });
  flowCum.applyOptions({ visible: sq });
  spdSeries.applyOptions({ visible: sq });
  subChart.priceScale('spd').applyOptions({ visible: false });
  subChart.priceScale('right').applyOptions({ scaleMargins: sq ? { top: 0.14, bottom: 0.46 } : { top: 0.18, bottom: 0.12 } });
  $('#subwrap').classList.toggle('tall', sq);
  if (!sq) { flowCum.setData([]); spdSeries.setData([]); }
  if (!sq) { applyNet(); return; }
  const cum = [], bars = []; let k = -1; const R = D.sq;
  for (let i = 0; i < D.barT.length; i++) {
    const t = D.barT[i];
    if (R) { while (k + 1 < R.times.length && R.times[k + 1] <= t) k++; }
    const row = R && k >= 0 && t - R.times[k] < 1800 ? R.rows[k] : null;
    if (row) {
      cum.push({ time: t, value: row[0] });
      bars.push({ time: t, value: row[1], color: row[1] >= 0 ? 'rgba(38,201,139,.8)' : 'rgba(255,92,108,.8)' });
    } else {
      cum.push({ time: t }); bars.push({ time: t });
    }
  }
  D.flowMax = R ? Math.max(1, ...R.rows.filter(Boolean).map((r) => Math.abs(r[1]))) : 1;
  flowCum.setData(cum);
  spdSeries.setData(bars);
  $('#subtitle').textContent = '訂單流動能 · 線:累積 Delta(綠正/紅負,口)· 柱:每分鐘主動買賣淨量(綠買/紅賣)· Bookmap 真實訂單流';
}
function setSubMode(m) {
  S.subMode = m; save(); syncUI(); applySq();
  if (m === 'sq') loadSq();
}
class SqRibbon {
  attached(p) { this.series = p.series; this.chart = p.chart; this.req = p.requestUpdate; }
  detached() {}
  updateAllViews() {}
  redraw() { if (this.req) this.req(); }
  paneViews() { return this._v || (this._v = [{ zOrder: () => 'top', renderer: () => ({ draw: (t) => this.draw(t) }) }]); }
  draw(target) {
    target.useBitmapCoordinateSpace((sc) => {
      const R = D.sq; if (!S.sqrib || !R || !R.rows.length || !D.barT.length) return;
      const ctx = sc.context, hr = sc.horizontalPixelRatio, vr = sc.verticalPixelRatio, ts = this.chart.timeScale(), W = sc.bitmapSize.width;
      const H = Math.round(15 * vr);                       // 色帶高度 (px)
      /* 深色底帶 + 底部亮線:還沒出現強訊號前也看得出這條帶子的位置 */
      ctx.fillStyle = 'rgba(9,13,20,.70)';
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = 'rgba(255,255,255,.12)';
      ctx.fillRect(0, H - 1, W, Math.max(1, Math.round(vr)));
      const xa = ts.timeToCoordinate(D.barT[Math.max(0, D.barT.length >> 1)]), xb = ts.timeToCoordinate(D.barT[Math.min(D.barT.length - 1, (D.barT.length >> 1) + 1)]);
      const barPx = xa != null && xb != null && xb > xa ? xb - xa : 6;
      for (let i = 0; i < R.rows.length; i++) {
        const row = R.rows[i]; if (!row) continue;
        const si = snapIdx(R.times[i]); if (si < 0) continue;
        const x0 = ts.timeToCoordinate(D.barT[si]); if (x0 == null) continue;
        let x1 = null;
        if (i + 1 < R.rows.length && R.times[i + 1] - R.times[i] <= 1800) { const sj = snapIdx(R.times[i + 1]); if (sj > si) x1 = ts.timeToCoordinate(D.barT[sj]); }
        if (x1 == null) x1 = x0 + barPx * 5;
        if (x1 < -2 || x0 > W / hr + 2) continue;
        const net = row[1], m = Math.abs(net) / Math.max(D.flowMax || 1, 1);
        if (m < 0.06) continue;                            // 動能太弱:維持底帶顏色
        const h = Math.max(2, Math.round(m * (H - 2)));    // 柱高隨動能成長
        const a = Math.min(0.95, 0.25 + m * 0.7), c = net >= 0 ? '38,201,139' : '255,92,108';
        ctx.fillStyle = `rgba(${c},${a})`;
        const px = Math.round(x0 * hr);
        ctx.fillRect(px, H - h, Math.max(1, Math.round(x1 * hr) - px), h);
      }
    });
  }
}
const sqRibbon = new SqRibbon(); candle.attachPrimitive(sqRibbon); window.__sqr = sqRibbon;
window.__sq = { D, applySq, redraw };
function rebuildAligned() {
  for (const c of D.cols) c.si = snapIdx(c.t);
  applyNet(); applySq(); applyLevels(); redraw();
}


/* ---- 副圖:拖曳調高度 / 收合(設定會記住) ---- */
function applySubSize() {
  const w = $('#subwrap'); if (!w) return;
  w.classList.toggle('folded', !!S.subFold);
  w.style.height = (S.subH > 0 && !S.subFold) ? S.subH + 'px' : '';
  $('#subFold').textContent = S.subFold ? '▴' : '▾';
}
(() => {
  const w = $('#subwrap'), hd = $('#subResize');
  let y0 = 0, h0 = 0, on = false;
  hd.addEventListener('pointerdown', (e) => { on = true; y0 = e.clientY; h0 = w.getBoundingClientRect().height; hd.setPointerCapture(e.pointerId); hd.classList.add('drag'); e.preventDefault(); });
  hd.addEventListener('pointermove', (e) => {
    if (!on) return;
    const maxH = Math.max(120, ($('.stage').getBoundingClientRect().height - 200) * 0.7);
    S.subH = Math.round(Math.min(maxH, Math.max(70, h0 + (y0 - e.clientY))));
    applySubSize();
  });
  const end = () => { if (!on) return; on = false; hd.classList.remove('drag'); save(); };
  hd.addEventListener('pointerup', end); hd.addEventListener('pointercancel', end);
  hd.addEventListener('dblclick', () => { S.subH = 0; save(); applySubSize(); });
  $('#subFold').addEventListener('click', () => { S.subFold = !S.subFold; save(); applySubSize(); });
})();

/* ---- 頂端摘要:現價 / 離牆與 flip 的點數 / gamma 環境 / 時間與開收盤倒數 ---- */
const NY_TZ = 'America/New_York', TW_TZ = 'Asia/Taipei';
function tzParts(ms, tz) {
  const f = new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric', weekday: 'short' });
  const o = {}; for (const p of f.formatToParts(new Date(ms))) o[p.type] = p.value;
  return { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour, mi: +o.minute, s: +o.second, wd: o.weekday };
}
/* 某時區的「年月日時分」→ UTC 毫秒(自動處理夏令/冬令):反覆用該時區的牆上時間修正猜測值 */
function zonedMs(y, mo, d, h, mi, tz) {
  const want = Date.UTC(y, mo - 1, d, h, mi);
  let g = want;
  for (let i = 0; i < 3; i++) {
    const p = tzParts(g, tz);
    g += want - Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, 0);
  }
  return g;
}
/* 你的交易時段 = 紐約 09:30–13:30(週一到週五;不含美股假日)。回傳 {inSession, start, end, next} */
function sessionInfo(nowMs) {
  const SESS_START = [9, 30], SESS_END = [13, 30];
  const ny = tzParts(nowMs, NY_TZ);
  const at = (d0, hm) => { const base = new Date(Date.UTC(ny.y, ny.mo - 1, ny.d + d0)); return zonedMs(base.getUTCFullYear(), base.getUTCMonth() + 1, base.getUTCDate(), hm[0], hm[1], NY_TZ); };
  const isWk = (d0) => { const w = new Date(Date.UTC(ny.y, ny.mo - 1, ny.d + d0)).getUTCDay(); return w >= 1 && w <= 5; };
  if (isWk(0)) {
    const s = at(0, SESS_START), e = at(0, SESS_END);
    if (nowMs >= s && nowMs < e) return { inSession: true, start: s, end: e };
    if (nowMs < s) return { inSession: false, next: s };
  }
  for (let k = 1; k <= 7; k++) if (isWk(k)) return { inSession: false, next: at(k, SESS_START) };
  return { inSession: false, next: null };
}
const pad2 = (n) => String(n).padStart(2, '0');
const fHMS = (ms) => { const t = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(t / 3600)}:${pad2(Math.floor(t % 3600 / 60))}:${pad2(t % 60)}`; };
const fClock = (ms, tz) => { const p = tzParts(ms, tz); return `${pad2(p.h)}:${pad2(p.mi)}`; };
function tickClock() {
  const el = $('#gClock'); if (!el) return;
  const now = Date.now(), si = sessionInfo(now);
  let line;
  if (si.inSession) line = `<small>交易時段中(紐約 09:30–13:30)</small><b>剩 ${fHMS(si.end - now)}</b>`;
  else if (si.next) line = `<small>距下次開盤(台北 ${fClock(si.next, TW_TZ)} · 紐約 09:30)</small><b>${fHMS(si.next - now)}</b>`;
  else line = '<small>交易時段</small><b>—</b>';
  el.innerHTML = line + `<small>台北 ${fClock(now, TW_TZ)} · 紐約 ${fClock(now, NY_TZ)}</small>`;
}
function updateGlance() {
  const g = $('#glance'); if (!g) return;
  const st = D.status;
  const spot = D.last != null ? D.last : (st && st.spot);
  if (!st || spot == null) { g.innerHTML = '<div class="gc px"><small>NQ</small><b>—</b></div><div class="gc clk" id="gClock"></div>'; tickClock(); return; }
  const lv = (st.levels && st.levels.NQ && st.levels.NQ.OI && st.levels.NQ.OI.ALL) || {};
  const z = (st.levels_zero && st.levels_zero.NQ && st.levels_zero.NQ.OI) || {};
  const cell = (label, price, tip) => {
    if (price == null) return `<div class="gc"><small>${label}</small><b>—</b></div>`;
    const d = price - spot, near = Math.abs(d) <= 40;
    const sign = d >= 0 ? '+' : '−';
    return `<div class="gc${near ? ' near' : ''}" title="${tip}"><small>${label}</small><b>${fP(price)}<em>${sign}${Math.abs(Math.round(d))} 點</em></b></div>`;
  };
  const up = lv.call_above, dn = lv.put_below;
  let reg = '<div class="gc reg"><small>gamma 環境</small><b>—</b></div>';
  if (z.flip != null) {
    const pos = spot >= z.flip;
    reg = `<div class="gc reg ${pos ? 'pos' : 'neg'}" title="現價相對 0DTE flip 的位置。「正 gamma 傾向壓波動、負 gamma 傾向放大波動」是依 call 為正、put 為負的慣例推論,不是已驗證的事實。"><small>gamma 環境(0DTE flip 之${pos ? '上' : '下'})</small><b>${pos ? '正 gamma · 壓波動' : '負 gamma · 放大波動'}</b></div>`;
  }
  const nearUp = up != null && up - spot <= 40 && up - spot >= 0, nearDn = dn != null && spot - dn <= 40 && spot - dn >= 0;
  const note = (nearUp || nearDn) ? `<div class="gnote">⚠ ${nearUp ? `上方 ${Math.round(up - spot)} 點內有 Call 牆` : ''}${nearUp && nearDn ? ';' : ''}${nearDn ? `下方 ${Math.round(spot - dn)} 點內有 Put 牆` : ''}(你的停利是 40 點)</div>` : '';
  g.innerHTML = `<div class="gc px"><small>NQ 現價</small><b>${fP(spot)}</b></div>`
    + cell('上方 Call 牆', up, '含 0DTE 的未平倉量算出的最近上方 Call 牆;「+N 點」是離現價的距離,40 點內會變黃。')
    + cell('下方 Put 牆', dn, '含 0DTE 的未平倉量算出的最近下方 Put 牆;「−N 點」是離現價的距離,40 點內會變黃。')
    + cell('Flip', lv.flip, '全部到期日的 gamma flip(未平倉量)')
    + cell('0DTE flip', z.flip, '只看當天到期的 gamma flip')
    + reg + '<div class="gc clk" id="gClock"></div>' + note;
  tickClock();
}
/* 訂單流狀態燈(Bookmap → VPS):即時 / 延遲 / 斷線 / 未連線 */
function updateOfPill() {
  const p = $('#ofpill'); if (!p) return;
  const of = D.status && D.status.orderflow;
  if (!of) { p.hidden = true; return; }
  p.hidden = false;
  let cls = '', txt = '';
  if (!of.connected) { cls = ''; txt = '訂單流 未連線'; p.title = 'VPS 還沒收到過 Bookmap 的資料(Windows 那台的 addon 沒在送)'; }
  else if (of.available) {
    if (of.lag_sec != null && of.lag_sec > 3) { cls = 'warn'; txt = `訂單流 停頓 ${Math.round(of.lag_sec)} 秒`; p.title = 'Bookmap 事件停頓,數字可能落後'; }
    else { cls = 'live'; txt = `訂單流 即時 · ${Math.round(of.age_sec || 0)} 秒前`; p.title = 'Bookmap 訂單流正在送資料'; }
  } else { cls = 'bad'; txt = `訂單流 斷線 · ${fDur(of.age_sec || 0)}前`; p.title = 'Bookmap 已停止送資料,或延遲過高;導師結論此時不含訂單流'; }
  p.className = 'pill ' + cls; $('span', p).textContent = txt;
}
setInterval(tickClock, 1000);

/* ---- live polling ---- */
async function poll() {
  try {
    const st = await api('status');
    const first = !D.status;
    D.status = st;
    updatePill(); updateOfPill(); updateGlance();
    if (!st.latest && !(st.bars && st.bars.last_t)) { showEmpty(); return; }
    if (first) {
      D.days = (await api('days')).days; fillDays();
      await loadAll(true);
      D.lastLatest = st.latest;
    } else if (st.latest !== D.lastLatest) {
      D.lastLatest = st.latest;
      if (!S.day) await loadAll(false); else renderSide();
    }
    if (!S.day) { await pollBars(); loadEvents(false); loadFlowEvents(); }
    updateGlance();
    if (!$('#view-anly').classList.contains('hidden') && st.latest !== D.anlyTs) loadAnalysis();
    if (!$('#view-table').classList.contains('hidden') && st.latest !== D.flowTs) loadFlow();
    if (st.latest !== D.thesisTs) loadThesis();
    renderSide();
  } catch (e) {
    console.error(e);
    const net = e instanceof TypeError && /fetch|network|load failed/i.test(String(e.message));
    setPill('warn', net ? '連不上伺服器' : '頁面程式錯誤:' + String(e.message).slice(0, 60));
    if (!net) toast('錯誤:' + e.message + (e.stack ? ' @ ' + String(e.stack).split('\n')[1] : ''));
  }
}
async function pollBars() {
  if (D.kind !== 'candles' || !D.barT.length) return;
  const tf = S.tf, lastT = D.barT[D.barT.length - 1];
  const r = await api('bars', { from: lastT - tf * 180, to: Math.floor(Date.now() / 1000) + 3600 * 6, tf });
  if (r.kind !== 'candles') return;
  let added = false;
  for (const [t, o, h, l, c] of r.data) {
    if (t < lastT) continue;
    if (o == null || h == null || l == null || c == null) continue;
    candle.update({ time: t, open: o, high: h, low: l, close: c });
    if (t > D.barT[D.barT.length - 1]) { D.barT.push(t); D.bars.push({ time: t, open: o, high: h, low: l, close: c }); added = true; }
    else D.bars[D.bars.length - 1] = { time: t, open: o, high: h, low: l, close: c };
    D.last = c;
  }
  if (added) rebuildAligned();
  updateLegend();
}
function showEmpty() {
  const e = $('#empty'); e.hidden = false;
  const v = D.status ? collectorView(D.status.collector) : null;
  e.textContent = '';
  if (v && (v.sev === 'bad' || v.sev === 'warn')) {
    e.append('資料庫裡還沒有快照。', document.createElement('br'), '原因請看上方提示。');
  } else {
    e.append('資料庫裡還沒有快照。', document.createElement('br'), '請先啟動收集器(start_gex.command),等第一輪完成後這裡就會出現資料。');
  }
}
function setPill(cls, text) { const p = $('#live'); p.className = 'pill ' + cls; $('span', p).textContent = text; const e = D.status && D.status.bars && D.status.bars.bars_err; p.title = e ? 'K棒來源訊息:' + e : ''; }
const fDur = (sec) => sec < 90 ? `${Math.round(sec)} 秒` : sec < 5400 ? `${Math.round(sec / 60)} 分鐘` : `${(sec / 3600).toFixed(1)} 小時`;
/* 收集器現在的狀況 → {sev: bad|warn|info|ok, short, text}。收集器沒有狀態檔(舊版)就回 null,退回舊的判斷。 */
function collectorView(c) {
  if (!c) return null;
  const dur = fDur(c.since_sec || 0);
  if (c.phase === 'dead') return { sev: 'bad', short: '收集器已停止', text: `收集器沒有回應(${fDur(c.age_sec || 0)}沒有更新狀態)· GEX 不會更新,請檢查伺服器上的 gex-collector 服務` };
  if (c.phase === 'connecting') return { sev: 'bad', short: 'IBKR 未連線', text: `連不上 IBKR:Gateway 可能沒登入、被登出或 API 沒開(已 ${dur},重試 ${c.retries || 0} 次)· GEX 不會更新` };
  if (c.link === 'lost') return { sev: 'bad', short: 'IBKR 連線中斷', text: `IBKR 連線中斷:${c.link_msg || '等待恢復'} · GEX 可能不會更新` };
  if (c.phase === 'building') return { sev: 'warn', short: '建立清單中', text: `${c.detail} · 已 ${dur}(開盤後重建約需 20–30 分鐘)· 這段時間 GEX 暫時不會更新` };
  if (c.phase === 'error') return { sev: 'warn', short: '本輪收集失敗', text: `本輪收集失敗:${c.error || '原因不明'}(下一輪會再試)` };
  if (c.phase === 'starting' || c.phase === 'connected') return { sev: 'warn', short: '收集器啟動中', text: c.detail || '收集器啟動中' };
  if (c.phase === 'noquotes') return { sev: 'info', short: '沒有報價', text: c.detail || '' };
  return { sev: 'ok', short: '', text: c.detail || '' };
}
function updateBanner(v) {
  const b = $('#sbar'); if (!b) return;
  if (!v || (v.sev !== 'bad' && v.sev !== 'warn')) { b.hidden = true; return; }
  b.className = 'sbar ' + v.sev; b.textContent = v.text; b.hidden = false;
}
function updatePill() {
  const st = D.status;
  const v = st ? collectorView(st.collector) : null;
  updateBanner(v);
  const age = st && st.latest ? st.now - st.latest : null;
  const txt = age == null ? '' : age < 90 ? '剛更新' : age < 3600 ? `${Math.round(age / 60)} 分鐘前` : age < 86400 ? `${(age / 3600).toFixed(1)} 小時前` : `${Math.round(age / 86400)} 天前`;
  if (v && (v.sev === 'bad' || v.sev === 'warn')) { setPill(v.sev === 'bad' ? 'bad' : 'warn', v.short + (txt ? ` · 最新 GEX ${txt}` : '')); return; }
  if (!st || !st.latest) { setPill('', '沒有資料'); return; }
  const bars = st.bars && st.bars.bars === 'ibkr' ? 'K棒:IBKR' : D.kind === 'candles' ? 'K棒:資料庫' : 'K棒:以快照價代替';
  if (age < 1200) setPill('live', `即時 · GEX ${txt} · ${bars}`); else setPill('warn', `GEX 資料 ${txt}(休市或收集器未執行)· ${bars}`);
}

/* ------------------------------ legend & tooltip ------------------------------ */
function updateLegend(param) {
  const lg = $('#legend');
  let b = D.bars[D.bars.length - 1];
  if (param && param.time) { const i = lowerIdx(D.barT, param.time); if (i >= 0 && D.barT[i] === param.time) b = D.bars[i]; }
  const up = b && b.close >= b.open, cls = up ? 'up' : 'dn';
  const ohlc = b ? `<div class="ohlc"><b>NQ</b>${D.kind === 'candles' ? `開 <span class="${cls}">${fP(b.open)}</span>  高 <span class="${cls}">${fP(b.high)}</span>  低 <span class="${cls}">${fP(b.low)}</span>  收 <span class="${cls}">${fP(b.close)}</span>` : `<span class="${cls}">${fP(b.close)}</span> (快照價)`}</div>` : '';
  const sw = (dash, color = '#aab4c8') => `<svg width="26" height="8"><line x1="1" y1="4" x2="25" y2="4" stroke="${color}" stroke-width="2" ${dash ? `stroke-dasharray="${dash}"` : ''}/></svg>`;
  const keys = S.lines ? `<div class="keys"><span class="k">${sw('')}Call 牆</span><span class="k">${sw('2 3')}Put 牆</span><span class="k">${sw('7 4')}Flip</span>${S.srcs.map((s) => `<span class="k"><i style="width:8px;height:8px;border-radius:50%;background:${SRC_COL[s]};display:inline-block"></i>${sn(s)}</span>`).join('')}</div>` : '';
  const bandU = D.bandUpPts[D.bandUpPts.length - 1], bandD = D.bandDnPts[D.bandDnPts.length - 1];
  const band0U = D.band0UpPts[D.band0UpPts.length - 1], band0D = D.band0DnPts[D.band0DnPts.length - 1];
  const bandKeys = (S.emband && (D.bandUpPts.length || D.band0UpPts.length)) ? `<div class="keys"><span class="k">${sw('4 3', '#e7a44c')}±1σ ${bandD && bandD.value != null ? fP(bandD.value) : '—'} – ${bandU && bandU.value != null ? fP(bandU.value) : '—'}</span><span class="k">${sw('4 3', '#5eead4')}0DTE ${band0D && band0D.value != null ? fP(band0D.value) : '—'} – ${band0U && band0U.value != null ? fP(band0U.value) : '—'}</span></div>` : '';
  const N = modeNames(), lut = S.lens === 'net' ? null : LUT[S.lens];
  const stops = lut ? [0, 64, 128, 192, 255].map((i) => lut[i]).join(',') : [LUT.netNeg[255], LUT.netNeg[140], 'rgba(0,0,0,0)', LUT.netPos[140], LUT.netPos[255]].join(',');
  const lensName = { abs: N.abs, net: 'Net(正負)', call: N.c, put: N.p }[S.lens];
  const heat = S.heat ? `<div class="heatinfo"><b>${N.title}</b><span>${lensName}</span><span class="cbar" style="background:linear-gradient(90deg,${stops})"></span><span>${lut ? '低→高' : '負 ← → 正'}</span></div>` : '';
  lg.innerHTML = ohlc + heat + keys + bandKeys;
}
const tip = $('#tip');
chart.subscribeCrosshairMove((param) => {
  updateLegend(param);
  if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0) {
    tip.hidden = true; if (hoverIdx !== -1) { hoverIdx = -1; if (S.follow) { redraw(); renderSideLight(); } } subChart.clearCrosshairPosition(); return;
  }
  /* column under the mouse */
  const bi = lowerIdx(D.barT, param.time);
  let ci = -1;
  for (let i = D.cols.length - 1; i >= 0; i--) if (D.cols[i].si >= 0 && D.cols[i].si <= bi) { ci = i; break; }
  if (ci !== hoverIdx) { hoverIdx = ci; if (S.follow && !R.on) redraw(); }
  try { subChart.setCrosshairPosition(0, param.time, subSeries()); } catch (e) { /* 子圖尚無資料(剛載入、滑鼠已在圖上)時會丟 Value is null */ }
  /* tooltip */
  const price = candle.coordinateToPrice(param.point.y);
  const col = ci >= 0 ? D.cols[ci] : null;
  const bin = price != null && D.bucket ? Math.round(price / D.bucket) * D.bucket : null;
  let c = null, p = null;
  if (col && bin != null) { const j = col.bins.indexOf(bin); if (j >= 0) { c = col.c[j]; p = col.p[j]; } else { c = 0; p = 0; } }
  const bar = bi >= 0 ? D.bars[bi] : null;
  let h = `<div class="t">台北 ${hmIn(param.time, 'Asia/Taipei')} · 紐約 ${hmIn(param.time, 'America/New_York')} · ${md(param.time)}</div>`;
  if (bin != null) h += `<div class="big">${f0.format(bin - D.bucket / 2)} – ${f0.format(bin + D.bucket / 2)}</div>`;
  if (c != null) {
    const N = modeNames();
    h += `<div class="r" style="color:var(--dim)"><span>${N.title}</span><span>${N.unit}</span></div>`;
    if (S.heatMode === 'profile') h += `<div class="r"><span>${N.net}</span><span class="${c + p >= 0 ? 'up' : 'dn'}">${fM(c + p)}</span></div>`;
    else h += `<div class="r"><span>${N.c}</span><span class="up">${fM(c)}</span></div><div class="r"><span>${N.p}</span><span class="dn">${fM(p)}</span></div>`
      + `<div class="r"><span>${N.net}</span><span>${fM(c + p)}</span></div><div class="r"><span>${N.abs}</span><span>${fM(c - p).replace('+', '')}</span></div>`;
  } else if (col == null) h += '<div class="r"><span>這個時間沒有 GEX 快照</span></div>';
  if (bar && D.kind === 'candles') h += `<div class="r" style="margin-top:6px;padding-top:6px;border-top:1px solid var(--line)"><span>該根 K 棒</span><span>${fP(bar.low)} – ${fP(bar.high)}</span></div>`;
  tip.innerHTML = h; tip.hidden = false;
  const w = $('.chartwrap'), tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = param.point.x + 18, y = param.point.y + 14;
  if (x + tw > w.clientWidth - 90) x = param.point.x - tw - 18;
  if (y + th > w.clientHeight - 10) y = w.clientHeight - th - 10;
  tip.style.left = Math.max(6, x) + 'px'; tip.style.top = Math.max(6, y) + 'px';
});
const renderSideLight = () => {};

/* ------------------------------ side panel ------------------------------ */
function asOfLevel(src, kind, t) {
  const s = D.levels[src]; if (!s || !s.t.length) return null;
  const i = lowerIdx(s.t, t); return i < 0 ? null : s[kind][i];
}
function nowCtx() {
  if (R.on && D.cols[R.idx]) {
    const c = D.cols[R.idx];
    const b = c.si >= 0 ? D.bars[c.si] : null;
    return { replay: true, t: c.t, price: b ? b.close : c.spot, get: (src, kind) => asOfLevel(src, kind, c.t) };
  }
  return { replay: false, t: D.status && D.status.latest, price: D.last, get: (src, kind) => { const lv = latestLevel(src); return lv ? lv[kind] : null; } };
}
let bookT = 0;
function renderSide() {
  const cx = nowCtx(), st = D.status;
  const wantT = R.on && D.cols[R.idx] ? D.cols[R.idx].t : 0;
  if (wantT !== bookT) { bookT = wantT; loadBookSoon(); }
  /* price card */
  const first = D.bars.length ? D.bars[0].open : null;
  const chg = cx.price != null && first != null ? cx.price - first : null;
  $('#cardPrice').innerHTML = `<h3>NQ ${cx.replay ? '<small>回放中</small>' : '<small>最新</small>'}</h3>
    <div class="px"><span class="n">${fP(cx.price)}</span><span class="d ${chg >= 0 ? 'up' : 'dn'}">${chg == null ? '' : fSigned(chg, 2) + ' (' + fSigned(chg / first * 100, 2) + '%)'}</span></div>
    <div class="sub">${cx.t ? `GEX 快照 ${mdhm(cx.t)} (${S.tz === 'Asia/Taipei' ? '台北' : S.tz === 'UTC' ? 'UTC' : '紐約'}時間)` : '—'} · 區間起點起算</div>`;
  /* regime card */
  let rows = '';
  for (const src of S.srcs) {
    const flip = cx.get(src, 'flip'), price = cx.price;
    const dist = flip != null && price != null ? price - flip : null;
    const info = st && st.sources && st.sources[src];
    const netKey = (S.basis === 'oi' ? 'net_oi' : 'net_vol') + (S.scope === 'ex0' ? '_ex0' : '');
    const net = !cx.replay && info && S.scope !== 'zero' ? info[netKey] / 1e6 : null;
    const badge = dist == null ? '' : dist >= 0 ? '<span class="badge above">flip 上方</span>' : '<span class="badge below">flip 下方</span>';
    rows += `<div class="srcrow"><span class="nm"><i style="background:${SRC_COL[src]}"></i>${sn(src)}</span>
      <span class="fl">${flip == null ? '無 flip' : fP(flip)}</span>${badge}
      <span class="meta">${dist == null ? '這個設定下沒有 flip' : `價格比 flip ${dist >= 0 ? '高' : '低'} ${f0.format(Math.abs(dist))} 點`}${net != null ? ` · Net GEX ${fM(net)}` : ''}</span></div>`;
  }
  $('#cardRegime').innerHTML = `<h3>位置<small>${S.basis === 'oi' ? '未平倉量' : '成交量'} · ${{ all: '全部', ex0: '排除0DTE', zero: '只0DTE' }[S.scope]}</small></h3>${rows || '<div class="sub">請選擇至少一個來源</div>'}
    <div class="note">依慣例的解讀:價格在 flip 上方,造市商傾向壓低波動;在下方則傾向放大波動。這是模型假設,不是已驗證的規律。</div>`;
  /* nearest levels */
  const items = [];
  for (const src of S.srcs) for (const [kind, name] of KINDS) {
    const v = cx.get(src, kind); if (v == null || cx.price == null) continue;
    items.push({ src, name, v, d: v - cx.price, kind });
  }
  items.sort((a, b) => Math.abs(a.d) - Math.abs(b.d));
  const top = items.slice(0, 9);
  const mx = Math.max(1, ...top.map((x) => Math.abs(x.d)));
  $('#cardLevels').innerHTML = `<h3>最近的關卡<small>離現價由近到遠</small></h3>` + (top.length ? top.map((x) =>
    `<div class="lv"><i style="background:${SRC_COL[x.src]};${x.kind === 'put' ? 'opacity:.7' : ''}"></i><span class="nm">${sn(x.src)} ${x.name}</span><span class="p">${fP(x.v)}</span>
     <span class="dist ${x.d >= 0 ? 'up' : 'dn'}">${fSigned(x.d, 0)} 點</span></div>`).join('') : '<div class="sub">這個設定下沒有關卡</div>');
}

/* ------------------------------ replay ------------------------------ */
function updateReplayBounds() {
  const s = $('#rSlider'), n = D.cols.length;
  s.max = Math.max(0, n - 1);
  if (R.idx > n - 1) R.idx = Math.max(0, n - 1);
  s.disabled = !R.on || n === 0; $('#rPlay').disabled = !R.on || n === 0;
  s.value = R.idx; updateReplayLabel();
}
function updateReplayLabel() {
  const c = D.cols[R.idx];
  $('#rTime').textContent = R.on && c ? `${mdhm(c.t)} · ${R.idx + 1}/${D.cols.length}` : '—';
}
function setReplay(on) {
  R.on = on; $('#rToggle').classList.toggle('on', on);
  if (on) { if (D.cols.length) R.idx = D.cols.length - 1; } else stopPlay();
  updateReplayBounds(); redraw(); renderSide();
}
function stopPlay() { if (R.timer) { clearInterval(R.timer); R.timer = null; } $('#rPlay').innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14"><path d="M3 2l9 5-9 5z" fill="currentColor"/></svg>'; }
function togglePlay() {
  if (R.timer) { stopPlay(); return; }
  if (R.idx >= D.cols.length - 1) R.idx = 0;
  $('#rPlay').innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14"><path d="M3 2h3v10H3zM8 2h3v10H8z" fill="currentColor"/></svg>';
  const speed = +$('#rSpeed').value;
  R.timer = setInterval(() => {
    if (R.idx >= D.cols.length - 1) { stopPlay(); return; }
    R.idx++; $('#rSlider').value = R.idx; updateReplayLabel(); redraw(); renderSide();
  }, 700 / speed);
}
$('#rToggle').onclick = () => setReplay(!R.on);
$('#rPlay').onclick = togglePlay;
$('#rSlider').oninput = (e) => { R.idx = +e.target.value; updateReplayLabel(); redraw(); renderSide(); };
$('#rSpeed').onchange = () => { if (R.timer) { stopPlay(); togglePlay(); } };

/* ------------------------------ controls ------------------------------ */
function syncUI() {
  updateTbSum();
  $$('.seg').forEach((seg) => {
    const key = seg.dataset.key;
    $$('button', seg).forEach((b) => {
      const v = b.dataset.v;
      b.classList.toggle('on', key === 'range' ? (!S.day && +v === S.hours) : String(S[key]) === v);
    });
  });
  $$('#srcs button').forEach((b) => b.classList.toggle('on', S.srcs.includes(b.dataset.v)));
  $$('#layers button').forEach((b) => b.classList.toggle('on', !!S[b.dataset.v]));
  $('#bucket').value = String(S.bucket); $('#tz').value = S.tz;
  $('#candleStyle').value = S.candle; $('#alpha').value = S.alpha; $('#compress').value = S.compress; $('#follow').checked = S.follow; $('#bandW').value = S.bandW; $('#bandA').value = S.bandA;
  $('#daysel').value = S.day || '';
  $('#subwrap').classList.toggle('off', !S.sub);
  applySubSize();
  $$('#subtabs button').forEach((b) => b.classList.toggle('on', S.subMode === b.dataset.v));
  updateLensLabels();
  $('#gridH').checked = S.gridH; $('#gridV').checked = S.gridV; $('#cross').value = S.cross; $('#pmode').value = String(S.pmode);
  $('#auto').checked = S.auto; $('#pline').checked = S.pline; $('#llabels').checked = S.llabels; $('#stay').checked = S.stay;
  $('#lw').value = String(S.lw); $('#roff').value = S.roff;
  $('#lcNQ').value = S.lineCol.NQ; $('#lcQQQ').value = S.lineCol.QQQ; $('#lcNDX').value = S.lineCol.NDX; $('#lcWGT').value = S.lineCol.WGT;
  $('#setWrap').hidden = !S.sets; $('#setCaret').textContent = S.sets ? '▾' : '▸';
  $$('#anlyTabs button').forEach((b) => b.classList.toggle('on', S.anly === b.dataset.v));
  $$('#anlySkewSrc button').forEach((b) => b.classList.toggle('on', S.anlySkewSrc === b.dataset.v));
}
const LENS_LABELS = {
  strike: [['abs', '集中度', '|Call|+|Put|:哪些價位的選擇權最集中'], ['net', 'Net', 'Call + Put(Put 為負)'], ['call', 'Call', '只看 Call 端'], ['put', 'Put', '只看 Put 端']],
  profile: [['abs', '壓力強度', '不分正負,哪些價位的 GEX 絕對值大'], ['net', '正負分色', '正 GEX(綠)與負 GEX(紅)分開上色'], ['call', '只看正', '只看正 GEX 的價位'], ['put', '只看負', '只看負 GEX 的價位']],
  charm: [['abs', '強度', '不分方向,Charm 絕對值大的價位'], ['net', '正負分色', '會讓 delta 增加(綠)或減少(紅)'], ['call', 'Call 端', '只看 Call 端'], ['put', 'Put 端', '只看 Put 端']],
};
function updateLensLabels() {
  $$('#lensSeg button').forEach((b, i) => { const L = LENS_LABELS[S.heatMode][i]; b.dataset.v = L[0]; b.textContent = L[1]; b.title = L[2]; b.classList.toggle('on', S.lens === L[0]); });
}
const reload = debounce(() => loadAll(false), 150);
$$('.seg').forEach((seg) => seg.addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  const key = seg.dataset.key, v = b.dataset.v;
  if (key === 'range') { S.hours = +v; S.day = null; save(); syncUI(); loadAll(true); loadAnalysis(); return; }
  if (key === 'anlyPcrBasis' || key === 'anlyDivBasis' || key === 'anlyDivScope') { S[key] = v; save(); syncUI(); anlyMeta(); loadAnalysis(); return; }
  S[key] = key === 'tf' ? +v : v;
  if (key === 'heatMode') S.lens = v === 'strike' ? 'abs' : 'net';
  save(); syncUI();
  if (key === 'lens') { computeVmax(); redraw(); updateLegend(); } else if (key === 'tf') { applyChartPrefs(); loadAll(true); }
  else if (key === 'heatMode' || key === 'heatSrc') { loadHeat(); updateLegend(); }
  else { reload(); }
}));
$('#srcs').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  const v = b.dataset.v, has = S.srcs.includes(v);
  if (has && S.srcs.length === 1) return;
  S.srcs = has ? S.srcs.filter((x) => x !== v) : [...S.srcs, v].sort((a, b2) => SRC_ORDER.indexOf(a) - SRC_ORDER.indexOf(b2));
  save(); syncUI(); reload();
});
$('#layers').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  const k = b.dataset.v; S[k] = !S[k]; save(); syncUI();
  if (k === 'lines') { for (const key in lineSeries) lineSeries[key].applyOptions({ visible: S.lines }); updateLegend(); }
  if (k === 'emband') { bandUp.applyOptions({ visible: S.emband, lastValueVisible: S.emband }); bandDn.applyOptions({ visible: S.emband, lastValueVisible: S.emband }); band0Up.applyOptions({ visible: S.emband, lastValueVisible: S.emband }); band0Dn.applyOptions({ visible: S.emband, lastValueVisible: S.emband }); }
  if (k === 'sub') requestAnimationFrame(() => { chart.timeScale().applyOptions({}); });
  if (k === 'events') { evPrim.redraw(); updateBell(); }
  if (k === 'sqrib' && S.sqrib) loadSq();
  if (k === 'mmarks') loadMentorMarks();
  if (k === 'flmark') { flowEvPrim.redraw(); updateFeLegend(); if (S.flmark) loadFlowEvents(); }
  if (k === 'mscene') applyChartPrefs();
  redraw(); updateLegend();
});
function updateTbSum() {
  const el = $('#tbsum'); if (!el) return;
  const rng = S.day ? S.day : (S.hours >= 24 ? (S.hours / 24) + 'D' : S.hours + 'H');
  const sc = { all: '含 0DTE', ex0: '排除 0DTE', zero: '只看 0DTE' }[S.scope] || S.scope;
  el.textContent = [S.srcs.map(sn).join('+'), S.basis === 'vol' ? '成交量' : '未平倉量', sc, S.tf + ' 分 K'].join(' · ');
  const tb = $('#toolbar'); tb.classList.toggle('collapsed', !S.tbOpen);
  $('#tbToggle').textContent = S.tbOpen ? '設定 ▴' : '設定 ▾';
}
$('#tbToggle').addEventListener('click', () => { S.tbOpen = !S.tbOpen; save(); updateTbSum(); requestAnimationFrame(() => { try { chart.timeScale().applyOptions({}); window.dispatchEvent(new Event('resize')); } catch (e) {} }); });
$('#preset').addEventListener('click', () => {
  Object.assign(S, { hours: 2, day: null, srcs: ['WGT'], scope: 'all', tf: 1, mmarks: true, lines: true, follow: true });
  save(); syncUI(); loadAll(true); loadAnalysis(); toast('已切到盤中模式:近 2 小時 · 加權合計 · 含 0DTE · 1 分 K');
});
$('#subtabs').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setSubMode(b.dataset.v); });
$('#anlyTabs').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setAnly(b.dataset.v); });
$('#anlySkewSrc').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  S.anlySkewSrc = b.dataset.v; save(); syncUI(); anlyMeta(); loadAnalysis();
});
$('#mentorRefresh').addEventListener('click', loadThesis);
/* 導師頁的收合區塊:點標題展開 / 收起 */
$$('#mentorCard .msec.fold > h4').forEach((h4) => h4.addEventListener('click', () => h4.parentElement.classList.toggle('closed')));


/* ---- 價位階梯圖(導師頁 / 問導師共用):把導師提到的價位、現價、方向與判定窗畫成一張圖 ---- */
function renderLadder(el, spec) {
  if (!el) return;
  const spot = spec.spot, all = (spec.levels || []).filter((l) => l.p != null && isFinite(l.p));
  const NEAR = 110;   // 只畫現價 ±110 點內的價位,太遠的會把有用的區間壓扁,改列在圖下方
  const lv = spot == null ? all : all.filter((l) => Math.abs(l.p - spot) <= NEAR), far = spot == null ? [] : all.filter((l) => Math.abs(l.p - spot) > NEAR);
  if (spot == null || !all.length) { el.innerHTML = '<span class="dim">資料不足,畫不出圖</span>'; return; }
  const dirUp = spec.dir === '偏多', dirDn = spec.dir === '偏空', sg = dirUp ? 1 : -1;
  const ps = lv.map((l) => l.p).concat([spot]);
  if (dirUp || dirDn) ps.push(spot + SCENE_X, spot - SCENE_X);
  let lo = Math.min(...ps), hi = Math.max(...ps); const pad = Math.max(8, (hi - lo) * 0.06); lo -= pad; hi += pad;
  const W = 340, H = 300, top = 10, bot = H - 10, Xl = 118, Xr = 236;
  const Y = (p) => bot - (p - lo) / (hi - lo) * (bot - top);
  const colOf = (k) => ({ call: DN, wall: DN, res: DN, put: UP, sup: UP, flip: '#f8fafc', band: '#7d90a3', band0: '#9fb0c3' }[k] || '#ffd166');
  // 標籤避讓
  const rows = lv.map((l) => ({ ...l, y: Y(l.p), ly: Y(l.p) })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < rows.length; i++) if (rows[i].ly - rows[i - 1].ly < 13) rows[i].ly = rows[i - 1].ly + 13;
  let g = `<svg viewBox="0 0 ${W} ${H}" class="ladder" role="img" aria-label="導師價位階梯圖">`;
  if (dirUp || dirDn) {
    const col = dirUp ? UP : DN, opp = dirUp ? DN : UP, yT = Y(spot + sg * SCENE_X), yS = Y(spot - sg * SCENE_X), y0 = Y(spot);
    g += `<rect x="${Xl}" y="${Math.min(y0, yT)}" width="${Xr - Xl}" height="${Math.abs(yT - y0)}" fill="${col}" opacity=".16"/>`;
    g += `<rect x="${Xl}" y="${Math.min(y0, yS)}" width="${Xr - Xl}" height="${Math.abs(yS - y0)}" fill="${opp}" opacity=".12"/>`;
    g += `<line x1="${Xl}" x2="${Xr}" y1="${yT}" y2="${yT}" stroke="${col}" stroke-width="1.4"/><line x1="${Xl}" x2="${Xr}" y1="${yS}" y2="${yS}" stroke="${opp}" stroke-width="1.4" stroke-dasharray="5 4"/>`;
    const ay = y0 + (yT - y0) * 0.88, ax = (Xl + Xr) / 2, tb = ay + (dirUp ? 9 : -9);
    g += `<line x1="${ax}" x2="${ax}" y1="${y0}" y2="${ay}" stroke="${col}" stroke-width="2.2"/><path d="M${ax} ${ay} L${ax - 6} ${tb} L${ax + 6} ${tb} Z" fill="${col}"/>`;
    g += `<text x="${Xr + 6}" y="${yT + 4}" fill="${col}" font-size="10.5">判對 ${(spot + sg * SCENE_X).toFixed(0)}</text><text x="${Xr + 6}" y="${yS + 4}" fill="${opp}" font-size="10.5">判錯 ${(spot - sg * SCENE_X).toFixed(0)}</text>`;
  }
  for (const r of rows) {
    const c = colOf(r.kind);
    g += `<line x1="${Xl - 8}" x2="${Xr}" y1="${r.y}" y2="${r.y}" stroke="${c}" stroke-width="1" ${r.kind === 'band' || r.kind === 'band0' ? 'stroke-dasharray="3 3"' : ''} opacity=".85"/>`;
    g += `<text x="${Xl - 12}" y="${r.ly + 4}" text-anchor="end" fill="${c}" font-size="10.5">${esc(r.label)}</text>`;
    if (!(dirUp || dirDn)) g += `<text x="${Xr + 6}" y="${r.ly + 4}" fill="${c}" font-size="10.5">${Math.round(r.p)}</text>`;
    else g += `<text x="${W - 2}" y="${r.ly + 4}" text-anchor="end" fill="${c}" font-size="10" opacity=".9">${Math.round(r.p)}</text>`;
  }
  const ys = Y(spot);
  g += `<line x1="${Xl - 8}" x2="${Xr}" y1="${ys}" y2="${ys}" stroke="#fff" stroke-width="2"/><circle cx="${Xl}" cy="${ys}" r="4" fill="#fff"/><text x="${Xl - 12}" y="${ys - 6}" text-anchor="end" fill="#fff" font-size="11" font-weight="700">現價 ${spot.toFixed(0)}</text>`;
  g += '</svg>';
  const farTxt = far.length ? `<div class="ladFar">較遠(未畫在圖上):${far.sort((a, b) => b.p - a.p).map((l) => esc(l.label) + ' ' + Math.round(l.p)).join(' · ')}</div>` : '';
  const head = (!spec.noHead && (dirUp || dirDn)) ? `<div class="ladHead ${dirUp ? 'up' : 'dn'}">${dirUp ? '↑' : '↓'} ${spec.dir}${spec.conf != null ? ' · 信心 ' + spec.conf + '%' : ''}<small>箭頭方向的色塊 = 順向 ${SCENE_X} 點(判對);另一側 = 反向 ${SCENE_X} 點(判錯)· ${SCENE_MIN} 分鐘內先到者為準 · 命中率尚未驗證</small></div>` : '';
  const note = (spec.noHead && (dirUp || dirDn)) ? `<div class="ladFar">箭頭方向的色塊 = 順向 ${SCENE_X} 點(判對);另一側 = 反向 ${SCENE_X} 點(判錯)· ${SCENE_MIN} 分鐘內先到者為準 · 命中率尚未驗證</div>` : '';
  el.innerHTML = head + g + note + farTxt;
}
function thesisLevels(T) {
  const kl = T.key_levels || {}, o = [];
  const add = (p, kind, label) => { if (p != null) o.push({ p, kind, label }); };
  add(kl.call, 'call', 'Call 牆'); add(kl.put, 'put', 'Put 牆'); add(kl.flip, 'flip', 'Flip');
  add(kl.flip0, 'flip', '0DTE flip'); add(kl.band_up, 'band', '±1σ 上'); add(kl.band_dn, 'band', '±1σ 下');
  add(kl.band0_up, 'band0', '0DTE 上'); add(kl.band0_dn, 'band0', '0DTE 下');
  return o;
}

/* ---- AI 導師(原始數據版):手動觸發 → /api/aimentor;紀錄與成績來自 /api/daily(kind='ai') ---- */
D.ai = null;
const ROLE_KIND = { support: 'sup', resistance: 'res', pivot: 'pivot' };
function aiLevelsOf(v) {
  const lv = ((v && v.key_levels) || []).map((l) => ({ p: l.p, kind: ROLE_KIND[l.role] || 'pivot', label: l.label || '' }));
  if (v && v.invalid_price != null) lv.push({ p: v.invalid_price, kind: 'inv', label: 'AI 認錯價' });
  return lv;
}
function setAiFromItem(x) {   // x: {t, spot, direction, confidence, invalid, ...}
  D.ai = x && (x.direction === '偏多' || x.direction === '偏空') ? x : null;
  scenePrim.redraw();
}
function renderAiResult(r) {
  const out = $('#aiOut'); if (!out) return;
  const v = r.verdict;
  const cls = v && v.direction === '偏多' ? 'up' : v && v.direction === '偏空' ? 'dn' : 'na';
  const arrow = cls === 'up' ? '↑ ' : cls === 'dn' ? '↓ ' : '';
  let h = '';
  if (v) {
    h += `<div class="aiVerdict ${cls}">${arrow}${esc(v.direction)}${v.confidence != null ? `<small>AI 自評信心 ${v.confidence}%(它自己說的,尚未驗證)</small>` : ''}</div>`;
    if (v.one_line) h += `<div class="aiOne">${esc(v.one_line)}</div>`;
    h += '<div id="aiLadder"></div>';
  } else {
    h += `<div class="aiWarn">這次回答沒有附上可解析的判定區塊(${esc(r.parse_error || '原因不明')}),所以不會被計入成績,也不會畫在圖上。以下是原文:</div>`;
  }
  h += `<div class="aiText">${esc(r.text || '').replace(/\n{3,}/g, '\n\n')}</div>`;
  const u = r.usage || {};
  h += `<div class="aiMeta">${esc(r.model || '')} · 輸入 ${u.prompt_tokens != null ? u.prompt_tokens.toLocaleString() : '?'} / 輸出 ${u.completion_tokens != null ? u.completion_tokens.toLocaleString() : '?'} tokens · ${Math.round((r.ms || 0) / 1000)} 秒${r.cost_usd != null ? ` · 約 $${r.cost_usd}(依 flash 非尖峰價估算)` : ''} · 訂單流:${esc(r.orderflow || '?')}${r.finish && r.finish !== 'stop' ? ` · <span class="aiWarn">結束原因:${esc(r.finish)}(可能被截斷)</span>` : ''}</div>`;
  out.innerHTML = h;
  if (v) renderLadder($('#aiLadder'), { spot: r.spot, levels: aiLevelsOf(v), dir: v.direction, conf: v.confidence, noHead: true });
}
async function runAiMentor() {
  const btn = $('#aiGo'), st = $('#aiStatus');
  btn.disabled = true; st.classList.remove('err');
  const t0 = Date.now();
  const tick = setInterval(() => { st.textContent = `AI 導師整理原始數據中… 已等 ${Math.round((Date.now() - t0) / 1000)} 秒(通常 30–90 秒)`; }, 1000);
  st.textContent = 'AI 導師整理原始數據中…';
  try {
    const r = await api('aimentor', { q: ($('#aiQ').value || '').trim() });
    renderAiResult(r);
    st.textContent = `完成 · 台北 ${hmIn(r.t, 'Asia/Taipei')} · 現價 ${r.spot}`;
    if (r.verdict) setAiFromItem({ t: r.t, spot: r.spot, direction: r.verdict.direction, confidence: r.verdict.confidence, invalid: r.verdict.invalid_price });
    loadAiHist(); loadMentorMarks();
  } catch (e) {
    st.classList.add('err'); st.textContent = '失敗:' + e.message;
  } finally { clearInterval(tick); btn.disabled = false; }
}
async function loadAiHist() {
  const el = $('#aiHist'); if (!el) return;
  try {
    const r = await api('daily', { day: nyDayOf(Date.now() / 1000), scope: 'all' });
    if (!r || !r.ok) { el.textContent = '今天還沒有紀錄'; return; }
    const items = r.timeline.filter((x) => x.kind === 'ai');
    const S2 = r.stats || {};
    const g = S2.ai || {};
    const line = (name, q) => q && q.n ? `${name}:${q.hit} 中 / ${q.miss} 錯(命中率 ${q.rate == null ? '—' : q.rate + '%'};逾時 ${q.timeout}、同棒 ${q.both})` : '';
    let h = '';
    if (items.length) {
      h += `<div class="dlyNote">${[line('AI 導師', g), line('同一批時間點「永遠做多」', S2.ai_always_long), line('同一批「永遠做空」', S2.ai_always_short), line('規則版(盤面指標)同日', S2.thesis)].filter(Boolean).join('<br>')}<br>損益兩平命中率約 ${S2.breakeven}%。一天樣本很少,只是描述;要累積多天才有意義。</div>`;
    }
    h += items.slice().reverse().map((x) => {
      const cls = x.verdict === '命中' ? 'ok' : x.verdict === '未命中' ? 'bad' : '';
      return `<details class="dlyItem"><summary><b>#${x.id}</b> 台北 ${x.tw} · NQ ${x.spot != null ? Math.round(x.spot) : '—'} · ${esc(x.direction || '觀望')}${x.confidence != null ? ' ' + x.confidence + '%' : ''} <span class="dlyV ${cls}">${esc(x.verdict)}${x.mfe != null ? ` · 順 ${x.mfe}/逆 ${x.mae}` : ''}</span></summary><div class="dlyText">${esc(x.one_line ? x.one_line + '\n\n' : '')}${esc(x.text).replace(/\n/g, '<br>')}</div></details>`;
    }).join('');
    el.innerHTML = h || '今天還沒有 AI 導師的紀錄(按上面的按鈕)';
    // 熱圖上畫最近一次(只畫 30 分鐘內的)
    const last = items.length ? items[items.length - 1] : null;
    if (last && Date.now() / 1000 - last.t <= 45 * 60) setAiFromItem({ t: last.t, spot: last.spot, direction: last.direction, confidence: last.confidence, invalid: last.invalid_price });
    else setAiFromItem(null);
  } catch (e) { el.textContent = '讀取失敗:' + e.message; }
}
$('#aiGo').addEventListener('click', runAiMentor);
$('#aiQ').addEventListener('keydown', (e) => { if (e.key === 'Enter') runAiMentor(); });
setTimeout(loadAiHist, 0);
setInterval(() => { if (D.ai && Date.now() / 1000 - D.ai.t > 45 * 60) setAiFromItem(null); }, 60000);

/* ---- 導師日結:讀 /api/daily(評論存檔 + 程式結算的 ±40 命中統計),AI 只負責把當天串成文字 ---- */
function esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
async function loadDaily(gen) {
  const out = $('#dailyOut');
  const day = $('#dailyDay').value, scope = $('#dailyScope').value;
  out.textContent = gen ? '導師整理中…(會呼叫一次 AI)' : '讀取中…';
  try {
    const q = { scope }; if (day) q.day = day; if (gen) q.gen = '1';
    const r = await api('daily', q);
    if (!r.ok) { out.textContent = r.error || '沒有資料'; return; }
    if (!day) $('#dailyDay').value = r.day;
    const S = r.stats, T = S.thesis;
    const row = (name, g) => g && g.n ? `<tr><td>${name}</td><td>${g.n}</td><td>${g.hit}</td><td>${g.miss}</td><td>${g.timeout}</td><td>${g.both}</td><td><b>${g.rate == null ? '—' : g.rate + '%'}</b></td></tr>` : '';
    const hint = (r.n === 0 && r.n_all > 0) ? `<div class="dlyNote">這個時段內沒有評論;這一天全天另有 ${r.n_all} 則(不在你的交易時段內),把右邊改成「全天」就能看到。</div>` : '';
    let h = `<div class="dlyHead">${esc(r.day)} · 存下 ${r.n} 則(自動結論 ${r.n_thesis}、待結算 ${r.pending}) · 判定 ±${r.X} 點 / ${r.hold} 分鐘 · 損益兩平命中率約 ${S.breakeven}%</div>`;
    h += hint;
    h += '<table class="dlyTbl"><tr><th>組別</th><th>則數</th><th>命中</th><th>未命中</th><th>逾時</th><th>同棒</th><th>命中率*</th></tr>'
      + row('盤面指標(規則版,舊總分方向)', T) + row('　其中偏多', S.long) + row('　其中偏空', S.short)
      + row('方向分數(不含gamma)', S.split_dir) + row('　同批舊總分', S.legacy_same_rows)
      + row('AI 導師(原始數據版)', S.ai) + row('　其中偏多', S.ai_long) + row('　其中偏空', S.ai_short) + row('　基準:同批時間永遠做多', S.ai_always_long) + row('　基準:同批時間永遠做空', S.ai_always_short)
      + row('基準:永遠做多', S.always_long) + row('基準:永遠做空', S.always_short)
      + Object.entries(S.by_regime || {}).map(([k, g]) => row('環境:' + k, g)).join('') + '</table>';
    h += '<div class="dlyNote">*命中率 = 命中 ÷ (命中+未命中);逾時、同棒不計入。相鄰評論的 30 分鐘窗口重疊,獨立樣本遠少於則數,單日只是描述,不是評分。</div>';
    if (r.summary && r.summary.text) {
      const t = esc(r.summary.text).replace(/#(\d+)/g, '<a href="#dly$1" class="dlyLink">#$1</a>').replace(/\n/g, '<br>');
      h += `<div class="dlySum">${t}<div class="dlyNote">${r.summary.ai ? 'AI 依上方程式判定的事實整理' : '規則版文字(沒有 AI)'} · 存檔時 ${r.summary.n_comments} 則</div></div>`;
    }
    h += '<div class="dlyTl">' + r.timeline.map((x) => {
      const cls = x.verdict === '命中' ? 'ok' : x.verdict === '未命中' ? 'bad' : '';
      const head = x.kind === 'ask' ? `問:${esc(x.question || '')}` : x.kind === 'ai' ? `AI 導師 ${esc(x.direction || '觀望')}${x.confidence != null ? ' ' + x.confidence + '%' : ''}` : `${esc(x.direction)} ${x.score > 0 ? '+' : ''}${x.score}` + (x.score_dir != null ? `(方向 ${x.score_dir > 0 ? '+' : ''}${x.score_dir})` : '') + ` · ${esc(x.regime || '')}`;
      const res = x.kind === 'ask' ? '' : `<span class="dlyV ${cls}">${esc(x.verdict)}${x.mfe != null ? ` · 順 ${x.mfe}/逆 ${x.mae}` : ''}</span>`;
      return `<details class="dlyItem" id="dly${x.id}"><summary><b>#${x.id}</b> 台北 ${x.tw} · NQ ${x.spot != null ? Math.round(x.spot) : '—'} · ${head} ${res}</summary><div class="dlyText">${esc(x.text).replace(/\n/g, '<br>')}</div></details>`;
    }).join('') + '</div>';
    out.innerHTML = h;
    out.querySelectorAll('.dlyLink').forEach((a) => a.addEventListener('click', (e) => {
      e.preventDefault(); const el = document.getElementById(a.getAttribute('href').slice(1)); if (el) { el.open = true; el.scrollIntoView({ block: 'center' }); }
    }));
  } catch (e) { out.textContent = '讀取失敗:' + e.message; }
}
$('#dailyLoad').addEventListener('click', () => loadDaily(false));
$('#dailyGen').addEventListener('click', () => loadDaily(true));
$('#askSend').addEventListener('click', askMentor);
$('#askInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') askMentor(); });


/* ---- 導師評論標在 K 線上:命中綠 / 未命中紅 / 逾時或待結算灰;點標記看原文 ---- */
const nyDayOf = (t) => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date(t * 1000));
const mentorMarkApi = LW.createSeriesMarkers(candle, []);
let mentorMarkItems = [];
async function loadMentorMarks() {
  if (!S.mmarks || !D.bars.length) { mentorMarkItems = []; mentorMarkApi.setMarkers([]); hideMentorPop(); return; }
  const lo = D.bars[0].time, hi = D.bars[D.bars.length - 1].time;
  const days = [...new Set([nyDayOf(lo), nyDayOf(hi)])];
  const tok = (loadMentorMarks.tok = (loadMentorMarks.tok || 0) + 1);
  let items = [];
  try {
    for (const day of days) {
      const r = await api('daily', { day, scope: 'all' });
      if (r && r.ok) items = items.concat(r.timeline.filter((x) => x.kind === 'ai'));
    }
  } catch (e) { items = []; }
  if (tok !== loadMentorMarks.tok) return;
  const mk = [];
  mentorMarkItems = [];
  for (const x of items) {
    const bi = lowerIdx(D.barT, x.t);
    if (bi < 0 || x.t < lo - 60 || x.t > hi + 3600) continue;
    const time = D.barT[bi];
    const long = x.direction && x.direction.indexOf('多') >= 0, short = x.direction && x.direction.indexOf('空') >= 0;
    const col = x.verdict === '命中' ? UP : x.verdict === '未命中' ? DN : '#8b97a8';
    mk.push({ time, position: short ? 'aboveBar' : 'belowBar', shape: short ? 'arrowDown' : long ? 'arrowUp' : 'circle', color: col, text: 'AI#' + x.id });
    mentorMarkItems.push({ time, x });
  }
  mk.sort((a, b) => a.time - b.time);
  mentorMarkApi.setMarkers(mk);
  window.__mm = { get items() { return mentorMarkItems; }, chart, candle, D };
}
function hideMentorPop() { const el = document.getElementById('mmPop'); if (el) el.hidden = true; }
chart.subscribeClick((param) => {
  if (!S.mmarks || !param.time || !param.point) return hideMentorPop();
  const bt = D.barT[lowerIdx(D.barT, param.time)];
  const hits = mentorMarkItems.filter((m) => m.time === bt);
  if (!hits.length) return hideMentorPop();
  let el = document.getElementById('mmPop');
  if (!el) { el = document.createElement('div'); el.id = 'mmPop'; el.className = 'mmPop'; document.body.appendChild(el); }
  el.innerHTML = '<button class="mmX" aria-label="關閉">×</button>' + hits.map(({ x }) => {
    const cls = x.verdict === '命中' ? 'ok' : x.verdict === '未命中' ? 'bad' : '';
    return `<div class="mmItem"><b>${x.kind === 'ai' ? 'AI 導師 ' : ''}#${x.id}</b> 台北 ${x.tw} · ${esc(x.direction || '觀望')} ${x.kind === 'ai' ? (x.confidence != null ? '信心 ' + x.confidence + '%' : '') : (x.score > 0 ? '+' : '') + x.score + ' · ' + esc(x.regime || '')} <span class="dlyV ${cls}">${esc(x.verdict)}${x.mfe != null ? ` · 順 ${x.mfe}/逆 ${x.mae}` : ''}</span><div class="dlyText">${esc(x.text).replace(/\n/g, '<br>')}</div></div>`;
  }).join('');
  el.hidden = false;
  el.querySelector('.mmX').onclick = hideMentorPop;
});

/* ---- 導師圖層:每次問導師後覆蓋重畫,舊的整組清掉(不累積) ---- */
function clearMentorOverlay() {
  for (const pl of mentorLines) { try { candle.removePriceLine(pl); } catch (e) {} }
  mentorLines = [];
  mentorOv = null;
  redraw();
}
function drawMentorOverlay(ov) {
  clearMentorOverlay();
  if (!ov) return;
  mentorOv = ov;
  for (const lv of (ov.levels || [])) {
    if (lv.p == null) continue;
    const col = lv.kind === 'flip' ? '#f8fafc'
      : (lv.kind === 'call' || lv.kind === 'wall') ? DN
      : lv.kind === 'put' ? UP
      : lv.kind === 'band' ? '#5b7083'
      : lv.kind === 'band0' ? '#7d90a3'
      : '#ffd166';
    const solid = lv.kind === 'flip' || lv.kind === 'call' || lv.kind === 'put' || lv.kind === 'wall';
    const pl = candle.createPriceLine({ price: lv.p, color: col, lineWidth: lv.kind === 'flip' ? 2 : 1, lineStyle: solid ? 0 : 2, axisLabelVisible: true, title: lv.label });
    mentorLines.push(pl);
  }
  redraw();
}

/* 問導師:把問題 + 最新盤勢一起丟給後端 /api/ask(DeepSeek 回答)。
   語音改「按了才合成」(/api/speak),不再拖慢回答——否則長答案合成要等好幾分鐘。 */
let askAudio = null;
async function askMentor() {
  const inp = $('#askInput'), out = $('#askAnswer'), play = $('#askPlay');
  const q = (inp.value || '').trim();
  if (!q) { toast('先輸入問題'); return; }
  out.textContent = '思考中…';
  play.hidden = true;
  if (askAudio) { try { askAudio.pause(); } catch (e) {} askAudio = null; }
  try {
    const r = await api('ask', { q });
    out.textContent = r.answer || '(空回應)';
    drawMentorOverlay(r.overlay);
    renderLadder($('#askLadder'), { spot: r.spot, levels: ((r.overlay || {}).levels || []).map((l) => ({ p: l.p, kind: l.kind, label: l.label })), dir: (r.overlay || {}).direction, conf: null });
    play.hidden = false;
    play.textContent = '唸出來';
    play.disabled = false;
    play.onclick = async () => {
      play.textContent = '合成中…';
      play.disabled = true;
      try {
        const s = await api('speak', { text: r.answer || '' });
        if (s.audio) {
          askAudio = new Audio(s.audio);
          play.textContent = '唸出來';
          play.disabled = false;
          askAudio.play().catch(() => {});
        } else {
          play.textContent = '唸不出來';
          play.disabled = false;
        }
      } catch (e) {
        play.textContent = '唸出來';
        play.disabled = false;
        toast('語音失敗:' + e.message);
      }
    };
  } catch (e) {
    out.textContent = '問導師失敗:' + e.message;
  }
}
$('#bucket').onchange = (e) => { S.bucket = +e.target.value; save(); reload(); };
$('#tz').onchange = (e) => {
  S.tz = e.target.value; save();
  const tf = (t, type) => (type <= 2 ? md(t) : hm(t));
  chart.applyOptions({ timeScale: { tickMarkFormatter: tf }, localization: { timeFormatter: (t) => mdhm(t) } });
  [pcrChart, skewChart, divChart, divSpreadChart].forEach((c) => c.applyOptions({ timeScale: { tickMarkFormatter: tf }, localization: { timeFormatter: (t) => mdhm(t) } }));
  renderSide(); updateReplayLabel(); redraw();
};
$('#alpha').oninput = (e) => { S.alpha = +e.target.value; save(); redraw(); };
$('#compress').oninput = (e) => { S.compress = +e.target.value; save(); redraw(); };
$('#candleStyle').onchange = (e) => { S.candle = e.target.value; save(); applyCandleStyle(); };
$('#bandW').oninput = (e) => { S.bandW = +e.target.value; save(); redraw(); };
$('#bandA').oninput = (e) => { S.bandA = +e.target.value; save(); redraw(); };
function wLabels() {
  const t = S.wgt.NQ + S.wgt.QQQ + S.wgt.NDX || 1;
  for (const k of ['NQ', 'QQQ', 'NDX']) { $('#w' + k).value = S.wgt[k]; $('#wl' + k).textContent = Math.round(S.wgt[k] / t * 100) + '%'; }
  $('#wnorm').checked = S.wnorm;
}
const wReload = debounce(() => { if (S.srcs.includes('WGT')) loadLevels(D.token); }, 450);
['NQ', 'QQQ', 'NDX'].forEach((k) => { $('#w' + k).oninput = (e) => { S.wgt[k] = +e.target.value; save(); wLabels(); wReload(); }; });
$('#wnorm').onchange = (e) => { S.wnorm = e.target.checked; save(); wReload(); };
$('#follow').onchange = (e) => { S.follow = e.target.checked; save(); redraw(); };
$('#daysel').onchange = (e) => { S.day = e.target.value || null; save(); syncUI(); loadAll(true); };
$('#reset').onclick = () => { S.auto = true; $('#auto').checked = true; save(); chart.timeScale().fitContent(); applyChartPrefs(); };
function fillDays() {
  const sel = $('#daysel'); sel.innerHTML = '<option value="">指定日期…</option>';
  [...D.days].reverse().forEach((d) => { const o = document.createElement('option'); o.value = d.date; o.textContent = `${d.date}(${d.n} 筆)`; sel.appendChild(o); });
  sel.value = S.day || '';
}
document.addEventListener('keydown', (e) => {
  if (/INPUT|SELECT|TEXTAREA/.test(e.target.tagName) && e.target.type !== 'range') return;
  if (e.key === 'r' || e.key === 'R') $('#reset').click();
  if (R.on && e.key === 'ArrowLeft') { R.idx = Math.max(0, R.idx - 1); $('#rSlider').value = R.idx; updateReplayLabel(); redraw(); renderSide(); }
  if (R.on && e.key === 'ArrowRight') { R.idx = Math.min(D.cols.length - 1, R.idx + 1); $('#rSlider').value = R.idx; updateReplayLabel(); redraw(); renderSide(); }
  if (R.on && e.key === ' ') { e.preventDefault(); togglePlay(); }
});

/* ------------------------------ tabs & table page ------------------------------ */
$('#tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  $$('#tabs button').forEach((x) => x.classList.toggle('on', x === b));
  $('#view-map').classList.toggle('hidden', b.dataset.tab !== 'map');
  $('#view-table').classList.toggle('hidden', b.dataset.tab !== 'table');
  $('#view-anly').classList.toggle('hidden', b.dataset.tab !== 'anly');
  $('#view-mentor').classList.toggle('hidden', b.dataset.tab !== 'mentor');
  $('#view-review').classList.toggle('hidden', b.dataset.tab !== 'review');
  if (b.dataset.tab === 'table') { renderTable(); loadFlow(); }
  if (b.dataset.tab === 'anly') { anlyVis(); anlyMeta(); loadAnalysis(); setTimeout(fitAnly, 60); }
  if (b.dataset.tab === 'mentor') loadThesis();
  if (b.dataset.tab === 'review') loadDaily(false);

});
function matrixTable(basis) {
  const price = D.last, B = basis.toUpperCase(), scopes = [['ALL', '全部'], ['EX0', '排除0DTE'], ['ZERO', '只看0DTE']];
  let h = `<table class="t"><thead><tr class="grouphead"><th></th>${scopes.map(([, n]) => `<th colspan="3" class="gap">${n}</th>`).join('')}</tr>
    <tr><th>${B === 'OI' ? '未平倉量' : '成交量'}</th>${scopes.map(() => '<th class="gap">Call 牆</th><th>Put 牆</th><th>Flip</th>').join('')}</tr></thead><tbody>`;
  for (const src of ['NQ', 'QQQ', 'NDX', 'SUM']) {
    h += `<tr><td class="nm" style="color:${SRC_COL[src]}">${src === 'SUM' ? '三者合計' : src}</td>`;
    for (const [sc] of scopes) {
      const st = D.status || {};
      const lv = sc === 'ZERO' ? (st.levels_zero && st.levels_zero[src] && st.levels_zero[src][B]) : (st.levels && st.levels[src] && st.levels[src][B] && st.levels[src][B][sc]);
      for (const [i, k] of ['call', 'put', 'flip'].entries()) {
        const v = lv ? lv[k] : null;
        h += `<td class="${i === 0 ? 'gap' : ''}">${v == null ? '<span class="muted">—</span>' : `${fP(v)}<small class="${v - price >= 0 ? 'up' : 'dn'}">${price != null ? fSigned(v - price, 0) : ''}</small>`}</td>`;
      }
    }
    h += '</tr>';
  }
  return h + '</tbody></table>';
}
function carriedNote() {
  const c = D.status && D.status.carried; if (!c) return '';
  const k = Object.keys(c); if (!k.length) return '';
  return ` · 夜盤:${k.join('/')} 沿用美股盤 ${mdhm(Math.min(...k.map(x => c[x])))} 的資料`;
}
async function renderTable() {
  $('#tNow').textContent = D.status && D.status.latest ? `GEX 快照 ${mdhm(D.status.latest)} · 現價 ${fP(D.last)}(括號內為離現價的點數)` + carriedNote() : '';
  $('#tMatrix').innerHTML = matrixTable('oi') + '<div style="height:14px"></div>' + matrixTable('vol');
  try {
    const hist = (await api('history')).days.slice().reverse();
    const src = $('#hSrc').value, b = $('#hBasis').value, sc = $('#hScope').value;
    let h = `<table class="t"><thead><tr><th>日期(紐約)</th><th>匯出時間(台北)</th><th>NQ 當時價</th><th>Call 牆</th><th>Put 牆</th><th>Flip</th><th>價 − Flip</th></tr></thead><tbody>`;
    for (const d of hist) {
      const g = (k) => d.levels[`${src}|${b}|${sc}|${k}`];
      const call = g('CALL'), put = g('PUT'), flip = g('FLIP');
      h += `<tr><td class="nm">${d.date}</td><td>${d.tw_time}</td><td>${fP(d.spot)}</td><td>${fP(call)}</td><td>${fP(put)}</td><td>${fP(flip)}</td>
        <td class="${flip != null && d.spot - flip >= 0 ? 'up' : 'dn'}">${flip == null ? '—' : fSigned(d.spot - flip, 0)}</td></tr>`;
    }
    $('#tHist').innerHTML = hist.length ? h + '</tbody></table>' : '<div class="muted">還沒有每日紀錄。收集器在美股開盤後第一輪完整資料出來時會自動寫入 levels_history.txt。</div>';
  } catch (e) { $('#tHist').textContent = '讀取失敗:' + e.message; }
}
['hSrc', 'hBasis', 'hScope'].forEach((id) => $('#' + id).addEventListener('change', renderTable));

/* ---- most-active strikes (成交增量排名) ---- */
let flowHL = null, flowSort = { k: 'dv', dir: -1 };
async function loadFlow() {
  try {
    D.flow = await api('flow', { src: $('#flowSrc').value, n: +$('#flowN').value });
    D.flowTs = D.status ? D.status.latest : null;
    renderFlow();
  } catch (e) { $('#tFlow').textContent = '讀取失敗:' + e.message; }
}
function renderFlow() {
  const F = D.flow;
  if (!F || !F.rows || !F.rows.length) { $('#tFlow').innerHTML = '<div class="muted">還沒有成交增量資料(需要至少兩次快照)。</div>'; return; }
  $('#flowNow').textContent = F.t ? `快照 ${mdhm(F.t)} 起新增` : '';
  const rows = F.rows.slice().sort((a, b) => (a[flowSort.k] - b[flowSort.k]) * flowSort.dir);
  const hdr = (k, name) => `<th data-k="${k}" class="${flowSort.k === k ? 'sorted' + (flowSort.dir < 0 ? '' : ' up') : ''}" style="cursor:pointer" title="點擊排序">${name}</th>`;
  let h = `<table class="t flow"><thead><tr>${hdr('strike', '履約價')}${hdr('dv', '成交增量')}<th>Call</th><th>Put</th>${hdr('call_vol', 'Call 累計量')}${hdr('put_vol', 'Put 累計量')}${hdr('oi', '未平倉量')}<th>Call / Put 占比</th></tr></thead><tbody>`;
  for (const r of rows) {
    const cd = r.call_dv, pd = r.put_dv, tot = cd + pd || 1;
    h += `<tr data-strike="${r.strike}"><td class="nm strike">${f0.format(r.strike)}</td>
      <td><b>${f0.format(r.dv)}</b></td>
      <td class="up">${cd ? '+' + f0.format(cd) : '·'}</td><td class="dn">${pd ? '+' + f0.format(pd) : '·'}</td>
      <td>${f0.format(r.call_vol)}</td><td>${f0.format(r.put_vol)}</td><td>${f0.format(r.oi)}</td>
      <td style="min-width:130px"><div class="pc"><i style="width:${(cd / tot) * 100}%;background:var(--up)"></i><i style="width:${(pd / tot) * 100}%;background:var(--dn)"></i></div></td></tr>`;
  }
  $('#tFlow').innerHTML = h + '</tbody></table>';
  $$('#tFlow th[data-k]').forEach((th) => { th.onclick = () => { const k = th.dataset.k; flowSort = { k, dir: flowSort.k === k ? -flowSort.dir : -1 }; renderFlow(); }; });
  $$('#tFlow tr[data-strike]').forEach((tr) => { tr.onclick = () => jumpToStrike(+tr.dataset.strike); });
}
function jumpToStrike(strike) {
  $('#tabs button[data-tab="map"]').click();
  setTimeout(() => {
    const ps = candle.priceScale(); const rg = ps.getVisibleRange();
    const span = rg ? rg.to - rg.from : 800;
    S.auto = false; $('#auto').checked = false; save();
    ps.applyOptions({ autoScale: false });
    ps.setVisibleRange({ from: strike - span / 2, to: strike + span / 2 });
    if (flowHL) candle.removePriceLine(flowHL);
    flowHL = candle.createPriceLine({ price: strike, color: '#e7a44c', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '活動' });
    clearTimeout(flowHL._h); flowHL._h = setTimeout(() => { if (flowHL) { candle.removePriceLine(flowHL); flowHL = null; } }, 8000);
  }, 60);
  toast(`已在地圖定位 ${f0.format(strike)}(橘色虛線標記)`);
}
$('#flowSrc').onchange = loadFlow; $('#flowN').onchange = loadFlow;

/* ------------------------------ whole book ------------------------------ */
let bookTimer = null;
async function loadBook() {
  const params = { src: 'NQ,QQQ,NDX', scope: S.scope };
  if (R.on && D.cols[R.idx]) params.t = D.cols[R.idx].t;
  try { D.book = await api('book', params); } catch (e) { D.book = null; }
  renderBook();
}
const loadBookSoon = () => { clearTimeout(bookTimer); bookTimer = setTimeout(loadBook, 250); };
function renderBook() {
  const box = $('#cardBook'), B = D.book;
  const bk = S.bookBasis || 'oi';
  if (!B || !B.books) { box.innerHTML = '<h3>整體部位 · Whole book</h3><div class="sub">沒有資料</div>'; return; }
  const key = bk === 'oi' ? 'gex_oi' : 'gex_vol', ck = bk === 'oi' ? 'charm_oi' : 'charm_vol';
  const rows = ['NQ', 'QQQ', 'NDX'].filter((x) => B.books[x]).map((x) => ({ src: x, ...B.books[x] }));
  const sum = (f) => rows.reduce((a, r) => a + f(r), 0);
  const tot = { src: 'SUM', [key]: [sum((r) => r[key][0]), sum((r) => r[key][1])], [ck]: [sum((r) => r[ck][0]), sum((r) => r[ck][1])],
    call_vol: sum((r) => r.call_vol), put_vol: sum((r) => r.put_vol), call_oi: sum((r) => r.call_oi), put_oi: sum((r) => r.put_oi) };
  const all = [...rows, tot];
  const share = (r) => (r[key][1] ? r[key][0] / r[key][1] : 0);
  const mx = Math.max(0.02, ...all.map((r) => Math.abs(share(r))));
  let h = `<h3>整體部位 · Whole book<small>${B.t ? hm(B.t) : ''}</small></h3>
    <div class="seg" style="margin-bottom:8px"><button data-b="oi" class="${bk === 'oi' ? 'on' : ''}">未平倉量</button><button data-b="vol" class="${bk === 'vol' ? 'on' : ''}">成交量</button></div>
    <div class="sub" style="margin-bottom:4px">淨曝險占比 = 淨 GEX ÷ 總 GEX 規模(−100% 全是 Put 端,+100% 全是 Call 端)</div>`;
  for (const r of all) {
    const sh = share(r), w = Math.min(50, Math.abs(sh) / mx * 50), col = sh >= 0 ? S.pal.pos : S.pal.neg;
    h += `<div class="bk"><span class="nm" style="color:${r.src === 'SUM' ? '#d58cff' : SRC_COL[r.src]}">${r.src === 'SUM' ? '合計' : r.src}</span>
      <span class="dv"><i style="background:${col};${sh >= 0 ? `left:50%;width:${w}%` : `right:50%;width:${w}%`}"></i></span>
      <span class="v" style="color:${col}">${fSigned(sh * 100, 1)}%</span></div>`;
  }
  const pcr = (r) => { const c = bk === 'oi' ? r.call_oi : r.call_vol, p = bk === 'oi' ? r.put_oi : r.put_vol; return c + p ? p / c : null; };
  h += `<table class="mini"><thead><tr><th></th>${all.map((r) => `<th>${r.src === 'SUM' ? '合計' : r.src}</th>`).join('')}</tr></thead><tbody>
    <tr><td>淨 GEX</td>${all.map((r) => `<td>${fM(r[key][0] / 1e6)}</td>`).join('')}</tr>
    <tr><td>GEX 規模</td>${all.map((r) => `<td>${fM(r[key][1] / 1e6).replace('+', '')}</td>`).join('')}</tr>
    <tr><td style="white-space:nowrap">淨 Charm</td>${all.map((r) => `<td>${fM(r[ck][0] / 1e6)}</td>`).join('')}</tr>
    <tr><td style="white-space:nowrap">P/C 比</td>${all.map((r) => { const v = pcr(r); return `<td>${v == null ? '—' : v.toFixed(2)}</td>`; }).join('')}</tr>
    </tbody></table>
    <div class="note">這是用「口數 × gamma」估的部位,不是買賣金額。成交量沒有買賣方向,無法分辨是買進還是賣出;要看「權利金流向」需要逐筆成交資料(目前沒有)。數字為 $ / 1% 價格變動;Charm 為每日 delta 變化($)。P/C 比 = Put 口數 ÷ Call 口數。</div>`;
  box.innerHTML = h;
  $$('[data-b]', box).forEach((b) => { b.onclick = () => { S.bookBasis = b.dataset.b; save(); renderBook(); }; });
}

/* ------------------------------ events ------------------------------ */
const EV_NAME = { surge: '大量成交', touch: '觸及關卡', cross: '穿越 Flip' };
const KIND_NAME = { call: 'Call 牆', put: 'Put 牆', flip: 'Flip' };
const evColor = (e) => (e.type === 'surge' ? SRC_COL[e.src] : e.type === 'cross' ? '#d58cff' : e.kind === 'call' ? S.pal.pos : e.kind === 'put' ? S.pal.neg : '#ffd166');
const evGlyph = (e) => (e.type === 'surge' ? '量' : e.type === 'cross' ? '穿' : '觸');
let evHits = [];
function barOf(t) { const i = snapIdx(t); return i < 0 ? -1 : i; }
function fwd(bi, level, mins) {   // what price did in the minutes after a candle
  const n = Math.max(1, Math.ceil(mins / S.tf)), end = Math.min(D.bars.length - 1, bi + n);
  if (end <= bi) return null;
  let hi = -Infinity, lo = Infinity;
  for (let i = bi + 1; i <= end; i++) { hi = Math.max(hi, D.bars[i].high); lo = Math.min(lo, D.bars[i].low); }
  return { up: hi - level, dn: lo - level, done: bi + n <= D.bars.length - 1 };
}
function rebuildClientEvents() {
  const out = [];
  if (D.kind === 'candles' && D.bars.length > 2) {
    const cool = new Map();
    for (const src of S.srcs) {
      const ser = D.levels[src]; if (!ser || !ser.t.length) continue;
      for (const kind of ['call', 'put', 'flip']) {
        let pj = -1;
        for (let i = 1; i < D.bars.length; i++) {
          const b = D.bars[i], pb = D.bars[i - 1], j = lowerIdx(ser.t, b.time);
          if (j < 0 || b.time - ser.t[j] > 1800) { pj = -1; continue; }
          const L = ser[kind][j]; if (L == null) continue;
          const pL = pj >= 0 ? ser[kind][pj] : L; pj = j;
          const key = src + kind;
          if (kind === 'flip') {
            const was = pb.close - pL, now = b.close - L;
            if (was * now < 0 && b.time - (cool.get(key) || 0) >= 2400) {
              cool.set(key, b.time);
              out.push({ type: 'cross', t: b.time, bi: i, src, kind, price: L, dir: now > 0 ? 'up' : 'dn', close: b.close });
            }
          } else if (b.low <= L && L <= b.high && !(pb.low <= pL && pL <= pb.high) && b.time - (cool.get(key) || 0) >= 3600) {
            cool.set(key, b.time);
            out.push({ type: 'touch', t: b.time, bi: i, src, kind, price: L, from: pb.close < L ? 'below' : 'above', hi: b.high, lo: b.low, close: b.close });
          }
        }
      }
    }
  }
  D.cevents = out; mergeEvents();
}
function mergeEvents() {
  const srv = D.srvEvents.map((e, k) => ({ ...e, id: 's' + k, bi: barOf(e.t) })).filter((e) => e.bi >= 0 && S.srcs.length >= 0);
  const cl = (D.cevents || []).map((e, k) => ({ ...e, id: 'c' + k }));
  D.events = [...srv, ...cl].sort((a, b) => a.t - b.t);
  if (evPrim) evPrim.redraw();
  updateBell(); if (!$('#drawer').hidden) renderDrawer();
}
let lastEvLoad = 0;
async function loadEvents(force) {
  const now = Date.now();
  if (!force && now - lastEvLoad < 240000) return;
  lastEvLoad = now;
  const { lo, hi } = rangeNow();
  try {
    const r = await api('events', { from: lo, to: hi, src: 'NQ,QQQ,NDX', scope: S.scope });
    D.srvEvents = r.events || [];
  } catch (e) { D.srvEvents = []; }
  mergeEvents();
}
const visEvents = () => D.events.filter((e) => S.evTypes[e.type]);
class EventPrim {
  attached(p) { this.series = p.series; this.chart = p.chart; this.req = p.requestUpdate; }
  detached() {}
  updateAllViews() {}
  redraw() { if (this.req) this.req(); }
  paneViews() { return this._v || (this._v = [{ zOrder: () => 'top', renderer: () => ({ draw: (t) => this.draw(t) }) }]); }
  draw(target) {
    target.useBitmapCoordinateSpace((sc) => {
      evHits = [];
      if (!S.events) return;
      const ctx = sc.context, hr = sc.horizontalPixelRatio, vr = sc.verticalPixelRatio, ts = this.chart.timeScale();
      const r = 7.5;
      ctx.font = `700 ${10.5 * hr}px -apple-system,"PingFang TC","Noto Sans TC",sans-serif`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      const used = new Map();
      for (const e of visEvents()) {
        const bt = D.barT[e.bi]; if (bt == null) continue;   // 事件指到的 K 棒已不在目前資料範圍(切換範圍/載入中)
        const x = ts.timeToCoordinate(bt), y = this.series.priceToCoordinate(e.price);
        if (x == null || y == null) continue;
        const k = Math.round(x / 18) + ':' + Math.round(y / 18), n = used.get(k) || 0; used.set(k, n + 1);
        const yy = y - n * 19;
        ctx.beginPath(); ctx.arc(x * hr, yy * vr, r * hr, 0, 7);
        ctx.fillStyle = evColor(e); ctx.globalAlpha = 0.95; ctx.fill(); ctx.globalAlpha = 1;
        ctx.lineWidth = 1.5 * hr; ctx.strokeStyle = 'rgba(8,11,17,.9)'; ctx.stroke();
        ctx.fillStyle = '#0a0d12'; ctx.fillText(evGlyph(e), x * hr, yy * vr + 0.5 * hr);
        evHits.push({ x, y: yy, e });
      }
    });
  }
}
const evPrim = new EventPrim(); candle.attachPrimitive(evPrim);

/* ---- orderflow 事件:吸收 / 冰山 / 力竭 / 掃單。bridge 偵測(推測式線索),畫在熱圖對應的時間與價位 ----
   顏色語意:綠 = 偏多含意、紅 = 偏空含意;大小 = 口數。形狀:盾牌=吸收、菱形+虛線=冰山、圓圈+箭頭=力竭、粗線箭頭=掃單 */
let flowEvents = [], feHits = [];
const FE = { sweep: { name: '掃單', glyph: '掃' }, stop: { name: '停損(推測)', glyph: '停' }, absorb: { name: '吸收', glyph: '吸' }, iceberg: { name: '冰山', glyph: '冰' }, exhaust: { name: '力竭', glyph: '竭' } };
const feBull = (e) => (e.k === 'exhaust' ? e.s === 'sell' : e.s === 'buy');
const feR = (e) => 6.5 + Math.min(4.5, Math.log10(Math.max(10, e.v) / 10) * 3.2);
class FlowEvPrim {
  attached(p) { this.series = p.series; this.chart = p.chart; this.req = p.requestUpdate; }
  detached() {}
  updateAllViews() {}
  redraw() { if (this.req) this.req(); }
  paneViews() { return this._v || (this._v = [{ zOrder: () => 'top', renderer: () => ({ draw: (t) => this.draw(t) }) }]); }
  draw(target) {
    target.useBitmapCoordinateSpace((sc) => {
      feHits = [];
      if (!S.flmark || !flowEvents.length || !D.barT.length) return;
      const ctx = sc.context, hr = sc.horizontalPixelRatio, vr = sc.verticalPixelRatio, ts = this.chart.timeScale(), ser = this.series;
      const W = sc.bitmapSize.width / hr;
      const used = new Map();
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      for (const e of flowEvents) {
        if (S.feKinds[e.k] === false) continue;                             // 圖例上關掉的種類不畫
        const bi = lowerIdx(D.barT, e.t); if (bi < 0) continue;
        let x = ts.timeToCoordinate(D.barT[bi]), y = ser.priceToCoordinate(e.p);
        if (x == null || y == null || x < -20 || x > W + 20) continue;
        const k = Math.round(x / 16) + ':' + Math.round(y / 16), n = used.get(k) || 0; used.set(k, n + 1);
        x += (n % 2 ? 1 : -1) * Math.ceil(n / 2) * 15;          // 重疊時左右錯開
        const bull = feBull(e), col = bull ? '#2bc08b' : '#ff5c6c', r = feR(e);
        const X = x * hr, Y = y * vr, R = r * hr;
        ctx.save();
        ctx.fillStyle = col; ctx.strokeStyle = col;
        if (e.k === 'sweep' || e.k === 'stop') {
          const isStop = e.k === 'stop';
          const y0 = ser.priceToCoordinate((e.x && e.x.p0) || e.p); const Y0 = (y0 == null ? y : y0) * vr;
          const dir = Y < Y0 ? -1 : 1;                                       // 往上掃 = 畫面上 Y 變小
          ctx.globalAlpha = 0.16; ctx.lineWidth = 8 * hr; ctx.beginPath(); ctx.moveTo(X, Y0); ctx.lineTo(X, Y); ctx.stroke();
          ctx.globalAlpha = 0.95; ctx.lineWidth = 2.6 * hr; if (isStop) ctx.setLineDash([5 * hr, 3 * hr]); ctx.beginPath(); ctx.moveTo(X, Y0); ctx.lineTo(X, Y - dir * 4 * hr); ctx.stroke(); ctx.setLineDash([]);
          ctx.beginPath(); ctx.moveTo(X, Y); ctx.lineTo(X - 5 * hr, Y - dir * 8 * hr); ctx.lineTo(X + 5 * hr, Y - dir * 8 * hr); ctx.closePath(); ctx.fill();
          ctx.font = `700 ${9.5 * hr}px -apple-system,"PingFang TC","Noto Sans TC",sans-serif`; ctx.textAlign = 'left';
          ctx.globalAlpha = 0.95; ctx.fillText((isStop ? '停? ' : '掃 ') + e.v, X + 7 * hr, (Y + Y0) / 2);
        } else {
          ctx.lineWidth = 1.6 * hr;
          if (e.k === 'iceberg') {                                           // 價位虛線 + 菱形
            ctx.globalAlpha = 0.5; ctx.setLineDash([4 * hr, 3 * hr]); ctx.beginPath(); ctx.moveTo(X - 40 * hr, Y); ctx.lineTo(X + 40 * hr, Y); ctx.stroke(); ctx.setLineDash([]);
            ctx.beginPath(); ctx.moveTo(X, Y - R * 1.2); ctx.lineTo(X + R * 1.1, Y); ctx.lineTo(X, Y + R * 1.2); ctx.lineTo(X - R * 1.1, Y); ctx.closePath();
          } else if (e.k === 'absorb') {                                     // 盾牌
            ctx.beginPath(); ctx.moveTo(X - R, Y - R * 0.85); ctx.lineTo(X + R, Y - R * 0.85); ctx.lineTo(X + R, Y + R * 0.1);
            ctx.quadraticCurveTo(X + R, Y + R * 0.95, X, Y + R * 1.2); ctx.quadraticCurveTo(X - R, Y + R * 0.95, X - R, Y + R * 0.1); ctx.closePath();
          } else {                                                           // 力竭:圓圈 + 預期反轉方向的小箭頭
            ctx.beginPath(); ctx.arc(X, Y, R, 0, 7);
            const up = bull, ay = up ? Y - R - 7 * hr : Y + R + 7 * hr, d = up ? 1 : -1;
            ctx.globalAlpha = 0.9; ctx.beginPath(); ctx.moveTo(X, ay - d * 4 * hr); ctx.lineTo(X - 4.5 * hr, ay + d * 3 * hr); ctx.lineTo(X + 4.5 * hr, ay + d * 3 * hr); ctx.closePath(); ctx.fill();
            ctx.beginPath(); ctx.arc(X, Y, R, 0, 7);
          }
          ctx.fillStyle = 'rgba(8,11,17,.8)'; ctx.globalAlpha = 1; ctx.fill();          // 深色底,字才讀得到
          ctx.fillStyle = col; ctx.globalAlpha = 0.24; ctx.fill(); ctx.globalAlpha = 0.95; ctx.stroke();
          ctx.fillStyle = '#f4f7fb'; ctx.globalAlpha = 1;
          ctx.font = `700 ${Math.round(Math.max(9, r * 1.05)) * hr}px -apple-system,"PingFang TC","Noto Sans TC",sans-serif`;
          ctx.fillText(FE[e.k].glyph, X, Y + 0.5 * hr);
        }
        ctx.restore();
        feHits.push({ x, y, r: Math.max(r, 9), e });
      }
    });
  }
}
const flowEvPrim = new FlowEvPrim(); candle.attachPrimitive(flowEvPrim); window.__flm = flowEvPrim; window.__feHits = () => feHits; window.__feN = () => flowEvents.length;
function feText(e) {
  const x = e.x || {}, buy = e.s === 'buy', f1 = (v) => (v == null ? '—' : (+v).toFixed(1));
  if (e.k === 'stop') return `${buy ? '買方' : '賣方'}一波由 ${x.aggr != null ? x.aggr : '多'} 張攻擊單在極短時間內從 ${f1(x.p0)} 衝到 ${f1(e.p)}(共 ${e.v} 口),並${buy ? '突破' : '跌破'}近期${buy ? '高' : '低'}點 ${f1(x.brk)} 點 → 疑似停損單被觸發。這是依特徵推測;API 沒有停損旗標,可能只是一般的突破追價。`;
  if (e.k === 'sweep') return `${buy ? '買方' : '賣方'}主動單連續吃穿 ${x.lv != null ? x.lv : '多'} 個價位,共 ${e.v} 口,從 ${f1(x.p0)} 推到 ${f1(e.p)}。常見於急迫的追價或停損被掃出。`;
  if (e.k === 'absorb') return `短時間內${buy ? '賣' : '買'}方主動成交 ${e.v} 口${x.thr ? `(門檻 ${f1(x.thr)} 口,為近 10 分鐘平均+標準差)` : ''},價格往攻擊方向只走了 ${f1(x.moved)} 點 → 疑似${buy ? '買' : '賣'}方在 ${f1(e.p)} 附近承接${x.conf === 2 ? '(同價附近另有冰山證據)' : ''}。`;
  if (e.k === 'iceberg') return `${f1(e.p)} 這一檔的${buy ? '買' : '賣'}盤被打掉 ${e.v} 口(${x.n != null ? x.n : '多'} 筆),看得見的掛量最多只有 ${f1(x.vis)} 口,價位還守著 → ${x.conf === 2 ? (x.by === 'id' ? '同一張委託被打掉的量遠超它的顯示量(證據較強)' : '成交後同價位立刻又出現新掛單(有補單證據)') : '只有成交量證據(較弱)'}。`;
  return `${buy ? '買' : '賣'}方一波推了 ${f1(x.rise != null ? x.rise : x.fall)} 點、淨主動 ${e.v} 口,最近 15 秒主動量只剩 ${f1(x.tail)} 口、也沒再創${buy ? '高' : '低'} → 這波力道疑似用完。`;
}
function feTipHtml(e) {
  const bull = feBull(e), who = e.k === 'absorb' || e.k === 'iceberg' ? (e.s === 'buy' ? '買方承接' : '賣方承接') : (e.s === 'buy' ? '買方主動' : '賣方主動');
  return `<div class="feH"><b style="color:${bull ? '#2bc08b' : '#ff5c6c'}">${FE[e.k].name}</b><span>${who} · ${bull ? '偏多含意' : '偏空含意'}</span></div>`
    + `<div class="feT">${fmtTime(e.t)}</div><div class="feB">${feText(e)}</div>`
    + '<div class="feF">程式用成交與掛單簿推測的線索,規則還沒用真資料驗證,不保證反轉。</div>';
}
(function setupFeUi() {
  const wrap = $('#chart').parentElement; wrap.style.position = wrap.style.position || 'relative';
  const tip = document.createElement('div'); tip.className = 'feTip'; tip.hidden = true; document.body.appendChild(tip);
  const lg = document.createElement('div'); lg.className = 'feLegend'; lg.id = 'feLegend'; lg.hidden = true;
  lg.innerHTML = '<button class="feG" data-k="absorb" title="點一下顯示/隱藏吸收"><i>吸</i><span>吸收</span></button><button class="feG" data-k="iceberg" title="點一下顯示/隱藏冰山"><i class="dm">冰</i><span>冰山</span></button>'
    + '<button class="feG" data-k="exhaust" title="點一下顯示/隱藏力竭"><i class="ci">竭</i><span>力竭</span></button><button class="feG" data-k="sweep" title="點一下顯示/隱藏掃單"><i class="sw">掃</i><span>掃單</span></button>'
    + '<button class="feG" data-k="stop" title="點一下顯示/隱藏停損(推測)"><i class="sw">停</i><span>停損(推測)</span></button><em>綠=偏多含意 紅=偏空含意 · 點圖例可個別開關</em>';
  lg.addEventListener('click', (ev) => {
    const b = ev.target.closest('.feG'); if (!b) return;
    const k = b.dataset.k; S.feKinds[k] = S.feKinds[k] === false; save(); flowEvPrim.redraw(); updateFeLegend();
  });
  wrap.appendChild(lg);
  window.__feTip = tip;
  chart.subscribeCrosshairMove((p) => {
    let hit = null;
    if (p.point && S.flmark) for (const h of feHits) if (Math.hypot(h.x - p.point.x, h.y - p.point.y) <= h.r + 3) { hit = h; break; }
    $('#chart').style.cursor = hit ? 'help' : '';
    if (!hit) { tip.hidden = true; return; }
    tip.innerHTML = feTipHtml(hit.e); tip.hidden = false;
    const rc = $('#chart').getBoundingClientRect(), tw = Math.min(320, window.innerWidth - 24);
    tip.style.width = tw + 'px';
    let left = rc.left + hit.x + 16; if (left + tw > window.innerWidth - 8) left = rc.left + hit.x - tw - 16;
    tip.style.left = Math.max(8, left) + 'px'; tip.style.top = Math.max(8, Math.min(window.innerHeight - tip.offsetHeight - 8, rc.top + hit.y - 20)) + 'px';
  });
})();
const FE_NAME = { absorb: '吸收', iceberg: '冰山', exhaust: '力竭', sweep: '掃單', stop: '停損(推測)' };
function updateFeLegend() {
  const lg = $('#feLegend'); if (!lg) return;
  lg.hidden = !(S.flmark && flowEvents.length);
  const cnt = {}; for (const e of flowEvents) cnt[e.k] = (cnt[e.k] || 0) + 1;
  $$('.feG', lg).forEach((b) => { const k = b.dataset.k, off = S.feKinds[k] === false; b.classList.toggle('off', off); b.setAttribute('aria-pressed', String(!off)); $('span', b).textContent = FE_NAME[k] + (cnt[k] ? ' ' + cnt[k] : ''); });
}
async function loadFlowEvents() {
  if (!D.bars.length) { flowEvents = []; flowEvPrim.redraw(); updateFeLegend(); return; }
  try {
    const lo = D.bars[0].time, hi = D.bars[D.bars.length - 1].time + 120;
    const r = await api('flowevents', { from: lo, to: hi });
    flowEvents = (r && r.events) || [];
  } catch (e) { flowEvents = []; }
  flowEvPrim.redraw(); updateFeLegend();
}
const fmtTime = (t) => `台北 ${hmIn(t, 'Asia/Taipei')} · 紐約 ${hmIn(t, 'America/New_York')} · ${md(t)}`;
function evTitle(e) {
  if (e.type === 'surge') return `${e.src} ${f0.format(e.strike)} ${e.cp === 'C' ? 'Call' : 'Put'} · ${e.dte === 0 ? '0DTE' : e.dte + ' 天後到期'}`;
  if (e.type === 'cross') return `${sn(e.src)} Flip ${e.dir === 'up' ? '向上穿越' : '向下穿越'} ${fP(e.price)}`;
  return `${sn(e.src)} ${KIND_NAME[e.kind]} ${fP(e.price)}`;
}
function evBody(e) {
  const row = (a, b) => `<div class="r"><span>${a}</span><span>${b}</span></div>`;
  let rows = '', det = '';
  const bar = D.bars[e.bi];
  if (e.type === 'surge') {
    rows += row('對應 NQ 點位', fP(e.price));
    if (bar) rows += row('當時 NQ', `${fP(bar.close)}(${fSigned(bar.close - e.price, 0)} 點)`);
    rows += row(`近 ${Math.round(e.mins)} 分鐘新增成交`, `<b>${f0.format(e.dvol)}</b> 口`);
    rows += row('累計成交', `${f0.format(e.vol)} 口`);
    rows += row('占該來源同期新增', `${(e.share * 100).toFixed(1)}%`);
    rows += row('該口 GEX 變化', `${fM(e.dgex)}`.replace('+', ''));
    det = `<b>事實:</b>這個合約在兩次快照之間累計成交量增加了 ${f0.format(e.dvol)} 口,而且遠大於同一來源其他合約同期的新增量(偵測門檻:新增 ≥ 30 口且 ≥ 同來源最活躍合約(前 5%)同期新增量的 3 倍;同一合約 30 分鐘內不重複標記)。<br><b>限制:</b>成交量沒有買賣方向,看不出是買進或賣出、開倉或平倉,所以不能直接說「有人在押多/押空」。要確認需要逐筆成交與委買委賣資料。`;
  } else if (e.type === 'cross') {
    rows += row('時間', hm(e.t)); rows += row('收盤價', fP(e.close));
    const f = fwd(e.bi, e.price, 30);
    if (f) rows += row(`之後 30 分鐘${f.done ? '' : '(進行中)'}`, `最高 ${fSigned(f.up, 0)} / 最低 ${fSigned(f.dn, 0)} 點`);
    det = `<b>事實:</b>這根 K 棒的收盤價與前一根在 ${e.src} 的 flip 兩側不同。<br><b>慣例解讀(未驗證):</b>flip 上方造市商傾向壓低波動、下方傾向放大波動;事件之後的走勢僅供對照,樣本太少不能當結論。`;
  } else {
    rows += row('觸及方向', e.from === 'below' ? '從下方碰到' : '從上方碰到');
    rows += row('該根 K 棒', `${fP(e.lo)} – ${fP(e.hi)}`); rows += row('收盤價', `${fP(e.close)}(${fSigned(e.close - e.price, 0)} 點)`);
    const f = fwd(e.bi, e.price, 30);
    if (f) rows += row(`之後 30 分鐘${f.done ? '' : '(進行中)'}`, `最高 ${fSigned(f.up, 0)} / 最低 ${fSigned(f.dn, 0)} 點`);
    det = `<b>事實:</b>當時這根 K 棒的高低範圍包含該關卡,且前一根沒有碰到。同一關卡 60 分鐘內不重複標記。<br><b>提醒:</b>關卡是依選擇權部位算出的位置,價格碰到之後會反彈、突破或無反應,這裡只是記錄,不是預測。`;
  }
  return { rows, det };
}
function showEvCard(e, at) {
  const card = $('#evcard'), { rows, det } = evBody(e);
  card.innerHTML = `<button class="x" aria-label="關閉">×</button><span class="chip">NOTABLE ACTIVITY · ${EV_NAME[e.type]}</span>
    <h4>${evTitle(e)}</h4><div class="when">${fmtTime(e.t)}</div>${rows}
    <button class="more">＋ 詳細說明</button><div class="det" hidden>${det}</div>`;
  card.hidden = false;
  $('.x', card).onclick = () => { card.hidden = true; };
  const more = $('.more', card), d = $('.det', card);
  more.onclick = () => { d.hidden = !d.hidden; more.textContent = d.hidden ? '＋ 詳細說明' : '－ 收起'; place(); };
  function place() {
    const w = $('.chartwrap'); let x = at.x + 16, y = at.y - 20;
    if (x + card.offsetWidth > w.clientWidth - 80) x = at.x - card.offsetWidth - 16;
    y = Math.max(8, Math.min(y, w.clientHeight - card.offsetHeight - 8)); card.style.left = Math.max(8, x) + 'px'; card.style.top = y + 'px';
  }
  place();
}
chart.subscribeClick((p) => {
  if (!p.point) return;
  let best = null, bd = 14;
  for (const h of evHits) { const d = Math.hypot(h.x - p.point.x, h.y - p.point.y); if (d < bd) { bd = d; best = h; } }
  if (best) showEvCard(best.e, { x: best.x, y: best.y }); else $('#evcard').hidden = true;
});
chart.subscribeCrosshairMove((p) => { const c = $('#chart'); let on = false; if (p.point) for (const h of evHits) if (Math.hypot(h.x - p.point.x, h.y - p.point.y) < 12) { on = true; break; } c.style.cursor = on ? 'pointer' : ''; });
function updateBell() {
  const n = visEvents().filter((e) => e.t > (S.evSeen || 0)).length, b = $('#badge');
  b.hidden = n === 0 || !S.events; b.textContent = n > 99 ? '99+' : n;
}
function renderDrawer() {
  $$('#evFilter button').forEach((b) => b.classList.toggle('on', !!S.evTypes[b.dataset.v]));
  const list = visEvents().slice().reverse();
  $('#dlist').innerHTML = list.length ? list.map((e) => `<div class="it" data-id="${e.id}"><span class="ic" style="background:${evColor(e)}">${evGlyph(e)}</span>
    <div><div class="tt">${evTitle(e)}</div><div class="ss">${md(e.t)} 台北 ${hmIn(e.t, 'Asia/Taipei')} · ${EV_NAME[e.type]}${e.type === 'surge' ? ` · +${f0.format(e.dvol)} 口` : ''}</div></div></div>`).join('')
    : '<div class="sub" style="padding:14px">這個範圍內沒有特別情況。</div>';
  $$('#dlist .it').forEach((it) => { it.onclick = () => { $('#drawer').hidden = true; focusEvent(D.events.find((x) => x.id === it.dataset.id)); }; });
}
function focusEvent(e) {
  if (!e) return;
  const t = D.barT[e.bi], i = e.bi, ts = chart.timeScale();
  const lr = ts.getVisibleLogicalRange(), span = lr ? lr.to - lr.from : 120;
  ts.setVisibleLogicalRange({ from: i - span * 0.6, to: i + span * 0.4 });
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const x = ts.timeToCoordinate(t), y = candle.priceToCoordinate(e.price);
    if (x != null && y != null) showEvCard(e, { x, y });
  }));
}
$('#bell').onclick = () => {
  const d = $('#drawer'); d.hidden = !d.hidden;
  if (!d.hidden) { renderDrawer(); const last = D.events.reduce((m, e) => Math.max(m, e.t), 0); S.evSeen = last; save(); updateBell(); }
};
$('#drawerX').onclick = () => { $('#drawer').hidden = true; };
$('#errbtn').onclick = () => {
  const d = $('#errdrawer');
  d.hidden = !d.hidden;
  if (!d.hidden) { loadErrors(); errSeen = Math.floor(Date.now() / 1000); updateErrBadge(); }
};
$('#errdrawerX').onclick = () => { $('#errdrawer').hidden = true; };
$('#errClear').onclick = () => { CLIENT_ERRORS.length = 0; DSRV_ERRORS = []; renderErrors(); updateErrBadge(); };
$('#evFilter').onclick = (e) => { const b = e.target.closest('button'); if (!b) return; S.evTypes[b.dataset.v] = !S.evTypes[b.dataset.v]; save(); renderDrawer(); evPrim.redraw(); updateBell(); };

/* ------------------------------ colours & chart controls ------------------------------ */
function fillPalUI() {
  const sel = $('#palPreset');
  sel.innerHTML = Object.keys(PRESETS).map((n) => `<option>${n}</option>`).join('') + '<option value="__c">自訂</option>';
  sel.value = PRESETS[S.pal.name] ? S.pal.name : '__c';
  $('#cLow').value = S.pal.low; $('#cMid').value = S.pal.mid; $('#cHigh').value = S.pal.high; $('#cPos').value = S.pal.pos; $('#cNeg').value = S.pal.neg;
  buildLUTs();
  const a = [0, 64, 128, 192, 255].map((i) => LUT.abs[i]).join(','), b = [LUT.put[255], LUT.put[110], 'rgba(0,0,0,0)', LUT.call[110], LUT.call[255]].join(',');
  $('#palBar').style.background = `linear-gradient(90deg,${a}), #0b0f16`;
  $('#palBar').style.backgroundImage = `linear-gradient(90deg,${a})`;
  $('#palBar').title = '集中度漸層(上)';
  void b;
}
function palChanged(custom) { if (custom) S.pal.name = '自訂'; save(); fillPalUI(); redraw(); updateLegend(); renderBook(); evPrim.redraw(); }
$('#palPreset').onchange = (e) => { const n = e.target.value; if (PRESETS[n]) { S.pal = { name: n, ...PRESETS[n] }; palChanged(false); } };
[['cLow', 'low'], ['cMid', 'mid'], ['cHigh', 'high'], ['cPos', 'pos'], ['cNeg', 'neg']].forEach(([id, k]) => { $('#' + id).oninput = (e) => { S.pal[k] = e.target.value; palChanged(true); }; });

function applyChartPrefs() {
  const g = { vertLines: { visible: S.gridV }, horzLines: { visible: S.gridH } };
  const hide = S.cross === 'hidden';
  chart.applyOptions({
    grid: g,
    crosshair: { mode: S.cross === 'magnet' ? LW.CrosshairMode.Magnet : LW.CrosshairMode.Normal, vertLine: { visible: !hide, labelVisible: !hide }, horzLine: { visible: !hide, labelVisible: !hide } },
    timeScale: { rightOffset: roffEff(), rightBarStaysOnScroll: S.stay },
  });
  subChart.applyOptions({ grid: g });
  candle.priceScale().applyOptions({ mode: +S.pmode, autoScale: S.auto });
  candle.applyOptions({ priceLineVisible: S.pline });
  for (const k in lineSeries) { const s = k.split('-')[0]; lineSeries[k].applyOptions({ color: (S.lineCol && S.lineCol[s]) || SRC_COL[s], lineWidth: lw(k), title: S.llabels ? lineTitle(k) : '', lastValueVisible: S.llabels }); }
}
const lw = (k) => (k.endsWith('flip') ? S.lw + 1 : S.lw);
const lineTitle = (k) => { const [src, kind] = k.split('-'); return `${sn(src)} ${KIND_NAME[kind]}`; };
$('#gridH').onchange = (e) => { S.gridH = e.target.checked; save(); applyChartPrefs(); };
$('#gridV').onchange = (e) => { S.gridV = e.target.checked; save(); applyChartPrefs(); };
$('#cross').onchange = (e) => { S.cross = e.target.value; save(); applyChartPrefs(); };
$('#pmode').onchange = (e) => { S.pmode = +e.target.value; save(); applyChartPrefs(); redraw(); };
$('#auto').onchange = (e) => { S.auto = e.target.checked; save(); applyChartPrefs(); };
$('#pline').onchange = (e) => { S.pline = e.target.checked; save(); applyChartPrefs(); };
$('#llabels').onchange = (e) => { S.llabels = e.target.checked; save(); applyChartPrefs(); };
$('#stay').onchange = (e) => { S.stay = e.target.checked; save(); applyChartPrefs(); };
$('#lw').oninput = (e) => { S.lw = +e.target.value; save(); applyChartPrefs(); };
[['lcNQ', 'NQ'], ['lcQQQ', 'QQQ'], ['lcNDX', 'NDX'], ['lcWGT', 'WGT']].forEach(([id, src]) => { $('#' + id).oninput = (e) => { S.lineCol[src] = e.target.value; save(); applyChartPrefs(); }; });
$('#roff').oninput = (e) => { S.roff = +e.target.value; save(); applyChartPrefs(); };
function setSidebar(off) { S.nosb = off; save(); $('.body').classList.toggle('nosidebar', off); $('#sideToggle').textContent = off ? '展開側欄' : '收起側欄'; }
$('#sideToggle').onclick = () => setSidebar(!S.nosb);
$('#reset2').onclick = () => $('#reset').click();
$('#moreBtn').onclick = () => {
  const t = $('#tmore'), open = t.hidden;
  t.hidden = !open;
  $('#moreBtn').textContent = open ? '更多 ▴' : '更多 ▾';
};
$('#setToggle').onclick = () => {
  S.sets = !S.sets; save();
  $('#setWrap').hidden = !S.sets;
  $('#setCaret').textContent = S.sets ? '▾' : '▸';
};
$('#fs').onclick = () => { try { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen(); } catch (e) { /* ignore */ } };
/* Alt + wheel = zoom the price axis only (turns auto-scale off) */
$('#chart').addEventListener('wheel', (e) => {
  if (!e.altKey) return;
  e.preventDefault(); e.stopPropagation();
  const ps = candle.priceScale(); if (+S.pmode !== 0) return;
  const rg = ps.getVisibleRange(), r = $('#chart').getBoundingClientRect(), p = candle.coordinateToPrice(e.clientY - r.top);
  if (!rg || p == null) return;
  const f = e.deltaY > 0 ? 1.12 : 0.89;
  S.auto = false; $('#auto').checked = false; save();
  ps.applyOptions({ autoScale: false });
  ps.setVisibleRange({ from: p - (p - rg.from) * f, to: p + (rg.to - p) * f });
}, { capture: true, passive: false });

/* ------------------------------ boot ------------------------------ */
syncUI(); wLabels(); fillPalUI(); applyChartPrefs(); setSidebar(!!S.nosb); anlyMeta(); updateLegend(); setPill('', '連線中…');
poll();
setInterval(poll, 3000);
loadErrors();
setInterval(loadErrors, 12000);
window.__gex = { D, S, chart, candle, evPrim, focusEvent, hits: () => evHits };   // handy for debugging in the browser console
})();
