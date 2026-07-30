"""
Walk-forward backtest of the phase classification and composite score.

The question this answers: do the phase labels predict forward *relative*
returns, and which components carry the signal?

Methodology commitments, and why each matters
---------------------------------------------
Relative, not absolute returns
    Forward return is measured as sector minus benchmark. Absolute returns are
    dominated by market beta and would make every phase look good in an uptrend.

Point-in-time metrics
    At each rebalance date the metrics are recomputed from data truncated at that
    date. The indicator functions were verified free of lookahead (appending
    future data does not change any value at time t), so truncation is sufficient.

Episodes, not sector-days
    Thirty consecutive days of "Confirmed Breakout" in XLE is ONE observation, not
    thirty. Counting days would inflate n by ~20x and produce meaningless
    significance. Every statistic here is computed on episodes: a maximal run of
    consecutive observations of the same phase for the same ticker. The forward
    return is taken from the episode's FIRST date, which is also the only date on
    which the signal was actionable new information.

Block bootstrap
    Overlapping forward windows are heavily autocorrelated. Significance comes
    from a stationary block bootstrap over time, not a naive t-test.

Known bias that is not corrected
    Constituent lists are current holdings applied to historical dates, so
    breadth is survivorship-biased upward in the past. This contaminates the 20%
    breadth component. `--no-breadth` reruns without it as a robustness check;
    if conclusions flip, the breadth result was an artifact.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import config, metrics, providers, scoring

PHASES = ["CONFIRMED_BREAKOUT", "STEALTH_ACCUMULATION", "NEUTRAL",
          "DISTRIBUTION", "CAPITAL_FLIGHT"]

# Expected ordering of mean forward relative return, best to worst. The
# monotonicity test checks the realised ordering against this.
EXPECTED_ORDER = PHASES


@dataclass
class Observation:
    date: pd.Timestamp
    ticker: str
    tier: int
    phase: str
    csri: float | None
    components: dict = field(default_factory=dict)
    fwd: dict = field(default_factory=dict)      # horizon -> relative return %
    extra: dict = field(default_factory=dict)    # additional signals to evaluate


# ---------------------------------------------------------------------------
# Walk-forward engine
# ---------------------------------------------------------------------------
def run_walk_forward(tickers: list[str] | None = None,
                     horizons: tuple[int, ...] = (21, 63),
                     step: int = 5,
                     warmup: int = 260,
                     skip_breadth: bool = False,
                     quiet: bool = False,
                     date_slice: tuple[int, int] | None = None) -> pd.DataFrame:
    """
    Step through history, classify every ticker at each date, and attach forward
    relative returns.

    `step=5` samples weekly rather than daily. Daily sampling multiplies compute
    5x while adding almost no independent information, because the underlying
    metrics use 21-200 day windows.
    """
    tickers = tickers or config.ALL_TICKERS

    def log(m: str) -> None:
        if not quiet:
            print(m)

    log("Loading price history...")
    bench = providers.history(config.BENCHMARK)
    if bench.empty or len(bench) < warmup + max(horizons) + 60:
        raise RuntimeError("Not enough benchmark history to backtest.")

    etf = {t: df for t in tickers if not (df := providers.history(t)).empty}
    log(f"  {len(etf)}/{len(tickers)} ETFs, benchmark {len(bench)} sessions")

    con_px: dict[str, pd.DataFrame] = {}
    con_map: dict[str, list[str]] = {}
    if not skip_breadth:
        for t in etf:
            con_map[t] = providers.etf_constituents(t)
        flat = sorted({c for v in con_map.values() for c in v})
        for c in flat:
            df = providers.history(c)
            if not df.empty:
                con_px[c] = df
        log(f"  {len(con_px)}/{len(flat)} constituents for breadth")

    # Short interest, fetched once per ticker. `compute_sector_metrics` applies the
    # publication lag per date, so a single full-history frame is point-in-time safe.
    si_map: dict[str, pd.DataFrame] = {}
    if config.POLYGON_API_KEY:
        for t in etf:
            d = providers.short_interest(t)
            if not d.empty:
                si_map[t] = d
        log(f"  {len(si_map)}/{len(etf)} ETFs with short-interest history")

    # Common calendar: dates present in the benchmark, after warmup, leaving
    # room for the longest forward horizon.
    cal = bench.index
    start_i = warmup
    end_i = len(cal) - max(horizons) - 1
    dates = cal[start_i:end_i:step]
    if date_slice is not None:
        # Chunking by DATE is the only safe decomposition. Cross-sectional RRG
        # normalisation and the institutional-flow percentile are computed across
        # tickers WITHIN a date, so splitting the ticker list changes the peer
        # group and therefore changes the signal. Splitting dates does not.
        lo, hi = date_slice
        dates = dates[lo:hi]
    log(f"  {len(dates)} rebalance dates: {dates[0].date()} -> {dates[-1].date()}"
        f" (step={step} sessions)")

    bench_close = bench["close"]
    rows: list[Observation] = []

    for n, d in enumerate(dates, 1):
        if not quiet and (n % 10 == 0 or n == len(dates)):
            pct = int(28 * n / len(dates))
            print(f"\r  [{'█'*pct}{'·'*(28-pct)}] {n}/{len(dates)} "
                  f"{d.date()}  obs={len(rows)}   ", end="", flush=True)

        b_hist = bench[bench.index <= d]
        snap: list[dict] = []
        for t, df in etf.items():
            hist = df[df.index <= d]
            if len(hist) < warmup:
                continue
            closes = {c: con_px[c]["close"][con_px[c].index <= d]
                      for c in con_map.get(t, []) if c in con_px}
            snap.append(metrics.compute_sector_metrics(t, hist, b_hist, closes,
                                                       None, si_map.get(t)))

        if len(snap) < 5:
            continue
        snap = metrics.finalise_rrg(snap)
        snap = [scoring.score_sector(s) for s in snap]
        snap = scoring.classify_all(snap)

        for s in snap:
            t = s["ticker"]
            fwd = {}
            for h in horizons:
                try:
                    i = etf[t].index.get_indexer([d], method="pad")[0]
                    j = i + h
                    bi = bench.index.get_indexer([d], method="pad")[0]
                    bj = bi + h
                    if j >= len(etf[t]) or bj >= len(bench):
                        continue
                    sr = float(etf[t]["close"].iloc[j] / etf[t]["close"].iloc[i] - 1)
                    br = float(bench_close.iloc[bj] / bench_close.iloc[bi] - 1)
                    fwd[h] = (sr - br) * 100.0
                except Exception:  # noqa: BLE001
                    continue
            if not fwd:
                continue
            obs_rec = Observation(
                date=d, ticker=t, tier=s["tier"], phase=s["phase"],
                csri=s.get("csri"),
                components={k: v for k, v in (s.get("components") or {}).items()},
                fwd=fwd,
            )
            # Extra signals to evaluate alongside the composite. Captured here so
            # they can be tested with the same machinery rather than asserted.
            obs_rec.extra = {
                "green_lights": s.get("green_lights"),
                "all_green": int(bool(s.get("all_green"))),
                "setup_count": s.get("setup_count"),
                "vms": s.get("vms"),
                "mom_12_1": s.get("mom_12_1"),
                "mansfield_rs": s.get("mansfield_rs"),
                "rs_momentum": s.get("rs_momentum"),
                "breadth": s.get("breadth"),
                "stage": s.get("stage"),
                "days_to_cover": s.get("days_to_cover"),
                "dtc_percentile": s.get("dtc_percentile"),
                "crowded_short": int(bool(s.get("crowded_short"))),
                "divergence": int(bool(s.get("divergence"))),
                "divergence_gap_pct": s.get("divergence_gap_pct"),
                "squeeze_score": s.get("squeeze_score"),
                "squeeze_setup": int(bool(s.get("squeeze_setup"))),
            }
            rows.append(obs_rec)
    if not quiet:
        print()

    recs = []
    for o in rows:
        r = {"date": o.date, "ticker": o.ticker, "tier": o.tier,
             "phase": o.phase, "csri": o.csri}
        for h, v in o.fwd.items():
            r[f"fwd_{h}"] = v
        for k, v in o.components.items():
            r[f"z_{k}"] = v
        for k, v in (o.extra or {}).items():
            r[k] = v
        recs.append(r)
    df = pd.DataFrame(recs).sort_values(["ticker", "date"]).reset_index(drop=True)
    log(f"  {len(df)} sector-date observations")
    return df


# ---------------------------------------------------------------------------
# Episode collapsing
# ---------------------------------------------------------------------------
def to_episodes(obs: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse consecutive same-phase observations per ticker into single episodes.

    This is the single most important step for honest statistics. Without it, a
    two-month breakout in one sector contributes ~9 correlated observations and
    the apparent sample size is a fiction.
    """
    if obs.empty:
        return obs
    out = []
    for tkr, grp in obs.groupby("ticker", sort=False):
        grp = grp.sort_values("date")
        run_id = (grp["phase"] != grp["phase"].shift()).cumsum()
        for _, ep in grp.groupby(run_id):
            first = ep.iloc[0]
            # `tier` and `csri` are optional: callers testing the statistics layer
            # supply minimal frames, and requiring them turned a missing column
            # into a KeyError deep inside the grouping loop.
            rec = {"ticker": tkr, "tier": first.get("tier", 1),
                   "phase": first["phase"],
                   "start": first["date"], "end": ep.iloc[-1]["date"],
                   "length_obs": len(ep), "csri": first.get("csri", np.nan)}
            for c in ep.columns:
                if c.startswith("fwd_") or c.startswith("z_"):
                    rec[c] = first[c]
            out.append(rec)
    return pd.DataFrame(out).sort_values("start").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def block_bootstrap_mean(values: np.ndarray, dates: np.ndarray | None = None,
                         n_boot: int = 5000, block: int = 8,
                         seed: int = 42) -> tuple[float, float, tuple[float, float]]:
    """
    Stationary block bootstrap of the mean.

    Returns (mean, two-sided p-value against zero, 95% CI). Resampling in blocks
    preserves the autocorrelation that overlapping forward windows introduce; an
    iid bootstrap or a naive t-test would understate the standard error badly.
    """
    v = np.asarray([x for x in values if np.isfinite(x)], dtype=float)
    n = len(v)
    if n < 8:
        return (float(v.mean()) if n else float("nan")), float("nan"), (float("nan"),) * 2

    rng = random.Random(seed)
    obs_mean = float(v.mean())
    means = np.empty(n_boot)
    n_blocks = max(1, math.ceil(n / block))
    for i in range(n_boot):
        idx = []
        for _ in range(n_blocks):
            s = rng.randrange(n)
            idx.extend([(s + k) % n for k in range(block)])
        means[i] = v[np.asarray(idx[:n])].mean()

    centred = means - means.mean()
    p = float((np.abs(centred) >= abs(obs_mean)).mean())
    lo, hi = np.percentile(means, [2.5, 97.5])
    return obs_mean, p, (float(lo), float(hi))


