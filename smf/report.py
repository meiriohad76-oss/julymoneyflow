"""
Dashboard renderer.

Produces a single self-contained HTML file: no CDN, no build step, no network
access required to view. All data is embedded as JSON and drawn with vanilla JS
+ SVG so it works offline and keeps working years from now.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import config

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0e17; --panel:#111827; --panel2:#161f33; --line:#243049;
  --txt:#e6edf7; --dim:#8b9ab5; --dim2:#5d6b87;
  --green:#22d38a; --yellow:#f5c445; --orange:#ff9445; --red:#ff5d6c;
  --blue:#4d9fff; --grey:#5d6b87; --gold:#d4af37;
}
body{background:var(--bg);color:var(--txt);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  padding:20px 24px 60px;max-width:1680px;margin:0 auto}
h1{font-size:22px;font-weight:650;letter-spacing:-.3px}
h2{font-size:15px;font-weight:600;letter-spacing:.02em;text-transform:uppercase;color:var(--dim);margin:28px 0 12px}
h3{font-size:14px;font-weight:600}
a{color:var(--blue);text-decoration:none}
.sub{color:var(--dim);font-size:12.5px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}

header{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;
  border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:4px;flex-wrap:wrap}
.hdr-badges{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.badge{font-size:11px;padding:3px 9px;border-radius:20px;border:1px solid var(--line);
  background:var(--panel);color:var(--dim);white-space:nowrap}
.badge.on{color:var(--green);border-color:rgba(34,211,138,.4)}
.badge.off{color:var(--dim2)}

.grid{display:grid;gap:14px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}

/* regime strip */
.regime{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-top:18px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim2)}
.kpi .v{font-size:24px;font-weight:650;margin-top:4px;letter-spacing:-.5px}
.kpi .n{font-size:11.5px;color:var(--dim);margin-top:5px;line-height:1.35}

.two{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:14px;align-items:start}
@media(max-width:1150px){.two{grid-template-columns:1fr}}

/* RRG */
.rrg-wrap{position:relative}
.rrg-ctl{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap;align-items:center}
.tab{font-size:11.5px;padding:4px 11px;border-radius:6px;border:1px solid var(--line);
  background:var(--panel2);color:var(--dim);cursor:pointer;user-select:none}
.tab:hover{color:var(--txt)}
.tab.active{background:var(--blue);border-color:var(--blue);color:#04122a;font-weight:600}
svg{display:block;max-width:100%;height:auto;overflow:visible}
.q-label{font-size:10.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;fill:var(--dim2)}
.node{cursor:pointer}
.node text{font-size:10.5px;font-weight:650;fill:#04122a;pointer-events:none}
.tail{fill:none;stroke-width:1.4;opacity:.55}

/* tooltip */
#tip{position:fixed;pointer-events:none;z-index:99;background:#040711;border:1px solid var(--line);
  border-radius:8px;padding:10px 12px;font-size:12px;max-width:290px;opacity:0;transition:opacity .1s;
  box-shadow:0 8px 28px rgba(0,0,0,.6)}
#tip .tt-h{font-weight:650;margin-bottom:6px;font-size:12.5px}
#tip .tt-r{display:flex;justify-content:space-between;gap:14px;color:var(--dim)}
#tip .tt-r b{color:var(--txt);font-weight:550}
/* per-indicator tooltip: big value, then what it is, then what THIS value means */
#tip.wide{max-width:340px}
#tip .tt-val{display:flex;align-items:baseline;gap:9px;margin:2px 0 8px}
#tip .tt-val em{font-style:normal;font-size:19px;font-weight:650;color:var(--txt);
  font-variant-numeric:tabular-nums}
#tip .tt-val span{font-size:11.5px;color:var(--dim)}
#tip .tt-def{color:var(--dim2);line-height:1.5;font-size:11px;margin-top:9px;
  padding-top:8px;border-top:1px solid var(--line)}
#tip .tt-def span{display:block;font-size:9.5px;text-transform:uppercase;
  letter-spacing:.06em;color:var(--dim2);opacity:.75;margin-bottom:3px}
#tip .tt-say{color:var(--txt);line-height:1.5;font-size:12px;
  border-left:2px solid var(--line);padding-left:8px}
#tip .tt-warn{color:var(--orange);line-height:1.45;font-size:11.5px;margin-top:7px;
  border-left:2px solid var(--orange);padding-left:8px}
#tip .tt-meta{color:var(--dim2);font-size:10.5px;margin-top:7px;
  display:flex;justify-content:space-between;gap:10px}
#tip .tt-sp{margin:6px 0 8px;height:34px}
#tip .tt-h-sub{color:var(--dim2);font-size:10.5px}
/* inline sparkline in a table cell — sits left of the number, never wraps */
.cs{display:inline-block;vertical-align:middle;margin-right:5px;opacity:.8}
td:hover .cs{opacity:1}
/* sparkline + bar side by side; the bar is block-level and would otherwise wrap */
.cw{display:flex;align-items:center;gap:5px;justify-content:flex-end}
.cw .bar{flex:0 0 auto}
.cw .cs{margin-right:0}

/* alert feed */
.feed{max-height:452px;overflow-y:auto;padding-right:4px}
.feed::-webkit-scrollbar{width:7px}
.feed::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
.alert{border-left:3px solid var(--grey);background:var(--panel2);border-radius:0 8px 8px 0;
  padding:10px 13px;margin-bottom:8px}
.alert.green{border-color:var(--green)} .alert.yellow{border-color:var(--yellow)}
.alert.orange{border-color:var(--orange)} .alert.red{border-color:var(--red)}
.alert .a-h{display:flex;justify-content:space-between;gap:10px;align-items:baseline;margin-bottom:3px}
.alert .a-t{font-weight:620;font-size:13px}
.alert .a-p{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;font-weight:650;white-space:nowrap}
.alert ul{margin:5px 0 0 15px;color:var(--dim);font-size:12px}
.alert li{margin-bottom:2px}
.g{color:var(--green)}.y{color:var(--yellow)}.o{color:var(--orange)}.r{color:var(--red)}.d{color:var(--dim)}

/* tables */
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:right;padding:7px 9px;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--dim2);border-bottom:1px solid var(--line);font-weight:600;white-space:nowrap;
  position:sticky;top:0;background:var(--panel);cursor:pointer;user-select:none}
th:hover{color:var(--txt)}
/* Headers are right-aligned, so cells must be too — otherwise every numeric
   column has its label floating over the far edge of left-aligned digits.
   JS re-decides per column (see alignColumns) and overrides both together. */
td{padding:7px 9px;border-bottom:1px solid rgba(36,48,73,.5);white-space:nowrap;
  text-align:right}
th:first-child,td:first-child{text-align:left}
th.l,td.l{text-align:left}
.sarr{color:var(--blue);font-weight:700}
th[data-dir=asc],th[data-dir=desc]{color:var(--txt)}
tbody tr:hover{background:var(--panel2)}
tbody tr{cursor:pointer}
.tk{font-weight:650}
.nm{color:var(--dim);font-size:11.5px}
.pill{display:inline-block;font-size:10px;font-weight:650;padding:2px 7px;border-radius:5px;
  letter-spacing:.03em;text-transform:uppercase}
.pill.green{background:rgba(34,211,138,.16);color:var(--green)}
.pill.yellow{background:rgba(245,196,69,.16);color:var(--yellow)}
.pill.orange{background:rgba(255,148,69,.16);color:var(--orange)}
.pill.red{background:rgba(255,93,108,.16);color:var(--red)}
.pill.grey{background:rgba(93,107,135,.16);color:var(--dim)}
.bar{position:relative;height:16px;background:var(--panel2);border-radius:3px;min-width:78px;overflow:hidden}
.bar i{position:absolute;top:0;bottom:0;left:50%;border-radius:2px}
.bar span{position:absolute;left:0;right:0;text-align:center;font-size:10.5px;line-height:16px;
  font-variant-numeric:tabular-nums}
.mid{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line)}

/* detail */
.detail{display:none;margin-top:14px}
.detail.open{display:block}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-top:14px}
.mcard{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:11px 13px}
.mcard .mk{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim2)}
.mcard .mv{font-size:19px;font-weight:650;margin:3px 0}
.mcard .mn{font-size:11px;color:var(--dim);line-height:1.35}
.spark{margin-top:8px}
.close-x{float:right;color:var(--dim);cursor:pointer;font-size:18px;line-height:1}
.close-x:hover{color:var(--txt)}
.note{font-size:11.5px;color:var(--dim2);margin-top:10px;line-height:1.5}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--dim);margin-top:10px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
/* gaining / losing ground — two independent ranked lists, deliberately not
   joined by arrows (see scoring.rotation_flow for why) */
.flow{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:820px){.flow{grid-template-columns:1fr}}
.flow-head{font-size:11px;font-weight:650;text-transform:uppercase;letter-spacing:.06em;
  padding-bottom:7px;margin-bottom:9px;border-bottom:1px solid var(--line)}
.flow-head.g{color:var(--green)}
.flow-head.r{color:var(--red)}
.fl-row{display:flex;align-items:baseline;gap:12px;padding:9px 11px;margin-bottom:7px;
  background:var(--panel2);border:1px solid var(--line);border-radius:8px}
/* Sectors inside the noise band stay visible but recede, so the reader can see
   where the meaningful signal stops rather than trusting a truncated list. */
.fl-row.faint{opacity:.5;background:transparent}
.fl-score{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums;
  min-width:52px;text-align:right}
.fl-score.g{color:var(--green)}
.fl-score.r{color:var(--red)}
.fl-body{min-width:0}
.fl-name{font-size:13px;color:var(--txt)}
.fl-name b{font-weight:650;margin-right:5px}
.fl-sub{font-size:11px;color:var(--dim);margin-top:2px}
.fl-tag{font-size:9.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim2);
  border:1px solid var(--line);border-radius:4px;padding:1px 5px;margin-left:7px}
.fl-empty{font-size:12px;color:var(--dim);padding:10px 2px}

/* price chart */
.chartwrap{background:#0c1220;border:1px solid var(--line);border-radius:10px;
  padding:10px 12px 4px}
.ax{fill:var(--dim2);font-size:10px;font-family:ui-monospace,Menlo,Consolas,monospace}
.clegend{display:flex;flex-wrap:wrap;gap:14px;align-items:center;
  font-size:10.5px;color:var(--dim);margin-bottom:6px}
.ck{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.ck i{width:12px;height:2.5px;border-radius:2px;display:inline-block}
.ck i.tri{width:0;height:0;background:none;border-left:4px solid transparent;
  border-right:4px solid transparent;border-bottom:7px solid var(--dim)}
.ck i.tri.hollow{border-bottom-color:var(--dim2);opacity:.55}
.ck.sep{margin-left:auto}
.ck.dim{color:var(--dim2);font-style:italic}
.bkt{cursor:pointer}
.bkt:hover polygon{stroke-width:2.2}

/* refresh controls — hidden unless the server reports the feature is available,
   so the static file opened from disk shows no button that cannot work */
.rfz{display:flex;flex-wrap:wrap;align-items:center;gap:8px;width:100%;
  margin-top:8px;justify-content:flex-end}
.rbtn{font:inherit;font-size:11.5px;font-weight:600;color:var(--txt);cursor:pointer;
  background:var(--panel2);border:1px solid var(--line);border-radius:6px;
  padding:6px 11px}
.rbtn:hover:not(:disabled){border-color:var(--blue);color:var(--blue)}
/* the one that spends API quota is visually distinct, deliberately */
.rbtn.paid{border-color:rgba(245,196,69,.45);color:var(--yellow)}
.rbtn.paid:hover:not(:disabled){border-color:var(--yellow);background:rgba(245,196,69,.1)}
.rbtn:disabled{opacity:.45;cursor:not-allowed}
.rfz-msg{font-size:11px;color:var(--dim);flex-basis:100%;text-align:right;
  min-height:14px}
.rfz-msg.err{color:var(--red)}
.rfz-msg.ok{color:var(--green)}
.rfz-msg.work{color:var(--yellow)}

/* trust panel — hit rate instead of rank IC */
.vmeter{display:grid;gap:11px}
.vm-row{display:grid;grid-template-columns:190px 150px 1fr;gap:18px;align-items:center;
  background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
@media(max-width:900px){.vm-row{grid-template-columns:1fr;gap:8px}}
.vm-head b{font-size:14px;font-weight:650}
.vm-full{font-size:10.5px;color:var(--dim2);margin-top:3px}
.vm-verdict{font-size:9.5px;font-weight:650;text-transform:uppercase;letter-spacing:.05em;
  padding:2px 6px;border-radius:4px;margin-left:7px;white-space:nowrap}
.vm-verdict.green{background:rgba(34,211,138,.16);color:var(--green)}
.vm-verdict.red{background:rgba(255,93,108,.16);color:var(--red)}
.vm-verdict.grey{background:rgba(93,107,135,.18);color:var(--dim)}
.vm-num{font-size:21px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.1}
.vm-num span{font-size:10.5px;color:var(--dim2);font-weight:400;margin-left:5px}
.vm-num.green{color:var(--green)}
.vm-num.red{color:var(--red)}
.vm-num.grey{color:var(--dim2)}
/* The bar starts at the coin-flip mark, so its length IS the edge. Anchoring at
   zero would make 52 and 50 look nearly identical and hide the whole point. */
.vm-bar{position:relative;height:7px;background:var(--panel);border-radius:4px;
  margin-top:7px;overflow:hidden}
.vm-fill{position:absolute;top:0;bottom:0;border-radius:3px}
.vm-fill.green{background:var(--green)}
.vm-fill.red{background:var(--red)}
.vm-coin{position:absolute;top:-2px;bottom:-2px;width:1px;background:var(--dim2)}
.vm-none{font-size:11px;color:var(--dim2);margin-top:6px;font-style:italic}
.vm-say{font-size:12px;color:var(--dim);line-height:1.55}
"""

