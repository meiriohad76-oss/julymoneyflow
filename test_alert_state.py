#!/usr/bin/env python3
"""
Fire-once alert state tests.

The whole value of this module is in what it does NOT do — not re-firing a
standing signal, not firing 32 alerts on a cold start, not letting a sector flap
in and out of a state twice a week. So most of these assert silence.

    python test_alert_state.py
"""
from __future__ import annotations

import sys

from smf import alert_state as A
from smf import config

passed = failed = 0


def ok(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {msg}")


def sect(ticker, phase="NEUTRAL", stage=1, quadrant="Lagging", flags=None):
    return {"ticker": ticker, "name": ticker, "phase": phase,
            "phase_label": phase.title() if phase else None, "stage": stage,
            "stage_label": f"Stage {stage}", "quadrant": quadrant,
            "flags": flags or []}


CD = config.ALERT_COOLDOWN_SESSIONS
print(f"\ncooldown = {CD} sessions, flag cooldown = {config.ALERT_FLAG_COOLDOWN_SESSIONS}")

print("\n=== 1. cold start seeds silently, fires nothing ===")
secs = [sect("XLE", "DISTRIBUTION", 3, "Leading"), sect("XLK", "NEUTRAL")]
fired, state = A.reconcile(secs, None, "2026-07-01")
ok(fired == [], f"cold start fires nothing (got {len(fired)})")
ok(all(s["new_today"] == [] for s in secs), "no sector marked new on cold start")
ok(set(state["tickers"]) == {"XLE", "XLK"}, "both tickers recorded")
ok(state["tickers"]["XLE"]["phase"] == "DISTRIBUTION", "state captured")

print("\n=== 2. no change => no alert (the core anti-spam property) ===")
fired2, state2 = A.reconcile(
    [sect("XLE", "DISTRIBUTION", 3, "Leading"), sect("XLK", "NEUTRAL")],
    state, "2026-07-02")
ok(fired2 == [], f"unchanged states fire nothing (got {len(fired2)})")

print("\n=== 3. a genuine transition fires once ===")
fired3, state3 = A.reconcile(
    [sect("XLE", "CAPITAL_FLIGHT", 4, "Lagging"), sect("XLK", "NEUTRAL")],
    state2, "2026-07-03")
xle = [a for a in fired3 if a["ticker"] == "XLE"]
ok(len(xle) >= 1, "XLE transition fired")
ok(any(a["dim"] == "phase" and a["to"] == "CAPITAL_FLIGHT" for a in xle),
   "phase transition captured")
ok(any(a["dim"] == "stage" and a["to"] == 4 for a in xle), "stage transition captured")
ok(any(a["severity"] == "high" for a in xle), "capital flight is high severity")
ok(not any(a["ticker"] == "XLK" for a in fired3), "unchanged XLK stayed silent")

print("\n=== 4. the same state re-firing next run is suppressed ===")
fired4, state4 = A.reconcile(
    [sect("XLE", "CAPITAL_FLIGHT", 4, "Lagging")], state3, "2026-07-06")
ok(fired4 == [], f"holding the state fires nothing (got {len(fired4)})")

print("\n=== 5. flap guard: leave and re-enter within cooldown is suppressed ===")
# XLE: CAPITAL_FLIGHT -> NEUTRAL (a new transition, fires) ...
f5a, s5a = A.reconcile([sect("XLE", "NEUTRAL", 1, "Improving")], state4, "2026-07-08")
ok(any(a["to"] == "NEUTRAL" for a in f5a), "the exit transition itself fires")
# ... then NEUTRAL -> CAPITAL_FLIGHT again only 3 sessions later: same
# transition as step 3, still inside 21 sessions -> flap, suppressed.
f5b, s5b = A.reconcile([sect("XLE", "CAPITAL_FLIGHT", 4, "Lagging")], s5a, "2026-07-13")
flap = [a for a in f5b if a["dim"] == "phase" and a["to"] == "CAPITAL_FLIGHT"]
ok(flap == [], "re-entering CAPITAL_FLIGHT within cooldown is suppressed as a flap")

print("\n=== 6. after the cooldown passes, the same transition may fire again ===")
# Seed a fired record far in the past, then trip the same transition.
base = {"version": A.STATE_VERSION, "as_of": "2026-01-01", "tickers": {
    "XLE": {"phase": "NEUTRAL", "stage": 1, "quadrant": "Lagging", "flags": [],
            "fired": {"phase=CAPITAL_FLIGHT": "2026-05-01"}}}}
fired6, _ = A.reconcile([sect("XLE", "CAPITAL_FLIGHT", 4, "Lagging")], base, "2026-07-01")
ok(any(a["to"] == "CAPITAL_FLIGHT" for a in fired6),
   "a transition last fired 2 months ago is allowed to fire again")

print("\n=== 7. threshold flags are edge-triggered ===")
st = {"version": A.STATE_VERSION, "as_of": "2026-07-01", "tickers": {
    "XLE": {"phase": "NEUTRAL", "stage": 1, "quadrant": "Lagging",
            "flags": [], "fired": {}}}}
vol = [{"kind": "unusual_volume", "level": "yellow", "text": "Unusual volume +3s"}]
# rising edge: flag newly present -> fires
f7a, s7a = A.reconcile([sect("XLE", flags=vol)], st, "2026-07-02")
ok(any(a.get("kind") == "unusual_volume" for a in f7a), "new flag fires on its rising edge")
# still present next run -> no re-fire
f7b, s7b = A.reconcile([sect("XLE", flags=vol)], s7a, "2026-07-03")
ok(not any(a.get("kind") == "unusual_volume" for a in f7b),
   "a persisting flag does not re-fire")
# clears, then returns after the flag cooldown -> re-arms and fires
f7c, s7c = A.reconcile([sect("XLE", flags=[])], s7b, "2026-07-04")
f7d, s7d = A.reconcile([sect("XLE", flags=vol)], s7c, "2026-07-14")
ok(any(a.get("kind") == "unusual_volume" for a in f7d),
   "a cleared flag re-arms and fires again after cooldown")

print("\n=== 8. partial run does not wipe untouched tickers ===")
full = {"version": A.STATE_VERSION, "as_of": "2026-07-01", "tickers": {
    "XLE": {"phase": "DISTRIBUTION", "stage": 3, "quadrant": "Leading", "flags": [], "fired": {}},
    "XLK": {"phase": "NEUTRAL", "stage": 2, "quadrant": "Leading", "flags": [], "fired": {}}}}
_, s8 = A.reconcile([sect("XLE", "DISTRIBUTION", 3, "Leading")], full, "2026-07-02")
ok("XLK" in s8["tickers"], "a tier-1-only run preserves XLK's state")
ok(s8["tickers"]["XLK"]["phase"] == "NEUTRAL", "preserved state is intact")

print("\n=== 9. idempotent apply() round-trip through disk ===")
import tempfile, pathlib
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d) / "alert_state.json"
    s = [sect("XLE", "DISTRIBUTION", 3, "Leading")]
    A.reconcile  # noqa
    first = A.apply(s, "2026-07-01", path=p)          # seeds
    ok(first == [], "first apply seeds silently")
    s2 = [sect("XLE", "CAPITAL_FLIGHT", 4, "Lagging")]
    second = A.apply(s2, "2026-07-02", path=p)        # fires
    ok(len(second) >= 1, "second apply fires the change")
    third = A.apply(s2, "2026-07-03", path=p)         # unchanged -> silent
    ok(third == [], "third apply on unchanged data is silent")
    ok(p.exists() and p.stat().st_size > 0, "state file written")

