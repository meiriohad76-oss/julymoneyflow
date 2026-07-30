#!/usr/bin/env python3
"""
Smart Money · Sector Transition Dashboard

Usage
-----
    python run.py                    # full run, opens nothing
    python run.py --open             # run and open the dashboard in a browser
    python run.py --tier1            # GICS sectors only (fast)
    python run.py --no-breadth       # skip constituent fetch (much faster, no breadth metric)
    python run.py --fresh            # ignore cache, refetch everything
    python run.py --tickers XLE,XLK,SMH,URA
    python run.py --alert            # post triggered alerts to SMF_WEBHOOK_URL

API keys (optional but recommended) — create a file named `.env` next to this script:

    FMP_API_KEY=your_key
    POLYGON_API_KEY=your_key
    SMF_WEBHOOK_URL=https://hooks.slack.com/services/...

Without a key the system falls back to Yahoo's free chart endpoint, which covers
prices, volume and therefore every metric except real off-exchange flow.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import webbrowser
from pathlib import Path

from smf import config, pipeline, report


def post_webhook(payload: dict) -> None:
    url = config.WEBHOOK_URL
    if not url:
        print("No SMF_WEBHOOK_URL configured — skipping alert post.")
        return

    # Post ONLY what actually changed this run. alert_state has already applied
    # fire-once semantics and the 21-session flap guard, so this no longer
    # re-posts the same standing signals every night.
    fired = payload.get("alerts_fired", [])
    if not fired:
        print("No new transitions since last run — nothing to post.")
        return

    by_tk = {s["ticker"]: s for s in payload["sectors"]}
    reg = payload["regime"]
    high = [a for a in fired if a["severity"] == "high"]
    lines = [f"*Smart Money · Sector Transition* — {payload['meta']['as_of']}",
             f"Macro weather: *{reg['regime']}* — {reg['note']}",
             f"{len(fired)} new transition(s), {len(high)} high-severity", ""]
    # group by ticker so a sector that changed on several dimensions reads as one
    order, seen = [], set()
    for a in fired:
        if a["ticker"] not in seen:
            seen.add(a["ticker"]); order.append(a["ticker"])
    for tk in order:
        s = by_tk.get(tk, {})
        events = [a for a in fired if a["ticker"] == tk]
        flag = "🔴 " if any(e["severity"] == "high" for e in events) else ""
        lines.append(f"{flag}*{tk} · {s.get('name', tk)}*")
        for a in events:
            lines.append(f"    • {a['text']}")
        lines.append("")

    body = json.dumps({"text": "\n".join(lines)}).encode()
    req = urllib.request.Request(url, data=body,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"Webhook posted ({r.status}) — {len(fired)} new transitions.")
    except Exception as exc:  # noqa: BLE001
        print(f"Webhook failed: {exc}")


def _max_age(args) -> float:
    """
    Resolve the cache-freshness window. `--fresh` wins over `--offline` if both
    are given, on the principle that the more explicit request for new data
    should not be silently overridden.
    """
    if args.fresh:
        return 0.0
    if args.max_age_hours is not None:
        return float(args.max_age_hours)
    if args.offline:
        return 24.0 * 365
    return 12.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Smart Money · Sector Transition Dashboard")
    ap.add_argument("--open", action="store_true", help="open the dashboard when finished")
    ap.add_argument("--tier1", action="store_true", help="GICS sectors only")
    ap.add_argument("--tier2", action="store_true", help="industry sub-groups only")
    ap.add_argument("--tickers", type=str, default="", help="comma-separated ETF list")
    ap.add_argument("--no-breadth", action="store_true", help="skip constituent fetch")
    ap.add_argument("--fresh", action="store_true", help="ignore cache")
    # Rebuilding the report from cache is the common case when iterating on the
    # dashboard itself: the numbers are unchanged, only the rendering differs.
    # Without this, every rebuild refetched ~480 series because the default
    # freshness window is 12h, so any run on a later calendar day paid full price
    # for data it already had.
    ap.add_argument("--max-age-hours", type=float, default=None, metavar="H",
                    help="accept cache younger than H hours (default 12). "
                         "Use a large value to rebuild with no API calls at all.")
    ap.add_argument("--offline", action="store_true",
                    help="shorthand for --max-age-hours 8760: rebuild purely from "
                         "cache, guaranteeing zero provider requests")
    # Re-rendering after a presentation-only change should not cost a full
    # recompute. --offline still recomputes every metric (~40s); this reuses the
    # last snapshot and only rebuilds the HTML (well under a second).
    ap.add_argument("--render-only", action="store_true",
                    help="rebuild dashboard.html from the existing "
                         "output/snapshot.json without recomputing anything")
    ap.add_argument("--alert", action="store_true", help="post signals to the webhook")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.render_only:
        snap = Path(config.OUTPUT_DIR) / "snapshot.json"
        if not snap.exists():
            print(f"{snap} not found — run without --render-only first")
            return 1
        payload = json.loads(snap.read_text(encoding="utf-8"))
        # write_json strips `_raw`, and render never reads it, so the snapshot is
        # a complete input for rendering. `pairs` predates `flow`; tolerate both.
        payload.setdefault("flow", payload.pop("pairs", {}))
        out = report.write_report(payload)
        print(f"Rendered from {snap.name} (no recompute)\n\nOpen: {out}")
        return 0

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        unknown = [t for t in tickers if t not in config.SECTORS]
        if unknown:
            print(f"Unknown tickers (add them to smf/config.py SECTORS): {', '.join(unknown)}")
            tickers = [t for t in tickers if t in config.SECTORS]
        if not tickers:
            return 1
    elif args.tier1:
        tickers = config.TIER1
    elif args.tier2:
        tickers = config.TIER2
    else:
        tickers = config.ALL_TICKERS

    try:
        payload = pipeline.run(
            tickers=tickers,
            skip_breadth=args.no_breadth,
            max_age_hours=_max_age(args),
            quiet=args.quiet,
        )
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return 1

    if args.alert:
        post_webhook(payload)

    out = Path(config.OUTPUT_DIR) / "dashboard.html"
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    else:
        print(f"\nOpen: {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