# ---------------------------------------------------------------------------
# JS
# ---------------------------------------------------------------------------
JS = r"""
const D = window.__SMF__;
const COL = {green:'#22d38a',yellow:'#f5c445',orange:'#ff9445',red:'#ff5d6c',grey:'#5d6b87'};

/* ---------------- tooltip ---------------- */
const tip = document.getElementById('tip');
function showTip(html, ev, wide){
  tip.innerHTML = html; tip.style.opacity = 1;
  tip.classList.toggle('wide', !!wide);
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 10) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 10) y = ev.clientY - r.height - 14;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip(){ tip.style.opacity = 0; }

/* =====================================================================
   INDICATOR REGISTRY
   ---------------------------------------------------------------------
   One definition per indicator, driving all three surfaces: the cell
   tooltip, the inline table sparkline, and the drill-down card. Adding an
   indicator in one place makes it appear everywhere, correctly.

   Fields
     label    short name shown as the tooltip heading
     dp,unit  formatting
     cls      'A' zero-centred (sign matters, show a zero line)
               'B' bounded/level (compare to its own history)
               'C' categorical (a delta is meaningless — show duration)
     ser      key in s.series, when a history exists
     zero     draw the sparkline against a symmetric zero axis
     good     +1 up is bullish, -1 down is bullish, 0 ambiguous
     hz       trend horizon in sessions. MUST be >= the indicator's own
              internal averaging window, otherwise the "trend" is just
              noise in our own smoother.
     dfield   pre-computed delta field on the sector, preferred over
              deriving one from the series
     what     Layer 1 — what the indicator is, in plain language
     bands    Layer 2 — what THIS value means. [upperBound, text],
              ascending; the last entry should be Infinity.
     states   for cls 'C': value -> plain-language reading
     warn     a caveat that must travel with the number
   ===================================================================== */
const TC = (window.__SMF__ && window.__SMF__.tcfg) ||
           {smooth:5, fast:21, slow:63, min_history:40, spark_len:120};
const INF = Infinity;

// WRITING RULES for this registry, learned the hard way:
//   `what`  ONE sentence. What the number is, nothing else. If it needs a
//           second sentence it belongs in `warn` or nowhere. An earlier version
//           averaged 184 characters of definition against a two-word reading of
//           the actual value — the reader's own number got less space than the
//           abstract explanation of it.
//   `bands` The part that earns the hover. Say what THIS value means and, where
//           it matters, what to do about it. Concrete beats hedged.
const META = {
vms: {label:'VMS', dp:2, cls:'A', ser:'vms', zero:true, good:1, hz:TC.fast,
  what:'Ranks this sector against its peers on 12-month momentum. The one score '
      +'here that passed testing.',
  bands:[[-1,'Among the weakest of its peers. Momentum is against you.'],
         [0,'Below the peer average. No momentum case for owning it.'],
         [1,'Modestly ahead of peers.'],
         [INF,'Leading its peer group. The strongest reading this dashboard offers.']]},

green_lights: {label:'Green Light Test', dp:0, unit:'/3', cls:'B', good:1, hz:TC.fast,
  what:'Three checks that should agree before you commit: beating the market, '
      +'above its own trend, money flowing in.',
  bands:[[1,'Nothing passes. No case here.'],
         [2,'One of three. Not a setup — one check passing is normal noise.'],
         [3,'Two of three. Watch it, but the setup is incomplete.'],
         [INF,'All three agree. This is the configuration the test exists to find.']]},

csri: {label:'CSRI', dp:2, cls:'A', ser:'csri', zero:true,
  good:1, hz:TC.fast, dfield:'csri_delta_21d',
  what:'A blend of five signals: relative strength, RS-momentum, breadth, money '
      +'flow and institutional footprint.',
  warn:'Do not trade on this. Tested over 10 years it picked the better of two '
      +'sectors 50 times in 100 — a coin flip. Kept as a diagnostic; use VMS.',
  bands:[[-1,'Weak across the blend.'],
         [0,'Below average on the blend.'],
         [1,'Above average on the blend.'],
         [INF,'Strong across the blend.']]},

csri_delta_21d: {label:'CSRI change, 1 month', dp:2, cls:'A', zero:true, good:1,
  what:'How far the CSRI blend moved over the last month.',
  bands:[[-0.75,'Deteriorating fast across the blend.'],
         [-0.25,'Drifting lower.'],
         [0.25,'Flat — no real change this month.'],
         [0.75,'Improving across the blend.'],
         [INF,'Improving fast across the blend.']]},

mansfield_rs: {label:'Mansfield RS', dp:2, cls:'A', ser:'mansfield',
  zero:true, good:1, hz:TC.slow, dfield:null,
  what:'How far this sector is beating the S&P 500, compared with how much it '
      +'normally beats it. Zero = its usual margin.',
  bands:[[-5,'Clearly losing to the market.'],
         [-1,'Behind the market.'],
         [1,'Matching the market. No edge either way.'],
         [5,'Just moved ahead. New leadership, not yet confirmed.'],
         [INF,'Established leadership.']]},

rs_ratio: {label:'RS-Ratio', dp:1, cls:'B', ser:'rs_ratio', good:1, hz:TC.fast,
  what:'Strength against the other sectors right now. Above 100 beats the average '
      +'peer; below 100 trails it.',
  bands:[[97,'Well behind the pack.'],[100,'Slightly behind the pack.'],
         [103,'Slightly ahead of the pack.'],[INF,'Well ahead of the pack.']]},

rs_momentum: {label:'RS-Momentum', dp:1, cls:'B', ser:'rs_momentum',
  good:1, hz:TC.fast,
  what:'Whether that strength is improving or fading — it turns before RS-Ratio '
      +'does, so it is the earlier warning.',
  bands:[[97,'Losing ground to peers quickly.'],
         [100,'Slowly losing ground to peers.'],
         [103,'Gaining ground on peers.'],
         [INF,'Gaining ground on peers quickly.']]},

quadrant: {label:'Rotation quadrant', cls:'C',
  what:'Where the sector sits in the rotation cycle. Money tends to travel '
      +'clockwise: Leading, Weakening, Lagging, Improving, back to Leading.',
  states:{'Leading':'Strong and still improving. The confirmed phase — and the '
                   +'crowded one.',
          'Weakening':'Still strong, but momentum is rolling over. Selling often '
                     +'starts here while the price still looks fine.',
          'Lagging':'Weak and getting weaker.',
          'Improving':'Still weak, but turning up. Early accumulation shows up '
                     +'here, before price confirms it.',
          'Unknown':'Not enough history to place it.'}},

breadth: {label:'Breadth', dp:0, unit:'%', dunit:'pp', cls:'B', ser:'breadth', good:1,
  hz:TC.fast, dfield:'breadth_chg_21d',
  what:'Share of the sector’s holdings above their own 50-day average.',
  bands:[[30,'Very narrow — two or three names are carrying the whole sector.'],
         [50,'Weak participation. The move is not broad.'],
         [70,'Healthy participation.'],
         [INF,'Broad. Most holdings are advancing together.']]},

cmf: {label:'Money flow (CMF)', dp:3, cls:'A', ser:'cmf', zero:true,
  good:1, hz:TC.fast,
  what:'Whether the sector has been closing near the top of its daily range on '
      +'real volume, over the last 21 days.',
  bands:[[-0.10,'Steady selling pressure.'],
         [-0.02,'Mild selling.'],
         [0.02,'Balanced. No clear pressure either way.'],
         [0.10,'Mild buying.'],
         [INF,'Strong buying.']]},

// No `ser`: this score blends observed tick data with daily-bar proxies, and
// tick coverage only goes back ~14 sessions. A sparkline built from the proxy
// alone would look like the history of this number without being it.
inst_flow_score: {label:'Institutional footprint', dp:2, cls:'A',
  zero:true, good:1, hz:TC.fast,
  what:'Signs of large players trading: absorption, accumulation days, block '
      +'trades and off-exchange volume, combined into one score from −1 to +1.',
  bands:[[-0.30,'Large players look like net sellers.'],
         [-0.05,'Mild net selling.'],
         [0.05,'No clear footprint either way.'],
         [0.30,'Mild net buying.'],
         [INF,'Clear accumulation. Someone large is building a position.']]},

volume_z: {label:'Volume vs normal', dp:2, unit:'σ', cls:'A', good:0, hz:TC.fast,
  what:'Today’s volume against this sector’s own 60-day norm.',
  bands:[[-1,'Unusually quiet for this sector.'],
         [1,'Normal volume for this sector.'],
         [2,'Busier than usual.'],
         [INF,'Highly unusual. Something is happening — check price and flow to '
             +'see which way.']]},

ret_21d: {label:'1-month return', dp:1, unit:'%', cls:'A', zero:true, good:1,
  what:'Price change over the last 21 sessions, about one month.',
  bands:[[-8,'Sharp decline over the month.'],[-2,'Drifting lower.'],
         [2,'Roughly flat over the month.'],[8,'Advancing steadily.'],
         [INF,'Sharp advance over the month.']]},

mom_12_1: {label:'12-month momentum', dp:1, unit:'%', cls:'A', zero:true, good:1,
  hz:TC.slow,
  what:'Return over 12 months, skipping the most recent one. The strongest single '
      +'predictor found in testing.',
  bands:[[-10,'Falling over the year.'],[0,'Slightly negative over the year.'],
         [15,'Positive but unremarkable.'],[INF,'Strong 12-month trend.']]},

stage: {label:'Weinstein stage', cls:'C',
  what:'The four-stage cycle a sector moves through, judged on price and volume '
      +'together.',
  states:{1:'Basing. Sideways price on quiet volume — supply is being absorbed. '
           +'Too early to buy, the right time to watch.',
          2:'Advancing. Rising price on expanding volume. The stage worth owning.',
          3:'Distribution. Momentum is stalling — either heavy volume going '
           +'nowhere, or an advance nobody is joining.',
          4:'Declining. Downtrend confirmed. Avoid.'}},

phase: {label:'Signal', cls:'C',
  what:'This dashboard’s one-word summary of stage, strength, breadth and flow.',
  warn:'Descriptive, not a forecast. These labels were never backtested — only '
      +'VMS and 12-month momentum were.',
  states:{
    'STEALTH_ACCUMULATION':'Quiet buying with no price move yet: volume being '
      +'absorbed while price stays flat. The earliest signal here, and the least '
      +'certain — most never go on to break out.',
    'CONFIRMED_BREAKOUT':'Price has broken out and breadth and money flow agree. '
      +'Later and more expensive than stealth accumulation, but far more of the '
      +'evidence is actually in hand.',
    'DISTRIBUTION':'Strength is being sold into. Price can still look healthy '
      +'while participation and flow rot underneath.',
    'CAPITAL_FLIGHT':'Money is leaving on both price and flow.',
    'NEUTRAL':'No clean configuration. Most sectors are here most of the time — '
      +'the honest answer rather than a manufactured signal.'}},

absorption: {label:'Absorption', dp:3, cls:'A', ser:'absorption', zero:true,
  good:1, hz:TC.fast,
  what:'Heavy volume that moved the price very little — the mark of a large buyer '
      +'soaking up supply without chasing it.',
  bands:[[0,'None — volume is moving the price normally.'],
         [0.15,'Slight. A little supply is being absorbed.'],
         [0.30,'Noticeable absorption on recent sessions.'],
         [INF,'Heavy. Someone large is absorbing supply — with a flat price, this '
             +'is the classic stealth-accumulation tell.']]},

ad_balance: {label:'Accumulation vs distribution', dp:2, cls:'A', zero:true,
  good:1, hz:TC.fast, ser:'ad_balance',
  what:'Days closing up on rising volume, minus days closing down on rising '
      +'volume, over the last 25 sessions.',
  bands:[[-0.3,'Selling days dominate.'],[-0.1,'Slightly more selling.'],
         [0.1,'Evenly matched.'],[0.3,'Slightly more buying.'],
         [INF,'Buying days dominate.']]},

obv_slope: {label:'On-Balance Volume slope', dp:2, cls:'A', zero:true, good:1,
  what:'Whether volume is arriving on up days or on down days.',
  bands:[[-0.2,'Volume is arriving on the down days.'],
         [0.2,'No clear bias.'],
         [INF,'Volume is arriving on the up days.']]},

// No `ser`: short interest publishes about twice a month, so a 60-point daily
// sparkline would be a step function pretending to be a trend.
days_to_cover: {label:'Days to cover', dp:2, cls:'B', good:0, hz:TC.slow, dfield:null,
  what:'How many normal trading days short sellers would need to buy back '
      +'everything they are short.',
  bands:[[1,'Almost no short interest — nothing to squeeze.'],
         [3,'Normal short interest for a sector ETF.'],
         [5,'Elevated, though not yet crowded.'],
         [INF,'Crowded short. Squeeze fuel — but also a signal that informed '
             +'sceptics are betting against this sector.']],
  warn:'Published on a lag: this is a position from up to two weeks ago, not today.'},

dtc_percentile: {label:'Days to cover vs its own history', dp:0, unit:'%ile', dunit:'pp',
  cls:'B', good:0,
  what:'Where today’s days-to-cover sits in this sector’s own range. More useful '
      +'than the raw number, because normal differs a lot by sector.',
  bands:[[20,'Unusually low for this sector.'],[80,'Its normal range.'],
         [INF,'Unusually crowded for this sector.']]},

squeeze_score: {label:'Squeeze setup', dp:0, unit:'/3', cls:'B', good:1,
  what:'How many of three conditions line up: shorts crowded, price diverging '
      +'from the index, relative strength turning up.',
  bands:[[1,'None of the three conditions are met.'],
         [2,'One of three. Not a setup.'],
         [3,'Two of three. Incomplete — the spark is missing.'],
         [INF,'All three met. The sector could move violently if it turns; a '
             +'reason to watch it, not a forecast.']]},

off_exchange_share: {label:'Off-exchange volume', dp:1, unit:'%', dunit:'pp', cls:'B',
  good:0, hz:TC.fast, dfield:'off_exchange_trend', pct:true,
  what:'Share of volume printed away from the public exchanges — dark pools, '
      +'where institutions work large orders without moving the price.',
  bands:[[30,'Below normal. Trading is mostly on the lit exchanges.'],
         [40,'Normal for a liquid sector ETF.'],
         [50,'Elevated. More size than usual is being worked out of sight.'],
         [INF,'Most volume is trading off-exchange.']]},

block_intensity_z: {label:'Block-trade intensity', dp:2, unit:'σ', cls:'A',
  zero:true, good:0, hz:TC.fast,
  what:'How unusual today’s large-block activity is for this sector. Blocks are '
      +'prints too big to come from retail.',
  bands:[[-1,'Fewer big prints than usual.'],
         [1,'Normal block activity for this sector.'],
         [2,'Elevated — more big prints than usual.'],
         [INF,'Very heavy block activity.']]},

block_direction: {label:'Block direction', dp:2, cls:'A', zero:true, good:1,
  what:'Whether the large prints hit nearer the ask (buying) or the bid '
      +'(selling). +1 is all buy-side, −1 all sell-side.',
  bands:[[-0.3,'Big prints are hitting the bid — sellers in a hurry.'],
         [-0.1,'Slight sell-side lean.'],
         [0.1,'Balanced between buy and sell side.'],
         [0.3,'Slight buy-side lean.'],
         [INF,'Big prints are lifting the offer — buyers in a hurry.']]},

volume_trend_pct: {label:'Volume trend', dp:1, unit:'%', cls:'A', ser:'volume_trend',
  zero:true, good:0,
  what:'Median volume over the last 20 sessions versus the 20 before it.',
  bands:[[-25,'Drying up sharply — interest is draining away.'],
         [-8,'Easing off from a month ago.'],
         [8,'Steady — no change in participation.'],
         [25,'Building — more participation than a month ago.'],
         [INF,'Expanding sharply — something has drawn attention here.']]},

percentile: {label:'Composite percentile', dp:0, unit:'%ile', dunit:'pp', cls:'B', good:1,
  what:'Where the composite sits in its own history, rather than against other '
      +'sectors.',
  bands:[[20,'Near the bottom of its own range.'],[40,'Below its own average.'],
         [60,'Around its own average.'],[80,'Above its own average.'],
         [INF,'Near the top of its own range.']]},

/* ---- observed tick-flow panel ---- */
dark_pool_share: {label:'Dark pool share', dp:1, unit:'%', dunit:'pp', cls:'B', good:0,
  hz:TC.fast, dfield:'dark_pool_trend', pct:true,
  what:'The subset of off-exchange volume that printed on an ATS — a registered '
      +'dark pool — rather than any other off-exchange venue.',
  bands:[[30,'Below normal for a liquid ETF.'],
         [40,'Normal for a liquid sector ETF.'],
         [50,'Elevated dark-pool routing.'],
         [INF,'Most volume is printing in dark pools.']]},

block_count: {label:'Block prints', dp:0, cls:'B', good:0,
  what:'How many single trades over the size threshold printed in the sampled '
      +'sessions. Retail order flow does not produce these.',
  bands:[[25,'Very few — too thin to read anything into.'],
         [150,'A modest number of large prints.'],
         [400,'Active block trading.'],
         [INF,'Heavy block activity.']]},

block_share: {label:'Block share of volume', dp:1, unit:'%', dunit:'pp', cls:'B', good:0,
  pct:true,
  what:'How much of the sector’s total volume arrived in those large prints, '
      +'rather than in ordinary-sized trades.',
  bands:[[15,'Little of the volume is institutional-sized.'],
         [25,'A normal share arrives in blocks.'],
         [35,'Elevated — a large slice of volume is big prints.'],
         [INF,'Most volume is arriving in large blocks.']]},

largest_print_notional: {label:'Largest single print', dp:1, cls:'B', good:0,
  what:'The dollar value of the biggest single trade in the sampled sessions.',
  bands:[[10e6,'Nothing unusually large.'],
         [50e6,'A sizeable print, but not remarkable.'],
         [150e6,'A large print — someone moved real size in one go.'],
         [INF,'A very large single print.']]},

flow_sessions: {label:'Sessions sampled', dp:0, cls:'B', good:0,
  what:'How many trading days of tick data these flow figures are built from.',
  bands:[[5,'Too few sessions to trust the shares.'],
         [15,'A short sample — treat the trend with caution.'],
         [INF,'Enough sessions for the shares to be stable.']]},

/* ---- crowded shorts & divergence panel ---- */
crowded_short: {label:'Crowded short', cls:'C',
  vlabel:{'true':'Crowded', 'false':'Not crowded'},
  what:'Whether short interest in this sector sits in the top of its own '
      +'historical range.',
  states:{true:'Crowded. An unusual number of shares are sold short relative to '
              +'this sector’s own history. That is squeeze fuel if the price '
              +'turns up — and a warning that informed sceptics are positioned '
              +'against it.',
          false:'Not crowded. Short interest is within its normal range, so '
               +'there is no unusual pressure either way.'}},

si_change_pct: {label:'Short interest change, 3 months', dp:1, unit:'%', cls:'A',
  zero:true, good:0,
  what:'How much short interest has grown or shrunk over the last three months.',
  bands:[[-15,'Shorts are covering fast — pressure coming off.'],
         [-3,'Shorts drifting lower.'],
         [3,'Little change.'],
         [15,'Shorts building.'],
         [INF,'Shorts building fast. Conviction against this sector is rising.']]},

sector_63d_pct: {label:'Sector return, 3 months', dp:1, unit:'%', cls:'A',
  zero:true, good:1,
  what:'What this sector returned over the last 63 sessions.',
  bands:[[-15,'Sharp decline over the quarter.'],[-3,'Down over the quarter.'],
         [3,'Roughly flat over the quarter.'],[15,'Up over the quarter.'],
         [INF,'Sharp advance over the quarter.']]},

bench_63d_pct: {label:'S&P 500 return, 3 months', dp:1, unit:'%', cls:'A',
  zero:true, good:1,
  what:'What the S&P 500 returned over the same 63 sessions, for comparison.',
  bands:[[-15,'The whole market fell sharply.'],[-3,'The market was down.'],
         [3,'The market was roughly flat.'],[15,'The market was up.'],
         [INF,'The market rose sharply.']]},

divergence: {label:'Divergence from the index', cls:'C',
  vlabel:{'true':'Diverging', 'false':'Moving with the market'},
  what:'Whether this sector moved in the opposite direction to the S&P 500 over '
      +'the last three months.',
  states:{true:'Yes — the sector and the index moved opposite ways. That is the '
              +'setup worth looking at: something sector-specific is happening '
              +'that the broad market is not explaining.',
          false:'No — the sector moved broadly with the market, so its return '
               +'says more about the market than about the sector.'}},

/* ---- unusual activity panel ---- */
ret_5d: {label:'5-day return', dp:1, unit:'%', cls:'A', zero:true, good:1,
  what:'Price change over the last five sessions.',
  bands:[[-4,'Sharp fall this week.'],[-1,'Down over the week.'],
         [1,'Roughly flat over the week.'],[4,'Up over the week.'],
         [INF,'Sharp rise this week.']]},

ticker: {label:'Sector', cls:'C',
  what:'The exchange-traded fund used as this sector’s proxy. Click the row for '
      +'the full breakdown, including a price chart with breakout markers.',
  states:{}},

/* ---- macro liquidity panel (FRED) ----
   One entry per series. These are the only inputs on the dashboard that are not
   about a single sector — they describe the tide that lifts or drops all of
   them, which is why the framework puts them first. */
macro_contribution: {label:'Contribution to the liquidity score', dp:2, cls:'A',
  zero:true, good:1,
  what:'How much this series pushes the overall liquidity reading, after '
      +'correcting its sign — a falling reverse-repo balance ADDS liquidity.',
  bands:[[-0.5,'Pulling the liquidity reading down hard.'],
         [-0.1,'Pulling the reading down slightly.'],
         [0.1,'Roughly neutral for liquidity.'],
         [0.5,'Adding to liquidity.'],
         [INF,'Adding strongly to liquidity.']]},

macro_WALCL: {label:'Fed balance sheet (WALCL)', dp:0, cls:'B', good:1,
  what:'Total assets held by the Federal Reserve. When it grows the Fed is '
      +'adding money to the system; when it shrinks it is withdrawing it.',
  bands:[[INF,'Read the 13-week change rather than the level — the direction is '
              +'what moves markets.']]},

macro_RRPONTSYD: {label:'Reverse repo balance', dp:0, cls:'B', good:-1,
  what:'Cash parked overnight at the Fed by money-market funds — money sitting '
      +'idle instead of being invested.',
  bands:[[INF,'Falling is bullish: cash is leaving the sidelines and going into '
              +'markets. Rising means money is being parked.']]},

macro_WTREGEN: {label:'Treasury General Account', dp:0, cls:'B', good:-1,
  what:'The US Treasury’s own checking account at the Fed.',
  bands:[[INF,'Falling is bullish: the Treasury is spending, which puts money '
              +'into the economy. Rising drains it back out.']]},

macro_NFCI: {label:'Financial conditions index', dp:2, cls:'A', zero:true, good:-1,
  what:'The Chicago Fed’s summary of how easy or hard it is to borrow. Below '
      +'zero means conditions are looser than average.',
  bands:[[-0.2,'Conditions are loose — credit is easy to come by.'],
         [0.2,'Conditions are about average.'],
         [INF,'Conditions are tight — borrowing is harder and riskier assets '
             +'usually struggle.']]},

macro_T10Y2Y: {label:'Yield curve (10y − 2y)', dp:2, cls:'A', zero:true, good:1,
  what:'The 10-year Treasury yield minus the 2-year. Negative means short-term '
      +'borrowing costs more than long-term, which is unusual.',
  bands:[[0,'Inverted. Historically a recession warning, though with long and '
            +'unreliable lead times.'],
         [1,'Flat — the curve is not saying much either way.'],
         [INF,'Normal upward slope.']]},

macro_DGS10: {label:'10-year Treasury yield', dp:2, unit:'%', cls:'B', good:0,
  what:'The interest rate on 10-year US government debt — the benchmark against '
      +'which most other assets are priced.',
  bands:[[3,'Low by recent standards.'],[4.5,'A middling level.'],
         [INF,'High. Rising yields pressure long-duration and growth sectors '
             +'hardest.']]},

dollar_volume_z: {label:'Dollar volume vs normal', dp:2, unit:'σ', cls:'A',
  zero:true, good:0,
  what:'Dollar volume against this sector’s own 60-day norm. Harder to fake than '
      +'share count, because it weights by price.',
  bands:[[-1,'Unusually quiet in dollar terms.'],
         [1,'Normal dollar volume for this sector.'],
         [2,'More money changing hands than usual.'],
         [INF,'Far more money changing hands than usual.']]},
};

/* ---------- trend computation ----------
   Median of the most recent `smooth` sessions minus the median of `smooth`
   sessions ending `hz` sessions ago. Medians, not endpoints, because a
   point-to-point delta on a daily series sign-flips on noise. Disjoint windows,
   because a baseline overlapping the measurement window absorbs its own signal
   — the bug previously found in volume_trend.                                */
function median(a){
  const b=a.slice().sort((x,y)=>x-y), m=b.length>>1;
  return b.length%2 ? b[m] : (b[m-1]+b[m])/2;
}
function seriesTrend(arr, hz){
  const v=(arr||[]).filter(Number.isFinite);
  const S=TC.smooth;
  if(v.length < Math.max(hz+S, TC.min_history)) return null;
  const recent = median(v.slice(-S));
  const prior  = median(v.slice(-hz-S, -hz));      // disjoint from `recent`
  if(!Number.isFinite(recent)||!Number.isFinite(prior)) return null;
  return {d: recent-prior, hz: hz, src:'series'};
}
function trendOf(key, s){
  const m=META[key]; if(!m||m.cls==='C') return null;
  const hz = m.hz || TC.fast;
  // A pre-computed delta from the Python side wins: it is measured on the full
  // series, not the 120-session display tail.
  if(m.dfield){
    const v=s[m.dfield];
    if(v!==null&&v!==undefined&&Number.isFinite(+v))
      return {d:+v * (m.pct?100:1), hz:hz, src:'field'};
  }
  if(m.ser) return seriesTrend(s.series&&s.series[m.ser], hz);
  return null;
}
/* The unit of a CHANGE is not always the unit of the value. Breadth of 85% that
   rose from 15% moved 70 percentage POINTS, and rendering that as "+70%" reads
   as a 70 percent relative gain — a different and much smaller move. Indicators
   whose value is itself a percentage declare `dunit:'pp'`. */
function arrowHtml(t, good, dp, unit){
  if(!t) return '';
  const d=t.d, flat=Math.abs(d) < 1e-9;
  const g = flat?'▬':(d>0?'▲':'▼');
  // Colour by whether the move is favourable, which is not always "up".
  let c='d';
  if(!flat && good) c = (d>0) === (good>0) ? 'g' : 'r';
  const txt=(d>0?'+':'')+d.toFixed(dp)+(unit||'');
  return `<span class="${c}">${g} ${txt}</span> <span class="tt-h-sub">(${t.hz}d)</span>`;
}

/* ---------- interpretation (Layer 2) ---------- */
function bandText(key, v){
  const m=META[key]; if(!m||!m.bands) return '';
  for(const [ub,txt] of m.bands) if(v < ub) return txt;
  return m.bands[m.bands.length-1][1];
}
function stateText(key, v){
  const m=META[key]; if(!m||!m.states) return '';
  return m.states[v] || m.states[String(v)] || '';
}

/* ---------- inline sparkline, fixed pixel size ---------- */
function mspark(vals, color, w=54, h=15, zero=false){
  const v=(vals||[]).filter(Number.isFinite);
  if(v.length<4) return '';
  let lo=Math.min(...v), hi=Math.max(...v);
  if(zero){ const m=Math.max(Math.abs(lo),Math.abs(hi))||1; lo=-m; hi=m; }
  if(hi===lo) hi=lo+1;
  const X=i=>(i/(v.length-1))*(w-2)+1, Y=x=>h-1-((x-lo)/(hi-lo))*(h-2);
  const pts=v.map((x,i)=>X(i).toFixed(1)+','+Y(x).toFixed(1)).join(' ');
  const z = (zero&&lo<0&&hi>0)
    ? `<line x1="0" y1="${Y(0).toFixed(1)}" x2="${w}" y2="${Y(0).toFixed(1)}" stroke="#31405f" stroke-dasharray="1 2"/>` : '';
  return `<svg class="cs" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">${z}`
    +`<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.3"/></svg>`;
}

/* ---------- per-indicator tooltip: definition + this value's meaning ---------- */
function cellTip(key, s){
  const m=META[key]; if(!m) return tipFor(s);
  const v=s[key];
  const miss = v===null||v===undefined||(typeof v==='number'&&Number.isNaN(v));

  let head=`<div class="tt-h">${m.label}</div>`
          +`<div class="tt-meta"><span>${s.ticker} · ${s.name}</span>`
          +`<span>as of ${s.as_of||''}</span></div>`;

  if(miss)
    return head+`<div class="tt-def">${m.what}</div>`
               +`<div class="tt-say">No value available for this sector.</div>`;

  let valTxt, say, extra='';
  if(m.cls==='C'){
    // Never show a raw `true`/`false` — a boolean field still needs a word.
    const disp = m.vlabel ? m.vlabel[String(v)]
                          : (s[key+'_label'] !== undefined ? s[key+'_label'] : v);
    valTxt=`<em>${disp}</em>`;
    say=stateText(key,v);
    // Show the evidence the classifier actually used, rather than asking the
    // reader to trust a one-word label.
    const why = key==='phase' ? s.phase_reasons
              : key==='stage' ? stageWhy(s.stage_evidence)
              : null;
    if(why&&why.length)
      extra=`<div class="tt-meta" style="display:block;line-height:1.5">`
           +why.slice(0,3).map(x=>'· '+x).join('<br>')+`</div>`;
    const dur=s[key+'_days'], prev=s[key+'_prev'], since=s[key+'_since'];
    if(dur!==null&&dur!==undefined){
      valTxt+=`<span>held ${dur} session${dur===1?'':'s'}</span>`;
      if(prev!==null&&prev!==undefined&&prev!=='')
        say+=` Previously ${m.states&&m.states[prev]?prev:prev}`
            +(since?`, changed ${since}.`:'.');
    }
  } else {
    const dp=m.dp===undefined?2:m.dp;
    valTxt=`<em>${(m.pct?+v*100:+v).toFixed(dp)}${m.unit||''}</em>`;
    const t=trendOf(key,s);
    if(t) valTxt+=`<span>${arrowHtml(t,m.good,dp,m.dunit!==undefined?m.dunit:m.unit)}</span>`;
    say=bandText(key, m.pct?+v*100:+v);
    // Class A: a sign change is the event, so say how long the sign has held.
    if(m.cls==='A'&&m.ser&&s.series&&s.series[m.ser]){
      const n=signRun(s.series[m.ser]);
      if(n>0) say+=` ${(+v>=0?'Positive':'Negative')} for ${n} session${n===1?'':'s'}.`;
    }
  }

  const spk = m.ser && s.series && s.series[m.ser]
    ? `<div class="tt-sp">${spark(s.series[m.ser], sparkColour(key,s), 300, 34, !!m.zero)}</div>`
    : '';

  // Order matters. The reader is already looking at the number; what they want
  // first is whether it is good. An earlier version led with three lines of
  // definition and closed with a two-word reading of the actual value.
  return head
    + `<div class="tt-val">${valTxt}</div>`
    + (say?`<div class="tt-say">${say}</div>`:'')
    + spk
    + extra
    + `<div class="tt-def"><span>What it is</span> ${m.what}</div>`
    + (m.warn?`<div class="tt-warn">${m.warn}</div>`:'')
    + (key==='inst_flow_score' && s.inst_flow_regime!=='observed'
        ? '<div class="tt-warn">Inferred from daily bars, not measured from trade '
          + 'data — this sector has no tick-level coverage.</div>' : '');
}
/* Turn the stage classifier's evidence dict into readable lines. The classifier
   uses price structure AND volume together, and both halves are worth showing —
   an unparticipated advance and a real one look identical on price alone. */
function stageWhy(ev){
  if(!ev) return null;
  const out=[];
  // The classifier already writes its own reason; lead with it.
  if(ev.reason) out.push(ev.reason);
  if(ev.sma50_slope_21d_pct!==undefined&&ev.sma50_slope_21d_pct!==null)
    out.push(`50-day average sloping ${(+ev.sma50_slope_21d_pct).toFixed(1)}% over 21 sessions`);
  if(ev.volume_regime)
    out.push(`volume ${ev.volume_regime}`
      +(ev.volume_trend_pct!==undefined&&ev.volume_trend_pct!==null
        ? ` (${(+ev.volume_trend_pct).toFixed(0)}%)` : ''));
  return out.length?out:null;
}

/* how many trailing sessions the series has held its current sign */
function signRun(arr){
  const v=(arr||[]).filter(Number.isFinite);
  if(!v.length) return 0;
  const pos=v[v.length-1]>=0; let n=0;
  for(let i=v.length-1;i>=0;i--){ if((v[i]>=0)===pos) n++; else break; }
  return n;
}
function sparkColour(key,s){
  const m=META[key]; if(!m) return '#8b9ab5';
  const v=+s[key];
  if(m.zero) return v>=0?'#22d38a':'#ff5d6c';
  return '#4d9fff';
}

/* ---------- row tooltip: compact cross-indicator summary ---------- */
function tipFor(s){
  const row = (k,v)=>`<div class="tt-r"><span>${k}</span><b>${v}</b></div>`;
  const f = (v,d=2)=> (v===null||v===undefined||Number.isNaN(v)) ? '–' : (+v).toFixed(d);
  const dl = (key,d)=>{ const t=trendOf(key,s); return t?' '+arrowHtml(t,(META[key]||{}).good,d):''; };
  return `<div class="tt-h">${s.ticker} · ${s.name}</div>`
    + '<div class="tt-def">Hover any single cell for what that indicator means and '
      + 'how to read this value.</div>'
    + row('VMS (validated)', f(s.vms) + (s.vms_rank ? ` (#${s.vms_rank})` : ''))
    + row('12-1 momentum', f(s.mom_12_1,1)+'%')
    + row('Signal (descriptive)', s.phase_label)
    + row('Stage', s.stage_label||s.stage||'–')
    + row('Quadrant', s.quadrant)
    + row('CSRI (not validated)', f(s.csri)+dl('csri',2))
    + row('Mansfield RS', f(s.mansfield_rs)+dl('mansfield_rs',2))
    + row('RS-Ratio / Mom', f(s.rs_ratio,1)+' / '+f(s.rs_momentum,1))
    + row('Breadth >50d SMA', s.breadth===null?'–':f(s.breadth,0)+'%'+dl('breadth',0))
    + row('Money flow (CMF)', f(s.cmf,3)+dl('cmf',3))
    + row('21d return', f(s.ret_21d,1)+'%')
    + (s.days_to_cover!==null&&s.days_to_cover!==undefined
        ? row('Days to cover', f(s.days_to_cover,2)
            + (s.crowded_short?' <b class="y">crowded</b>':''))
        : '')
    + (s.squeeze_score ? row('Squeeze', s.squeeze_score+'/3') : '');
}

/* ---------------- RRG ---------------- */
let rrgTier = 1;
function drawRRG(){
  const W=660,H=520,P=52;
  const data = D.sectors.filter(s=>s.tier===rrgTier && s.rs_ratio!==null && s.rs_momentum!==null
                                   && Number.isFinite(s.rs_ratio) && Number.isFinite(s.rs_momentum));
  const host = document.getElementById('rrg');
  if(!data.length){ host.innerHTML='<div class="sub">Not enough history to plot.</div>'; return; }

  const xs=[], ys=[];
  data.forEach(s=>{ xs.push(s.rs_ratio); ys.push(s.rs_momentum);
    (s.series.rrg_x||[]).forEach(v=>Number.isFinite(v)&&xs.push(v));
    (s.series.rrg_y||[]).forEach(v=>Number.isFinite(v)&&ys.push(v)); });
  const pad=v=>Math.max(v,0.9);
  const xr=pad(Math.max(...xs.map(v=>Math.abs(v-100)))*1.22);
  const yr=pad(Math.max(...ys.map(v=>Math.abs(v-100)))*1.22);
  const X=v=>P+((v-100+xr)/(2*xr))*(W-2*P);
  const Y=v=>H-P-((v-100+yr)/(2*yr))*(H-2*P);

  let g='';
  // quadrant fills: leading (green, top-right), improving (blue, top-left),
  // weakening (orange, bottom-right), lagging (red, bottom-left)
  g+=`<rect x="${X(100)}" y="${P}" width="${W-P-X(100)}" height="${Y(100)-P}" fill="rgba(34,211,138,.055)"/>`;
  g+=`<rect x="${P}" y="${P}" width="${X(100)-P}" height="${Y(100)-P}" fill="rgba(77,159,255,.055)"/>`;
  g+=`<rect x="${X(100)}" y="${Y(100)}" width="${W-P-X(100)}" height="${H-P-Y(100)}" fill="rgba(255,148,69,.055)"/>`;
  g+=`<rect x="${P}" y="${Y(100)}" width="${X(100)-P}" height="${H-P-Y(100)}" fill="rgba(255,93,108,.055)"/>`;
  // frame + axes
  g+=`<rect x="${P}" y="${P}" width="${W-2*P}" height="${H-2*P}" fill="none" stroke="#243049"/>`;
  g+=`<line x1="${X(100)}" y1="${P}" x2="${X(100)}" y2="${H-P}" stroke="#31405f" stroke-dasharray="3 3"/>`;
  g+=`<line x1="${P}" y1="${Y(100)}" x2="${W-P}" y2="${Y(100)}" stroke="#31405f" stroke-dasharray="3 3"/>`;
  // quadrant labels
  g+=`<text class="q-label" x="${W-P-8}" y="${P+16}" text-anchor="end">Leading</text>`;
  g+=`<text class="q-label" x="${P+8}" y="${P+16}">Improving</text>`;
  g+=`<text class="q-label" x="${W-P-8}" y="${H-P-8}" text-anchor="end">Weakening</text>`;
  g+=`<text class="q-label" x="${P+8}" y="${H-P-8}">Lagging</text>`;
  // axis titles
  g+=`<text class="q-label" x="${W/2}" y="${H-14}" text-anchor="middle">RS-Ratio  →  relative strength</text>`;
  g+=`<text class="q-label" transform="translate(16,${H/2}) rotate(-90)" text-anchor="middle">RS-Momentum  →  velocity</text>`;

  // tails then nodes
  data.forEach(s=>{
    const tx=s.series.rrg_x||[], ty=s.series.rrg_y||[];
    const c=COL[s.phase_level]||COL.grey;
    const n=Math.min(tx.length,ty.length);
    if(n>1){
      let pts=[];
      for(let i=0;i<n;i++) if(Number.isFinite(tx[i])&&Number.isFinite(ty[i])) pts.push(X(tx[i])+','+Y(ty[i]));
      if(pts.length>1) g+=`<polyline class="tail" points="${pts.join(' ')}" stroke="${c}"/>`;
      for(let i=0;i<n-1;i++) if(Number.isFinite(tx[i])&&Number.isFinite(ty[i]))
        g+=`<circle cx="${X(tx[i])}" cy="${Y(ty[i])}" r="1.9" fill="${c}" opacity="${0.18+0.5*i/n}"/>`;
    }
  });
  data.forEach((s,i)=>{
    const c=COL[s.phase_level]||COL.grey;
    const r=s.tier===1?17:14;
    g+=`<g class="node" data-i="${i}" data-tk="${s.ticker}">`
      +`<circle cx="${X(s.rs_ratio)}" cy="${Y(s.rs_momentum)}" r="${r}" fill="${c}" stroke="#0a0e17" stroke-width="1.5"/>`
      +`<text x="${X(s.rs_ratio)}" y="${Y(s.rs_momentum)+3.6}" text-anchor="middle">${s.ticker}</text></g>`;
  });

  host.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="${W}">${g}</svg>`;
  host.querySelectorAll('.node').forEach(nd=>{
    const s=data[+nd.dataset.i];
    nd.addEventListener('mousemove',e=>showTip(tipFor(s),e));
    nd.addEventListener('mouseleave',hideTip);
    nd.addEventListener('click',()=>openDetail(s.ticker));
  });
}

/* ---------------- generic table sorter ----------------
   Attaches to EVERY table on the page, including ones rendered later into the
   detail panel. One implementation rather than per-table handlers, so any table
   added in future is sortable automatically with no extra code.

   Sorting is tri-state: descending -> ascending -> back to the original document
   order. That third state matters here because several tables ship in a
   meaningful order (the alert feed is ranked by signal quality, the breakout
   table is chronological) and a sort should be undoable.                      */

/* Parse a cell into something comparable. The tables mix raw numbers with
   "+0.46%", "$150M", "1.14x", "+1.5σ", "2,512", "●●○", ISO dates and plain
   text, so a naive parseFloat would mis-sort most columns. */
function cellValue(td){
  // A ragged row (fewer cells than headers) must not take the sorter down. This
  // happened for real when a duplicate column was dropped from the body but
  // left in the header.
  if(!td) return null;
  const raw = (td.textContent || '').trim();
  if(!raw || raw === '–' || raw === '-' || raw === 'n/a') return null;   // always last

  // Light glyphs: rank by how many are filled.
  if(/^[●○]+$/.test(raw)) return (raw.match(/●/g) || []).length;

  // ISO date -> sortable number.
  const d = raw.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if(d) return Number(d[1] + d[2] + d[3]);

  // Numeric with optional sign, separators, magnitude suffix and unit.
  // Handles the unicode minus (−) that appears in generated text.
  const m = raw.replace(/[−–]/g, '-')
               .match(/^[^\d\-+.]*([-+]?[\d,]*\.?\d+)\s*([KMB])?/i);
  if(m){
    let v = parseFloat(m[1].replace(/,/g, ''));
    if(!Number.isFinite(v)) return raw.toLowerCase();
    const suf = (m[2] || '').toUpperCase();
    if(suf === 'K') v *= 1e3;
    else if(suf === 'M') v *= 1e6;
    else if(suf === 'B') v *= 1e9;
    return v;
  }
  return raw.toLowerCase();
}

function makeSortable(table){
  if(!table || table.dataset.sortable === '1') return;
  const thead = table.tHead, tb = table.tBodies && table.tBodies[0];
  if(!thead || !tb || tb.rows.length < 2) return;
  table.dataset.sortable = '1';

  const rows = Array.from(tb.rows);
  rows.forEach((r,i)=>{ r.dataset.origIdx = i; });

  Array.from(thead.rows[0].cells).forEach((th, col)=>{
    th.style.cursor = 'pointer';
    th.title = 'click to sort';
    if(!/[▾▴]/.test(th.textContent)) th.insertAdjacentHTML('beforeend','<span class="sarr"></span>');
    th.addEventListener('click', ()=>{
      // cycle: none -> desc -> asc -> none
      const cur = th.dataset.dir || '';
      const dir = cur === '' ? 'desc' : cur === 'desc' ? 'asc' : '';
      Array.from(thead.rows[0].cells).forEach(o=>{
        o.dataset.dir='';
        const a=o.querySelector('.sarr'); if(a) a.textContent='';
      });
      th.dataset.dir = dir;
      const arr = th.querySelector('.sarr');
      if(arr) arr.textContent = dir === 'desc' ? ' ▾' : dir === 'asc' ? ' ▴' : '';

      const sorted = rows.slice();
      if(dir === ''){
        sorted.sort((a,b)=> Number(a.dataset.origIdx) - Number(b.dataset.origIdx));
      } else {
        const sign = dir === 'desc' ? -1 : 1;
        sorted.sort((a,b)=>{
          const av = cellValue(a.cells[col]), bv = cellValue(b.cells[col]);
          // Nulls sink to the bottom in BOTH directions — an empty cell is
          // absence of data, not a small value, and letting it float to the top
          // on an ascending sort would bury the rows you actually want.
          if(av === null && bv === null) return 0;
          if(av === null) return 1;
          if(bv === null) return -1;
          if(typeof av === 'number' && typeof bv === 'number') return (av - bv) * sign;
          return String(av).localeCompare(String(bv)) * sign;
        });
      }
      const frag = document.createDocumentFragment();
      sorted.forEach(r=>frag.appendChild(r));
      tb.appendChild(frag);
    });
  });
}

/* Decide alignment per COLUMN from its actual contents, and apply it to the
   header and the body cells in the same pass. Doing it from one decision is the
   point: the previous bug was a right-aligned `th` rule with no matching `td`
   rule, so labels and digits drifted apart. They cannot now.

   Numbers right (so decimal points line up down the column), text left. */
function alignColumns(table){
  if(!table) return;
  const thead = table.tHead, tb = table.tBodies && table.tBodies[0];
  if(!thead || !tb || !thead.rows.length) return;
  const heads = Array.from(thead.rows[0].cells);
  const rows = Array.from(tb.rows);

  heads.forEach((th, col)=>{
    // An explicit .l in the markup is an author decision — respect it.
    if(th.classList.contains('l')){ th.style.textAlign='left';
      rows.forEach(r=>{ if(r.cells[col]) r.cells[col].style.textAlign='left'; });
      return; }
    let num=0, seen=0;
    for(const r of rows){
      const td=r.cells[col]; if(!td) continue;
      const v=cellValue(td);
      if(v===null) continue;              // blanks don't vote
      seen++; if(typeof v==='number') num++;
    }
    // Majority rules; an all-blank column falls back to left.
    const align = (seen>0 && num/seen >= 0.6) ? 'right' : 'left';
    th.style.textAlign = align;
    rows.forEach(r=>{ if(r.cells[col]) r.cells[col].style.textAlign = align; });
  });
}

/* Tooltips for ANY table, not just the ranking ones.
   Every server-rendered panel — observed flow, crowded shorts, unusual activity,
   macro liquidity — previously had no explanation of any kind. Binding here
   means a panel gets tooltips as soon as its cells carry data-k, with no
   per-panel code. Sector context comes from the row's data-tk where present. */
function bindTips(table){
  if(table.dataset.tips === '1') return;
  table.dataset.tips = '1';
  const look = tk => tk ? D.sectors.find(x=>x.ticker===tk) : null;

  table.addEventListener('mousemove', e=>{
    const cell = e.target.closest && e.target.closest('td[data-k], th[data-k]');
    if(!cell){ hideTip(); return; }
    const m = META[cell.dataset.k];
    if(!m){ hideTip(); return; }
    if(cell.tagName === 'TH'){
      showTip(`<div class="tt-h">${m.label}</div>`
        + (m.warn?`<div class="tt-warn">${m.warn}</div>`:'')
        + `<div class="tt-def"><span>What it is</span> ${m.what}</div>`, e, true);
      return;
    }
    const row = cell.closest('tr');
    const s = look(row && row.dataset.tk);
    // Without a sector we can still explain the column, just not this value.
    showTip(s ? cellTip(cell.dataset.k, s)
              : `<div class="tt-h">${m.label}</div>`
                + `<div class="tt-def"><span>What it is</span> ${m.what}</div>`, e, true);
  });
  table.addEventListener('mouseleave', hideTip);
}

/* Apply to every table. The two ranking tables re-render from the underlying
   data on sort (full precision, not DOM text) so they keep their own sorter —
   attaching the generic one too would give them two competing handlers. */
function enhanceTables(root){
  (root || document).querySelectorAll('table').forEach(t=>{
    const own = t.closest('#tbl1, #tbl2');
    if(!own){ makeSortable(t); bindTips(t); }
    alignColumns(t);
  });
}

/* ---------------- price chart with SMA breakout markers ---------------- */
/* Collapse breakout clusters.
   A real chart carried up to 30 markers over 120 sessions — one every four days,
   which reads as confetti rather than information. Measured across all 32
   sectors, each SMA is re-crossed every ~7 sessions, and the 20-day alone
   produces half of all markers.

   The clustering window scales with the SMA period, because that is what makes
   it meaningful: re-crossing a 20-day average within 20 sessions is the same
   episode wobbling, not two events, and the same logic at 150 days needs 150
   sessions of separation. A fixed 8-session gap removed only 16% of markers;
   scaling to the period removes 46% and caps the busiest chart at 15. */
function thinBreakouts(list){
  const out=[], last={};
  for(const b of (list||[]).slice().sort((a,c)=>a.idx-c.idx)){
    const k=b.sma+':'+b.direction;
    if(last[k]!==undefined && b.idx-last[k] < b.sma) continue;
    last[k]=b.idx; out.push(b);
  }
  return out;
}

function priceChart(s, w=760, h=330){
  const px=(s.series.price||[]).filter(Number.isFinite);
  if(px.length<10) return '<div class="sub">not enough price history</div>';
  const smas=[[20,s.series.sma20,'#4d9fff'],
              [50,s.series.sma50,'#f5c445'],
              [150,s.series.sma150,'#c88bff']];
  const smaCol={20:'#4d9fff',50:'#f5c445',150:'#c88bff'};
  const L=52, R=64, T=14, B=30;          // margins: room for labels and price tag
  const n=px.length;

  let vals=px.slice();
  smas.forEach(([,arr])=>{ if(arr) arr.filter(Number.isFinite).forEach(v=>vals.push(v)); });
  let lo=Math.min(...vals), hi=Math.max(...vals);
  const pad=(hi-lo)*0.10||1; lo-=pad; hi+=pad;
  const X=i=>L+(i/(n-1))*(w-L-R);
  const Y=v=>h-B-((v-lo)/(hi-lo))*(h-T-B);

  const up = px[n-1] >= px[0];
  const accent = up ? '#22d38a' : '#ff5d6c';
  const uid = 's'+(s.ticker||'x').replace(/[^A-Za-z0-9]/g,'');

  let g='';
  // Gradient area under the price, tinted by the direction of the window.
  g+=`<defs><linearGradient id="g${uid}" x1="0" y1="0" x2="0" y2="1">`
    +`<stop offset="0%" stop-color="${accent}" stop-opacity=".22"/>`
    +`<stop offset="100%" stop-color="${accent}" stop-opacity="0"/></linearGradient></defs>`;

  // Grid: fewer lines, lighter, labels outside the plot.
  for(let k=0;k<=4;k++){
    const v=lo+(hi-lo)*k/4, y=Y(v);
    g+=`<line x1="${L}" y1="${y.toFixed(1)}" x2="${w-R}" y2="${y.toFixed(1)}" `
      +`stroke="#1c2740"/>`
      +`<text x="${L-9}" y="${(y+3.5).toFixed(1)}" text-anchor="end" class="ax">`
      +`${v.toFixed(v>=100?0:1)}</text>`;
  }

  const linePts=px.map((v,i)=>X(i).toFixed(1)+','+Y(v).toFixed(1)).join(' ');
  g+=`<polygon points="${X(0).toFixed(1)},${(h-B).toFixed(1)} ${linePts} `
    +`${X(n-1).toFixed(1)},${(h-B).toFixed(1)}" fill="url(#g${uid})"/>`;

  // SMAs beneath the price line — right-aligned so the last point is today.
  smas.forEach(([p,arr,col])=>{
    if(!arr||arr.length<2) return;
    const off=n-arr.length;
    const pts=arr.map((v,i)=>Number.isFinite(v)?`${X(i+off).toFixed(1)},${Y(v).toFixed(1)}`:null)
                 .filter(Boolean);
    if(pts.length>1)
      g+=`<polyline points="${pts.join(' ')}" fill="none" stroke="${col}" `
        +`stroke-width="1.3" opacity=".75" stroke-linejoin="round"/>`;
  });

  g+=`<polyline points="${linePts}" fill="none" stroke="#e6edf7" stroke-width="2" `
    +`stroke-linejoin="round" stroke-linecap="round"/>`;

  // Current price: dot plus a tag on the right edge, so the number you care
  // about most does not have to be read off the axis.
  const lastY=Y(px[n-1]);
  g+=`<line x1="${L}" y1="${lastY.toFixed(1)}" x2="${w-R}" y2="${lastY.toFixed(1)}" `
    +`stroke="${accent}" stroke-dasharray="2 4" opacity=".5"/>`
    +`<circle cx="${X(n-1).toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5" fill="${accent}"/>`
    +`<rect x="${(w-R+4).toFixed(1)}" y="${(lastY-9).toFixed(1)}" width="${R-8}" height="18" `
    +`rx="4" fill="${accent}" opacity=".18" stroke="${accent}" stroke-opacity=".5"/>`
    +`<text x="${(w-R/2).toFixed(1)}" y="${(lastY+4).toFixed(1)}" text-anchor="middle" `
    +`class="ax" fill="${accent}" style="font-weight:650">${px[n-1].toFixed(2)}</text>`;

  // Breakouts, de-cluttered. Confirmed = filled, failed = hollow.
  const shown=thinBreakouts(s.breakouts);
  shown.forEach(b=>{
    if(b.idx<0||b.idx>=n) return;
    const x=X(b.idx), y=Y(b.price), isUp=b.direction==='up';
    const col=smaCol[b.sma]||'#8b9ab5';
    const r=4.2;
    const d=isUp?`${x},${(y-r-3).toFixed(1)} ${(x-r).toFixed(1)},${(y-0.5).toFixed(1)} ${(x+r).toFixed(1)},${(y-0.5).toFixed(1)}`
                :`${x},${(y+r+3).toFixed(1)} ${(x-r).toFixed(1)},${(y+0.5).toFixed(1)} ${(x+r).toFixed(1)},${(y+0.5).toFixed(1)}`;
    const solid=b.confirmed!==false;
    g+=`<g class="bkt" data-b='${JSON.stringify(b).replace(/'/g,"&#39;")}'>`
      +`<polygon points="${d}" fill="${solid?col:'#0a0e17'}" stroke="${col}" `
      +`stroke-width="1.3"/>`
      +`<circle cx="${x}" cy="${y}" r="9" fill="transparent"/></g>`;
  });

  // x-axis. `dates` is aligned 1:1 with `price` (see _tail_dates), so index
  // positionally rather than assuming a length.
  const dts=s.series.dates||[];
  if(dts.length===n && n>1){
    const short=d=>d? d.slice(5).replace('-','/') : '';
    [[0,'start'],[Math.floor((n-1)/3),'middle'],[Math.floor(2*(n-1)/3),'middle'],
     [n-1,'end']].forEach(([i,anch])=>{
      if(dts[i]) g+=`<text x="${X(i).toFixed(1)}" y="${h-9}" text-anchor="${anch}" `
                  +`class="ax">${short(dts[i])}</text>`;
    });
  }

  const hid=(s.breakouts||[]).length - shown.length;
  const key=smas.filter(([,a])=>a&&a.length>1)
    .map(([p,,c])=>`<span class="ck"><i style="background:${c}"></i>${p}-day</span>`).join('');
  return `<div class="chartwrap">`
    + `<div class="clegend"><span class="ck"><i style="background:#e6edf7"></i>Price</span>${key}`
    + `<span class="ck sep"><i class="tri"></i>breakout held</span>`
    + `<span class="ck"><i class="tri hollow"></i>failed next session</span>`
    + (hid>0?`<span class="ck dim">${hid} clustered repeat${hid===1?'':'s'} hidden</span>`:'')
    + `</div>`
    + `<svg viewBox="0 0 ${w} ${h}" width="100%" preserveAspectRatio="xMidYMid meet">${g}</svg>`
    + `</div>`;
}

/* ---------------- sparkline ---------------- */
function spark(vals,color,w=250,h=42,zero=false){
  const v=(vals||[]).filter(Number.isFinite);
  if(v.length<3) return '<div class="sub">no data</div>';
  let lo=Math.min(...v),hi=Math.max(...v);
  if(zero){ const m=Math.max(Math.abs(lo),Math.abs(hi))||1; lo=-m; hi=m; }
  if(hi===lo){hi=lo+1;}
  const X=i=>(i/(v.length-1))*w, Y=x=>h-((x-lo)/(hi-lo))*h;
  const pts=v.map((x,i)=>X(i).toFixed(1)+','+Y(x).toFixed(1)).join(' ');
  let z='';
  if(zero&&lo<0&&hi>0) z=`<line x1="0" y1="${Y(0).toFixed(1)}" x2="${w}" y2="${Y(0).toFixed(1)}" stroke="#31405f" stroke-dasharray="2 3"/>`;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="100%" preserveAspectRatio="none">${z}`
    +`<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6"/>`
    +`<circle cx="${X(v.length-1).toFixed(1)}" cy="${Y(v[v.length-1]).toFixed(1)}" r="2.4" fill="${color}"/></svg>`;
}

/* ---------------- tables ---------------- */
const fmt=(v,d=2,suf='')=> (v===null||v===undefined||Number.isNaN(v))?'<span class="d">–</span>':(+v).toFixed(d)+suf;
function sgn(v,d=2,suf=''){
  if(v===null||v===undefined||Number.isNaN(v)) return '<span class="d">–</span>';
  const c=v>0?'g':(v<0?'r':'d');
  return `<span class="${c}">${v>0?'+':''}${(+v).toFixed(d)}${suf}</span>`;
}
function lightsCell(s){
  const n=s.green_lights;
  if(n===null||n===undefined) return '<span class="d">–</span>';
  const c=n===3?'g':n===2?'y':n===1?'o':'d';
  return `<span class="${c}" title="Green Light Test">${'●'.repeat(n)}${'○'.repeat(3-n)}</span>`;
}
function csriBar(v){
  if(v===null||v===undefined||Number.isNaN(v)) return '<span class="d">–</span>';
  const w=Math.min(Math.abs(v)/2.2,1)*50, pos=v>=0;
  const c=pos?'var(--green)':'var(--red)';
  return `<div class="bar"><div class="mid"></div>`
    +`<i style="${pos?'left:50%':'right:50%;left:auto'};width:${w}%;background:${c}"></i>`
    +`<span>${v>0?'+':''}${v.toFixed(2)}</span></div>`;
}

/* Columns that carry an inline trend sparkline. Driven off META.ser, so a column
   listed here starts showing its sparkline automatically once that series is
   exported — no further change needed in this file. */
const SPARK_COLS = new Set(['vms','mansfield_rs','breadth','cmf','inst_flow_score']);
function sparkFor(key, s){
  if(!SPARK_COLS.has(key)) return '';
  const m=META[key]; if(!m||!m.ser) return '';
  const arr=s.series&&s.series[m.ser];
  if(!arr||arr.length<4) return '';
  return mspark(arr, sparkColour(key,s), 44, 14, !!m.zero);
}

let sortKey='vms', sortDir=-1;
function renderTable(tier, elId){
  const rows=D.sectors.filter(s=>s.tier===tier);
  rows.sort((a,b)=>{
    let x=a[sortKey], y=b[sortKey];
    if(typeof x==='string'||typeof y==='string') return String(x??'').localeCompare(String(y??''))*sortDir;
    x=(x===null||x===undefined||Number.isNaN(x))?-1e9:x; y=(y===null||y===undefined||Number.isNaN(y))?-1e9:y;
    return (x-y)*sortDir;
  });
  const cols=[['ticker','Sector','l'],['vms','VMS ✓'],['green_lights','Lights'],['csri','CSRI ✗'],['csri_delta_21d','Δ21d'],
              ['mansfield_rs','Mansfield RS'],['rs_ratio','RS-Ratio'],['rs_momentum','RS-Mom'],
              ['quadrant','Quadrant','l'],['breadth','Breadth'],['cmf','CMF'],
              ['inst_flow_score','Inst. Flow'],['volume_z','Vol σ'],['ret_21d','21d %'],
              ['stage','Stage'],['phase','Signal','l']];
  let h='<table><thead><tr>'+cols.map(c=>
      `<th class="${c[2]||''}" data-k="${c[0]}">${c[1]}${sortKey===c[0]?(sortDir<0?' ▾':' ▴'):''}</th>`).join('')+'</tr></thead><tbody>';
  rows.forEach(s=>{
    // td() tags every cell with its indicator key, which is what lets a hover
    // resolve to the right explanation instead of one tooltip for the whole row.
    const td=(key,inner,cls)=>`<td data-k="${key}"${cls?` class="${cls}"`:''}>`
                             +`${sparkFor(key,s)}${inner}</td>`;
    // Bar cells need the sparkline and the bar side by side, since the bar is a
    // block element and would otherwise push the sparkline onto its own line.
    const tdBar=(key,val)=>`<td data-k="${key}"><div class="cw">`
                          +`${sparkFor(key,s)}${csriBar(val)}</div></td>`;
    h+=`<tr data-tk="${s.ticker}">`
      +`<td class="l"><span class="tk">${s.ticker}</span> <span class="nm">${s.name}</span></td>`
      +tdBar('vms', s.vms)
      +td('green_lights', lightsCell(s))
      +tdBar('csri', s.csri)
      +td('csri_delta_21d', sgn(s.csri_delta_21d))
      +td('mansfield_rs', sgn(s.mansfield_rs))
      +td('rs_ratio', fmt(s.rs_ratio,1), 'mono')
      +td('rs_momentum', fmt(s.rs_momentum,1), 'mono')
      +td('quadrant', `<span class="nm">${s.quadrant}</span>`, 'l')
      +td('breadth', fmt(s.breadth,0,'%'), 'mono')
      +td('cmf', sgn(s.cmf,3))
      +td('inst_flow_score', sgn(s.inst_flow_score,2))
      +td('volume_z', sgn(s.volume_z,1,'σ'))
      +td('ret_21d', sgn(s.ret_21d,1,'%'))
      +td('stage', s.stage||'–', 'mono')
      +td('phase', `<span class="pill ${s.phase_level}">${s.phase_label}</span>`, 'l')
      +`</tr>`;
  });
  h+='</tbody></table>';
  const el=document.getElementById(elId); el.innerHTML=h;
  alignColumns(el.querySelector('table'));   // innerHTML wiped the inline styles
  el.querySelectorAll('th').forEach(th=>th.onclick=()=>{
    const k=th.dataset.k;
    if(sortKey===k) sortDir*=-1; else {sortKey=k; sortDir=-1;}
    renderTable(1,'tbl1'); renderTable(2,'tbl2');
  });
  el.querySelectorAll('tbody tr').forEach(tr=>tr.onclick=()=>openDetail(tr.dataset.tk));

  // Hover resolves to the specific cell, so each indicator explains itself.
  // Falling back to the row summary keeps the first (ticker) column useful.
  el.querySelectorAll('tbody tr').forEach(tr=>{
    const s=D.sectors.find(x=>x.ticker===tr.dataset.tk);
    if(!s) return;
    tr.addEventListener('mousemove',e=>{
      const cell=e.target.closest ? e.target.closest('td') : null;
      const k=cell&&cell.dataset.k;
      if(k&&META[k]) showTip(cellTip(k,s), e, true);
      else showTip(tipFor(s), e, false);
    });
    tr.addEventListener('mouseleave',hideTip);
  });

  // Header hover gives the definition without a specific value attached.
  el.querySelectorAll('thead th').forEach(th=>{
    const k=th.dataset.k, m=k&&META[k];
    if(!m) return;
    th.addEventListener('mousemove',e=>showTip(
      `<div class="tt-h">${m.label}</div><div class="tt-def">${m.what}</div>`
      +(m.warn?`<div class="tt-warn">${m.warn}</div>`:''), e, true));
    th.addEventListener('mouseleave',hideTip);
  });
}

/* ---------------- detail ---------------- */
function openDetail(tk){
  const s=D.sectors.find(x=>x.ticker===tk); if(!s) return;
  const c=COL[s.phase_level]||COL.grey;
  const card=(k,v,n,sp)=>`<div class="mcard"><div class="mk">${k}</div><div class="mv">${v}</div>`
    +`<div class="mn">${n}</div>${sp||''}</div>`;
  const comp=s.components||{};
  const wu=s.csri_weights_used||{};
  let compRows='';
  [['mansfield_rs','Mansfield RS'],['rs_momentum','RS-Momentum'],['breadth','Breadth'],
   ['money_flow','Money flow'],['inst_flow','Institutional flow']].forEach(([k,lbl])=>{
    const z=comp[k], w=wu[k];
    if(z===null||z===undefined) return;
    compRows+=`<tr><td class="l">${lbl}</td><td>${sgn(z,2,'σ')}</td>`
      +`<td class="mono">${w?(w*100).toFixed(0)+'%':'–'}</td>`
      +`<td>${sgn(z*(w||0),3)}</td></tr>`;
  });

  let ipRows='';
  Object.entries(s.inst_flow_parts||{}).forEach(([k,v])=>{
    const nm={absorption:'Absorption (heavy vol, flat price)',ad_days:'Accumulation − distribution days',
      block_intensity:'Block-print concentration',off_exchange:'Off-exchange volume share'}[k]||k;
    ipRows+=`<tr><td class="l">${nm}</td><td>${sgn(v,3)}</td></tr>`;
  });

  document.getElementById('detail').innerHTML=`
    <div class="panel">
      <span class="close-x" onclick="closeDetail()">×</span>
      <h3>${s.ticker} · ${s.name} <span class="pill ${s.phase_level}" style="margin-left:8px">${s.phase_label}</span></h3>
      <div class="sub" style="margin-top:5px">${s.stage_label} · ${s.quadrant} quadrant · $${s.price} · as of ${s.as_of}
        · ${s.n_constituents} constituents sampled for breadth</div>
      <ul style="margin:12px 0 0 18px;color:var(--dim);font-size:12.5px">
        ${(s.phase_reasons||[]).map(r=>`<li>${r}</li>`).join('')}
      </ul>
      <h2>Price &amp; SMA breakouts <span class="sub" style="font-weight:400">
        · ${s.breakout_count||0} crossings in the last 120 sessions,
        ${s.breakouts_recent_21d||0} in the last 21</span></h2>
      <div class="panel" style="background:var(--panel2)">
        ${priceChart(s)}
        <div class="legend">
          <span><i style="background:#e6edf7"></i>Price</span>
          <span><i style="background:#4d9fff"></i>20d SMA</span>
          <span><i style="background:#f5c445"></i>50d SMA</span>
          <span><i style="background:#c88bff"></i>150d SMA</span>
          <span>▲ upward cross · ▼ downward cross · hollow = next session failed to hold</span>
        </div>
        ${(s.breakouts&&s.breakouts.length)?`
        <table style="margin-top:12px"><thead><tr>
          <th class="l">Date</th><th>SMA</th><th class="l">Direction</th><th>Close</th>
          <th>SMA level</th><th>Gap</th><th>SMA slope</th><th>Volume</th><th class="l">Held?</th>
        </tr></thead><tbody>
        ${s.breakouts.slice().reverse().slice(0,12).map(b=>`<tr>
          <td class="l mono">${b.date}</td>
          <td class="mono">${b.sma}d</td>
          <td class="l"><span class="${b.direction==='up'?'g':'r'}">${b.direction==='up'?'▲ up':'▼ down'}</span></td>
          <td class="mono">${b.price}</td>
          <td class="mono">${b.sma_value}</td>
          <td>${sgn(b.gap_pct,2,'%')}</td>
          <td>${sgn(b.sma_slope_21d,2,'%')}</td>
          <td class="mono">${b.volume_ratio!==null?b.volume_ratio+'x':'–'}
            ${b.volume_confirmed?'<span class="g">✓</span>':''}</td>
          <td class="l">${b.confirmed===true?'<span class="g">held</span>'
                        :b.confirmed===false?'<span class="r">failed</span>'
                        :'<span class="d">pending</span>'}</td>
        </tr>`).join('')}
        </tbody></table>`:''}
        <div class="note">A crossing counts only when the close moves through the average;
          crossings within 3 sessions of the previous one on the same SMA are suppressed as
          whipsaw. <b>Held?</b> reports whether the <i>next</i> session stayed on the new side —
          a failed cross is a false breakout, which is exactly what the source framework's
          confirmation rule exists to filter. Volume ✓ marks a cross on 1.2x or more of the
          50-day average. Note the SMA slope: a cross up through a <i>falling</i> average is a
          weaker event than one through a rising average.</div>
      </div>

      <div class="dgrid">
        ${card('VMS (validated)', (s.vms===null?'–':(s.vms>0?'+':'')+s.vms.toFixed(2)),
               (s.vms_rank?`rank #${s.vms_rank} of ${s.vms_tier_size} in tier · `:'')
               +'12-1 momentum + RS-Momentum, cross-sectional z-scores', '')}
        ${card('CSRI (not validated)', (s.csri===null?'–':(s.csri>0?'+':'')+s.csri.toFixed(2)),
               'Original 5-component composite. Holdout IC 0.010, Sharpe 0.016 — kept for diagnosis only', '')}
        ${card('Mansfield RS', (s.mansfield_rs===null?'–':(s.mansfield_rs>0?'+':'')+s.mansfield_rs.toFixed(2)),
               'Above zero = outperforming its own long-run trend vs SPY',
               spark(s.series.mansfield, c, 250, 42, true))}
        ${card('Breadth', (s.breadth===null?'–':s.breadth.toFixed(0)+'%'),
               'Constituents above their 50-day SMA', spark(s.series.breadth, '#4d9fff'))}
        ${card('Chaikin Money Flow', (s.cmf===null?'–':(s.cmf>0?'+':'')+s.cmf.toFixed(3)),
               'Volume-weighted accumulation over 21 sessions',
               spark(s.series.cmf, '#f5c445', 250, 42, true))}
        ${card('Absorption', (s.absorption===null?'–':(s.absorption>0?'+':'')+s.absorption.toFixed(2)),
               'Heavy volume with the close held high and little net price move',
               spark(s.series.absorption, '#22d38a', 250, 42, true))}
        ${card('Price (120d)', '$'+s.price, 'Sector ETF close', spark(s.series.price, '#8b9ab5'))}
      </div>
      <div class="two" style="margin-top:16px">
        <div>
          <h2 style="margin-top:0">Green Light Test
            <span class="pill ${s.all_green?'green':s.green_lights>=2?'yellow':'grey'}"
                  style="margin-left:6px">${s.green_lights}/3</span></h2>
          <div class="note" style="margin:0 0 8px">The framework's three-part entry gate.
            All three must pass — one light is a different claim from three.</div>
          <table><tbody>
          ${Object.entries(s.green_light_detail||{}).map(([k,v])=>
            `<tr><td class="l">${v.pass?'<span class="g">● pass</span>':'<span class="d">○ fail</span>'}</td>
             <td class="l"><b>${({relative_strength:'Relative strength',
               price_trend:'Climbing, not falling',volume_confirms:'Volume confirms'})[k]||k}</b>
             <div class="nm">${v.detail}</div></td></tr>`).join('')
            || '<tr><td class="d">no data</td></tr>'}
          </tbody></table>

          <h2>Technical setups</h2>
          ${(s.setups&&s.setups.length)
            ? s.setups.map(x=>`<div class="mcard" style="margin-bottom:8px">
                <div class="mk">${x.date} · ${x.direction}</div>
                <div style="font-weight:620;font-size:13px;margin:2px 0">${x.setup}</div>
                <div class="mn">${x.detail}</div>
                ${x.confirmation_note?`<div class="mn" style="margin-top:4px">
                  <b class="${x.confirmed?'g':x.confirmed===false?'r':'y'}">
                  ${x.confirmation_note}</b></div>`:''}
                ${x.stop_hint?`<div class="mn">suggested stop reference: ${x.stop_hint}</div>`:''}
              </div>`).join('')
            : '<div class="sub">No setup detected in the last 5 sessions.</div>'}
          <div class="note">MA Bounce / Breakout / Breakdown and Reversal Confirmation, detected
            against the 50-day SMA. The confirmation line reports whether the framework's
            day-2 follow-through actually happened — that rule exists to filter false breakouts,
            so an unconfirmed breakout is a reason to wait, not to act.</div>

          <h2>Block classification</h2>
          ${(s.block_bucket_detail&&s.block_bucket_detail.length)
            ? s.block_bucket_detail.map(b=>`<div class="mcard" style="margin-bottom:8px">
                <div style="font-weight:620;font-size:13px">${b.bucket}</div>
                <div class="mn">${b.detail}</div></div>`).join('')
              + `<div class="note">${s.block_summary||''}</div>`
            : `<div class="sub">No block buckets — ${D.meta.off_exchange
                ? 'activity is unremarkable for this sector.'
                : 'requires tick data (run backfill_flow.py).'}</div>`}
        </div>
        <div>
          <h2 style="margin-top:0">Composite breakdown</h2>
          <table><thead><tr><th class="l">Component</th><th>Z-score</th><th>Weight</th><th>Contribution</th></tr></thead>
          <tbody>${compRows||'<tr><td colspan="4" class="d">no components</td></tr>'}</tbody></table>
        </div>
        <div>
          <h2 style="margin-top:0">Institutional footprint detail
            <span class="pill ${s.inst_flow_regime==='observed'?'green':'grey'}"
                  style="margin-left:6px">${s.inst_flow_regime==='observed'?'measured':'inferred'}</span></h2>
          <table><thead><tr><th class="l">Signal</th><th>Score</th></tr></thead>
          <tbody>${ipRows||'<tr><td colspan="2" class="d">no flow data</td></tr>'}</tbody></table>
          <div class="note">Range −1 to +1. ${s.inst_flow_regime==='observed'
            ? 'Observed from Polygon trade data (75% weight) blended with the daily-bar proxies (25%), which are kept as a sanity anchor in case tick coverage truncated.'
            : 'Inferred from daily bars only. This sector is not in OFF_EXCHANGE_TICKERS, or tick data was unavailable for it.'}</div>
        </div>
      </div>
    </div>`;
  const d=document.getElementById('detail'); d.classList.add('open');
  enhanceTables(d);        // detail tables are built fresh on every open
  d.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function closeDetail(){ document.getElementById('detail').classList.remove('open'); }

/* ---------------- boot ---------------- */
document.querySelectorAll('.rrg-ctl .tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.rrg-ctl .tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active'); rrgTier=+t.dataset.tier; drawRRG();
});
drawRRG(); renderTable(1,'tbl1'); renderTable(2,'tbl2');
enhanceTables();                      // every server-rendered table on the page
window.addEventListener('resize',()=>drawRRG());

/* ---------------- refresh controls ----------------
   Two actions, because they cost different things:
     Rebuild from cache  recomputes everything from data already on disk. Free,
                         and the right button after a code change.
     Fetch latest data   pulls new prices from Polygon. Spends API quota, so it
                         is rate limited server-side and marked differently.
   The controls stay hidden until /status confirms the server supports them, so
   opening dashboard.html straight from disk shows no button that cannot work. */
(function(){
  const box=document.getElementById('rfz');
  const bOff=document.getElementById('rfz-off');
  const bGet=document.getElementById('rfz-get');
  const msg=document.getElementById('rfz-msg');
  if(!box) return;
  let polling=false, builtAt=null;

  const say=(t,cls)=>{ msg.textContent=t; msg.className='rfz-msg'+(cls?' '+cls:''); };
  const mmss=s=>Math.floor(s/60)+'m '+String(s%60).padStart(2,'0')+'s';
  const ago=s=>s<90?s+'s ago':s<5400?Math.round(s/60)+' min ago'
                :s<172800?Math.round(s/3600)+'h ago':Math.round(s/86400)+'d ago';

  async function status(){
    const r=await fetch('/status',{cache:'no-store'});
    if(!r.ok) throw new Error('status '+r.status);
    return r.json();
  }

  // Button state and the message are set separately on purpose. Repainting both
  // after a rejection overwrote the reason with the routine status line, so the
  // error flashed for a moment and vanished.
  function setEnabled(s){
    const busy=s.running||s.queued;
    bOff.disabled=busy;
    bGet.disabled=busy||s.fetch_cooldown_sec>0;
    bGet.title=s.fetch_cooldown_sec>0
      ? 'Rate limited for '+mmss(s.fetch_cooldown_sec)+' to protect API quota'
      : 'Pulls fresh prices from Polygon, then recomputes. Takes a few minutes.';
    return busy;
  }

  function paint(s){
    const busy=setEnabled(s);
    if(busy){ say(s.running?'Rebuilding on the Pi...':'Queued...','work'); return; }
    // A completed run changes the file mtime; that is how we detect finishing.
    if(builtAt!==null && s.built_at && s.built_at>builtAt){
      say('Done. Reloading...','ok');
      setTimeout(()=>location.reload(),700);
      return;
    }
    let t='Data built '+(s.built_age_sec!=null?ago(s.built_age_sec):'unknown');
    if(s.fetch_cooldown_sec>0) t+=' · fetch available in '+mmss(s.fetch_cooldown_sec);
    say(t);
  }

  async function poll(){
    if(polling) return;
    polling=true;
    try{
      while(true){
        const s=await status();
        paint(s);
        if(!(s.running||s.queued)) break;
        await new Promise(r=>setTimeout(r,3000));
      }
    }catch(e){ say('Lost contact with the server.','err'); }
    finally{ polling=false; }
  }

  async function go(mode){
    if(mode==='fetch' && !confirm(
        'Fetch fresh prices from Polygon?\n\n'
      + 'This re-downloads every series and takes a few minutes. If the data is '
      + 'already current, "Rebuild from cache" gets the same result in about a '
      + 'minute.')) return;
    bOff.disabled=bGet.disabled=true;
    say('Requesting...','work');
    try{
      const r=await fetch('/refresh?mode='+mode,{method:'POST'});
      const j=await r.json().catch(()=>({}));
      if(r.status===202){ builtAt=j.built_at; poll(); return; }
      if(r.status===429){
        say('Rate limited to protect API quota. Try again in '
            +mmss(j.fetch_cooldown_sec||0)+'.','err');
      } else if(r.status===409){
        say('A refresh is already running.','work');
        // Only follow it if the server actually says something is in flight;
        // otherwise polling would immediately overwrite this message.
        if(j.running||j.queued){ poll(); return; }
      } else {
        say(j.error||('Failed: HTTP '+r.status),'err');
      }
      // Re-enable buttons WITHOUT touching the message the user needs to read.
      const s1=await status().catch(()=>null); if(s1) setEnabled(s1);
      return;
    }catch(e){ say('Could not reach the server.','err'); }
    const s=await status().catch(()=>null); if(s) setEnabled(s);
  }

  bOff.addEventListener('click',()=>go('offline'));
  bGet.addEventListener('click',()=>go('fetch'));

  status().then(s=>{
    if(!s.refresh_enabled) return;      // stay hidden
    box.hidden=false; builtAt=s.built_at; paint(s);
    if(s.running||s.queued) poll();
  }).catch(()=>{ /* opened from disk, not served: leave hidden */ });
})();
"""


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------
def _macro_html(reg: dict) -> str:
    """FRED central-bank liquidity — Step 1 of the source framework."""
    m = reg.get("macro") or {}
    if not m or m.get("regime") == "UNKNOWN":
        return ('<div class="panel" style="margin-top:14px"><h3>Macro liquidity</h3>'
                '<div class="sub">FRED series unavailable — the price-based regime '
                'above is being used instead.</div></div>')
    imp = m.get("liquidity_impulse")
    col = {"EXPANDING": "g", "NEUTRAL": "y", "DRAINING": "r"}.get(m["regime"], "d")

    rows = ""
    for sid, d in (m.get("series") or {}).items():
        sz = d.get("signed_z")
        contrib = ("<span class='d'>context only</span>" if sz is None
                   else f"<span class='{'g' if sz > 0 else 'r' if sz < 0 else 'd'}'>"
                        f"{sz:+.2f}</span>")
        pct = d.get("change_13w_pct")
        k = f"macro_{sid}"
        rows += (f"<tr><td class='l' data-k='{k}'><span class='tk'>{sid}</span> "
                 f"<span class='nm'>{d.get('name','')}</span></td>"
                 f"<td class='mono' data-k='{k}'>{d.get('latest'):,.2f}</td>"
                 f"<td data-k='{k}'>{'' if pct is None else f'{pct:+.1f}%'}</td>"
                 f"<td data-k='macro_contribution'>{contrib}</td>"
                 f"<td class='nm l'>{d.get('note','')}</td>"
                 f"<td class='nm'>{d.get('as_of','')}</td></tr>")

    return f"""
<div class="panel" style="margin-top:14px">
  <h3>Macro liquidity <span class="{col}" style="margin-left:8px">{m['regime']}</span>
    <span class="sub" style="font-weight:400"> · impulse {imp:+.2f}</span></h3>
  <div class="sub" style="margin:5px 0 12px">{m.get('note','')}
    {('· ' + m['curve_note']) if m.get('curve_note') else ''}
    {(f"· 10y {m['ten_year_yield']}%") if m.get('ten_year_yield') else ''}</div>
  <table><thead><tr><th class="l">Series</th><th>Latest</th><th>13w chg</th>
    <th>Contribution</th><th class="l">Interpretation</th><th>As of</th></tr></thead>
  <tbody>{rows}</tbody></table>
  <div class="note">This is <b>Step 1</b> of the framework: central-bank liquidity sits above
    sector selection. The impulse is a weighted mean of z-scored 13-week changes, sign-corrected
    so positive always means liquidity being <i>added</i> — reverse repo, the Treasury General
    Account and the financial-conditions index are inverted, since a falling balance or a
    loosening NFCI releases liquidity rather than absorbing it.
    Source: {m.get('source','FRED')}.</div>
</div>"""


