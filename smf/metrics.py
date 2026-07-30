"""
Metrics engine.

Everything here is pure pandas/numpy on OHLCV frames — no network calls — so it
is deterministic and easy to unit test.

Implements:
  * Dorsey Relative Strength         (raw sector/benchmark price ratio)
  * Mansfield Relative Strength      (Weinstein stage-analysis normalised RS)
  * JdK RS-Ratio / RS-Momentum       (Relative Rotation Graph coordinates)
  * Constituent breadth              (% above 50d SMA)
  * Chaikin Money Flow               (volume-weighted accumulation)
  * Volume anomaly z-score           (unusual trading detection)
  * Accumulation/Distribution days   (IBD-style institutional footprint)
  * Absorption score                 (high volume, no price progress = stealth bid)
  * Block intensity                  (share of volume in outsized prints)
  * Weinstein stage classification   (Stage 1-4)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _rolling_z(s: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    min_periods = min_periods or max(20, window // 4)
    mu = s.rolling(window, min_periods=min_periods).mean()
    sd = s.rolling(window, min_periods=min_periods).std(ddof=0)
    return ((s - mu) / sd.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan)


def _last(s: pd.Series, default: float = float("nan"),
          as_of=None, max_stale_days: int | None = None) -> float:
    """
    Latest finite value.

    With `as_of` and `max_stale_days`, refuses to return a reading older than the
    tolerance. Previously a series whose tail was all-NaN silently returned a value
    from months earlier, reported alongside today's `as_of` with nothing marking it
    stale — a metric can go dark and the dashboard shows the last good number as if
    it were current.
    """
    s = s.dropna()
    if not len(s):
        return default
    if as_of is not None and max_stale_days is not None:
        try:
            age = (pd.Timestamp(as_of) - pd.Timestamp(s.index[-1])).days
            if age > max_stale_days:
                return default
        except Exception:  # noqa: BLE001, S110
            pass
    return float(s.iloc[-1])


def _staleness(s: pd.Series, as_of) -> int | None:
    """Calendar days between `as_of` and the series' last finite observation."""
    s = s.dropna()
    if not len(s):
        return None
    try:
        return int((pd.Timestamp(as_of) - pd.Timestamp(s.index[-1])).days)
    except Exception:  # noqa: BLE001
        return None


def _tail(s: pd.Series, n: int, dp: int = 4) -> list[float]:
    """
    Last `n` finite observations, rounded.

    `dp` matters more than it looks: these arrays are ~60% of the dashboard's
    size, and four decimals on a dollar price or an eight-digit volume is pure
    file size for precision nothing renders.
    """
    v = s.dropna().tail(n)
    if dp <= 0:
        return [int(round(float(x))) for x in v]
    return [round(float(x), dp) for x in v]


def _spark(s: pd.Series, n: int = config.SPARK_LEN,
           points: int | None = None, dp: int = 3) -> list[float]:
    """
    Display series: take the last `n` observations, then decimate to `points`.

    Decimation keeps the first and last observation exactly — the last one is
    "today" and is drawn as the endpoint marker, so it must not be resampled
    away — and spaces the rest evenly. Rounded to 3dp rather than 4 because
    nothing downstream draws finer than a pixel.
    """
    v = s.dropna().tail(n)
    pts = points if points is not None else config.SPARK_POINTS
    if pts and len(v) > pts:
        idx = np.linspace(0, len(v) - 1, pts).round().astype(int)
        idx = np.unique(idx)
        v = v.iloc[idx]
    if dp <= 0:
        return [int(round(float(x))) for x in v]
    return [round(float(x), dp) for x in v]


def _tail_dates(s: pd.Series, n: int) -> list[str]:
    """
    ISO dates for the same observations `_tail(s, n)` would return.

    Kept adjacent to `_tail` on purpose: the two must stay in lockstep. Deriving
    dates from a different slice than the values is what produced the 108-session
    x-axis error, and `dropna()` is the subtle part — a series with interior gaps
    has fewer points than the last `n` calendar sessions, so slicing the index
    directly does not align.
    """
    return [d.strftime("%Y-%m-%d") for d in s.dropna().tail(n).index]


# ---------------------------------------------------------------------------
# Relative strength family
# ---------------------------------------------------------------------------
def dorsey_rs(sector_close: pd.Series, bench_close: pd.Series) -> pd.Series:
    """Raw relative-strength ratio, indexed to 100 at the start of the window."""
    aligned = pd.concat([sector_close, bench_close], axis=1, join="inner").dropna()
    if aligned.empty:
        return pd.Series(dtype=float)
    rs = aligned.iloc[:, 0] / aligned.iloc[:, 1]
    return (rs / rs.iloc[0]) * 100.0


