#!/usr/bin/env python3
"""
Verify the trust panel still matches the backtest data.

WHY THIS EXISTS
---------------
The panel states, in plain language, how often each score picks the better of
two sectors. Those numbers live in `config.VALIDATION` as constants, because
recomputing 51,000 pairwise comparisons on every dashboard build would be a
waste. Constants drift. This recomputes them from
`output/backtest_observations.csv` and fails if the dashboard is now claiming
something the data does not support.

A wrong number here is worse than a missing one: the whole point of the panel
is to tell the user what to believe.

    python test_validation.py
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from smf import config

OBS = Path("output/backtest_observations.csv")
TOL = 0.35          # percentage points

passed = failed = 0


def check(cond: bool, msg: str) -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {msg}")


def hit_rate(d: pd.DataFrame, score: str, fwd: str = "fwd_21") -> tuple[float, int]:
    """
    Pairwise concordance, computed WITHIN each date.

    Within-date is the whole point: comparing a sector in 2018 against another in
    2024 would mostly measure which year was better for equities, not whether the
    score ranks sectors. Ties on either side are excluded rather than counted as
    half, so the figure is "of the pairs where both differ, how many were ordered
    correctly".
    """
    good = tot = 0
    for _, g in d.groupby("date"):
        g = g.dropna(subset=[score, fwd])
        if len(g) < 3:
            continue
        s = g[score].to_numpy()
        f = g[fwd].to_numpy()
        for i, j in itertools.combinations(range(len(g)), 2):
            if s[i] == s[j] or f[i] == f[j]:
                continue
            tot += 1
            if (s[i] > s[j]) == (f[i] > f[j]):
                good += 1
    return (100.0 * good / tot if tot else float("nan")), tot


def main() -> int:
    if not OBS.exists():
        print(f"{OBS} not found — run the backtest first")
        return 2
    d = pd.read_csv(OBS)

    print(f"\nobservations: {len(d):,}   dates: {d['date'].nunique()}   "
          f"tickers: {d['ticker'].nunique()}")

    print("\n=== 1. panel figures match the data ===")
    for key, col in (("vms", "vms"), ("csri", "csri")):
        want = config.VALIDATION[key]["hit_rate"]
        got, n = hit_rate(d, col)
        print(f"  {key:5} panel says {want:.1f} in 100, data says {got:.1f}  "
              f"({n:,} pairs)")
        check(abs(got - want) <= TOL,
              f"{key} hit rate drifted: panel {want:.1f} vs data {got:.1f} "
              f"(tolerance {TOL})")

    print("\n=== 2. the ordering the panel asserts ===")
    vms, _ = hit_rate(d, "vms")
    csri, _ = hit_rate(d, "csri")
    check(vms > csri, "VMS must beat CSRI, which is why the panel says to use it")
    check(vms > config.COIN_FLIP, "VMS must beat a coin flip")
    check(abs(csri - config.COIN_FLIP) < 1.0,
          "CSRI must sit near a coin flip, which is why the panel says not to trade it")
    # And the edge must stay small — if this ever reads like a strong signal,
    # the panel's "a real edge, and a small one" wording is no longer honest.
    check(vms - config.COIN_FLIP < 5.0,
          "VMS edge must remain small; the panel describes it as small")

    print("\n=== 3. phase labels are not claimed to be predictive ===")
    check(config.VALIDATION["phase"]["hit_rate"] is None,
          "phase labels must carry no hit rate — they were never tested as a forecast")
    check("descriptive" in config.VALIDATION["phase"]["verdict"].lower(),
          "phase verdict must say descriptive")

    print("\n=== 4. sign stability, the claim behind 'every time period' ===")
    # The panel says VMS was right more often in all four subperiods. Check it.
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    edges = np.array_split(np.sort(d["date"].unique()), 4)
    per = []
    for i, blk in enumerate(edges, 1):
        sub = d[d["date"].isin(blk)]
        hr, n = hit_rate(sub, "vms")
        per.append(hr)
        print(f"  subperiod {i}: {hr:.1f} in 100  ({n:,} pairs)")
    check(all(h > config.COIN_FLIP for h in per),
          f"VMS must beat a coin flip in all four subperiods, got {[round(h,1) for h in per]}")

    csri_per = []
    for blk in edges:
        hr, _ = hit_rate(d[d["date"].isin(blk)], "csri")
        csri_per.append(hr)
    print(f"  CSRI by subperiod: {[round(h, 1) for h in csri_per]}")
    # The panel claims CSRI is "between 50 and 51 in every period". Assert
    # exactly that, in the same units the panel uses. An earlier version of this
    # test asserted CSRI *reverses* across periods, which is true of its rank IC
    # but false of its hit rate — the panel had inherited a claim from a
    # different measure than the one it displays.
    check(all(config.COIN_FLIP - 0.5 <= h <= config.COIN_FLIP + 1.0 for h in csri_per),
          f"CSRI must stay within a point of a coin flip every period, got "
          f"{[round(h, 1) for h in csri_per]}")
    check(max(csri_per) - config.COIN_FLIP < (vms - config.COIN_FLIP),
          "CSRI's best subperiod must still be weaker than VMS overall")

    print("\n=== 5. panel text is self-consistent ===")
    for key, v in config.VALIDATION.items():
        check(bool(v["plain"]) and len(v["plain"]) > 60,
              f"{key} has a plain-language explanation")
        check(v["level"] in ("green", "red", "grey"), f"{key} has a valid level")
        check(bool(v["verdict"]), f"{key} has a verdict")
    check(config.COIN_FLIP == 50.0, "coin flip baseline is 50")

    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
