"""
Data layer.

Provider chain: FMP -> Polygon -> Yahoo (free, no key).
All providers return the same normalised OHLCV frame so the rest of the system
never needs to know where the data came from.

A local parquet/CSV cache keeps repeat runs fast and lets the dashboard work
offline once data has been pulled at least once.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import pandas as pd

from . import config

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

COLUMNS = ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _get_json(url: str, timeout: int = 30, retries: int = 3) -> dict | list | None:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in (401, 403, 404):
                return None                      # permanent — don't retry
            time.sleep(1.5 * (attempt + 1))      # 429 / 5xx — back off
        except Exception as exc:                 # noqa: BLE001
            last_err = exc
            time.sleep(1.0 * (attempt + 1))
    if last_err:
        print(f"    ! fetch failed: {type(last_err).__name__} {url.split('?')[0]}")
    return None


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Sort ascending, drop dupes/incomplete rows, coerce numeric."""
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[COLUMNS].dropna(subset=["close"])
    df["volume"] = df["volume"].fillna(0.0)
    return df


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------
def _fmp_history(ticker: str, days: int) -> pd.DataFrame:
    if not config.FMP_API_KEY:
        return pd.DataFrame()
    frm = (date.today() - timedelta(days=days)).isoformat()
    to = date.today().isoformat()
    # `stable` is FMP's current namespace; legacy v3 kept as a fallback.
    urls = [
        f"https://financialmodelingprep.com/stable/historical-price-eod/full"
        f"?symbol={ticker}&from={frm}&to={to}&apikey={config.FMP_API_KEY}",
        f"https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}"
        f"?from={frm}&to={to}&apikey={config.FMP_API_KEY}",
    ]
    for url in urls:
        data = _get_json(url)
        rows = None
        if isinstance(data, list) and data:
            rows = data
        elif isinstance(data, dict) and data.get("historical"):
            rows = data["historical"]
        if not rows:
            continue
        df = pd.DataFrame(rows)
        if "date" not in df.columns:
            continue
        df = df.set_index("date")
        return _normalise(df)
    return pd.DataFrame()


def _polygon_history(ticker: str, days: int) -> pd.DataFrame:
    if not config.POLYGON_API_KEY:
        return pd.DataFrame()
    frm = (date.today() - timedelta(days=days)).isoformat()
    to = date.today().isoformat()
    sym = ticker.replace("-", ".")            # Polygon uses BRK.B not BRK-B
    url = (f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/{frm}/{to}"
           f"?adjusted=true&sort=asc&limit=50000&apiKey={config.POLYGON_API_KEY}")
    data = _get_json(url)
    if not isinstance(data, dict) or not data.get("results"):
        return pd.DataFrame()
    df = pd.DataFrame(data["results"])
    df["date"] = pd.to_datetime(df["t"], unit="ms")
    df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume"}).set_index("date")
    return _normalise(df)


def _yahoo_history(ticker: str, days: int) -> pd.DataFrame:
    rng = "2y" if days <= 730 else "5y"
    sym = ticker.replace(".", "-")            # Yahoo uses BRK-B
    for host in ("query2", "query1"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}"
               f"?range={rng}&interval=1d&includeAdjustedClose=true")
        data = _get_json(url)
        try:
            res = data["chart"]["result"][0]                      # type: ignore[index]
            q = res["indicators"]["quote"][0]
            df = pd.DataFrame({
                "open": q["open"], "high": q["high"], "low": q["low"],
                "close": q["close"], "volume": q["volume"],
            }, index=pd.to_datetime(res["timestamp"], unit="s"))
            adj = res["indicators"].get("adjclose")
            if adj and adj[0].get("adjclose"):
                # Scale OHLC by the close/adjclose ratio so splits don't create
                # phantom breakouts in the relative-strength series.
                a = pd.Series(adj[0]["adjclose"], index=df.index).astype(float)
                ratio = (a / df["close"].astype(float)).fillna(1.0)
                for c in ("open", "high", "low", "close"):
                    df[c] = df[c].astype(float) * ratio
            out = _normalise(df)
            if not out.empty:
                return out
        except Exception:  # noqa: BLE001, S110
            continue
    return pd.DataFrame()


_HISTORY_FNS = {"fmp": _fmp_history, "polygon": _polygon_history, "yahoo": _yahoo_history}


class ProviderUnavailable(RuntimeError):
    """Raised when config.REQUIRE_PROVIDER cannot be satisfied."""


