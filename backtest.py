#!/usr/bin/env python3
"""
Walk-forward backtest of the Smart Money sector-transition signals.

    python backtest.py                      # full run, 5y, weekly steps
    python backtest.py --step 10            # faster, coarser sampling
    python backtest.py --tier1              # GICS sectors only
    python backtest.py --no-breadth         # robustness check without the
                                            # survivorship-biased breadth metric
    python backtest.py --fit-weights        # also search for optimal weights
    python backtest.py --horizons 21,63,126

Outputs `output/backtest_report.md`, `output/backtest_observations.csv` and
`output/backtest_episodes.csv`.

DATA PROVENANCE MATTERS HERE. Run `python audit_data.py` first. A backtest on
Yahoo bars is fine for iterating on the harness, but Yahoo does not reliably
split-adjust volume (four metrics are volume-based) and deletes delisted tickers
(the main source of survivorship bias in breadth). Results intended to inform
real decisions should come from Polygon.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from smf import backtest as bt
from smf import config, providers


def _fmt_table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_no data_\n"
    return df.to_markdown(index=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=5,
                    help="sessions between rebalance dates (default 5 = weekly)")
    ap.add_argument("--horizons", type=str, default="21,63")
    ap.add_argument("--tier1", action="store_true")
    ap.add_argument("--tier2", action="store_true")
    ap.add_argument("--no-breadth", action="store_true")
    ap.add_argument("--fit-weights", action="store_true")
    ap.add_argument("--warmup", type=int, default=260)
    ap.add_argument("--allow-fallback", action="store_true",
                    help="proceed even if bars are not from config.REQUIRE_PROVIDER "
                         "(results are NOT trustworthy; for harness development only)")
    ap.add_argument("--lenient-rules", action="store_true",
                    help="use the dashboard's lenient phase rules (a missing metric "
                         "does not disqualify). Produces results that do NOT "
                         "correspond to the shipped classifier — diagnostic only")
    ap.add_argument("--cost-bps", type=float, default=5.0,
                    help="round-trip transaction cost for the portfolio simulation")
    ap.add_argument("--reuse-obs", action="store_true",
                    help="skip the walk-forward and re-analyse "
                         "output/backtest_observations.csv from a previous run")
    args = ap.parse_args()

    # The backtest must measure the rules the product ships. With lenient rules a
    # missing metric silently relaxes the gate, so phases fire on a weaker
    # definition than the dashboard uses — which invalidated an earlier run.
    config.STRICT_RULES = not args.lenient_rules

    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    tickers = (config.TIER1 if args.tier1 else
               config.TIER2 if args.tier2 else config.ALL_TICKERS)

    print("=" * 70)
    print("Smart Money · Walk-Forward Backtest")
    print("=" * 70)

    # ---- data-source gate -------------------------------------------------
    # A backtest is only as trustworthy as its bars. Rather than warn and
    # continue, refuse: a report that says PASS on the wrong data is worse than
    # no report, because it gets remembered as evidence.
    req = getattr(config, "REQUIRE_PROVIDER", None)
    try:
        act = providers.active_providers()
    except providers.ProviderUnavailable as exc:
        print(f"\nERROR: {exc}\n")
        print("Backtest aborted. No results are better than results built on the")
        print("wrong data source.")
        return 2

    audit = providers.cache_audit()
    print(f"Data provenance: required={req or 'any'} active={act} "
          f"cached={audit['cached_series']} by_provider={audit['by_provider']}")

    if req and not args.allow_fallback:
        wrong = {k: v for k, v in audit["by_provider"].items()
                 if k not in (req, "unknown") and v > 0}
        if wrong:
            print(f"\n  ! {sum(wrong.values())} cached series came from {wrong} "
                  f"rather than {req}.")
            print(f"    With STRICT_CACHE_PROVENANCE they will be refetched from "
                  f"{req} as the backtest runs.")
        n_unknown = audit["by_provider"].get("unknown", 0)
        if n_unknown:
            print(f"  ! {n_unknown} series have unknown provenance — also refetched.")
    if audit["mixed"]:
        print("  ! MIXED PROVENANCE — cross-sectional metrics would compare "
              "unlike things. Delete data/ and refetch from one provider.")
        if not args.allow_fallback:
            return 2
    print()

    obs_path = Path(config.OUTPUT_DIR) / "backtest_observations.csv"
    if args.reuse_obs:
        if not obs_path.exists():
            print(f"No cached observations at {obs_path}. Run without --reuse-obs first.")
            return 1
        obs = pd.read_csv(obs_path, parse_dates=["date"])
        print(f"Reusing {len(obs):,} cached observations from {obs_path.name} "
              f"({obs['ticker'].nunique()} tickers, "
              f"{obs['date'].min().date()} → {obs['date'].max().date()})")
        print("  NOTE: the walk-forward was NOT recomputed. Metric or rule changes "
              "since that run are not reflected.")
    else:
        obs = bt.run_walk_forward(tickers=tickers, horizons=horizons, step=args.step,
                                  warmup=args.warmup, skip_breadth=args.no_breadth)
    if obs.empty:
        print("No observations produced — check data availability.")
        return 1

    eps = bt.to_episodes(obs)
    print(f"  collapsed to {len(eps)} episodes "
          f"(median length {eps['length_obs'].median():.0f} observations)\n")

    out = Path(config.OUTPUT_DIR)
    if not args.reuse_obs:
        obs.to_csv(out / "backtest_observations.csv", index=False)
    eps.to_csv(out / "backtest_episodes.csv", index=False)

    # ---------------- analysis ----------------
    verdict = bt.evaluate(eps, obs, step=args.step)

    lines: list[str] = []
    A = lines.append
    A("# Smart Money · Backtest Report\n")
    A(f"- **Universe**: {len(tickers)} ETFs "
      f"({'tier 1 only' if args.tier1 else 'tier 2 only' if args.tier2 else 'both tiers'})")
    A(f"- **Period**: {obs['date'].min().date()} → {obs['date'].max().date()}")
    A(f"- **Rebalance**: every {args.step} sessions")
    A(f"- **Observations**: {len(obs):,} sector-dates → **{len(eps):,} episodes**")
    A(f"- **Breadth included**: {'no (robustness run)' if args.no_breadth else 'yes'}")
    A(f"- **Phase rules**: {'LENIENT (diagnostic only — not the shipped rules)' if args.lenient_rules else 'strict (missing metric disqualifies — matches product)'}")
    A(f"- **Historical members**: "
      f"{'included (survivorship-corrected)' if config.INCLUDE_HISTORICAL_MEMBERS else 'excluded (survivorship-biased)'}")
    A(f"- **Data provenance**: {audit['by_provider']}")
    A("")
    A(f"## Verdict: **{verdict['verdict']}**")
    A(f"{verdict['n_pass']} checks passed, {verdict['n_fail']} failed\n")
    for c in verdict["checks"]:
        mark = "PASS" if c["pass"] else ("FAIL" if c["pass"] is False else "n/a ")
        A(f"- `{mark}` **{c['check']}** — {c['detail']}")
    if verdict["kill"]:
        A("\n### Kill criteria triggered\n")
        for k in verdict["kill"]:
            A(f"- {k}")
    A("")

    for h in horizons:
        A(f"## Forward relative return by phase — {h} sessions\n")
        A("_Episode-level. Forward return is sector minus benchmark, in percent._\n")
        A(_fmt_table(bt.phase_table(eps, h)))
        sp = bt.spread_test(eps, h)
        A(f"**Breakout − Flight spread**: {sp.get('spread_%')}% "
          f"(p={sp.get('p_value')}, n_long={sp.get('n_long')}, "
          f"n_short={sp.get('n_short')}, ci95={sp.get('ci95')})\n")
        mono = bt.monotonicity(eps, h)
        A(f"**Ordering** — expected `{' > '.join(mono['expected'])}`; "
          f"realised `{' > '.join(mono['realised'])}`; "
          f"rank corr **{mono['rank_corr']}**; monotonic: **{mono['monotonic']}**\n")

    A("## Component attribution (21 sessions)\n")
    A("_Top-tercile minus bottom-tercile forward relative return, formed "
      "cross-sectionally at each date. A component that cannot separate terciles "
      "is not carrying signal, whatever weight it was given._\n")
    A(_fmt_table(bt.component_attribution(obs, 21)))

    A("## Composite score buckets (21 sessions)\n")
    A("_Bucket count adapts to the cross-section: quintiles need ~20 names per date, "
      "otherwise terciles. Forcing quintiles onto 11 sectors gives 2.2 per bucket, "
      "which is noise in a monotonic-looking table._\n")
    A(_fmt_table(bt.csri_buckets(obs, 21)))

    # ---------------- statistical rigour ----------------
    A("## Statistical rigour\n")

    d = verdict.get("distribution")
    if d and d.get("n"):
        A("### Distribution of forward returns\n")
        A(f"- n={d['n']}, mean **{d['mean']}%**, median **{d['median']}%** "
          f"(gap {d['mean_median_gap']})")
        A(f"- shape: **{d['shape']}**, **{d['tails']}** "
          f"(skew {d['skew']}, excess kurtosis {d['excess_kurtosis']})")
        A(f"- spread: sd {d['sd']}, IQR {d['iqr']}; "
          f"p5 {d['p5']} / p25 {d['p25']} / p75 {d['p75']} / p95 {d['p95']}")
        A(f"- {d['outliers_iqr_fence']} observations ({d['outlier_pct']}%) beyond the "
          f"IQR fence")
        A(f"- **mean is an appropriate summary: {d['mean_is_appropriate']}**"
          + ("  ← prefer the median; the mean is distorted by tail observations"
             if d.get("prefer_median") else ""))
        A("")

    ef = verdict.get("effect_size")
    if ef and ef.get("cohens_d") is not None:
        A("### Effect size — practical vs statistical significance\n")
        A(f"- raw difference **{ef['raw_difference']}%** "
          f"(breakout {ef['mean_a']}% vs flight {ef['mean_b']}%)")
        A(f"- Cohen's d **{ef['cohens_d']}**, Hedges' g {ef['hedges_g']} "
          f"→ **{ef['magnitude']}**")
        A(f"- P(random breakout beats random flight) = **{ef['prob_superiority']}**")
        A(f"- practically meaningful: **{ef['practically_meaningful']}**")
        A("\n_A p-value says 'probably not chance'. It says nothing about whether the "
          "difference is large enough to act on. Cross-sectional equity signals with "
          "|d| > 0.3 are rare and valuable._\n")

    rt = verdict.get("rank_test")
    if rt and rt.get("p_value") is not None:
        A("### Rank-based cross-check (no normality assumption)\n")
        A(f"- Mann-Whitney U p=**{rt['p_value']}**, AUC {rt['auc']}, "
          f"significant={rt['significant']}")
        A(f"- medians: breakout {rt['median_a']}% vs flight {rt['median_b']}%")
        A("")

    oi = verdict.get("outlier_influence")
    if oi and oi.get("raw_mean") is not None:
        A("### Outlier influence on the breakout mean\n")
        A(f"| raw | median | 10% trimmed | winsorised | excl. most extreme |")
        A(f"|---|---|---|---|---|")
        A(f"| {oi['raw_mean']}% | {oi['median']}% | {oi.get('trimmed_10pct')}% | "
          f"{oi['winsorised_mean']}% | {oi['mean_excl_most_extreme']}% |")
        A(f"\n{oi['verdict']} (shrinkage under trimming: "
          f"{oi.get('trim_shrinkage_pct')}%)\n")
        A("_Outliers are measured, not removed._\n")

    cv = verdict.get("cross_validation")
    if cv and cv.get("agree") is not None:
        A("### Cross-validation — same number, two ways\n")
        A(f"- group means: {cv['method_a_group_means']}%")
        A(f"- OLS dummy coefficient: {cv['method_b_ols_dummy']}%")
        A(f"- **{cv['verdict']}** (discrepancy {cv['absolute_discrepancy']})\n")

    sub = verdict.get("subperiods")
    if sub:
        A("### Subperiod stability\n")
        A("_A signal that only works in one stretch is a regime artifact._\n")
        A(_fmt_table(pd.DataFrame(sub)))

    ss = verdict.get("segment_stability")
    if ss and ss.get("stability"):
        A(f"**Segment stability: {ss['stability']}** — "
          f"{ss['segments_agreeing']}/{ss['segments_evaluated']} time blocks agree "
          f"with the aggregate spread of {ss['overall_spread']}%\n")

    hv = verdict.get("held_vs_onset")
    if hv:
        A("### Onset return vs held return\n")
        A("_Onset = forward return from the episode's first date (entry timing). "
          "Held = average across all observations in the phase (sustained state)._\n")
        A(_fmt_table(pd.DataFrame(hv)))

    pf = bt.portfolio_simulation(obs, 21, cost_bps=args.cost_bps, step=args.step) \
        if args.cost_bps != 5.0 else verdict.get("portfolio")
    if pf and "error" not in pf:
        A("### Portfolio simulation with turnover and costs\n")
        A(f"Long top bucket / short bottom bucket, {pf['names_per_side']} names per "
          f"side, {pf['rebalances']} rebalances, {pf['cost_bps_assumed']}bps round-trip.\n")
        A(f"| metric | gross | net |")
        A(f"|---|---|---|")
        A(f"| annualised return | {pf['gross_annualised_%']}% | "
          f"**{pf['net_annualised_%']}%** |")
        A(f"\n- volatility {pf['volatility_annualised_%']}%, Sharpe **{pf['sharpe']}**")
        A(f"- hit rate {pf['hit_rate_%']}%, max drawdown {pf['max_drawdown_%']}%")
        A(f"- mean turnover {pf['mean_turnover']} per rebalance → "
          f"cost drag {pf['cost_drag_annualised_%']}%/yr")
        A(f"- **survives costs: {pf['survives_costs']}**\n")
        A("_Episode statistics are not strategy P&L. This is the closest thing here "
          "to an implementable result, though it still ignores slippage, capacity, "
          "borrow cost and financing._\n")

    mt = verdict.get("multiple_testing")
    if mt and mt.get("results"):
        A("### Multiple-testing correction (Benjamini-Hochberg FDR)\n")
        A(f"{mt['n_tests']} simultaneous tests at alpha={mt['alpha']}. "
          f"Uncorrected, the family-wise error rate would be about "
          f"{(1 - (1 - mt['alpha']) ** mt['n_tests']) * 100:.0f}%.\n")
        rows = [{"test": k, "raw_p": v["raw_p"], "BH_p": v["bh_p"],
                 "significant": v["significant"]}
                for k, v in sorted(mt["results"].items(), key=lambda kv: kv[1]["raw_p"])]
        A(_fmt_table(pd.DataFrame(rows)))

    rf = verdict.get("red_flags")
    if rf:
        A("### Automated red-flag scan\n")
        A(_fmt_table(pd.DataFrame(rf)))

    conf = verdict.get("confidence")
    if conf:
        A(f"### Confidence assessment: **{conf['level']}**\n")
        if conf["blocking"]:
            A("**Blocking issues — resolve before acting on this:**\n")
            for x in conf["blocking"]:
                A(f"- {x}")
            A("")
        if conf["caveats"]:
            A("**Caveats that must travel with these numbers:**\n")
            for x in conf["caveats"]:
                A(f"- {x}")
            A("")

    A("## Phase transition matrix\n")
    A("_Row = current phase, column = next phase, in percent. Tests the mechanism: "
      "does Stealth Accumulation actually lead to Confirmed Breakout?_\n")
    tm = bt.transition_matrix(obs)
    A(_fmt_table(tm.reset_index() if not tm.empty else tm))

    if args.fit_weights:
        A("## Weight fitting\n")
        for h in horizons:
            print(f"  fitting weights for {h}d horizon...")
            fw = bt.fit_weights(obs, horizon=h)
            A(f"### {h}-session horizon\n")
            if "error" in fw:
                A(f"_{fw['error']}_\n")
                continue
            A(f"- Train: {fw['train_dates'][0]} → {fw['train_dates'][1]} "
              f"({fw['train_obs']:,} obs)")
            A(f"- Test: {fw['test_dates'][0]} → {fw['test_dates'][1]} "
              f"({fw['test_obs']:,} obs)\n")
            rr = []
            for name, r in fw["results"].items():
                if not r:
                    continue
                rr.append({"weighting": name, "train_IC": r["train_ic"],
                           "test_IC": r["test_ic"],
                           "weights": ", ".join(f"{k}={v}" for k, v in r["weights"].items()
                                                if v > 0.001)})
            A(_fmt_table(pd.DataFrame(rr)))
            A(f"**IC decay train→test**: {fw['ic_decay_train_to_test']:+.4f}\n")
            A(f"**Recommendation**: {fw['recommendation']}\n")
            A(f"_{fw['caveat']}_\n")

    A("## Methodology & limitations\n")
    A("- **Relative returns.** Sector minus benchmark. Absolute returns are "
      "dominated by market beta and would flatter every phase in an uptrend.")
    A("- **Episodes, not days.** Consecutive same-phase observations for one "
      "ticker collapse to a single observation taken at the episode's first date. "
      "Counting days would inflate n by roughly the mean episode length and make "
      "any significance test meaningless.")
    A("- **Block bootstrap.** Overlapping forward windows are autocorrelated, so "
      "p-values come from a stationary block bootstrap, not a naive t-test.")
    A("- **Point-in-time.** Metrics at each date are computed from truncated "
      "history. The indicator functions were verified free of lookahead: "
      "appending future data does not change any value at time t.")
    A("- **Survivorship bias in breadth (NOT corrected).** Constituent lists are "
      "current holdings applied to historical dates, so breadth is biased upward "
      "in the past. Rerun with `--no-breadth`; if conclusions change, the breadth "
      "result was an artifact.")
    A("- **No transaction costs, slippage or capacity assumptions.** These are "
      "signal-quality tests, not a tradeable strategy backtest.")
    A("- **Institutional footprint uses proxies here.** The walk-forward runs "
      "without tick data, so the 15% footprint component is the daily-bar proxy "
      "version. Backfilling Polygon flow history would let it be measured.")
    A("")

    rp = out / "backtest_report.md"
    rp.write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"VERDICT: {verdict['verdict']}  "
          f"({verdict['n_pass']} pass / {verdict['n_fail']} fail)")
    print("=" * 70)
    for c in verdict["checks"]:
        mark = "PASS" if c["pass"] else ("FAIL" if c["pass"] is False else "n/a ")
        print(f"  [{mark}] {c['check']}: {c['detail']}")
    if verdict["kill"]:
        print("\n  KILL CRITERIA TRIGGERED (system-level):")
        for k in verdict["kill"]:
            print(f"    - {k}")
    if verdict.get("underpowered"):
        print("\n  UNDERPOWERED (no conclusion drawn):")
        for k in verdict["underpowered"]:
            print(f"    - {k}")
    if verdict.get("signal_findings"):
        print("\n  SIGNAL-SCOPED FINDINGS (do not invalidate other signals):")
        for k in verdict["signal_findings"]:
            print(f"    - {k}")

    conf = verdict.get("confidence") or {}
    if conf:
        print(f"\n  CONFIDENCE: {conf.get('level')}  "
              f"({conf.get('n_high_flags')} high / {conf.get('n_medium_flags')} "
              f"medium red flags)")
    pf = verdict.get("portfolio") or {}
    if "error" not in pf and pf:
        print(f"  PORTFOLIO: net {pf['net_annualised_%']}%/yr, Sharpe {pf['sharpe']}, "
              f"turnover {pf['mean_turnover']}, survives costs: {pf['survives_costs']}")
    ef = verdict.get("effect_size") or {}
    if ef.get("cohens_d") is not None:
        print(f"  EFFECT SIZE: d={ef['cohens_d']} ({ef['magnitude']}), "
              f"P(sup)={ef['prob_superiority']}")

    print(f"\nReport: {rp}")
    print(f"Data:   {out/'backtest_episodes.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
