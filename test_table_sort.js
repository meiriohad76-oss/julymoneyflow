/**
 * Table sorting + alignment tests, run against the REAL generated dashboard.
 *
 * Why against the real file rather than fixtures: the sorter has to cope with
 * whatever the Python side actually emits — "+0.46%", "$150M", "1.14x", "+1.5σ",
 * "●●○", ISO dates, "–" placeholders, and cells wrapped in <span>/<div>. A
 * fixture would only test the cases I remembered to write down.
 *
 * Requires jsdom:  npm install jsdom
 *
 *   node test_table_sort.js
 */
const fs = require('fs');
const path = require('path');
let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  try { ({ JSDOM } = require('/tmp/node_modules/jsdom')); }
  catch (e2) {
    console.error('jsdom not installed — run: npm install jsdom');
    process.exit(2);
  }
}

const HTML = path.join(__dirname, 'output', 'dashboard.html');
if (!fs.existsSync(HTML)) {
  console.error('output/dashboard.html not found — run `python run.py` first');
  process.exit(2);
}

// jsdom does not implement scrollIntoView; openDetail calls it after rendering.
// Stub it before the page script runs so the drill-down test isn't drowned in
// stack traces for a browser API that has nothing to do with sorting.
const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously',
  beforeParse(w) { w.Element.prototype.scrollIntoView = function () {}; },
});
const { window } = dom;
const { document } = window;