def _regime_html(reg: dict, sectors: list[dict]) -> str:
    colour = {"RISK-ON": "g", "PULLBACK": "y", "CAUTION": "o", "RISK-OFF": "r"}.get(reg["regime"], "d")
    counts: dict[str, int] = {}
    for s in sectors:
        counts[s["phase"]] = counts.get(s["phase"], 0) + 1

    accum = counts.get("STEALTH_ACCUMULATION", 0)
    brk = counts.get("CONFIRMED_BREAKOUT", 0)
    dist = counts.get("DISTRIBUTION", 0) + counts.get("CAPITAL_FLIGHT", 0)

    tilt = reg.get("risk_on_tilt")
    tilt_s = "–" if tilt is None else f"{tilt:+.2f}"
    tilt_c = "g" if (tilt or 0) > 0.15 else ("r" if (tilt or 0) < -0.15 else "d")

    return f"""
<div class="regime">
  <div class="kpi"><div class="k">Macro weather</div>
    <div class="v {colour}">{reg['regime']}</div>
    <div class="n">{reg['note']}</div></div>
  <div class="kpi"><div class="k">{reg['benchmark']} structure</div>
    <div class="v">${reg['price']:,.2f}</div>
    <div class="n">{'Above' if reg['above_sma50'] else 'Below'} 50d ·
      {'above' if reg['above_sma200'] else 'below'} 200d · 200d slope {reg['sma200_slope_42d']:+.2f}%</div></div>
  <div class="kpi"><div class="k">Cyclical vs defensive tilt</div>
    <div class="v {tilt_c}">{tilt_s}</div>
    <div class="n">{reg['tilt_note']}</div></div>
  <div class="kpi"><div class="k">Accumulation signals</div>
    <div class="v y">{accum}</div>
    <div class="n">Sectors in stealth accumulation — inflow before relative price confirms</div></div>
  <div class="kpi"><div class="k">Confirmed breakouts</div>
    <div class="v g">{brk}</div>
    <div class="n">Mansfield RS above zero with breadth and money flow confirming</div></div>
  <div class="kpi"><div class="k">Distribution / flight</div>
    <div class="v r">{dist}</div>
    <div class="n">Sectors institutions appear to be funding rotations out of</div></div>
</div>"""


