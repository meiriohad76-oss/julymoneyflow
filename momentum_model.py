#!/usr/bin/env python3
"""
Test simpler momentum models against the 5-component composite.

WHY
---
The walk-forward backtest found the composite has no stable edge (rank IC +0.012,
sign-flipping across subperiods and across tiers) while plain 12-1 momentum posted
IC +0.050, positive in all four subperiods and both tiers, strengthening as the
cross-section widened. This script asks whether a deliberately simpler model
outperforms the composite, and by enough to justify replacing it.

CONTAMINATION DISCLOSURE — read before believing any number below
-----------------------------------------------------------------
Momentum's edge was *observed* on the 2017-2026 window. Re-testing it on that same
window is not out-of-sample, and no amount of train/test splitting inside a window
I have already looked at fixes that. Three mitigations, none of them perfect:

  1. Every model is fixed-form with NO fitted parameters. There is nothing to
     overfit — the weights are 1.0 or equal, never optimised. This is the strongest
     protection available here.
  2. Horizons 5, 10, 42 and 63 sessions were not examined during discovery (only 21
     was), so results at those horizons are closer to genuinely fresh.
  3. A time-ordered holdout is still reported, and sign stability across four
     subperiods is required, so a result carried by one lucky stretch fails.

Treat a pass as *confirmatory*, not as discovery. Genuine out-of-sample validation
requires forward time that has not happened yet.

PRE-COMMITTED ACCEPTANCE CRITERIA (fixed before any result was seen)
--------------------------------------------------------------------
A challenger replaces the composite only if it clears ALL of:

  C1  Higher rank IC than CSRI at the 21-session horizon, on the holdout period.
  C2  Sign-stable: positive IC in all 4 subperiods (CSRI failed this).
  C3  Beats equal-weight-5-components, proving the gain comes from dropping
      components rather than from changing their weights.
  C4  Costed portfolio Sharpe > 0.30 at 5bps (CSRI achieved 0.02).
  C5  IC positive at a majority of the tested horizons, not just at 21.

Anything short of all five means "no change" — the composite stays, flawed, rather
than being swapped for something equally unproven.

    python momentum_model.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from smf import backtest as bt
from smf import config, providers

HORIZONS = (5, 10, 21, 42, 63)
HOLDOUT_FRAC = 0.30          # final 30% of dates, time-ordered
COST_BPS = 5.0

CRITERIA = {
    "C1_beats_csri_holdout_21d": None,
    "C2_sign_stable_4_of_4": None,
    "C3_beats_equal_weight_5": None,
    "C4_sharpe_above": 0.30,
    "C5_positive_majority_horizons": None,
}


# ---------------------------------------------------------------------------
def build_features(obs: pd.DataFrame) -> pd.DataFrame:
    """Add point-in-time momentum variants to the existing observation panel."""
    px = {}
    for t in obs["ticker"].unique():
        d = providers.history(t)
        if not d.empty:
            px[t] = d["close"]

    specs = {           # name -> (lookback sessions, skip sessions)
        "mom_12_1": (252, 21),
        "mom_6_1": (126, 21),
        "mom_3_1": (63, 21),
        "mom_12_0": (252, 0),
    }
    cols = {k: [] for k in specs}
    for _, r in obs.iterrows():
        s = px.get(r["ticker"])
        h = s[s.index <= r["date"]] if s is not None else None
        for name, (lb, sk) in specs.items():
            if h is None or len(h) < lb + sk + 2:
                cols[name].append(np.nan)
            else:
                cols[name].append(float(h.iloc[-1 - sk] / h.iloc[-1 - sk - lb] - 1) * 100)
    out = obs.copy()
    for k, v in cols.items():
        out[k] = v
    return out


def zscore_by_date(d: pd.DataFrame, col: str) -> pd.Series:
    """Cross-sectional z-score so components are comparable when combined."""
    g = d.groupby("date")[col]
    return (d[col] - g.transform("mean")) / g.transform("std").replace(0, np.nan)


MODELS = {
    # name: list of (column, weight). Weights are FIXED, never fitted.
    "CSRI (5-component, current)": [("csri", 1.0)],
    "equal-weight 5 components": [("z_mansfield_rs", .2), ("z_rs_momentum", .2),
                                  ("z_breadth", .2), ("z_money_flow", .2),
                                  ("z_inst_flow", .2)],
    "12-1 momentum only": [("_z_mom_12_1", 1.0)],
    "6-1 momentum only": [("_z_mom_6_1", 1.0)],
    "RS-Momentum only": [("z_rs_momentum", 1.0)],
    "Mansfield RS only": [("z_mansfield_rs", 1.0)],
    "12-1 mom + RS-Momentum": [("_z_mom_12_1", .5), ("z_rs_momentum", .5)],
    "12-1 mom + Mansfield": [("_z_mom_12_1", .5), ("z_mansfield_rs", .5)],
}


def score(d: pd.DataFrame, spec: list[tuple[str, float]]) -> pd.Series:
    """Weighted sum, renormalised per row over available components."""
    num = pd.Series(0.0, index=d.index)
    den = pd.Series(0.0, index=d.index)
    for col, w in spec:
        if col not in d.columns:
            continue
        v = d[col]
        ok = v.notna()
        num = num.add((v.fillna(0.0) * w).where(ok, 0.0), fill_value=0.0)
        den = den.add(pd.Series(np.where(ok, w, 0.0), index=d.index), fill_value=0.0)
    s = num / den.replace(0, np.nan)
    return s


def rank_ic(d: pd.DataFrame, score_col: str, fwd: str,
            min_names: int = 6) -> tuple[float, int]:
    """Mean per-date rank IC. See `rank_ic_stats` for significance."""
    ics = _ic_series(d, score_col, fwd, min_names)
    return (float(np.mean(ics)) if ics else np.nan), len(ics)


def _ic_series(d: pd.DataFrame, score_col: str, fwd: str,
               min_names: int = 6) -> list[float]:
    if score_col not in d.columns or fwd not in d.columns:
        return []
    ics = []
    for _, g in d.groupby("date"):
        g = g.dropna(subset=[score_col, fwd])
        if len(g) < min_names or g[score_col].nunique() < 3:
            continue
        v = g[score_col].corr(g[fwd], method="spearman")
        if np.isfinite(v):
            ics.append(float(v))
    return ics


def rank_ic_stats(d: pd.DataFrame, score_col: str, fwd: str,
                  min_names: int = 6) -> dict:
    """
    Rank IC WITH a significance test.

    The previous version reported a mean IC and a count and nothing else, so
    "12-1 momentum works, IC +0.050" was stated without any test. It does not
    survive one: t = 1.51, p = 0.13 on 109 dates — before any correction for the
    40 model x horizon combinations this script computes.
    """
    ics = _ic_series(d, score_col, fwd, min_names)
    n = len(ics)
    if n < 8:
        return {"ic": np.nan, "n": n, "t": None, "p": None, "ci95": None}
    a = np.asarray(ics)
    mean = float(a.mean())
    se = float(a.std(ddof=1) / np.sqrt(n))
    t = mean / se if se > 0 else np.nan
    try:
        from scipy import stats as _st
        p = float(2 * (1 - _st.t.cdf(abs(t), n - 1))) if np.isfinite(t) else np.nan
    except Exception:  # noqa: BLE001
        p = float(2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))) \
            if np.isfinite(t) else np.nan
    return {"ic": round(mean, 4), "n": n, "se": round(se, 4),
            "t": round(float(t), 2) if np.isfinite(t) else None,
            "p": round(p, 4) if np.isfinite(p) else None,
            "ci95": [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)],
            "significant": bool(np.isfinite(p) and p < 0.05)}


def costed_portfolio(d: pd.DataFrame, score_col: str, fwd: str,
                     step: int = 20, cost_bps: float = COST_BPS) -> dict:
    sub = d[["date", "ticker", score_col, fwd]].dropna()
    if len(sub) < 200:
        return {"sharpe": None, "net_ann": None}
    per_date = sub.groupby("date").size().median()
    n_side = max(1, int(per_date // 5))
    prev: set = set()
    recs = []
    for _, g in sub.groupby("date"):
        g = g.sort_values(score_col, ascending=False)
        L = set(g.head(n_side)["ticker"]); S = set(g.tail(n_side)["ticker"])
        if not L or not S:
            continue
        gross = float(g[g.ticker.isin(L)][fwd].mean()
                      - g[g.ticker.isin(S)][fwd].mean()) / 2.0
        cur = L | S
        to = (len(cur ^ prev) / max(len(cur | prev), 1)) if prev else 1.0
        recs.append(gross - to * cost_bps / 100.0)
        prev = cur
    if len(recs) < 10:
        return {"sharpe": None, "net_ann": None}
    r = np.array(recs)
    h = int(fwd.split("_")[1])
    per_yr = 252.0 / step
    net_ann = r.mean() * per_yr / (h / step)
    sd_ann = r.std(ddof=1) * np.sqrt(per_yr)
    return {"sharpe": round(float(net_ann / sd_ann), 3) if sd_ann > 0 else None,
            "net_ann": round(float(net_ann), 2),
            "rebalances": len(r)}


def main() -> int:
    p = Path(config.OUTPUT_DIR) / "backtest_observations.csv"
    if not p.exists():
        print(f"Missing {p}. Run the walk-forward backtest first.")
        return 1

    print("=" * 78)
    print("Simpler-Model Test — momentum variants vs the 5-component composite")
    print("=" * 78)
    print("\nCONTAMINATION: momentum's edge was observed on this window. Every model")
    print("below is fixed-form with no fitted parameters, so there is nothing to")
    print("overfit, but a pass here is CONFIRMATORY, not out-of-sample discovery.\n")

    obs = pd.read_csv(p, parse_dates=["date"])
    print(f"Panel: {len(obs):,} obs, {obs['ticker'].nunique()} tickers, "
          f"{obs['date'].nunique()} dates, "
          f"{obs['date'].min().date()} -> {obs['date'].max().date()}")

    print("Building momentum features...")
    d = build_features(obs)
    for c in ("mom_12_1", "mom_6_1", "mom_3_1", "mom_12_0"):
        d[f"_z_{c}"] = zscore_by_date(d, c)

    # forward returns for horizons not already present
    need = [h for h in HORIZONS if f"fwd_{h}" not in d.columns]
    if need:
        print(f"Computing forward returns for horizons {need}...")
        bench = providers.history(config.BENCHMARK)["close"]
        pxm = {t: providers.history(t)["close"] for t in d["ticker"].unique()}
        for h in need:
            vals = []
            for _, r in d.iterrows():
                c = pxm.get(r["ticker"])
                if c is None:
                    vals.append(np.nan); continue
                i = c.index.get_indexer([r["date"]], method="pad")[0]
                bi = bench.index.get_indexer([r["date"]], method="pad")[0]
                if i < 0 or bi < 0 or i + h >= len(c) or bi + h >= len(bench):
                    vals.append(np.nan); continue
                vals.append(((c.iloc[i+h]/c.iloc[i]-1) - (bench.iloc[bi+h]/bench.iloc[bi]-1))*100)
            d[f"fwd_{h}"] = vals

    for name, spec in MODELS.items():
        d[f"S::{name}"] = score(d, spec)

    dates = np.sort(d["date"].unique())
    cut = dates[int(len(dates) * (1 - HOLDOUT_FRAC))]
    train, hold = d[d["date"] < cut], d[d["date"] >= cut]
    print(f"\nTime-ordered split at {pd.Timestamp(cut).date()}: "
          f"train {len(train):,} obs / holdout {len(hold):,} obs")

    d["blk"] = pd.qcut(d["date"].rank(method="first"), 4, labels=False, duplicates="drop")

    rows = []
    for name in MODELS:
        sc = f"S::{name}"
        rec = {"model": name}
        for h in HORIZONS:
            ic, _ = rank_ic(d, sc, f"fwd_{h}")
            rec[f"IC_{h}d"] = round(ic, 4) if np.isfinite(ic) else None
        ic_tr, _ = rank_ic(train, sc, "fwd_21")
        ic_ho, _ = rank_ic(hold, sc, "fwd_21")
        rec["IC21_train"] = round(ic_tr, 4) if np.isfinite(ic_tr) else None
        rec["IC21_holdout"] = round(ic_ho, 4) if np.isfinite(ic_ho) else None
        sub = []
        for _, g in d.groupby("blk"):
            v, _ = rank_ic(g, sc, "fwd_21")
            sub.append(round(v, 3) if np.isfinite(v) else None)
        rec["IC21_by_period"] = sub
        ok = [x for x in sub if x is not None]
        rec["sign_stable"] = bool(len(ok) == len(sub) and all(x > 0 for x in ok))
        pf = costed_portfolio(d, sc, "fwd_21")
        rec["sharpe"] = pf["sharpe"]
        rec["net_ann_%"] = pf["net_ann"]
        hz = [rec[f"IC_{h}d"] for h in HORIZONS if rec[f"IC_{h}d"] is not None]
        rec["pos_horizons"] = f"{sum(1 for x in hz if x > 0)}/{len(hz)}"
        rows.append(rec)

    res = pd.DataFrame(rows)
    base = res[res["model"] == "CSRI (5-component, current)"].iloc[0]
    eq5 = res[res["model"] == "equal-weight 5 components"].iloc[0]

    print("\n" + "-" * 78)
    print("RANK IC BY HORIZON (whole sample)")
    print("-" * 78)
    print(res[["model"] + [f"IC_{h}d" for h in HORIZONS] + ["pos_horizons"]]
          .to_string(index=False))

    print("\n" + "-" * 78)
    print("HOLDOUT, STABILITY, COSTED PORTFOLIO")
    print("-" * 78)
    print(res[["model", "IC21_train", "IC21_holdout", "IC21_by_period",
               "sign_stable", "sharpe", "net_ann_%"]].to_string(index=False))

    print("\n" + "=" * 78)
    print("PRE-COMMITTED CRITERIA")
    print("=" * 78)
    print(f"  baseline CSRI: holdout IC21 {base['IC21_holdout']}, "
          f"Sharpe {base['sharpe']}, sign-stable {base['sign_stable']}")
    print(f"  equal-weight-5: holdout IC21 {eq5['IC21_holdout']}, "
          f"Sharpe {eq5['sharpe']}\n")

    winners = []
    for _, r in res.iterrows():
        if r["model"] in ("CSRI (5-component, current)", "equal-weight 5 components"):
            continue
        c1 = (r["IC21_holdout"] is not None and base["IC21_holdout"] is not None
              and r["IC21_holdout"] > base["IC21_holdout"])
        c2 = bool(r["sign_stable"])
        c3 = (r["IC21_holdout"] is not None and eq5["IC21_holdout"] is not None
              and r["IC21_holdout"] > eq5["IC21_holdout"])
        c4 = (r["sharpe"] is not None and r["sharpe"] > CRITERIA["C4_sharpe_above"])
        pos, tot = (int(x) for x in r["pos_horizons"].split("/"))
        c5 = pos > tot / 2
        allc = all([c1, c2, c3, c4, c5])
        mark = lambda b: "PASS" if b else "fail"  # noqa: E731
        print(f"  {r['model']}")
        print(f"    C1 beats CSRI on holdout : {mark(c1)}  ({r['IC21_holdout']} vs {base['IC21_holdout']})")
        print(f"    C2 sign-stable 4/4       : {mark(c2)}  {r['IC21_by_period']}")
        print(f"    C3 beats equal-weight-5  : {mark(c3)}")
        print(f"    C4 Sharpe > 0.30         : {mark(c4)}  ({r['sharpe']})")
        print(f"    C5 IC>0 majority horizons: {mark(c5)}  ({r['pos_horizons']})")
        print(f"    => {'ADOPT' if allc else 'reject'}\n")
        if allc:
            winners.append((r["model"], r["IC21_holdout"], r["sharpe"]))

    print("=" * 78)
    if winners:
        winners.sort(key=lambda x: -(x[1] or -9))
        print(f"RECOMMENDATION: adopt '{winners[0][0]}' "
              f"(holdout IC21 {winners[0][1]}, Sharpe {winners[0][2]})")
        if len(winners) > 1:
            print(f"  also cleared all criteria: "
                  f"{', '.join(w[0] for w in winners[1:])}")
        print("\n  Reminder: this is confirmatory on a window where momentum was")
        print("  already observed to work. Track live forward performance before")
        print("  treating it as validated.")
    else:
        print("RECOMMENDATION: no change. No challenger cleared all five criteria.")
        print("  The composite stays — not because it works, but because replacing")
        print("  it with something equally unproven is not an improvement.")

    out = Path(config.OUTPUT_DIR) / "momentum_model_test.csv"
    res.to_csv(out, index=False)
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