def _has_key(p: str) -> bool:
    if p == "fmp":
        return bool(config.FMP_API_KEY)
    if p == "polygon":
        return bool(config.POLYGON_API_KEY)
    return True                      # yahoo needs no key


def active_providers() -> list[str]:
    """
    Providers usable right now, in preference order.

    If config.REQUIRE_PROVIDER is set, ONLY that provider is returned — no
    fallback. A missing key then raises rather than quietly downgrading to free
    data, which would leave every downstream number looking authoritative while
    resting on a different foundation than intended.
    """
    req = getattr(config, "REQUIRE_PROVIDER", None)
    if req:
        if not _has_key(req):
            raise ProviderUnavailable(
                f"config.REQUIRE_PROVIDER = {req!r} but no API key is set for it.\n"
                f"  Create a file named '.env' next to run.py containing:\n"
                f"      {req.upper()}_API_KEY=your_key_here\n"
                f"  Or set config.REQUIRE_PROVIDER = None to allow fallback."
            )
        return [req]

    return [p for p in config.PROVIDER_ORDER if _has_key(p)]


# ---------------------------------------------------------------------------
# Cached public API
# ---------------------------------------------------------------------------
def _cache_path(ticker: str) -> "config.Path":
    return config.CACHE_DIR / f"{ticker.replace('/', '_')}.csv"


# Negative cache: tickers that came back empty from every provider. Index
# constituents change (acquisitions, delistings), and without this the system
# re-requests dead symbols on every run, which is the single biggest source of
# wasted time on a cold cache.
_MISS_FILE = config.CACHE_DIR / "_misses.json"
_MISS_TTL_HOURS = 72.0


def _load_misses() -> dict[str, float]:
    """
    Misses are scoped to the provider that produced them. A ticker Yahoo cannot
    serve (delisted names, which Yahoo deletes) is often perfectly available from
    Polygon, so inheriting the miss list across a provider change would keep
    27 recoverable series permanently excluded.
    """
    if not _MISS_FILE.exists():
        return {}
    try:
        data = json.loads(_MISS_FILE.read_text(encoding="utf-8"))
        now = time.time()
        prov = preferred_provider() or "?"
        out: dict[str, float] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                if v.get("provider") != prov:
                    continue
                ts = float(v.get("at", 0))
            else:                       # legacy flat format: provider unknown
                continue                # treat as not-a-miss and retry
            if (now - ts) / 3600.0 < _MISS_TTL_HOURS:
                out[k] = ts
        return out
    except Exception:  # noqa: BLE001
        return {}


