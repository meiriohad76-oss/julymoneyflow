"""
Composite scoring and phase classification.

Each raw metric is converted to a rolling z-score over a 252-day window so that
sectors with structurally different volatility are comparable, then combined
into a single Composite Sector Rotation Index (CSRI).

The CSRI answers one question: how much net institutional demand is this sector
absorbing right now, relative to its own history?

Phase classification then maps the metric configuration to one of four
actionable states, mirroring the Weinstein cycle:

    Phase 1  STEALTH_ACCUMULATION   still underperforming, but momentum, breadth
                                    and flow are turning up. Highest edge, lowest
                                    confirmation.
    Phase 2  CONFIRMED_BREAKOUT     Mansfield crossed above zero with breadth
                                    and money flow confirming. Trend is live.
    Phase 3  DISTRIBUTION           price still leads but momentum and flow are
                                    decaying. Institutions selling into strength.
    Phase 4  CAPITAL_FLIGHT         underperformance plus negative momentum and
                                    collapsing breadth. Avoid or short.
    NEUTRAL                         no clean configuration.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, metrics

PHASE_META = {
    "STEALTH_ACCUMULATION": {"label": "Stealth Accumulation", "level": "yellow", "rank": 1},
    "CONFIRMED_BREAKOUT":   {"label": "Confirmed Breakout",   "level": "green",  "rank": 0},
    "DISTRIBUTION":         {"label": "Distribution",         "level": "orange", "rank": 2},
    "CAPITAL_FLIGHT":       {"label": "Capital Flight",       "level": "red",    "rank": 3},
    "NEUTRAL":              {"label": "Neutral",              "level": "grey",   "rank": 4},
}


def _z_last(series: pd.Series, window: int = config.ZSCORE_WINDOW) -> float:
    """Latest rolling z-score of a series, clipped to +/-3."""
    s = series.dropna()
    if len(s) < 40:
        return float("nan")
    w = min(window, len(s))
    mu = s.tail(w).mean()
    sd = s.tail(w).std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return 0.0
    return float(np.clip((s.iloc[-1] - mu) / sd, -3.0, 3.0))


def institutional_flow_score(m: dict) -> tuple[float, dict, str]:
    """
    Composite institutional footprint in the range roughly -1..+1.

    Two regimes, and they are deliberately not blended 50/50:

      observed  Tick data is available. Off-exchange share, dark pool (ATS)
                share and block direction are measured, not inferred, so they
                carry 75% of the score. The daily-bar proxies stay in at 25% as
                a sanity anchor — tick coverage can truncate on heavy sessions,
                and a proxy that disagrees loudly with observed flow is worth
                seeing rather than discarding.

      proxied   No tick data. Falls back entirely to absorption, accumulation/
                distribution day balance and block concentration.

    Returns (score, component parts, regime label) so the dashboard can be
    honest about which sectors are measured and which are inferred.
    """
    raw = m["_raw"]
    proxy: dict[str, float] = {}

    absorp_z = _z_last(raw["absorption"])
    adb = m.get("ad_balance")
    block_z = m.get("block_intensity_z")

    if np.isfinite(absorp_z):
        proxy["absorption"] = float(np.tanh(absorp_z / 1.5))
    if adb is not None and np.isfinite(adb):
        proxy["ad_days"] = float(np.clip(adb * 2.5, -1, 1))
    if block_z is not None and np.isfinite(block_z):
        proxy["block_intensity"] = float(np.tanh(block_z / 2.0))

    def _blend(parts: dict[str, float], weights: dict[str, float]) -> float:
        usable = {k: w for k, w in weights.items() if k in parts}
        tot = sum(usable.values())
        if not tot:
            return float("nan")
        return sum(parts[k] * (w / tot) for k, w in usable.items())

    proxy_score = _blend(proxy, config.INST_FLOW_WEIGHTS)

    fl = m.get("flow")
    if fl:
        from .flow import flow_score
        obs_score, obs_parts = flow_score(fl)
        if np.isfinite(obs_score):
            parts = {**proxy, **obs_parts}
            if np.isfinite(proxy_score):
                score = 0.75 * obs_score + 0.25 * proxy_score
            else:
                score = obs_score
            return float(score), {k: round(v, 3) for k, v in parts.items()}, "observed"

    if not proxy or not np.isfinite(proxy_score):
        return float("nan"), {}, "none"
    return float(proxy_score), {k: round(v, 3) for k, v in proxy.items()}, "proxied"


def score_sector(m: dict) -> dict:
    """Attach z-scored components, the CSRI, and a phase classification."""
    raw = m["_raw"]

    components: dict[str, float] = {
        "mansfield_rs": _z_last(raw["mansfield"]),
        "rs_momentum": _z_last(raw["rs_mom"]),
        "breadth": _z_last(raw["breadth"]),
        "money_flow": _z_last(raw["cmf"]),
    }
    inst_score, inst_parts, inst_regime = institutional_flow_score(m)
    components["inst_flow"] = inst_score * 2.0 if np.isfinite(inst_score) else float("nan")
    m["inst_flow_regime"] = inst_regime

    # Redistribute the weight of any unavailable component.
    usable = {k: w for k, w in config.CSRI_WEIGHTS.items()
              if k in components and np.isfinite(components[k])}
    tot_w = sum(usable.values())
    if tot_w > 0:
        csri = sum(components[k] * (w / tot_w) for k, w in usable.items())
    else:
        csri = float("nan")

    m["components"] = {k: (round(v, 3) if np.isfinite(v) else None)
                       for k, v in components.items()}
    m["inst_flow_parts"] = inst_parts
    m["inst_flow_score"] = round(inst_score, 3) if np.isfinite(inst_score) else None
    m["csri"] = round(csri, 3) if np.isfinite(csri) else None
    m["csri_weights_used"] = {k: round(w / tot_w, 3) for k, w in usable.items()} if tot_w else {}

    # 21-day CSRI delta, recomputed on lagged series (approximation: shift the
    # component series back 21 sessions and rescore with the same weights).
    lagged: dict[str, float] = {}
    for key, series_key in (("mansfield_rs", "mansfield"), ("rs_momentum", "rs_mom"),
                            ("breadth", "breadth"), ("money_flow", "cmf")):
        s = raw[series_key].dropna()
        lagged[key] = _z_last(s.iloc[:-21]) if len(s) > 60 else float("nan")
    lagged["inst_flow"] = _z_last(raw["absorption"].dropna().iloc[:-21]) \
        if len(raw["absorption"].dropna()) > 60 else float("nan")
    lu = {k: w for k, w in config.CSRI_WEIGHTS.items()
          if k in lagged and np.isfinite(lagged[k])}
    if lu and m["csri"] is not None:
        tw = sum(lu.values())
        prev = sum(lagged[k] * (w / tw) for k, w in lu.items())
        m["csri_prev_21d"] = round(prev, 3)
        m["csri_delta_21d"] = round(csri - prev, 3)
    else:
        m["csri_prev_21d"] = None
        m["csri_delta_21d"] = None

    m["series"]["csri"] = _csri_history(raw, m["csri"])
    return m


def _csri_history(raw: dict, current: float | None) -> list[float]:
    """
    CSRI at each historical date, for the sparkline.

    Each component is z-scored on the SAME rolling window `_z_last` uses. Full-
    sample z-scoring would be a lookahead — the value shown for a date two years
    ago would have been computed partly from data that came after it.

    ONE COMPONENT CANNOT BE RECONSTRUCTED. The headline `inst_flow` blends
    observed tick data (75%) with the daily-bar proxies (25%), but tick coverage
    only goes back ~14 sessions, so no earlier date can carry it. The history
    therefore uses the proxy alone, which is genuinely what would have been
    knowable at each past date.

    That leaves the final historical point differing from the headline by up to
    ~0.16, purely because today has better data than any past day did. A
    sparkline whose endpoint disagrees with the number printed beside it erodes
    trust in both, so the last point is set to the headline value: every point
    then shows the best information available as of its own date.
    """
    _np = np
    keys = (("mansfield_rs", "mansfield"), ("rs_momentum", "rs_mom"),
            ("breadth", "breadth"), ("money_flow", "cmf"),
            ("inst_flow", "absorption"))
    cols, weights = {}, {}
    w_all = config.CSRI_WEIGHTS
    for name, src in keys:
        s = raw.get(src)
        if s is None or not len(s.dropna()) or name not in w_all:
            continue
        s = s.dropna()
        win = min(config.ZSCORE_WINDOW, max(len(s), 1))
        mu = s.rolling(win, min_periods=40).mean()
        sd = s.rolling(win, min_periods=40).std(ddof=0).replace(0, _np.nan)
        cols[name] = ((s - mu) / sd).clip(-3.0, 3.0)
        weights[name] = w_all[name]
    if not cols:
        return []
    df = pd.DataFrame(cols)
    # Redistribute the weight of components missing on a given date, exactly as
    # the scalar path does, rather than treating a gap as a zero score.
    wser = pd.Series(weights)
    mask = df.notna()
    tot = mask.mul(wser, axis=1).sum(axis=1).replace(0, _np.nan)
    csri = df.mul(wser, axis=1).sum(axis=1, min_count=1) / tot
    out = metrics._spark(csri.dropna())
    if out and current is not None and np.isfinite(current):
        out[-1] = round(float(current), 3)
    return out


def classify_all(sectors: list[dict]) -> list[dict]:
    """
    Assign phases after every sector has been scored.

    The institutional-footprint condition is evaluated cross-sectionally rather
    than against a fixed cutoff. An absolute threshold does not survive contact
    with real markets: in a broad selloff every sector's absorption reading
    falls, so a fixed bar silently switches off the stealth-accumulation signal
    exactly when rotation is most worth detecting. What matters is which sectors
    are absorbing supply *relative to the rest of the tape right now*.
    """
    for tier in (1, 2):
        grp = [s for s in sectors if s.get("tier") == tier]
        vals = sorted(s["inst_flow_score"] for s in grp
                      if s.get("inst_flow_score") is not None)
        for s in grp:
            v = s.get("inst_flow_score")
            if v is None or not vals:
                s["inst_flow_pct"] = None
            else:
                below = sum(1 for x in vals if x < v)
                s["inst_flow_pct"] = round(100.0 * below / len(vals), 0)

    for s in sectors:
        s["phase"], s["phase_reasons"] = classify_phase(s)
        s["phase_label"] = PHASE_META[s["phase"]]["label"]
        s["phase_level"] = PHASE_META[s["phase"]]["level"]
        s["flags"] = build_flags(s)
    return sectors


def classify_phase(m: dict) -> tuple[str, list[str]]:
    """Rule-based phase assignment with human-readable reasons."""
    T = config.THRESHOLDS
    mrs = m.get("mansfield_rs")
    rsm = m.get("rs_momentum")
    br = m.get("breadth")
    cmf = m.get("cmf")
    inst = m.get("inst_flow_score")
    quad = m.get("quadrant")

    def ok(v) -> bool:
        return v is not None and np.isfinite(v)

    # Optional conditions: when a metric is unavailable (e.g. breadth was
    # skipped, or an ETF's constituents could not be fetched) the condition is
    # treated as "not disqualifying" rather than silently failing every rule.
    # Otherwise a missing feed would push the entire universe to NEUTRAL.
    #
    # STRICT_RULES inverts this. Correct for the live dashboard (degrade, don't go
    # silent) but WRONG for a backtest: relaxing the gate means the phases being
    # measured are not the phases the product emits. Running with breadth absent
    # and lenient rules produced 59 "Confirmed Breakout" episodes that had never
    # passed a breadth test at all, and their forward returns were then wrongly
    # attributed to the shipped classifier.
    strict = getattr(config, "STRICT_RULES", False)

    def opt(v, cond) -> bool:
        if not ok(v):
            return not strict          # strict: missing input disqualifies
        return cond(v)

    def note(v, text: str, missing: str) -> str:
        return text.format(v=v) if ok(v) else missing

    reasons: list[str] = []

    # --- Phase 2: confirmed breakout -------------------------------------
    if (ok(mrs) and mrs > T["p2_mansfield_min"]
            and opt(br, lambda v: v >= T["p2_breadth_min"])
            and ok(cmf) and cmf >= T["p2_cmf_min"]
            and quad in ("Leading", "Improving")):
        reasons = [
            f"Mansfield RS {mrs:+.2f} above zero (outperforming SPY trend)",
            note(br, "Breadth {v:.0f}% of constituents above 50d SMA",
                 "Breadth unavailable — not confirmed by participation"),
            f"Chaikin Money Flow {cmf:+.3f} confirms accumulation",
            f"RRG quadrant: {quad}",
        ]
        if m.get("mansfield_cross_up"):
            reasons.insert(0, "Mansfield RS crossed above zero within the last month")
        return "CONFIRMED_BREAKOUT", reasons

    # --- Phase 1: stealth accumulation -----------------------------------
    pct = m.get("inst_flow_pct")
    inst_ok = (ok(inst) and inst >= T["p1_instflow_floor"]
               and (pct is None or pct >= T["p1_instflow_pct_min"]))
    if (ok(mrs) and mrs <= T["p1_mansfield_max"]
            and ok(rsm) and rsm >= T["p1_rs_mom_min"]
            and opt(br, lambda v: v >= T["p1_breadth_min"])
            and inst_ok):
        reasons = [
            f"Mansfield RS still {mrs:+.2f} (has not yet crossed zero)",
            f"RS-Momentum {rsm:.1f} above 100 — relative strength accelerating vs peers",
            note(br, "Breadth {v:.0f}%" + f" ({m.get('breadth_chg_21d') or 0:+.0f}pp over 21d)",
                 "Breadth unavailable"),
            (f"Institutional footprint {inst:+.2f}"
             + (f" — {pct:.0f}th percentile of its tier" if pct is not None else "")
             + " — supply being absorbed quietly"),
        ]
        return "STEALTH_ACCUMULATION", reasons

    # --- Phase 3: distribution -------------------------------------------
    if (ok(mrs) and mrs > T["p3_mansfield_min"]
            and ok(rsm) and rsm < T["p3_rs_mom_max"]
            and ok(cmf) and cmf <= T["p3_cmf_max"]):
        reasons = [
            f"Mansfield RS {mrs:+.2f} still positive on price",
            f"RS-Momentum {rsm:.1f} below 100 — velocity decaying vs peers",
            f"Chaikin Money Flow {cmf:+.3f} negative despite price strength",
        ]
        if m.get("stage") == 3:
            reasons.append("Weinstein Stage 3 — choppy action around a flattening 50d MA")
        return "DISTRIBUTION", reasons

    # --- Phase 4: capital flight -----------------------------------------
    if (ok(mrs) and mrs <= T["p4_mansfield_max"]
            and ok(rsm) and rsm < T["p4_rs_mom_max"]
            and opt(br, lambda v: v <= T["p4_breadth_max"])):
        reasons = [
            f"Mansfield RS {mrs:+.2f} below zero",
            f"RS-Momentum {rsm:.1f} below 100 — no velocity support",
            note(br, "Breadth collapsed to {v:.0f}%", "Breadth unavailable"),
        ]
        return "CAPITAL_FLIGHT", reasons

    parts = []
    if ok(mrs):
        parts.append(f"Mansfield RS {mrs:+.2f}")
    if ok(rsm):
        parts.append(f"RS-Momentum {rsm:.1f}")
    if ok(br):
        parts.append(f"breadth {br:.0f}%")
    return "NEUTRAL", ["No clean configuration: " + ", ".join(parts)] if parts else ["Insufficient data"]


def build_flags(m: dict) -> list[dict]:
    """Discrete, notable events worth surfacing in the alert feed."""
    T = config.THRESHOLDS
    out: list[dict] = []

    if m.get("mansfield_cross_up"):
        out.append({"kind": "mansfield_cross", "level": "green",
                    "text": "Mansfield RS crossed above zero (structural outperformance began)"})

    vz = m.get("volume_z")
    if vz is not None and np.isfinite(vz) and vz >= T["volume_z_alert"]:
        out.append({"kind": "unusual_volume", "level": "yellow",
                    "text": f"Unusual volume: {vz:+.1f}σ vs 60-day baseline"})

    bz = m.get("block_intensity_z")
    if bz is not None and np.isfinite(bz) and bz >= T["block_z_alert"]:
        out.append({"kind": "block_activity", "level": "yellow",
                    "text": f"Block-print concentration {bz:+.1f}σ — volume arriving in outsized clips"})

    d = m.get("csri_delta_21d")
    if d is not None and np.isfinite(d):
        if d >= T["csri_delta_alert"]:
            out.append({"kind": "csri_surge", "level": "green",
                        "text": f"Composite score up {d:+.2f} over 21 sessions — inflow accelerating"})
        elif d <= -T["csri_delta_alert"]:
            out.append({"kind": "csri_slump", "level": "orange",
                        "text": f"Composite score down {d:+.2f} over 21 sessions — outflow accelerating"})

    ab = m.get("absorption")
    if ab is not None and np.isfinite(ab) and ab > 0.25 and (m.get("ret_21d") or 0) < 3.0:
        out.append({"kind": "absorption", "level": "yellow",
                    "text": "High-volume absorption with flat price — classic stealth bid"})

    # Observed tick-level flow flags
    dpt = m.get("dark_pool_trend")
    if dpt is not None and np.isfinite(dpt) and abs(dpt) > 0.015:
        lvl = "green" if dpt > 0 else "orange"
        verb = "rising" if dpt > 0 else "falling"
        out.append({"kind": "dark_pool", "level": lvl,
                    "text": (f"Dark pool (ATS) share {verb} {dpt*100:+.1f}pp vs recent average "
                             f"— now {(m.get('dark_pool_share') or 0)*100:.1f}% of volume")})

    oes = m.get("off_exchange_trend")
    if oes is not None and np.isfinite(oes) and oes > 0.02:
        out.append({"kind": "off_exchange", "level": "green",
                    "text": (f"Off-exchange volume share rising {oes*100:+.1f}pp "
                             f"— now {(m.get('off_exchange_share') or 0)*100:.1f}%")})

    bd = m.get("block_direction")
    if bd is not None and np.isfinite(bd) and abs(bd) > 0.15:
        lvl = "green" if bd > 0 else "red"
        side = "buy" if bd > 0 else "sell"
        out.append({"kind": "block_direction", "level": lvl,
                    "text": (f"Block prints skewed {side}-side ({bd:+.2f}) across "
                             f"{m.get('block_count') or 0} blocks — institutions crossing the spread")})

    lp = m.get("largest_print_notional")
    if lp is not None and np.isfinite(lp) and lp >= 25_000_000:
        out.append({"kind": "large_print", "level": "yellow",
                    "text": f"Single print of ${lp/1e6:.0f}M observed in the lookback window"})

    if m.get("stage") == 4:
        out.append({"kind": "stage4", "level": "red",
                    "text": "Weinstein Stage 4 — price below a declining 50d MA"})

    return out


# ---------------------------------------------------------------------------
# Cross-sectional views
# ---------------------------------------------------------------------------
def rank_sectors(sectors: list[dict]) -> list[dict]:
    """Rank by CSRI within tier and attach percentile position."""
    for tier in (1, 2):
        grp = [s for s in sectors if s.get("tier") == tier and s.get("csri") is not None]
        grp.sort(key=lambda s: s["csri"], reverse=True)
        n = len(grp)
        for i, s in enumerate(grp):
            s["rank_in_tier"] = i + 1
            s["tier_size"] = n
            s["percentile"] = round(100.0 * (n - i) / n, 0) if n else None
    return sectors


# A sector must stand this far from its peer average, in cross-sectional
# standard deviations, before it is called out as gaining or losing ground.
# Below this the sectors are not distinguishable from each other: on a recent
# run, nine of eleven tier-1 sectors sat inside a 0.67 band, and ranking those
# against one another produces confident-looking noise.
MATERIAL_Z = 0.5


def rotation_flow(sectors: list[dict], top_n: int = 4,
                  key: str = "vms") -> dict:
    """
    Two ranked lists — gaining ground and losing ground — NOT a set of pairs.

    This replaced a version that zipped the Nth-strongest sector with the
    Nth-weakest and drew an arrow between them. That pairing was arbitrary: there
    is no relationship between the 3rd-best and 3rd-worst sector, yet an arrow
    asserts one. It also produced claims the data contradicts — arrows pointing
    INTO sectors scoring below their own peer average, and in one case an arrow
    from a sector in the Improving quadrant to one in Lagging, which is backwards.

    The honest statement is weaker and clearer: these sectors are gaining
    relative strength, those are losing it. Whether a dollar left one and arrived
    in the other is unobserved.

    IMPORTANT — still an INFERENCE. No data source shows dollars leaving one
    sector and arriving in another. What is measured is relative performance.
    The rotation reading rests on the premise that institutions fund new
    positions by selling old ones: plausible, and unobserved here.

    Ranked by `vms` (holdout IC 0.036, Sharpe 0.530) rather than `csri`, which
    failed validation — ranking by a score with no predictive power would
    produce a confident-looking ordering built on noise.
    """
    scored = [s for s in sectors if s.get(key) is not None]
    if len(scored) < 4:
        return {"gaining": [], "losing": [], "score_used": key,
                "n_total": len(scored), "n_material": 0, "note": None}

    def _entry(x: dict) -> dict:
        v = float(x[key])
        return {"ticker": x["ticker"], "name": x["name"],
                "score": round(v, 3),
                "material": bool(abs(v) >= MATERIAL_Z),
                "mom_12_1": x.get("mom_12_1"),
                "quadrant": x.get("quadrant"),
                "quadrant_days": x.get("quadrant_days"),
                "stage_label": x.get("stage_label"),
                "phase": x.get("phase_label")}

    ranked = sorted(scored, key=lambda s: -float(s[key]))
    gaining = [_entry(s) for s in ranked if float(s[key]) > 0][:top_n]
    losing = [_entry(s) for s in reversed(ranked) if float(s[key]) < 0][:top_n]

    n_material = sum(1 for s in scored if abs(float(s[key])) >= MATERIAL_Z)
    n_middle = len(scored) - n_material
    note = None
    if n_middle:
        note = (f"{n_material} of {len(scored)} sectors stand more than "
                f"{MATERIAL_Z:g} standard deviations from the peer average. The "
                f"other {n_middle} sit inside that band, which is not a "
                f"distinguishable difference — treat their ordering as noise.")
    return {"gaining": gaining, "losing": losing, "score_used": key,
            "n_total": len(scored), "n_material": n_material,
            "material_z": MATERIAL_Z, "note": note}


def market_regime(bench_df: pd.DataFrame, sectors: list[dict]) -> dict:
    """
    Top-level 'macro weather': is the tape supporting risk at all?

    A sector rotation signal means something different in a market trading above
    a rising 200d MA than in one below it, so this gates interpretation of
    everything below.
    """
    close = bench_df["close"]
    sma50 = close.rolling(50, min_periods=50).mean()
    sma200 = close.rolling(200, min_periods=200).mean()
    px = float(close.iloc[-1])

    above50 = pd.notna(sma50.iloc[-1]) and px > float(sma50.iloc[-1])
    above200 = pd.notna(sma200.iloc[-1]) and px > float(sma200.iloc[-1])
    slope200 = ((float(sma200.iloc[-1]) / float(sma200.iloc[-42]) - 1) * 100
                if len(sma200.dropna()) > 42 else 0.0)

    t1 = [s for s in sectors if s.get("tier") == 1]
    risk_on_names = {"XLK", "XLY", "XLC", "XLI", "XLF"}
    defensive = {"XLP", "XLU", "XLV", "XLRE"}
    ron = np.nanmean([s["csri"] for s in t1
                      if s["ticker"] in risk_on_names and s.get("csri") is not None] or [np.nan])
    rdef = np.nanmean([s["csri"] for s in t1
                       if s["ticker"] in defensive and s.get("csri") is not None] or [np.nan])
    tilt = float(ron - rdef) if np.isfinite(ron) and np.isfinite(rdef) else float("nan")

    if above200 and above50 and slope200 > 0:
        regime, note = "RISK-ON", "Benchmark above a rising 200d MA — trend-following exposure supported"
    elif above200 and not above50:
        regime, note = "PULLBACK", "Above the 200d but below the 50d — correction inside an uptrend"
    elif not above200 and slope200 > 0:
        regime, note = "CAUTION", "Below the 200d MA but the long trend is still rising — mixed"
    else:
        regime, note = "RISK-OFF", "Below a flat or declining 200d MA — capital preservation regime"

    return {
        "regime": regime,
        "note": note,
        "benchmark": config.BENCHMARK,
        "price": round(px, 2),
        "above_sma50": bool(above50),
        "above_sma200": bool(above200),
        "sma200_slope_42d": round(slope200, 2),
        "risk_on_tilt": round(tilt, 3) if np.isfinite(tilt) else None,
        "tilt_note": ("Cyclical/growth sectors are absorbing more flow than defensives"
                      if np.isfinite(tilt) and tilt > 0.15 else
                      "Defensive sectors are absorbing more flow than cyclicals"
                      if np.isfinite(tilt) and tilt < -0.15 else
                      "No decisive cyclical-vs-defensive tilt"),
    }