def phase_table(eps: pd.DataFrame, horizon: int = 21,
                tier: int | None = None, step: int = 20) -> pd.DataFrame:
    """Per-phase forward relative return statistics on episodes."""
    col = f"fwd_{horizon}"
    d = eps if tier is None else eps[eps["tier"] == tier]
    if col not in d.columns:
        return pd.DataFrame()

    rows = []
    for ph in PHASES:
        sub = d[d["phase"] == ph][col].dropna()
        if sub.empty:
            rows.append({"phase": ph, "episodes": 0})
            continue
        # Block MUST scale with the overlap ratio. A fixed block=8 gave a 13.2%
        # false-positive rate at the 63-session horizon (nominal 5%), because 8
        # observations span fewer sessions than the forward window itself.
        blk = max(8, 2 * math.ceil(horizon / max(step, 1)))
        mean, p, (lo, hi) = block_bootstrap_mean(sub.to_numpy(), block=blk)
        rows.append({
            "phase": ph,
            "episodes": int(len(sub)),
            "mean_rel_%": round(mean, 3),
            "median_%": round(float(sub.median()), 3),
            "hit_rate_%": round(float((sub > 0).mean() * 100), 1),
            "p_value": round(p, 4) if np.isfinite(p) else None,
            "ci95_lo": round(lo, 3) if np.isfinite(lo) else None,
            "ci95_hi": round(hi, 3) if np.isfinite(hi) else None,
        })
    return pd.DataFrame(rows)


def spread_test(eps: pd.DataFrame, horizon: int = 21,
                long_phase: str = "CONFIRMED_BREAKOUT",
                short_phase: str = "CAPITAL_FLIGHT",
                block: int | None = None, step: int = 5) -> dict:
    """
    The headline test: does long-the-best minus short-the-worst make money?

    TWO-SIDED, deliberately. An earlier version computed
    `p = (diffs <= 0).mean() * 2`, which returns p≈1.0 whenever the observed
    spread is negative — so a signal working reliably *backwards* was reported as
    "not significant" and discarded. Verified on synthetic data: a true spread of
    −4.0% reported p=1.0.

    That is the most expensive kind of bug in a research tool, because a reliably
    inverted signal is a real and tradeable finding. The verdict field now
    distinguishes three outcomes that the old design collapsed into one:

        POSITIVE    significant, right sign
        INVERTED    significant, wrong sign — tradeable in reverse
        NO_EDGE     tight confidence interval around zero
        UNDERPOWERED  cannot distinguish; do not conclude anything
    """
    col = f"fwd_{horizon}"
    if eps is None or eps.empty or col not in eps.columns:
        return {"horizon": horizon, "n_long": 0, "n_short": 0,
                "spread_%": None, "p_value": None, "verdict": "UNDERPOWERED",
                "note": f"no {col} column"}
    a = eps[eps["phase"] == long_phase][col].dropna().to_numpy()
    b = eps[eps["phase"] == short_phase][col].dropna().to_numpy()
    if len(a) < 8 or len(b) < 8:
        return {"horizon": horizon, "n_long": len(a), "n_short": len(b),
                "spread_%": None, "p_value": None, "verdict": "UNDERPOWERED",
                "note": "insufficient episodes"}

    if block is None:
        block = max(8, 2 * math.ceil(horizon / max(step, 1)))

    ma, _, _ = block_bootstrap_mean(a, block=block, seed=1)
    mb, _, _ = block_bootstrap_mean(b, block=block, seed=2)
    obs = ma - mb

    # Block-resample each leg, preserving within-leg autocorrelation.
    rng = random.Random(99)
    n_boot = 4000
    diffs = np.empty(n_boot)

    def _blk(arr: np.ndarray) -> np.ndarray:
        n = len(arr)
        idx: list[int] = []
        for _ in range(max(1, math.ceil(n / block))):
            s = rng.randrange(n)
            idx.extend([(s + k) % n for k in range(block)])
        return arr[np.asarray(idx[:n])]

    for i in range(n_boot):
        diffs[i] = _blk(a).mean() - _blk(b).mean()

    # Two-sided p against the null that the true spread is zero: centre the
    # bootstrap distribution and ask how often it reaches |observed|.
    centred = diffs - diffs.mean()
    p = float((np.abs(centred) >= abs(obs)).mean())
    lo, hi = (float(x) for x in np.percentile(diffs, [2.5, 97.5]))

    sig = p < 0.05
    # NO_EDGE asserts a positive fact — that the true spread is near zero — and
    # requires enough sample to support it. Without a minimum-n guard a 12-episode
    # sample produced a narrow CI by luck and "no edge" was declared on data that
    # could not support any conclusion. "Cannot tell" is UNDERPOWERED.
    enough = min(len(a), len(b)) >= 50
    if sig and obs > 0:
        verdict = "POSITIVE"
    elif sig and obs < 0:
        verdict = "INVERTED"
    elif enough and abs(hi - lo) < 1.0 and abs(obs) < 0.25:
        verdict = "NO_EDGE"
    else:
        verdict = "UNDERPOWERED"

    return {
        "horizon": horizon, "block": block,
        "n_long": int(len(a)), "n_short": int(len(b)),
        "long_mean_%": round(ma, 3), "short_mean_%": round(mb, 3),
        "spread_%": round(obs, 3),
        "p_value": round(p, 4),
        "verdict": verdict,
        "ci95": [round(lo, 3), round(hi, 3)],
    }