print("\n=== 10. corrupt / wrong-version state file re-seeds, never crashes ===")
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d) / "alert_state.json"
    p.write_text("{ this is not json", encoding="utf-8")
    out = A.apply([sect("XLE", "DISTRIBUTION")], "2026-07-01", path=p)
    ok(out == [], "corrupt file -> seed silently, no alerts, no crash")
    p.write_text('{"version": 0, "tickers": {}}', encoding="utf-8")
    out2 = A.apply([sect("XLE", "DISTRIBUTION")], "2026-07-01", path=p)
    ok(out2 == [], "stale schema version -> re-seed silently")

print("\n=== 11. no-data sentinels never fire a transition ===")
seed = {"version": A.STATE_VERSION, "as_of": "2026-07-01", "tickers": {
    "XLE": {"phase": "DISTRIBUTION", "stage": 3, "quadrant": "Leading",
            "flags": [], "fired": {}}}}
# data thins out: phase None, stage 0, quadrant Unknown
gone = sect("XLE", phase=None, stage=0, quadrant="Unknown")
gone["phase"] = None
f11, s11 = A.reconcile([gone], seed, "2026-07-02")
ok(f11 == [], f"transitions into None/0/Unknown fire nothing (got {f11})")
ok(s11["tickers"]["XLE"]["stage"] == 0, "the sentinel is still recorded as state")

print("\n=== 12. text carries readable labels, not raw enums ===")
seed2 = {"version": A.STATE_VERSION, "as_of": "2026-07-01", "tickers": {
    "XLE": {"phase": "NEUTRAL", "stage": 2, "quadrant": "Leading", "flags": [], "fired": {}}}}
f12, _ = A.reconcile([sect("XLE", "CAPITAL_FLIGHT", 4, "Lagging")], seed2, "2026-07-02")
phase_ev = next(a for a in f12 if a["dim"] == "phase")
ok("->" in phase_ev["text"], "transition text shows a from->to arrow")
ok("Capital_Flight" in phase_ev["text"] or "CAPITAL_FLIGHT" in phase_ev["text"],
   "destination label present in text")

print(f"\n{passed} passed, {failed} failed\n")
sys.exit(1 if failed else 0)