def _alerts_html(sectors: list[dict]) -> str:
    from .scoring import PHASE_META
    ranked = sorted(
        [s for s in sectors if s["phase"] != "NEUTRAL" or s.get("flags")],
        key=lambda s: (PHASE_META[s["phase"]]["rank"], -(s.get("csri") or -9)),
    )
    if not ranked:
        return '<div class="sub">No sectors currently meet an alert configuration.</div>'

    out = []
    for s in ranked[:26]:
        lvl = s["phase_level"]
        bullets = "".join(f"<li>{r}</li>" for r in s.get("phase_reasons", [])[:4])
        flags = "".join(
            f'<li><span class="{ {"green":"g","yellow":"y","orange":"o","red":"r"}.get(f["level"],"d") }">'
            f'▸ {f["text"]}</span></li>' for f in s.get("flags", [])[:4]
        )
        csri = "–" if s.get("csri") is None else f"{s['csri']:+.2f}"
        out.append(f"""
<div class="alert {lvl}">
  <div class="a-h">
    <span class="a-t">{s['ticker']} · {s['name']}</span>
    <span class="a-p {'g' if lvl=='green' else 'y' if lvl=='yellow' else 'o' if lvl=='orange' else 'r' if lvl=='red' else 'd'}">{s['phase_label']}</span>
  </div>
  <div class="sub">CSRI {csri} · Δ21d {('–' if s.get('csri_delta_21d') is None else f"{s['csri_delta_21d']:+.2f}")}
    · {s['quadrant']} · tier {s['tier']}</div>
  <ul>{bullets}{flags}</ul>
</div>""")
    return "".join(out)