# ---------------------------------------------------------------------------
# Null models — "different from zero" is not the bar
# ---------------------------------------------------------------------------
def permutation_null(obs: pd.DataFrame, horizon: int = 21,
                     n_perm: int = 1000, seed: int = 5) -> dict:
    """
    Does the classifier beat a random classifier with the SAME phase frequencies?

    Phase labels are shuffled within each date, preserving both the per-date
    cross-section and the overall phase mix, then the Breakout-minus-Flight spread
    is recomputed. If the real spread sits inside the permutation distribution,
    the classifier is not adding information over "pick sectors at random in these
    proportions" — regardless of whether it beats zero.
    """
    col = f"fwd_{horizon}"
    if col not in obs.columns or obs.empty:
        return {"error": "no data"}

    d = obs[["date", "phase", col]].dropna()
    if len(d) < 100:
        return {"error": "insufficient data"}
    # Shuffling labels within a date is only meaningful if dates contain several
    # observations. With one row per date the permutation is the identity and the
    # "null" distribution collapses onto the actual value, which silently reports
    # that a genuinely strong signal is indistinguishable from random.
    per_date = d.groupby("date").size()
    if per_date.median() < 4:
        return {"error": f"median {per_date.median():.0f} observations per date — "
                         f"within-date permutation is not meaningful"}

    def _spread(frame: pd.DataFrame) -> float:
        a = frame[frame["phase"] == "CONFIRMED_BREAKOUT"][col]
        b = frame[frame["phase"] == "CAPITAL_FLIGHT"][col]
        if len(a) < 5 or len(b) < 5:
            return np.nan
        return float(a.mean() - b.mean())

    actual = _spread(d)
    rng = np.random.default_rng(seed)
    null = np.empty(n_perm)
    arrs = {k: g.copy() for k, g in d.groupby("date")}
    for i in range(n_perm):
        parts = []
        for g in arrs.values():
            g2 = g.copy()
            g2["phase"] = rng.permutation(g2["phase"].to_numpy())
            parts.append(g2)
        null[i] = _spread(pd.concat(parts, ignore_index=True))
    null = null[np.isfinite(null)]
    if not len(null) or not np.isfinite(actual):
        return {"error": "could not compute"}

    p = float((np.abs(null) >= abs(actual)).mean())
    return {
        "horizon": horizon,
        "actual_spread_%": round(actual, 3),
        "null_mean_%": round(float(null.mean()), 3),
        "null_sd_%": round(float(null.std(ddof=0)), 3),
        "percentile_of_actual": round(float((null < actual).mean() * 100), 1),
        "p_value": round(p, 4),
        "beats_random": bool(p < 0.05),
        "n_perm": int(len(null)),
    }


def momentum_benchmark(obs: pd.DataFrame, tickers: list[str],
                       horizon: int = 21, lookback: int = 252,
                       skip: int = 21) -> dict:
    """
    Does the composite beat naive 12-1 momentum, which is free?

    12-1 momentum (trailing 12-month return skipping the most recent month) is the
    standard cheap cross-sectional signal. If a five-component composite with a
    dark-pool feed cannot beat it, the complexity is not earning its keep.
    """
    col = f"fwd_{horizon}"
    if col not in obs.columns:
        return {"error": "no data"}

    px = {}
    for t in set(obs["ticker"]):
        df = providers.history(t)
        if not df.empty:
            px[t] = df["close"]
    if not px:
        return {"error": "no price data"}

    rows = []
    for _, r in obs.iterrows():
        s = px.get(r["ticker"])
        if s is None:
            continue
        hist = s[s.index <= r["date"]]
        if len(hist) < lookback + skip + 2:
            continue
        mom = float(hist.iloc[-1 - skip] / hist.iloc[-1 - skip - lookback] - 1) * 100
        rows.append({"date": r["date"], "mom": mom, "fwd": r[col],
                     "csri": r.get("csri")})
    if len(rows) < 100:
        return {"error": f"only {len(rows)} usable rows"}
    d = pd.DataFrame(rows).dropna(subset=["mom", "fwd"])

    def _ic(score_col: str) -> float:
        ics = []
        for _, g in d.groupby("date"):
            g = g.dropna(subset=[score_col, "fwd"])
            if len(g) < 6 or g[score_col].nunique() < 3:
                continue
            ics.append(g[score_col].corr(g["fwd"], method="spearman"))
        ics = [x for x in ics if np.isfinite(x)]
        return float(np.mean(ics)) if ics else float("nan")

    ic_mom = _ic("mom")
    ic_csri = _ic("csri") if "csri" in d.columns else float("nan")
    return {
        "horizon": horizon,
        "momentum_12_1_ic": round(ic_mom, 4) if np.isfinite(ic_mom) else None,
        "csri_ic": round(ic_csri, 4) if np.isfinite(ic_csri) else None,
        "csri_beats_momentum": (bool(ic_csri > ic_mom)
                                if np.isfinite(ic_mom) and np.isfinite(ic_csri) else None),
        "n": int(len(d)),
    }


def power_analysis(eps: pd.DataFrame, horizon: int = 21,
                   long_phase: str = "CONFIRMED_BREAKOUT",
                   short_phase: str = "CAPITAL_FLIGHT",
                   target: float | None = None,
                   alpha: float = 0.05, power: float = 0.80) -> dict:
    """
    Minimum detectable effect and achieved power — the question this study never
    asked of itself.

    Reporting "no edge" from a test that could not have detected the effect being
    sought is the single most consequential error available in backtesting, and
    this project made it. A non-significant result means one of two very different
    things, and only this calculation distinguishes them:

        adequately powered + non-significant -> evidence the effect is small
        underpowered       + non-significant -> no information either way

    MDE is the two-sample formula at the observed dispersion and sample sizes:
        MDE = (z_{1-a/2} + z_{1-b}) * sd_pooled * sqrt(1/n1 + 1/n2)

    Everything is also expressed annualised, because "0.5% per 21 sessions" is
    hard to judge and "6% a year" is not.
    """
    col = f"fwd_{horizon}"
    if col not in eps.columns:
        return {"error": f"no {col}"}
    a = eps[eps["phase"] == long_phase][col].dropna().to_numpy()
    b = eps[eps["phase"] == short_phase][col].dropna().to_numpy()
    if len(a) < 5 or len(b) < 5:
        return {"n_long": len(a), "n_short": len(b), "error": "insufficient episodes"}

    n1, n2 = len(a), len(b)
    sd = math.sqrt(((n1 - 1) * a.var(ddof=1) + (n2 - 1) * b.var(ddof=1))
                   / max(n1 + n2 - 2, 1))
    se = sd * math.sqrt(1.0 / n1 + 1.0 / n2)

    # z-quantiles without scipy
    def _z(p: float) -> float:
        # Acklam's inverse-normal approximation, accurate to ~1e-9
        a_ = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
              1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
        b_ = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
              6.680131188771972e+01, -1.328068155288572e+01]
        c_ = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
              -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
        d_ = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
              3.754408661907416e+00]
        pl = 0.02425
        if p < pl:
            q = math.sqrt(-2 * math.log(p))
            return (((((c_[0]*q+c_[1])*q+c_[2])*q+c_[3])*q+c_[4])*q+c_[5]) / \
                   ((((d_[0]*q+d_[1])*q+d_[2])*q+d_[3])*q+1)
        if p > 1 - pl:
            return -_z(1 - p)
        q = p - 0.5
        r = q * q
        return (((((a_[0]*r+a_[1])*r+a_[2])*r+a_[3])*r+a_[4])*r+a_[5])*q / \
               (((((b_[0]*r+b_[1])*r+b_[2])*r+b_[3])*r+b_[4])*r+1)

    z_a, z_b = _z(1 - alpha / 2), _z(power)
    mde = (z_a + z_b) * se
    per_year = 252.0 / horizon
    obs = float(a.mean() - b.mean())

    # Achieved power against the pre-committed target
    target = CRITERIA["breakout_21d_mean"] if target is None else target
    ncp = target / se if se > 0 else float("inf")
    achieved = 1.0 - 0.5 * (1 + math.erf((z_a - ncp) / math.sqrt(2)))

    return {
        "horizon": horizon,
        "n_long": n1, "n_short": n2,
        "pooled_sd": round(sd, 3),
        "standard_error": round(se, 4),
        "observed_spread": round(obs, 3),
        "observed_spread_annualised_%": round(obs * per_year, 2),
        "mde": round(mde, 3),
        "mde_annualised_%": round(mde * per_year, 1),
        "target_effect": target,
        "achieved_power_vs_target": round(achieved, 3),
        "adequately_powered": bool(achieved >= power),
        "verdict": (
            "adequately powered — a non-significant result IS evidence the effect is small"
            if achieved >= power else
            f"UNDERPOWERED: only {achieved:.0%} chance of detecting the target "
            f"{target}% effect. A non-significant result carries no information. "
            f"Detecting it reliably needs ~{int(((z_a + z_b) * sd / target) ** 2 * 2)} "
            f"episodes per group (have {min(n1, n2)})."),
    }


