// 測頂端摘要的時間函式:夏令 / 冬令的開盤時間、倒數、週末跳到週一。用法:node tests/test_glance_time.js
const fs = require('fs'), path = require('path'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
const a = src.indexOf('const NY_TZ'), b = src.indexOf('function tickClock');
const m = { exports: {} };
new Function('module', src.slice(a, b) + '\nmodule.exports={sessionInfo,zonedMs,fHMS,fClock,TW_TZ,NY_TZ};')(m);
const { sessionInfo, fHMS, fClock, TW_TZ, NY_TZ } = m.exports;
const iso = (s) => Date.parse(s);
// 夏令(EDT,UTC-4):2026-10-28 週三。開盤 13:30Z = 台北 21:30;收盤 17:30Z
let si = sessionInfo(iso('2026-10-28T12:00:00Z'));
assert(!si.inSession && si.next === iso('2026-10-28T13:30:00Z'), 'EDT 開盤');
assert.strictEqual(fClock(si.next, TW_TZ), '21:30'); assert.strictEqual(fClock(si.next, NY_TZ), '09:30');
si = sessionInfo(iso('2026-10-28T15:00:00Z'));
assert(si.inSession && si.end === iso('2026-10-28T17:30:00Z'), 'EDT 時段中');
assert.strictEqual(fHMS(si.end - iso('2026-10-28T15:00:00Z')), '2:30:00');
// 收盤後 → 下一個交易日
si = sessionInfo(iso('2026-10-28T18:00:00Z'));
assert(!si.inSession && si.next === iso('2026-10-29T13:30:00Z'));
// 冬令(EST,UTC-5):2026-11-02 週一。開盤 14:30Z = 台北 22:30(使用者說的「變 22:30」)
si = sessionInfo(iso('2026-11-02T10:00:00Z'));
assert(si.next === iso('2026-11-02T14:30:00Z'), 'EST 開盤');
assert.strictEqual(fClock(si.next, TW_TZ), '22:30');
// 夏令轉冬令的週日(11/1):週日不是交易日 → 下一個開盤在 11/2 週一
si = sessionInfo(iso('2026-11-01T20:00:00Z'));
assert(si.next === iso('2026-11-02T14:30:00Z'), '週日跳到週一');
// 週五收盤後 → 下週一
si = sessionInfo(iso('2026-10-30T20:00:00Z'));
assert(si.next === iso('2026-11-02T14:30:00Z'), '週五跳週一');
console.log('glance time ok');