def _rotation_side(entries: list[dict], gaining: bool) -> str:
    """One column of the gaining/losing panel."""
    if not entries:
        return ('<div class="fl-empty">No sector is '
                + ('above' if gaining else 'below')
                + ' its peer average on the validated score.</div>')
    cls = "g" if gaining else "r"
    out = []
    for e in entries:
        m = e.get("mom_12_1")
        q = e.get("quadrant", "–")
        qd = e.get("quadrant_days")
        # Sectors inside the noise band are dimmed rather than hidden: seeing
        # where the line falls is more informative than a truncated list.
        faint = "" if e.get("material") else " faint"
        out.append(
            f'<div class="fl-row{faint}">'
            f'<div class="fl-score {cls}">{e["score"]:+.2f}</div>'
            f'<div class="fl-body">'
            f'<div class="fl-name"><b>{e["ticker"]}</b> {e["name"]}'
            + ('' if e.get("material")
               else '<span class="fl-tag">within noise band</span>')
            + '</div>'
            f'<div class="fl-sub">{q}'
            + (f' for {qd} sessions' if qd else '')
            + (f' · 12-month {m:+.1f}%' if m is not None else '')
            + '</div></div></div>')
    return "".join(out)


def _validation_html() -> str:
    """
    How much to trust each score, stated as a hit rate rather than a rank IC.

    The previous version led with "holdout rank IC 0.036, costed Sharpe 0.530,
    sign-stable across four subperiods". Every one of those is accurate and none
    of them answers the reader's actual question, which is whether to believe
    the number in the table. A rank IC also gives no sense of scale: nothing
    tells you whether 0.036 is impressive or negligible unless you already work
    with them.

    Restated as pairwise concordance — shown two sectors, how often does this
    score pick the one that goes on to do better — the same finding becomes
    legible, and the smallness of the edge becomes visible rather than hidden
    behind a decimal.
    """
    lo, hi = 48.0, 54.0          # scale for the hit-rate bar
    def _pos(v: float) -> float:
        return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))

    rows = []
    for key in ("vms", "csri", "phase"):
        v = config.VALIDATION[key]
        hr = v["hit_rate"]
        lvl = v["level"]
        if hr is None:
            meter = ('<div class="vm-none">not tested as a forecast</div>')
            num = '<div class="vm-num grey">—</div>'
        else:
            num = f'<div class="vm-num {lvl}">{hr:.0f}<span>in 100</span></div>'
            meter = f"""<div class="vm-bar">
      <div class="vm-fill {lvl}" style="left:{_pos(config.COIN_FLIP):.1f}%;
           width:{max(_pos(hr) - _pos(config.COIN_FLIP), 0.8):.1f}%"></div>
      <div class="vm-coin" style="left:{_pos(config.COIN_FLIP):.1f}%"></div>
    </div>"""
        rows.append(f"""
  <div class="vm-row">
    <div class="vm-head"><b class="{lvl}">{v['label']}</b>
      <span class="vm-verdict {lvl}">{v['verdict']}</span>
      <div class="vm-full">{v['full']}</div></div>
    <div class="vm-mid">{num}{meter}</div>
    <div class="vm-say">{v['plain']}</div>
  </div>""")

    return f"""
<h2>How much to trust these numbers</h2>
<div class="panel" style="border-color:rgba(245,196,69,.45)">
  <div class="sub" style="line-height:1.6;margin-bottom:16px">
    Each score was tested against what actually happened next.
    <b>The test:</b> shown two sectors, how often does the score pick the one that
    goes on to do better over the following month? A coin flip gets 50 in 100.<br>
    <span style="color:var(--dim2)">{config.VALIDATION_BASIS}</span>
  </div>
  <div class="vmeter">{"".join(rows)}</div>
  <div class="note" style="margin-top:16px">
    <b>The honest caveat.</b> {config.VALIDATION_CAVEAT}
    Full detail in <code>output/BACKTEST_RESULTS.md</code>.
  </div>
</div>"""


