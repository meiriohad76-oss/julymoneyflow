#!/usr/bin/env python3
"""
Pre-fetch every price series into the local cache.

Useful on first run, on a slow connection, or when you want to schedule the
data pull separately from dashboard generation. Safe to interrupt and re-run:
already-cached tickers are skipped.

    python warm_cache.py            # fetch everything still missing
    python warm_cache.py --limit 80 # fetch at most 80 tickers this pass
"""
from __future__ import annotations

import argparse
import sys

from smf import config, providers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max tickers to fetch this pass")
    ap.add_argument("--max-age-hours", type=float, default=24.0)
    args = ap.parse_args()

    wanted: list[str] = [config.BENCHMARK] + config.ALL_TICKERS
    for meta in config.SECTORS.values():
        wanted.extend(meta.get("constituents", []))
    if getattr(config, "INCLUDE_HISTORICAL_MEMBERS", False):
        for extra in (getattr(config, "HISTORICAL_MEMBERS", {}) or {}).values():
            wanted.extend(extra)

    seen: set[str] = set()
    ordered = [t for t in wanted if not (t in seen or seen.add(t))]

    # Provenance matters: a series cached from a different provider is NOT a hit.
    # Without this the cache silently survives a provider switch and you keep
    # computing on the old source.
    want_prov = providers.preferred_provider() if config.STRICT_CACHE_PROVENANCE else None
    todo = [t for t in ordered
            if not providers.cache_is_valid(t, args.max_age_hours,
                                            require_provider=want_prov,
                                            require_days=config.HISTORY_DAYS)]
    print(f"provider: {want_prov or 'any'}")
    print(f"{len(ordered)} tickers total · {len(ordered)-len(todo)} already cached "
          f"from {want_prov or 'any'} · {len(todo)} to fetch")
    if not todo:
        print("Cache is warm.")
        return 0

    if args.limit:
        todo = todo[: args.limit]
    got = providers.batch_history(todo, max_age_hours=args.max_age_hours, pause=0.05)
    failed = [t for t in todo if t not in got]
    print(f"fetched {len(got)}/{len(todo)}")
    if failed:
        print(f"failed (will retry next pass): {', '.join(failed[:40])}")
    remaining = len([t for t in ordered
                     if not providers.cache_is_valid(t, args.max_age_hours,
                                                     require_provider=want_prov,
                                                     require_days=config.HISTORY_DAYS)])
    print(f"{remaining} still missing overall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
