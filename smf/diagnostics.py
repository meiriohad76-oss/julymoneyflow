"""
Statistical rigour layer for the backtest.

The backtest answers "is the mean different from zero?". That is necessary and
nowhere near sufficient. This module adds the checks that decide whether the
answer means anything:

  * Distribution characterisation — if forward returns are heavy-tailed, the mean
    is the wrong summary statistic and reporting it alone misleads.
  * Effect size — statistical significance without practical significance is a
    trap, especially as sample size grows.
  * Outlier influence — a "+0.8% mean" produced by three extreme episodes is not
    a signal. Trimmed and winsorised means expose that.
  * Rank-based cross-check — Mann-Whitney does not assume normality, so agreement
    with the bootstrap raises confidence and disagreement localises the problem.
  * Segment stability (Simpson's paradox) — a conclusion that reverses inside
    subperiods or tiers is an artifact of the mix, not a finding.
  * Red-flag scan — exact round numbers, rates pinned at 0/100%, results that
    confirm the hypothesis too cleanly. Reality is messier than that.

scipy is used when present; every test has a numpy fallback so the module never
becomes a hard dependency.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

try:                                    # optional
    from scipy import stats as _st
    HAVE_SCIPY = True
except Exception:                       # noqa: BLE001
    _st = None
    HAVE_SCIPY = False


# ---------------------------------------------------------------------------
# Distribution characterisation
# ---------------------------------------------------------------------------
def describe(values, label: str = "") -> dict:
    """
    Full distribution profile, not just mean and n.

    Reports mean AND median together: when they diverge the data is skewed and the
    mean alone is misleading. Also reports the percentile spread, tail weight, and
    how many observations sit beyond the IQR fence — the facts needed to decide
    whether a mean-based test was appropriate in the first place.
    """
    v = np.asarray([x for x in np.asarray(values, dtype=float) if np.isfinite(x)])
    n = len(v)
    if n < 3:
        return {"label": label, "n": n, "note": "too few observations"}

    q = np.percentile(v, [1, 5, 25, 50, 75, 95, 99])
    iqr = q[4] - q[2]
    lo_f, hi_f = q[2] - 1.5 * iqr, q[4] + 1.5 * iqr
    n_out = int(((v < lo_f) | (v > hi_f)).sum())
    mean, med, sd = float(v.mean()), float(np.median(v)), float(v.std(ddof=1))

    if HAVE_SCIPY:
        skew = float(_st.skew(v))
        kurt = float(_st.kurtosis(v))          # excess kurtosis
        # Normality: Shapiro on small samples, D'Agostino otherwise
        try:
            if n <= 5000:
                _, p_norm = _st.shapiro(v)
            else:
                _, p_norm = _st.normaltest(v)
        except Exception:                     # noqa: BLE001
            p_norm = float("nan")
    else:
        m2 = ((v - mean) ** 2).mean()
        skew = float(((v - mean) ** 3).mean() / m2 ** 1.5) if m2 > 0 else float("nan")
        kurt = float(((v - mean) ** 4).mean() / m2 ** 2 - 3.0) if m2 > 0 else float("nan")
        p_norm = float("nan")

    if abs(skew) < 0.5:
        shape = "approximately symmetric"
    elif skew > 0:
        shape = "right-skewed"
    else:
        shape = "left-skewed"
    tails = ("heavy-tailed" if kurt > 1.0 else
             "light-tailed" if kurt < -1.0 else "near-normal tails")

    return {
        "label": label, "n": n,
        "mean": round(mean, 4), "median": round(med, 4),
        "mean_median_gap": round(mean - med, 4),
        "sd": round(sd, 4),
        "iqr": round(float(iqr), 4),
        "cv": round(sd / abs(mean), 3) if mean else None,
        "p1": round(float(q[0]), 3), "p5": round(float(q[1]), 3),
        "p25": round(float(q[2]), 3), "p50": round(float(q[3]), 3),
        "p75": round(float(q[4]), 3), "p95": round(float(q[5]), 3),
        "p99": round(float(q[6]), 3),
        "min": round(float(v.min()), 3), "max": round(float(v.max()), 3),
        "skew": round(skew, 3), "excess_kurtosis": round(kurt, 3),
        "shape": shape, "tails": tails,
        "outliers_iqr_fence": n_out,
        "outlier_pct": round(100.0 * n_out / n, 1),
        "normality_p": round(float(p_norm), 4) if np.isfinite(p_norm) else None,
        "mean_is_appropriate": bool(abs(skew) < 1.0 and kurt < 3.0),
        "prefer_median": bool(abs(skew) >= 1.0 or kurt >= 3.0),
    }


# ---------------------------------------------------------------------------
# Effect size — practical vs statistical significance
# ---------------------------------------------------------------------------
def effect_size(a, b) -> dict:
    """
    Cohen's d with Hedges' g small-sample correction, plus rank-based
    probability of superiority.

    A p-value says "probably not chance". It says nothing about whether the
    difference is big enough to act on. With a few hundred episodes a 0.1%
    edge can be significant and worthless. Effect size is the missing half.

    Interpretation (Cohen's conventions, which are generous for finance —
    cross-sectional equity signals with |d| > 0.3 are rare and valuable):
        |d| < 0.2  negligible
        0.2-0.5    small
        0.5-0.8    medium
        > 0.8      large
    """
    a = np.asarray([x for x in np.asarray(a, dtype=float) if np.isfinite(x)])
    b = np.asarray([x for x in np.asarray(b, dtype=float) if np.isfinite(x)])
    na, nb = len(a), len(b)
    if na < 5 or nb < 5:
        return {"n_a": na, "n_b": nb, "note": "too few observations"}

    ma, mb = a.mean(), b.mean()
    va, vb = a.var(ddof=1), b.var(ddof=1)
    pooled = math.sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)) if na + nb > 2 else np.nan
    d = float((ma - mb) / pooled) if pooled and np.isfinite(pooled) and pooled > 0 else float("nan")
    # Hedges' g correction for small samples
    J = 1.0 - (3.0 / (4.0 * (na + nb) - 9.0)) if (na + nb) > 3 else 1.0
    g = d * J if np.isfinite(d) else float("nan")

    # Probability of superiority: P(random a > random b). Non-parametric, and far
    # more intuitive for a decision-maker than a standardised mean difference.
    wins = 0
    if na * nb <= 4_000_000:
        wins = float((a[:, None] > b[None, :]).mean())
    else:
        rng = np.random.default_rng(0)
        ia = rng.integers(0, na, 200_000)
        ib = rng.integers(0, nb, 200_000)
        wins = float((a[ia] > b[ib]).mean())

    ad = abs(d) if np.isfinite(d) else 0.0
    mag = ("negligible" if ad < 0.2 else "small" if ad < 0.5
           else "medium" if ad < 0.8 else "large")
    return {
        "n_a": na, "n_b": nb,
        "mean_a": round(float(ma), 4), "mean_b": round(float(mb), 4),
        "raw_difference": round(float(ma - mb), 4),
        "cohens_d": round(d, 3) if np.isfinite(d) else None,
        "hedges_g": round(g, 3) if np.isfinite(g) else None,
        "magnitude": mag,
        "prob_superiority": round(wins, 3),
        "practically_meaningful": bool(ad >= 0.2),
    }


# ---------------------------------------------------------------------------
# Rank-based cross-check
# ---------------------------------------------------------------------------
def rank_test(a, b) -> dict:
    """
    Mann-Whitney U — does not assume normality.

    Run alongside the block bootstrap as a cross-check. Agreement between a
    parametric-ish and a rank-based test raises confidence; disagreement usually
    means outliers are driving the mean, which localises the problem instead of
    leaving it hidden.
    """
    a = np.asarray([x for x in np.asarray(a, dtype=float) if np.isfinite(x)])
    b = np.asarray([x for x in np.asarray(b, dtype=float) if np.isfinite(x)])
    if len(a) < 5 or len(b) < 5:
        return {"note": "too few observations", "n_a": len(a), "n_b": len(b)}

    if HAVE_SCIPY:
        u, p = _st.mannwhitneyu(a, b, alternative="two-sided")
        u = float(u)
    else:
        # Normal approximation with tie correction.
        comb = np.concatenate([a, b])
        order = comb.argsort()
        ranks = np.empty(len(comb), dtype=float)
        ranks[order] = np.arange(1, len(comb) + 1)
        # average ranks for ties
        s = pd.Series(comb)
        ranks = s.rank(method="average").to_numpy()
        r1 = ranks[: len(a)].sum()
        n1, n2 = len(a), len(b)
        u = r1 - n1 * (n1 + 1) / 2.0
        mu = n1 * n2 / 2.0
        _, counts = np.unique(comb, return_counts=True)
        tie = float(((counts ** 3 - counts).sum()))
        N = n1 + n2
        sd = math.sqrt((n1 * n2 / 12.0) * ((N + 1) - tie / (N * (N - 1))))
        z = (u - mu) / sd if sd > 0 else 0.0
        p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2))))

    n1, n2 = len(a), len(b)
    auc = u / (n1 * n2)          # = probability of superiority
    return {
        "test": "Mann-Whitney U",
        "n_a": n1, "n_b": n2,
        "u": round(u, 1),
        "p_value": round(float(p), 4),
        "auc": round(float(auc), 3),
        "significant": bool(p < 0.05),
        "median_a": round(float(np.median(a)), 4),
        "median_b": round(float(np.median(b)), 4),
        "scipy": HAVE_SCIPY,
    }


# ---------------------------------------------------------------------------
# Outlier influence
# ---------------------------------------------------------------------------
def outlier_influence(values, trim: float = 0.10) -> dict:
    """
    Is the mean an artifact of a handful of extreme observations?

    Reports the raw mean, the symmetrically trimmed mean, the winsorised mean, and
    the mean with the single most extreme value removed. If the sign flips or the
    magnitude collapses under trimming, the "edge" lives in the tail and should
    not be traded as if it were typical.

    Following the statistical-analysis guidance: outliers are NOT removed. They
    are measured, and their influence is reported.
    """
    v = np.asarray([x for x in np.asarray(values, dtype=float) if np.isfinite(x)])
    n = len(v)
    if n < 10:
        return {"n": n, "note": "too few observations"}

    raw = float(v.mean())
    k = max(1, int(n * trim))
    s = np.sort(v)
    trimmed = float(s[k:n - k].mean()) if n - 2 * k > 0 else float("nan")
    lo, hi = np.percentile(v, [trim * 100, 100 - trim * 100])
    wins = float(np.clip(v, lo, hi).mean())
    drop1 = float(np.delete(v, np.abs(v - raw).argmax()).mean())

    med = float(np.median(v))
    sd = float(v.std(ddof=1)) if n > 1 else float("nan")
    se = sd / math.sqrt(n) if np.isfinite(sd) and n > 0 else float("nan")
    shrink = (abs(raw - trimmed) / abs(raw)) if raw else float("nan")

    # The trigger MUST be scaled by dispersion, not by |raw_mean|.
    #
    # The previous version used `|raw - trimmed| / |raw| > 0.5` — a ratio whose
    # DENOMINATOR is the very quantity being tested for being near zero. It is
    # therefore unbounded whenever the mean is small, regardless of whether any
    # outlier exists. Monte Carlo on clean iid Gaussian data with NO outliers:
    #     n=50 -> flagged 27.5%   n=374 -> 29.0%   n=5000 -> 26.5%
    # It fired on ~27% of pure-null samples at EVERY sample size (it does not
    # converge), and it fired LESS as a real effect grew (0.0% at a true mean of
    # 1.0%). It was an inverted restatement of "the mean is small", reported as a
    # data-quality verdict — and it was the sole KILL that fired on the composite
    # backtest, overriding an otherwise UNDERPOWERED result.
    #
    # The replacement asks the actual question: is the gap between the raw and
    # trimmed mean large relative to the sampling noise of the mean itself?
    gap = abs(raw - trimmed) if np.isfinite(trimmed) else float("nan")
    driven = bool(np.isfinite(gap) and np.isfinite(se) and se > 0
                  and gap > 1.0 * se)
    flip = bool(np.isfinite(trimmed) and np.sign(trimmed) != np.sign(raw)
                and abs(raw) > se)          # a sign flip only counts if raw != ~0

    return {
        "n": n,
        "raw_mean": round(raw, 4),
        "median": round(med, 4),
        f"trimmed_{int(trim*100)}pct": round(trimmed, 4) if np.isfinite(trimmed) else None,
        "winsorised_mean": round(wins, 4),
        "mean_excl_most_extreme": round(drop1, 4),
        "raw_minus_trimmed": round(gap, 4) if np.isfinite(gap) else None,
        "standard_error": round(se, 4) if np.isfinite(se) else None,
        "gap_in_se_units": round(gap / se, 2) if np.isfinite(gap) and se else None,
        "trim_shrinkage_pct": round(100 * shrink, 1) if np.isfinite(shrink) else None,
        "sign_flips_under_trim": flip,
        "outlier_driven": driven,
        "verdict": ("trimming moves the mean by more than one standard error — "
                    "tail observations are material" if driven
                    else "mean is robust to trimming (gap is within sampling noise)"),
        "caveat_only": True,   # never a KILL: this is a caveat, not an invalidation
    }


# ---------------------------------------------------------------------------
# Segment stability / Simpson's paradox
# ---------------------------------------------------------------------------
def segment_stability(df: pd.DataFrame, value_col: str, group_col: str,
                      segment_col: str, positive_group: str,
                      negative_group: str, min_n: int = 15) -> dict:
    """
    Does the headline conclusion survive segmentation?

    Computes the positive-minus-negative spread overall, then within each segment.
    A spread that is positive overall but negative in most segments is Simpson's
    paradox: the aggregate is being driven by segment mix, not by the signal.

    The statistical-analysis guidance is explicit that this must be checked rather
    than assumed, because the aggregate trend can reverse under segmentation.
    """
    def _spread(frame: pd.DataFrame) -> tuple[float, int, int]:
        a = frame[frame[group_col] == positive_group][value_col].dropna()
        b = frame[frame[group_col] == negative_group][value_col].dropna()
        if len(a) < min_n or len(b) < min_n:
            return float("nan"), len(a), len(b)
        return float(a.mean() - b.mean()), len(a), len(b)

    overall, na, nb = _spread(df)
    rows = []
    for seg, g in df.groupby(segment_col, sort=True):
        sp, ka, kb = _spread(g)
        rows.append({"segment": str(seg), "spread": round(sp, 3) if np.isfinite(sp) else None,
                     "n_pos": ka, "n_neg": kb,
                     "same_sign_as_overall": (bool(np.sign(sp) == np.sign(overall))
                                              if np.isfinite(sp) and np.isfinite(overall)
                                              else None)})
    valid = [r for r in rows if r["spread"] is not None]
    agree = [r for r in valid if r["same_sign_as_overall"]]
    frac = len(agree) / len(valid) if valid else float("nan")

    if not valid or not np.isfinite(overall):
        stability = "unknown"
    elif frac >= 0.8:
        stability = "stable"
    elif frac >= 0.5:
        stability = "mixed"
    else:
        stability = "UNSTABLE — likely Simpson's paradox"

    return {
        "overall_spread": round(overall, 3) if np.isfinite(overall) else None,
        "n_pos": na, "n_neg": nb,
        "segments": rows,
        "segments_evaluated": len(valid),
        "segments_agreeing": len(agree),
        "agreement_fraction": round(frac, 2) if np.isfinite(frac) else None,
        "stability": stability,
    }


# ---------------------------------------------------------------------------
# Red-flag scan (from the validate-data QA checklist)
# ---------------------------------------------------------------------------
def red_flags(obs: pd.DataFrame, eps: pd.DataFrame, horizons=(21, 63)) -> list[dict]:
    """
    Automated pass over the pre-delivery QA checklist.

    Looks for the specific patterns that indicate a data or logic problem rather
    than a finding: rates pinned at exactly 0 or 100%, suspiciously round numbers,
    identical values across groups (a dimension being ignored), incomplete forward
    windows, and results that confirm the hypothesis too cleanly.
    """
    out: list[dict] = []

    def flag(sev: str, what: str, detail: str) -> None:
        out.append({"severity": sev, "check": what, "detail": detail})

    if obs is None or obs.empty:
        flag("high", "empty observations", "no rows produced")
        return out

    # --- completeness of forward windows ---
    for h in horizons:
        c = f"fwd_{h}"
        if c not in obs.columns:
            flag("medium", f"missing {c}", "horizon not computed")
            continue
        miss = obs[c].isna().mean()
        if miss > 0.02:
            flag("medium", f"incomplete {c}",
                 f"{miss:.1%} of observations lack a {h}-session forward return "
                 f"(partial periods at the end of the sample)")

    # --- hit rates pinned at the boundary ---
    for h in horizons:
        c = f"fwd_{h}"
        if c not in eps.columns:
            continue
        for ph, g in eps.groupby("phase"):
            s = g[c].dropna()
            if len(s) < 10:
                continue
            hit = (s > 0).mean()
            if hit in (0.0, 1.0):
                flag("high", "hit rate at boundary",
                     f"{ph} {h}d hit rate is exactly {hit:.0%} on n={len(s)} — "
                     f"suggests a filter or logic error, not a signal")

    # --- identical values across phases (a dimension being ignored) ---
    for h in horizons:
        c = f"fwd_{h}"
        if c not in eps.columns:
            continue
        means = eps.groupby("phase")[c].mean().dropna()
        if len(means) >= 3 and means.round(6).nunique() == 1:
            flag("high", "identical phase means",
                 f"all phases have the same mean {h}d return — the phase "
                 f"dimension is being ignored somewhere")

    # --- zero-variance components ---
    for c in [c for c in obs.columns if c.startswith("z_")]:
        s = obs[c].dropna()
        if len(s) > 50 and s.nunique() <= 1:
            flag("high", "constant component", f"{c} has no variation — not computed")
        elif len(s) > 50 and s.std() < 1e-6:
            flag("medium", "near-constant component", f"{c} sd={s.std():.2e}")

    # --- suspicious phase concentration ---
    mix = eps["phase"].value_counts(normalize=True) if "phase" in eps.columns else pd.Series()
    if len(mix) and mix.iloc[0] > 0.85:
        flag("medium", "phase concentration",
             f"{mix.index[0]} is {mix.iloc[0]:.0%} of all episodes — the classifier "
             f"is barely discriminating")
    if len(mix) and len(mix) < 3:
        flag("medium", "few phases populated",
             f"only {len(mix)} phases ever fired: {list(mix.index)}")

    # --- results too clean ---
    for h in horizons:
        c = f"fwd_{h}"
        if c not in eps.columns:
            continue
        ord_means = [eps[eps["phase"] == p][c].mean()
                     for p in ("CONFIRMED_BREAKOUT", "STEALTH_ACCUMULATION",
                               "NEUTRAL", "DISTRIBUTION", "CAPITAL_FLIGHT")
                     if p in set(eps["phase"])]
        ord_means = [m for m in ord_means if np.isfinite(m)]
        if len(ord_means) >= 4:
            strictly = all(ord_means[i] > ord_means[i + 1] for i in range(len(ord_means) - 1))
            gaps = np.diff(ord_means)
            if strictly and len(gaps) > 2 and np.std(gaps) / (abs(np.mean(gaps)) + 1e-9) < 0.10:
                flag("medium", "results suspiciously clean",
                     f"{h}d phase means are perfectly ordered with near-uniform gaps — "
                     f"real data is messier; check for leakage")

    # --- duplicate observations (double counting) ---
    if {"date", "ticker"}.issubset(obs.columns):
        dup = int(obs.duplicated(subset=["date", "ticker"]).sum())
        if dup:
            flag("high", "duplicate observations",
                 f"{dup} duplicate (date, ticker) rows — double counting")

    # --- episode length sanity ---
    if "length_obs" in eps.columns and len(eps):
        if eps["length_obs"].median() <= 1:
            flag("medium", "episode collapsing ineffective",
                 "median episode length is 1 observation — sampling step is coarser "
                 "than typical phase persistence, so episode collapsing did no work")

    if not out:
        out.append({"severity": "none", "check": "red-flag scan",
                    "detail": "no automated red flags detected"})
    return out


# ---------------------------------------------------------------------------
# Cross-validation: same number, two ways
# ---------------------------------------------------------------------------
def cross_validate_spread(eps: pd.DataFrame, horizon: int = 21) -> dict:
    """
    Compute the headline spread two independent ways and confirm they agree.

    Method A: difference of group means.
    Method B: OLS coefficient on a phase dummy (algebraically identical for two
              groups, so any disagreement means a data-handling bug upstream).

    The validate-data checklist calls for calculating key metrics two ways; this
    is that check, automated.
    """
    col = f"fwd_{horizon}"
    if col not in eps.columns:
        return {"note": f"no {col}"}
    a = eps[eps["phase"] == "CONFIRMED_BREAKOUT"][col].dropna()
    b = eps[eps["phase"] == "CAPITAL_FLIGHT"][col].dropna()
    if len(a) < 5 or len(b) < 5:
        return {"note": "insufficient episodes"}

    method_a = float(a.mean() - b.mean())

    y = np.concatenate([a.to_numpy(), b.to_numpy()])
    x = np.concatenate([np.ones(len(a)), np.zeros(len(b))])
    X = np.column_stack([np.ones(len(y)), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    method_b = float(beta[1])

    diff = abs(method_a - method_b)
    return {
        "horizon": horizon,
        "method_a_group_means": round(method_a, 6),
        "method_b_ols_dummy": round(method_b, 6),
        "absolute_discrepancy": round(diff, 9),
        "agree": bool(diff < 1e-6),
        "verdict": ("both methods agree" if diff < 1e-6
                    else "DISCREPANCY — investigate data handling"),
    }


# ---------------------------------------------------------------------------
# Confidence assessment (validate-data 3-level scale)
# ---------------------------------------------------------------------------
def confidence_assessment(verdict: dict, flags: list[dict],
                          dist: dict | None, infl: dict | None,
                          stability: dict | None) -> dict:
    """
    Roll everything into the three-level scale from the validate-data skill, with
    the caveats that must travel with the numbers.
    """
    blocking: list[str] = []
    caveats: list[str] = []

    high = [f for f in flags if f.get("severity") == "high"]
    med = [f for f in flags if f.get("severity") == "medium"]
    for f in high:
        blocking.append(f"{f['check']}: {f['detail']}")
    for f in med:
        caveats.append(f"{f['check']}: {f['detail']}")

    v = verdict.get("verdict")
    if v == "UNDERPOWERED":
        caveats.append("Sample is underpowered — no conclusion about edge in either "
                       "direction is supported.")
    if verdict.get("kill"):
        caveats.extend(verdict["kill"])
    if verdict.get("signal_findings"):
        caveats.extend(verdict["signal_findings"])

    if dist and dist.get("prefer_median"):
        caveats.append(
            f"Forward returns are {dist.get('shape')} and {dist.get('tails')} "
            f"(skew {dist.get('skew')}, excess kurtosis {dist.get('excess_kurtosis')}) — "
            f"median is the more honest summary than mean.")
    if infl and infl.get("outlier_driven"):
        blocking.append(f"Outlier influence: {infl.get('verdict')}")
    if stability and str(stability.get("stability", "")).startswith("UNSTABLE"):
        blocking.append(
            f"Conclusion reverses across segments (only "
            f"{stability.get('segments_agreeing')}/{stability.get('segments_evaluated')} "
            f"agree) — likely Simpson's paradox.")
    elif stability and stability.get("stability") == "mixed":
        caveats.append("Conclusion holds in only about half of segments — treat as fragile.")

    if blocking:
        level = "Needs revision"
    elif caveats:
        level = "Share with noted caveats"
    else:
        level = "Ready to share"

    return {"level": level, "blocking": blocking, "caveats": caveats,
            "n_high_flags": len(high), "n_medium_flags": len(med)}
