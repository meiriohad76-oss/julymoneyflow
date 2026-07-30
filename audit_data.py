#!/usr/bin/env python3
"""
Data provenance and integrity audit.

Run this before trusting any backtest. Two jobs:

1. Report which provider produced the bars currently on disk. A mixed-provenance
   cache means some series are Polygon-adjusted and others Yahoo-adjusted, and any
   cross-sectional metric computed across them compares unlike things.

2. When more than one provider is available, fetch the same tickers from each and
   quantify the disagreement — separately for price and for volume. Volume is the
   one that matters most here: Yahoo does not reliably split-adjust volume, and
   four metrics (Chaikin Money Flow, volume z-scores, absorption, accumulation/
   distribution days) are volume-based. A price series can agree to four decimal
   places while volume disagrees by orders of magnitude across a split.

    python audit_data.py                 # provenance report + integrity checks
    python audit_data.py --compare       # force provider-vs-provider comparison
    python audit_data.py --tickers XLE,XLK,SMH
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from smf import config, providers


def integrity_checks(ticker: str, df: pd.DataFrame) -> list[str]:
    """Structural problems that would silently corrupt indicator values."""
    issues = []
    if df.empty:
        return ["no data"]

    # OHLC coherence
    bad_hl = int((df["high"] < df["low"]).sum())
    if bad_hl:
        issues.append(f"{bad_hl} bars with high < low")
    bad_c = int(((df["close"] > df["high"] * 1.001) |
                 (df["close"] < df["low"] * 0.999)).sum())
    if bad_c:
        issues.append(f"{bad_c} bars with close outside high/low")

    # Zero or missing volume — breaks CMF and every volume z-score
    zv = int((df["volume"] <= 0).sum())
    if zv:
        issues.append(f"{zv} bars with zero/negative volume")

    # Unadjusted split detection.
    #
    # A volume level shift ALONE is not evidence of a split — genuine volume
    # regimes change (the 2020 crash moved every ETF's volume 3x; a newly launched
    # ETF ramps from thin to liquid over its first year). Flagging those produced
    # false positives on 6 of 10 series, which trains the reader to ignore the
    # audit.
    #
    # The actual signature of an unadjusted split is a volume shift accompanied by
    # a *price* shift of inverse magnitude on the same day: a 2-for-1 split halves
    # the price and doubles the share volume. Requiring both makes the check
    # specific instead of merely sensitive.
    v = df["volume"].replace(0, np.nan).dropna()
    c = df["close"].reindex(v.index)
    if len(v) > 120:
        vr = (v.rolling(20).median() / v.rolling(20).median().shift(20)).dropna()
        shifts = vr[(vr > 2.5) | (vr < 1 / 2.5)]
        splits = []
        for dt_, ratio in shifts.items():
            # same-day price gap
            i = c.index.get_indexer([dt_], method="pad")[0]
            if i < 1:
                continue
            pr = float(c.iloc[i] / c.iloc[i - 1])
            # split: price ratio ~ 1/volume ratio (within 25%)
            if pr > 0 and abs(pr - 1.0 / ratio) < 0.25 * max(pr, 1.0 / ratio):
                splits.append((dt_, ratio, pr))
        if splits:
            dt_, ratio, pr = splits[0]
            issues.append(f"{len(splits)} probable UNADJUSTED SPLIT(s): volume "
                          f"{ratio:.1f}x with price {pr:.2f}x on {dt_.date()}")
        elif len(shifts) > 20:
            issues.append(f"{len(shifts)} volume regime shifts (no matching price "
                          f"gaps, so not splits — likely genuine liquidity changes)")

    # Single-day volume spikes far beyond anything else in the series
    if len(v) > 250:
        vz = v / v.rolling(250, min_periods=100).median()
        extreme = int((vz > 30).sum())
        if extreme:
            issues.append(f"{extreme} day(s) with volume >30x the trailing median")

    # Price gaps beyond what a real session plausibly produces
    ret = df["close"].pct_change().dropna()
    ext = ret[ret.abs() > 0.35]
    if len(ext):
        issues.append(f"{len(ext)} daily moves >35% (check for adjustment errors)")

    # Calendar gaps
    if len(df) > 60:
        gaps = df.index.to_series().diff().dt.days.dropna()
        big = int((gaps > 10).sum())
        if big:
            issues.append(f"{big} calendar gaps >10 days")

    # Staleness
    stale = int((df["close"].diff() == 0).sum())
    if stale > len(df) * 0.10:
        issues.append(f"{stale}/{len(df)} bars with unchanged close "
                      f"({stale/len(df):.0%}) — possible stale quotes")
    return issues


def compare_providers(tickers: list[str], provs: list[str]) -> pd.DataFrame:
    """Fetch the same tickers from each provider and measure disagreement."""
    fns = providers._HISTORY_FNS  # noqa: SLF001
    rows = []
    for t in tickers:
        series = {}
        for p in provs:
            try:
                df = fns[p](t, config.HISTORY_DAYS)
            except Exception:  # noqa: BLE001
                df = pd.DataFrame()
            if not df.empty:
                series[p] = df
        if len(series) < 2:
            rows.append({"ticker": t, "providers": len(series),
                         "note": "need 2+ providers with data"})
            continue

        base = provs[0] if provs[0] in series else list(series)[0]
        for p, df in series.items():
            if p == base:
                continue
            a, b = series[base], df
            idx = a.index.intersection(b.index)
            if len(idx) < 60:
                rows.append({"ticker": t, "pair": f"{base} vs {p}",
                             "note": f"only {len(idx)} shared dates"})
                continue
            ca, cb = a.loc[idx, "close"], b.loc[idx, "close"]
            va, vb = a.loc[idx, "volume"], b.loc[idx, "volume"]
            px_dev = ((ca - cb).abs() / cb.replace(0, np.nan)).dropna()
            vol_ratio = (va / vb.replace(0, np.nan)).dropna()
            rows.append({
                "ticker": t,
                "pair": f"{base} vs {p}",
                "shared_days": len(idx),
                "px_median_dev_bps": round(float(px_dev.median() * 10_000), 2),
                "px_max_dev_%": round(float(px_dev.max() * 100), 3),
                "px_days_over_1%": int((px_dev > 0.01).sum()),
                "vol_median_ratio": round(float(vol_ratio.median()), 4),
                "vol_days_over_2x": int(((vol_ratio > 2) | (vol_ratio < 0.5)).sum()),
                "rows_a": len(a), "rows_b": len(b),
            })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", type=str, default="")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--limit", type=int, default=12)
    args = ap.parse_args()

    print("=" * 72)
    print("Data Provenance & Integrity Audit")
    print("=" * 72)

    act = providers.active_providers()
    print(f"\nAvailable providers : {act}")
    print(f"Preferred           : {providers.preferred_provider()}")
    print(f"FMP key             : {'set' if config.FMP_API_KEY else 'MISSING'}")
    print(f"Polygon key         : {'set' if config.POLYGON_API_KEY else 'MISSING'}")
    print(f"Strict provenance   : {config.STRICT_CACHE_PROVENANCE}")

    a = providers.cache_audit()
    print(f"\nCached series       : {a['cached_series']}")
    print(f"By provider         : {a['by_provider']}")
    print(f"Mixed provenance    : {a['mixed']}")
    n_stale = len(a["not_from_preferred"])
    if n_stale:
        print(f"Not from preferred  : {n_stale} series will be refetched on next run")

    if a["by_provider"].get("polygon", 0) == 0:
        print("\n  >> NOT running on Polygon data. Two consequences that matter:")
        print("     1. Yahoo does not reliably split-adjust volume, and four")
        print("        metrics are volume-based (CMF, volume z, absorption, A/D).")
        print("     2. Yahoo deletes delisted tickers, which is the main source")
        print("        of survivorship bias in the breadth calculation.")
        print("     Add POLYGON_API_KEY to .env and rerun to fix both.")

    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               or (config.TIER1 + ["SMH", "XOP", "GDX"])[: args.limit])

    print(f"\n{'-'*72}\nIntegrity checks on cached bars ({len(tickers)} tickers)\n{'-'*72}")
    clean = 0
    for t in tickers:
        df = providers.history(t, max_age_hours=24 * 365)
        src = providers.cache_provider(t) or "unknown"
        iss = integrity_checks(t, df)
        if iss:
            print(f"  {t:<6} [{src:<7}] {len(df):>5} bars  " + "; ".join(iss))
        else:
            clean += 1
            print(f"  {t:<6} [{src:<7}] {len(df):>5} bars  ok")
    print(f"\n  {clean}/{len(tickers)} series clean")

    if args.compare or len(act) > 1:
        if len(act) < 2:
            print(f"\n{'-'*72}")
            print("Provider comparison skipped: only one provider available.")
            print("Add a Polygon or FMP key to compare against Yahoo and quantify")
            print("how much the free data was distorting the metrics.")
        else:
            print(f"\n{'-'*72}\nProvider comparison ({' vs '.join(act)})\n{'-'*72}")
            cmp = compare_providers(tickers[:8], act)
            if not cmp.empty:
                print(cmp.to_string(index=False))
                if "vol_median_ratio" in cmp.columns:
                    vr = cmp["vol_median_ratio"].dropna()
                    bad = cmp[cmp.get("vol_days_over_2x", 0) > 5] if "vol_days_over_2x" in cmp else pd.DataFrame()
                    print(f"\n  Volume ratio median across tickers: {vr.median():.4f} "
                          f"(1.0 = perfect agreement)")
                    if len(bad):
                        print(f"  ! {len(bad)} tickers disagree on volume by >2x on "
                              f"more than 5 days — volume-based metrics are affected")
                    px = cmp["px_median_dev_bps"].dropna()
                    if len(px):
                        print(f"  Price median deviation: {px.median():.2f} bps "
                              f"({'negligible' if px.median() < 5 else 'MATERIAL'})")
                out = config.OUTPUT_DIR / "data_audit.csv"
                cmp.to_csv(out, index=False)
                print(f"\n  Saved: {out}")

    print(f"\n{'-'*72}")
    if a["by_provider"].get("polygon", 0) > 0 and not a["mixed"]:
        print("Verdict: provenance is clean and Polygon-sourced. Backtest results")
        print("         can be treated as reliable on the data dimension.")
    elif a["mixed"]:
        print("Verdict: MIXED provenance. Delete data/ and refetch from a single")
        print("         provider before backtesting.")
    else:
        print("Verdict: usable for developing and sanity-checking the harness, but")
        print("         not for results you intend to act on. Add a Polygon key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