def _rotation_html(rot: dict) -> str:
    """
    Gaining / losing ground, as two ranked lists.

    Deliberately NOT drawn as from->to arrows. See scoring.rotation_flow: the
    arrows asserted a relationship between two specific sectors that no data
    supports, and produced rows pointing into below-average sectors.

    Named `_rotation_html`, not `_flow_html` — that name already belongs to the
    observed tick-flow panel, which is a different thing entirely (measured
    order flow, not inferred rotation).
    """
    if not rot or (not rot.get("gaining") and not rot.get("losing")):
        return '<div class="sub">Not enough scored sectors to rank.</div>'
    note = rot.get("note")
    return f"""
<div class="flow">
  <div class="flow-col">
    <div class="flow-head g">Gaining ground</div>
    {_rotation_side(rot.get("gaining") or [], True)}
  </div>
  <div class="flow-col">
    <div class="flow-head r">Losing ground</div>
    {_rotation_side(rot.get("losing") or [], False)}
  </div>
</div>
{f'<div class="note">{note}</div>' if note else ''}"""


def _flow_html(sectors: list[dict], meta: dict) -> str:
    """Observed tick-level flow table. Only rendered when Polygon data exists."""
    rows_data = [s for s in sectors if s.get("flow")]
    if not rows_data:
        return ""
    rows_data.sort(key=lambda s: -(s.get("dark_pool_trend") or -9))

    def sg(v, d=1, suf="", scale=1.0):
        if v is None or v != v:
            return '<span class="d">–</span>'
        v = v * scale
        c = "g" if v > 0 else ("r" if v < 0 else "d")
        return f'<span class="{c}">{v:+.{d}f}{suf}</span>'

    def pc(v, d=1):
        return '<span class="d">–</span>' if v is None or v != v else f"{v*100:.{d}f}%"

    # Polygon reports the ATS (dark pool) subset as exactly the off-exchange
    # total for these ETFs, so those two columns printed the same number twice —
    # four of eight columns conveying one fact. Detected rather than assumed:
    # flow.py sets `dark_equals_off` per session.
    collapsed = all(
        s.get("dark_pool_share") == s.get("off_exchange_share")
        for s in rows_data if s.get("off_exchange_share") is not None)

    body = ""
    for s in rows_data:
        f = s["flow"]
        dark = "" if collapsed else (
            f'<td class="mono" data-k="dark_pool_share">{pc(s.get("dark_pool_share"))}</td>'
            f'<td data-k="dark_pool_share">{sg(s.get("dark_pool_trend"), 1, "pp", 100)}</td>')
        body += (
            f'<tr data-tk="{s["ticker"]}"><td class="l"><span class="tk">{s["ticker"]}</span> '
            f'<span class="nm">{s["name"]}</span></td>'
            f'<td class="mono" data-k="off_exchange_share">{pc(s.get("off_exchange_share"))}</td>'
            f'<td data-k="off_exchange_share">{sg(s.get("off_exchange_trend"), 1, "pp", 100)}</td>'
            + dark +
            f'<td class="mono" data-k="block_count">{s.get("block_count") or 0}</td>'
            f'<td class="mono" data-k="block_share">{pc(s.get("block_share"))}</td>'
            f'<td data-k="block_intensity_z">{sg(s.get("block_intensity_z"), 2, "σ")}</td>'
            f'<td data-k="block_direction">{sg(s.get("block_direction"), 2)}</td>'
            f'<td class="mono">${(s.get("largest_print_notional") or 0)/1e6:.1f}M</td>'
            f'<td class="mono">{f["sessions"]}</td></tr>'
        )
    trunc = sum(s["flow"].get("truncated_sessions", 0) for s in rows_data)
    warn = ("" if not trunc else
            f' <span class="o">{trunc} session(s) hit the pagination cap, so shares are '
            f'understated — raise FLOW_MAX_PAGES in config for full coverage.</span>')
    return f"""
<h2>Observed institutional flow <span class="badge on" style="margin-left:8px">measured</span></h2>
<div class="panel">
  <table><thead><tr><th class="l" data-k="ticker">Sector</th>
    <th data-k="off_exchange_share">Off-exchange %</th>
    <th data-k="off_exchange_share">1-month change</th>
    {'' if collapsed else
     '<th data-k="dark_pool_share">Dark pool %</th>'
     '<th data-k="dark_pool_share">1-month change</th>'}
    <th data-k="block_count">Block prints</th>
    <th data-k="block_share">Block share</th>
    <th data-k="block_intensity_z">Vs normal</th>
    <th data-k="block_direction">Buy or sell</th>
    <th data-k="largest_print_notional">Largest print</th>
    <th data-k="flow_sessions">Sessions</th></tr></thead>
  <tbody>{body}</tbody></table>
  <div class="note">Read directly from Polygon trade data, not inferred from daily bars.
    <b>Off-exch %</b> is the share of volume reported to a FINRA TRF; <b>dark pool %</b> is the
    ATS subset (prints carrying a <span class="mono">trf_id</span>) and is the cleanest read on
    stealth positioning. <b>Blocks</b> are single prints of
    {meta.get("block_min_shares", 10000):,}+ shares or
    ${meta.get("block_min_notional", 200000) / 1000:,.0f}k+ notional.
    <b>Block dir</b> applies a tick test to each block: positive means blocks are arriving on
    upticks, i.e. buyers crossing the spread. A rising dark pool share with flat price and a
    positive block direction is the textbook accumulation signature.{warn}</div>
</div>"""


