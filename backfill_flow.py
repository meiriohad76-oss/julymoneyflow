#!/usr/bin/env python3
"""
Backfill Polygon tick-level order flow so the institutional-footprint component
can be evaluated on observed data instead of daily-bar proxies.

This is the one part of the original thesis that cannot be proxied: real
off-exchange volume share, dark-pool (ATS) share, and block prints with direction.

WHY A CONTIGUOUS BLOCK
----------------------
Flow metrics need a rolling lookback (default 20 sessions). Fetching a *sparse*
set of rebalance dates means each date's lookback is fetched fresh. Fetching a
*contiguous* block means every date after the first reuses cached sessions, so
N sessions of data yields roughly N-lookback usable observations per ETF instead
of N/lookback. That is a ~20x efficiency difference.

COST
----
Roughly 7 seconds per ticker-day (about 5s network, 2s parse, measured on 2018
sessions of sector ETFs). Contiguous:

    11 ETFs x 126 sessions (6 months)  =  1,386 ticker-days  ~ 2.7 hours
    11 ETFs x 252 sessions (1 year)    =  2,772 ticker-days  ~ 5.4 hours
    11 ETFs x 2252 sessions (10 years) = 24,772 ticker-days  ~ 48 hours

Fully resumable: each ticker-day is written as a small JSON summary in
data/flow/ and never refetched. Interrupt with Ctrl-C and rerun; it picks up
where it stopped. Run it overnight.

USAGE
-----
    python backfill_flow.py --estimate                  # cost only, fetch nothing
    python backfill_flow.py --sessions 126              # 6 months, 11 sector ETFs
    python backfill_flow.py --sessions 252 --tickers XLK,XLE,XLV
    python backfill_flow.py --sessions 126 --max-minutes 60   # bounded pass
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from smf import config, flow, providers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=126,
                    help="contiguous trading sessions back from the most recent "
                         "complete session (126 ~ 6 months)")
    ap.add_argument("--tickers", type=str, default="",
                    help="comma-separated; defaults to config.OFF_EXCHANGE_TICKERS")
    ap.add_argument("--estimate", action="store_true",
                    help="report cost and what is already cached, fetch nothing")
    ap.add_argument("--max-minutes", type=float, default=0.0,
                    help="stop after this many minutes (resumable)")
    ap.add_argument("--secs-per-day", type=float, default=7.1,
                    help="cost assumption for the estimate")
    ap.add_argument("--offset", type=int, default=25,
                    help="end the window this many sessions before the latest "
                         "session, so forward returns exist for every date "
                         "(must exceed your longest test horizon)")
    args = ap.parse_args()

    try:
        providers.active_providers()
    except providers.ProviderUnavailable as exc:
        print(f"ERROR: {exc}")
        return 2
    if not config.POLYGON_API_KEY:
        print("ERROR: POLYGON_API_KEY is not set. Tick data is unavailable.")
        return 2

    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               or list(config.OFF_EXCHANGE_TICKERS))

    print("=" * 72)
    print("Polygon Tick-Flow Backfill")
    print("=" * 72)
    trf = flow.trf_exchange_ids()
    print(f"TRF (off-exchange) exchange ids: {sorted(trf)}")

    # Trading calendar from the benchmark price series; never request today, since
    # a partial session yields a misleading share.
    bench = providers.history(config.BENCHMARK)
    if bench.empty:
        print("ERROR: no benchmark history.")
        return 1
    today = time.strftime("%Y-%m-%d")
    cal = [d.date().isoformat() for d in bench.index if d.date().isoformat() < today]
    # Offset the window back so every date in it HAS a forward return. Fetching the
    # most recent sessions instead produces flow data whose forward outcome has not
    # happened yet — which would waste the entire backfill at the recent end.
    end = len(cal) - args.offset if args.offset else len(cal)
    sessions = cal[max(0, end - args.sessions):end]
    print(f"Window: {sessions[0]} -> {sessions[-1]} ({len(sessions)} sessions)")
    print(f"  offset {args.offset} sessions back from {cal[-1]} so forward "
          f"returns exist for every date")
    print(f"Tickers: {len(tickers)} — {', '.join(tickers)}")

    todo: list[tuple[str, str]] = []
    have = 0
    for t in tickers:
        for d in sessions:
            if flow._summary_path(t, d).exists():   # noqa: SLF001
                have += 1
            else:
                todo.append((t, d))

    total = len(tickers) * len(sessions)
    est_h = len(todo) * args.secs_per_day / 3600.0
    print(f"\nTicker-days: {total:,} total · {have:,} already cached · "
          f"{len(todo):,} to fetch")
    print(f"Estimated time: {est_h:.1f} hours at {args.secs_per_day}s each")
    usable = max(0, len(sessions) - config.OFF_EXCHANGE_LOOKBACK_DAYS) * len(tickers)
    print(f"Yields ~{usable:,} usable (date, ticker) observations once complete "
          f"(lookback {config.OFF_EXCHANGE_LOOKBACK_DAYS} sessions)")

    if args.estimate:
        print("\n--estimate given; nothing fetched.")
        return 0
    if not todo:
        print("\nNothing to do — window fully cached.")
        return 0

    budget = args.max_minutes * 60 if args.max_minutes else None
    print(f"\nFetching{f' for up to {args.max_minutes:.0f} min' if budget else ''}. "
          f"Ctrl-C is safe — progress is saved per ticker-day.\n")

    t0 = time.time()
    done = fail = 0
    try:
        for i, (t, d) in enumerate(todo, 1):
            s = flow.fetch_day_flow(t, d)
            done += 1 if s else 0
            fail += 0 if s else 1
            el = time.time() - t0
            rate = el / i
            eta = (len(todo) - i) * rate / 60.0
            w = 26
            filled = int(w * i / len(todo))
            print(f"\r  [{'█'*filled}{'·'*(w-filled)}] {i}/{len(todo)}  "
                  f"ok={done} fail={fail}  {rate:.1f}s/day  ETA {eta:.0f}min   ",
                  end="", flush=True)
            if budget and el > budget:
                print(f"\n\n  time budget reached — {len(todo)-i:,} ticker-days remain.")
                print("  Rerun the same command to continue.")
                break
    except KeyboardInterrupt:
        print("\n\n  interrupted — progress saved. Rerun to continue.")

    el = time.time() - t0
    print(f"\n\nFetched {done} ticker-days in {el/60:.1f} min "
          f"({el/max(done,1):.1f}s each), {fail} failed.")

    remaining = sum(1 for t in tickers for d in sessions
                    if not flow._summary_path(t, d).exists())   # noqa: SLF001
    print(f"{remaining:,} of {total:,} still missing.")
    if remaining == 0:
        print("\nWindow complete. Next:  python flow_backtest.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
