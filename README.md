# Smart Money · Sector Transition Dashboard

Detects institutional capital rotation across sectors and industry sub-groups — where
money is quietly accumulating before price confirms, where it is being distributed into
retail strength, and which sectors are seeing unusual block activity.

Built on the Felix Prehn / Goat Academy "top-down capital flow" framework (macro weather →
sector allocation → asset leadership), implemented with Weinstein stage analysis, Mansfield
relative strength, Relative Rotation Graphs, and volume-microstructure proxies for
institutional footprints.

---

## Quick start

**A Polygon API key is required.** `smf/config.py` sets
`REQUIRE_PROVIDER = "polygon"`, so the pipeline aborts rather than quietly
falling back to a free source — see [Data source is enforced, not
preferred](#data-source-is-enforced-not-preferred). Copy `.env.example` to
`.env` and fill it in first.

```bash
pip install -r requirements.txt

python warm_cache.py        # first run only: pulls ~480 price series (15-40 min cold)
python run.py --open        # generates and opens output/dashboard.html
```

### The three rebuild modes, from cheapest to most expensive

| Command | What it does | Typical time |
|---|---|---|
| `run.py --render-only` | Rebuilds the HTML from the last `snapshot.json`. Recomputes nothing. | **under 1s** |
| `run.py --offline` | Recomputes every metric from the cache. **Zero API calls.** | ~40s |
| `run.py` | Refetches anything older than 12h, then recomputes. | 1-3 min warm |

`--render-only` is the one you want after changing the report layout;
`--offline` after changing a metric; the bare command when you want new prices.

### Tests

```bash
python test_flow.py test_diagnostics.py test_volume.py test_validation.py
python audit_invariants.py           # 1,600+ system self-consistency checks

npm install jsdom                    # browser suites only
node test_tooltips.js                # tooltips, trends, sparklines, panels
node test_table_sort.js              # sorting and column alignment
node test_refresh_ui.js              # in-page refresh controls
```

`audit_invariants.py` is the one to run when something looks wrong: it checks
that displayed values match freshly recomputed ones, that no past value changes
when future data is appended (lookahead), and that no secret reached the output.

### With your FMP or Polygon key

Create a file called `.env` next to `run.py`:

```
FMP_API_KEY=your_fmp_key
POLYGON_API_KEY=your_polygon_key
SMF_WEBHOOK_URL=https://hooks.slack.com/services/...
```

What each key adds:

| Key | Unlocks |
|---|---|
| **FMP** | Live ETF holdings (replaces the static constituent samples, so breadth uses real current weights), more reliable history, fewer dead tickers |
| **Polygon** | Tick-level trade data → real off-exchange volume share, dark pool (ATS) share, and actual block prints with direction. Already enabled in config; just add the key. Needs a plan with `/v3/trades` |
| **Webhook** | `python run.py --alert` posts Phase 1 and Phase 2 signals to Slack/Discord |

### Verifying the tick-flow module

The flow code can't run without a paid Polygon key, so it ships with a fixture-driven
test suite that has hand-computable answers:

```bash
python test_flow.py        # 35 assertions: volume accounting, block detection,
                           # tick test, caching, pagination cap, scoring bounds
```

Run this after changing anything in `smf/flow.py`.

---

## Data source is enforced, not preferred

`smf/config.py` sets `REQUIRE_PROVIDER = "polygon"`. With that set, the system
**refuses to run** rather than falling back to free data:

```
ERROR: config.REQUIRE_PROVIDER = 'polygon' but no API key is set for it.
```

This is deliberate. A silent fallback is the most dangerous failure mode here — the
numbers still render, the dashboard still looks authoritative, and nothing tells you the
foundation changed. Set `REQUIRE_PROVIDER = None` to allow fallback for development.

Cached bars also record which provider produced them (`data/_sources.json`). Adding a
Polygon key invalidates every Yahoo-sourced series and forces a refetch, so you can never
believe you're on Polygon data while actually reading Yahoo bars.

Check provenance any time with:

```bash
python audit_data.py          # provenance + integrity report, provider comparison
```

That also runs structural checks (high < low, zero volume, sustained volume level shifts
indicating an unadjusted split, implausible daily moves, stale quotes) and — when two
providers are available — quantifies how much they disagree on price *and volume*
separately. Volume is the one that matters: Yahoo doesn't reliably split-adjust it, and
four metrics are volume-based.

## Backtesting

```bash
python backtest.py                       # 5y walk-forward, weekly steps
python backtest.py --step 20             # faster, coarser
python backtest.py --tier1               # GICS sectors only
python backtest.py --no-breadth          # robustness check without the
                                         # survivorship-biased breadth metric
python backtest.py --fit-weights         # search for optimal component weights
python backtest.py --horizons 21,63,126
```

Writes `output/backtest_report.md` plus episode- and observation-level CSVs.

**Run `--step 20`, not `--step 5`.** Tested directly: 5 years at step 20 gives 754
episodes, step 5 gives 743 — four times the compute for identical statistical power,
because finer sampling re-samples the same phase runs. Episode count tracks *history
length*, not sampling frequency.

**Methodology commitments**, each of which changes the answer materially:

- **Relative returns** (sector minus benchmark). Absolute returns are dominated by market
  beta and would flatter every phase in an uptrend.
- **Episodes, not sector-days.** Thirty consecutive days of "Confirmed Breakout" in XLE is
  one observation, not thirty. Counting days inflates *n* by roughly the mean episode
  length and makes any significance test meaningless.
- **Block bootstrap** for p-values, block size `max(8, 2·ceil(horizon/step))`. Measured
  autocorrelation of 21-day forward returns is +0.74 at lag 5, so a fixed small block
  overstates significance at longer horizons.
- **Two-sided spread test** with an explicit verdict taxonomy. An earlier one-sided
  version reported p=1.0 for a true −4% spread, discarding a reliably *inverted* signal
  as noise — the most expensive kind of bug in research code.
- **Point-in-time metrics**, verified: appending future data does not change any indicator
  value at time *t*, including the cross-sectional RRG normalization.
- **Strict phase rules by default.** The dashboard degrades gracefully when a metric is
  missing; the backtest must not, or it measures rules the product doesn't ship.
- **Benjamini-Hochberg FDR** across the test family. Sixteen tests at raw α=0.05 give a
  ~56% family-wise error rate.
- **Pre-committed pass/fail/kill criteria** in `smf/backtest.py::CRITERIA`, so the verdict
  isn't decided after seeing the numbers.

### Beyond significance testing

`smf/diagnostics.py` adds the checks that decide whether a significant result *means*
anything (50 tests in `test_diagnostics.py`, each with a known correct answer):

| Check | Question it answers |
|---|---|
| Distribution characterisation | Are returns heavy-tailed, making the mean the wrong statistic? Reports shape, skew, excess kurtosis, percentiles, `mean_is_appropriate` |
| Effect size | Cohen's d, Hedges' g, probability of superiority. Is the difference *large enough to act on*, not just unlikely to be chance? |
| Mann-Whitney cross-check | Does a rank-based test agree with the bootstrap? Disagreement localises the problem to outliers |
| Outlier influence | Raw vs median vs trimmed vs winsorised means. Does the edge survive removing the tail? Outliers are measured, never silently dropped |
| Segment stability | Does the conclusion reverse inside subperiods? (Simpson's paradox — a KILL condition) |
| Cross-validation | The headline spread computed two independent ways; any discrepancy is a KILL |
| Red-flag scan | Hit rates pinned at 0/100%, identical phase means, constant components, duplicate rows, phase concentration, suspiciously uniform ordering |
| Portfolio simulation | Long-top/short-bottom with turnover and costs in bps. Episode statistics are not strategy P&L |
| Confidence assessment | Ready to share / Share with caveats / Needs revision, with the caveats attached |

### Verdict taxonomy

| Verdict | Meaning | Action |
|---|---|---|
| `PASS` | Criteria met, adequately powered | Act on it |
| `MARGINAL` | More passes than fails | Watchlist, not a signal |
| `UNDERPOWERED` | Tests could not resolve the question | Get more data. **Do not conclude** |
| `FAIL` | More fails than passes | Rework |
| `KILL` | Significantly inverted, confirmed zero, outlier-driven, Simpson's paradox, or failed cross-validation | Abandon or invert |

`UNDERPOWERED` exists because an earlier version reported `KILL` on absence of
evidence. Those are different conclusions and now have different labels.

### Does it answer what weight each indicator should get?

Yes — `--fit-weights`. The objective is **rank information coefficient**: the mean
cross-sectional Spearman correlation between the weighted score and forward relative
return, computed per date then averaged. That's the correct objective because the score is
used to *rank* sectors, not to predict a level.

Guards against fooling yourself:

- **Time-ordered train/test split**, never random. A random holdout leaks, because
  neighbouring dates share overlapping forward windows and autocorrelated features.
- **Weights constrained non-negative and summing to 1.** Every component is built so
  higher is more bullish; allowing negative weights would let the fit invert an
  indicator's meaning to chase noise and produce an uninterpretable model.
- **Four reference weightings reported side by side** — fitted, equal, current (the
  borrowed 30/20/20/15/15), and single-best-component — each with train *and* test IC.

The number to look at is not the fitted weights. It's whether they beat **equal weights
out of sample**. Often they don't, and the honest conclusion is then "use equal weights,
the fit was chasing noise". The tool says so explicitly rather than burying it. It also
flags when a single component explains ≥90% of the signal, in which case the composite is
theater and you should use that one indicator alone.

Rank IC around 0.03–0.05 is typical for a real cross-sectional signal. Above ~0.15 on this
sample size is more likely a data problem than an edge.

## Commands

```bash
python run.py                          # full universe: 11 sectors + 21 industry groups
python run.py --open                   # ...and open in a browser
python run.py --tier1                  # GICS sectors only (fast)
python run.py --tier2                  # industry sub-groups only
python run.py --tickers XLE,SMH,URA    # specific ETFs
python run.py --no-breadth             # skip constituent fetch (much faster, drops breadth)
python run.py --fresh                  # ignore cache, refetch everything
python run.py --alert                  # post signals to SMF_WEBHOOK_URL

python warm_cache.py                   # pre-fetch all price series
python warm_cache.py --limit 100       # ...in a bounded pass (resumable)
```

Output lands in `output/dashboard.html` (self-contained, works offline) and
`output/snapshot.json` (machine-readable, for backtesting or feeding elsewhere).

---

## What it measures

### The four phases

Every sector is classified into one of four states, mirroring the Weinstein cycle:

| Phase | Signature | What it means |
|---|---|---|
| **Stealth Accumulation** (yellow) | Mansfield RS still below zero, but RS-Momentum > 100, breadth broadening, positive institutional footprint | Institutions are absorbing supply before relative price confirms. Highest edge, least confirmation |
| **Confirmed Breakout** (green) | Mansfield RS crossed above zero, breadth > 60%, money flow positive, Leading/Improving quadrant | The trend is live and broadly participated. Prehn's execution window |
| **Distribution** (orange) | Mansfield RS still positive, but RS-Momentum decaying and money flow negative | Institutions selling into retail enthusiasm. Tighten stops or trim |
| **Capital Flight** (red) | Mansfield RS below zero, no momentum support, breadth collapsed | Capital has left. Avoid long exposure; short/hedge candidate |

### The metrics

| Metric | Weight in composite | What it captures |
|---|---|---|
| **Mansfield Relative Strength** | 30% | `((RS / SMA₂₀₀(RS)) − 1) × 100` where `RS = sector / SPY`. Zero-bounded, so an upward zero-cross is an unambiguous structural-outperformance trigger |
| **RS-Momentum** (RRG y-axis) | 20% | Velocity of relative strength. Leads price, which is what makes rotation visible before it shows up on a chart |
| **Constituent breadth** | 20% | % of constituents above their 50-day SMA. Catches the case where two mega-cap weights carry an ETF while the median name bleeds |
| **Chaikin Money Flow** | 15% | Volume-weighted accumulation over 21 sessions. Bounded −1..+1 |
| **Institutional footprint** | 15% | Composite of absorption, accumulation/distribution day balance, and block-print concentration (see below) |

Each is converted to a rolling 252-day z-score before weighting, so sectors with
structurally different volatility are comparable. The result is the **Composite Sector
Rotation Index (CSRI)**. If a component is unavailable, its weight is redistributed
proportionally rather than defaulting to zero.

### Relative Rotation Graph

Sectors rotate clockwise through four quadrants:

```
   RS-Momentum
        ▲
        │  Improving          Leading
        │  (accumulating)     (participating)
    100 ┼──────────────────────────────
        │  Lagging            Weakening
        │  (capital flight)   (distributing)
        └──────────── 100 ─────────────▶  RS-Ratio
```

**Important implementation note:** RRG coordinates are normalised *cross-sectionally* —
z-scored across the peer group at each date, per tier. Normalising each sector against its
own history (the obvious approach, and what a first pass of this code did) makes a sector in
sustained freefall score above 100 the moment its decline merely decelerates, which inverts
the meaning of the quadrants entirely.

### Institutional footprint — observed vs inferred

This is the one metric with two regimes, and the dashboard labels which one each sector is
using (`measured` vs `inferred` badge).

**Observed** (Polygon key present, sector in `OFF_EXCHANGE_TICKERS`). Read directly from
trade-level data:

| Signal | Weight | What it is |
|---|---|---|
| **Dark pool share trend** | 35% | Share of volume printing on an ATS. Polygon tags off-exchange trades with `exchange == 4`; those carrying a `trf_id` are ATS executions specifically. This is the cleanest read on stealth positioning |
| **Off-exchange share trend** | 25% | All volume reported to a FINRA TRF, ATS or otherwise |
| **Block direction** | 25% | A tick test on each block print: positive means blocks arriving on upticks, i.e. buyers crossing the spread |
| **Block share trend** | 15% | Growing share of volume arriving in outsized single prints |

A block is a single print of ≥10,000 shares **or** ≥$200,000 notional (either condition
suffices, so large share counts in cheap names and large notional in expensive names both
register). TRF exchange ids are read from Polygon's reference endpoint at runtime rather than
hardcoded, since Polygon has added TRF venues before.

The observed score carries 75% weight, blended with 25% of the daily-bar proxies. The proxies
stay in deliberately: tick coverage can truncate on heavy sessions, and a proxy that disagrees
loudly with observed flow is worth seeing rather than discarding.

Cost is controlled by never storing raw trades — each ticker-day is reduced to a small JSON
summary in `data/flow/` and fetched exactly once. A completed session's trades never change,
so only new sessions cost anything. First run on 11 sector ETFs across 20 sessions is roughly
220 ticker-days.

**Inferred** (no key, or sector outside `OFF_EXCHANGE_TICKERS`). Three daily-bar proxies:

- **Absorption** — heavy volume with the close pinned in the upper part of the range and a
  suppressed daily return. When a large buyer works an order through dark pools and VWAP
  algos, this is what the daily tape looks like: supply absorbed without advertising the bid.
- **Accumulation/distribution day balance** — up-closes on above-average volume minus
  down-closes on above-average volume over 25 sessions. One of the oldest and most robust
  proxies for which side is being forced to cross the spread.
- **Block concentration** — how much of the period's volume arrived in a handful of outsized
  sessions, versus a uniform distribution. Detects the fat right tail that block execution
  leaves behind.

These correlate with real dark-pool flow but are not the same thing.

**The signature to look for**, in either regime: a rising dark pool share with flat price and
positive block direction. That combination means size is being accumulated without moving the
quote — which is precisely what an institution building a position is trying to achieve.

---

## Reading the dashboard

Read it top-down, in this order — the same hierarchy the methodology prescribes:

1. **Macro weather.** Is the benchmark above a rising 200d MA? A stealth-accumulation signal
   in a RISK-OFF regime is a much weaker signal than the same reading in RISK-ON.
2. **Cyclical vs defensive tilt.** Which side of the market is absorbing flow overall.
3. **Rotation pairs.** Institutions fund new positions by selling old ones, so the panel
   pairs the strongest net-inflow sectors against the strongest net-outflow sectors. The
   source of capital is often more informative than the destination.
4. **Sector table**, then **industry table.** A strong parent-sector signal is only
   actionable if a specific sub-group is absorbing the flow — this is where "picks and
   shovels" lives.
5. **Unusual activity.** Volume and block-concentration z-scores, for names where something
   changed today.

Click any row or RRG bubble for the full component breakdown: which metric contributed how
much to the composite, and the institutional footprint detail.

---

## Configuration

Everything tunable lives in `smf/config.py`:

- `SECTORS` — the universe. Add an ETF with its `tier`, `group`, and a constituent sample
- `CSRI_WEIGHTS` — composite weights
- `THRESHOLDS` — phase-classification and alert cutoffs
- `MANSFIELD_SMA`, `BREADTH_SMA`, `CMF_PERIOD`, `RRG_*`, `ZSCORE_WINDOW` — metric parameters
- `ENABLE_POLYGON_OFF_EXCHANGE` — turn on real dark-pool analysis

To add a sector ETF:

```python
"XME": {
    "name": "Metals & Mining",
    "tier": 2,
    "group": "Materials",
    "constituents": ["NUE", "STLD", "FCX", "NEM", "AA", "CLF", "X", "RS", "CMC", "ATI"],
},
```

---

## Layout

```
smf/
  config.py      universe, weights, thresholds, API keys
  providers.py   FMP → Polygon → Yahoo fallback chain, caching, negative cache
  metrics.py     all indicator math (pure pandas, no network — easy to test)
  flow.py        tick-level order flow: off-exchange share, dark pool, blocks
  scoring.py     z-scoring, composite index, phase classification, regime
  report.py      self-contained HTML renderer (inline SVG + vanilla JS, no CDN)
  pipeline.py    orchestration
  backtest.py    walk-forward engine, episode stats, attribution, weight fitting
run.py           CLI — generate the dashboard
backtest.py      CLI — run the walk-forward backtest
audit_data.py    CLI — data provenance and integrity audit
warm_cache.py    resumable bulk price fetch
test_flow.py     fixture-driven tests for the flow module
data/            price cache (CSV), _sources.json provenance, flow/ summaries
output/          dashboard.html, snapshot.json, backtest_report.md
```

## Preliminary backtest result — read this before trusting the dashboard

A first walk-forward run returned **KILL**, with the phase ordering *inverted*:
Confirmed Breakout averaged −0.54% forward relative return over 21 sessions with a 40.7%
hit rate, while Capital Flight was the best-performing phase.

That run was on degraded settings — Yahoo bars, `--no-breadth` (removing 20% of the
composite and the breadth conditions in the phase rules), monthly sampling that made
episode-collapsing a no-op, tier 1 only, and ~3.7 years spanning roughly one macro regime.
So it is **not** a verdict. But it is a serious red flag, and there is a plausible
mechanism: at a 21-day horizon, sector ETFs exhibit mean reversion, and a momentum-based
classifier can sit on the wrong side of it.

Rerun properly on Polygon data, full universe, weekly steps, with breadth, before drawing
conclusions — and treat the dashboard's phase labels as unvalidated until then.

---

## Known limitations

- **The signals are unvalidated.** This is the real limitation. The composite weights and
  phase thresholds come from the source research, not from a backtest on your data. One
  threshold shipped effectively dead (`inst_flow >= 0.30` cleared 1 of 32 sectors, which
  would have silenced the stealth-accumulation signal entirely) and was only caught by
  inspecting the distribution. Others may be miscalibrated in less obvious ways. Until you
  replay history and measure forward returns by phase, treat the phase labels as a
  prioritised watchlist rather than as evidence of edge.
- **Dark pool data is proxied, not observed** for any sector outside `OFF_EXCHANGE_TICKERS`,
  or entirely if no Polygon key is set.
- **Flow is measured on the ETF, not its constituents.** Off-exchange share on XLK reflects
  trading in the ETF itself; a large buyer accumulating individual semis may not show up
  there. Adding constituent-level flow is possible but multiplies the request count.
- **No options flow.** Sweep sentiment (the ratio of ask-side call sweeps to put sweeps) is
  in the original design but needs an options-flow provider. Unusual Whales is the usual
  choice; the hook is `institutional_flow_score()` in `scoring.py`.
- **Positive flow does not override momentum.** A sector can show accumulating dark-pool flow
  and still be classified Capital Flight, because Phase 1 additionally requires RS-Momentum
  above 100. This is intentional — it stops the system catching falling knives — but it means
  the flow table sometimes disagrees with the phase label. When it does, the flow table is
  the earlier signal and the phase label is the more conservative one.
- **13F data is not used.** Quarterly filings lag by up to 45 days, which is far too slow for
  rotation detection.
- **Breadth uses a 10–20 name sample** per ETF unless FMP holdings are available. Readings
  from fewer than 8 resolvable constituents are suppressed rather than reported.
- **Daily bars only.** Intraday rotation is not detected.
- **Constituent lists drift.** Acquisitions and delistings accumulate; the negative cache
  stops them wasting fetch time, but the lists should be refreshed occasionally (or use an
  FMP key, which pulls live holdings).

---

## Extending it

**Add options flow.** Implement a provider function returning ask-side call and put sweep
volume per sector, then add a `sweep_ratio` key to `institutional_flow_score()` in
`scoring.py` and give it a weight in `INST_FLOW_WEIGHTS`.

**Backtest the signals.** `output/snapshot.json` is written on every run. Schedule daily runs
to accumulate a time series of CSRI and phase labels, then measure forward returns by phase.
This is the right way to calibrate `CSRI_WEIGHTS` and `THRESHOLDS` — the current weights come
from the source research, not from your own fitted results.

**Schedule it.** On Windows, Task Scheduler running `python run.py --alert` after the close
(around 4:30pm ET) gives you a daily rotation update in Slack.

---

*Research tooling, not investment advice. Nothing produced by this system is a recommendation
to buy or sell any security. Signals are probabilistic and a phase classification is a reason
to investigate, not a reason to trade.*