def _squeeze_html(sectors: list[dict]) -> str:
    """Crowded shorts, index-vs-sector divergence, and squeeze setups."""
    have = [s for s in sectors if s.get("days_to_cover") is not None]
    if not have:
        return ""
    have.sort(key=lambda s: (-(s.get("squeeze_score") or 0),
                             -(s.get("dtc_percentile") or 0)))

    rows = ""
    for s in have:
        pct = s.get("dtc_percentile")
        crowd = ("<span class='y'>crowded</span>" if s.get("crowded_short")
                 else "<span class='d'>–</span>")
        chg = s.get("si_change_pct")
        chg_s = ("<span class='d'>–</span>" if chg is None else
                 f"<span class='{'r' if chg > 0 else 'g'}'>{chg:+.1f}%</span>")
        div = ("<span class='y'>YES</span>" if s.get("divergence")
               else "<span class='d'>–</span>")
        sc = s.get("squeeze_score") or 0
        sq = (f"<span class='{'g' if sc == 3 else 'y' if sc == 2 else 'd'}'>"
              f"{'●' * sc}{'○' * (3 - sc)}</span>")
        rows += (
            f"<tr data-tk='{s['ticker']}'><td class='l'><span class='tk'>{s['ticker']}</span> "
            f"<span class='nm'>{s['name']}</span></td>"
            f"<td class='mono' data-k='days_to_cover'>{s['days_to_cover']:.2f}</td>"
            f"<td class='mono' data-k='dtc_percentile'>"
            f"{'' if pct is None else f'{pct:.0f}'}</td>"
            f"<td data-k='crowded_short'>{crowd}</td>"
            f"<td data-k='si_change_pct'>{chg_s}</td>"
            f"<td class='mono' data-k='sector_63d_pct'>"
            f"{(s.get('sector_63d_pct') or 0):+.1f}%</td>"
            f"<td class='mono' data-k='bench_63d_pct'>"
            f"{(s.get('bench_63d_pct') or 0):+.1f}%</td>"
            f"<td data-k='divergence'>{div}</td>"
            f"<td data-k='squeeze_score'>{sq}</td>"
            f"<td class='nm l'>{s.get('squeeze_note','')[:64]}</td></tr>")

    n_sq = sum(1 for s in have if s.get("squeeze_setup"))
    n_div = sum(1 for s in have if s.get("divergence"))
    as_of = next((s.get("si_as_of") for s in have if s.get("si_as_of")), "")

    return f"""
<h2>Crowded shorts &amp; divergence
  <span class="badge on" style="margin-left:8px">free on your Polygon key</span></h2>
<div class="panel">
  <div class="sub" style="margin-bottom:12px;line-height:1.6">
    <b>What a squeeze is.</b> Short sellers borrow shares and sell them, hoping to buy
    back cheaper. If the price rises instead, they are forced to buy back to cut their
    losses — and that buying pushes the price up further. The more crowded the shorts,
    the more violent that unwind can be.<br>
    <b>So a squeeze setup is bullish</b> — a sector that could move up sharply. But
    crowded shorts also mean well-informed people are betting against the sector, and
    they are often right. It is a reason to look, not a reason to buy.<br><br>
    <b>{n_sq}</b> full setup(s) · <b>{n_div}</b> divergence(s) ·
    short interest as of {as_of} · hover any cell for what it means</div>
  <table><thead><tr><th class="l">Sector</th><th>Days to cover</th><th>%ile</th>
    <th>Crowded</th><th>SI Δ 3mo</th><th>Sector 63d</th><th>Bench 63d</th>
    <th>Divergence</th><th>Squeeze</th><th class="l">Note</th></tr></thead>
  <tbody>{rows}</tbody></table>
  <div class="note">
    <b>Days to cover</b> = short shares / average daily volume: how many normal
    sessions shorts would need to exit. Judged as a <b>percentile against each
    sector's own history</b>, since a reading of 3 is unremarkable for a thin
    thematic ETF and extreme for XLK. Crowded = top quartile of own history.<br><br>
    <b>Divergence</b> requires the sector and benchmark to be moving in <i>opposite</i>
    directions over 63 sessions with a gap of 8pp or more — not merely
    underperformance, which Mansfield RS already covers. This is the source
    document's worked example: the NASDAQ 100 rising while software fell, a gap it
    attributes to crowded short positioning.<br><br>
    <b>Squeeze</b> needs all three: crowded shorts (the fuel), divergence (sentiment
    already extreme), and relative strength <i>turning up</i> (the spark). The third
    is what separates a squeeze setup from a value trap — crowded shorts in a still
    deteriorating sector usually means the shorts are right.<br><br>
    <span style="color:var(--dim2)"><b>Tested, and underpowered.</b> The 3/3 setup fired
    43 times in nine years across 32 ETFs (1.3% of observations, 39 usable episodes) —
    below the minimum needed to evaluate. Its 21-session mean of +1.0% shrinks to
    +0.23% under 10% trimming (76.8% shrinkage), so it rests on three tail
    observations, and Mann-Whitney p=0.995 says the typical squeeze is
    indistinguishable from the typical non-squeeze. The 63-session mean is −2.3%.<br>
    Use this as a <b>watchlist trigger</b> — "this sector could move violently, go
    look" — not as a return forecast. Detail in output/SQUEEZE_TEST.md.</span></div>
</div>"""


