"""
Macro weather — Step 1 of the framework.

The rotation research specifies central-bank liquidity as the top tier of the
hierarchy: Fed total assets (WALCL) and the overnight reverse repo facility
(RRPONTSYD). The dashboard previously substituted a price-based regime (SPY versus
its 200-day MA), which measures market *state* rather than liquidity, and is
therefore a proxy for the thing the framework actually asks about.

No API key required. FRED's graph CSV endpoint serves these series openly, which
avoids adding another credential for four public time series.

Series and why each is here
---------------------------
WALCL       Fed total assets, weekly. Expanding = liquidity being added.
RRPONTSYD   Overnight reverse repo, daily. Cash parked at the Fed rather than in
            risk assets — a *falling* balance releases liquidity into markets, so
            its sign is inverted relative to WALCL.
WTREGEN     Treasury General Account. Drawdowns inject liquidity; rebuilds drain it.
NFCI        Chicago Fed National Financial Conditions Index. Negative = looser than
            average. A single well-constructed summary of credit conditions.
T10Y2Y      10y-2y spread. Curve shape, which drives the cyclical-vs-defensive call.
DGS10       10-year yield level, for rate-sensitivity context.
"""
from __future__ import annotations

import io
import json
import time
import urllib.request
from datetime import datetime

import numpy as np
import pandas as pd

from . import config

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
FRED_API = ("https://api.stlouisfed.org/fred/series/observations"
            "?series_id={sid}&api_key={key}&file_type=json")
CACHE_DIR = config.CACHE_DIR / "fred"
CACHE_DIR.mkdir(exist_ok=True)

SERIES = {
    "WALCL": {"name": "Fed total assets", "unit": "$M", "direction": +1,
              "note": "expanding balance sheet adds liquidity"},
    "RRPONTSYD": {"name": "Overnight reverse repo", "unit": "$B", "direction": -1,
                  "note": "cash parked at the Fed; falling balance releases liquidity"},
    "WTREGEN": {"name": "Treasury General Account", "unit": "$B", "direction": -1,
                "note": "drawdowns inject liquidity, rebuilds drain it"},
    "NFCI": {"name": "Financial Conditions Index", "unit": "index", "direction": -1,
             "note": "negative is looser than average"},
    "T10Y2Y": {"name": "10y-2y spread", "unit": "%", "direction": 0,
               "note": "curve shape; drives the cyclical-vs-defensive call"},
    "DGS10": {"name": "10-year Treasury yield", "unit": "%", "direction": 0,
              "note": "rate-sensitivity context"},
}

# Weights for the composite liquidity impulse. Only `direction != 0` series
# contribute; the curve and yield level are reported as context, not scored.
LIQUIDITY_WEIGHTS = {"WALCL": 0.40, "RRPONTSYD": 0.25,
                     "WTREGEN": 0.15, "NFCI": 0.20}

_CACHE_HOURS = 12.0


def _parse_csv(raw: str, sid: str) -> pd.Series:
    """Keyless fredgraph CSV: first column dates, second column values."""
    df = pd.read_csv(io.StringIO(raw))
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col]).set_index(date_col)
    return pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()


def _parse_json(raw: str, sid: str) -> pd.Series:
    """FRED JSON API: {"observations":[{"date","value"},...]}. Missing = '.'."""
    doc = json.loads(raw)
    obs = doc.get("observations", [])
    idx = pd.to_datetime([o.get("date") for o in obs], errors="coerce")
    val = pd.to_numeric([o.get("value") for o in obs], errors="coerce")
    s = pd.Series(val, index=idx).dropna()
    return s[s.index.notna()]


def _read_cache(p) -> pd.Series:
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    return pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()


def fetch_series(sid: str, max_age_hours: float = _CACHE_HOURS) -> pd.Series:
    """
    One FRED series, cached to disk. Returns an empty Series on failure.

    Prefers the keyed JSON API when FRED_API_KEY is set — it is the reliable,
    rate-limited endpoint meant for automated use. Falls back to the keyless
    fredgraph CSV export otherwise. Either way the result is cached, and a stale
    cache is used if the network fails, so a blocked FRED never breaks the run.
    """
    p = CACHE_DIR / f"{sid}.csv"
    if p.exists() and (time.time() - p.stat().st_mtime) / 3600.0 < max_age_hours:
        try:
            return _read_cache(p)
        except Exception:  # noqa: BLE001, S110
            pass

    key = getattr(config, "FRED_API_KEY", "")
    timeout = getattr(config, "FRED_TIMEOUT_SEC", 8)
    if key:
        url, parse = FRED_API.format(sid=sid, key=key), _parse_json
    else:
        url, parse = FRED_CSV.format(sid=sid), _parse_csv
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "smf/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
        s = parse(raw, sid)
        if len(s):
            s.to_frame(sid).to_csv(p)
        return s
    except Exception as exc:  # noqa: BLE001
        via = "API" if key else "CSV"
        print(f"    ! FRED {sid} ({via}) failed: {type(exc).__name__}")
        if p.exists():
            try:
                return _read_cache(p)
            except Exception:  # noqa: BLE001, S110
                pass
        return pd.Series(dtype=float)


