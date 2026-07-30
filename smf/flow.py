"""
Real institutional order flow from Polygon tick data.

This replaces the daily-bar proxies in `metrics.py` with directly observed
off-exchange activity. Three things become measurable that daily bars cannot show:

  1. Off-exchange volume share
     Every trade executed away from a lit exchange must be reported to a FINRA
     Trade Reporting Facility. Polygon tags those with `exchange == 4`, and
     prints carrying a `trf_id` are ATS (dark pool) executions specifically.
     A rising off-exchange share while price is flat is the textbook signature
     of a large order being worked quietly.

  2. Actual block prints
     A block is a single print above a size or notional threshold — not inferred
     from a volume distribution, but observed. Count, volume share, mean size and
     the largest print of the session all become available.

  3. Block direction (buy vs sell pressure)
     Using a tick test against the trade sequence, each block is classified as
     arriving on an uptick or a downtick. Net block direction is the closest
     thing to knowing which side is being forced to cross the spread.

Cost control: raw trades are never stored. Each ticker-day is reduced to a small
JSON summary cached on disk, so a day is fetched exactly once and all subsequent
runs are free. Only new sessions cost anything.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import config, providers

FLOW_DIR = config.CACHE_DIR / "flow"
FLOW_DIR.mkdir(exist_ok=True)

# Populated once per process from Polygon's reference data. Hardcoding "4" works
# today but Polygon has added TRF venues before; asking the API which ids are
# TRFs makes this self-correcting.
_TRF_IDS: set[int] | None = None


def trf_exchange_ids() -> set[int]:
    """Exchange ids that represent off-exchange (TRF) reporting venues."""
    global _TRF_IDS
    if _TRF_IDS is not None:
        return _TRF_IDS

    fallback = {4}
    if not config.POLYGON_API_KEY:
        _TRF_IDS = fallback
        return _TRF_IDS

    url = ("https://api.polygon.io/v3/reference/exchanges"
           f"?asset_class=stocks&apiKey={config.POLYGON_API_KEY}")
    data = providers._get_json(url)  # noqa: SLF001
    ids: set[int] = set()
    if isinstance(data, dict):
        for row in data.get("results", []) or []:
            if str(row.get("type", "")).upper() == "TRF" and row.get("id") is not None:
                ids.add(int(row["id"]))
    _TRF_IDS = ids or fallback
    if ids:
        print(f"      TRF exchange ids from Polygon reference: {sorted(ids)}")
    else:
        print("      could not read exchange reference; assuming TRF id = 4")
    return _TRF_IDS


# ---------------------------------------------------------------------------
# Per-session summary
# ---------------------------------------------------------------------------
def _summary_path(ticker: str, day: str):
    return FLOW_DIR / f"{ticker.replace('/', '_')}_{day}.json"


def fetch_day_flow(ticker: str, day: str, force: bool = False) -> dict | None:
    """
    Reduce one session of tick data to a flow summary. Cached permanently —
    a completed session's trades never change.
    """
    path = _summary_path(ticker, day)
    if path.exists() and not force:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001, S110
            pass

    if not config.POLYGON_API_KEY:
        return None

    trf = trf_exchange_ids()
    sym = ticker.replace("-", ".")
    block_sh = config.BLOCK_MIN_SHARES
    block_nt = config.BLOCK_MIN_NOTIONAL

    tot_vol = off_vol = dark_vol = 0.0
    tot_trades = off_trades = 0
    blk_vol = blk_up = blk_dn = 0.0
    blk_n = 0
    blk_off_vol = 0.0
    largest = 0.0
    largest_notional = 0.0
    notional_sum = 0.0
    last_px: float | None = None
    truncated = False

    url = (f"https://api.polygon.io/v3/trades/{sym}"
           f"?timestamp={day}&limit=50000&order=asc"
           f"&apiKey={config.POLYGON_API_KEY}")
    pages = 0
    while url:
        if pages >= config.FLOW_MAX_PAGES:
            truncated = True
            break
        data = providers._get_json(url, timeout=60)  # noqa: SLF001
        if not isinstance(data, dict):
            break
        rows = data.get("results") or []
        if not rows and pages == 0:
            return None                      # no data for this session

        # Vectorised aggregation. A Python-level loop over 80k trades per session
        # cost ~5s of the ~14s per ticker-day, which over a multi-thousand-day
        # backfill is hours of pure interpreter overhead.
        f = pd.DataFrame(rows, columns=None)
        if f.empty:
            nxt = data.get("next_url")
            url = f"{nxt}&apiKey={config.POLYGON_API_KEY}" if nxt else None
            pages += 1
            continue
        for c in ("size", "price", "exchange"):
            if c not in f.columns:
                f[c] = 0
        size = pd.to_numeric(f["size"], errors="coerce").fillna(0.0).to_numpy(float)
        px_a = pd.to_numeric(f["price"], errors="coerce").fillna(0.0).to_numpy(float)
        keep = (size > 0) & (px_a > 0)
        size, px_a = size[keep], px_a[keep]
        if not len(size):
            nxt = data.get("next_url")
            url = f"{nxt}&apiKey={config.POLYGON_API_KEY}" if nxt else None
            pages += 1
            continue

        exch = pd.to_numeric(f.loc[keep, "exchange"], errors="coerce") \
                 .fillna(0).astype(int).to_numpy()
        has_trf = (f.loc[keep, "trf_id"].notna().to_numpy()
                   if "trf_id" in f.columns else np.zeros(len(size), bool))

        notional = size * px_a
        tot_vol += float(size.sum())
        tot_trades += int(len(size))
        notional_sum += float(notional.sum())

        is_off = np.isin(exch, list(trf))
        off_vol += float(size[is_off].sum())
        off_trades += int(is_off.sum())
        dark_vol += float(size[is_off & has_trf].sum())

        # Tick test across the page, carrying `last_px` over the page boundary.
        # Compare each trade to the previous *different* price, matching the
        # sequential definition.
        prev = np.empty(len(px_a))
        prev[0] = last_px if last_px is not None else px_a[0]
        if len(px_a) > 1:
            ff = pd.Series(px_a).where(pd.Series(px_a).diff() != 0).ffill()
            prev[1:] = ff.shift(1).fillna(prev[0]).to_numpy()[1:]
        direction = np.sign(px_a - prev)
        last_px = float(px_a[-1])

        is_blk = (size >= block_sh) | (notional >= block_nt)
        if is_blk.any():
            bs, bd = size[is_blk], direction[is_blk]
            blk_n += int(is_blk.sum())
            blk_vol += float(bs.sum())
            blk_off_vol += float(size[is_blk & is_off].sum())
            blk_up += float(bs[bd > 0].sum())
            blk_dn += float(bs[bd < 0].sum())
            largest = max(largest, float(bs.max()))
            largest_notional = max(largest_notional, float(notional[is_blk].max()))

        nxt = data.get("next_url")
        url = f"{nxt}&apiKey={config.POLYGON_API_KEY}" if nxt else None
        pages += 1
        time.sleep(config.FLOW_PAUSE)

    if tot_vol <= 0:
        return None

    out = {
        "ticker": ticker,
        "day": day,
        "trades": tot_trades,
        "volume": tot_vol,
        "vwap": round(notional_sum / tot_vol, 4),
        "off_exchange_volume": off_vol,
        "off_exchange_share": round(off_vol / tot_vol, 5),
        "off_exchange_trades": off_trades,
        "dark_pool_volume": dark_vol,
        "dark_pool_share": round(dark_vol / tot_vol, 5),
        # Empirically, Polygon reports ALL off-exchange volume under exchange id 4
        # and attaches a trf_id to every such print, so these two measures come out
        # identical. When that holds they must not be scored as two independent
        # signals — that would double-count one measurement. Verified on XLE:
        # off-exchange and dark-pool share were both exactly 28.07%.
        "dark_equals_off": abs(dark_vol - off_vol) < max(1.0, off_vol * 1e-6),
        "block_count": blk_n,
        "block_volume": blk_vol,
        "block_share": round(blk_vol / tot_vol, 5),
        "block_off_exchange_share": round(blk_off_vol / blk_vol, 5) if blk_vol else None,
        "block_uptick_volume": blk_up,
        "block_downtick_volume": blk_dn,
        "block_direction": (round((blk_up - blk_dn) / (blk_up + blk_dn), 4)
                            if (blk_up + blk_dn) > 0 else None),
        "largest_print_shares": largest,
        "largest_print_notional": round(largest_notional, 0),
        "pages": pages,
        "truncated": truncated,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        path.write_text(json.dumps(out), encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass
    return out


# ---------------------------------------------------------------------------
# Multi-session aggregation
# ---------------------------------------------------------------------------
def sector_flow(ticker: str, sessions: list[str], quiet: bool = False) -> dict | None:
    """
    Aggregate per-session summaries into the flow metrics the scorer consumes.

    `sessions` should be actual trading days (take them from the price index —
    generating a date range would waste requests on weekends and holidays).
    """
    days: list[dict] = []
    fetched = 0
    for d in sessions:
        s = fetch_day_flow(ticker, d)
        if s:
            days.append(s)
            fetched += int(not _summary_path(ticker, d).exists() or s.get("pages", 0) > 0)
    if len(days) < 3:
        return None

    df = pd.DataFrame(days).sort_values("day").reset_index(drop=True)

    def _trend(col: str) -> float | None:
        v = df[col].dropna().astype(float)
        if len(v) < 4:
            return None
        recent = v.iloc[-3:].mean()
        base = v.iloc[:-3].mean()
        return float(recent - base) if np.isfinite(base) else None

    n_trunc = int(df["truncated"].sum())
    if n_trunc and not quiet:
        print(f"      ! {ticker}: {n_trunc}/{len(df)} sessions hit the page cap — "
              f"shares are understated; raise FLOW_MAX_PAGES for full coverage")

    return {
        "sessions": len(df),
        "latest_day": str(df["day"].iloc[-1]),
        "off_exchange_share": round(float(df["off_exchange_share"].iloc[-1]), 5),
        "off_exchange_share_mean": round(float(df["off_exchange_share"].mean()), 5),
        "off_exchange_trend": (round(t, 5) if (t := _trend("off_exchange_share")) is not None else None),
        "dark_pool_share": round(float(df["dark_pool_share"].iloc[-1]), 5),
        "dark_pool_trend": (round(t, 5) if (t := _trend("dark_pool_share")) is not None else None),
        "block_share": round(float(df["block_share"].iloc[-1]), 5),
        "block_share_mean": round(float(df["block_share"].mean()), 5),
        "block_trend": (round(t, 5) if (t := _trend("block_share")) is not None else None),
        "block_count_latest": int(df["block_count"].iloc[-1]),
        "block_count_mean": round(float(df["block_count"].mean()), 1),
        "block_direction": (round(float(v.mean()), 4)
                            if len(v := df["block_direction"].dropna()) else None),
        "block_off_exchange_share": (round(float(v2.iloc[-1]), 4)
                                     if len(v2 := df["block_off_exchange_share"].dropna()) else None),
        "largest_print_notional": float(df["largest_print_notional"].max()),
        "truncated_sessions": n_trunc,
        "dark_equals_off": bool(df["dark_equals_off"].all())
                           if "dark_equals_off" in df.columns else False,
        "daily": df[["day", "off_exchange_share", "dark_pool_share",
                     "block_share", "block_count", "block_direction"]]
                 .to_dict(orient="records"),
    }


def classify_blocks(fl: dict, price: float | None = None,
                    adv_dollar: float | None = None) -> dict:
    """
    The four block-trade buckets from the source framework, plus a Carpool-style
    summary line.

    The framework describes these as classifications of institutional block
    activity. The raw prints are already collected — count, volume share, largest
    notional, tick direction — so this is the classification layer that was missing,
    not new data.

      Safe Bets      Large, steady institutional participation in a liquid name.
                     High block share, unexceptional relative to the asset's own
                     norm, direction not aggressively negative. A floor of support.
      Most Unusual   Block activity far above this asset's historical norm. The
                     framework treats this as an impending volatility catalyst.
      Top Positions  Largest absolute dollar concentration — where the biggest
                     money is actually working, regardless of relative surprise.
      Longshots      Aggressive directional blocks in a thinner venue: high block
                     share and strong directional skew on modest liquidity.

    A sector can occupy more than one bucket; these are tags, not a partition.
    """
    if not fl:
        return {"buckets": [], "summary": "no flow data"}

    bs = fl.get("block_share")
    bs_mean = fl.get("block_share_mean")
    cnt = fl.get("block_count_latest")
    cnt_mean = fl.get("block_count_mean")
    direction = fl.get("block_direction")
    largest = fl.get("largest_print_notional")
    off = fl.get("off_exchange_share")

    tags: list[dict] = []

    # Unusualness relative to the asset's own history
    surprise = None
    if bs is not None and bs_mean:
        surprise = bs / bs_mean if bs_mean else None
    cnt_surprise = None
    if cnt is not None and cnt_mean:
        cnt_surprise = cnt / cnt_mean if cnt_mean else None

    liquid = bool(adv_dollar and adv_dollar > 2.0e8)      # >$200M/day

    if (bs is not None and bs >= 0.20
            and (surprise is None or surprise < 1.5)
            and (direction is None or direction > -0.25)):
        tags.append({
            "bucket": "Safe Bets",
            "detail": (f"{bs*100:.0f}% of volume in blocks at a normal rate"
                       + (f" ({surprise:.2f}x its own average)" if surprise else "")
                       + f", direction {direction:+.2f}" if direction is not None else ""),
        })

    if (surprise is not None and surprise >= 1.5) or \
       (cnt_surprise is not None and cnt_surprise >= 2.0):
        parts = []
        if surprise:
            parts.append(f"block share {surprise:.2f}x its average")
        if cnt_surprise:
            parts.append(f"block count {cnt_surprise:.2f}x its average")
        tags.append({"bucket": "Most Unusual",
                     "detail": "; ".join(parts) + " — potential volatility catalyst"})

    if largest is not None and largest >= 5.0e7:          # >=$50M single print
        tags.append({"bucket": "Top Positions",
                     "detail": f"single print of ${largest/1e6:.0f}M observed"})

    if (direction is not None and abs(direction) >= 0.30
            and bs is not None and bs >= 0.15 and not liquid):
        side = "buy" if direction > 0 else "sell"
        tags.append({"bucket": "Longshots",
                     "detail": (f"aggressive {side}-side blocks ({direction:+.2f}) "
                                f"on {bs*100:.0f}% block share in a thinner venue")})

    # Carpool-style clustered summary
    bits = []
    if off is not None:
        bits.append(f"{off*100:.0f}% off-exchange")
    if cnt is not None:
        bits.append(f"{cnt} blocks")
    if direction is not None:
        bits.append(f"direction {direction:+.2f}")
    if largest:
        bits.append(f"largest ${largest/1e6:.0f}M")
    summary = " · ".join(bits) if bits else "no flow data"

    return {"buckets": tags,
            "bucket_names": [t["bucket"] for t in tags],
            "summary": summary,
            "block_surprise": round(surprise, 2) if surprise else None,
            "count_surprise": round(cnt_surprise, 2) if cnt_surprise else None}


def flow_score(fl: dict) -> tuple[float, dict]:
    """
    Collapse observed flow into a -1..+1 institutional footprint.

    Components, and why each is signal rather than noise:
      off_exchange_trend  rising share of volume printing away from lit venues
      dark_pool_trend     the ATS subset of that, which is the cleanest read
      block_direction     blocks arriving on upticks vs downticks
      block_trend         growing share of volume arriving in outsized clips
    """
    parts: dict[str, float] = {}
    weights = dict(config.REAL_FLOW_WEIGHTS)

    # Collinearity guard. Polygon tags every off-exchange print with a trf_id, so
    # dark-pool share and off-exchange share are the same series. Scoring both
    # would award one measurement 60% of the weight under two names. When they
    # coincide, collapse to a single term carrying the combined weight.
    collapsed = bool(fl.get("dark_equals_off"))
    if collapsed:
        w = weights.pop("dark_pool_trend", 0.0) + weights.pop("off_exchange_trend", 0.0)
        weights["off_exchange_trend"] = w
        if (v := fl.get("off_exchange_trend")) is not None:
            parts["off_exchange_trend"] = float(np.clip(v * 25.0, -1, 1))
    else:
        if (v := fl.get("off_exchange_trend")) is not None:
            parts["off_exchange_trend"] = float(np.clip(v * 25.0, -1, 1))
        if (v := fl.get("dark_pool_trend")) is not None:
            parts["dark_pool_trend"] = float(np.clip(v * 30.0, -1, 1))

    if (v := fl.get("block_direction")) is not None:
        parts["block_direction"] = float(np.clip(v * 2.0, -1, 1))
    if (v := fl.get("block_trend")) is not None:
        parts["block_trend"] = float(np.clip(v * 20.0, -1, 1))

    if not parts:
        return float("nan"), {}

    usable = {k: w for k, w in weights.items() if k in parts}
    tot = sum(usable.values()) or 1.0
    score = sum(parts[k] * (w / tot) for k, w in usable.items())
    return float(score), {k: round(v, 3) for k, v in parts.items()}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def collect(tickers: list[str], price_index: dict[str, pd.DatetimeIndex],
            lookback: int | None = None, quiet: bool = False,
            fetch_missing: bool = False) -> dict[str, dict]:
    """
    Aggregate flow for each ticker.

    `fetch_missing=False` (the default) uses ONLY already-cached sessions and never
    hits the network. This matters: a dashboard run would otherwise fetch 11 ETFs x
    20 sessions = 220 ticker-days at ~7s each, turning a 10-second refresh into a
    25-minute one. Populate the cache with `backfill_flow.py` instead, which is
    resumable and designed for it.
    """
    if not (config.POLYGON_API_KEY and config.ENABLE_POLYGON_OFF_EXCHANGE):
        return {}

    lookback = lookback or config.OFF_EXCHANGE_LOOKBACK_DAYS
    out: dict[str, dict] = {}
    today = date.today().isoformat()

    for i, t in enumerate(tickers, 1):
        idx = price_index.get(t)
        if idx is None or len(idx) < lookback:
            continue
        # Use real trading days from the price series, and never request today —
        # a partial session produces a misleading share.
        sessions = [d.date().isoformat() for d in idx[-(lookback + 1):]
                    if d.date().isoformat() < today][-lookback:]
        if not fetch_missing:
            sessions = [d for d in sessions if _summary_path(t, d).exists()]
            if len(sessions) < 3:
                continue
        cached = sum(1 for d in sessions if _summary_path(t, d).exists())
        if not quiet:
            print(f"      [{i}/{len(tickers)}] {t:<6} {len(sessions)} sessions "
                  f"({cached} cached)...", end="", flush=True)
        fl = sector_flow(t, sessions, quiet=quiet)
        if fl:
            out[t] = fl
            if not quiet:
                print(f" off-exch {fl['off_exchange_share']:.1%} "
                      f"(trend {fl['off_exchange_trend']:+.2%}) · "
                      f"{fl['block_count_latest']} blocks · "
                      f"dir {fl['block_direction'] if fl['block_direction'] is not None else 0:+.2f}")
        elif not quiet:
            print(" no data")
    return out
