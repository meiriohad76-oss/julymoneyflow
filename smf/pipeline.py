"""
Orchestration: fetch -> compute -> score -> render.
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from . import config, flow, macro, metrics, providers, report, scoring


def _constituent_universe(tickers: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Resolve the constituent list for each sector ETF and the flat fetch list."""
    per_sector: dict[str, list[str]] = {}
    flat: set[str] = set()
    for t in tickers:
        names = providers.etf_constituents(t)
        per_sector[t] = names
        flat.update(names)
    return per_sector, sorted(flat)


def run(tickers: list[str] | None = None,
        skip_breadth: bool = False,
        max_age_hours: float = 12.0,
        quiet: bool = False) -> dict:
    t0 = time.time()
    tickers = tickers or config.ALL_TICKERS

    def log(msg: str) -> None:
        if not quiet:
            print(msg)

    log("=" * 66)
    log("Smart Money · Sector Transition Dashboard")
    log("=" * 66)
    status = providers.provider_status()
    log(f"Providers: {' > '.join(status['active_order'])}"
        f"   dark-pool tick data: {'ON' if status['off_exchange'] else 'off'}")

    # ---- 1. benchmark ------------------------------------------------------
    log(f"\n[1/5] Benchmark {config.BENCHMARK}")
    bench = providers.history(config.BENCHMARK, max_age_hours=max_age_hours)
    if bench.empty or len(bench) < 250:
        raise RuntimeError(
            f"Could not fetch enough history for the benchmark {config.BENCHMARK}. "
            "Check your network connection or set FMP_API_KEY / POLYGON_API_KEY in .env"
        )
    log(f"      {len(bench)} sessions, {bench.index[0].date()} → {bench.index[-1].date()}")

    # ---- 2. sector ETFs ----------------------------------------------------
    log(f"\n[2/5] Sector & industry ETFs ({len(tickers)})")
    etf = providers.batch_history(tickers, max_age_hours=max_age_hours, label="")
    missing = [t for t in tickers if t not in etf]
    if missing:
        log(f"      missing: {', '.join(missing)}")

    # ---- 3. constituents for breadth --------------------------------------
    con_map: dict[str, list[str]] = {}
    con_px: dict[str, pd.DataFrame] = {}
    if not skip_breadth:
        log(f"\n[3/5] Constituent prices for breadth")
        con_map, flat = _constituent_universe(list(etf.keys()))
        log(f"      {len(flat)} unique constituents across {len(con_map)} ETFs")
        con_px = providers.batch_history(flat, max_age_hours=max_age_hours * 2, label="")
    else:
        log("\n[3/5] Breadth skipped (--no-breadth)")

    # ---- 4. metrics + scoring ---------------------------------------------
    # ---- 3b. real order flow (Polygon tick data) ---------------------------
    flows: dict[str, dict] = {}
    if config.ENABLE_POLYGON_OFF_EXCHANGE and config.POLYGON_API_KEY:
        want = [t for t in config.OFF_EXCHANGE_TICKERS if t in etf]
        log(f"\n[3b] Tick-level order flow (cached sessions only; "
            f"run backfill_flow.py to populate)")
        flows = flow.collect(want, {t: etf[t].index for t in want},
                            quiet=quiet, fetch_missing=False)
        log(f"      observed flow for {len(flows)}/{len(want)} ETFs"
            + ("" if flows else " — none cached yet, using daily-bar proxies"))
    elif config.ENABLE_POLYGON_OFF_EXCHANGE:
        log("\n[3b] Tick-level flow enabled but POLYGON_API_KEY is missing — "
            "falling back to daily-bar proxies")

    # ---- 3c. short interest (free on the standard Polygon key) -------------
    si_map: dict[str, "pd.DataFrame"] = {}
    if config.POLYGON_API_KEY:
        log(f"\n[3c] Short interest ({len(etf)} ETFs)")
        for t in etf:
            d = providers.short_interest(t)
            if not d.empty:
                si_map[t] = d
        log(f"      {len(si_map)}/{len(etf)} ETFs with short-interest history")

    log(f"\n[4/5] Computing metrics")
    sectors: list[dict] = []
    for t, df in etf.items():
        closes = {c: con_px[c]["close"] for c in con_map.get(t, []) if c in con_px}
        sectors.append(metrics.compute_sector_metrics(t, df, bench, closes,
                                                     flows.get(t), si_map.get(t)))

    # RRG coordinates are normalised across the peer group, so this must happen
    # after every sector's raw series exists — and before scoring, which reads
    # the RS-Momentum series as a composite component.
    sectors = metrics.finalise_rrg(sectors)
    sectors = [scoring.score_sector(s) for s in sectors]
    # Phase assignment is also cross-sectional (institutional footprint is
    # ranked within tier), so it runs as a second pass.
    sectors = scoring.classify_all(sectors)

    sectors = scoring.rank_sectors(sectors)
    sectors.sort(key=lambda s: -(s.get("vms") if s.get("vms") is not None else -9))
    regime = scoring.market_regime(bench, sectors)

    # ---- macro weather: FRED central-bank liquidity (framework Step 1) ------
    log("\n[4b] Macro weather (FRED liquidity series)")
    try:
        macro_w = macro.macro_weather()
        log(f"      liquidity {macro_w['regime']} "
            f"(impulse {macro_w['liquidity_impulse']}, "
            f"{macro_w['components_used']} series)")
    except Exception as exc:  # noqa: BLE001
        log(f"      ! FRED unavailable ({type(exc).__name__}) — price-based regime only")
        macro_w = {"regime": "UNKNOWN", "liquidity_impulse": None,
                   "note": "FRED unavailable", "series": {}}
    regime["macro"] = macro_w
    # NOT named `flow` — that shadows the `smf.flow` module imported above, and
    # the shadow only bites further up the function where `flow.collect` is called.
    rotation = scoring.rotation_flow([s for s in sectors if s["tier"] == 1])

    payload = {
        "sectors": sectors,
        "regime": regime,
        "flow": rotation,
        "meta": {
            "as_of": bench.index[-1].strftime("%Y-%m-%d"),
            "generated_at": report.timestamp(),
            "providers": status,
            "off_exchange": bool(flows),
            "flow_coverage": sorted(flows.keys()),
            "block_min_shares": config.BLOCK_MIN_SHARES,
            "block_min_notional": config.BLOCK_MIN_NOTIONAL,
            "n_tier1": sum(1 for s in sectors if s["tier"] == 1),
            "n_tier2": sum(1 for s in sectors if s["tier"] == 2),
            "breadth_sample": max((s["n_constituents"] for s in sectors), default=0),
            "benchmark": config.BENCHMARK,
            "weights": config.CSRI_WEIGHTS,
            "data_quality": {
                "requested": len(tickers),
                "ok": len(etf),
                "missing": missing,
                "constituents_ok": len(con_px),
            },
            "runtime_sec": None,
        },
    }

    # ---- 5. output ---------------------------------------------------------
    log(f"\n[5/5] Rendering")
    payload["meta"]["runtime_sec"] = round(time.time() - t0, 1)
    html = report.write_report(payload)
    js = report.write_json(payload)
    log(f"      {html}")
    log(f"      {js}")

    _summary(sectors, regime, log)
    log(f"\nDone in {payload['meta']['runtime_sec']}s")
    return payload