def bh_adjust(pvals: dict[str, float], alpha: float = 0.05) -> dict:
    """
    Benjamini-Hochberg FDR adjustment across the test family.

    With ~16 simultaneous tests at raw alpha 0.05, the family-wise error rate is
    about 56%. BH controls the false discovery rate while retaining far more power
    than Bonferroni, which matters when the sample is already thin.
    """
    items = [(k, v) for k, v in pvals.items() if v is not None and np.isfinite(v)]
    if not items:
        return {}
    items.sort(key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, dict] = {}
    prev = 1.0
    for i in range(m - 1, -1, -1):
        k, p = items[i]
        adj = min(prev, p * m / (i + 1))
        prev = adj
        out[k] = {"raw_p": round(p, 4), "bh_p": round(adj, 4),
                  "significant": bool(adj < alpha)}
    return {"alpha": alpha, "n_tests": m, "results": out}


def monotonicity(eps: pd.DataFrame, horizon: int = 21) -> dict:
    """Is the realised phase ordering the one the design predicts?"""
    col = f"fwd_{horizon}"
    means = {}
    for ph in PHASES:
        sub = eps[eps["phase"] == ph][col].dropna()
        if len(sub) >= 8:
            means[ph] = float(sub.mean())
    ranked = [p for p, _ in sorted(means.items(), key=lambda kv: -kv[1])]
    expected = [p for p in EXPECTED_ORDER if p in means]

    # Spearman correlation between expected rank and realised rank
    if len(expected) >= 3:
        er = {p: i for i, p in enumerate(expected)}
        rr = {p: i for i, p in enumerate(ranked)}
        x = np.array([er[p] for p in expected], dtype=float)
        y = np.array([rr[p] for p in expected], dtype=float)
        rho = float(np.corrcoef(x, y)[0, 1]) if len(x) > 1 else float("nan")
    else:
        rho = float("nan")
    return {"horizon": horizon, "expected": expected, "realised": ranked,
            "rank_corr": round(rho, 3) if np.isfinite(rho) else None,
            "monotonic": ranked == expected,
            "means": {k: round(v, 3) for k, v in means.items()}}


def component_attribution(obs: pd.DataFrame, horizon: int = 21) -> pd.DataFrame:
    """
    Which component actually predicts?

    Each component z-score is ranked cross-sectionally at each date, then the
    forward relative return of the top tercile minus the bottom tercile is
    measured. A component that cannot separate terciles is not carrying signal,
    regardless of the weight it was assigned.
    """
    col = f"fwd_{horizon}"
    comps = [c for c in obs.columns if c.startswith("z_")]
    rows = []
    for c in comps + ["csri"]:
        d = obs[[c, col, "date"]].dropna()
        if len(d) < 200:
            rows.append({"component": c.replace("z_", ""), "n": len(d)})
            continue
        # tercile split within each date
        def _spread(g):
            if len(g) < 6:
                return np.nan
            q = g[c].quantile([1 / 3, 2 / 3]).to_numpy()
            top = g[g[c] >= q[1]][col].mean()
            bot = g[g[c] <= q[0]][col].mean()
            return top - bot
        try:      # pandas >= 2.2
            per_date = d.groupby("date", group_keys=False).apply(
                _spread, include_groups=False).dropna()
        except TypeError:
            per_date = d.groupby("date", group_keys=False).apply(_spread).dropna()
        if len(per_date) < 20:
            rows.append({"component": c.replace("z_", ""), "n": len(d)})
            continue
        mean, p, (lo, hi) = block_bootstrap_mean(per_date.to_numpy(), n_boot=3000)
        rows.append({
            "component": c.replace("z_", ""),
            "n": int(len(d)),
            "dates": int(len(per_date)),
            "top_minus_bottom_%": round(mean, 3),
            "p_value": round(p, 4) if np.isfinite(p) else None,
            "ci95_lo": round(lo, 3), "ci95_hi": round(hi, 3),
        })
    out = pd.DataFrame(rows)
    if "top_minus_bottom_%" in out.columns:
        out = out.sort_values("top_minus_bottom_%", ascending=False,
                              na_position="last")
    return out.reset_index(drop=True)


def transition_matrix(obs: pd.DataFrame) -> pd.DataFrame:
    """
    Does Stealth Accumulation actually precede Confirmed Breakout?

    This tests the mechanism rather than the returns. If accumulation leads to
    breakout no more often than the base rate, the narrative is wrong even if
    the return numbers happen to look acceptable.
    """
    eps = to_episodes(obs)
    if eps.empty:
        return pd.DataFrame()
    nxt = []
    for tkr, grp in eps.groupby("ticker", sort=False):
        grp = grp.sort_values("start")
        for i in range(len(grp) - 1):
            nxt.append((grp.iloc[i]["phase"], grp.iloc[i + 1]["phase"]))
    if not nxt:
        return pd.DataFrame()
    t = pd.DataFrame(nxt, columns=["from", "to"])
    mat = pd.crosstab(t["from"], t["to"], normalize="index") * 100
    return mat.round(1)


def csri_buckets(obs: pd.DataFrame, horizon: int = 21,
                 n_buckets: int | None = None) -> pd.DataFrame:
    """
    Forward relative return by composite-score bucket, formed per date.

    Bucket count adapts to the cross-section. Forcing quintiles onto 11 sectors
    puts 2.2 names per bucket, which is noise dressed as a monotonic table — so
    below ~20 names per date this drops to terciles automatically.
    """
    col = f"fwd_{horizon}"
    need = {"date", "csri", col}
    if not need.issubset(obs.columns):
        return pd.DataFrame()
    d = obs[["date", "csri", col]].dropna().copy()
    if len(d) < 300:
        return pd.DataFrame()

    per_date = d.groupby("date")["csri"].size().median()
    if n_buckets is None:
        n_buckets = 5 if per_date >= 20 else (3 if per_date >= 9 else 2)

    def _cut(s: pd.Series):
        if s.nunique() < n_buckets:
            return pd.Series([np.nan] * len(s), index=s.index)
        return pd.qcut(s, n_buckets, labels=False, duplicates="drop")

    d["q"] = d.groupby("date")["csri"].transform(_cut)
    d = d.dropna(subset=["q"])
    if d.empty:
        return pd.DataFrame()

    label = {2: "H", 3: "T", 5: "Q"}.get(n_buckets, "B")
    rows = []
    for q in sorted(d["q"].unique()):
        sub = d[d["q"] == q][col]
        mean, p, ci = block_bootstrap_mean(sub.to_numpy(), n_boot=2000)
        rows.append({"bucket": f"{label}{int(q)+1}", "n": len(sub),
                     "mean_rel_%": round(mean, 3),
                     "median_%": round(float(sub.median()), 3),
                     "hit_rate_%": round(float((sub > 0).mean() * 100), 1),
                     "p_value": round(p, 4) if np.isfinite(p) else None,
                     "n_buckets": n_buckets,
                     "names_per_date": round(float(per_date), 1)})
    return pd.DataFrame(rows)


# Backwards-compatible alias
csri_quintiles = csri_buckets


