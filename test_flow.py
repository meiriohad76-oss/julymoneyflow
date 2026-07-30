#!/usr/bin/env python3
"""
Unit tests for the tick-level flow module.

The flow code cannot be exercised without a paid Polygon key, so these tests
drive it with synthetic trade fixtures that have known, hand-computable answers.
Run this after changing anything in smf/flow.py:

    python test_flow.py
"""
from __future__ import annotations

import json
import shutil
import sys

from smf import config, flow, providers

PASS = FAIL = 0


def check(name: str, got, want, tol: float | None = None) -> None:
    global PASS, FAIL
    if tol is not None and isinstance(got, (int, float)) and isinstance(want, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")


def build_fixture() -> list[dict]:
    """
    12 trades with deliberately known properties.

    exchange 11 = a lit exchange, exchange 4 = TRF (off-exchange).
    A trf_id marks it as an ATS / dark pool print.

    Prices walk 100 -> 101 -> 100 -> 102 so the tick test has ups and downs.
    """
    return [
        # lit, small
        {"exchange": 11, "size": 100,    "price": 100.00},                    # flat (first)
        {"exchange": 11, "size": 200,    "price": 101.00},                    # uptick
        # off-exchange, no trf_id -> off-exchange but not dark pool
        {"exchange": 4,  "size": 1_000,  "price": 101.00},                    # flat
        # dark pool block on an uptick: 15k shares
        {"exchange": 4,  "size": 15_000, "price": 102.00, "trf_id": 2},       # uptick, BLOCK
        # dark pool block on a downtick: 20k shares
        {"exchange": 4,  "size": 20_000, "price": 100.00, "trf_id": 2},       # downtick, BLOCK
        # lit block by notional only: 2,500 sh x $102 = $255k >= $200k
        {"exchange": 11, "size": 2_500,  "price": 102.00},                    # uptick, BLOCK
        # lit, small
        {"exchange": 12, "size": 300,    "price": 102.00},                    # flat
        # dark pool, small
        {"exchange": 4,  "size": 500,    "price": 102.00, "trf_id": 1},       # flat
        # lit block on a downtick: 11k shares
        {"exchange": 11, "size": 11_000, "price": 101.00},                    # downtick, BLOCK
        # junk rows that must be ignored
        {"exchange": 11, "size": 0,      "price": 101.00},
        {"exchange": 11, "size": 100,    "price": 0},
        {"exchange": 4,  "size": 400,    "price": 101.00, "trf_id": 3},       # flat
    ]


def main() -> int:
    print("Tick-flow unit tests\n" + "=" * 60)

    trades = build_fixture()
    # single page response
    fake = {"results": trades, "next_url": None}

    # ---- stub the network + force a known TRF set ------------------------
    orig_get = providers._get_json
    providers._get_json = lambda url, timeout=30, retries=3: fake  # noqa: SLF001
    flow._TRF_IDS = {4}                                            # noqa: SLF001
    config.POLYGON_API_KEY = "test-key"
    config.BLOCK_MIN_SHARES = 10_000
    config.BLOCK_MIN_NOTIONAL = 200_000
    config.FLOW_PAUSE = 0.0

    # clean fixture cache so we always recompute
    for p in flow.FLOW_DIR.glob("TEST_*.json"):
        try:
            p.unlink()
        except OSError:
            pass

    s = flow.fetch_day_flow("TEST", "2026-07-20", force=True)
    assert s is not None, "fetch_day_flow returned None"

    print("\n[volume accounting]")
    # valid trades: 100+200+1000+15000+20000+2500+300+500+11000+400 = 51,000
    check("total volume", s["volume"], 51_000)
    check("trades counted (junk excluded)", s["trades"], 10)
    # off-exchange (exchange 4): 1000+15000+20000+500+400 = 36,900
    check("off-exchange volume", s["off_exchange_volume"], 36_900)
    check("off-exchange share", s["off_exchange_share"], 36_900 / 51_000, tol=1e-4)
    # dark pool (exchange 4 AND trf_id): 15000+20000+500+400 = 35,900
    check("dark pool volume", s["dark_pool_volume"], 35_900)
    check("dark pool share", s["dark_pool_share"], 35_900 / 51_000, tol=1e-4)

    print("\n[block detection]")
    # blocks: 15000, 20000, 2500(notional $255k), 11000  -> 4 blocks, 48,500 shares
    check("block count", s["block_count"], 4)
    check("block volume", s["block_volume"], 48_500)
    check("block share", s["block_share"], 48_500 / 51_000, tol=1e-4)
    check("largest print shares", s["largest_print_shares"], 20_000)
    check("largest print notional", s["largest_print_notional"], 20_000 * 100.0)
    # block off-exchange share: (15000+20000)/48500
    check("block off-exchange share", s["block_off_exchange_share"],
          35_000 / 48_500, tol=1e-4)

    print("\n[tick test / block direction]")
    # uptick blocks: 15000 (100->102... prev price 101, so up) + 2500 (100->102 up) = 17,500
    # downtick blocks: 20000 (102->100 down) + 11000 (102->101 down) = 31,000
    check("block uptick volume", s["block_uptick_volume"], 17_500)
    check("block downtick volume", s["block_downtick_volume"], 31_000)
    check("block direction (sell-skewed)", s["block_direction"],
          (17_500 - 31_000) / 48_500, tol=1e-4)
    assert s["block_direction"] < 0, "expected sell-side skew"

    print("\n[vwap]")
    notional = sum(t["size"] * t["price"] for t in trades
                   if t["size"] > 0 and t["price"] > 0)
    check("vwap", s["vwap"], notional / 51_000, tol=1e-3)

    print("\n[caching]")
    p = flow._summary_path("TEST", "2026-07-20")            # noqa: SLF001
    check("summary written to disk", p.exists(), True)
    providers._get_json = lambda *a, **k: (_ for _ in ()).throw(  # noqa: SLF001
        AssertionError("network hit on a cached day"))
    again = flow.fetch_day_flow("TEST", "2026-07-20")
    check("second call served from cache", again["volume"], 51_000)
    providers._get_json = lambda url, timeout=30, retries=3: fake  # noqa: SLF001

    print("\n[pagination cap]")
    calls = {"n": 0}

    def paged(url, timeout=30, retries=3):
        calls["n"] += 1
        return {"results": trades, "next_url": "https://api.polygon.io/next"}

    providers._get_json = paged                                    # noqa: SLF001
    config.FLOW_MAX_PAGES = 3
    s2 = flow.fetch_day_flow("TEST", "2026-07-21", force=True)
    check("stopped at page cap", calls["n"], 3)
    check("truncation flagged", s2["truncated"], True)
    check("volume accumulated across pages", s2["volume"], 51_000 * 3)

    print("\n[multi-session aggregation]")
    providers._get_json = lambda url, timeout=30, retries=3: fake  # noqa: SLF001
    config.FLOW_MAX_PAGES = 40
    days = [f"2026-07-{d:02d}" for d in (6, 7, 8, 9, 10, 13, 14)]
    for p in flow.FLOW_DIR.glob("TEST2_*.json"):
        try:
            p.unlink()
        except OSError:
            pass
    agg = flow.sector_flow("TEST2", days, quiet=True)
    assert agg is not None, "sector_flow returned None"
    check("sessions aggregated", agg["sessions"], 7)
    check("off-exchange share carried through", agg["off_exchange_share"],
          36_900 / 51_000, tol=1e-4)
    # every session is identical, so every trend must be exactly zero
    check("flat series -> zero off-exchange trend", agg["off_exchange_trend"], 0.0, tol=1e-9)
    check("flat series -> zero dark pool trend", agg["dark_pool_trend"], 0.0, tol=1e-9)
    check("too few sessions returns None", flow.sector_flow("TEST2", days[:2], quiet=True), None)

    print("\n[flow score]")
    # Note: block_direction is a *level* (net buy vs sell pressure), not a trend,
    # so a flat series with sell-skewed blocks correctly scores negative. Only a
    # flat series with neutral blocks should score ~0.
    sc_neutral, _ = flow.flow_score(dict(agg, block_direction=0.0))
    check("flat trends + neutral blocks scores ~0", sc_neutral, 0.0, tol=0.02)
    sc, parts = flow.flow_score(agg)
    check("flat trends + sell-skewed blocks scores negative", sc < -0.05, True)
    check("only the block-direction term is non-zero",
          [k for k, v in parts.items() if abs(v) > 1e-9], ["block_direction"])
    # a rising dark pool share plus buy-side blocks must score positive
    bull = dict(agg, dark_pool_trend=0.03, off_exchange_trend=0.025,
                block_direction=0.4, block_trend=0.02)
    sc_b, _ = flow.flow_score(bull)
    check("accumulation signature scores positive", sc_b > 0.4, True)
    bear = dict(agg, dark_pool_trend=-0.03, off_exchange_trend=-0.025,
                block_direction=-0.4, block_trend=-0.02)
    sc_r, _ = flow.flow_score(bear)
    check("distribution signature scores negative", sc_r < -0.4, True)
    check("score is bounded", -1.0 <= sc_b <= 1.0 and -1.0 <= sc_r <= 1.0, True)
    check("missing components -> nan", flow.flow_score({})[0] != flow.flow_score({})[0], True)

    print("\n[TRF id discovery]")
    flow._TRF_IDS = None                                           # noqa: SLF001
    providers._get_json = lambda url, timeout=30, retries=3: {      # noqa: SLF001
        "results": [{"id": 1, "type": "exchange"}, {"id": 4, "type": "TRF"},
                    {"id": 5, "type": "TRF"}, {"id": 2, "type": "SIP"}]}
    check("reads TRF ids from reference data", flow.trf_exchange_ids(), {4, 5})
    flow._TRF_IDS = None                                           # noqa: SLF001
    providers._get_json = lambda url, timeout=30, retries=3: None   # noqa: SLF001
    check("falls back to id 4 when reference unavailable",
          flow.trf_exchange_ids(), {4})

    # cleanup
    providers._get_json = orig_get                                 # noqa: SLF001
    for pat in ("TEST_*.json", "TEST2_*.json"):
        for p in flow.FLOW_DIR.glob(pat):
            try:
                p.unlink()
            except OSError:
                pass          # read-only mount; harmless, fixtures are namespaced

    print("\n" + "=" * 60)
    print(f"{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