def _unusual_html(sectors: list[dict]) -> str:
    rows = []
    cand = [s for s in sectors
            if any(v is not None and v == v and abs(v) >= 1.2
                   for v in (s.get("volume_z"), s.get("block_intensity_z")))]
    cand.sort(key=lambda s: -(max(s.get("volume_z") or 0, s.get("block_intensity_z") or 0)))
    for s in cand[:14]:
        def sg(v, d=1, suf=""):
            if v is None or v != v:
                return '<span class="d">–</span>'
            c = "g" if v > 0 else "r"
            return f'<span class="{c}">{v:+.{d}f}{suf}</span>'
        rows.append(
            f'<tr data-tk="{s["ticker"]}"><td class="l"><span class="tk">{s["ticker"]}</span> '
            f'<span class="nm">{s["name"]}</span></td>'
            f'<td data-k="volume_z">{sg(s.get("volume_z"), 1, "σ")}</td>'
            f'<td data-k="dollar_volume_z">{sg(s.get("dollar_volume_z"), 1, "σ")}</td>'
            f'<td data-k="block_intensity_z">{sg(s.get("block_intensity_z"), 1, "σ")}</td>'
            f'<td data-k="absorption">{sg(s.get("absorption"), 2)}</td>'
            f'<td data-k="ad_balance">{sg(s.get("ad_balance"), 2)}</td>'
            f'<td data-k="ret_5d">{sg(s.get("ret_5d"), 1, "%")}</td></tr>'
        )
    if not rows:
        return '<div class="sub">No unusual volume or block-concentration readings today.</div>'
    return ("<table><thead><tr><th class='l'>Sector</th><th>Volume σ</th><th>$Volume σ</th>"
            "<th>Block conc. σ</th><th>Absorption</th><th>A/D balance</th><th>5d %</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table>")


def render(payload: dict) -> str:
    sectors = payload["sectors"]
    reg = payload["regime"]
    meta = payload["meta"]

    # strip in-process pandas objects before embedding
    clean = []
    for s in sectors:
        c = {k: v for k, v in s.items() if k != "_raw"}
        clean.append(c)
    # Trend parameters travel with the data rather than being duplicated as JS
    # literals, so the dashboard can never disagree with config.py about what a
    # "21-day change" actually measures.
    embed = json.dumps({"sectors": clean, "regime": reg, "meta": meta,
                        "flow": payload["flow"],
                        "tcfg": {"smooth": config.TREND_SMOOTH,
                                 "fast": config.TREND_HORIZON_FAST,
                                 "slow": config.TREND_HORIZON_SLOW,
                                 "min_history": config.TREND_MIN_HISTORY,
                                 "spark_len": config.SPARK_LEN}},
                       default=str)

    prov = meta["providers"]
    badges = "".join(
        f'<span class="badge {"on" if prov.get(k) else "off"}">{lbl}: '
        f'{"live" if prov.get(k) else "off"}</span>'
        for k, lbl in (("fmp", "FMP"), ("polygon", "Polygon"),
                       ("yahoo", "Yahoo"), ("off_exchange", "Dark pool"))
    )

    dq = meta.get("data_quality", {})
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Money · Sector Transition Dashboard</title>
<style>{CSS}</style></head><body>
<div id="tip"></div>

<header>
  <div>
    <h1>Smart Money · Sector Transition Dashboard</h1>
    <div class="sub">Institutional capital flow, sector rotation and unusual activity across
      {meta['n_tier1']} GICS sectors and {meta['n_tier2']} industry sub-groups ·
      benchmark {reg['benchmark']} · data as of {meta['as_of']}</div>
  </div>
  <div class="hdr-badges">{badges}
    <span class="badge">generated {meta['generated_at']}</span>
    <div id="rfz" class="rfz" hidden>
      <button id="rfz-off" class="rbtn">Rebuild from cache</button>
      <button id="rfz-get" class="rbtn paid">Fetch latest data</button>
      <div id="rfz-msg" class="rfz-msg"></div>
    </div>
  </div>
</header>

{_regime_html(reg, sectors)}

{_macro_html(reg)}

<div class="two" style="margin-top:22px">
  <div class="panel rrg-wrap">
    <h3>Relative Rotation Graph</h3>
    <div class="sub" style="margin:4px 0 10px">Rotation runs clockwise. Improving → Leading is the
      accumulation-to-markup transition; Weakening → Lagging is distribution into capital flight.
      Tails show the last {config.RRG_TAIL} sessions. Click a bubble for detail.</div>
    <div class="rrg-ctl">
      <span class="tab active" data-tier="1">GICS sectors</span>
      <span class="tab" data-tier="2">Industry sub-groups</span>
    </div>
    <div id="rrg"></div>
    <div class="legend">
      <span><i style="background:var(--green)"></i>Confirmed breakout</span>
      <span><i style="background:var(--yellow)"></i>Stealth accumulation</span>
      <span><i style="background:var(--orange)"></i>Distribution</span>
      <span><i style="background:var(--red)"></i>Capital flight</span>
      <span><i style="background:var(--grey)"></i>Neutral</span>
    </div>
  </div>
  <div class="panel">
    <h3>Alert feed</h3>
    <div class="sub" style="margin:4px 0 10px">Ordered by signal quality: confirmed breakouts,
      then stealth accumulation, then distribution and flight.</div>
    <div class="feed">{_alerts_html(sectors)}</div>
  </div>
</div>

<h2>Where relative strength is moving</h2>
<div class="panel">
  <div class="sub" style="margin-bottom:14px;line-height:1.6">
    <b>Nobody can see money move between sectors.</b> There is no ledger linking a sale in
    one sector to a purchase in another, and sector ETF volume is a small slice of total
    sector exposure. What these two lists <i>do</i> show is measured: which sectors are
    beating their peer group and which are trailing it, ranked by VMS — the one score here
    that passed out-of-sample testing.<br><br>
    Reading it as rotation adds an assumption: that institutions fund new positions by
    selling old ones. Plausible, and unobserved. To watch rotation actually happen, use the
    RRG above — each tail traces a sector's real path through the quadrants over the last
    {config.RRG_TAIL} sessions.</div>
  {_rotation_html(payload['flow'])}
</div>

{_validation_html()}

<h2>GICS sectors — ranked by VMS</h2>
<div class="panel"><div id="tbl1"></div>
  <div class="note"><b>VMS</b> (validated) = equal-weighted cross-sectional z-scores of 12-1
    momentum and RS-Momentum. <b>CSRI</b> (not validated) = weighted z-score of Mansfield RS
    30%, RS-Momentum 20%, breadth 20%, Chaikin Money Flow 15%, institutional footprint 15%.
    Both are shown so you can see where they disagree — CSRI is kept for diagnosis, not
    for decisions. Note that all models tested had <i>negative</i> IC at a 5-session horizon,
    so this is not a short-term timing tool. Click any row for detail; click headers to sort.</div>
</div>

<h2>Industry sub-groups — ranked by VMS</h2>
<div class="panel"><div id="tbl2"></div>
  <div class="note">Sub-industry view. Prehn's "picks and shovels" framing lives here: a strong
    parent sector signal is only actionable if a specific sub-group is absorbing the flow.</div>
</div>

{_flow_html(sectors, meta)}

{_squeeze_html(sectors)}

<h2>Unusual activity</h2>
<div class="panel">{_unusual_html(sectors)}
  <div class="note">Volume σ and $Volume σ are z-scores against a {config.VOLUME_ZSCORE_WINDOW}-day
    baseline. Block concentration measures how much of the period's volume arrived in a handful of
    outsized sessions. Absorption flags heavy volume with the close held high and little net price
    progress — the signature of a large order being worked without advertising the bid.
    {'Off-exchange share from tick data is included in the institutional footprint.' if prov.get('off_exchange') else 'These are daily-bar proxies for block activity; a tick-data or dark-pool feed replaces them with actual off-exchange prints.'}</div>
</div>

<div id="detail" class="detail"></div>

<h2>Method & caveats</h2>
<div class="panel">
  <div class="sub" style="line-height:1.65">
    <b>What this measures.</b> Institutional capital cannot enter or exit large positions quickly
    without moving price, so it leaves footprints — in relative strength before absolute price, in
    breadth before the index, and in volume distribution before the trend. This dashboard scores
    those footprints per sector and classifies each into an accumulation, markup, distribution or
    decline phase.<br><br>
    <b>What it does not measure.</b> {'' if prov.get('off_exchange') else 'Without a tick-data or dark-pool feed, actual off-exchange block prints and options sweeps are not observed; the institutional footprint column uses daily-bar proxies (absorption, accumulation/distribution day balance, volume concentration). These correlate with but do not equal real dark pool flow. '}13F positions
    are quarterly and lag by up to 45 days, so they are not used here. Breadth uses a
    {meta['breadth_sample']}-name sample per ETF rather than full holdings.<br><br>
    <b>Interpretation order.</b> Read macro weather first — a stealth accumulation signal in a
    RISK-OFF regime is weaker than the same signal in RISK-ON. Then read the sector, then the
    sub-industry. Signals are probabilistic, not deterministic; a phase classification is a reason
    to investigate, not a trade instruction.<br><br>
    <b>Data quality.</b> {dq.get('ok', 0)} of {dq.get('requested', 0)} instruments returned usable
    history; {dq.get('constituents_ok', 0)} constituent series fetched for breadth.
    {('Missing: ' + ', '.join(dq.get('missing', []))) if dq.get('missing') else ''}<br><br>
    <span style="color:var(--dim2)">This is research tooling, not investment advice. Nothing here is
    a recommendation to buy or sell any security.</span>
  </div>
</div>

<script>window.__SMF__ = {embed};</script>
<script>{JS}</script>
</body></html>"""


def write_report(payload: dict, path=None) -> str:
    path = path or (config.OUTPUT_DIR / "dashboard.html")
    html = render(payload)
    path.write_text(html, encoding="utf-8")
    return str(path)


def write_json(payload: dict, path=None) -> str:
    path = path or (config.OUTPUT_DIR / "snapshot.json")
    clean = {
        "meta": payload["meta"],
        "regime": payload["regime"],
        "flow": payload["flow"],
        "sectors": [{k: v for k, v in s.items() if k != "_raw"} for s in payload["sectors"]],
    }
    path.write_text(json.dumps(clean, indent=2, default=str), encoding="utf-8")
    return str(path)


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
