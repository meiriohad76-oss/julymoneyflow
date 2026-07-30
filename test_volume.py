#!/usr/bin/env python3
"""
Tests for the volume-regime metrics and the volume-aware stage classifier.

Each case is a synthetic OHLCV frame constructed so the correct stage is known by
design. The stage classifier now has eight branches; without tests, a change to one
threshold silently reshuffles the others.

    python test_volume.py
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from smf import metrics as m

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


def frame(prices, volumes, spread: float = 0.01) -> pd.DataFrame:
    """Build an OHLCV frame from close and volume paths."""
    n = len(prices)
    idx = pd.bdate_range("2023-01-02", periods=n)
    c = np.asarray(prices, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(c, o) * (1 + spread)
    lo = np.minimum(c, o) * (1 - spread)
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c,
                         "volume": np.asarray(volumes, dtype=float)}, index=idx)


def main() -> int:
    rng = np.random.default_rng(7)
    N = 300
    print("Volume-regime and stage-classifier tests\n" + "=" * 66)

    # ---------------- volume_trend ----------------
    print("\n[volume_trend]")
    flat = frame(np.full(N, 100.0) + rng.normal(0, .3, N), np.full(N, 1e6))
    check("flat volume -> ~0%", m.volume_trend(flat).iloc[-1], 0.0, tol=6)

    # volume doubling over the last 20 sessions
    v = np.concatenate([np.full(N - 20, 1e6), np.full(20, 2e6)])
    exp = frame(np.full(N, 100.0), v)
    check("recent doubling -> expanding", m.volume_trend(exp).iloc[-1] > 25, True)

    v = np.concatenate([np.full(N - 20, 1e6), np.full(20, 4e5)])
    con = frame(np.full(N, 100.0), v)
    check("recent 60% drop -> contracting", m.volume_trend(con).iloc[-1] < -20, True)

    # a single 20x spike must NOT create an expansion regime (median, not mean)
    v = np.full(N, 1e6); v[-3] = 2e7
    spike = frame(np.full(N, 100.0), v)
    check("single 20x spike does not fake expansion",
          abs(m.volume_trend(spike).iloc[-1]) < 10, True)

    # ---------------- volume_regime_profile: strength + length ----------------
    print("\n[volume_regime_profile — strength and length]")
    jit0 = lambda a: a * rng.uniform(0.85, 1.2, len(a))  # noqa: E731

    # sustained expansion: 40 sessions of doubled volume
    v = jit0(np.concatenate([np.full(N - 40, 1e6), np.full(40, 2.2e6)]))
    pr = m.volume_regime_profile(frame(np.full(N, 100.0), v))
    check("sustained expansion detected", pr["volume_regime"], "expanding")
    check("length counted (>=20 sessions)", pr["regime_days"] >= 20, True)
    check("flagged as sustained", pr["regime_sustained"], True)
    check("strength is a z-score", isinstance(pr["strength_z"], float), True)

    # A brief 4-session burst must NOT register as an expansion REGIME — four days
    # cannot move a 20-session median. That is the correct division of labour:
    # `volume_zscore` detects bursts, `volume_trend` detects regimes, and conflating
    # them would make every earnings day look like a structural shift.
    v = jit0(np.concatenate([np.full(N - 4, 1e6), np.full(4, 2.5e6)]))
    f2 = frame(np.full(N, 100.0), v)
    pr2 = m.volume_regime_profile(f2)
    check("4-session burst is NOT an expansion regime",
          pr2["volume_regime"] != "expanding", True)
    check("...but the burst IS caught by the spike detector",
          m.volume_zscore(f2).iloc[-1] > 1.5, True)

    # extreme contraction should register high |z| and a low percentile
    v = jit0(np.concatenate([np.full(N - 25, 2e6), np.full(25, 2e5)]))
    pr3 = m.volume_regime_profile(frame(np.full(N, 100.0), v))
    check("extreme contraction -> negative z", pr3["strength_z"] < -1.0, True)
    check("extreme contraction -> low percentile", pr3["strength_pct"] < 25, True)
    check("strength label escalates", pr3["strength_label"] in
          ("notable", "strong", "extreme"), True)

    # a routine wobble should read unremarkable
    v = jit0(np.full(N, 1e6))
    pr4 = m.volume_regime_profile(frame(np.full(N, 100.0), v))
    check("routine volume -> unremarkable", pr4["strength_label"], "unremarkable")
    check("insufficient history handled",
          m.volume_regime_profile(frame([100]*20, [1e6]*20))["regime_days"], None)

    # ---------------- distribution / accumulation days ----------------
    print("\n[distribution_days / accumulation_days]")
    # alternate: every other day is a down-close on rising volume
    px, vol = [100.0], [1e6]
    for i in range(1, 60):
        px.append(px[-1] * (0.99 if i % 2 else 1.01))
        vol.append(vol[-1] * (1.2 if i % 2 else 0.9))
    alt = frame(px, vol)
    dd = m.distribution_days(alt).iloc[-1]
    ad = m.accumulation_days(alt).iloc[-1]
    check("distribution days detected", dd > 5, True)
    check("more distribution than accumulation in this construction", dd > ad, True)

    up = frame(np.linspace(100, 130, 80), np.linspace(1e6, 2e6, 80))
    check("steady advance on rising volume -> accumulation days dominate",
          m.accumulation_days(up).iloc[-1] > m.distribution_days(up).iloc[-1], True)
    check("counts are integers within window", m.distribution_days(alt).iloc[-1] <= 25, True)

    # ---------------- volume_price_divergence ----------------
    print("\n[volume_price_divergence]")
    # price up 15%, volume contracting hard
    px = np.concatenate([np.full(N - 60, 100.0), np.linspace(100, 115, 60)])
    v = np.concatenate([np.full(N - 20, 1e6), np.full(20, 4e5)])
    d = m.volume_price_divergence(frame(px, v))
    check("price up + volume contracting -> bearish", d["divergence"], "bearish")
    check("flagged as depletion warning", d["depletion_warning"], True)

    px = np.concatenate([np.full(N - 60, 100.0), np.linspace(100, 85, 60)])
    d = m.volume_price_divergence(frame(px, v))
    check("price down + volume contracting -> bullish", d["divergence"], "bullish")

    v = np.concatenate([np.full(N - 20, 1e6), np.full(20, 2e6)])
    px = np.concatenate([np.full(N - 60, 100.0), np.linspace(100, 115, 60)])
    d = m.volume_price_divergence(frame(px, v))
    check("price up + volume expanding -> confirmed_up", d["divergence"], "confirmed_up")
    check("confirmed_up is not a depletion warning", d["depletion_warning"], False)

    check("insufficient history handled",
          "note" in m.volume_price_divergence(frame([100] * 20, [1e6] * 20)), True)

    # ---------------- stage classifier ----------------
    print("\n[weinstein_stage — volume is now part of the definition]")

    # Stage 2: sustained advance on expanding volume
    px = np.linspace(100, 150, N) + rng.normal(0, .4, N)
    v = np.linspace(8e5, 2e6, N)
    st, lbl, ev = m.weinstein_stage(frame(px, v))
    check("rising price + expanding volume -> Stage 2", st, 2)
    check("evidence records volume regime", ev["volume_regime"] in
          ("expanding", "flat"), True)

    # Stage 4: sustained decline
    px = np.linspace(150, 100, N) + rng.normal(0, .4, N)
    st, lbl, ev = m.weinstein_stage(frame(px, np.linspace(1e6, 1.6e6, N)))
    check("sustained decline -> Stage 4", st, 4)

    # Stage 1 vs Stage 3 — the discrimination the old code got wrong.
    # Both are sideways price around a flat MA; volume is the only difference.
    # NOTE: volume needs day-to-day jitter. `distribution_days` compares volume to
    # the PRIOR session, so a perfectly constant series can never register a single
    # accumulation or distribution day. Real data always jitters; fixtures must too.
    base = 100 + np.sin(np.linspace(0, 14, N)) * 4 + rng.normal(0, .8, N)
    jit = lambda a: a * rng.uniform(0.8, 1.25, len(a))  # noqa: E731

    quiet_v = jit(np.concatenate([np.full(N - 20, 1.6e6), np.full(20, 5e5)]))
    st1, lbl1, ev1 = m.weinstein_stage(frame(base, quiet_v))
    heavy_v = jit(np.concatenate([np.full(N - 20, 7e5), np.full(20, 2.4e6)]))
    st3, lbl3, ev3 = m.weinstein_stage(frame(base, heavy_v))
    print(f"        trending + contracting volume -> Stage {st1} "
          f"({ev1['volume_regime']}, {ev1['distribution_days_25d']}d dist)")
    print(f"        trending + expanding volume    -> Stage {st3} "
          f"({ev3['volume_regime']}, {ev3['distribution_days_25d']}d dist)")
    check("identical price, different volume -> different stage", st1 != st3, True)

    # Contracting volume means OPPOSITE things depending on price structure, and
    # both readings are in the source framework:
    #   advancing price + contracting volume -> Stage 3, unparticipated advance
    #   ranging   price + contracting volume -> Stage 1, quiet accumulation
    # Asserting only one of these would let a regression in the other pass silently.
    check("advance on contracting volume -> Stage 3 (unparticipated)", st1, 3)
    check("advance on expanding volume -> Stage 2 (participated)", st3, 2)

    # Now the genuinely range-bound case: many SMA crossings, flat slope.
    osc = 100 + np.sin(np.linspace(0, 70, N)) * 2.5 + rng.normal(0, .4, N)
    q = jit(np.concatenate([np.full(N - 20, 1.8e6), np.full(20, 5e5)]))
    s_quiet, _, e_quiet = m.weinstein_stage(frame(osc, q))
    h = jit(np.concatenate([np.full(N - 20, 7e5), np.full(20, 2.6e6)]))
    s_heavy, _, e_heavy = m.weinstein_stage(frame(osc, h))
    print(f"        ranging + contracting volume  -> Stage {s_quiet} "
          f"({e_quiet['volume_regime']}, {e_quiet['sma50_crossings_60d']} crossings)")
    print(f"        ranging + expanding volume    -> Stage {s_heavy} "
          f"({e_heavy['volume_regime']}, {e_heavy['sma50_crossings_60d']} crossings)")
    check("range + contracting volume -> Stage 1 (quiet accumulation)", s_quiet, 1)
    check("range + expanding volume -> Stage 3 (churn without progress)", s_heavy, 3)
    check("the range case really is ranging",
          e_quiet["sma50_crossings_60d"] >= 6, True)

    # Stage 4 requires a DOWNWARD-sloping MA, not merely price below it.
    # A long strong advance then a brief shallow dip: price drops under the 50d SMA
    # while that SMA is still rising.
    px = np.concatenate([np.linspace(100, 160, N - 12), np.linspace(160, 150, 12)])
    v = jit(np.full(N, 1e6))
    st, lbl, ev = m.weinstein_stage(frame(px, v))
    check("price below a RISING MA is not Stage 4", st != 4, True)
    check("  ...and the SMA slope is indeed positive",
          ev["sma50_slope_21d_pct"] > 0, True)

    check("insufficient history -> stage 0", m.weinstein_stage(
        frame([100] * 40, [1e6] * 40))[0], 0)
    check("returns a 3-tuple with evidence",
          len(m.weinstein_stage(frame(px, v))) == 3, True)
    check("evidence includes the reason string",
          "reason" in m.weinstein_stage(frame(px, v))[2], True)

    print("\n" + "=" * 66)
    print(f"{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
