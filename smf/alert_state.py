"""
Fire-once alert state — persist sector states between runs so a transition
alerts once, not on every refresh.

WHY
---
`build_flags` and the phase classifier recompute from scratch each run, so the
webhook re-posted the same "XLE entered Distribution" every night for as long as
it held the state. This module remembers the last run's state per ticker and
emits an alert only on a genuine CHANGE — the pattern borrowed from the
SECTOR_MOMENTUM repo's state.json / state_transitions.jsonl.

Two guards, because the two alert kinds behave differently:

  STATE transitions (phase, stage, quadrant) are edge events: fire when the
  value changes. A FLAP GUARD suppresses re-entry into the same state within
  `cooldown` sessions, because these signals are slow (Mansfield is a 200-day
  construct) and a sub-monthly round trip is far more likely to be the
  classifier wobbling than a real move. Default 21 sessions = the horizon the
  system was validated on (fwd_21), matching TREND_HORIZON_FAST.

  THRESHOLD flags (unusual volume, block prints, ...) are momentary. They are
  edge-triggered — a flag fires only on the run it first appears, stays silent
  while it persists, and re-arms once it clears — with a shorter cooldown, since
  a genuine second volume spike a week later really is news.

The core (`reconcile`) is a pure function: it takes the previous state dict and
returns the fired alerts plus the new state, with no file I/O, so it is unit
testable without a filesystem. `load`/`save` are thin wrappers.

FIRST RUN seeds silently: with no prior state, every sector would look "new", so
the first sighting of a ticker records its state and fires nothing. Alerts begin
on the first genuine change thereafter. This also makes the whole thing
idempotent — running twice on the same data fires nothing the second time.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import numpy as np

from . import config

STATE_VERSION = 2

# Categorical dimensions tracked as transitions. Value read straight off the
# sector dict; label field (for the alert text) is the *_label sibling if present.
_DIMS = (
    ("phase", "phase_label"),
    ("stage", "stage_label"),
    ("quadrant", None),
)

# Phase transitions worth an immediate (HIGH) alert rather than the digest.
# Mirrors config.WEBHOOK_MIN_PHASE but is explicit here so severity is local.
_HIGH_PHASES = {"CAPITAL_FLIGHT", "DISTRIBUTION",
                "STEALTH_ACCUMULATION", "CONFIRMED_BREAKOUT"}


def _sessions_between(a: str, b: str) -> int:
    """
    Trading days between two ISO dates, a <= b. Uses busday_count, which
    excludes weekends (not holidays — close enough for a flap guard, and it
    never under-counts in a way that would let a same-week flap through).
    """
    try:
        d1 = np.datetime64(a, "D")
        d2 = np.datetime64(b, "D")
    except Exception:  # noqa: BLE001
        return 10**6
    return int(np.busday_count(d1, d2))


def _severity(dim: str, to_val) -> str:
    if dim == "phase":
        return "high" if to_val in _HIGH_PHASES else "normal"
    if dim == "stage":
        return "high" if to_val == 4 else "normal"
    return "normal"


def _blank(ticker: str) -> dict:
    return {"phase": None, "stage": None, "quadrant": None,
            "flags": [], "fired": {}}


def reconcile(sectors: list[dict], prev: dict | None, as_of: str, *,
              cooldown: int | None = None,
              flag_cooldown: int | None = None) -> tuple[list[dict], dict]:
    """
    Diff current sector states against `prev`; return (fired, new_state).

    Side effect: annotates each sector with `s["new_today"]` — the alerts fired
    for that ticker this run — so the report can highlight fresh rows. Empty
    list when nothing changed.
    """
    cooldown = config.ALERT_COOLDOWN_SESSIONS if cooldown is None else cooldown
    flag_cooldown = (config.ALERT_FLAG_COOLDOWN_SESSIONS
                     if flag_cooldown is None else flag_cooldown)

    prev = prev or {}
    prev_tk = prev.get("tickers", {}) if isinstance(prev, dict) else {}
    seeding = not prev_tk                      # first run ever: record, don't alert

    # Start from the previous state and overlay only the tickers in THIS run, so
    # a partial run (--tier1, --tickers XLE) does not erase the state of tickers
    # it didn't touch and make them re-seed (and mis-fire) on the next full run.
    new_tk: dict[str, dict] = dict(prev_tk)
    fired: list[dict] = []

    for s in sectors:
        tk = s.get("ticker")
        if not tk:
            continue
        old = prev_tk.get(tk)
        rec = _blank(tk)
        # carry the fire-history forward so cooldowns span runs
        rec["fired"] = dict((old or {}).get("fired", {})) if old else {}
        emitted: list[dict] = []

        # ---- categorical transitions -----------------------------------
        for dim, label_key in _DIMS:
            cur = s.get(dim)
            rec[dim] = cur
            if old is None:
                continue                       # first sight of this ticker
            was = old.get(dim)
            if was is None or was == cur:
                continue                       # no change
            # Never fire a transition INTO a no-data sentinel. phase=None,
            # stage=0 ("insufficient history"), quadrant="Unknown" all mean the
            # data thinned out, not that the sector moved -- alerting "Stage 3 ->
            # 0" would be noise. Record the sentinel as the new state (so the
            # eventual recovery transition fires) but emit nothing now.
            if cur in (None, 0, "Unknown", ""):
                continue
            # Flap guard keys on the DESTINATION, not the from->to pair. The
            # alert is "XLE entered Capital Flight"; if it entered Capital Flight
            # a week ago we should stay quiet however it got back there. Keying on
            # from->to would miss a round trip through a different middle state
            # (CAPITAL_FLIGHT -> NEUTRAL -> CAPITAL_FLIGHT reads as two distinct
            # transitions and both would fire).
            key = f"{dim}={cur}"
            last = rec["fired"].get(key)
            if last is not None and _sessions_between(last, as_of) < cooldown:
                continue                       # flap: re-entered this state too recently
            rec["fired"][key] = as_of
            to_label = (s.get(label_key) if label_key else None) or str(cur)
            from_label = str(was)
            emitted.append({
                "ticker": tk, "name": s.get("name", tk),
                "dim": dim, "from": was, "to": cur,
                "severity": _severity(dim, cur),
                "text": f"{dim.capitalize()}: {from_label} -> {to_label}",
            })

        # ---- threshold flags (edge-triggered) --------------------------
        cur_flags = {f.get("kind"): f for f in (s.get("flags") or []) if f.get("kind")}
        prev_flags = set((old or {}).get("flags", [])) if old else set()
        rec["flags"] = sorted(cur_flags)
        if old is not None:
            for kind, flag in cur_flags.items():
                if kind in prev_flags:
                    continue                   # already active last run — not an edge
                key = f"flag:{kind}"
                last = rec["fired"].get(key)
                if last is not None and _sessions_between(last, as_of) < flag_cooldown:
                    continue
                rec["fired"][key] = as_of
                emitted.append({
                    "ticker": tk, "name": s.get("name", tk),
                    "dim": "flag", "kind": kind,
                    "severity": "high" if flag.get("level") in ("red", "orange") else "normal",
                    "text": flag.get("text", kind),
                })

        # prune fire-history older than 2x cooldown so the file stays bounded
        horizon = max(cooldown, flag_cooldown) * 2
        rec["fired"] = {k: v for k, v in rec["fired"].items()
                        if _sessions_between(v, as_of) <= horizon}

        s["new_today"] = [] if seeding else emitted
        if not seeding:
            fired.extend(emitted)
        new_tk[tk] = rec

    new_state = {"version": STATE_VERSION, "as_of": as_of, "tickers": new_tk}
    return fired, new_state


# ---------------------------------------------------------------------------
# thin I/O
# ---------------------------------------------------------------------------
def _path() -> Path:
    return config.CACHE_DIR / "alert_state.json"


def load(path: Path | None = None) -> dict:
    p = path or _path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # A schema bump invalidates the old file rather than mis-reading it; the
    # next run re-seeds silently, which is the safe direction (no false alerts).
    if not isinstance(d, dict) or d.get("version") != STATE_VERSION:
        return {}
    return d


def save(state: dict, path: Path | None = None) -> None:
    p = path or _path()
    try:
        p.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    except OSError:
        pass


def apply(sectors: list[dict], as_of: str, *, path: Path | None = None,
          persist: bool = True) -> list[dict]:
    """
    Load state, reconcile, save, return the fired alerts. The one call the
    pipeline makes. `persist=False` (used by --offline reruns that should not
    advance the alert clock) reconciles for display without writing.
    """
    if not getattr(config, "ALERT_STATE_ENABLED", True):
        for s in sectors:
            s["new_today"] = []
        return []
    prev = load(path)
    fired, new_state = reconcile(sectors, prev, as_of)
    if persist:
        save(new_state, path)
    return fired
