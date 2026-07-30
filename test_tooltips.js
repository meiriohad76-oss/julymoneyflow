/**
 * Tooltip + trend tests, run against the REAL generated dashboard.
 *
 * The point of testing against the real file: the tooltips have to survive every
 * value the pipeline actually produces — nulls, missing series, sectors with no
 * tick coverage, categorical fields with unseen states. A fixture would only
 * cover the cases I thought of.
 *
 * Requires jsdom:  npm install jsdom
 *
 *   node test_tooltips.js
 */
const fs = require('fs');
const path = require('path');
let JSDOM;
try { ({ JSDOM } = require('jsdom')); }
catch (e) {
  try { ({ JSDOM } = require('/tmp/node_modules/jsdom')); }
  catch (e2) { console.error('jsdom not installed — run: npm install jsdom'); process.exit(2); }
}

const HTML = path.join(__dirname, 'output', 'dashboard.html');
if (!fs.existsSync(HTML)) {
  console.error('output/dashboard.html not found — run `python run.py --offline` first');
  process.exit(2);
}

const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously',
  beforeParse(w) { w.Element.prototype.scrollIntoView = function () {}; },
});
const { window } = dom;
const { document } = window;
// Function declarations become window properties; top-level `const` does not —
// it lands in the global lexical environment instead. window.eval reaches both,
// so the test can read the real objects without exporting them just for testing.
const { cellTip, tipFor, trendOf, seriesTrend, median, signRun, bandText } = window;
const META = window.eval('META');
const TC = window.eval('TC');
const D = window.__SMF__;

