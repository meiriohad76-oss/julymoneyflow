#!/usr/bin/env python3
"""
Tests for the statistical rigour layer.

Every function here gates a verdict, so each is driven with data whose correct
answer is known by construction. A diagnostic that silently returns the wrong
answer is worse than no diagnostic, because it manufactures confidence.

    python test_diagnostics.py
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from smf import backtest as bt
from smf import diagnostics as dg

PASS = FAIL = 0


def check(name: str, got, want=True, tol: float | None = None) -> None:
    global PASS, FAIL
    if tol is not None:
        ok = got is not None and abs(float(got) - float(want)) <= tol
    else:
        ok = got == want
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")


def main() -> int:
    rng = np.random.default_rng(42)
    print("Statistical rigour tests\n" + "=" * 66)

    # ---------------- describe ----------------
    print("\n[describe]")
    normal = rng.normal(0, 1, 3000)
    d = dg.describe(normal, "normal")
    check("normal: symmetric", d["shape"], "approximately symmetric")
    check("normal: near-normal tails", d["tails"], "near-normal tails")
    check("normal: mean appropriate", d["mean_is_appropriate"], True)
    check("normal: mean ~0", d["mean"], 0.0, tol=0.1)
    check("normal: median ~0", d["median"], 0.0, tol=0.1)

    heavy = rng.standard_t(2.0, 3000)
    d2 = dg.describe(heavy, "t(2)")
    check("t(2): heavy-tailed detected", d2["tails"], "heavy-tailed")
    check("t(2): prefers median", d2["prefer_median"], True)
    check("t(2): mean flagged inappropriate", d2["mean_is_appropriate"], False)

    skewed = rng.exponential(1.0, 3000)
    d3 = dg.describe(skewed, "exponential")
    check("exponential: right-skewed", d3["shape"], "right-skewed")
    check("exponential: mean > median", d3["mean"] > d3["median"], True)

    check("too few observations handled", "note" in dg.describe([1, 2]), True)

    # ---------------- effect size ----------------
    print("\n[effect_size]")
    a = rng.normal(1.0, 1.0, 500)
    b = rng.normal(0.0, 1.0, 500)
    e = dg.effect_size(a, b)
    check("d ~ 1.0 for 1-sd separation", e["cohens_d"], 1.0, tol=0.15)
    check("magnitude large", e["magnitude"], "large")
    check("P(superiority) ~0.76", e["prob_superiority"], 0.76, tol=0.05)
    check("practically meaningful", e["practically_meaningful"], True)

    e0 = dg.effect_size(rng.normal(0, 1, 500), rng.normal(0, 1, 500))
    check("identical distributions -> negligible", e0["magnitude"], "negligible")
    check("identical -> not meaningful", e0["practically_meaningful"], False)
    check("P(sup) ~0.5 for identical", e0["prob_superiority"], 0.5, tol=0.05)
    check("hedges g < cohens d (small-sample correction)",
          abs(dg.effect_size(a[:20], b[:20])["hedges_g"])
          < abs(dg.effect_size(a[:20], b[:20])["cohens_d"]), True)

    # ---------------- rank test ----------------
    print("\n[rank_test]")
    r = dg.rank_test(a, b)
    check("detects a real shift", r["significant"], True)
    check("AUC ~0.76", r["auc"], 0.76, tol=0.05)
    r0 = dg.rank_test(rng.normal(0, 1, 400), rng.normal(0, 1, 400))
    check("no shift -> not significant", r0["significant"], False)
    check("AUC ~0.5 under the null", r0["auc"], 0.5, tol=0.06)
    # rank test must be robust where the mean is not
    contaminated = np.concatenate([rng.normal(0, 1, 300), np.array([500.0] * 5)])
    clean = rng.normal(0, 1, 300)
    rc = dg.rank_test(contaminated, clean)
    check("rank test resists 5 extreme outliers", rc["significant"], False)

    # ---------------- outlier influence ----------------
    print("\n[outlier_influence]")
    base = rng.normal(0.0, 0.5, 200)
    spiked = np.concatenate([base, np.array([60.0, 55.0, 70.0])])
    oi = dg.outlier_influence(spiked)
    check("detects outlier-driven mean", oi["outlier_driven"], True)
    check("raw mean is positive", oi["raw_mean"] > 0.5, True)
    check("trimmed mean collapses toward zero",
          abs(oi["trimmed_10pct"]) < abs(oi["raw_mean"]) * 0.4, True)
    oi2 = dg.outlier_influence(rng.normal(1.0, 0.5, 300))
    check("clean data not flagged", oi2["outlier_driven"], False)
    check("clean data: trimmed ~ raw",
          abs(oi2["trimmed_10pct"] - oi2["raw_mean"]) < 0.1, True)

    # ---------------- segment stability / Simpson ----------------
    print("\n[segment_stability]")
    # Stable: spread positive in every block
    rows = []
    for blk in range(4):
        for _ in range(40):
            rows.append({"blk": blk, "ph": "CONFIRMED_BREAKOUT", "v": rng.normal(2, 1)})
            rows.append({"blk": blk, "ph": "CAPITAL_FLIGHT", "v": rng.normal(-2, 1)})
    st = dg.segment_stability(pd.DataFrame(rows), "v", "ph", "blk",
                              "CONFIRMED_BREAKOUT", "CAPITAL_FLIGHT")
    check("consistent signal -> stable", st["stability"], "stable")
    check("all 4 blocks agree", st["segments_agreeing"], 4)

    # Simpson's paradox: aggregate positive, most blocks negative
    rows = []
    for blk in range(4):
        # block 0 has a huge favourable imbalance that carries the aggregate
        if blk == 0:
            for _ in range(200):
                rows.append({"blk": blk, "ph": "CONFIRMED_BREAKOUT", "v": rng.normal(9, 1)})
            for _ in range(20):
                rows.append({"blk": blk, "ph": "CAPITAL_FLIGHT", "v": rng.normal(-9, 1)})
        else:
            for _ in range(30):
                rows.append({"blk": blk, "ph": "CONFIRMED_BREAKOUT", "v": rng.normal(-1, 1)})
                rows.append({"blk": blk, "ph": "CAPITAL_FLIGHT", "v": rng.normal(1, 1)})
    st2 = dg.segment_stability(pd.DataFrame(rows), "v", "ph", "blk",
                               "CONFIRMED_BREAKOUT", "CAPITAL_FLIGHT")
    check("aggregate spread positive", st2["overall_spread"] > 0, True)
    check("Simpson's paradox detected",
          st2["stability"].startswith("UNSTABLE"), True)
    check("only 1 of 4 blocks agrees", st2["segments_agreeing"], 1)

    # ---------------- cross validation ----------------
    print("\n[cross_validate_spread]")
    eps = pd.DataFrame({
        "phase": ["CONFIRMED_BREAKOUT"] * 60 + ["CAPITAL_FLIGHT"] * 60,
        "fwd_21": list(rng.normal(2, 1, 60)) + list(rng.normal(-1, 1, 60))})
    cvr = dg.cross_validate_spread(eps, 21)
    check("two methods agree", cvr["agree"], True)
    check("discrepancy is ~0", cvr["absolute_discrepancy"] < 1e-6, True)

    # ---------------- red flags ----------------
    print("\n[red_flags]")
    dates = pd.date_range("2024-01-01", periods=50, freq="W")
    obs = pd.DataFrame({
        "date": np.repeat(dates, 6),
        "ticker": np.tile([f"T{i}" for i in range(6)], 50),
        "phase": np.tile(["CONFIRMED_BREAKOUT", "CAPITAL_FLIGHT", "NEUTRAL"] * 2, 50),
        "fwd_21": rng.normal(0, 2, 300), "fwd_63": rng.normal(0, 3, 300)})
    e = bt.to_episodes(obs)
    fl = dg.red_flags(obs, e)
    check("clean data -> no flags", fl[0]["severity"], "none")

    # duplicates must be caught
    dup = pd.concat([obs, obs.iloc[:5]], ignore_index=True)
    fl2 = dg.red_flags(dup, bt.to_episodes(dup))
    check("duplicate (date,ticker) detected",
          any(f["check"] == "duplicate observations" for f in fl2), True)

    # constant component must be caught
    obs3 = obs.copy(); obs3["z_breadth"] = 1.0
    fl3 = dg.red_flags(obs3, bt.to_episodes(obs3))
    check("constant component detected",
          any(f["check"] == "constant component" for f in fl3), True)

    # phase concentration must be caught
    obs4 = obs.copy(); obs4["phase"] = "NEUTRAL"
    fl4 = dg.red_flags(obs4, bt.to_episodes(obs4))
    check("phase concentration detected",
          any(f["check"] in ("phase concentration", "few phases populated")
              for f in fl4), True)

    # ---------------- confidence assessment ----------------
    print("\n[confidence_assessment]")
    clean_v = {"verdict": "PASS", "kill": [], "signal_findings": []}
    c1 = dg.confidence_assessment(clean_v, [{"severity": "none", "check": "x",
                                             "detail": "y"}],
                                  {"prefer_median": False}, {"outlier_driven": False},
                                  {"stability": "stable"})
    check("clean -> ready to share", c1["level"], "Ready to share")

    c2 = dg.confidence_assessment(clean_v, [{"severity": "medium", "check": "m",
                                             "detail": "d"}],
                                  {"prefer_median": True, "shape": "right-skewed",
                                   "tails": "heavy-tailed", "skew": 3, "excess_kurtosis": 9},
                                  {"outlier_driven": False}, {"stability": "stable"})
    check("medium flags -> caveats", c2["level"], "Share with noted caveats")
    check("heavy tails produce a median caveat",
          any("median" in x for x in c2["caveats"]), True)

    c3 = dg.confidence_assessment(clean_v, [{"severity": "high", "check": "h",
                                             "detail": "bad"}],
                                  {"prefer_median": False}, {"outlier_driven": False},
                                  {"stability": "stable"})
    check("high flags -> needs revision", c3["level"], "Needs revision")

    c4 = dg.confidence_assessment(clean_v, [], {"prefer_median": False},
                                  {"outlier_driven": True, "verdict": "tail-driven"},
                                  {"stability": "stable"})
    check("outlier-driven blocks", c4["level"], "Needs revision")

    # ---------------- adaptive buckets ----------------
    print("\n[csri_buckets adapts to cross-section]")
    for n_names, want in ((29, 5), (11, 3), (5, 2)):
        dts = pd.date_range("2022-01-01", periods=120, freq="W")
        o = pd.DataFrame({
            "date": np.repeat(dts, n_names),
            "csri": rng.normal(0, 1, 120 * n_names),
            "fwd_21": rng.normal(0, 2, 120 * n_names)})
        tbl = bt.csri_buckets(o, 21)
        got = int(tbl["n_buckets"].iloc[0]) if not tbl.empty else None
        check(f"{n_names} names/date -> {want} buckets", got, want)

    # ---------------- IC renormalisation, not zero-fill ----------------
    print("\n[information_coefficient handles missingness]")
    dts = pd.date_range("2022-01-01", periods=200, freq="W")
    n = 10
    o = pd.DataFrame({"date": np.repeat(dts, n)})
    sig = rng.normal(0, 1, len(o))
    o["fwd_21"] = sig * 2 + rng.normal(0, 1, len(o))
    o["z_a"] = sig
    o["z_b"] = sig
    ic_full = bt.information_coefficient(o, {"a": 0.5, "b": 0.5}, 21)
    o2 = o.copy()
    o2.loc[o2.sample(frac=0.4, random_state=2).index, "z_b"] = np.nan
    ic_miss = bt.information_coefficient(o2, {"a": 0.5, "b": 0.5}, 21)
    check("IC positive with complete data", ic_full > 0.3, True)
    # With renormalisation, dropping half of a redundant component should barely
    # move the IC. Zero-filling would drag scores toward zero and degrade it.
    check("IC robust to 40% missingness (renormalised, not zero-filled)",
          abs(ic_full - ic_miss) < 0.10, True)

    print("\n" + "=" * 66)
    print(f"{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