# ---------------------------------------------------------------------------
# Subperiod / regime stability
# ---------------------------------------------------------------------------
def subperiod_stability(eps: pd.DataFrame, horizon: int = 21,
                        n_periods: int = 4) -> pd.DataFrame:
    """
    Split the sample into equal calendar blocks and recompute the headline spread.

    A signal that only works in one subperiod is a regime artifact, not an edge.
    This is the Simpson's-paradox check applied to time: if the aggregate is
    positive but three of four subperiods are negative, the aggregate is being
    carried by one lucky stretch.
    """
    col = f"fwd_{horizon}"
    if col not in eps.columns or eps.empty or "start" not in eps.columns:
        return pd.DataFrame()

    e = eps.dropna(subset=[col]).sort_values("start").copy()
    if len(e) < n_periods * 20:
        n_periods = max(2, len(e) // 40) or 2
    e["block"] = pd.qcut(e["start"].rank(method="first"), n_periods,
                         labels=False, duplicates="drop")

    rows = []
    for blk, g in e.groupby("block"):
        a = g[g["phase"] == "CONFIRMED_BREAKOUT"][col]
        b = g[g["phase"] == "CAPITAL_FLIGHT"][col]
        sp = float(a.mean() - b.mean()) if len(a) >= 5 and len(b) >= 5 else np.nan
        rows.append({
            "period": f"P{int(blk)+1}",
            "from": str(g["start"].min().date()),
            "to": str(g["start"].max().date()),
            "episodes": len(g),
            "n_breakout": len(a), "n_flight": len(b),
            "spread_%": round(sp, 3) if np.isfinite(sp) else None,
            "breakout_mean_%": round(float(a.mean()), 3) if len(a) >= 5 else None,
            "flight_mean_%": round(float(b.mean()), 3) if len(b) >= 5 else None,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Held return vs onset return
# ---------------------------------------------------------------------------
def held_vs_onset(obs: pd.DataFrame, eps: pd.DataFrame,
                  horizon: int = 21) -> pd.DataFrame:
    """
    Separate "good entry timing" from "good sustained state".

    The episode statistics measure the forward return from an episode's FIRST
    date — signal onset. But an investor holding through a three-month breakout
    earns the average return across the whole episode. Reporting only onset
    understates a persistently-correct classifier; reporting only held return
    overstates one whose edge is purely at the turn.

    Both are reported so the difference is visible rather than assumed away.
    """
    col = f"fwd_{horizon}"
    if col not in obs.columns or eps.empty:
        return pd.DataFrame()

    rows = []
    for ph in PHASES:
        onset = eps[eps["phase"] == ph][col].dropna()
        held = obs[obs["phase"] == ph][col].dropna()
        if len(onset) < 5 and len(held) < 5:
            continue
        rows.append({
            "phase": ph,
            "onset_n": len(onset),
            "onset_mean_%": round(float(onset.mean()), 3) if len(onset) else None,
            "held_n": len(held),
            "held_mean_%": round(float(held.mean()), 3) if len(held) else None,
            "difference_%": (round(float(held.mean() - onset.mean()), 3)
                             if len(onset) and len(held) else None),
            "edge_concentrated_at_onset": (bool(onset.mean() > held.mean() * 1.5)
                                           if len(onset) and len(held) and held.mean() > 0
                                           else None),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Portfolio simulation with turnover
# ---------------------------------------------------------------------------
def portfolio_simulation(obs: pd.DataFrame, horizon: int = 21,
                         cost_bps: float = 5.0, step: int = 20) -> dict:
    """
    Long the top CSRI bucket, short the bottom, rebalanced at every date.

    Episode statistics are not strategy P&L. This adds the two things that decide
    whether a signal survives contact with reality:

      turnover — a signal that flips constantly pays costs constantly
      costs    — applied per unit of turnover at `cost_bps` round-trip

    Returns are relative (each leg is already sector-minus-benchmark), so this
    approximates a dollar-neutral long/short sector overlay.
    """
    col = f"fwd_{horizon}"
    if not {"date", "ticker", "csri", col}.issubset(obs.columns):
        return {"error": "missing columns"}
    d = obs[["date", "ticker", "csri", col]].dropna().copy()
    if len(d) < 200:
        return {"error": f"only {len(d)} usable rows"}

    per_date = d.groupby("date")["csri"].size().median()
    n_side = max(1, int(per_date // 5) if per_date >= 20 else int(per_date // 3))

    prev: dict[str, float] = {}
    recs = []
    for dt_, g in d.groupby("date"):
        g = g.sort_values("csri", ascending=False)
        longs = set(g.head(n_side)["ticker"])
        shorts = set(g.tail(n_side)["ticker"])
        if not longs or not shorts:
            continue
        r_long = g[g["ticker"].isin(longs)][col].mean()
        r_short = g[g["ticker"].isin(shorts)][col].mean()
        gross = float(r_long - r_short) / 2.0        # dollar-neutral halves

        # Turnover on SIGNED WEIGHTS, sum|w_t - w_{t-1}|.
        #
        # The previous version used set-membership churn over `longs | shorts`. A
        # ticker moving from the long leg to the SHORT leg stays inside that union,
        # so a book reversing completely at every rebalance registered as ~zero
        # trading despite a 200% position change per name. Verified: a forced full
        # reversal reported turnover 0.025, and a static book reported the SAME
        # 0.025 — the metric could not distinguish a book that never traded from
        # one that reversed entirely. Corrected, those read 3.95 and 0.05: for a
        # two-leg book at weights +/-1/n, a full reversal moves each name by 2/n
        # across 2n names, so ~4.0 is the correct scale, not 2.0.
        w = {t: 1.0 / len(longs) for t in longs}
        for t in shorts:
            w[t] = w.get(t, 0.0) - 1.0 / len(shorts)
        turnover = (sum(abs(w.get(t, 0.0) - prev.get(t, 0.0))
                        for t in set(w) | set(prev)) if prev else 2.0)
        cost = turnover * (cost_bps / 100.0)         # bps -> percent
        recs.append({"date": dt_, "gross_%": gross, "turnover": turnover,
                     "cost_%": cost, "net_%": gross - cost,
                     "n_long": len(longs), "n_short": len(shorts)})
        prev = w

    if len(recs) < 10:
        return {"error": "too few rebalance dates"}
    r = pd.DataFrame(recs)

    # Annualise: each observation is a `horizon`-session forward return sampled
    # every `step` sessions, so periods per year ~ 252/step (overlapping).
    # Each observation IS a `horizon`-session return, so both the mean and the sd
    # annualise by 252/horizon — NOT by 252/step. Using step made the reported
    # Sharpe depend on a sampling-frequency label: identical data gave Sharpe 1.31
    # at step=1 and 5.85 at step=20. Error factor was sqrt(horizon/step).
    periods_per_year = 252.0 / max(horizon, 1)
    gross_ann = float(r["gross_%"].mean()) * periods_per_year
    net_ann = float(r["net_%"].mean()) * periods_per_year
    sd_ann = float(r["net_%"].std(ddof=1)) * math.sqrt(periods_per_year)
    sharpe = net_ann / sd_ann if sd_ann > 0 else float("nan")

    eq = (1.0 + r["net_%"] / 100.0).cumprod()
    dd = float(((eq / eq.cummax()) - 1.0).min() * 100)

    return {
        "horizon": horizon, "rebalances": len(r),
        "names_per_side": n_side,
        "gross_mean_per_period_%": round(float(r["gross_%"].mean()), 4),
        "net_mean_per_period_%": round(float(r["net_%"].mean()), 4),
        "gross_annualised_%": round(gross_ann, 2),
        "net_annualised_%": round(net_ann, 2),
        "volatility_annualised_%": round(sd_ann, 2),
        "sharpe": round(sharpe, 2) if np.isfinite(sharpe) else None,
        "hit_rate_%": round(float((r["net_%"] > 0).mean() * 100), 1),
        "mean_turnover": round(float(r["turnover"].mean()), 3),
        "cost_drag_annualised_%": round(gross_ann - net_ann, 2),
        "max_drawdown_%": round(dd, 2),
        "cost_bps_assumed": cost_bps,
        "survives_costs": bool(net_ann > 0),
    }


# ---------------------------------------------------------------------------
# Weight fitting
# ---------------------------------------------------------------------------
COMPONENT_KEYS = ["mansfield_rs", "rs_momentum", "breadth", "money_flow", "inst_flow"]


def information_coefficient(obs: pd.DataFrame, weights: dict[str, float],
                            horizon: int = 21) -> float:
    """
    Mean cross-sectional rank correlation between the weighted score and forward
    relative return.

    This is the right objective function because the score is used to *rank*
    sectors against each other, not to predict a level. A weighting that gets the
    ordering right but the magnitude wrong is entirely useful; the reverse is not.
    Computed per date, then averaged across dates.
    """
    col = f"fwd_{horizon}"
    cols = [f"z_{k}" for k in weights if f"z_{k}" in obs.columns]
    if not cols or col not in obs.columns:
        return float("nan")

    d = obs[["date", col] + cols].copy()
    w = np.array([weights[c.replace("z_", "")] for c in cols], dtype=float)
    if w.sum() == 0:
        return float("nan")

    # Renormalise per row over the components that are actually present, rather
    # than filling missing components with 0 and applying their full weight.
    # Zero-filling fabricates a neutral reading AND dilutes the score toward zero
    # for exactly those sectors with incomplete data, which is a silent bias
    # correlated with data availability.
    mat = d[cols].to_numpy(dtype=float)
    present = np.isfinite(mat)
    wmat = np.where(present, w[None, :], 0.0)
    wsum = wmat.sum(axis=1)
    vals = np.where(present, mat, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        score = (vals * wmat).sum(axis=1) / wsum
    score[wsum <= 0] = np.nan
    d["score"] = score
    d = d.dropna(subset=["score"])
    if d.empty:
        return float("nan")

    ics = []
    for _, g in d.groupby("date"):
        g = g.dropna(subset=[col])
        if len(g) < 6 or g["score"].nunique() < 3:
            continue
        ics.append(g["score"].corr(g[col], method="spearman"))
    ics = [x for x in ics if np.isfinite(x)]
    return float(np.mean(ics)) if ics else float("nan")


def fit_weights(obs: pd.DataFrame, horizon: int = 21,
                train_frac: float = 0.70, n_samples: int = 4000,
                seed: int = 17, quiet: bool = False) -> dict:
    """
    Search the weight simplex for the combination that maximises rank IC, then
    report performance out of sample.

    Deliberate choices:

    Time-ordered split, never random
        A random split leaks: neighbouring dates share overlapping forward windows
        and highly autocorrelated features, so a random holdout would look like a
        clean test while actually being contaminated.

    Non-negative weights summing to 1
        Every component is designed so that "higher is more bullish". Allowing
        negative weights would let the fit invert an indicator's meaning to chase
        noise, and produce a model nobody can interpret.

    Random simplex search rather than gradient descent
        The objective (mean per-date Spearman) is non-smooth. Dirichlet sampling
        over the simplex is robust, needs no dependencies, and with a few thousand
        draws gets close enough given that the sample cannot support fine tuning.

    The verdict to watch is not the fitted weights themselves — it is whether they
    beat EQUAL weights out of sample. Very often they do not, and the honest
    conclusion is then "use equal weights", because the fitted set was fitting
    noise. That result is reported, not hidden.
    """
    col = f"fwd_{horizon}"
    if col not in obs.columns or obs.empty:
        return {"error": f"no {col} column"}

    have = [k for k in COMPONENT_KEYS if f"z_{k}" in obs.columns]
    if len(have) < 2:
        return {"error": "fewer than two components available"}

    dates = np.sort(obs["date"].unique())
    cut = dates[int(len(dates) * train_frac)]
    # Embargo: the last training date's forward window overlaps the first test
    # dates, so drop `horizon` sessions of training data at the boundary.
    embargo = pd.Timedelta(days=int(horizon * 1.5))
    train = obs[obs["date"] < (pd.Timestamp(cut) - embargo)]
    test = obs[obs["date"] >= cut]
    if len(train) < 200 or len(test) < 100:
        return {"error": f"insufficient data (train={len(train)}, test={len(test)})"}

    rng = np.random.default_rng(seed)

    # ---- reference weightings ------------------------------------------
    equal = {k: 1.0 / len(have) for k in have}
    current = {k: config.CSRI_WEIGHTS.get(k, 0.0) for k in have}
    if sum(current.values()) == 0:
        current = dict(equal)

    singles = {}
    for k in have:
        w = {x: 0.0 for x in have}
        w[k] = 1.0
        singles[k] = w

    # ---- search --------------------------------------------------------
    draws = rng.dirichlet(np.ones(len(have)), size=n_samples)
    scored: list[tuple[float, dict]] = []
    for i in range(n_samples):
        w = {k: float(draws[i, j]) for j, k in enumerate(have)}
        ic = information_coefficient(train, w, horizon)
        if np.isfinite(ic):
            scored.append((ic, w))
        if not quiet and (i + 1) % 500 == 0:
            bst = max(s[0] for s in scored) if scored else float("nan")
            print(f"\r    searching weights {i+1}/{n_samples}  "
                  f"best train IC {bst:+.4f}   ", end="", flush=True)
    if not quiet:
        print()

    if not scored:
        return {"error": "search failed to find a finite IC"}
    scored.sort(key=lambda kv: -kv[0])
    best_ic, best_w = scored[0]

    def _both(w):
        return {"train_ic": round(information_coefficient(train, w, horizon), 4),
                "test_ic": round(information_coefficient(test, w, horizon), 4),
                "weights": {k: round(v, 3) for k, v in w.items()}}

    results = {
        "fitted": _both(best_w),
        "equal": _both(equal),
        "current": _both(current),
        "single_best": None,
    }
    sb_name, sb = None, -np.inf
    for k, w in singles.items():
        r = _both(w)
        if np.isfinite(r["test_ic"]) and r["test_ic"] > sb:
            sb, sb_name = r["test_ic"], k
            results["single_best"] = {**r, "component": k}

    fit_test = results["fitted"]["test_ic"]
    eq_test = results["equal"]["test_ic"]
    cur_test = results["current"]["test_ic"]

    # Overfitting diagnostic: how much IC was lost going from train to test?
    decay = (results["fitted"]["train_ic"] - fit_test)

    # Plateau vs spike. Take the top 1% of candidates by TRAIN IC and evaluate all
    # of them on test. If the optimum is a genuine region of weight space, those
    # neighbours also score well on test and their weights resemble each other. If
    # it is a noise spike, test IC scatters and the weights disagree wildly — in
    # which case the single "best" weighting is an artifact of the search.
    top_k = max(5, int(len(scored) * 0.01))
    top = scored[:top_k]
    top_test = [information_coefficient(test, w, horizon) for _, w in top]
    top_test = [t for t in top_test if np.isfinite(t)]
    wmat = np.array([[w[k] for k in have] for _, w in top])
    plateau = {
        "candidates_examined": top_k,
        "train_ic_range": [round(top[-1][0], 4), round(top[0][0], 4)],
        "test_ic_mean": round(float(np.mean(top_test)), 4) if top_test else None,
        "test_ic_sd": round(float(np.std(top_test, ddof=0)), 4) if top_test else None,
        "test_ic_min": round(float(np.min(top_test)), 4) if top_test else None,
        "test_ic_max": round(float(np.max(top_test)), 4) if top_test else None,
        "weight_dispersion": {k: round(float(wmat[:, j].std(ddof=0)), 3)
                              for j, k in enumerate(have)},
        "max_weight_dispersion": round(float(wmat.std(axis=0).max()), 3),
    }
    # A plateau has low test-IC scatter relative to its level, and weights that
    # cluster. Thresholds are deliberately loose — this is a diagnostic, not a test.
    spike = bool(
        top_test
        and (plateau["test_ic_sd"] or 0) > max(0.01, abs(plateau["test_ic_mean"] or 0) * 0.8)
    ) or plateau["max_weight_dispersion"] > 0.28
    plateau["shape"] = "noise spike" if spike else "plateau"
    plateau["interpretation"] = (
        "The optimum does not generalise — its top-1% neighbours disagree on test "
        "IC and/or on weights. Treat the fitted weights as an artifact of the search."
        if spike else
        "The optimum sits in a stable region: neighbouring weightings score "
        "similarly out of sample and agree on the weights.")

    if not np.isfinite(fit_test) or not np.isfinite(eq_test):
        rec = "inconclusive — IC could not be computed out of sample"
    elif plateau["shape"] == "noise spike":
        rec = ("USE EQUAL WEIGHTS — the fitted optimum is a noise spike, not a "
               "stable region of weight space (see plateau diagnostic)")
    elif fit_test <= eq_test:
        rec = ("USE EQUAL WEIGHTS — the fitted weights did not beat equal weighting "
               "out of sample, which means the fit was chasing noise")
    elif fit_test <= cur_test:
        rec = ("KEEP CURRENT WEIGHTS — fitting did not improve on the existing "
               "weighting out of sample")
    elif decay > abs(fit_test) * 0.6:
        rec = (f"TREAT FITTED WEIGHTS WITH CAUTION — IC decayed {decay:+.4f} from "
               f"train to test, a sign of overfitting despite the improvement")
    else:
        rec = "ADOPT FITTED WEIGHTS — they beat both equal and current out of sample"

    return {
        "horizon": horizon,
        "components": have,
        "train_dates": [str(pd.Timestamp(dates[0]).date()), str(pd.Timestamp(cut).date())],
        "test_dates": [str(pd.Timestamp(cut).date()), str(pd.Timestamp(dates[-1]).date())],
        "train_obs": int(len(train)), "test_obs": int(len(test)),
        "results": results,
        "plateau_diagnostic": plateau,
        "ic_decay_train_to_test": round(float(decay), 4),
        "recommendation": rec,
        "caveat": ("Rank IC around 0.03-0.05 is typical for a real cross-sectional "
                   "signal; above 0.15 on this sample size is more likely a data "
                   "problem than an edge worth trusting."),
    }


# ---------------------------------------------------------------------------
# Success criteria — pre-committed
# ---------------------------------------------------------------------------
CRITERIA = {
    "breakout_21d_mean": 0.5,       # %
    "breakout_21d_hit": 55.0,       # %
    "breakout_min_episodes": 40,
    "accum_63d_mean": 1.5,
    "accum_63d_hit": 55.0,
    "accum_min_episodes": 30,
    "flight_21d_mean_max": -0.5,
    "spread_p_max": 0.05,
    "quintile_spread_min": 2.0,     # Q5 - Q1, annualised-ish
    "single_component_dominance": 0.90,
}


def evaluate(eps: pd.DataFrame, obs: pd.DataFrame, step: int = 5) -> dict:
    """Score the run against the pre-committed criteria and return a verdict."""
    # kill            -> invalidates the SYSTEM (inverted or confirmed-zero headline)
    # underpowered    -> cannot conclude; gather more data
    # signal_findings -> one component/phase unusable, others unaffected
    res: dict = {"checks": [], "kill": [], "underpowered": [],
                 "signal_findings": [], "verdict": None}

    def add(name: str, passed: bool | None, detail: str) -> None:
        res["checks"].append({"check": name, "pass": passed, "detail": detail})

    t21 = phase_table(eps, 21)
    t63 = phase_table(eps, 63)

    def _row(tbl, ph):
        if tbl.empty:
            return None
        r = tbl[tbl["phase"] == ph]
        return None if r.empty or r.iloc[0].get("episodes", 0) == 0 else r.iloc[0]

    br = _row(t21, "CONFIRMED_BREAKOUT")
    if br is not None:
        ok = (br["episodes"] >= CRITERIA["breakout_min_episodes"]
              and br["mean_rel_%"] > CRITERIA["breakout_21d_mean"]
              and br["hit_rate_%"] > CRITERIA["breakout_21d_hit"])
        add("Confirmed Breakout 21d", bool(ok),
            f"n={br['episodes']} mean={br['mean_rel_%']}% hit={br['hit_rate_%']}% "
            f"(need n>{CRITERIA['breakout_min_episodes']}, "
            f"mean>{CRITERIA['breakout_21d_mean']}%, hit>{CRITERIA['breakout_21d_hit']}%)")
        # "Too rarely to evaluate" is the definition of underpowered, not of a
        # failed signal. Previously this was a KILL, which asserted a conclusion
        # the data explicitly could not support.
        if br["episodes"] < CRITERIA["breakout_min_episodes"]:
            res["underpowered"].append(
                f"Confirmed Breakout produced only {br['episodes']} episodes "
                f"(want >{CRITERIA['breakout_min_episodes']}) — cannot evaluate")
    else:
        add("Confirmed Breakout 21d", None, "no episodes")

    ac = _row(t63, "STEALTH_ACCUMULATION")
    if ac is not None:
        ok = (ac["episodes"] >= CRITERIA["accum_min_episodes"]
              and ac["mean_rel_%"] > CRITERIA["accum_63d_mean"]
              and ac["hit_rate_%"] > CRITERIA["accum_63d_hit"])
        add("Stealth Accumulation 63d", bool(ok),
            f"n={ac['episodes']} mean={ac['mean_rel_%']}% hit={ac['hit_rate_%']}%")
        # Scoped to the signal, not the system. A rarely-firing accumulation
        # signal makes THAT signal unusable; it says nothing about whether the
        # breakout/flight classification works. Previously this was a global KILL,
        # so a system with a +4% significant headline spread was still killed.
        if ac["episodes"] < CRITERIA["accum_min_episodes"]:
            res["signal_findings"].append(
                f"Stealth Accumulation fired only {ac['episodes']} times — that "
                f"signal is too rare to act on (does not invalidate the others)")
    else:
        add("Stealth Accumulation 63d", None, "no episodes")
        res["signal_findings"].append(
            "Stealth Accumulation never fired — signal unusable as configured")

    fl = _row(t21, "CAPITAL_FLIGHT")
    if fl is not None:
        ok = fl["mean_rel_%"] < CRITERIA["flight_21d_mean_max"]
        add("Capital Flight 21d (short side)", bool(ok),
            f"n={fl['episodes']} mean={fl['mean_rel_%']}% "
            f"(need < {CRITERIA['flight_21d_mean_max']}%)")

    mono = monotonicity(eps, 21)
    add("Phase ordering monotonic (21d)", mono["monotonic"],
        f"expected {' > '.join(p[:6] for p in mono['expected'])} | "
        f"realised {' > '.join(p[:6] for p in mono['realised'])} | "
        f"rank corr {mono['rank_corr']}")
    if mono["rank_corr"] is not None and mono["rank_corr"] < 0:
        res["kill"].append("Phase ordering is inverted — classification is backwards")

    sp = spread_test(eps, 21, step=step)
    res["spread"] = sp
    if sp.get("spread_%") is not None:
        v = sp["verdict"]
        add("Breakout − Flight spread", v == "POSITIVE",
            f"spread={sp['spread_%']}% p={sp['p_value']} ci={sp['ci95']} "
            f"verdict={v}")
        # Only a *significant* wrong-sign or confirmed-zero result is a kill.
        # UNDERPOWERED means the test could not tell, which is a reason to gather
        # more data, not a reason to abandon. Conflating the two was a defect.
        if v == "INVERTED":
            res["kill"].append(
                f"Spread is significantly INVERTED ({sp['spread_%']}%, p={sp['p_value']}) "
                f"— the classification is backwards. Consider trading it in reverse.")
        elif v == "NO_EDGE":
            # Only a KILL if the test could actually have seen the target effect.
            if (res.get("power", {}).get(21) or {}).get("adequately_powered"):
                res["kill"].append(
                    "Spread is tightly bounded around zero AND the test was "
                    "adequately powered — no edge in this signal")
            else:
                res["underpowered"].append(
                    "Spread looks near zero but the test is underpowered — cannot "
                    "distinguish 'no edge' from 'edge too small to see here'")
        elif v == "UNDERPOWERED":
            res["underpowered"].append(
                f"Spread test underpowered (n_long={sp['n_long']}, "
                f"n_short={sp['n_short']}, ci={sp['ci95']}) — no conclusion drawn")
    else:
        add("Breakout − Flight spread", None, sp.get("note", "n/a"))
        res["underpowered"].append("Spread test had insufficient episodes")

    # ---- null models: beating zero is not the bar -----------------------
    perm = permutation_null(obs, 21)
    res["permutation"] = perm
    if "error" not in perm:
        add("Beats a random classifier", perm["beats_random"],
            f"actual={perm['actual_spread_%']}% vs null "
            f"{perm['null_mean_%']}±{perm['null_sd_%']}% "
            f"(pct {perm['percentile_of_actual']}, p={perm['p_value']})")
        if not perm["beats_random"]:
            res["underpowered"].append(
                "Spread is inside the distribution of a random classifier with the "
                "same phase frequencies — no information beyond the phase mix")

    mom = momentum_benchmark(obs, list(set(obs["ticker"])), 21)
    res["momentum"] = mom
    if "error" not in mom and mom.get("csri_beats_momentum") is not None:
        add("CSRI beats naive 12-1 momentum", mom["csri_beats_momentum"],
            f"CSRI IC={mom['csri_ic']} vs momentum IC={mom['momentum_12_1_ic']}")
        if mom["csri_beats_momentum"] is False:
            res["underpowered"].append(
                "A free 12-1 momentum signal ranks sectors better than the "
                "five-component composite — the complexity is not earning its keep")

    q = csri_quintiles(obs, 21)
    if not q.empty and len(q) >= 5:
        spread = float(q.iloc[-1]["mean_rel_%"] - q.iloc[0]["mean_rel_%"])
        add("CSRI Q5 − Q1 spread", bool(spread > CRITERIA["quintile_spread_min"] / 4),
            f"{spread:.2f}% over 21d "
            f"(need > {CRITERIA['quintile_spread_min']/4:.2f}%)")

    attr = component_attribution(obs, 21)
    if not attr.empty and "top_minus_bottom_%" in attr.columns:
        comps = attr[attr["component"] != "csri"].dropna(subset=["top_minus_bottom_%"])
        if len(comps) >= 2:
            vals = comps["top_minus_bottom_%"].abs()
            share = float(vals.iloc[0] / vals.sum()) if vals.sum() else 0.0
            dominant = comps.iloc[0]["component"]
            add("No single component dominates",
                share < CRITERIA["single_component_dominance"],
                f"top component '{dominant}' accounts for {share:.0%} of "
                f"combined tercile spread")
            if share >= CRITERIA["single_component_dominance"]:
                res["kill"].append(
                    f"'{dominant}' explains {share:.0%} of the signal — the composite "
                    f"adds nothing, use that indicator alone")

    # ---- statistical rigour layer ---------------------------------------
    from . import diagnostics as dg

    col21 = "fwd_21"
    a = eps[eps["phase"] == "CONFIRMED_BREAKOUT"][col21].dropna() \
        if col21 in eps.columns else pd.Series(dtype=float)
    b = eps[eps["phase"] == "CAPITAL_FLIGHT"][col21].dropna() \
        if col21 in eps.columns else pd.Series(dtype=float)

    res["distribution"] = (dg.describe(eps[col21].dropna(), "all episodes, 21d")
                           if col21 in eps.columns else None)
    res["effect_size"] = dg.effect_size(a, b) if len(a) and len(b) else None
    res["rank_test"] = dg.rank_test(a, b) if len(a) and len(b) else None
    res["outlier_influence"] = (dg.outlier_influence(a) if len(a) >= 10 else None)
    res["cross_validation"] = dg.cross_validate_spread(eps, 21)
    res["subperiods"] = subperiod_stability(eps, 21).to_dict(orient="records")
    res["held_vs_onset"] = held_vs_onset(obs, eps, 21).to_dict(orient="records")
    res["portfolio"] = portfolio_simulation(obs, 21, step=step)

    # Simpson's-paradox check across time blocks
    if "start" in eps.columns and col21 in eps.columns and len(eps) > 60:
        e = eps.dropna(subset=[col21]).copy()
        e["block"] = pd.qcut(e["start"].rank(method="first"),
                             min(4, max(2, len(e) // 40)),
                             labels=False, duplicates="drop")
        res["segment_stability"] = dg.segment_stability(
            e, col21, "phase", "block", "CONFIRMED_BREAKOUT", "CAPITAL_FLIGHT")
    else:
        res["segment_stability"] = None

    res["red_flags"] = dg.red_flags(obs, eps)

    # Power FIRST — it determines whether any non-significant result means anything.
    res["power"] = {h: power_analysis(eps, h) for h in (21, 63)}
    pw21 = res["power"].get(21) or {}
    if pw21.get("adequately_powered") is False:
        res["underpowered"].append(
            f"MDE at 21d is {pw21.get('mde_annualised_%')}%/yr; achieved power vs the "
            f"pre-committed {pw21.get('target_effect')}% target is only "
            f"{pw21.get('achieved_power_vs_target', 0):.0%}. Non-significant results "
            f"here carry NO information about whether an edge exists.")

    # Practical-significance gate: statistical significance alone is not a pass.
    ef = res["effect_size"]
    if ef and ef.get("cohens_d") is not None:
        add("Effect size is practically meaningful", ef["practically_meaningful"],
            f"Cohen's d={ef['cohens_d']} ({ef['magnitude']}), "
            f"P(breakout>flight)={ef['prob_superiority']}")

    rt = res["rank_test"]
    sp_v = (res.get("spread") or {}).get("verdict")
    if rt and rt.get("p_value") is not None and sp_v:
        agree = (rt["significant"] and sp_v in ("POSITIVE", "INVERTED")) or \
                (not rt["significant"] and sp_v in ("NO_EDGE", "UNDERPOWERED"))
        add("Bootstrap and rank test agree", agree,
            f"Mann-Whitney p={rt['p_value']} (sig={rt['significant']}) vs "
            f"bootstrap verdict {sp_v}")
        if not agree:
            res["underpowered"].append(
                "Parametric and rank-based tests disagree — usually means outliers "
                "are driving the mean; see outlier_influence")

    oi = res["outlier_influence"]
    if oi and oi.get("outlier_driven"):
        # A caveat, NOT a kill. The previous trigger fired on ~27% of clean null
        # samples at every sample size, and it was the only KILL firing on the
        # composite run — manufacturing an "abandon" verdict from an otherwise
        # UNDERPOWERED result.
        res["signal_findings"].append(
            f"Tail observations are material: raw mean {oi['raw_mean']}% vs "
            f"trimmed {oi.get('trimmed_10pct')}% "
            f"({oi.get('gap_in_se_units')} standard errors apart)")

    ss = res["segment_stability"]
    if ss and str(ss.get("stability", "")).startswith("UNSTABLE"):
        res["kill"].append(
            f"Spread reverses across time blocks — only {ss['segments_agreeing']}"
            f"/{ss['segments_evaluated']} agree with the aggregate (Simpson's paradox)")

    cv = res["cross_validation"]
    if cv and cv.get("agree") is False:
        # Demoted from KILL. Group-mean difference and an OLS binary-dummy
        # coefficient are the same number by construction, so this can only ever
        # detect a numerical fault, never a data-handling bug. It was presented as
        # satisfying a "compute it two ways" check; it does not.
        res["signal_findings"].append(
            f"Numerical inconsistency in the spread calculation: {cv['verdict']}")

    pf = res["portfolio"]
    if pf and "error" not in pf:
        add("Survives transaction costs", pf["survives_costs"],
            f"net {pf['net_annualised_%']}%/yr after {pf['cost_drag_annualised_%']}% "
            f"cost drag at {pf['cost_bps_assumed']}bps, turnover {pf['mean_turnover']}, "
            f"Sharpe {pf['sharpe']}")

    # ---- multiple-testing correction across the family ------------------
    fam: dict[str, float] = {}
    for h in (21, 63):
        for ph in PHASES:
            t = phase_table(eps, h)
            if t.empty:
                continue
            r = t[t["phase"] == ph]
            if not r.empty and r.iloc[0].get("p_value") is not None:
                fam[f"{ph}_{h}d"] = float(r.iloc[0]["p_value"])
    if res.get("spread", {}).get("p_value") is not None:
        fam["spread_21d"] = res["spread"]["p_value"]
    if res.get("permutation", {}).get("p_value") is not None:
        fam["permutation_21d"] = res["permutation"]["p_value"]
    # Component attribution belongs in the SAME family. Adjusting it separately
    # controls FDR within each table but not across the study, which is what the
    # reader actually draws conclusions from.
    try:
        attr_all = component_attribution(obs, 21)
        for _, r in attr_all.iterrows():
            if r.get("p_value") is not None and np.isfinite(r["p_value"]):
                fam[f"component_{r['component']}_21d"] = float(r["p_value"])
    except Exception:  # noqa: BLE001, S110
        pass
    res["multiple_testing"] = bh_adjust(fam)

    passed = [c for c in res["checks"] if c["pass"] is True]
    failed = [c for c in res["checks"] if c["pass"] is False]
    res["n_pass"], res["n_fail"] = len(passed), len(failed)

    # Verdict. UNDERPOWERED outranks FAIL: if the tests could not resolve the
    # question, the honest answer is "we do not know", not "it does not work".
    # The previous logic collapsed those and reported KILL on absence of evidence.
    n_ep = len(eps)
    thin = n_ep < 200 or len(obs["date"].unique()) < 100
    if res["kill"]:
        res["verdict"] = "KILL"
    elif thin or len(res["underpowered"]) >= 2:
        res["verdict"] = "UNDERPOWERED"
    elif len(failed) == 0 and len(passed) >= 4:
        res["verdict"] = "PASS"
    elif len(passed) > len(failed):
        res["verdict"] = "MARGINAL"
    else:
        res["verdict"] = "FAIL"

    res["power_note"] = (
        f"{n_ep} episodes across {len(obs['date'].unique())} dates. "
        + ("Sample is thin; treat all conclusions as provisional."
           if thin else "Sample size is adequate for the tests performed."))

    # ---- confidence assessment (validate-data 3-level scale) -------------
    res["confidence"] = dg.confidence_assessment(
        res, res["red_flags"], res["distribution"],
        res["outlier_influence"], res["segment_stability"])
    return res