def _summary(sectors: list[dict], regime: dict, log) -> None:
    log("\n" + "-" * 66)
    log(f"MACRO WEATHER: {regime['regime']} — {regime['note']}")
    log("-" * 66)

    def fmt(s: dict) -> str:
        vms = "  n/a" if s.get("vms") is None else f"{s['vms']:+5.2f}"
        csri = "  n/a" if s.get("csri") is None else f"{s['csri']:+5.2f}"
        mrs = "  n/a" if s.get("mansfield_rs") is None else f"{s['mansfield_rs']:+6.2f}"
        br = "n/a" if s.get("breadth") is None or s["breadth"] != s["breadth"] else f"{s['breadth']:3.0f}%"
        mom = "  n/a" if s.get("mom_12_1") is None else f"{s['mom_12_1']:+6.1f}%"
        return (f"  {s['ticker']:<6}{s['name'][:24]:<25} VMS {vms}  mom12-1 {mom}  "
                f"CSRI {csri}  breadth {br}  {s['phase_label']}")

    for phase in ("CONFIRMED_BREAKOUT", "STEALTH_ACCUMULATION", "DISTRIBUTION", "CAPITAL_FLIGHT"):
        grp = [s for s in sectors if s["phase"] == phase]
        if not grp:
            continue
        log(f"\n{scoring.PHASE_META[phase]['label'].upper()}  ({len(grp)})")
        for s in grp:
            log(fmt(s))

    log("\nTOP 5 BY VMS (validated momentum score)")
    for s in sectors[:5]:
        log(fmt(s))
    log("\nBOTTOM 5 BY VMS")
    for s in [x for x in sectors if x.get("vms") is not None][-5:]:
        log(fmt(s))
