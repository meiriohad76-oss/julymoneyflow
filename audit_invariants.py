#!/usr/bin/env python3
"""
Invariant audit — hunts for bugs by asserting things that must be true.

Different in kind from the unit tests: those check that a function does what it
was written to do. These check that the SYSTEM is self-consistent — that a
number shown in one place agrees with the same number derived another way, that
nothing on the dashboard could have been computed from data that did not exist
yet, and that the display layer has not silently diverged from the source.

    python audit_invariants.py
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from smf import config, metrics, providers, scoring  # noqa: E402

SNAP = Path("output/snapshot.json")
issues: list[tuple[str, str]] = []
checks = 0


def bad(sev: str, msg: str) -> None:
    issues.append((sev, msg))


def ok(cond: bool, sev: str, msg: str) -> None:
    global checks
    checks += 1
    if not cond:
        bad(sev, msg)


def hdr(t: str) -> None:
    print(f"\n=== {t} ===")


def main() -> int:
    if not SNAP.exists():
        print("run `python run.py --offline` first")
        return 2
    d = json.loads(SNAP.read_text(encoding="utf-8"))
    S = d["sectors"]
    print(f"auditing {len(S)} sectors")

    # ------------------------------------------------------------------
    hdr("1. displayed value vs freshly recomputed value")
    # Every headline number should be reproducible from the cache. If one is not,
    # either the pipeline is non-deterministic or something is being mutated.
    sample = S[:6]
    for s in sample:
        t = s["ticker"]
        df = providers.history(t, max_age_hours=24 * 365)
        if df.empty:
            continue
        st, lab, ev = metrics.weinstein_stage(df)
        ok(st == s["stage"], "HIGH",
           f"{t}: stage {s['stage']} shown, {st} recomputed")
        mom = metrics.momentum_12_1(df["close"])
        ok(abs(mom - s["mom_12_1"]) < 0.05, "HIGH",
           f"{t}: mom_12_1 {s['mom_12_1']} shown, {mom:.2f} recomputed")
        # the series endpoint must equal the scalar it is drawn beside
        ms = metrics.momentum_12_1_series(df["close"]).dropna()
        if len(ms):
            ok(abs(float(ms.iloc[-1]) - mom) < 0.05, "HIGH",
               f"{t}: momentum series endpoint {ms.iloc[-1]:.2f} != scalar {mom:.2f}")
    print(f"  recomputed {len(sample)} sectors")

    # ------------------------------------------------------------------
    hdr("2. point-in-time correctness of the new history functions")
    # Appending future data must not change any past value. This is the check
    # that catches accidental lookahead.
    t = S[0]["ticker"]
    df = providers.history(t, max_age_hours=24 * 365)
    if not df.empty and len(df) > 400:
        cut = len(df) - 60
        full = metrics.stage_history(df, n=400)
        part = metrics.stage_history(df.iloc[:cut], n=400)
        common = full.index.intersection(part.index)
        # allow the warm-up tail of `part` to differ where windows are incomplete
        diff = (full.loc[common] != part.loc[common]).sum()
        ok(diff == 0, "CRITICAL",
           f"stage_history: {diff}/{len(common)} past values changed when future "
           f"data was appended — LOOKAHEAD")
        print(f"  stage_history: {len(common)} shared dates, {diff} differ")

        m_full = metrics.momentum_12_1_series(df["close"])
        m_part = metrics.momentum_12_1_series(df["close"].iloc[:cut])
        c2 = m_full.index.intersection(m_part.index)
        md = float((m_full.loc[c2] - m_part.loc[c2]).abs().max())
        ok(md < 1e-9, "CRITICAL",
           f"momentum_12_1_series: max past change {md} when future appended")
        print(f"  momentum series: max past change {md:.2e}")

    # ------------------------------------------------------------------
    hdr("3. sparkline endpoints vs printed values (all sectors, all series)")
    pairs = [("vms", "vms"), ("csri", "csri"), ("mansfield_rs", "mansfield"),
             ("breadth", "breadth"), ("cmf", "cmf"), ("rs_ratio", "rs_ratio"),
             ("rs_momentum", "rs_momentum"), ("absorption", "absorption"),
             ("ad_balance", "ad_balance"), ("stage", "stage"),
             ("volume_trend_pct", "volume_trend")]
    n = 0
    for s in S:
        for k, sk in pairs:
            v, arr = s.get(k), (s.get("series") or {}).get(sk)
            if v is None or not arr:
                continue
            n += 1
            ok(abs(arr[-1] - v) <= max(0.02, abs(v) * 0.02), "HIGH",
               f"{s['ticker']}/{k}: series ends {arr[-1]}, value is {v}")
    print(f"  checked {n} endpoints")

    # ------------------------------------------------------------------
    hdr("4. series lengths and the date axis")
    for s in S:
        ser = s.get("series") or {}
        if not ser:
            continue
        ok(len(ser.get("dates", [])) == len(ser.get("price", [])), "HIGH",
           f"{s['ticker']}: dates {len(ser.get('dates', []))} != price "
           f"{len(ser.get('price', []))}")
        if ser.get("dates"):
            ok(ser["dates"][-1] == s["as_of"], "MED",
               f"{s['ticker']}: last chart date {ser['dates'][-1]} != as_of {s['as_of']}")
            ok(ser["dates"] == sorted(ser["dates"]), "HIGH",
               f"{s['ticker']}: chart dates are not in order")
        for k, arr in ser.items():
            if k in ("dates", "rrg_dates"):
                continue
            ok(all(isinstance(x, (int, float)) and np.isfinite(x) for x in arr),
               "HIGH", f"{s['ticker']}/{k}: contains a non-finite value")
    # A decimated series has no date array of matching length — flag if anything
    # could try to index dates by a sparkline position.
    s0 = S[0]["series"]
    short = [k for k, v in s0.items()
             if k not in ("dates", "rrg_dates", "rrg_x", "rrg_y")
             and len(v) != len(s0.get("dates", []))]
    print(f"  {len(short)} decimated series have no matching date array: "
          f"{', '.join(short[:6])}{'...' if len(short) > 6 else ''}")

    # ------------------------------------------------------------------
    hdr("5. decimation preserves shape")
    idx = pd.bdate_range("2024-01-01", periods=300)
    for name, raw in (("ramp", pd.Series(np.arange(300.0), index=idx)),
                      ("step", pd.Series([0.0] * 150 + [1.0] * 150, index=idx)),
                      ("noisy", pd.Series(np.random.RandomState(0).randn(300), index=idx))):
        # Compare at MATCHED precision: _tail rounds to 4dp and _spark to 3dp,
        # so a naive comparison flags a 1e-4 rounding gap as a lost observation.
        # (It did. This tolerance is the fix, not a papering-over — verified that
        # the final index is genuinely retained by the linspace selection.)
        full = metrics._tail(raw, config.SPARK_LEN, dp=3)
        dec = metrics._spark(raw)
        tol = 5e-4
        ok(len(dec) <= config.SPARK_POINTS, "MED", f"{name}: decimated too long")
        ok(abs(dec[-1] - full[-1]) < tol, "HIGH",
           f"{name}: decimation lost the final observation")
        ok(abs(dec[0] - full[0]) < tol, "MED",
           f"{name}: decimation lost the first observation")
        ok(min(dec) >= min(full) - tol and max(dec) <= max(full) + tol, "MED",
           f"{name}: decimation invented a value outside the original range")
    print(f"  decimation: {config.SPARK_LEN} -> {config.SPARK_POINTS} points")

    # ------------------------------------------------------------------
    hdr("6. categorical duration is consistent with its own history")
    for s in S:
        dur, prev, since = (s.get("stage_days"), s.get("stage_prev"),
                            s.get("stage_since"))
        if dur is None:
            continue
        ok(dur >= 1, "HIGH", f"{s['ticker']}: stage_days {dur} < 1")
        ok(prev is None or prev != s["stage"], "HIGH",
           f"{s['ticker']}: stage_prev {prev} equals current stage {s['stage']}")
        ok((prev is None) == (since is None), "MED",
           f"{s['ticker']}: stage_prev and stage_since disagree on existence")
        qd = s.get("quadrant_days")
        if qd is not None:
            ok(qd >= 1, "HIGH", f"{s['ticker']}: quadrant_days {qd} < 1")
            ok(s.get("quadrant_prev") != s["quadrant"], "HIGH",
               f"{s['ticker']}: quadrant_prev equals current quadrant")

    # ------------------------------------------------------------------
    hdr("7. cross-sectional scores are actually cross-sectional")
    for tier in (1, 2):
        grp = [s for s in S if s["tier"] == tier and s.get("vms") is not None]
        if len(grp) < 4:
            continue
        mean = np.mean([s["vms"] for s in grp])
        ok(abs(mean) < 0.35, "MED",
           f"tier {tier}: VMS mean {mean:+.3f} — a cross-sectional z-score "
           f"should centre near zero")
        ranks = sorted(grp, key=lambda x: -x["vms"])
        for i, s in enumerate(ranks, 1):
            ok(s["vms_rank"] == i, "HIGH",
               f"{s['ticker']}: vms_rank {s['vms_rank']} but sorts at {i}")
        # VMS series must be centred at EVERY date, not just today
        lens = {len(s["series"]["vms"]) for s in grp if s["series"].get("vms")}
        ok(len(lens) <= 1, "HIGH",
           f"tier {tier}: VMS series lengths differ {lens} — the panel is not "
           f"aligned across the peer group")
        if len(lens) == 1:
            L = lens.pop()
            worst = max(abs(np.mean([s["series"]["vms"][i] for s in grp
                                     if s["series"].get("vms")]))
                        for i in range(L))
            ok(worst < 0.4, "MED",
               f"tier {tier}: VMS not centred at some date (worst |mean| {worst:.3f})")
        print(f"  tier {tier}: {len(grp)} sectors, VMS mean {mean:+.3f}")

    # ------------------------------------------------------------------
    hdr("8. rotation panel claims")
    f = d.get("flow") or {}
    for e in f.get("gaining", []):
        ok(e["score"] > 0, "HIGH", f"{e['ticker']} listed gaining with score {e['score']}")
    for e in f.get("losing", []):
        ok(e["score"] < 0, "HIGH", f"{e['ticker']} listed losing with score {e['score']}")
    gt = {e["ticker"] for e in f.get("gaining", [])}
    for e in f.get("losing", []):
        ok(e["ticker"] not in gt, "HIGH", f"{e['ticker']} on both sides")
    # material flag must match the stated threshold
    for e in f.get("gaining", []) + f.get("losing", []):
        ok(e["material"] == (abs(e["score"]) >= f["material_z"]), "HIGH",
           f"{e['ticker']}: material flag disagrees with the threshold")
    # the panel only ranks tier 1
    t1 = {s["ticker"] for s in S if s["tier"] == 1}
    for e in f.get("gaining", []) + f.get("losing", []):
        ok(e["ticker"] in t1, "HIGH", f"{e['ticker']} in rotation panel is not tier 1")
    print(f"  {len(f.get('gaining', []))} gaining, {len(f.get('losing', []))} losing, "
          f"{f.get('n_material')}/{f.get('n_total')} material")

    # ------------------------------------------------------------------
    hdr("9. flow panel — the collapse guard")
    fl = [s for s in S if s.get("flow")]
    if fl:
        same = sum(1 for s in fl
                   if s.get("dark_pool_share") == s.get("off_exchange_share"))
        print(f"  dark == off-exchange in {same}/{len(fl)} sectors")
        for s in fl:
            o = s.get("off_exchange_share")
            if o is not None:
                ok(0.0 <= o <= 1.0, "HIGH",
                   f"{s['ticker']}: off_exchange_share {o} outside 0-1")
            bs = s.get("block_share")
            if bs is not None:
                ok(0.0 <= bs <= 1.0, "HIGH",
                   f"{s['ticker']}: block_share {bs} outside 0-1")
            bd = s.get("block_direction")
            if bd is not None:
                ok(-1.0 <= bd <= 1.0, "HIGH",
                   f"{s['ticker']}: block_direction {bd} outside -1..1")
            ok((s["flow"].get("sessions") or 0) > 0, "MED",
               f"{s['ticker']}: flow row with zero sessions")

    # ------------------------------------------------------------------
    hdr("10. bounded metrics stay in bounds")
    bounds = {"breadth": (0, 100), "cmf": (-1, 1), "dtc_percentile": (0, 100),
              "percentile": (0, 100), "inst_flow_score": (-1.05, 1.05),
              "green_lights": (0, 3), "squeeze_score": (0, 3),
              "rs_ratio": (80, 120), "rs_momentum": (80, 120)}
    for s in S:
        for k, (lo, hi) in bounds.items():
            v = s.get(k)
            if v is None:
                continue
            ok(lo <= v <= hi, "HIGH", f"{s['ticker']}/{k} = {v} outside [{lo},{hi}]")

    # ------------------------------------------------------------------
    hdr("11. stage label agrees with stage number")
    for s in S:
        lab = s.get("stage_label") or ""
        if not lab or s.get("stage") in (None, 0):
            continue
        ok(lab.startswith(f"Stage {s['stage']}"), "HIGH",
           f"{s['ticker']}: stage {s['stage']} labelled '{lab}'")
        ev = s.get("stage_evidence") or {}
        if "price_above_sma50" in ev and "sma50_slope_21d_pct" in ev:
            # the guards the audit added: no Stage 2 on a clearly falling MA
            if s["stage"] == 2:
                ok(ev["sma50_slope_21d_pct"] > -0.25, "CRITICAL",
                   f"{s['ticker']}: Stage 2 (uptrend) on a 50d SMA sloping "
                   f"{ev['sma50_slope_21d_pct']}%")
            if s["stage"] == 1:
                ok(ev["sma50_slope_21d_pct"] < 0.80, "CRITICAL",
                   f"{s['ticker']}: Stage 1 (basing) on a 50d SMA sloping "
                   f"{ev['sma50_slope_21d_pct']}%")

    # ------------------------------------------------------------------
    hdr("12. no secrets or absolute paths leaked into the artefacts")
    html = Path("output/dashboard.html").read_text(encoding="utf-8", errors="ignore")
    key = (config.POLYGON_API_KEY or "").strip()
    if key:
        ok(key not in html, "CRITICAL", "POLYGON API KEY APPEARS IN dashboard.html")
        ok(key not in SNAP.read_text(encoding="utf-8"), "CRITICAL",
           "POLYGON API KEY APPEARS IN snapshot.json")
        print(f"  key ({len(key)} chars) absent from both artefacts")
    for pat in ("/sessions/", "C:\\\\Users", "/home/", "POLYGON_API_KEY="):
        ok(pat not in html, "MED", f"dashboard.html leaks a path/env token: {pat}")

    # ------------------------------------------------------------------
    hdr("13. fire-once alerts are internally consistent")
    fired = d.get("alerts_fired", [])
    print(f"  {len(fired)} alerts fired in the last build")
    tickers = {s["ticker"] for s in S}
    for a in fired:
        ok(a.get("ticker") in tickers, "HIGH",
           f"alert references unknown ticker {a.get('ticker')}")
        ok(a.get("severity") in ("high", "normal"), "MED",
           f"alert has odd severity {a.get('severity')}")
        ok(a.get("to") not in (None, 0, "Unknown", ""), "HIGH",
           f"{a.get('ticker')}: alert fired INTO a no-data sentinel ({a.get('to')})")
        ok("->" in a.get("text", "") or a.get("dim") == "flag", "MED",
           f"{a.get('ticker')}: transition text missing an arrow")
    # Reconciling the CURRENT snapshot against the persisted state must be a
    # no-op: the pipeline just saved that state, so a re-diff can't produce new
    # alerts. If it does, the save path and the read path disagree.
    from smf import alert_state as _as
    prev = _as.load()
    if prev:
        again, _ = _as.reconcile(S, prev, d["meta"]["as_of"])
        ok(again == [], "HIGH",
           f"re-diffing the saved state fires {len(again)} phantom alerts "
           f"(persist/read mismatch)")
        print(f"  re-diff against saved state: {len(again)} alerts (want 0)")

    # ------------------------------------------------------------------
    print(f"\n{'=' * 60}")
    crit = [m for s_, m in issues if s_ == "CRITICAL"]
    high = [m for s_, m in issues if s_ == "HIGH"]
    med = [m for s_, m in issues if s_ == "MED"]
    print(f"{checks} invariants checked")
    print(f"  CRITICAL {len(crit)}   HIGH {len(high)}   MED {len(med)}")
    for sev, group in (("CRITICAL", crit), ("HIGH", high), ("MED", med)):
        for m in group[:12]:
            print(f"  [{sev}] {m}")
        if len(group) > 12:
            print(f"  ... and {len(group) - 12} more {sev}")
    if not issues:
        print("\n  no invariant violations found")
    return 1 if (crit or high) else 0


if __name__ == "__main__":
    sys.exit(main())