def _record_miss(ticker: str) -> None:
    try:
        raw = json.loads(_MISS_FILE.read_text(encoding="utf-8")) \
            if _MISS_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        raw = {}
    raw[ticker] = {"provider": preferred_provider() or "?", "at": time.time()}
    try:
        _MISS_FILE.write_text(json.dumps(raw), encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass


# Cache provenance.
#
# Cached bars must record which provider produced them. Without this, adding a
# Polygon key would silently reuse 465 previously-cached Yahoo files — you would
# believe you were computing on Polygon data while actually using Yahoo bars.
# Any provider upgrade must invalidate the cache, not inherit it.
_SRC_FILE = config.CACHE_DIR / "_sources.json"


def _load_sources() -> dict[str, str]:
    """ticker -> provider, tolerating both the legacy flat and current dict forms."""
    out: dict[str, str] = {}
    for k, v in _load_sources_raw().items():
        if isinstance(v, dict):
            p = v.get("provider")
            if p:
                out[k] = str(p)
        elif isinstance(v, str):
            out[k] = v
    return out


def _record_source(ticker: str, provider: str, days: int | None = None,
                   rows: int | None = None) -> None:
    """
    Record provenance AND the requested history depth.

    Depth matters as much as provider: raising HISTORY_DAYS from 900 to 3800 does
    not invalidate a cache keyed only on provider, so a series holding 2.5 years
    would be silently reused where 10 were expected — quietly capping backtest
    power with no error anywhere.
    """
    try:
        raw = json.loads(_SRC_FILE.read_text(encoding="utf-8")) \
            if _SRC_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        raw = {}
    raw[ticker] = {"provider": provider, "days": days, "rows": rows,
                   "at": time.time()}
    try:
        _SRC_FILE.write_text(json.dumps(raw, indent=0, sort_keys=True),
                             encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass


def _source_entry(ticker: str) -> dict:
    v = _load_sources_raw().get(ticker)
    if isinstance(v, dict):
        return v
    if isinstance(v, str):                 # legacy flat format
        return {"provider": v, "days": None, "rows": None}
    return {}


def _load_sources_raw() -> dict:
    if not _SRC_FILE.exists():
        return {}
    try:
        return json.loads(_SRC_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def cache_provider(ticker: str) -> str | None:
    """Which provider produced the cached bars for `ticker`, if known."""
    return _source_entry(ticker).get("provider")


def cache_days(ticker: str) -> int | None:
    """History depth (calendar days) requested when the cache was written."""
    d = _source_entry(ticker).get("days")
    return int(d) if d else None


def cache_is_valid(ticker: str, max_age_hours: float,
                   require_provider: str | None = None,
                   require_days: int | None = None,
                   min_rows: int = 250) -> bool:
    """
    Cheap cache-validity check using only the sidecar metadata and file mtime.

    Deliberately does NOT parse the CSV. Deciding what to fetch by reading every
    cached file meant ~480 full CSV parses of 2,500 rows each before a single
    request went out, which dominated the runtime of a warm pass and made the
    fetch look rate-limited when it was not.
    """
    p = _cache_path(ticker)
    if not p.exists():
        return False
    if (time.time() - p.stat().st_mtime) / 3600.0 > max_age_hours:
        return False
    e = _source_entry(ticker)
    if require_provider is not None and e.get("provider") != require_provider:
        return False
    if require_days is not None:
        d = e.get("days")
        if not d or int(d) < require_days * 0.95:
            return False
    rows = e.get("rows")
    if rows is not None and int(rows) < min_rows:
        return False
    if rows is None:
        # Pre-metadata entry: fall back to a size heuristic rather than a parse.
        # A 250-row OHLCV CSV is comfortably over 8 KB.
        return p.stat().st_size > 8_000
    return True


def preferred_provider() -> str | None:
    try:
        act = active_providers()
    except ProviderUnavailable:
        return getattr(config, "REQUIRE_PROVIDER", None)
    return act[0] if act else None


def _read_cache(ticker: str, max_age_hours: float,
                require_provider: str | None = None,
                require_days: int | None = None) -> pd.DataFrame:
    p = _cache_path(ticker)
    if not p.exists():
        return pd.DataFrame()
    age_h = (time.time() - p.stat().st_mtime) / 3600.0
    if age_h > max_age_hours:
        return pd.DataFrame()
    if require_provider is not None:
        got = cache_provider(ticker)
        # Unknown provenance is treated as a miss: pre-provenance cache files
        # cannot be trusted to have come from the provider now in use.
        if got != require_provider:
            return pd.DataFrame()
    if require_days is not None:
        got_days = cache_days(ticker)
        # Cached with a shallower history request than now needed -> refetch.
        if got_days is None or got_days < require_days * 0.95:
            return pd.DataFrame()
    try:
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        return _normalise(df)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def history(ticker: str, days: int | None = None, max_age_hours: float = 12.0,
            use_cache: bool = True) -> pd.DataFrame:
    """Daily OHLCV for `ticker`, from cache when fresh, else from providers."""
    days = days or config.HISTORY_DAYS
    want = preferred_provider() if config.STRICT_CACHE_PROVENANCE else None

    if use_cache:
        cached = _read_cache(ticker, max_age_hours, require_provider=want,
                             require_days=days)
        if len(cached) > 250:
            return cached
        if ticker in _load_misses():
            return _read_cache(ticker, max_age_hours=24 * 365)

    for prov in active_providers():
        df = _HISTORY_FNS[prov](ticker, days)
        if len(df) > 100:
            df.to_csv(_cache_path(ticker))
            _record_source(ticker, prov, days=days, rows=len(df))
            return df

    # Everything failed — fall back to a stale cache rather than nothing.
    stale = _read_cache(ticker, max_age_hours=24 * 365)
    if stale.empty:
        _record_miss(ticker)
    else:
        print(f"    ~ {ticker}: using stale cache "
              f"({len(stale)} rows, source={cache_provider(ticker) or 'unknown'})")
    return stale


def batch_history(tickers: list[str], days: int | None = None,
                  max_age_hours: float = 12.0, pause: float = 0.12,
                  label: str = "") -> dict[str, pd.DataFrame]:
    """Fetch many tickers with progress output. Missing tickers are skipped."""
    out: dict[str, pd.DataFrame] = {}
    total = len(tickers)
    hits = 0
    width = 28
    for i, t in enumerate(tickers, 1):
        # Only rate-limit actual network calls. Applying the pause to cache hits
        # turned a warm 400-ticker run into a minute of pure sleeping.
        want = preferred_provider() if config.STRICT_CACHE_PROVENANCE else None
        from_cache = cache_is_valid(t, max_age_hours, require_provider=want,
                                    require_days=days or config.HISTORY_DAYS)
        df = history(t, days=days, max_age_hours=max_age_hours)
        if not df.empty:
            out[t] = df
        hits += int(from_cache)
        filled = int(width * i / total)
        bar = "█" * filled + "·" * (width - filled)
        print(f"\r  {label}[{bar}] {i}/{total}  ok={len(out)}  cached={hits}  {t:<9}",
              end="", flush=True)
        if not from_cache:
            time.sleep(pause)
    print(f"\r  {label}[{'█' * width}] {total}/{total}  ok={len(out)}  "
          f"cached={hits}{' ' * 12}")
    return out


# ---------------------------------------------------------------------------
# ETF holdings (breadth universe) — FMP only; static fallback otherwise
# ---------------------------------------------------------------------------
def etf_constituents(ticker: str, limit: int = 25,
                     include_historical: bool | None = None) -> list[str]:
    """
    Constituent sample for breadth.

    Historical (since-delisted) members are appended when available, which is the
    survivorship-bias correction: breadth computed only from names that survived
    to today is biased upward in the past. Each name contributes only on dates
    where it has data, so the delisted ones drop out naturally after their last
    session.
    """
    meta = config.SECTORS.get(ticker, {})
    static = list(meta.get("constituents", []))

    if include_historical is None:
        include_historical = getattr(config, "INCLUDE_HISTORICAL_MEMBERS", False)
    hist = (getattr(config, "HISTORICAL_MEMBERS", {}) or {}).get(ticker, []) \
        if include_historical else []
    # De-duplicate while preserving order, current members first.
    seen: set[str] = set()
    static = [t for t in (static + list(hist)) if not (t in seen or seen.add(t))]

    if config.FMP_API_KEY:
        for url in (
            f"https://financialmodelingprep.com/stable/etf/holdings"
            f"?symbol={ticker}&apikey={config.FMP_API_KEY}",
            f"https://financialmodelingprep.com/api/v3/etf-holder/{ticker}"
            f"?apikey={config.FMP_API_KEY}",
        ):
            data = _get_json(url)
            if isinstance(data, list) and data:
                key = "asset" if "asset" in data[0] else "symbol"
                wkey = "weightPercentage" if "weightPercentage" in data[0] else "weight"
                rows = [d for d in data if d.get(key)]
                try:
                    rows.sort(key=lambda d: float(d.get(wkey) or 0), reverse=True)
                except Exception:  # noqa: BLE001, S110
                    pass
                live = [str(d[key]).strip().upper().replace(".", "-")
                        for d in rows][:limit]
                live = [s for s in live if s.isalpha() or "-" in s]
                if len(live) >= 8:
                    # Live holdings are current-only, so historical members still
                    # need appending to avoid reintroducing survivorship bias.
                    seen2 = set(live)
                    return live + [h for h in hist if h not in seen2]
    return static[: limit + len(hist)]


# ---------------------------------------------------------------------------
# Off-exchange volume share (real dark pool proxy) — Polygon tick data
# ---------------------------------------------------------------------------
def off_exchange_share(ticker: str, lookback_days: int = 10) -> dict | None:
    """
    Share of consolidated volume printed off-exchange, per day.

    Polygon tags every trade with an exchange id; id 4 is the FINRA/TRF tape,
    which is where ATS (dark pool) and other off-exchange prints are reported.
    A rising off-exchange share while price is flat is the classic institutional
    accumulation signature.

    Requires a Polygon plan with /v3/trades. Returns None if unavailable.
    """
    if not (config.POLYGON_API_KEY and config.ENABLE_POLYGON_OFF_EXCHANGE):
        return None

    sym = ticker.replace("-", ".")
    daily: dict[str, dict[str, float]] = {}
    px = history(ticker)
    if px.empty:
        return None
    sessions = [d.date().isoformat() for d in px.index[-lookback_days:]]

    for day in sessions:
        off = tot = 0.0
        url = (f"https://api.polygon.io/v3/trades/{sym}"
               f"?timestamp={day}&limit=50000&apiKey={config.POLYGON_API_KEY}")
        pages = 0
        while url and pages < 20:
            data = _get_json(url, timeout=45)
            if not isinstance(data, dict):
                break
            for tr in data.get("results", []) or []:
                size = float(tr.get("size") or 0)
                tot += size
                if int(tr.get("exchange") or 0) == 4:
                    off += size
            nxt = data.get("next_url")
            url = f"{nxt}&apiKey={config.POLYGON_API_KEY}" if nxt else None
            pages += 1
            time.sleep(0.1)
        if tot > 0:
            daily[day] = {"off": off, "total": tot, "share": off / tot}

    if not daily:
        return None
    shares = [v["share"] for v in daily.values()]
    return {
        "daily": daily,
        "mean_share": sum(shares) / len(shares),
        "latest_share": shares[-1],
        "trend": shares[-1] - (sum(shares[:-1]) / max(len(shares) - 1, 1)),
    }


def short_interest(ticker: str, max_age_hours: float = 24.0,
                   limit: int = 500) -> pd.DataFrame:
    """
    Semi-monthly short interest from Polygon.

    Already entitled on the standard key — verified 206 records for XLE back to
    2017-12, at a median 15-day settlement cadence which matches the exchange
    reporting cycle.

    Columns: short_interest (shares), avg_daily_volume, days_to_cover.

    `days_to_cover` is the useful one: short interest divided by average daily
    volume, i.e. how many sessions of normal trading it would take shorts to
    exit. That is the direct measure of the "crowded short" condition the source
    document's divergence example rests on.

    Semi-monthly with a settlement lag, so treat it as a positioning indicator,
    not a timing one.
    """
    if not config.POLYGON_API_KEY:
        return pd.DataFrame()

    p = config.CACHE_DIR / f"si_{ticker.replace('/', '_')}.csv"
    if p.exists() and (time.time() - p.stat().st_mtime) / 3600.0 < max_age_hours:
        try:
            df = pd.read_csv(p, index_col=0, parse_dates=True)
            if not df.empty:
                return df
        except Exception:  # noqa: BLE001, S110
            pass

    sym = ticker.replace("-", ".")
    url = (f"https://api.polygon.io/stocks/v1/short-interest"
           f"?ticker={sym}&limit={limit}&apiKey={config.POLYGON_API_KEY}")
    data = _get_json(url)
    if not isinstance(data, dict) or not data.get("results"):
        return pd.DataFrame()

    df = pd.DataFrame(data["results"])
    if "settlement_date" not in df.columns:
        return pd.DataFrame()
    df["settlement_date"] = pd.to_datetime(df["settlement_date"], errors="coerce")
    df = df.dropna(subset=["settlement_date"]).set_index("settlement_date").sort_index()
    for c in ("short_interest", "avg_daily_volume", "days_to_cover"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    keep = [c for c in ("short_interest", "avg_daily_volume", "days_to_cover")
            if c in df.columns]
    df = df[keep].dropna(how="all")
    if not df.empty:
        try:
            df.to_csv(p)
        except Exception:  # noqa: BLE001, S110
            pass
    return df


def provider_status() -> dict:
    from collections import Counter
    src = Counter(_load_sources().values())
    return {
        "fmp": bool(config.FMP_API_KEY),
        "polygon": bool(config.POLYGON_API_KEY),
        "yahoo": True,
        "off_exchange": bool(config.POLYGON_API_KEY and config.ENABLE_POLYGON_OFF_EXCHANGE),
        "active_order": active_providers(),
        "preferred": preferred_provider(),
        "cache_sources": dict(src),
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def cache_audit() -> dict:
    """
    Which provider produced the bars currently on disk.

    Run this before trusting a backtest. A mixed-provenance cache means some
    series are Polygon-adjusted and others are Yahoo-adjusted, and any
    cross-sectional metric computed across them is comparing unlike things.
    """
    from collections import Counter
    src = _load_sources()
    tracked = [config.BENCHMARK] + config.ALL_TICKERS
    for meta in config.SECTORS.values():
        tracked.extend(meta.get("constituents", []))
    seen: set[str] = set()
    tracked = [t for t in tracked if not (t in seen or seen.add(t))]

    on_disk = [t for t in tracked if _cache_path(t).exists()]
    counts = Counter(src.get(t, "unknown") for t in on_disk)
    pref = preferred_provider()
    stale_prov = [t for t in on_disk if src.get(t) != pref]
    return {
        "preferred_provider": pref,
        "cached_series": len(on_disk),
        "by_provider": dict(counts),
        "not_from_preferred": stale_prov,
        "mixed": len([k for k in counts if k != "unknown"]) > 1,
    }