let pass = 0, fail = 0;
const ok = (cond, msg) => { if (cond) { pass++; } else { fail++; console.log('  FAIL: ' + msg); } };
const eq = (a, b, msg) => ok(Object.is(a, b), `${msg} — got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

console.log('\n=== 1. cellValue parsing ===');
const cv = (t) => { const td = document.createElement('td'); td.innerHTML = t; return window.cellValue(td); };

eq(cv('+0.46%'), 0.46, 'signed percent');
eq(cv('<span class="r">-1.23%</span>'), -1.23, 'negative inside span');
eq(cv('−0.169%'), -0.169, 'unicode minus (U+2212)');
eq(cv('–0.5'), -0.5, 'en-dash used as minus');
eq(cv('$150M'), 150e6, 'dollar millions');
eq(cv('$1.5B'), 1.5e9, 'dollar billions');
eq(cv('820K'), 820e3, 'thousands');
eq(cv('2,512'), 2512, 'thousands separator');
eq(cv('1.14x'), 1.14, 'multiple suffix');
eq(cv('+1.5σ'), 1.5, 'sigma suffix');
eq(cv('95'), 95, 'bare integer');
eq(cv('2026-07-24'), 20260724, 'ISO date -> ordinal');
eq(cv('●●○'), 2, 'two of three lights');
eq(cv('○○○'), 0, 'zero lights');
eq(cv('●●●'), 3, 'three lights');
eq(cv('–'), null, 'en-dash placeholder -> null');
eq(cv(''), null, 'empty -> null');
eq(cv('   '), null, 'whitespace -> null');
eq(cv('<span class="d">–</span>'), null, 'placeholder inside span');
eq(cv('held'), 'held', 'text lowercased');
eq(cv('Capital Flight'), 'capital flight', 'multiword text');

// Ordering sanity: dates must compare in calendar order, not lexically.
ok(cv('2026-01-05') < cv('2026-07-24'), 'dates order correctly');
ok(cv('2025-12-31') < cv('2026-01-01'), 'dates order across a year boundary');
// Magnitude suffixes must not collapse: 1.5B > 150M > 820K.
ok(cv('$1.5B') > cv('$150M') && cv('$150M') > cv('820K'), 'magnitude suffixes rank correctly');

console.log('\n=== 2. every table got a sorter ===');
const tables = Array.from(document.querySelectorAll('table'));
console.log(`  found ${tables.length} tables`);
ok(tables.length >= 5, 'dashboard has several tables');

let sortable = 0, skippedNoHead = 0, ranking = 0;
for (const t of tables) {
  if (t.closest('#tbl1, #tbl2')) { ranking++; continue; }
  if (!t.tHead) { skippedNoHead++; continue; }
  if (!t.tBodies[0] || t.tBodies[0].rows.length < 2) continue;
  ok(t.dataset.sortable === '1', `table with ${t.tBodies[0].rows.length} rows is sortable`);
  if (t.dataset.sortable === '1') sortable++;
}
console.log(`  sortable: ${sortable}   ranking (own sorter): ${ranking}   headerless: ${skippedNoHead}`);
ok(sortable >= 3, 'multiple server-rendered tables are sortable');

console.log('\n=== 2b. header and body column counts agree ===');
// A ragged table crashed the sorter for real: a duplicate column was removed
// from the body but left in the header, so every row was one cell short.
for (const t of tables) {
  if (!t.tHead || !t.tBodies[0] || !t.tBodies[0].rows.length) continue;
  const nHead = t.tHead.rows[0].cells.length;
  const name = t.tHead.rows[0].cells[0]?.textContent.trim() || '?';
  for (const r of t.tBodies[0].rows) {
    // Placeholder rows legitimately use colspan.
    if (r.cells.length === 1 && r.cells[0].hasAttribute('colspan')) continue;
    eq(r.cells.length, nHead, `"${name}" row has one cell per header`);
  }
}

console.log('\n=== 3. sorting actually orders rows ===');
function colValues(t, col) {
  return Array.from(t.tBodies[0].rows).map(r => window.cellValue(r.cells[col]));
}
function isSorted(vals, dir) {
  const v = vals.filter(x => x !== null);            // nulls are parked at the end
  for (let i = 1; i < v.length; i++) {
    if (typeof v[i] === 'number' && typeof v[i-1] === 'number') {
      if (dir === 'desc' && v[i] > v[i-1] + 1e-9) return false;
      if (dir === 'asc'  && v[i] < v[i-1] - 1e-9) return false;
    } else {
      const c = String(v[i-1]).localeCompare(String(v[i]));
      if (dir === 'desc' && c < 0) return false;
      if (dir === 'asc'  && c > 0) return false;
    }
  }
  return true;
}
function nullsLast(vals) {
  let seenNull = false;
  for (const v of vals) { if (v === null) seenNull = true; else if (seenNull) return false; }
  return true;
}

let tested = 0;
for (const t of tables) {
  if (t.dataset.sortable !== '1') continue;
  const nCols = t.tHead.rows[0].cells.length;
  for (let c = 0; c < nCols; c++) {
    const th = t.tHead.rows[0].cells[c];
    const before = Array.from(t.tBodies[0].rows).map(r => r.outerHTML);

    th.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    const desc = colValues(t, c);
    ok(isSorted(desc, 'desc'), `col ${c} descending`);
    ok(nullsLast(desc), `col ${c} descending keeps blanks last`);

    th.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    const asc = colValues(t, c);
    ok(isSorted(asc, 'asc'), `col ${c} ascending`);
    // The important one: blanks must NOT float to the top on an ascending sort,
    // which would bury the rows the user is looking for.
    ok(nullsLast(asc), `col ${c} ascending keeps blanks last`);

    th.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    const after = Array.from(t.tBodies[0].rows).map(r => r.outerHTML);
    eq(after.join('|') === before.join('|'), true, `col ${c} third click restores original order`);

    // No row may be lost or duplicated by any of that.
    eq(after.length, before.length, `col ${c} row count preserved`);
    tested++;
  }
}
console.log(`  exercised ${tested} columns through desc/asc/reset`);
ok(tested > 0, 'at least one column was exercised');

console.log('\n=== 4. only one sort indicator at a time ===');
for (const t of tables) {
  if (t.dataset.sortable !== '1') continue;
  const heads = Array.from(t.tHead.rows[0].cells);
  heads[0].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  if (heads.length > 1) heads[1].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const marked = heads.filter(h => (h.querySelector('.sarr')?.textContent || '').trim() !== '');
  eq(marked.length, 1, 'exactly one header shows an arrow after sorting a second column');
  const dirs = heads.filter(h => h.dataset.dir);
  eq(dirs.length, 1, 'exactly one header retains a direction');
}

console.log('\n=== 5. header and body alignment agree ===');
let checked = 0, mismatches = [];
for (const t of tables) {
  if (!t.tHead || !t.tBodies[0]) continue;
  const heads = Array.from(t.tHead.rows[0].cells);
  const rows = Array.from(t.tBodies[0].rows);
  if (!rows.length) continue;
  heads.forEach((th, c) => {
    const hAlign = th.style.textAlign;
    if (!hAlign) return;
    for (const r of rows) {
      const td = r.cells[c];
      if (!td) continue;
      if (td.style.textAlign !== hAlign) {
        mismatches.push(`${th.textContent.trim()} col${c}: th=${hAlign} td=${td.style.textAlign}`);
      }
    }
    checked++;
  });
}
eq(mismatches.length, 0, 'no header/body alignment mismatch: ' + mismatches.slice(0, 5).join('; '));
console.log(`  checked ${checked} columns`);
ok(checked > 0, 'alignment was actually applied');

console.log('\n=== 6. numeric columns right, text columns left ===');
// Spot-check the ranking table, whose columns we know by name.
const rank = document.querySelector('#tbl1 table');
if (rank) {
  const heads = Array.from(rank.tHead.rows[0].cells);
  const byName = {};
  heads.forEach((h, i) => { byName[h.textContent.replace(/[▾▴\s]+$/, '').trim()] = i; });
  const expectRight = ['Mansfield RS', 'RS-Ratio', 'RS-Mom', 'Breadth', 'CMF', '21d %', 'Vol σ'];
  const expectLeft  = ['Sector', 'Quadrant', 'Signal'];
  for (const n of expectRight) {
    if (byName[n] === undefined) continue;
    eq(heads[byName[n]].style.textAlign, 'right', `"${n}" is right-aligned`);
  }
  for (const n of expectLeft) {
    if (byName[n] === undefined) continue;
    eq(heads[byName[n]].style.textAlign, 'left', `"${n}" is left-aligned`);
  }
} else {
  ok(false, 'ranking table present');
}

console.log('\n=== 7. ranking tables keep their own sorter ===');
const r1 = document.querySelector('#tbl1 table');
ok(r1 && r1.dataset.sortable !== '1', 'ranking table did not get the generic sorter (would double-bind)');
if (r1) {
  const th = r1.tHead.rows[0].cells[1];
  const first = () => document.querySelector('#tbl1 tbody tr')?.dataset.tk;
  const a = first();
  th.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const b = first();
  ok(a !== undefined && b !== undefined, 'ranking table still sorts on click');
  // And re-rendering must not lose alignment (innerHTML wipes inline styles).
  const t2 = document.querySelector('#tbl1 table');
  ok(!!t2.tHead.rows[0].cells[1].style.textAlign,
     'alignment reapplied after the ranking table re-renders');
}

console.log('\n=== 8. detail-panel tables (built on demand) ===');
const row = document.querySelector('#tbl1 tbody tr');
ok(!!row, 'a ranking row exists to drill into');
if (row) {
  row.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const detail = document.getElementById('detail');
  ok(detail.classList.contains('open'), 'detail panel opened');

  const dTables = Array.from(detail.querySelectorAll('table'));
  console.log(`  detail has ${dTables.length} tables`);
  ok(dTables.length >= 2, 'detail rendered its tables');

  let dSortable = 0, dAligned = 0;
  for (const t of dTables) {
    if (!t.tHead) continue;                       // headerless key/value spec table
    if (t.tBodies[0] && t.tBodies[0].rows.length >= 2) {
      ok(t.dataset.sortable === '1', 'detail table is sortable');
      if (t.dataset.sortable === '1') dSortable++;
    }
    const heads = Array.from(t.tHead.rows[0].cells);
    if (heads.some(h => h.style.textAlign)) dAligned++;
    // header/body agreement again, on freshly-built DOM
    heads.forEach((th, c) => {
      if (!th.style.textAlign) return;
      for (const r of t.tBodies[0].rows) {
        const td = r.cells[c];
        if (td) eq(td.style.textAlign, th.style.textAlign,
                   `detail col ${c} body matches header`);
      }
    });
  }
  console.log(`  sortable: ${dSortable}   aligned: ${dAligned}`);
  ok(dAligned > 0, 'detail tables were aligned');

  // Sorting one of them must not corrupt it.
  const t = dTables.find(x => x.dataset.sortable === '1');
  if (t) {
    const n = t.tBodies[0].rows.length;
    t.tHead.rows[0].cells[1].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    eq(t.tBodies[0].rows.length, n, 'detail sort preserves row count');
  }

  // Re-opening must not double-bind handlers or duplicate arrows.
  row.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const reopened = Array.from(detail.querySelectorAll('table'));
  for (const t2 of reopened) {
    if (!t2.tHead) continue;
    for (const th of t2.tHead.rows[0].cells) {
      ok(th.querySelectorAll('.sarr').length <= 1,
         'no duplicate sort arrow after reopening the detail panel');
    }
  }
}

console.log(`\n${pass} passed, ${fail} failed\n`);
process.exit(fail ? 1 : 0);
