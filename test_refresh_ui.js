/**
 * Refresh-button tests.
 *
 * The interesting cases are all about NOT doing something: staying hidden when
 * the page is opened from disk, keeping the quota-spending button disabled while
 * the server says it is rate limited, and never letting a user-supplied mode
 * reach anything that executes.
 *
 * Requires jsdom:  npm install jsdom
 *
 *   node test_refresh_ui.js
 */
const fs = require('fs');
const path = require('path');
let JSDOM;
try { ({ JSDOM } = require('jsdom')); }
catch (e) {
  try { ({ JSDOM } = require('/tmp/node_modules/jsdom')); }
  catch (e2) { console.error('jsdom not installed'); process.exit(2); }
}

const HTML = fs.readFileSync(path.join(__dirname, 'output', 'dashboard.html'), 'utf8');
let pass = 0, fail = 0;
const ok = (c, m) => { if (c) pass++; else { fail++; console.log('  FAIL: ' + m); } };
const eq = (a, b, m) => ok(Object.is(a, b), `${m} - got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

function build(statusObj, opts = {}) {
  const calls = [];
  const dom = new JSDOM(HTML, {
    runScripts: 'dangerously',
    url: 'http://pi.local/',
    beforeParse(w) {
      w.Element.prototype.scrollIntoView = function () {};
      w.confirm = () => opts.confirm !== false;
      w.fetch = (u, o) => {
        calls.push({ url: String(u), method: (o && o.method) || 'GET' });
        if (opts.reject) return Promise.reject(new Error('no server'));
        if (String(u).startsWith('/refresh')) {
          const st = opts.postStatus || 202;
          return Promise.resolve({ ok: st < 400, status: st,
            json: () => Promise.resolve(Object.assign({}, statusObj, opts.postBody || {})) });
        }
        return Promise.resolve({ ok: true, status: 200,
          json: () => Promise.resolve(statusObj) });
      };
    },
  });
  return { dom, calls };
}
const settle = () => new Promise(r => setTimeout(r, 60));

const READY = { built_at: 1785000000, built_age_sec: 120, queued: false,
                running: false, fetch_cooldown_sec: 0, refresh_enabled: true };

(async () => {
  console.log('\n=== 1. opened from disk: no controls at all ===');
  {
    // The static file may be emailed or opened locally. A button that cannot
    // possibly work must not be shown.
    const { dom } = build(READY, { reject: true });
    await settle();
    const box = dom.window.document.getElementById('rfz');
    ok(!!box, 'the control block exists in the markup');
    eq(box.hidden, true, 'controls stay hidden with no server');
    dom.window.close();
  }

  console.log('\n=== 2. served and available: controls appear ===');
  {
    const { dom } = build(READY);
    await settle();
    const d = dom.window.document;
    eq(d.getElementById('rfz').hidden, false, 'controls become visible');
    eq(d.getElementById('rfz-off').disabled, false, 'rebuild is enabled');
    eq(d.getElementById('rfz-get').disabled, false, 'fetch is enabled');
    ok(/cache/i.test(d.getElementById('rfz-off').textContent), 'free button mentions cache');
    ok(/fetch|latest/i.test(d.getElementById('rfz-get').textContent), 'paid button mentions fetching');
    ok(/quota/i.test(d.getElementById('rfz-get').title), 'paid button warns about quota in its tooltip');
    ok(/built/i.test(d.getElementById('rfz-msg').textContent), 'status line reports when data was built');
    dom.window.close();
  }

  console.log('\n=== 3. server disables the feature ===');
  {
    const { dom } = build(Object.assign({}, READY, { refresh_enabled: false }));
    await settle();
    eq(dom.window.document.getElementById('rfz').hidden, true,
       'controls stay hidden when the server says refresh is unavailable');
    dom.window.close();
  }

  console.log('\n=== 4. cooldown protects API quota ===');
  {
    const { dom } = build(Object.assign({}, READY, { fetch_cooldown_sec: 600 }));
    await settle();
    const d = dom.window.document;
    eq(d.getElementById('rfz-get').disabled, true, 'the quota-spending button is disabled');
    eq(d.getElementById('rfz-off').disabled, false, 'the free rebuild stays available');
    ok(/10m/.test(d.getElementById('rfz-msg').textContent),
       'the wait is shown in the status line');
    ok(/rate limited/i.test(d.getElementById('rfz-get').title),
       'tooltip explains why it is disabled');
    dom.window.close();
  }

  console.log('\n=== 5. a run in progress locks both buttons ===');
  {
    const { dom } = build(Object.assign({}, READY, { running: true }));
    await settle();
    const d = dom.window.document;
    eq(d.getElementById('rfz-off').disabled, true, 'rebuild locked while running');
    eq(d.getElementById('rfz-get').disabled, true, 'fetch locked while running');
    ok(/rebuild/i.test(d.getElementById('rfz-msg').textContent), 'progress is reported');
    dom.window.close();
  }

  console.log('\n=== 6. clicking sends the right request ===');
  {
    const { dom, calls } = build(READY);
    await settle();
    calls.length = 0;
    dom.window.document.getElementById('rfz-off').click();
    await settle();
    const post = calls.find(c => c.method === 'POST');
    ok(!!post, 'a POST was issued');
    if (post) {
      eq(post.url, '/refresh?mode=offline', 'rebuild sends mode=offline');
      // Only ever these two words reach the server.
      ok(/mode=(offline|fetch)$/.test(post.url), 'mode is one of the two fixed values');
    }
    dom.window.close();
  }

  console.log('\n=== 7. the paid action asks first ===');
  {
    // Declining the confirm must send nothing at all.
    const { dom, calls } = build(READY, { confirm: false });
    await settle();
    calls.length = 0;
    dom.window.document.getElementById('rfz-get').click();
    await settle();
    eq(calls.filter(c => c.method === 'POST').length, 0,
       'declining the prompt sends no request, so no quota is spent');
    dom.window.close();
  }
  {
    const { dom, calls } = build(READY, { confirm: true });
    await settle();
    calls.length = 0;
    dom.window.document.getElementById('rfz-get').click();
    await settle();
    const post = calls.find(c => c.method === 'POST');
    ok(!!post && post.url === '/refresh?mode=fetch', 'accepting sends mode=fetch');
    dom.window.close();
  }

  console.log('\n=== 8. server rejections are surfaced, not swallowed ===');
  for (const [code, expect] of [[429, /rate limited/i], [409, /already/i],
                                [500, /./]]) {
    const { dom } = build(READY, { postStatus: code,
                                   postBody: { error: 'boom', fetch_cooldown_sec: 300 } });
    await settle();
    dom.window.document.getElementById('rfz-off').click();
    await settle();
    const msg = dom.window.document.getElementById('rfz-msg').textContent;
    ok(expect.test(msg), `HTTP ${code} is explained to the user (got "${msg}")`);
    dom.window.close();
  }

  console.log(`\n${pass} passed, ${fail} failed\n`);
  process.exit(fail ? 1 : 0);
})();