def _trend(s: pd.Series, weeks: int = 13) -> dict:
    """Level, change and z-scored momentum over roughly a quarter."""
    if len(s) < 10:
        return {}
    latest = float(s.iloc[-1])
    # observations per week varies by series (daily vs weekly), so work in dates
    cutoff = s.index[-1] - pd.Timedelta(weeks=weeks)
    prior = s[s.index <= cutoff]
    prev = float(prior.iloc[-1]) if len(prior) else float("nan")
    chg = latest - prev if np.isfinite(prev) else float("nan")
    pct = (chg / abs(prev) * 100.0) if np.isfinite(prev) and prev != 0 else float("nan")

    # z-score of the change distribution over ~3 years of same-length changes
    look = s[s.index >= s.index[-1] - pd.Timedelta(weeks=156)]
    if len(look) > 20:
        step = max(1, int(len(look) * weeks / 156))
        diffs = look.diff(step).dropna()
        z = ((chg - diffs.mean()) / diffs.std(ddof=0)
             if len(diffs) > 5 and diffs.std(ddof=0) > 0 else float("nan"))
    else:
        z = float("nan")
    return {"latest": round(latest, 4),
            "as_of": s.index[-1].strftime("%Y-%m-%d"),
            "change_13w": round(chg, 4) if np.isfinite(chg) else None,
            "change_13w_pct": round(pct, 2) if np.isfinite(pct) else None,
            "change_z": round(float(z), 2) if np.isfinite(z) else None}


def macro_weather(max_age_hours: float = _CACHE_HOURS) -> dict:
    """
    Composite liquidity impulse plus per-series detail.

    The impulse is a weighted sum of z-scored 13-week changes, sign-corrected so
    that positive always means "liquidity being added". RRPONTSYD, WTREGEN and NFCI
    are inverted: a falling reverse-repo balance or a loosening NFCI *adds*
    liquidity, so their raw declines must count as positive.
    """
    out: dict[str, dict] = {}
    for sid in SERIES:
        s = fetch_series(sid, max_age_hours)
        t = _trend(s)
        if t:
            out[sid] = {**SERIES[sid], **t}

    contribs, weights = [], []
    for sid, w in LIQUIDITY_WEIGHTS.items():
        d = out.get(sid)
        if not d or d.get("change_z") is None:
            continue
        signed = d["change_z"] * SERIES[sid]["direction"]
        d["signed_z"] = round(signed, 2)
        contribs.append(signed * w)
        weights.append(w)

    impulse = (sum(contribs) / sum(weights)) if weights else float("nan")

    if not np.isfinite(impulse):
        regime, note = "UNKNOWN", "FRED series unavailable"
    elif impulse > 0.5:
        regime = "EXPANDING"
        note = "Liquidity is being added — historically supportive of risk assets"
    elif impulse > -0.5:
        regime = "NEUTRAL"
        note = "Liquidity roughly flat — sector rotation matters more than direction"
    else:
        regime = "DRAINING"
        note = "Liquidity is being withdrawn — favours defensives and quality"

    curve = out.get("T10Y2Y", {}).get("latest")
    curve_note = None
    if curve is not None:
        curve_note = ("Curve inverted — historically a late-cycle signal"
                      if curve < 0 else
                      "Curve steepening" if curve > 0.5 else
                      "Curve flat")

    return {
        "liquidity_impulse": round(float(impulse), 3) if np.isfinite(impulse) else None,
        "regime": regime,
        "note": note,
        "series": out,
        "curve_10y2y": curve,
        "curve_note": curve_note,
        "ten_year_yield": out.get("DGS10", {}).get("latest"),
        "components_used": len(weights),
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": ("FRED JSON API" if getattr(config, "FRED_API_KEY", "")
                   else "FRED fredgraph CSV (keyless)"),
    }