let pass = 0, fail = 0;
const ok = (c, m) => { if (c) pass++; else { fail++; console.log('  FAIL: ' + m); } };
const eq = (a, b, m) => ok(Object.is(a, b), `${m} — got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

console.log('\n=== 1. trend config came from config.py, not a JS literal ===');
ok(TC && TC.smooth === 5, 'smooth reached the page');
ok(TC.fast === 21 && TC.slow === 63, 'horizons reached the page');
ok(TC.min_history === 40, 'min_history reached the page');

console.log('\n=== 2. trend windows are disjoint (the volume_trend bug class) ===');
// Construct a series with a known step change and verify the delta recovers it.
// If the baseline overlapped the measurement window, the step would be damped.
// The step must land BETWEEN the two windows to be measurable: prior reads
// indices 174-178, recent reads 195-199, so the level must change in between.
const step = new Array(180).fill(10).concat(new Array(20).fill(20));
const t = seriesTrend(step, 21);
ok(t !== null, 'trend computed on a long series');
eq(t.d, 10, 'a clean +10 step between the windows is recovered exactly, undamped');
eq(t.hz, 21, 'horizon reported back');
// A step entirely INSIDE the lookback (older than both windows) is already
// fully absorbed and must read zero — otherwise the indicator would keep
// reporting a month-old move as if it were current.
const oldStep = new Array(150).fill(10).concat(new Array(50).fill(20));
eq(seriesTrend(oldStep, 21).d, 0, 'a step older than both windows reads zero');

// Overlap check, stated directly: recent window and prior window share no index.
const N = 200, S = TC.smooth, HZ = 21;
const recentIdx = new Set(); for (let i = N - S; i < N; i++) recentIdx.add(i);
const priorIdx = []; for (let i = N - HZ - S; i < N - HZ; i++) priorIdx.push(i);
ok(priorIdx.every(i => !recentIdx.has(i)), 'prior window shares no index with recent window');

console.log('\n=== 3. trend refuses to guess on thin history ===');
eq(seriesTrend(new Array(20).fill(1), 21), null, 'too short -> null, not a fabricated 0');
eq(seriesTrend(new Array(30).fill(1), 21), null, 'below min_history -> null');
eq(seriesTrend([], 21), null, 'empty -> null');
eq(seriesTrend(null, 21), null, 'missing -> null');
ok(seriesTrend(new Array(200).fill(1), 21) !== null, 'enough history -> computed');
eq(seriesTrend(new Array(200).fill(7), 21).d, 0, 'a flat series has zero trend');

console.log('\n=== 4. helpers ===');
eq(median([3, 1, 2]), 2, 'odd-length median');
eq(median([4, 1, 2, 3]), 2.5, 'even-length median');
eq(signRun([1, 2, 3]), 3, 'all positive');
eq(signRun([-1, -2, 3, 4]), 2, 'sign run stops at the flip');
eq(signRun([1, 2, -3]), 1, 'run of one');
eq(signRun([]), 0, 'empty run');
// Medians, not means: one wild spike must not move the answer much.
const spiky = new Array(200).fill(10); spiky[199] = 1000;
const ts = seriesTrend(spiky, 21);
ok(Math.abs(ts.d) < 1, `a single 100x spike barely moves a median-based delta (got ${ts.d})`);

console.log('\n=== 5. every indicator has complete metadata ===');
const keys = Object.keys(META);
console.log(`  ${keys.length} indicators registered`);
ok(keys.length >= 20, 'registry is populated');
for (const k of keys) {
  const m = META[k];
  ok(!!m.label, `${k} has a label`);
  ok(!!m.what && m.what.length > 40, `${k} has a plain-language definition`);
  ok(m.cls === 'A' || m.cls === 'B' || m.cls === 'C', `${k} has a valid class`);
  if (m.cls === 'C') {
    ok(!!m.states, `${k} (categorical) has states, not bands`);
    ok(!m.bands, `${k} (categorical) has no bands — a delta there is meaningless`);
  } else {
    // A single catch-all band is legitimate where the LEVEL carries no meaning
    // and only the direction does — the Fed balance sheet is a good example.
    ok(!!m.bands && m.bands.length >= 1, `${k} has interpretation bands`);
    // Bands must be ascending and terminate at Infinity, or values fall through.
    const ubs = m.bands.map(b => b[0]);
    ok(ubs.every((v, i) => i === 0 || v > ubs[i - 1]), `${k} bands ascend`);
    eq(ubs[ubs.length - 1], Infinity, `${k} bands terminate at Infinity`);
    ok(m.bands.every(b => typeof b[1] === 'string' && b[1].length > 8),
       `${k} every band has readable text`);
  }
}

console.log('\n=== 6. bands cover the whole real line ===');
for (const k of keys) {
  const m = META[k];
  if (m.cls === 'C') continue;
  for (const v of [-1e9, -100, -1, -0.001, 0, 0.001, 1, 100, 1e9]) {
    const txt = bandText(k, v);
    ok(typeof txt === 'string' && txt.length > 0, `${k} interprets ${v}`);
  }
}

console.log('\n=== 7. cellTip renders for every indicator on every sector ===');
const sectors = D.sectors;
console.log(`  ${sectors.length} sectors x ${keys.length} indicators = ${sectors.length * keys.length} tooltips`);
let rendered = 0, withTrend = 0, withSpark = 0, withWarn = 0, empty = 0;
for (const s of sectors) {
  for (const k of keys) {
    let html;
    try { html = cellTip(k, s); }
    catch (err) { fail++; console.log(`  FAIL: cellTip('${k}','${s.ticker}') threw ${err.message}`); continue; }
    if (typeof html !== 'string' || !html.length) { empty++; continue; }
    rendered++;
    // Must never leak a raw null/undefined/NaN into the user's face.
    ok(!/>\s*(null|undefined|NaN)\s*</.test(html),
       `${s.ticker}/${k} shows no raw null/undefined/NaN`);
    // Must always carry the Layer 1 definition.
    ok(html.includes('tt-def'), `${s.ticker}/${k} includes a definition`);
    if (html.includes('▲') || html.includes('▼') || html.includes('▬')) withTrend++;
    if (html.includes('<svg')) withSpark++;
    if (html.includes('tt-warn')) withWarn++;
  }
}
console.log(`  rendered ${rendered}, empty ${empty}, with trend ${withTrend}, with sparkline ${withSpark}, with warning ${withWarn}`);
eq(empty, 0, 'no tooltip came back empty');
ok(withTrend > 0, 'some tooltips show a trend');
ok(withSpark > 0, 'some tooltips show a sparkline');

console.log('\n=== 8. the two warnings that must never be dropped ===');
const anyS = sectors[0];
// Assert the SUBSTANCE, not a particular phrasing — otherwise improving the
// wording breaks the test and the temptation is to revert the wording.
const csriTip = cellTip('csri', anyS);
ok(/do not trade|not predictive|failed validation/i.test(csriTip),
   'CSRI tooltip tells the reader not to trade on it');
ok(/coin flip|50 (times )?in 100/i.test(csriTip),
   'CSRI tooltip quantifies how weak it is');
ok(csriTip.includes('VMS'), 'CSRI tooltip points at VMS instead');
ok(/not a forecast|descriptive|not a validated/i.test(cellTip('phase', anyS)),
   'phase tooltip flags that it is descriptive only');
const proxied = sectors.find(x => x.inst_flow_regime !== 'observed');
if (proxied) {
  ok(cellTip('inst_flow_score', proxied).includes('Inferred from daily bars'),
     'proxied institutional flow says so');
} else {
  console.log('  (all sectors have observed flow — proxy warning not exercised)');
}
const observed = sectors.find(x => x.inst_flow_regime === 'observed');
if (observed) {
  ok(!cellTip('inst_flow_score', observed).includes('Inferred from daily bars'),
     'observed institutional flow does NOT carry the proxy warning');
}
ok(cellTip('days_to_cover', anyS).includes('lag'),
   'days-to-cover tooltip mentions the publication lag');

console.log('\n=== 9. trend direction is coloured by favourable, not by up ===');
// days_to_cover has good:0 (ambiguous), so it must never be coloured green/red.
const dtc = sectors.find(x => x.days_to_cover != null);
if (dtc) {
  const m = META['days_to_cover'];
  eq(m.good, 0, 'days_to_cover is marked directionally ambiguous');
}
eq(META['mansfield_rs'].good, 1, 'Mansfield: up is good');
eq(META['volume_z'].good, 0, 'unusual volume is directionally neutral');

console.log('\n=== 10. slow indicators use the slow horizon ===');
// A 21-day delta on a 200-day average is mostly noise; these must use 63.
eq(META['mansfield_rs'].hz, TC.slow, 'Mansfield RS uses the 63-day horizon');
eq(META['mom_12_1'].hz, TC.slow, '12-1 momentum uses the 63-day horizon');
eq(META['days_to_cover'].hz, TC.slow, 'days-to-cover uses the 63-day horizon');
eq(META['breadth'].hz, TC.fast, 'breadth (fast) uses 21 days');
eq(META['cmf'].hz, TC.fast, 'CMF (21d accumulation) uses 21 days');

console.log('\n=== 11. table cells route to the right indicator ===');
const cells = Array.from(document.querySelectorAll('#tbl1 tbody tr:first-child td[data-k]'));
console.log(`  ${cells.length} keyed cells in the first row`);
ok(cells.length >= 12, 'most columns are keyed');
for (const c of cells) ok(!!META[c.dataset.k], `cell key "${c.dataset.k}" exists in META`);
const headers = Array.from(document.querySelectorAll('#tbl1 thead th[data-k]'));
const keyedHeaders = headers.filter(h => META[h.dataset.k]);
ok(keyedHeaders.length >= 12, `${keyedHeaders.length} headers resolve to an indicator`);

console.log('\n=== 12. inline sparklines rendered ===');
const inline = document.querySelectorAll('#tbl1 td .cs, #tbl2 td .cs');
console.log(`  ${inline.length} inline sparklines`);
ok(inline.length > 0, 'sparklines present in the ranking tables');
// Every sparkline must sit in a column listed for it, and carry a real polyline.
for (const sp of Array.from(inline).slice(0, 40)) {
  const td = sp.closest('td');
  ok(!!td && !!td.dataset.k, 'sparkline sits in a keyed cell');
  const poly = sp.querySelector('polyline');
  ok(!!poly && (poly.getAttribute('points') || '').split(' ').length >= 4,
     'sparkline has a real polyline');
}

console.log('\n=== 13. sparklines do not break sorting or alignment ===');
// cellValue reads textContent; an SVG contributes none, so the number must win.
const cv = window.cellValue;
const vmsCell = document.querySelector('#tbl1 tbody tr td[data-k="vms"]');
if (vmsCell) {
  const v = cv(vmsCell);
  ok(typeof v === 'number' && Number.isFinite(v),
     `VMS cell with a sparkline still parses as a number (got ${JSON.stringify(v)})`);
}
const mCell = document.querySelector('#tbl1 tbody tr td[data-k="mansfield_rs"]');
if (mCell) ok(typeof cv(mCell) === 'number', 'Mansfield cell still parses as a number');

console.log('\n=== 14. price chart x-axis is now truthful ===');
const s0 = sectors[0];
eq(s0.series.dates.length, s0.series.price.length,
   'dates align 1:1 with price points');
ok(s0.series.rrg_dates && s0.series.rrg_dates.length === 12,
   'RRG keeps its own 12-point date array');
eq(s0.series.dates[s0.series.dates.length - 1], s0.as_of,
   'last chart date equals as_of');
// ~120 sessions is ~5-7 calendar months; the old bug made this ~2 weeks.
const span = (new Date(s0.series.dates[s0.series.dates.length - 1])
            - new Date(s0.series.dates[0])) / 86400000;
ok(span > 120 && span < 260, `chart spans ${Math.round(span)} calendar days, consistent with 120 sessions`);

console.log('\n=== 15. row tooltip still works as the fallback ===');
const rt = tipFor(sectors[0]);
ok(rt.includes('tt-h'), 'row tooltip has a heading');
ok(rt.includes('Hover any single cell'), 'row tooltip explains per-cell hovering');
ok(!/>\s*(null|undefined|NaN)\s*</.test(rt), 'row tooltip leaks no raw nulls');
for (const s of sectors) {
  let h; try { h = tipFor(s); } catch (e) { fail++; console.log(`  FAIL: tipFor(${s.ticker}) threw`); continue; }
  ok(typeof h === 'string' && h.length > 50, `${s.ticker} row tooltip renders`);
}

console.log('\n=== 16. categorical fields carry duration, not deltas ===');
for (const key of ['stage', 'quadrant']) {
  let withDur = 0;
  for (const s of sectors) {
    const d = s[key + '_days'];
    if (d === null || d === undefined) continue;
    withDur++;
    ok(Number.isInteger(d) && d >= 1, `${s.ticker} ${key}_days is a positive integer`);
    // If a previous state is recorded, a transition date must come with it.
    const p = s[key + '_prev'], since = s[key + '_since'];
    if (p !== null && p !== undefined) {
      ok(!!since, `${s.ticker} ${key} has a transition date alongside prev`);
      ok(p !== s[key], `${s.ticker} ${key} prev differs from current`);
      ok(/^\d{4}-\d{2}-\d{2}$/.test(since), `${s.ticker} ${key}_since is an ISO date`);
    }
  }
  console.log(`  ${key}: ${withDur}/${sectors.length} sectors have duration`);
  ok(withDur > sectors.length / 2, `most sectors have ${key} duration`);
  // Categorical tooltips must never show an arrow — a delta is meaningless there.
  const tip = cellTip(key, sectors[0]);
  ok(!/[▲▼▬]/.test(tip), `${key} tooltip shows no delta arrow`);
  ok(/held \d+ session/.test(tip), `${key} tooltip states how long it has held`);
  eq(trendOf(key, sectors[0]), null, `trendOf refuses to produce a delta for ${key}`);
}

console.log('\n=== 17. sparkline endpoints equal the printed number ===');
// A sparkline whose last point disagrees with the value beside it undermines
// both. This is the check that caught the CSRI tick-data discrepancy.
const pairs = [['vms','vms'],['csri','csri'],['mansfield_rs','mansfield'],
               ['breadth','breadth'],['cmf','cmf'],['rs_ratio','rs_ratio'],
               ['rs_momentum','rs_momentum'],['stage','stage'],
               ['ad_balance','ad_balance']];
let checkedEnds = 0;
for (const s of sectors) {
  for (const [k, sk] of pairs) {
    const v = s[k], arr = s.series && s.series[sk];
    if (v == null || !arr || !arr.length) continue;
    const gap = Math.abs(arr[arr.length - 1] - v);
    ok(gap <= Math.max(0.02, Math.abs(v) * 0.02),
       `${s.ticker}/${k}: sparkline ends at ${arr[arr.length-1]}, value is ${v}`);
    checkedEnds++;
  }
}
console.log(`  checked ${checkedEnds} endpoints`);
ok(checkedEnds > 100, 'endpoints were actually checked');

console.log('\n=== 18. display series are decimated, chart series are not ===');
const sx = sectors[0].series;
eq(sx.price.length, 120, 'price keeps every session for the full-size chart');
eq(sx.dates.length, sx.price.length, 'dates still align with price');
for (const k of ['mansfield','breadth','cmf','vms','csri','absorption','ad_balance']) {
  ok(sx[k].length <= 60, `${k} decimated to <=60 points (got ${sx[k].length})`);
  ok(sx[k].length >= 20, `${k} still has enough points to draw (got ${sx[k].length})`);
}
// Decimation must preserve the endpoint, which is drawn as "today".
ok(Math.abs(sx.mansfield[sx.mansfield.length-1] - sectors[0].mansfield_rs) < 0.02,
   'decimation preserved the final observation');

console.log('\n=== 19. VMS sparkline is cross-sectional, like the score ===');
// Every sector in a tier must have the same number of VMS points, because the
// score only exists on dates where the whole peer group does.
for (const tier of [1, 2]) {
  const lens = sectors.filter(s => s.tier === tier && s.series.vms)
                      .map(s => s.series.vms.length);
  if (lens.length > 2) {
    ok(new Set(lens).size === 1,
       `tier ${tier} VMS series all share a length (${[...new Set(lens)].join(',')})`);
  }
}
// A cross-sectional z-score is centred: per-date values across a tier average ~0.
const t1 = sectors.filter(s => s.tier === 1 && s.series.vms);
if (t1.length > 3) {
  const n = t1[0].series.vms.length;
  let worst = 0;
  for (let i = 0; i < n; i++) {
    const mean = t1.reduce((a, s) => a + s.series.vms[i], 0) / t1.length;
    worst = Math.max(worst, Math.abs(mean));
  }
  ok(worst < 0.35, `tier-1 VMS is centred at every date (worst |mean| = ${worst.toFixed(3)})`);
}

console.log('\n=== 20. rotation panel makes no claim the data cannot support ===');
const F = D.flow;
ok(!!F, 'flow payload present');
if (F) {
  // The old panel zipped Nth-strongest with Nth-weakest and drew an arrow. The
  // arrow asserted a relationship between two specific sectors that nothing
  // measures, and produced rows pointing INTO below-average sectors.
  ok(!('pairs' in D), 'the arbitrary pairing payload is gone');
  ok(Array.isArray(F.gaining) && Array.isArray(F.losing), 'two independent lists');

  // Sign discipline: nothing above zero may appear as losing, and vice versa.
  for (const e of F.gaining) ok(e.score > 0, `${e.ticker} listed as gaining has a positive score`);
  for (const e of F.losing)  ok(e.score < 0, `${e.ticker} listed as losing has a negative score`);

  // No sector may appear on both sides.
  const g = new Set(F.gaining.map(e => e.ticker));
  for (const e of F.losing) ok(!g.has(e.ticker), `${e.ticker} appears on one side only`);

  // Each list is ranked by magnitude.
  const desc = a => a.every((v, i) => i === 0 || Math.abs(v) <= Math.abs(a[i-1]));
  ok(desc(F.gaining.map(e => e.score)), 'gaining list is ranked');
  ok(desc(F.losing.map(e => e.score)), 'losing list is ranked');

  // Materiality must be consistent with the stated threshold.
  const z = F.material_z;
  for (const e of [...F.gaining, ...F.losing])
    eq(e.material, Math.abs(e.score) >= z, `${e.ticker} material flag matches |score| >= ${z}`);
  ok(F.n_material <= F.n_total, 'material count is a subset of the total');
  if (F.n_material < F.n_total) ok(!!F.note, 'a note explains the indistinguishable middle');

  // The rendered panel must not reintroduce directional arrows.
  const panel = Array.from(document.querySelectorAll('.flow'))[0];
  ok(!!panel, 'rotation panel rendered');
  if (panel) {
    ok(!panel.textContent.includes('→'), 'panel draws no from->to arrows');
    const heads = Array.from(panel.querySelectorAll('.flow-head')).map(h => h.textContent.trim());
    ok(heads.some(h => /gaining/i.test(h)), 'a "gaining ground" heading exists');
    ok(heads.some(h => /losing/i.test(h)), 'a "losing ground" heading exists');
    const rows = panel.querySelectorAll('.fl-row');
    eq(rows.length, F.gaining.length + F.losing.length, 'every entry rendered a row');
    // Noise-band entries must be visibly de-emphasised, not silently dropped.
    const faint = panel.querySelectorAll('.fl-row.faint').length;
    const expectFaint = [...F.gaining, ...F.losing].filter(e => !e.material).length;
    eq(faint, expectFaint, 'noise-band rows are dimmed rather than hidden');
    for (const r of panel.querySelectorAll('.fl-row.faint'))
      ok(/noise band/i.test(r.textContent), 'dimmed rows say why they are dimmed');
  }
  // The caveat has to survive; it is the whole reason the panel is honest.
  const body = document.body.textContent;
  ok(/Nobody can see money move between sectors/i.test(body),
     'the panel states plainly that the movement is not observed');
}

console.log('\n=== 21. EVERY table has tooltips, not just the ranking ones ===');
// The gap this catches: flow / crowded-shorts / unusual-activity were entirely
// server-rendered and carried no explanation of any kind. A column with no
// data-k is a column the reader cannot ask about.
const allTables = Array.from(document.querySelectorAll('table'));
let bare = [];
for (const t of allTables) {
  const tb = t.tBodies[0];
  if (!tb || !tb.rows.length || !t.tHead) continue;
  const keyed = t.querySelectorAll('td[data-k]').length;
  const cells = tb.rows.length * tb.rows[0].cells.length;
  // The first column is the sector name and needs no tooltip; allow for it.
  if (keyed === 0) bare.push(t.tHead.rows[0].cells[0]?.textContent.trim() || '?');
  ok(keyed > 0 || cells < 4,
     `table "${t.tHead.rows[0].cells[0]?.textContent.trim()}" has explained cells`);
  ok(t.dataset.tips === '1' || !!t.closest('#tbl1, #tbl2'),
     'table is wired for tooltips');
}
if (bare.length) console.log('  tables with no tooltips:', bare.join(', '));
console.log(`  ${allTables.length} tables checked`);

// Every data-k on the page must resolve, or the hover silently does nothing.
const domKeys = new Set();
document.querySelectorAll('[data-k]').forEach(e => domKeys.add(e.dataset.k));
console.log(`  ${domKeys.size} distinct indicator keys in the DOM`);
for (const k of domKeys) ok(!!META[k], `data-k="${k}" resolves to a registry entry`);

console.log('\n=== 22. the specific panels the user flagged ===');
for (const [name, sel] of [['observed flow', 'off_exchange_share'],
                           ['crowded shorts', 'days_to_cover'],
                           ['crowded shorts', 'squeeze_score'],
                           ['crowded shorts', 'divergence'],
                           ['unusual activity', 'dollar_volume_z']]) {
  const cell = document.querySelector(`td[data-k="${sel}"]`);
  ok(!!cell, `${name}: "${sel}" column is explained`);
}
// "What does a squeeze mean? positive? negative?" must be answerable.
const sq = document.body.textContent;
ok(/forced to buy back/i.test(sq), 'the page explains the squeeze mechanism');
ok(/squeeze setup is bullish/i.test(sq), 'the page says plainly whether it is bullish');
ok(/reason to look, not a reason to buy/i.test(sq), 'and qualifies it');

console.log('\n=== 22b. boolean fields never render as raw true/false ===');
for (const k of ['crowded_short', 'divergence']) {
  for (const s of sectors) {
    if (s[k] === undefined || s[k] === null) continue;
    const tip = cellTip(k, s);
    ok(!/<em>\s*(true|false)\s*<\/em>/i.test(tip),
       `${s.ticker}/${k} shows a word, not a raw boolean`);
    ok(/<em>[A-Z]/.test(tip), `${s.ticker}/${k} shows a readable label`);
  }
}

console.log('\n=== 23. duplicate flow columns collapsed ===');
// Polygon reported dark-pool share identical to off-exchange share for every
// sector, so both columns printed the same number — four of eight columns
// carrying one fact.
const withFlow = sectors.filter(s => s.flow && s.off_exchange_share != null);
if (withFlow.length) {
  const identical = withFlow.every(s => s.dark_pool_share === s.off_exchange_share);
  console.log(`  dark == off-exchange in ${withFlow.filter(s => s.dark_pool_share === s.off_exchange_share).length}/${withFlow.length} sectors`);
  if (identical) {
    ok(!document.querySelector('td[data-k="dark_pool_share"]'),
       'the duplicate dark-pool column is hidden when it equals off-exchange');
  }
}

console.log('\n=== 24. definitions are short, readings are specific ===');
// The failure being guarded against: 184 characters of abstract definition
// above a two-word reading of the user's actual number.
let longest = 0, longestKey = '';
for (const k of Object.keys(META)) {
  const w = META[k].what;
  if (w.length > longest) { longest = w.length; longestKey = k; }
  ok(w.length <= 155, `${k}: definition is one sentence (${w.length} chars)`);
  // One sentence, allowing a trailing period and internal abbreviations.
  ok((w.match(/\.\s+[A-Z]/g) || []).length <= 1,
     `${k}: definition does not run to three sentences`);
}
console.log(`  longest definition: ${longestKey} at ${longest} chars`);
// And the reading of the value must not be shorter than a label.
for (const k of Object.keys(META)) {
  const m = META[k];
  if (m.cls === 'C') continue;
  for (const [, txt] of m.bands)
    ok(txt.length >= 12, `${k}: band text "${txt}" says something`);
}

console.log('\n=== 25. a change in a percentage is in POINTS, not percent ===');
// Breadth of 85% that rose from 15% moved 70 percentage points. Rendered as
// "+70%" that reads as a 70 percent relative gain — a different, much smaller
// move. Every percentage-valued indicator must label its delta "pp".
for (const k of ['breadth', 'off_exchange_share', 'dark_pool_share',
                 'dtc_percentile', 'percentile', 'block_share']) {
  const m = META[k];
  if (!m) continue;
  ok(m.dunit === 'pp', `${k}: delta is labelled in percentage points`);
}
const bs = sectors.find(s => s.breadth != null && s.breadth_chg_21d != null);
if (bs) {
  const tip = cellTip('breadth', bs);
  ok(/pp<\/span>|pp\s*<\//.test(tip) || tip.includes('pp'),
     'breadth tooltip shows its change in pp');
  ok(!/[▲▼]\s*[-+][\d.]+%/.test(tip),
     'breadth tooltip does NOT show its change as a percent');
}
// A genuine percent change keeps '%'.
ok(META['si_change_pct'].dunit === undefined,
   'short-interest change is a real percent change and keeps %');

console.log('\n=== 26. price chart ===');
let charted = 0, worstMarkers = 0, totalRaw = 0, totalDrawn = 0;
for (const s of sectors) {
  window.openDetail(s.ticker);
  const d = document.getElementById('detail');
  const svg = d.querySelector('.chartwrap svg');
  ok(!!svg, `${s.ticker} (tier ${s.tier}) renders a price chart`);
  if (!svg) continue;
  charted++;
  // The chart must plot price plus every SMA that has data.
  const lines = svg.querySelectorAll('polyline').length;
  ok(lines >= 2, `${s.ticker}: price and at least one moving average drawn`);
  ok(!!svg.querySelector('linearGradient'), `${s.ticker}: area fill present`);
  ok(d.querySelectorAll('.clegend .ck').length >= 4, `${s.ticker}: chart has a legend`);

  const drawn = d.querySelectorAll('.bkt').length;
  const raw = (s.breakouts || []).length;
  totalRaw += raw; totalDrawn += drawn;
  worstMarkers = Math.max(worstMarkers, drawn);
  ok(drawn <= raw, `${s.ticker}: thinning never invents markers`);
  // Markers must sit inside the plotted window.
  for (const b of s.breakouts || [])
    ok(b.idx >= 0 && b.idx < s.series.price.length,
       `${s.ticker}: breakout index ${b.idx} inside the chart window`);
}
console.log(`  ${charted}/${sectors.length} charts, ${totalRaw} raw markers -> ${totalDrawn} drawn`);
console.log(`  busiest chart: ${worstMarkers} markers`);
ok(charted === sectors.length, 'every sector charts, sub-sectors included');
ok(worstMarkers <= 20, `no chart is confetti (busiest has ${worstMarkers})`);

// Every crossing marker must sit ON the line it crossed, at the interpolated
// intersection — not at the close price, which floated markers up to 2 points
// off their own SMA when price gapped through.
const crossPoint = window.crossPoint;
if (crossPoint) {
  let checked = 0, worst = 0;
  for (const s of sectors) {
    const price = s.series.price, n = price.length;
    const smaMap = { 20: s.series.sma20, 50: s.series.sma50, 150: s.series.sma150 };
    for (const b of (s.breakouts || [])) {
      if (b.idx < 1 || b.idx >= n) continue;
      const arr = smaMap[b.sma]; if (!arr) continue;
      const cp = crossPoint(b, price, arr, n);
      const off = n - arr.length;
      const si = Math.max(0, Math.min(arr.length - 1, Math.round(cp.i) - off));
      const gap = Math.abs(cp.v - arr[si]);
      worst = Math.max(worst, gap);
      checked++;
    }
  }
  ok(checked > 100, 'many crossing markers checked');
  ok(worst < 1.5, `every marker lands on its SMA line (worst gap ${worst.toFixed(2)})`);
  // A parallel/degenerate segment must fall back safely, not produce NaN.
  const deg = crossPoint({ idx: 5, sma: 20, price: 50, direction: 'up' },
                         [50, 50, 50, 50, 50, 50], [50, 50, 50, 50, 50, 50], 6);
  ok(Number.isFinite(deg.i) && Number.isFinite(deg.v), 'degenerate crossing stays finite');
}

// Thinning must keep the FIRST of each cluster, never a later one, and must be
// idempotent — running it twice changes nothing.
const thin = window.thinBreakouts;
const fake = [{sma:20,direction:'up',idx:10},{sma:20,direction:'up',idx:15},
              {sma:20,direction:'up',idx:40},{sma:50,direction:'up',idx:12},
              {sma:20,direction:'down',idx:16}];
const th = thin(fake);
eq(th.length, 4, 'a 20-day repeat 5 sessions later is collapsed');
eq(th[0].idx, 10, 'the first of a cluster is kept, not the last');
ok(th.some(b => b.sma === 50 && b.idx === 12),
   'a different SMA at the same time is NOT collapsed');
ok(th.some(b => b.direction === 'down' && b.idx === 16),
   'the opposite direction is NOT collapsed');
eq(thin(th).length, th.length, 'thinning is idempotent');
eq(thin([]).length, 0, 'thinning handles an empty list');
eq(thin(null).length, 0, 'thinning handles a missing list');

console.log(`\n${pass} passed, ${fail} failed\n`);
process.exit(fail ? 1 : 0);
