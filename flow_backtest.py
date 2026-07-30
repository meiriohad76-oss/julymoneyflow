#!/usr/bin/env python3
"""
Test the OBSERVED institutional-flow component against forward returns.

This answers the one question the daily-bar backtest could not: does real
off-exchange / dark-pool / block-print activity predict sector relative returns?

Requires `python backfill_flow.py` to have populated data/flow/ first.

The tests, and why each is here
-------------------------------
1. Rank IC of each observed flow metric, cross-sectionally per date. The direct
   measure of predictive power.

2. Comparison against the daily-bar PROXIES computed on the same dates. The
   proxies (absorption, A/D day balance, block concentration) are what the
   dashboard falls back on. If observed flow does not beat its own proxy, the
   entire tick-data layer is not worth its cost — that is the decision this makes.

3. Comparison against 12-1 momentum, which the composite already lost to. Any new
   component has to clear that bar to justify inclusion.

4. Sign stability across subperiods. A metric that flips sign is noise regardless
   of its full-sample IC — this is what disqualified the composite.

    python flow_backtest.py
    python flow_backtest.py --horizons 5,10,21
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from smf import backtest as bt
from smf import config, flow, metrics, providers
from smf import diagnostics as dg

FLOW_METRICS = ["off_exchange_share", "dark_pool_share", "block_share",
                "block_direction", "off_exchange_trend", "dark_pool_trend",
                "block_trend", "flow_score"]


def load_flow_panel(tickers: list[str], lookback: int) -> pd.DataFrame:
    """
    Build a (date, ticker) panel of observed flow metrics from cached summaries.

    A rolling window ending at each date is aggregated exactly as the live
    dashboard does, so what is tested here is what the product would use.
    """
    rows = []
    for t in tickers:
        files = sorted(Path(flow.FLOW_DIR).glob(f"{t}_*.json"))
        days = [f.stem.split("_", 1)[1] for f in files]
        days = sorted(d for d in days if len(d) == 10)
        if len(days) <= lookback:
            continue
        for i in range(lookback, len(days)):
            window = days[i - lookback:i]
            fl = flow.sector_flow(t, window, quiet=True)
            if not fl:
                continue
            score, parts = flow.flow_score(fl)
            rec = {"date": pd.Timestamp(days[i]), "ticker": t,
                   "flow_score": score if np.isfinite(score) else np.nan}
            for k in ("off_exchange_share", "dark_pool_share", "block_share",
                      "block_direction", "off_exchange_trend", "dark_pool_trend",
                      "block_trend"):
                rec[k] = fl.get(k)
            rows.append(rec)
    return pd.DataFrame(rows)


def attach_returns_and_proxies(panel: pd.DataFrame,
                              horizons: tuple[int, ...]) -> pd.DataFrame:
    """Forward relative returns plus the daily-bar proxies for the same dates."""
    bench = providers.history(config.BENCHMARK)
    b = bench["close"]
    out = []
    for t, g in panel.groupby("ticker"):
        px = providers.history(t)
        if px.empty:
            continue
        c = px["close"]
        # proxies, computed point-in-time from daily bars
        absorp = metrics.absorption_score(px)
        adb = metrics.ad_day_balance(px)
        blocki = metrics.block_intensity(px)
        cmf = metrics.chaikin_money_flow(px)
        for _, r in g.iterrows():
            d = r["date"]
            i = c.index.get_indexer([d], method="pad")[0]
            bi = b.index.get_indexer([d], method="pad")[0]
            if i < 0 or bi < 0:
                continue
            rec = r.to_dict()
            for h in horizons:
                if i + h < len(c) and bi + h < len(b):
                    sr = float(c.iloc[i + h] / c.iloc[i] - 1)
                    br = float(b.iloc[bi + h] / b.iloc[bi] - 1)
                    rec[f"fwd_{h}"] = (sr - br) * 100.0
            for nm, s in (("proxy_absorption", absorp), ("proxy_ad_balance", adb),
                          ("proxy_block_intensity", blocki), ("proxy_cmf", cmf)):
                v = s.reindex([c.index[i]]).iloc[0] if i < len(c) else np.nan
                rec[nm] = float(v) if pd.notna(v) else np.nan
            # 12-1 momentum benchmark
            h2 = c.iloc[:i + 1]
            rec["mom12_1"] = (float(h2.iloc[-22] / h2.iloc[-274] - 1) * 100
                              if len(h2) > 275 else np.nan)
            out.append(rec)
    return pd.DataFrame(out)


def rank_ic(d: pd.DataFrame, col: str, fwd: str, min_names: int = 5) -> tuple[float, int]:
    if col not in d.columns or fwd not in d.columns:
        return np.nan, 0
    ics = []
    for _, g in d.groupby("date"):
        g = g.dropna(subset=[col, fwd])
        if len(g) < min_names or g[col].nunique() < 3:
            continue
        v = g[col].corr(g[fwd], method="spearman")
        if np.isfinite(v):
            ics.append(v)
    return (float(np.mean(ics)) if ics else np.nan), len(ics)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", type=str, default="5,10,21")
    ap.add_argument("--lookback", type=int, default=config.OFF_EXCHANGE_LOOKBACK_DAYS)
    ap.add_argument("--min-names", type=int, default=5)
    args = ap.parse_args()
    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())

    print("=" * 74)
    print("Observed Institutional Flow — Predictive Test")
    print("=" * 74)

    tickers = list(config.OFF_EXCHANGE_TICKERS)
    cached = {}
    for t in tickers:
        n = len(list(Path(flow.FLOW_DIR).glob(f"{t}_*.json")))
        if n:
            cached[t] = n
    if not cached:
        print("\nNo cached flow summaries in data/flow/.")
        print("Run:  python backfill_flow.py --sessions 126")
        return 1

    print(f"\nCached sessions per ticker: "
          f"{ {k: v for k, v in sorted(cached.items())} }")
    usable = {k: v - args.lookback for k, v in cached.items() if v > args.lookback}
    if not usable:
        print(f"\nEvery ticker has <= {args.lookback} cached sessions, which is the "
              f"lookback. No observations can be formed yet.")
        print("Fetch more sessions with backfill_flow.py, then rerun.")
        return 1
    print(f"Usable observations available: {sum(usable.values()):,} "
          f"across {len(usable)} tickers")

    print("\nBuilding flow panel...")
    panel = load_flow_panel(list(usable), args.lookback)
    if panel.empty:
        print("Panel is empty.")
        return 1
    print(f"  {len(panel):,} (date, ticker) rows")

    print("Attaching forward returns and daily-bar proxies...")
    d = attach_returns_and_proxies(panel, horizons)
    if d.empty:
        print("No rows with forward returns.")
        return 1
    names_per_date = d.groupby("date").size().median()
    print(f"  {len(d):,} rows, {d['date'].nunique()} dates, "
          f"median {names_per_date:.0f} names per date")

    if names_per_date < args.min_names:
        print(f"\n  ! Median {names_per_date:.0f} names per date is below "
              f"{args.min_names}. Cross-sectional IC is not meaningful — backfill "
              f"more tickers, not more dates.")

    out = Path(config.OUTPUT_DIR)
    d.to_csv(out / "flow_panel.csv", index=False)

    # ---------------- IC tables ----------------
    lines: list[str] = []
    A = lines.append
    A("# Observed Institutional Flow — Predictive Test\n")
    A(f"- Panel: **{len(d):,} rows**, {d['date'].nunique()} dates, "
      f"{d['ticker'].nunique()} tickers, median {names_per_date:.0f} names/date")
    A(f"- Window: {d['date'].min().date()} → {d['date'].max().date()}")
    A(f"- Flow lookback: {args.lookback} sessions\n")

    observed = [c for c in FLOW_METRICS if c in d.columns]
    proxies = [c for c in d.columns if c.startswith("proxy_")]

    for h in horizons:
        fwd = f"fwd_{h}"
        if fwd not in d.columns:
            continue
        rows = []
        for c in observed + proxies + ["mom12_1"]:
            ic, n = rank_ic(d, c, fwd, args.min_names)
            kind = ("observed flow" if c in observed else
                    "daily-bar proxy" if c.startswith("proxy_") else "benchmark")
            rows.append({"metric": c, "kind": kind,
                         "rank_IC": round(ic, 4) if np.isfinite(ic) else None,
                         "dates": n})
        tbl = pd.DataFrame(rows).sort_values(
            "rank_IC", ascending=False, na_position="last")
        A(f"## Rank IC — {h}-session forward relative return\n")
        A(tbl.to_markdown(index=False) + "\n")

        best_obs = tbl[tbl["kind"] == "observed flow"]["rank_IC"].max()
        best_prx = tbl[tbl["kind"] == "daily-bar proxy"]["rank_IC"].max()
        mom = tbl[tbl["metric"] == "mom12_1"]["rank_IC"].iloc[0] \
            if len(tbl[tbl["metric"] == "mom12_1"]) else None
        A(f"- best observed flow metric: **{best_obs}**")
        A(f"- best daily-bar proxy: **{best_prx}**")
        A(f"- 12-1 momentum: **{mom}**")
        verdict = []
        if best_obs is not None and best_prx is not None:
            verdict.append("observed flow BEATS its proxy — the tick data earns its cost"
                           if best_obs > best_prx else
                           "observed flow does NOT beat its own daily-bar proxy — "
                           "the tick-data layer is not worth its cost")
        if best_obs is not None and mom is not None:
            verdict.append("and beats 12-1 momentum" if best_obs > mom
                           else "and does not clear the 12-1 momentum bar")
        A(f"- **{'; '.join(verdict)}**\n")

    # ---------------- sign stability ----------------
    A("## Sign stability across subperiods\n")
    A("_A metric that flips sign is noise regardless of its full-sample IC. "
      "This is what disqualified the composite._\n")
    nb = min(4, max(2, d["date"].nunique() // 20))
    if nb >= 2:
        d = d.copy()
        d["blk"] = pd.qcut(d["date"].rank(method="first"), nb,
                           labels=False, duplicates="drop")
        # Use the longest horizon that actually has data. The most recent dates in
        # the window have no forward return yet, so a long horizon can be entirely
        # empty even though shorter ones are fine.
        avail = [h for h in horizons
                 if f"fwd_{h}" in d.columns and d[f"fwd_{h}"].notna().sum() >= 20]
        h0 = max(avail) if avail else horizons[0]
        A(f"_Using the {h0}-session horizon (longest with sufficient data)._\n")
        rows = []
        for c in observed + ["mom12_1"]:
            vals = []
            for _, g in d.groupby("blk"):
                ic, _ = rank_ic(g, c, f"fwd_{h0}", args.min_names)
                vals.append(round(ic, 3) if np.isfinite(ic) else None)
            ok = [v for v in vals if v is not None]
            rows.append({"metric": c, "by_subperiod": str(vals),
                         "sign_stable": (len({np.sign(v) for v in ok}) == 1
                                         if len(ok) == len(vals) and ok else None)})
        A(pd.DataFrame(rows).to_markdown(index=False) + "\n")
    else:
        A("_Too few dates to split._\n")

    # ---------------- distribution sanity ----------------
    A("## Flow metric distributions\n")
    rows = []
    for c in observed:
        s = d[c].dropna()
        if len(s) < 20:
            continue
        desc = dg.describe(s, c)
        rows.append({"metric": c, "n": desc["n"], "mean": desc["mean"],
                     "median": desc["median"], "sd": desc["sd"],
                     "shape": desc["shape"], "tails": desc["tails"]})
    if rows:
        A(pd.DataFrame(rows).to_markdown(index=False) + "\n")

    A("## Caveats\n")
    A(f"- {len(d):,} observations over {d['date'].nunique()} dates is a short window. "
      "Flow metrics are noisy; treat a single-window IC as indicative, not settled.")
    A("- Off-exchange and dark-pool share are the same series in Polygon data "
      "(every off-exchange print carries a trf_id), so they are collinear by "
      "construction and the flow score collapses them into one term.")
    A("- Flow is measured on the ETF itself, not its constituents. A buyer "
      "accumulating individual names may not appear in ETF flow.")
    A("- No multiple-testing correction applied across the metric table above; "
      "with ~12 metrics x 3 horizons, expect ~2 spuriously 'best' cells.\n")

    rp = out / "flow_test_report.md"
    rp.write_text("\n".join(lines), encoding="utf-8")

    # ---------------- console summary ----------------
    print("\n" + "=" * 74)
    for h in horizons:
        fwd = f"fwd_{h}"
        if fwd not in d.columns:
            continue
        best = None
        for c in observed:
            ic, n = rank_ic(d, c, fwd, args.min_names)
            if np.isfinite(ic) and (best is None or ic > best[1]):
                best = (c, ic)
        pbest = None
        for c in proxies:
            ic, n = rank_ic(d, c, fwd, args.min_names)
            if np.isfinite(ic) and (pbest is None or ic > pbest[1]):
                pbest = (c, ic)
        mom, _ = rank_ic(d, "mom12_1", fwd, args.min_names)
        print(f"{h:>3}d: best observed {best[0] if best else '-'}="
              f"{best[1]:+.4f}" if best else f"{h}d: no observed IC", end="")
        if pbest:
            print(f" | best proxy {pbest[0]}={pbest[1]:+.4f}", end="")
        if np.isfinite(mom):
            print(f" | mom12-1={mom:+.4f}", end="")
        print()
    print(f"\nReport: {rp}")
    print(f"Panel:  {out/'flow_panel.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