def mansfield_rs(rs: pd.Series, n: int = config.MANSFIELD_SMA) -> pd.Series:
    """
    Mansfield RS = ((RS / SMA_n(RS)) - 1) * 100

    Zero-bounded: > 0 means the sector is outperforming its own long-run
    relative-strength trend. An upward zero-cross is the accumulation trigger.
    """
    if rs.empty:
        return pd.Series(dtype=float)
    sma = rs.rolling(n, min_periods=max(30, n // 4)).mean()
    return ((rs / sma) - 1.0) * 100.0


def rrg_raw(rs: pd.Series,
            short: int = config.RRG_SHORT,
            long: int = config.RRG_LONG,
            mom: int = config.RRG_MOM) -> tuple[pd.Series, pd.Series]:
    """
    Un-normalised RRG inputs for one sector.

    ratio_raw : SMA_short(RS) / SMA_long(RS) — is relative strength trending up?
    mom_raw   : ratio_raw / SMA_mom(ratio_raw) — is that trend accelerating?

    These are deliberately *not* normalised here. Normalisation must happen
    cross-sectionally (see `normalise_rrg`) because 100 on a Relative Rotation
    Graph means "in line with the peer group", not "in line with this sector's
    own recent history". Normalising each sector against itself makes a sector
    in sustained freefall score above 100 the moment its decline decelerates,
    which inverts the whole point of the quadrants.
    """
    if len(rs) < long + 20:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    ratio_raw = 100.0 * (rs.rolling(short, min_periods=short).mean()
                         / rs.rolling(long, min_periods=long).mean())
    mom_raw = 100.0 * (ratio_raw / ratio_raw.rolling(mom, min_periods=mom).mean())
    return ratio_raw, mom_raw


def normalise_rrg(raw_by_ticker: dict[str, pd.Series],
                  clip: float = 4.0) -> dict[str, pd.Series]:
    """
    Cross-sectional normalisation: at each date, z-score across the peer group
    and centre on 100.

    Called separately for each tier so that 11 broad GICS sectors are not
    normalised against 21 narrow industry ETFs (which have far wider dispersion
    and would compress the sector readings toward 100).

    Guarantees the useful property that roughly half the group sits either side
    of 100 on any given day — i.e. the quadrants describe genuine relative
    position, which is what makes rotation visible.
    """
    usable = {k: v for k, v in raw_by_ticker.items() if v is not None and not v.empty}
    if len(usable) < 3:
        # Too small a peer group for cross-sectional stats — fall back to a long
        # self-referencing window, which is at least stable.
        return {k: 100.0 + _rolling_z(v, 252).clip(-clip, clip) for k, v in usable.items()}

    mat = pd.DataFrame(usable)
    mu = mat.mean(axis=1)
    sd = mat.std(axis=1, ddof=0).replace(0.0, np.nan)
    z = mat.sub(mu, axis=0).div(sd, axis=0).clip(-clip, clip)
    return {c: 100.0 + z[c] for c in z.columns}


def rrg_quadrant(x: float, y: float) -> str:


    if not (np.isfinite(x) and np.isfinite(y)):
        return "Unknown"
    if x >= 100 and y >= 100:
        return "Leading"
    if x < 100 <= y:
        return "Improving"
    if x >= 100 > y:
        return "Weakening"
    return "Lagging"


# ---------------------------------------------------------------------------
# Breadth
# ---------------------------------------------------------------------------
MIN_BREADTH_SAMPLE = 8


def breadth_above_sma(constituent_closes: dict[str, pd.Series],
                      n: int = config.BREADTH_SMA) -> pd.Series:
    """
    % of constituents trading above their n-day SMA.

    This is the participation check: a sector ETF can be dragged up by two
    mega-cap weights while the median constituent bleeds. Broad accumulation
    shows up here first.

    Returns an empty series below MIN_BREADTH_SAMPLE resolvable constituents —
    a "0% breadth" reading computed from two tickers is worse than no reading,
    because it looks authoritative and feeds straight into the composite score.
    """
    flags = {}
    for tkr, close in constituent_closes.items():
        if close is None or len(close) < n + 5:
            continue
        sma = close.rolling(n, min_periods=n).mean()
        flags[tkr] = (close > sma).astype(float).where(sma.notna())
    if len(flags) < MIN_BREADTH_SAMPLE:
        return pd.Series(dtype=float)
    mat = pd.DataFrame(flags)
    valid = mat.notna().sum(axis=1)
    return (mat.sum(axis=1) / valid.replace(0, np.nan)) * 100.0


# ---------------------------------------------------------------------------
# Volume / money-flow family
# ---------------------------------------------------------------------------
def chaikin_money_flow(df: pd.DataFrame, n: int = config.CMF_PERIOD) -> pd.Series:
    """
    CMF = sum(MFV, n) / sum(volume, n),  MFV = ((C-L)-(H-C))/(H-L) * V

    Bounded -1..+1. Above +0.05 = accumulation, below -0.05 = distribution.
    """
    hl = (df["high"] - df["low"]).replace(0.0, np.nan)
    mult = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl
    mfv = (mult * df["volume"]).fillna(0.0)
    vol_sum = df["volume"].rolling(n, min_periods=max(5, n // 3)).sum()
    return mfv.rolling(n, min_periods=max(5, n // 3)).sum() / vol_sum.replace(0.0, np.nan)


def volume_zscore(df: pd.DataFrame, window: int = config.VOLUME_ZSCORE_WINDOW) -> pd.Series:
    """Unusual-volume detector on log volume (volume is heavily right-skewed)."""
    lv = np.log1p(df["volume"].astype(float))
    return _rolling_z(lv, window)


def dollar_volume_zscore(df: pd.DataFrame,
                         window: int = config.VOLUME_ZSCORE_WINDOW) -> pd.Series:
    ldv = np.log1p((df["volume"] * df["close"]).astype(float))
    return _rolling_z(ldv, window)


def ad_day_balance(df: pd.DataFrame, window: int = config.ADLINE_WINDOW) -> pd.Series:
    """
    Accumulation minus distribution days over `window`, scaled to -1..+1.

    An accumulation day = close up on above-average volume (institutions have to
    pay up to get filled). A distribution day = close down on above-average
    volume. The net balance is one of the oldest and most robust proxies for
    which side is being forced to cross the spread.
    """
    ret = df["close"].pct_change()
    avg_vol = df["volume"].rolling(50, min_periods=20).mean()
    heavy = df["volume"] > avg_vol
    acc = ((ret > 0.002) & heavy).astype(float)
    dist = ((ret < -0.002) & heavy).astype(float)
    net = (acc - dist).rolling(window, min_periods=max(5, window // 3)).sum()
    return net / float(window)


def absorption_score(df: pd.DataFrame, window: int = config.ADLINE_WINDOW) -> pd.Series:
    """
    Stealth-accumulation proxy: heavy volume with the close pinned high in the
    range but little net price progress.

    When a large buyer works an order through dark pools and VWAP algos, the
    tape shows unusually high volume, a close in the upper half of the daily
    range, and a suppressed daily return — supply is being absorbed without
    advertising the bid. That combination is what this scores.
    """
    hl = (df["high"] - df["low"]).replace(0.0, np.nan)
    clv = ((df["close"] - df["low"]) / hl)          # close location value 0..1
    volz = volume_zscore(df)
    ret_abs = df["close"].pct_change().abs()
    quiet = 1.0 / (1.0 + (ret_abs / 0.004).fillna(0.0))   # 1 when flat, ->0 when trending

    daily = (clv - 0.5) * 2.0 * volz.clip(lower=0) * quiet
    return daily.rolling(window, min_periods=max(5, window // 3)).mean()


def block_intensity(df: pd.DataFrame, window: int = config.ADLINE_WINDOW) -> pd.Series:
    """
    Proxy for block-trade participation from daily bars.

    True block prints need tick data. From daily bars the observable signature
    is a volume distribution with a fat right tail: a handful of sessions
    absorbing a disproportionate share of the period's volume. This measures
    that concentration as the excess of the top-3 sessions' volume share over
    what a uniform distribution would give.
    """
    def _concentration(v: np.ndarray) -> float:
        tot = v.sum()
        if tot <= 0 or len(v) < 5:
            return np.nan
        k = max(3, len(v) // 8)
        top = np.sort(v)[-k:].sum() / tot
        return float(top - (k / len(v)))

    return (df["volume"].astype(float)
            .rolling(window, min_periods=max(5, window // 2))
            .apply(_concentration, raw=True))


def momentum_12_1(close: pd.Series,
                  lookback: int = None,
                  skip: int = None) -> float:
    """
    12-1 momentum: trailing 12-month return, skipping the most recent month.

    The skip matters. Including the last month contaminates the signal with
    short-horizon reversal, which the backtest confirmed is real — every model
    tested had NEGATIVE rank IC at a 5-session horizon while being positive at
    10-63 sessions.

    This is the single strongest predictor found in the whole project: rank IC
    +0.050 full-sample, positive in all four subperiods and both tiers, and it
    strengthened as the cross-section widened.
    """
    # `or` discards an explicit 0. Verified: momentum_12_1(c, skip=0) silently
    # returned the skip=21 value, and lookback=0 returned a full 252-day return.
    lookback = config.MOM_LOOKBACK if lookback is None else lookback
    skip = config.MOM_SKIP if skip is None else skip
    if lookback <= 0:
        return float("nan")
    s = close.dropna()
    if len(s) < lookback + skip + 2:
        return float("nan")
    return float(s.iloc[-1 - skip] / s.iloc[-1 - skip - lookback] - 1.0) * 100.0


def momentum_12_1_series(close: pd.Series,
                         lookback: int = None,
                         skip: int = None) -> pd.Series:
    """
    `momentum_12_1` evaluated at every date, for the VMS sparkline.

    Deliberately built from the same shift arithmetic as the scalar version so
    the last point of this series equals the scalar reading — a sparkline whose
    endpoint disagreed with the number printed beside it would be worse than no
    sparkline at all.
    """
    lookback = config.MOM_LOOKBACK if lookback is None else lookback
    skip = config.MOM_SKIP if skip is None else skip
    if lookback <= 0:
        return pd.Series(dtype=float)
    s = close.dropna()
    if len(s) < lookback + skip + 2:
        return pd.Series(dtype=float)
    return (s.shift(skip) / s.shift(skip + lookback) - 1.0) * 100.0


def volume_trend(df: pd.DataFrame, short: int = 20, long: int = 60) -> pd.Series:
    """
    Volume REGIME — expanding or contracting — as a percentage.

    `volume_zscore` answers "is today unusual". This answers "is volume
    structurally rising or falling over weeks", which is a different question and
    the one the source framework actually asks: Stage 1 is defined by *contracting*
    volume during quiet accumulation, Stage 2 by *expanding* volume during markup.

    Uses medians rather than means so a single 10x session does not create a phantom
    expansion regime.

    Positive = expanding. Roughly +25% or more is a genuine expansion regime;
    −20% or less is contraction.

    The baseline window is LAGGED by `short` so it does not include the recent
    window it is being compared against. Comparing a 20-day median to a 60-day
    median that contains those same 20 days means a sustained shift gets absorbed
    into its own baseline: a 40-session expansion reads as 0% because the 60-day
    median has already moved up with it. That made the metric a pure *change*
    detector that decayed to zero exactly when a regime became established — the
    opposite of what the framework's "sustained markup on expanding volume"
    requires, and it capped the measurable regime length at ~30 sessions.
    """
    v = df["volume"].replace(0, np.nan)
    recent = v.rolling(short, min_periods=max(5, short // 2)).median()
    prior = (v.rolling(long, min_periods=max(15, long // 2))
              .median().shift(short))
    return ((recent / prior) - 1.0) * 100.0


def volume_regime_profile(df: pd.DataFrame, short: int = 20, long: int = 60,
                          hist: int = 252) -> dict:
    """
    Volume regime with STRENGTH and LENGTH, not just magnitude.

    `volume_trend` answers "how much" — a single percentage. Two things it cannot
    tell you, and both change the interpretation:

    STRENGTH — is this expansion unusual *for this sector*?
        A +30% expansion is routine for a thin thematic ETF and extreme for XLP.
        Judged as a z-score and percentile against the sector's own 252-day history
        of volume-trend readings, so the comparison is self-referential rather than
        against an arbitrary cutoff.

    LENGTH — how long has the regime held?
        A 3-session expansion is noise. A 40-session expansion is a regime, and the
        framework's Stage 2 markup is explicitly a sustained condition. Counted as
        consecutive sessions on the current side of the expansion/contraction
        thresholds.

    Also reports whether the trend is itself accelerating or decaying, which
    distinguishes a regime that is building from one that is rolling over.
    """
    vt = volume_trend(df, short, long).dropna()
    if len(vt) < 30:
        return {"volume_trend_pct": None, "strength_z": None,
                "strength_pct": None, "regime_days": None, "note": "insufficient history"}

    now = float(vt.iloc[-1])
    look = vt.tail(hist)

    sd = float(look.std(ddof=0))
    z = (now - float(look.mean())) / sd if sd > 0 else 0.0
    pctile = float((look < now).mean() * 100)

    # Length: consecutive sessions on the current side of the thresholds.
    EXP, CON = 15.0, -10.0
    if now > EXP:
        regime = "expanding"
        mask = vt > EXP
    elif now < CON:
        regime = "contracting"
        mask = vt < CON
    else:
        regime = "flat"
        mask = (vt >= CON) & (vt <= EXP)
    run = 0
    for v in reversed(mask.to_numpy()):
        if v:
            run += 1
        else:
            break

    # Is the regime building or rolling over?
    slope = None
    if len(vt) > 22:
        slope = now - float(vt.iloc[-22])

    if abs(z) < 0.75:
        strength = "unremarkable"
    elif abs(z) < 1.5:
        strength = "notable"
    elif abs(z) < 2.5:
        strength = "strong"
    else:
        strength = "extreme"

    return {
        "volume_trend_pct": round(now, 1),
        "volume_regime": regime,
        "strength_z": round(float(z), 2),
        "strength_pct": round(pctile, 0),
        "strength_label": strength,
        "regime_days": int(run),
        "regime_sustained": bool(run >= 20),
        "trend_slope_21d": round(slope, 1) if slope is not None else None,
        "trend_direction": (None if slope is None else
                            "building" if slope > 5 else
                            "rolling over" if slope < -5 else "steady"),
        "note": (f"{regime} {now:+.0f}% for {run} sessions "
                 f"({strength}, {pctile:.0f}th percentile of own history)"),
    }


def distribution_days(df: pd.DataFrame, window: int = 25,
                      threshold: float = -0.002) -> pd.Series:
    """
    Raw count of distribution days in a rolling window — the IBD-standard form.

    A distribution day is a close down more than 0.2% on volume above the prior
    session's. Traders read the raw count directly ("5 distribution days in 4 weeks
    is a topping signal"); `ad_day_balance` nets accumulation against distribution
    and scales it, which hides exactly that.

    Volume is compared to the PRIOR SESSION rather than to an average, which is the
    conventional definition and makes the count comparable to published figures.
    """
    ret = df["close"].pct_change()
    vol_up = df["volume"] > df["volume"].shift(1)
    dist = ((ret < threshold) & vol_up).astype(float)
    return dist.rolling(window, min_periods=max(5, window // 3)).sum()


def accumulation_days(df: pd.DataFrame, window: int = 25,
                      threshold: float = 0.002) -> pd.Series:
    """Mirror of `distribution_days` — up-closes on rising volume."""
    ret = df["close"].pct_change()
    vol_up = df["volume"] > df["volume"].shift(1)
    acc = ((ret > threshold) & vol_up).astype(float)
    return acc.rolling(window, min_periods=max(5, window // 3)).sum()


def volume_price_divergence(df: pd.DataFrame, window: int = 42) -> dict:
    """
    Price advancing on CONTRACTING volume — the classic depletion signature.

    This is the inverse of `absorption_score`, and the two together cover both
    failure modes:

        absorption            volume without price  -> stealth accumulation
        volume-price divergence  price without volume -> depleting demand

    A rally on thinning participation means fewer buyers are required to lift the
    price, which is what "capital draining from a sector" looks like on the tape
    before it shows up in price.

    Both directions are reported. Price *falling* on contracting volume is the
    bullish mirror — selling pressure exhausting — which is what the framework
    describes at the end of Stage 4.
    """
    close = df["close"]
    if len(close) < window + 10:
        return {"divergence": None, "note": "insufficient history"}

    px_chg = (float(close.iloc[-1]) / float(close.iloc[-1 - window]) - 1.0) * 100.0
    vt = volume_trend(df).dropna()
    if vt.empty:
        return {"divergence": None, "note": "no volume trend"}
    v_now = float(vt.iloc[-1])

    kind, note = None, None
    if px_chg > 3.0 and v_now < -10.0:
        kind = "bearish"
        note = (f"price +{px_chg:.1f}% over {window} sessions while volume "
                f"contracted {v_now:.0f}% — advancing on thinning participation")
    elif px_chg < -3.0 and v_now < -10.0:
        kind = "bullish"
        note = (f"price {px_chg:.1f}% over {window} sessions with volume "
                f"contracting {v_now:.0f}% — selling pressure exhausting")
    elif px_chg > 3.0 and v_now > 15.0:
        kind = "confirmed_up"
        note = (f"price +{px_chg:.1f}% on volume expanding {v_now:.0f}% — "
                f"advance is participated")
    elif px_chg < -3.0 and v_now > 15.0:
        kind = "confirmed_down"
        note = (f"price {px_chg:.1f}% on volume expanding {v_now:.0f}% — "
                f"decline is participated, not drift")

    return {
        "divergence": kind,
        "price_change_pct": round(px_chg, 2),
        "volume_trend_pct": round(v_now, 1),
        "note": note,
        "window": window,
        # A bearish divergence is the depletion warning the framework describes.
        "depletion_warning": kind == "bearish",
    }


def obv_slope(df: pd.DataFrame, window: int = 21) -> pd.Series:
    """Normalised slope of On-Balance Volume — direction of cumulative flow."""
    sign = np.sign(df["close"].diff()).fillna(0.0)
    obv = (sign * df["volume"]).cumsum()
    slope = obv.diff(window) / df["volume"].rolling(window, min_periods=5).sum().replace(0, np.nan)
    return slope


# ---------------------------------------------------------------------------
# Short interest / crowded shorts
# ---------------------------------------------------------------------------
def short_interest_metrics(si: pd.DataFrame,
                           as_of: pd.Timestamp | None = None,
                           lag_days: int | None = None) -> dict:
    """
    Crowded-short positioning from semi-monthly short interest.

    `days_to_cover` is the headline: short shares divided by average daily volume,
    i.e. how many normal sessions shorts would need to exit. It is scale-free, so
    it compares across sectors of very different size in a way raw share counts do
    not.

    Everything is measured as a **percentile against the ticker's own history**
    rather than an absolute threshold. A days-to-cover of 3 is unremarkable for a
    thin thematic ETF and extreme for XLK, so a shared cutoff would be meaningless.

    Point-in-time safe: when `as_of` is supplied, only settlements on or before that
    date are used, so this can be called inside the walk-forward backtest.
    """
    if si is None or si.empty or "days_to_cover" not in si.columns:
        return {"days_to_cover": None, "dtc_percentile": None,
                "short_interest": None, "si_change_pct": None,
                "crowded_short": False, "si_as_of": None}

    # Publication lag. `settlement_date` is when the position was MEASURED, not
    # when it became public — exchanges publish roughly 8 business days later.
    # Filtering on settlement_date alone would use data that did not yet exist,
    # which is lookahead bias in a backtest and would flatter any result.
    if lag_days is None:
        lag_days = getattr(config, "SI_PUBLICATION_LAG_DAYS", 10)
    if as_of is None:
        d = si
    else:
        cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=int(lag_days))
        d = si[si.index <= cutoff]
    d = d.dropna(subset=["days_to_cover"])
    if d.empty:
        return {"days_to_cover": None, "dtc_percentile": None,
                "short_interest": None, "si_change_pct": None,
                "crowded_short": False, "si_as_of": None}

    dtc = float(d["days_to_cover"].iloc[-1])
    hist = d["days_to_cover"].dropna()
    pct = (float((hist < dtc).mean() * 100) if len(hist) >= 12 else None)

    si_now = (float(d["short_interest"].iloc[-1])
              if "short_interest" in d.columns and pd.notna(d["short_interest"].iloc[-1])
              else None)
    chg = None
    if si_now is not None and len(d) >= 7 and "short_interest" in d.columns:
        prior = d["short_interest"].dropna()
        if len(prior) >= 7:
            base = float(prior.iloc[-7:-1].mean())     # ~3 months of settlements
            if base:
                chg = (si_now / base - 1.0) * 100.0

    return {
        "days_to_cover": round(dtc, 2),
        "dtc_percentile": round(pct, 0) if pct is not None else None,
        "short_interest": si_now,
        "si_change_pct": round(chg, 1) if chg is not None else None,
        # Crowded = days-to-cover in the top quartile of this ticker's own history.
        "crowded_short": bool(pct is not None and pct >= 75),
        "si_as_of": d.index[-1].strftime("%Y-%m-%d"),
    }


def divergence_screen(sector_close: pd.Series, bench_close: pd.Series,
                      window: int = 63, min_gap: float = 8.0) -> dict:
    """
    Index-versus-sector divergence — the source document's worked example.

    That example was the NASDAQ 100 rising ~70% while software fell ~8%, a gap the
    document attributes to $24bn of crowded short positioning, and which resolved
    violently when sentiment turned.

    A divergence here is not merely underperformance: it requires the benchmark and
    the sector to be moving in **opposite directions** over the window, which is
    what makes the gap a potential snap-back rather than ordinary relative
    weakness. Ordinary underperformance is already covered by Mansfield RS.
    """
    # Positional indexing on two independently-loaded series reads the benchmark's
    # own last bar, which may be LATER than the sector's — future data. Verified to
    # produce a +6.7pp error when the benchmark cache was 10 sessions fresher.
    # dorsey_rs already joins correctly; this did not.
    aligned = pd.concat([sector_close.dropna(), bench_close.dropna()],
                        axis=1, join="inner").dropna()
    if len(aligned) <= window:
        return {"divergence": False, "gap_pct": None}
    s = aligned.iloc[:, 0]
    b = aligned.iloc[:, 1]

    p0s, p0b = float(s.iloc[-1 - window]), float(b.iloc[-1 - window])
    if p0s <= 0 or p0b <= 0:
        return {"divergence": False, "gap_pct": None, "note": "non-positive price"}
    sr = (float(s.iloc[-1]) / p0s - 1.0) * 100.0
    br = (float(b.iloc[-1]) / p0b - 1.0) * 100.0
    gap = sr - br
    opposed = (sr < 0 < br) or (br < 0 < sr)
    diverged = bool(opposed and abs(gap) >= min_gap)

    direction = None
    if diverged:
        direction = "sector lagging a rising benchmark" if sr < br else \
                    "sector leading a falling benchmark"

    return {
        "divergence": diverged,
        "gap_pct": round(gap, 2),
        "sector_return_pct": round(sr, 2),
        "benchmark_return_pct": round(br, 2),
        "opposed_direction": opposed,
        "divergence_note": direction,
        "window": window,
    }


def short_squeeze_setup(si_m: dict, div: dict, mansfield: pd.Series) -> dict:
    """
    The asymmetric setup the source document describes.

    Three conditions together, and the conjunction is the point:

      1. Crowded shorts — days-to-cover in the top quartile of the ticker's own
         history. This is the fuel.
      2. Divergence — benchmark and sector moving in opposite directions, so
         negative sentiment is already extreme rather than merely soft.
      3. Relative strength turning UP — Mansfield RS improving over 21 sessions.
         This is the spark, and without it the first two describe a value trap
         rather than a squeeze.

    Condition 3 is what separates this from catching a falling knife: crowded shorts
    in a still-deteriorating sector usually means the shorts are correct.
    """
    mrs = mansfield.dropna()
    turning = bool(len(mrs) >= 22 and float(mrs.iloc[-1]) > float(mrs.iloc[-22]))

    conds = {
        "crowded_shorts": bool(si_m.get("crowded_short")),
        "divergence": bool(div.get("divergence")),
        "rs_turning_up": turning,
    }
    n = sum(conds.values())
    return {
        "squeeze_conditions": conds,
        "squeeze_score": n,
        "squeeze_setup": n == 3,
        "squeeze_note": (
            "all three present — crowded shorts into a diverged sector with relative "
            "strength turning up" if n == 3 else
            f"{n}/3 conditions" +
            ("; relative strength has NOT turned, which is the difference between a "
             "squeeze setup and a value trap" if not turning and n == 2 else "")),
    }


# ---------------------------------------------------------------------------
# Green Light Test — the source framework's three-part entry gate
# ---------------------------------------------------------------------------
def green_light_test(df: pd.DataFrame, mansfield: pd.Series,
                     rs_ratio_series: pd.Series | None = None) -> dict:
    """
    The three "lights" from the Prahn framework, as an explicit pass/fail gate.

    All three inputs already existed as separate fields; what was missing was the
    conjunction the framework actually specifies. A sector passing one light is not
    the same claim as a sector passing all three.

      Light 1  Relative strength — outperforming the benchmark, and rising.
      Light 2  Climbing not falling — price above a rising 50-day SMA, which is the
               framework's guard against buying a falling knife.
      Light 3  Volume confirms — a volume expansion, so the move is backed by
               participation rather than drift on thin tape.

    Returns per-light booleans, the count, and the reason each passed or failed, so
    a near-miss is visible rather than collapsing to a single False.
    """
    close = df["close"]
    lights: dict[str, dict] = {}

    # ---- Light 1: relative strength -------------------------------------
    mrs = mansfield.dropna()
    if len(mrs) >= 22:
        now = float(mrs.iloc[-1])
        then = float(mrs.iloc[-22])
        rising = now > then
        ok1 = now > 0 and rising
        lights["relative_strength"] = {
            "pass": bool(ok1),
            "detail": (f"Mansfield RS {now:+.2f} "
                       f"({'above' if now > 0 else 'below'} zero), "
                       f"{'rising' if rising else 'falling'} over 21 sessions "
                       f"(from {then:+.2f})"),
        }
    else:
        lights["relative_strength"] = {"pass": False, "detail": "insufficient history"}

    # ---- Light 2: climbing, not falling ---------------------------------
    sma = close.rolling(50, min_periods=50).mean()
    if pd.notna(sma.iloc[-1]) and len(sma.dropna()) > 21:
        px = float(close.iloc[-1])
        ma = float(sma.iloc[-1])
        slope = (ma / float(sma.iloc[-22]) - 1.0) * 100.0
        above = px > ma
        ok2 = above and slope > 0
        lights["price_trend"] = {
            "pass": bool(ok2),
            "detail": (f"price {'above' if above else 'below'} 50d SMA "
                       f"({px:.2f} vs {ma:.2f}), SMA slope {slope:+.2f}% over 21 sessions"),
        }
    else:
        lights["price_trend"] = {"pass": False, "detail": "insufficient history"}

    # ---- Light 3: volume confirms ---------------------------------------
    vz = volume_zscore(df).dropna()
    if len(vz):
        # Best volume reading in the last five sessions: the framework describes a
        # burst on the breakout bar, not necessarily on the day you happen to look.
        recent = float(vz.tail(5).max())
        up = float(close.pct_change().tail(5).max() or 0)
        ok3 = recent >= 1.0 and up > 0
        lights["volume_confirms"] = {
            "pass": bool(ok3),
            "detail": (f"peak volume {recent:+.1f}σ in last 5 sessions, "
                       f"best up-day {up*100:+.1f}%"),
        }
    else:
        lights["volume_confirms"] = {"pass": False, "detail": "no volume data"}

    n = sum(1 for v in lights.values() if v["pass"])
    return {"lights": lights, "count": n, "all_green": n == 3,
            "summary": f"{n}/3 lights green"}


# ---------------------------------------------------------------------------
# Technical setups from the source framework
# ---------------------------------------------------------------------------
def technical_setups(df: pd.DataFrame, tol: float = 0.02) -> dict:
    """
    Detect the four setups and the confirmation-candle protocol the framework
    specifies, using the 50-day SMA as the reference line.

      MA Bounce       price retraces to a RISING 50d SMA from above, then turns up
                      on expanding volume
      MA Breakout     large up-candle closes above a flat-or-rising 50d SMA
      MA Breakdown    large down-candle closes below the 50d SMA on heavy volume
      Reversal        green day closing above the prior red candle's open, then a
                      third day continuing higher on above-average volume

    Confirmation protocol: the framework requires a breakout be confirmed on day
    two or three rather than acted on immediately. `confirmed` reports whether the
    follow-through actually happened, which is the whole point of the rule.

    Detection is over the last 5 sessions so a setup that triggered earlier in the
    week is still visible.
    """
    if len(df) < 60:
        return {"setups": [], "note": "insufficient history"}

    c, h, l, o, v = (df["close"], df["high"], df["low"],
                     df["open"], df["volume"])
    sma = c.rolling(50, min_periods=50).mean()
    avg_v = v.rolling(50, min_periods=20).mean()
    rng = (h - l).rolling(20, min_periods=10).mean()
    found: list[dict] = []

    n = len(df)
    for k in range(max(50, n - 5), n):
        if pd.isna(sma.iloc[k]) or pd.isna(avg_v.iloc[k]) or k < 22:
            continue
        d = df.index[k].strftime("%Y-%m-%d")
        ma = float(sma.iloc[k])
        slope = (ma / float(sma.iloc[k - 21]) - 1.0) * 100.0 if pd.notna(sma.iloc[k - 21]) else 0.0
        px, pv = float(c.iloc[k]), float(v.iloc[k])
        heavy = pv > float(avg_v.iloc[k])
        big = (float(h.iloc[k]) - float(l.iloc[k])) > 1.3 * float(rng.iloc[k] or 0)
        up_day = px > float(o.iloc[k])
        prev_above = float(c.iloc[k - 1]) > (float(sma.iloc[k - 1])
                                             if pd.notna(sma.iloc[k - 1]) else np.inf)

        # --- MA Bounce ---
        touched = float(l.iloc[k]) <= ma * (1 + tol) and px > ma
        if touched and slope > 0 and up_day and heavy and prev_above:
            found.append({
                "setup": "Moving Average Bounce", "date": d, "direction": "long",
                "detail": (f"low {float(l.iloc[k]):.2f} touched rising 50d SMA "
                           f"({ma:.2f}, slope {slope:+.2f}%), closed up on "
                           f"{pv/float(avg_v.iloc[k]):.1f}x average volume"),
                "stop_hint": round(min(float(l.iloc[k]), ma) * 0.99, 2),
            })

        # --- MA Breakout ---
        if (not prev_above) and px > ma and up_day and big and slope > -0.5:
            conf = None
            if k + 1 < n:
                conf = bool(float(c.iloc[k + 1]) > float(h.iloc[k]))
            found.append({
                "setup": "Moving Average Breakout", "date": d, "direction": "long",
                "detail": (f"closed {px:.2f} above 50d SMA {ma:.2f} on a wide bar; "
                           f"volume {pv/float(avg_v.iloc[k]):.1f}x average"),
                "confirmed": conf,
                "confirmation_note": ("day-2 closed above the breakout high"
                                      if conf else
                                      "day-2 did NOT confirm — framework says wait"
                                      if conf is False else
                                      "awaiting day-2 confirmation"),
                "stop_hint": round(ma * 0.99, 2),
            })

        # --- MA Breakdown ---
        if prev_above and px < ma and (not up_day) and heavy:
            found.append({
                "setup": "Moving Average Breakdown", "date": d, "direction": "exit/short",
                "detail": (f"closed {px:.2f} below 50d SMA {ma:.2f} on "
                           f"{pv/float(avg_v.iloc[k]):.1f}x average volume"),
                "stop_hint": round(ma * 1.01, 2),
            })

        # --- Reversal confirmation (3-day) ---
        if k >= 2:
            red = float(c.iloc[k - 2]) < float(o.iloc[k - 2])
            rev = float(c.iloc[k - 1]) > float(o.iloc[k - 2])
            third = px > float(c.iloc[k - 1]) and heavy
            if red and rev and third:
                found.append({
                    "setup": "Reversal Confirmation", "date": d, "direction": "long",
                    "detail": (f"green day closed above the prior red candle's open, "
                               f"third day continued higher on "
                               f"{pv/float(avg_v.iloc[k]):.1f}x average volume"),
                    "stop_hint": round(float(l.iloc[k - 2]) * 0.99, 2),
                })

    # de-duplicate, keeping the most recent instance of each setup type
    latest: dict[str, dict] = {}
    for f in found:
        latest[f["setup"]] = f
    return {"setups": list(latest.values()), "count": len(latest)}


# ---------------------------------------------------------------------------
# Multi-SMA breakout points
# ---------------------------------------------------------------------------
SMA_BREAKOUT_PERIODS = (20, 50, 150)


def sma_breakouts(df: pd.DataFrame,
                  periods: tuple[int, ...] = SMA_BREAKOUT_PERIODS,
                  lookback: int = 120,
                  min_gap: int = 3) -> dict:
    """
    Breakout points in BOTH directions across the 20, 50 and 150-day SMAs.

    A breakout is a close crossing the average, confirmed on the next session so a
    single intraday poke does not register. Both directions are returned: an
    upward cross of the 150-day is a very different event from an upward cross of
    the 20-day, and the framework treats a downward cross as an exit trigger.

    `min_gap` suppresses whipsaw — crossings within `min_gap` sessions of the
    previous one on the same average are dropped, since price oscillating around a
    flat MA would otherwise produce a dozen meaningless "breakouts" in a fortnight.

    Each point carries the context needed to judge it:
        volume_ratio  volume vs its own 50-day average on the crossing bar
        confirmed     did the NEXT session hold beyond the average?
        sma_slope     was the average itself rising or falling?
        gap_pct       how far beyond the average the close finished

    Indices are relative to the trailing `lookback` window so the chart can plot
    them directly without re-deriving positions.
    """
    close = df["close"]
    vol = df["volume"]
    n = len(close)
    if n < 30:
        return {"periods": list(periods), "points": [], "note": "insufficient history"}

    avg_v = vol.rolling(50, min_periods=10).mean()
    start = max(0, n - lookback)
    points: list[dict] = []

    for p in periods:
        if n < p + 5:
            continue
        sma = close.rolling(p, min_periods=p).mean()
        above = close > sma
        # A crossing is a change in the above/below state.
        cross = above.ne(above.shift(1)) & above.notna() & above.shift(1).notna()
        last_idx = -10 ** 9
        for i in range(max(start, p), n):
            if not bool(cross.iloc[i]) or pd.isna(sma.iloc[i]):
                continue
            if i - last_idx < min_gap:
                continue
            last_idx = i
            up = bool(above.iloc[i])
            ma = float(sma.iloc[i])
            px = float(close.iloc[i])
            slope = ((ma / float(sma.iloc[i - 21]) - 1.0) * 100.0
                     if i >= 21 and pd.notna(sma.iloc[i - 21]) and sma.iloc[i - 21] else 0.0)
            av = float(avg_v.iloc[i]) if pd.notna(avg_v.iloc[i]) and avg_v.iloc[i] else 0.0
            vr = (float(vol.iloc[i]) / av) if av > 0 else None
            # Confirmation uses the NEXT session, which exists only if i < n-1.
            conf = None
            if i + 1 < n and pd.notna(sma.iloc[i + 1]):
                conf = bool((close.iloc[i + 1] > sma.iloc[i + 1]) == up)
            points.append({
                "sma": p,
                "date": close.index[i].strftime("%Y-%m-%d"),
                "idx": i - start,               # position within the chart window
                "direction": "up" if up else "down",
                "price": round(px, 2),
                "sma_value": round(ma, 2),
                "gap_pct": round((px / ma - 1.0) * 100.0, 2),
                "sma_slope_21d": round(slope, 2),
                "volume_ratio": round(vr, 2) if vr else None,
                "volume_confirmed": bool(vr is not None and vr >= 1.2),
                "confirmed": conf,
            })

    points.sort(key=lambda x: (x["date"], x["sma"]))
    recent = [p for p in points if p["idx"] >= max(0, (n - start) - 21)]
    return {
        "periods": list(periods),
        "points": points,
        "count": len(points),
        "recent_21d": len(recent),
        "latest": points[-1] if points else None,
        "window": lookback,
    }


# ---------------------------------------------------------------------------
# Weinstein stage analysis
# ---------------------------------------------------------------------------
def weinstein_stage(df: pd.DataFrame) -> tuple[int, str, dict]:
    """
    Weinstein stage classification using BOTH price structure and volume regime.

    The earlier version used only close and the 50-day SMA, which implemented half
    the framework's definition. The source document defines the stages partly by
    volume behaviour:

        Stage 1  sideways price, "volume typically CONTRACTS as institutional
                 accumulation occurs quietly"
        Stage 2  breakout "accompanied by EXPANDING volume"
        Stage 3  "volume remains HIGH but fails to produce further upward progress"
        Stage 4  breakdown, capital abandonment

    Volume is what actually discriminates Stage 1 from Stage 3 — both are sideways
    price around a flattening MA, and the difference is quiet contraction versus
    heavy churn. The old code guessed between them using prior price direction,
    which gets it wrong whenever a range follows a decline that itself followed an
    advance.

    Returns (stage, label, evidence) so the reasoning is inspectable rather than
    a bare number.
    """
    close = df["close"]
    if len(close) < 80:
        return 0, "Insufficient history", {}
    f = _stage_features(df)
    if f is None:
        return 0, "Insufficient history", {}
    stage, label, ev = _stage_decide(**{k: f[k].iloc[-1] for k in _STAGE_FEATURES})
    return stage, label, ev


# Feature names shared by the scalar and historical paths. Keeping them in one
# tuple is what stops the two from drifting apart.
_STAGE_FEATURES = ("above", "slope", "crossings", "v_now", "n_dist", "n_acc")


def _stage_features(df: pd.DataFrame) -> pd.DataFrame | None:
    """
    Every input the stage decision needs, as aligned series.

    Computing these once as series lets `weinstein_stage` read the last row and
    `stage_history` read every row, so the displayed stage and the "held for N
    sessions" figure beside it can never come from different logic.
    """
    close = df["close"]
    if len(close) < 80:
        return None
    sma = close.rolling(50, min_periods=50).mean()
    # shift(21) is 21 sessions back, matching the scalar version's iloc[-22].
    # green_light_test and technical_setups both use 21, and this field gates
    # every +/-0.75% stage boundary, so an off-by-one here moves classifications.
    slope = (sma / sma.shift(21) - 1.0) * 100.0
    # The scalar version took the last 60 ROWS and diffed inside them, which is
    # 59 transitions, not 60. Using rolling(60) here would silently add one and
    # shift classifications at the `crossings >= 6` boundary.
    crossings = ((np.sign(close - sma).diff().abs() > 0)
                 .rolling(59, min_periods=59).sum())
    out = pd.DataFrame({
        "above": (close > sma).astype(float),
        "slope": slope.fillna(0.0),
        "crossings": crossings,
        "v_now": volume_trend(df).reindex(close.index).fillna(0.0),
        "n_dist": distribution_days(df).reindex(close.index),
        "n_acc": accumulation_days(df).reindex(close.index),
    })
    return out


def _stage_decide(above, slope, crossings, v_now, n_dist, n_acc
                  ) -> tuple[int, str, dict]:
    """
    The stage decision itself, on plain scalars.

    Extracted so that the current stage and the stage history are produced by
    exactly the same rules. The logic below is unchanged from when it lived
    inline in `weinstein_stage`.
    """
    above = bool(above)
    slope = float(slope) if pd.notna(slope) else 0.0
    crossings = int(crossings) if pd.notna(crossings) else 0
    v_now = float(v_now) if pd.notna(v_now) else 0.0
    n_dist = int(n_dist) if pd.notna(n_dist) else 0
    n_acc = int(n_acc) if pd.notna(n_acc) else 0

    expanding = v_now > 15.0
    contracting = v_now < -10.0

    ev = {
        "price_above_sma50": bool(above),
        "sma50_slope_21d_pct": round(slope, 2),
        "sma50_crossings_60d": crossings,
        "volume_trend_pct": round(v_now, 1),
        "volume_regime": ("expanding" if expanding else
                          "contracting" if contracting else "flat"),
        "distribution_days_25d": n_dist,
        "accumulation_days_25d": n_acc,
    }

    ranging = crossings >= 6 and abs(slope) <= 0.75

    # ---- Stage 4: breakdown ------------------------------------------------
    if not above and slope < -0.75:
        ev["reason"] = (f"price below a declining 50d SMA (slope {slope:+.2f}%)"
                        + (f", volume expanding {v_now:.0f}% — active liquidation"
                           if expanding else
                           f", volume contracting {v_now:.0f}% — sellers thinning"))
        return 4, "Stage 4 - Capitulation / decline", ev

    # ---- Stage 2: markup, requires participation ---------------------------
    if above and slope > 0.75:
        if not contracting:
            ev["reason"] = (f"price above a rising 50d SMA (slope {slope:+.2f}%) "
                            f"with volume {ev['volume_regime']} ({v_now:+.0f}%), "
                            f"{n_acc} accumulation days in 25")
            return 2, "Stage 2 - Structural uptrend", ev
        # Rising price on contracting volume is the framework's weak markup: the
        # advance is real but unparticipated, which is the Stage 3 precursor.
        ev["reason"] = (f"price rising but volume contracting {v_now:.0f}% — "
                        f"advance is unparticipated, {n_dist} distribution days in 25")
        return 3, "Stage 3 - Distribution (unparticipated advance)", ev

    # ---- Stage 1 vs Stage 3: volume is the discriminator -------------------
    if ranging:
        if contracting:
            ev["reason"] = (f"sideways around a flat 50d SMA with volume contracting "
                            f"{v_now:.0f}% — quiet accumulation, {n_acc} accumulation "
                            f"vs {n_dist} distribution days")
            return 1, "Stage 1 - Consolidation / accumulation", ev
        if expanding or n_dist >= 5:
            ev["reason"] = (f"sideways around a flat 50d SMA but volume "
                            f"{ev['volume_regime']} ({v_now:+.0f}%) with {n_dist} "
                            f"distribution days in 25 — heavy churn without progress")
            return 3, "Stage 3 - Distribution", ev
        # Flat volume, flat price: fall back to which side of the tape is heavier.
        if n_dist > n_acc:
            ev["reason"] = (f"sideways, flat volume, but {n_dist} distribution vs "
                            f"{n_acc} accumulation days")
            return 3, "Stage 3 - Distribution", ev
        ev["reason"] = (f"sideways, flat volume, {n_acc} accumulation vs "
                        f"{n_dist} distribution days")
        return 1, "Stage 1 - Consolidation / accumulation", ev

    # ---- shallow trends ----------------------------------------------------
    # The day counts have to be consulted here too. Falling back to "Stage 1 -
    # accumulation" purely because price sits below a flat SMA labelled sectors
    # with 9-10 distribution days in 25 sessions as quiet accumulation, which is
    # the opposite of what was happening. Stage 1 requires selling to have
    # EXHAUSTED; heavy distribution means it has not.
    heavy_dist = n_dist >= 6 and n_dist > n_acc

    # A steeply-sloping SMA must never reach the "shallow trend" fallthrough.
    # Audit found Stage 2 "Structural uptrend" returned on a 50d SMA declining
    # -13.7% (87% of 139 fires had a DECLINING SMA), and Stage 1 "accumulation"
    # returned on an SMA rising +14.9%. The Stage 2 gate above requires
    # slope > 0.75, so the fallthrough contradicted the function's own definition.
    if not above and slope < -0.25:
        ev["reason"] = (f"price below a declining 50d SMA ({slope:+.2f}%), "
                        f"volume {ev['volume_regime']}, {n_dist} distribution days")
        return 4, "Stage 4 - Capitulation / decline", ev
    if above and slope < -0.25:
        ev["reason"] = (f"price above the 50d SMA but that SMA is declining "
                        f"({slope:+.2f}%) — rally into a falling trend")
        return 3, "Stage 3 - Distribution", ev
    if not above and slope > 0.25:
        # Price under a RISING MA is a pullback inside an uptrend, not a base.
        # Audit found this returning Stage 1 "accumulation / selling exhausted" on
        # SMAs rising up to +14.9%. Heavy distribution makes it Stage 3; otherwise
        # the trend is intact and it stays Stage 2.
        if heavy_dist:
            ev["reason"] = (f"pullback below a rising 50d SMA ({slope:+.2f}%) with "
                            f"{n_dist} distribution vs {n_acc} accumulation days — "
                            f"being sold into")
            return 3, "Stage 3 - Distribution", ev
        ev["reason"] = (f"pullback below a rising 50d SMA ({slope:+.2f}%), "
                        f"{n_acc} accumulation vs {n_dist} distribution days — "
                        f"uptrend intact")
        return 2, "Stage 2 - Structural uptrend (pullback)", ev

    if above:
        if heavy_dist:
            ev["reason"] = (f"price above the 50d SMA but {n_dist} distribution vs "
                            f"{n_acc} accumulation days — supply is being fed into "
                            f"the advance")
            return 3, "Stage 3 - Distribution", ev
        ev["reason"] = (f"price above the 50d SMA on a shallow slope "
                        f"({slope:+.2f}%), volume {ev['volume_regime']}, "
                        f"{n_acc} accumulation vs {n_dist} distribution days")
        return 2, "Stage 2 - Structural uptrend", ev

    if heavy_dist:
        # Stage 4 requires a DOWNWARD-sloping 50d MA per the framework. Price below
        # a still-rising MA with heavy distribution is a pullback being sold into —
        # Stage 3, not Stage 4. Returning Stage 4 on slope +4.5% (as an earlier
        # version did for XLK) contradicts the source definition.
        if slope > 0.0:
            ev["reason"] = (f"price below the 50d SMA but that SMA is still rising "
                            f"({slope:+.2f}%), with {n_dist} distribution vs {n_acc} "
                            f"accumulation days — pullback being sold into")
            return 3, "Stage 3 - Distribution", ev
        ev["reason"] = (f"price below a flat-to-declining 50d SMA ({slope:+.2f}%) "
                        f"with {n_dist} distribution vs {n_acc} accumulation days — "
                        f"selling still active, not exhausted")
        return 4, "Stage 4 - Capitulation / decline", ev
    ev["reason"] = (f"price below the 50d SMA on a shallow slope ({slope:+.2f}%), "
                    f"volume {ev['volume_regime']}, selling appears exhausted "
                    f"({n_dist} distribution vs {n_acc} accumulation days)")
    return 1, "Stage 1 - Consolidation / accumulation", ev


def stage_history(df: pd.DataFrame, n: int = 260) -> pd.Series:
    """
    Stage classification at each of the last `n` sessions.

    Uses `_stage_decide`, the same function that produces the current stage, so
    the "held for N sessions" figure is derived from the identical rules rather
    than an approximation that could disagree with the label beside it.
    """
    f = _stage_features(df)
    if f is None or f.empty:
        return pd.Series(dtype=float)
    f = f.tail(n).dropna(subset=["crossings"])
    if f.empty:
        return pd.Series(dtype=float)
    vals = [_stage_decide(*row)[0] for row in
            f[list(_STAGE_FEATURES)].itertuples(index=False, name=None)]
    return pd.Series(vals, index=f.index, dtype=float)


def state_duration(states: pd.Series) -> dict:
    """
    How long a categorical series has held its current value, and what preceded it.

    This is what "trend" means for a categorical field. A delta between Stage 3
    and Stage 2 is not 1 — it is a transition, and the useful facts are how long
    the current state has persisted, what it replaced, and when. A sector that
    entered Stage 2 yesterday and one that has held it for eight months are in
    very different situations while showing the identical label.
    """
    s = states.dropna()
    if s.empty:
        return {"days": None, "prev": None, "since": None}
    cur = s.iloc[-1]
    days = 0
    for v in reversed(s.tolist()):
        if v == cur:
            days += 1
        else:
            break
    prev, since = None, None
    if days < len(s):
        prev = s.iloc[-days - 1]
        idx = s.index[-days]
        since = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
    return {"days": int(days),
            "prev": (int(prev) if isinstance(prev, (int, float))
                     and float(prev).is_integer() else prev),
            "since": since}


# ---------------------------------------------------------------------------
# Full per-sector metric bundle
# ---------------------------------------------------------------------------
def compute_sector_metrics(ticker: str,
                           sector_df: pd.DataFrame,
                           bench_df: pd.DataFrame,
                           constituent_closes: dict[str, pd.Series],
                           off_exchange: dict | None = None,
                           si: pd.DataFrame | None = None) -> dict:
    """Everything the scoring engine and dashboard need for one sector."""
    meta = config.SECTORS.get(ticker, {})
    close = sector_df["close"]

    rs = dorsey_rs(close, bench_df["close"])
    mrs = mansfield_rs(rs)
    ratio_raw, mom_raw = rrg_raw(rs)
    breadth = breadth_above_sma(constituent_closes)
    # Constituent calendars union in the DataFrame, so the breadth index can end
    # AFTER the sector's own last bar; _last() would then return a future reading.
    if len(breadth):
        breadth = breadth[breadth.index <= close.index[-1]]
    cmf = chaikin_money_flow(sector_df)
    volz = volume_zscore(sector_df)
    dvolz = dollar_volume_zscore(sector_df)
    adb = ad_day_balance(sector_df)
    absorp = absorption_score(sector_df)
    blocki = block_intensity(sector_df)
    blocki_z = _rolling_z(blocki, 120)
    obv = obv_slope(sector_df)
    stage, stage_label, stage_evidence = weinstein_stage(sector_df)
    stage_hist = stage_history(sector_df)
    stage_dur = state_duration(stage_hist)
    vtrend = volume_trend(sector_df)
    n_dist = distribution_days(sector_df)
    n_acc = accumulation_days(sector_df)
    vpd = volume_price_divergence(sector_df)
    vprof = volume_regime_profile(sector_df)
    brk = sma_breakouts(sector_df)
    glt = green_light_test(sector_df, mrs)
    setups = technical_setups(sector_df)
    adv_dollar = float((sector_df["volume"] * sector_df["close"])
                       .tail(50).mean()) if len(sector_df) >= 50 else None
    from .flow import classify_blocks
    blocks = classify_blocks(off_exchange, price=float(close.iloc[-1]),
                             adv_dollar=adv_dollar)

    # Short interest / crowded shorts. `as_of` keeps this point-in-time safe so the
    # same code path is valid inside the walk-forward backtest.
    si_m = short_interest_metrics(si, as_of=close.index[-1])
    div = divergence_screen(close, bench_df["close"])
    squeeze = short_squeeze_setup(si_m, div, mrs)

    sma50 = close.rolling(50, min_periods=50).mean()
    sma200 = close.rolling(200, min_periods=200).mean()

    def _ret(n: int) -> float:
        if len(close) <= n:
            return float("nan")
        return (float(close.iloc[-1]) / float(close.iloc[-1 - n]) - 1.0) * 100.0

    return {
        "ticker": ticker,
        "name": meta.get("name", ticker),
        "tier": meta.get("tier", 2),
        "group": meta.get("group", ""),
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
        "price": round(float(close.iloc[-1]), 2),

        # relative strength
        "dorsey_rs": round(_last(rs), 2),
        "mansfield_rs": round(_last(mrs), 3),
        "mansfield_rs_prev": round(_last(mrs.iloc[:-21]) if len(mrs) > 21 else float("nan"), 3),
        "mansfield_cross_up": bool(len(mrs.dropna()) > 22
                                   and _last(mrs) > 0
                                   and float(mrs.dropna().iloc[-22]) <= 0),
        # RRG coordinates are filled in by finalise_rrg() once the whole tier is
        # available, because normalisation is cross-sectional.
        "rs_ratio": None,
        "rs_momentum": None,
        "quadrant": "Unknown",

        # breadth
        "breadth": round(_last(breadth), 1),
        "breadth_chg_21d": round(_last(breadth) - (_last(breadth.iloc[:-21])
                                                  if len(breadth) > 21 else float("nan")), 1),
        "n_constituents": len(constituent_closes),

        # money flow / institutional footprint
        "cmf": round(_last(cmf), 4),
        "volume_z": round(_last(volz), 2),
        "dollar_volume_z": round(_last(dvolz), 2),
        "ad_balance": round(_last(adb), 3),
        "absorption": round(_last(absorp), 3),
        "block_intensity": round(_last(blocki), 4),
        "block_intensity_z": round(_last(blocki_z), 2),
        "obv_slope": round(_last(obv), 4),

        # Observed tick-level flow (None unless Polygon tick data is enabled).
        "flow": off_exchange,
        "off_exchange_share": (off_exchange or {}).get("off_exchange_share"),
        "off_exchange_trend": (off_exchange or {}).get("off_exchange_trend"),
        "dark_pool_share": (off_exchange or {}).get("dark_pool_share"),
        "dark_pool_trend": (off_exchange or {}).get("dark_pool_trend"),
        "block_share": (off_exchange or {}).get("block_share"),
        "block_count": (off_exchange or {}).get("block_count_latest"),
        "block_direction": (off_exchange or {}).get("block_direction"),
        "largest_print_notional": (off_exchange or {}).get("largest_print_notional"),

        # Volume regime — expanding vs contracting (framework's Stage 1/2 language)
        "volume_trend_pct": round(_last(vtrend), 1),
        "volume_regime": vprof.get("volume_regime"),
        # STRENGTH: unusual for this sector, or routine?
        "volume_strength_z": vprof.get("strength_z"),
        "volume_strength_pct": vprof.get("strength_pct"),
        "volume_strength_label": vprof.get("strength_label"),
        # LENGTH: how long has the regime held?
        "volume_regime_days": vprof.get("regime_days"),
        "volume_regime_sustained": vprof.get("regime_sustained"),
        "volume_trend_slope_21d": vprof.get("trend_slope_21d"),
        "volume_trend_direction": vprof.get("trend_direction"),
        "volume_regime_note": vprof.get("note"),
        "distribution_days_25d": (int(_last(n_dist))
                                  if np.isfinite(_last(n_dist)) else None),
        "accumulation_days_25d": (int(_last(n_acc))
                                  if np.isfinite(_last(n_acc)) else None),

        # Volume-price divergence — the depletion signature
        "vp_divergence": vpd.get("divergence"),
        "vp_divergence_note": vpd.get("note"),
        "depletion_warning": bool(vpd.get("depletion_warning")),
        "vp_price_change_pct": vpd.get("price_change_pct"),

        # Multi-SMA breakout points (20 / 50 / 150), both directions
        "breakouts": brk.get("points", []),
        "breakout_count": brk.get("count", 0),
        "breakouts_recent_21d": brk.get("recent_21d", 0),
        "latest_breakout": brk.get("latest"),
        "breakout_periods": brk.get("periods", []),

        # structure. Duration is the categorical equivalent of a trend: a sector
        # that entered Stage 2 yesterday and one that has held it for months
        # display the identical label but are in very different situations.
        "stage": stage,
        "stage_label": stage_label,
        "stage_evidence": stage_evidence,
        "stage_days": stage_dur["days"],
        "stage_prev": stage_dur["prev"],
        "stage_since": stage_dur["since"],

        # Green Light Test (source framework's 3-part entry gate)
        "green_lights": glt["count"],
        "all_green": glt["all_green"],
        "green_light_detail": glt["lights"],

        # Technical setups + confirmation protocol
        "setups": setups.get("setups", []),
        "setup_count": setups.get("count", 0),

        # Short interest / crowded shorts (free on the standard Polygon key)
        "days_to_cover": si_m["days_to_cover"],
        "dtc_percentile": si_m["dtc_percentile"],
        "short_interest": si_m["short_interest"],
        "si_change_pct": si_m["si_change_pct"],
        "crowded_short": si_m["crowded_short"],
        "si_as_of": si_m["si_as_of"],

        # Index-vs-sector divergence (source document's worked example)
        "divergence": div["divergence"],
        "divergence_gap_pct": div.get("gap_pct"),
        "divergence_note": div.get("divergence_note"),
        "sector_63d_pct": div.get("sector_return_pct"),
        "bench_63d_pct": div.get("benchmark_return_pct"),

        # Short-squeeze setup (crowded + diverged + RS turning up)
        "squeeze_score": squeeze["squeeze_score"],
        "squeeze_setup": squeeze["squeeze_setup"],
        "squeeze_conditions": squeeze["squeeze_conditions"],
        "squeeze_note": squeeze["squeeze_note"],

        # Dark-pool block classification buckets
        "block_buckets": blocks.get("bucket_names", []),
        "block_bucket_detail": blocks.get("buckets", []),
        "block_summary": blocks.get("summary"),
        "block_surprise": blocks.get("block_surprise"),
        "adv_dollar": round(adv_dollar / 1e6, 1) if adv_dollar else None,
        "above_sma50": bool(pd.notna(sma50.iloc[-1]) and close.iloc[-1] > sma50.iloc[-1]),
        "above_sma200": bool(pd.notna(sma200.iloc[-1]) and close.iloc[-1] > sma200.iloc[-1]),

        # returns
        "ret_5d": round(_ret(5), 2),
        "ret_21d": round(_ret(21), 2),
        "ret_63d": round(_ret(63), 2),
        "mom_12_1": round(momentum_12_1(close), 2),
        # VMS is filled in by finalise_rrg(), since it needs cross-sectional
        # z-scores over the peer group.
        "vms": None,
        "vms_rank": None,

        # series for charts
        "series": {
            # `dates` MUST align with `price`, which is the x-axis reference for
            # the drill-down chart. It previously carried only RRG_TAIL (12)
            # entries while the metric series carried 120, so the chart labelled
            # its left edge with the date from 12 sessions ago — an error of ~108
            # sessions. The RRG tail now has its own array.
            "dates": _tail_dates(close, config.SPARK_LEN),
            "rrg_dates": _tail_dates(close, config.RRG_TAIL),
            "rrg_x": [],          # filled by finalise_rrg()
            "rrg_y": [],
            "mansfield": _spark(mrs),
            "breadth": _spark(breadth),
            "cmf": _spark(cmf),
            # price and its SMAs feed the full-size chart, where decimation
            # would be visible — they keep every session. 2dp because these are
            # dollar prices and further decimals render nowhere.
            "price": _tail(close, config.SPARK_LEN, dp=2),
            "absorption": _spark(absorp),
            "volume_trend": _spark(vtrend, dp=1),
            "volume": _spark(sector_df["volume"], dp=0),
            "ad_balance": _spark(adb),
            "stage": _spark(stage_hist, points=config.SPARK_POINTS),
            "sma20": _tail(close.rolling(20, min_periods=20).mean()
                           .reindex(close.index), config.SPARK_LEN, dp=2),
            "sma50": _tail(close.rolling(50, min_periods=50).mean()
                           .reindex(close.index), config.SPARK_LEN, dp=2),
            "sma150": _tail(close.rolling(150, min_periods=150).mean()
                            .reindex(close.index), config.SPARK_LEN, dp=2),
        },
        "_raw": {          # kept in-process for the scoring engine, stripped before export
            "mansfield": mrs, "breadth": breadth, "cmf": cmf,
            "absorption": absorp, "ad_balance": adb, "block_z": blocki_z,
            "ratio_raw": ratio_raw, "mom_raw": mom_raw,
            "mom_series": momentum_12_1_series(close),
            "rs_mom": pd.Series(dtype=float),   # replaced by finalise_rrg()
        },
    }


def finalise_rrg(sectors: list[dict]) -> list[dict]:
    """
    Fill in cross-sectionally normalised RRG coordinates, per tier.

    Must run after every sector's raw metrics exist, since the x/y coordinates
    of any one sector depend on where its peers sit that day.
    """
    for tier in (1, 2):
        grp = [s for s in sectors if s.get("tier") == tier]
        if not grp:
            continue
        x_norm = normalise_rrg({s["ticker"]: s["_raw"]["ratio_raw"] for s in grp})
        y_norm = normalise_rrg({s["ticker"]: s["_raw"]["mom_raw"] for s in grp})

        for s in grp:
            xs = x_norm.get(s["ticker"], pd.Series(dtype=float))
            ys = y_norm.get(s["ticker"], pd.Series(dtype=float))
            s["rs_ratio"] = round(_last(xs), 2) if len(xs.dropna()) else None
            s["rs_momentum"] = round(_last(ys), 2) if len(ys.dropna()) else None
            s["quadrant"] = rrg_quadrant(_last(xs), _last(ys))
            s["series"]["rrg_x"] = _tail(xs, config.RRG_TAIL)
            s["series"]["rrg_y"] = _tail(ys, config.RRG_TAIL)
            s["_raw"]["rs_mom"] = ys

            # Longer coordinate history than the 12-point RRG tail, for the
            # sparklines and for measuring how long the sector has sat in one
            # quadrant. Rotation is the whole premise of the dashboard, so "in
            # Leading for 4 sessions" versus "for 90" is the actual signal.
            s["series"]["rs_ratio"] = _spark(xs, dp=2)
            s["series"]["rs_momentum"] = _spark(ys, dp=2)
            aligned = pd.concat([xs, ys], axis=1, join="inner").dropna()
            if len(aligned):
                quads = pd.Series(
                    [rrg_quadrant(a, b) for a, b in aligned.itertuples(index=False,
                                                                      name=None)],
                    index=aligned.index)
                qd = state_duration(quads)
            else:
                qd = {"days": None, "prev": None, "since": None}
            s["quadrant_days"] = qd["days"]
            s["quadrant_prev"] = qd["prev"]
            s["quadrant_since"] = qd["since"]

        # ---- Validated Momentum Score, cross-sectional within tier ----------
        # Both inputs are z-scored across the peer group at this date, matching
        # how the model was validated. Combining raw values would let 12-1
        # momentum (percent) swamp RS-Momentum (centred on 100).
        def _z(vals: list[float]) -> list[float]:
            a = np.array([v if v is not None and np.isfinite(v) else np.nan
                          for v in vals], dtype=float)
            mu, sd = np.nanmean(a), np.nanstd(a)
            if not np.isfinite(sd) or sd == 0:
                return [0.0 if np.isfinite(x) else np.nan for x in a]
            return list((a - mu) / sd)

        zm = _z([s.get("mom_12_1") for s in grp])
        zr = _z([s.get("rs_momentum") for s in grp])
        w = config.VMS_WEIGHTS
        for s, a, b in zip(grp, zm, zr):
            parts, wts = [], []
            if np.isfinite(a):
                parts.append(a * w["mom_12_1"]); wts.append(w["mom_12_1"])
            if np.isfinite(b):
                parts.append(b * w["rs_momentum"]); wts.append(w["rs_momentum"])
            s["vms"] = round(sum(parts) / sum(wts), 3) if wts else None
            s["vms_components"] = {
                "mom_12_1_z": round(a, 3) if np.isfinite(a) else None,
                "rs_momentum_z": round(b, 3) if np.isfinite(b) else None,
            }
        ranked = sorted([s for s in grp if s["vms"] is not None],
                        key=lambda s: -s["vms"])
        for i, s in enumerate(ranked, 1):
            s["vms_rank"] = i
            s["vms_tier_size"] = len(ranked)

        _vms_history(grp)
    return sectors


def _vms_history(grp: list[dict]) -> None:
    """
    VMS at each historical date, for the sparkline.

    VMS is cross-sectional: a sector's score depends on where its peers sat on
    that same day. So this cannot be computed one sector at a time — the whole
    tier has to be assembled into a date x ticker matrix and z-scored across
    each row. Doing it per-sector against its own history would answer a
    different question entirely, and would drift from the headline number.
    """
    mom, rsm = {}, {}
    for s in grp:
        m = s["_raw"].get("mom_series")
        r = s["_raw"].get("rs_mom")
        if m is not None and len(m.dropna()):
            mom[s["ticker"]] = m
        if r is not None and len(r.dropna()):
            rsm[s["ticker"]] = r
    if len(mom) < 3 or len(rsm) < 3:
        return

    M = pd.DataFrame(mom).dropna(how="all")
    R = pd.DataFrame(rsm).dropna(how="all")
    idx = M.index.intersection(R.index)
    if len(idx) < 30:
        return
    M, R = M.loc[idx], R.loc[idx]

    # Row-wise (per-date) z-score across the peer group, matching _z above.
    def zrows(df: pd.DataFrame) -> pd.DataFrame:
        mu = df.mean(axis=1)
        sd = df.std(axis=1, ddof=0).replace(0, np.nan)
        return df.sub(mu, axis=0).div(sd, axis=0)

    w = config.VMS_WEIGHTS
    Z = (zrows(M) * w["mom_12_1"] + zrows(R) * w["rs_momentum"]) \
        / (w["mom_12_1"] + w["rs_momentum"])
    for s in grp:
        col = s["ticker"]
        if col in Z.columns:
            s["series"]["vms"] = _spark(Z[col])
