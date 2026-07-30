"""
Smart Money / Sector Transition Dashboard - configuration.

Universe, metric parameters, composite weights and alert thresholds.
Edit this file to tune the system; nothing else needs to change.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# API keys.  Set as environment variables, or drop them in a file named
# `.env` next to run.py as  FMP_API_KEY=xxxx  /  POLYGON_API_KEY=xxxx
# ---------------------------------------------------------------------------
def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "").strip()
# Optional. With a key, macro.py uses FRED's JSON API (reliable, documented rate
# limits). Without it, it falls back to the keyless fredgraph CSV export, which
# is the human download feature and is slow/flaky for automated polling — six
# 30s timeouts once wedged a Pi rebuild at 200s. Free key: fred.stlouisfed.org.
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
# Per-series network timeout. Deliberately short: a blocked FRED endpoint should
# fail fast and fall back to cache, not stall the whole run for minutes.
FRED_TIMEOUT_SEC = float(os.environ.get("FRED_TIMEOUT_SEC", "8"))

# Provider preference order. First one with a usable key wins; "yahoo" needs no key.
#
# Polygon is preferred over FMP for price history because it adjusts volume for
# splits (Yahoo does not, reliably) and retains delisted tickers. Both matter for
# backtesting: four metrics are volume-based, and dropped constituents are the
# main source of survivorship bias in the breadth calculation.
PROVIDER_ORDER = ["polygon", "fmp", "yahoo"]

# Refuse to reuse cached bars that came from a different provider than the one
# now preferred. Without this, adding a Polygon key would silently keep serving
# previously-cached Yahoo bars, and you would believe you were computing on
# Polygon data. Set False only to save bandwidth when provenance doesn't matter.
STRICT_CACHE_PROVENANCE = True

# Hard requirement on data source. When set, the providers module will NOT fall
# back to anything else — a missing key or a failed request becomes an error
# rather than a silent downgrade to free data.
#
# This exists because a silent fallback is the most dangerous failure mode in the
# whole system: the numbers still render, the dashboard still looks authoritative,
# and nothing tells you the foundation changed underneath.
REQUIRE_PROVIDER: str | None = "polygon"

# When True, a missing metric DISQUALIFIES a phase rule instead of being skipped.
# The live dashboard wants False (degrade gracefully rather than go silent); the
# backtest sets this True so it measures the same rules the product ships.
STRICT_RULES = False

# Short-interest publication lag, calendar days. Exchanges publish roughly 8
# business days after the settlement date, so treating settlement_date as the
# availability date would use data that was not yet public — lookahead bias that
# would flatter any backtest of a short-interest signal. 10 calendar days is a
# conservative approximation of 8 business days.
#
# CORRECTED: 10 calendar days spanning a weekend is only 6 BUSINESS days, so the
# old value was anti-conservative — it exposed data 1-2 business days before
# publication, in the exact code path whose purpose is to prevent lookahead.
# 14 calendar days guarantees >= 8 business days regardless of weekend placement.
SI_PUBLICATION_LAG_DAYS = 14

# ---------------------------------------------------------------------------
# Real order flow via Polygon tick data (/v3/trades).
#
# Turn this on once POLYGON_API_KEY is set and your plan includes trade-level
# data. It replaces the daily-bar proxies with observed off-exchange volume
# share, dark pool (ATS) share, and actual block prints with direction.
#
# Cost: each ticker-day is fetched once and cached forever as a small JSON
# summary, so only new sessions cost anything. First run on 11 sector ETFs
# across 20 sessions is roughly 220 ticker-days.
# ---------------------------------------------------------------------------
ENABLE_POLYGON_OFF_EXCHANGE = True
OFF_EXCHANGE_LOOKBACK_DAYS = 20

# Which ETFs get tick-level treatment. Broad sector ETFs are the sweet spot:
# liquid enough to be meaningful, not so heavily traded that pagination explodes
# (SPY alone can print several million trades a session).
OFF_EXCHANGE_TICKERS = ["XLK", "XLE", "XLV", "XLF", "XLI", "XLU",
                        "XLY", "XLP", "XLB", "XLRE", "XLC"]

# A print qualifying as a block. Either condition is sufficient, so genuinely
# large share counts in cheap names and large notional in expensive names both
# register.
BLOCK_MIN_SHARES = 10_000
BLOCK_MIN_NOTIONAL = 200_000

# Pagination guard. 50k trades per page, so 40 pages = 2M trades per session.
# If a ticker regularly truncates, raise this or narrow OFF_EXCHANGE_TICKERS.
FLOW_MAX_PAGES = 40
FLOW_PAUSE = 0.05

# Weights inside the observed-flow score (used instead of INST_FLOW_WEIGHTS
# whenever real tick data is available for a sector).
REAL_FLOW_WEIGHTS = {
    "dark_pool_trend": 0.35,      # ATS share direction — the cleanest read
    "off_exchange_trend": 0.25,   # all off-exchange reporting
    "block_direction": 0.25,      # blocks on upticks vs downticks
    "block_trend": 0.15,          # growing share of volume in outsized clips
}

# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------
BENCHMARK = "SPY"

# ---------------------------------------------------------------------------
# Universe: 11 GICS sector ETFs (tier 1) + industry / thematic sub-groups (tier 2)
#
# `group` ties an industry ETF back to its parent sector so the dashboard can
# cascade a sector signal down into the sub-industry that is actually driving it.
# `constituents` is a static fallback used for breadth when no holdings API is
# available. Keep 10-25 liquid names per ETF; breadth is a proportion, so a
# representative sample is sufficient and far cheaper than full holdings.
# ---------------------------------------------------------------------------
SECTORS: dict[str, dict] = {
    # ----------------------------- TIER 1: GICS sectors ---------------------
    "XLK": {
        "name": "Technology",
        "tier": 1,
        "group": "Technology",
        "constituents": ["NVDA", "MSFT", "AAPL", "AVGO", "ORCL", "CRM", "AMD", "ADBE",
                         "CSCO", "ACN", "TXN", "QCOM", "INTU", "IBM", "NOW", "AMAT",
                         "MU", "LRCX", "KLAC", "PANW"],
    },
    "XLE": {
        "name": "Energy",
        "tier": 1,
        "group": "Energy",
        "constituents": ["XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "WMB",
                         "OKE", "VLO", "BKR", "KMI", "FANG", "DVN", "HAL",
                         "OXY", "TRGP", "APA", "EQT", "LNG"],
    },
    "XLV": {
        "name": "Health Care",
        "tier": 1,
        "group": "Health Care",
        "constituents": ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "DHR",
                         "AMGN", "PFE", "ISRG", "BSX", "SYK", "MDT", "GILD", "VRTX",
                         "CI", "ELV", "REGN", "ZTS"],
    },
    "XLF": {
        "name": "Financials",
        "tier": 1,
        "group": "Financials",
        "constituents": ["BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "SPGI",
                         "AXP", "MS", "BLK", "C", "SCHW", "PGR", "CB", "MMC",
                         "ICE", "CME", "PNC", "USB"],
    },
    "XLY": {
        "name": "Consumer Discretionary",
        "tier": 1,
        "group": "Consumer Discretionary",
        "constituents": ["AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "TJX", "SBUX",
                         "NKE", "ABNB", "CMG", "ORLY", "MAR", "GM", "F", "HLT",
                         "AZO", "ROST", "DHI", "LEN"],
    },
    "XLP": {
        "name": "Consumer Staples",
        "tier": 1,
        "group": "Consumer Staples",
        "constituents": ["PG", "COST", "WMT", "KO", "PEP", "PM", "MO", "MDLZ",
                         "CL", "TGT", "KMB", "GIS", "SYY", "STZ", "KVUE", "KR",
                         "HSY", "KHC", "DG", "CHD"],
    },
    "XLI": {
        "name": "Industrials",
        "tier": 1,
        "group": "Industrials",
        "constituents": ["GE", "CAT", "RTX", "UBER", "HON", "UNP", "BA", "ETN",
                         "DE", "LMT", "ADP", "UPS", "PH", "TT", "GD", "EMR",
                         "NOC", "CSX", "ITW", "WM"],
    },
    "XLB": {
        "name": "Materials",
        "tier": 1,
        "group": "Materials",
        "constituents": ["LIN", "SHW", "APD", "ECL", "FCX", "NEM", "DOW", "NUE",
                         "CTVA", "DD", "PPG", "VMC", "MLM", "IFF", "LYB", "STLD",
                         "ALB", "CF", "PKG", "IP"],
    },
    "XLU": {
        "name": "Utilities",
        "tier": 1,
        "group": "Utilities",
        "constituents": ["NEE", "SO", "DUK", "CEG", "AEP", "SRE", "D", "PCG",
                         "EXC", "XEL", "ED", "PEG", "VST", "WEC", "AWK", "EIX",
                         "DTE", "ETR", "AEE", "PPL"],
    },
    "XLRE": {
        "name": "Real Estate",
        "tier": 1,
        "group": "Real Estate",
        "constituents": ["PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "CCI",
                         "DLR", "CBRE", "EXR", "VICI", "AVB", "IRM", "EQR", "SBAC",
                         "INVH", "MAA", "ARE", "UDR"],
    },
    "XLC": {
        "name": "Communication Services",
        "tier": 1,
        "group": "Communication Services",
        "constituents": ["META", "GOOGL", "NFLX", "DIS", "TMUS", "CMCSA", "T", "VZ",
                         "EA", "TTWO", "OMC", "WBD", "LYV", "TTD", "FOXA", "NWSA",
                         "MTCH", "RBLX", "CHTR", "DASH"],
    },

    # ------------------- TIER 2: industry / "picks and shovels" -------------
    "SMH": {
        "name": "Semiconductors",
        "tier": 2,
        "group": "Technology",
        "constituents": ["NVDA", "TSM", "AVGO", "AMD", "ASML", "AMAT", "MU", "LRCX",
                         "KLAC", "ADI", "INTC", "NXPI", "MRVL", "TER", "MCHP", "ON",
                         "SNPS", "CDNS", "SWKS", "QRVO"],
    },
    "IGV": {
        "name": "Software",
        "tier": 2,
        "group": "Technology",
        "constituents": ["MSFT", "ORCL", "CRM", "NOW", "ADBE", "PANW", "INTU", "SNOW",
                         "CRWD", "DDOG", "WDAY", "TEAM", "HUBS", "ZS", "MDB", "NET",
                         "PLTR", "SNPS", "CDNS", "FTNT"],
    },
    "SKYY": {
        "name": "Cloud Computing",
        "tier": 2,
        "group": "Technology",
        "constituents": ["MSFT", "AMZN", "GOOGL", "ORCL", "CRM", "SNOW", "MDB", "NET",
                         "DDOG", "AKAM", "TWLO", "ZS", "CRWD", "NTNX", "BOX", "DBX"],
    },
    "OIH": {
        "name": "Oil Services & Equipment",
        "tier": 2,
        "group": "Energy",
        "constituents": ["SLB", "BKR", "HAL", "TDW", "CHRD", "WFRD", "NOV", "FTI",
                         "RIG", "OII", "LBRT", "PTEN", "HP", "WHD", "CLB"],
    },
    "XOP": {
        "name": "Oil & Gas E&P",
        "tier": 2,
        "group": "Energy",
        "constituents": ["EOG", "COP", "FANG", "DVN", "OXY", "CTRA", "APA", "MRO",
                         "MTDR", "PR", "CRC", "SM", "MUR", "CHRD", "OVV"],
    },
    "URA": {
        "name": "Uranium & Nuclear Fuel",
        "tier": 2,
        "group": "Energy",
        "constituents": ["CCJ", "LEU", "UEC", "DNN", "NXE", "UUUU", "SMR", "OKLO",
                         "BWXT", "PALAF", "URG", "LTBR"],
    },
    "TAN": {
        "name": "Solar",
        "tier": 2,
        "group": "Energy",
        "constituents": ["FSLR", "ENPH", "SEDG", "RUN", "NXT", "SHLS", "ARRY", "CSIQ",
                         "NOVA", "JKS", "DQ"],
    },
    "GDX": {
        "name": "Gold Miners",
        "tier": 2,
        "group": "Materials",
        "constituents": ["NEM", "AEM", "GOLD", "WPM", "FNV", "KGC", "GFI", "AU",
                         "RGLD", "AGI", "PAAS", "BTG", "EGO", "IAG", "HMY"],
    },
    "COPX": {
        "name": "Copper Miners",
        "tier": 2,
        "group": "Materials",
        "constituents": ["FCX", "SCCO", "TECK", "HBM", "ERO", "BHP", "RIO", "VALE",
                         "NGD", "TRQ", "CDE", "HL"],
    },
    "LIT": {
        "name": "Lithium & Battery",
        "tier": 2,
        "group": "Materials",
        "constituents": ["ALB", "SQM", "TSLA", "MP", "EOSE", "ENS", "PCRFY", "LAC",
                         "SLI", "ATLX", "FLNC", "STEM"],
    },
    "STCE": {
        "name": "Steel",
        "tier": 2,
        "group": "Materials",
        "constituents": ["NUE", "STLD", "CLF", "ZEUS", "CMC", "RS", "ATI", "CRS",
                         "TX", "GGB", "MT"],
    },
    "IHI": {
        "name": "Medical Devices",
        "tier": 2,
        "group": "Health Care",
        "constituents": ["ABT", "ISRG", "BSX", "SYK", "MDT", "BDX", "EW", "ZBH",
                         "DXCM", "PODD", "RMD", "STE", "ICUI", "TFX", "GMED"],
    },
    "XBI": {
        "name": "Biotech",
        "tier": 2,
        "group": "Health Care",
        "constituents": ["VRTX", "REGN", "AMGN", "GILD", "BIIB", "INCY", "ALNY", "NBIX",
                         "EXEL", "SRPT", "IONS", "UTHR", "BMRN", "JAZZ", "RARE"],
    },
    "IHF": {
        "name": "Healthcare Providers",
        "tier": 2,
        "group": "Health Care",
        "constituents": ["UNH", "CI", "ELV", "HCA", "CVS", "MCK", "COR", "CNC",
                         "HUM", "MOH", "UHS", "THC", "DVA", "CAH"],
    },
    "ITA": {
        "name": "Aerospace & Defense",
        "tier": 2,
        "group": "Industrials",
        "constituents": ["GE", "RTX", "BA", "LMT", "NOC", "GD", "TDG", "LHX",
                         "HWM", "AXON", "TXT", "HEI", "CW", "LDOS", "SPR"],
    },
    "PAVE": {
        "name": "US Infrastructure",
        "tier": 2,
        "group": "Industrials",
        "constituents": ["ETN", "PWR", "URI", "EMR", "PH", "NUE", "FAST", "MLM",
                         "VMC", "TT", "CSX", "UNP", "J", "ACM", "STLD"],
    },
    "ITB": {
        "name": "Homebuilders",
        "tier": 2,
        "group": "Consumer Discretionary",
        "constituents": ["DHI", "LEN", "NVR", "PHM", "TOL", "HD", "LOW", "BLDR",
                         "MAS", "MHK", "GRBK", "KBH", "MTH", "IBP", "TMHC"],
    },
    "XRT": {
        "name": "Retail",
        "tier": 2,
        "group": "Consumer Discretionary",
        "constituents": ["AMZN", "TJX", "ROST", "ORLY", "AZO", "BBY", "DKS", "GPS",
                         "ULTA", "FIVE", "BURL", "KSS", "M", "W", "CHWY"],
    },
    "KRE": {
        "name": "Regional Banks",
        "tier": 2,
        "group": "Financials",
        "constituents": ["PNC", "USB", "TFC", "FITB", "MTB", "HBAN", "RF", "KEY",
                         "CFG", "ZION", "ONB", "WBS", "EWBC", "CADE", "PB"],
    },
    "IAI": {
        "name": "Broker-Dealers & Exchanges",
        "tier": 2,
        "group": "Financials",
        "constituents": ["GS", "MS", "SCHW", "SPGI", "ICE", "CME", "MCO", "COIN",
                         "NDAQ", "CBOE", "HOOD", "RJF", "IBKR", "MKTX", "TW"],
    },
    "IYT": {
        "name": "Transportation",
        "tier": 2,
        "group": "Industrials",
        "constituents": ["UBER", "UNP", "UPS", "CSX", "NSC", "FDX", "ODFL", "DAL",
                         "UAL", "LUV", "JBHT", "CHRW", "XPO", "SAIA", "MATX"],
    },
}

# ---------------------------------------------------------------------------
# Historical members — the survivorship-bias correction.
#
# `constituents` above lists CURRENT holdings. Applying those to historical dates
# biases breadth upward in the past, because every name that was later acquired,
# merged or delisted is silently excluded — and those are disproportionately the
# names that were falling.
#
# Polygon retains delisted history (Yahoo deletes it), so these names can be
# included. Breadth is a proportion computed only over constituents with data on
# a given date, so a delisted name contributes while it was alive and drops out
# afterwards — exactly the desired behaviour. Verified available on Polygon with
# delisting dates spanning 2022-2026.
#
# Not recoverable and therefore still missing: IVN, LUN, ANTO, GLEN (TSX/LSE
# listings outside Polygon's US equity coverage).
# ---------------------------------------------------------------------------
HISTORICAL_MEMBERS: dict[str, list[str]] = {
    "XLE":  ["HES", "CTRA", "MRO"],
    "XLF":  ["MMC", "CMA", "SNV", "CADE"],
    "XLP":  ["K"],
    "XLC":  ["IPG", "PARA"],
    "XLB":  ["X"],
    "XLY":  ["GPS"],
    "XLV":  ["HOLX"],
    "XLI":  ["SPR"],
    "OIH":  ["CHX"],
    "XOP":  ["CIVI", "MRO"],
    "LIT":  ["LTHM", "PLL", "FREY", "PCRFY"],
    "TAN":  ["MAXN", "NOVA"],
    "GDX":  ["NGD"],
    "COPX": ["TRQ"],
    "STCE": ["X", "ZEUS"],
    "IHI":  ["HOLX"],
    "ITA":  ["SPR"],
    "ITB":  ["TPH"],
    "XRT":  ["GPS"],
    "KRE":  ["CMA", "SNV", "CADE"],
    "IHF":  [],
}

# Set False to reproduce the survivorship-biased behaviour (useful as an A/B
# check on how much the bias was affecting breadth results).
INCLUDE_HISTORICAL_MEMBERS = True

# Convenience views
TIER1 = [t for t, m in SECTORS.items() if m["tier"] == 1]
TIER2 = [t for t, m in SECTORS.items() if m["tier"] == 2]
ALL_TICKERS = list(SECTORS.keys())

# ---------------------------------------------------------------------------
# Metric parameters
# ---------------------------------------------------------------------------
# Calendar days of history to pull. This directly caps backtest power: the metrics
# burn roughly 500 sessions of warmup (260 warmup + 200-day Mansfield SMA + 252-day
# z-score window overlap), so usable backtest length is history minus ~2 years.
#
# Was 900 (~2.5 years), which left only ~6 months of testable signal. Polygon
# serves ~10 years (2,512 bars from 2016-07-27 for XLE), and episode count scales
# with history length rather than sampling frequency — so this is the single most
# valuable number for statistical conviction.
HISTORY_DAYS = 3800         # ~10.4 calendar years

MANSFIELD_SMA = 200         # daily Mansfield RS baseline (52 on weekly charts)
BREADTH_SMA = 50            # constituent breadth: % above n-day SMA
CMF_PERIOD = 21             # Chaikin Money Flow lookback
VOLUME_ZSCORE_WINDOW = 60   # baseline window for unusual-volume detection
RRG_WINDOW = 60             # z-score window for RRG normalisation
RRG_SHORT = 10              # short SMA of the relative-strength ratio
RRG_LONG = 40               # long SMA of the relative-strength ratio
RRG_MOM = 10                # momentum SMA applied to the RS-Ratio
RRG_TAIL = 12               # trailing observations drawn as the RRG tail
ZSCORE_WINDOW = 252         # rolling window for composite normalisation
ADLINE_WINDOW = 25          # accumulation/distribution day count window

# ---------------------------------------------------------------------------
# Display series
# ---------------------------------------------------------------------------
# How many trailing sessions of each indicator are exported for charts. This is
# the dominant term in dashboard file size — series are ~55% of the snapshot —
# so it is a single knob rather than a literal scattered through metrics.py.
SPARK_LEN = 120

# Sparklines render 40-80 px wide, so 120 points is below the pixel resolution
# of the thing drawing them — the extra points cost file size and show nothing.
# Display series are decimated to this many points. `price` is exempt because it
# feeds the full-size drill-down chart where the detail is visible.
SPARK_POINTS = 60

# Trend measurement. Deltas compare the median of the most recent
# TREND_SMOOTH sessions against the median of TREND_SMOOTH sessions ending
# `horizon` sessions ago. Medians rather than endpoints because a point-to-point
# delta on a daily series sign-flips on noise; disjoint windows because a
# baseline that contains the measurement window absorbs its own signal (the bug
# previously found in volume_trend).
TREND_SMOOTH = 5
TREND_HORIZON_FAST = 21     # default: matches the validated forward horizon
TREND_HORIZON_SLOW = 63     # for indicators built on 200d+ internal averages
TREND_MIN_HISTORY = 40      # below this, report no trend rather than a guess

# ---------------------------------------------------------------------------
# Fire-once alerts (smf/alert_state.py)
# ---------------------------------------------------------------------------
# A transition alerts once, not on every refresh. The cooldown is the flap
# guard: how long the SAME transition is suppressed before it may re-fire.
# 21 sessions = the validated forward horizon (fwd_21), so the alert clock
# speaks the same timescale as the signals. Threshold flags (unusual volume,
# block prints) get a shorter cooldown since a genuine repeat is real news.
ALERT_STATE_ENABLED = True
ALERT_COOLDOWN_SESSIONS = 21
ALERT_FLAG_COOLDOWN_SESSIONS = 5

# ---------------------------------------------------------------------------
# Validation results, in a form a human can act on
# ---------------------------------------------------------------------------
# "Rank IC 0.036" is meaningless to anyone who does not already know what rank
# IC is, and gives no sense of whether 0.036 is good. `hit_rate` is the same
# fact restated as the question a user actually has: shown two sectors, how
# often does this score pick the one that goes on to do better over the next
# month?
#
# Measured by pairwise concordance on output/backtest_observations.csv over
# ~51,000 within-date sector pairs. test_validation.py recomputes these from
# that file and fails if the panel has drifted from the data.
COIN_FLIP = 50.0
VALIDATION = {
    "vms": {
        "label": "VMS",
        "full": "Validated Momentum Score",
        "hit_rate": 52.1,
        "verdict": "use this",
        "level": "green",
        "plain": "Right more often than a coin flip, and in every one of the four "
                 "time periods tested. That makes it a real edge — and a small one. "
                 "Size positions accordingly.",
    },
    "csri": {
        "label": "CSRI",
        "full": "the original 5-component composite",
        "hit_rate": 50.4,
        "verdict": "do not trade",
        "level": "red",
        # Earlier wording here said CSRI "reversed direction between periods".
        # That is true of its rank IC, but NOT of its hit rate, which sits at
        # 50.1-50.9 in all four subperiods. Mixing the two measures in a panel
        # built for clarity reintroduces exactly the confusion it removes, so
        # the claim is now stated in the same units as the headline number.
        "plain": "Essentially a coin flip. In all four time periods tested it "
                 "scored between 50 and 51 in 100 — never meaningfully better "
                 "than guessing. Kept on the dashboard as a diagnostic only.",
    },
    "phase": {
        "label": "Signal labels",
        "full": "Stealth Accumulation, Confirmed Breakout, and so on",
        "hit_rate": None,
        "verdict": "descriptive only",
        "level": "grey",
        "plain": "Never tested as a forecast. They describe where capital has "
                 "already been, not where it is going. In testing, two of them "
                 "reversed meaning depending on which ETFs were included.",
    },
}
VALIDATION_BASIS = "10 years · 32 ETFs · 3,412 observations · ~51,000 sector pairs"
VALIDATION_CAVEAT = (
    "Momentum's edge was found on this same 10 years, so the VMS result confirms "
    "it rather than independently testing it. Nothing in VMS is fitted or tuned, "
    "so there is nothing to overfit — but watch it live before trusting it."
)

# ---------------------------------------------------------------------------
# Validated Momentum Score (VMS) — the model the backtest actually supports.
#
# The 5-component CSRI below failed validation: holdout rank IC 0.010, costed
# Sharpe 0.016, sign-flipping across subperiods, and beaten 4x by free momentum.
# Equal-weighting those same five components produced a NEGATIVE Sharpe (-0.163),
# which shows the problem is the components, not their weights.
#
# This pair cleared all five pre-committed acceptance criteria:
#   holdout IC 0.036 · Sharpe 0.530 · positive IC at 5 of 5 horizons ·
#   sign-stable across all 4 subperiods [0.063, 0.038, 0.069, 0.018]
#
# Both terms are momentum measures on different lookbacks, cross-sectionally
# z-scored before combination. 12-1 momentum carries the ranking accuracy;
# RS-Momentum lowers turnover, which is why the pair's Sharpe beats 12-1 alone
# (0.530 vs 0.338) despite a slightly lower IC.
VMS_WEIGHTS = {
    "mom_12_1": 0.50,      # 12-month return skipping the most recent month
    "rs_momentum": 0.50,   # RRG y-axis: velocity of relative strength
}

# 12-1 momentum specification, in trading sessions.
MOM_LOOKBACK = 252
MOM_SKIP = 21

# Show VMS as the primary ranking and CSRI as a secondary diagnostic. Set False to
# revert to CSRI-primary.
VMS_PRIMARY = True

# ---------------------------------------------------------------------------
# Composite Sector Rotation Index weights.
# If a component is unavailable (e.g. no dark pool feed) its weight is
# redistributed proportionally across the remaining components.
# ---------------------------------------------------------------------------
CSRI_WEIGHTS = {
    "mansfield_rs": 0.30,   # structural outperformance vs SPY
    "rs_momentum": 0.20,    # relative-strength velocity (RRG y-axis)
    "breadth": 0.20,        # % of constituents above 50d SMA
    "money_flow": 0.15,     # Chaikin Money Flow (volume-weighted accumulation)
    "inst_flow": 0.15,      # institutional footprint composite (see below)
}

# The institutional-footprint sub-score. Real dark pool / sweep data replaces
# these proxies when a flow provider is configured.
INST_FLOW_WEIGHTS = {
    "absorption": 0.40,     # high volume, no price progress = supply being absorbed
    "ad_days": 0.35,        # accumulation minus distribution days (IBD-style)
    "block_intensity": 0.25,  # share of volume arriving in outsized prints
}

# ---------------------------------------------------------------------------
# Alert thresholds (Phase 1-4 classification)
# ---------------------------------------------------------------------------
THRESHOLDS = {
    # Phase 1 - Stealth Accumulation
    "p1_mansfield_max": 0.0,        # still below zero (not yet outperforming)
    "p1_rs_mom_min": 100.0,         # but momentum turning up
    "p1_breadth_min": 45.0,         # participation broadening
    # Institutional footprint is judged cross-sectionally: it must clear a small
    # absolute floor AND sit in the upper part of its tier. A fixed absolute
    # threshold (this was 0.30) silences the signal in a broad selloff, when
    # every sector's absorption reading falls together.
    "p1_instflow_floor": -0.10,
    "p1_instflow_pct_min": 60.0,    # percentile within tier

    # Phase 2 - Confirmed Breakout
    "p2_mansfield_min": 0.0,        # Mansfield crossed above zero
    "p2_breadth_min": 60.0,
    "p2_cmf_min": 0.05,

    # Phase 3 - Distribution / exhaustion
    "p3_mansfield_min": 0.0,        # still positive on price...
    "p3_rs_mom_max": 100.0,         # ...but momentum decaying
    "p3_cmf_max": 0.0,

    # Phase 4 - Capital flight
    "p4_mansfield_max": 0.0,
    "p4_rs_mom_max": 100.0,
    "p4_breadth_max": 40.0,

    # Unusual activity flags
    "volume_z_alert": 2.0,          # volume z-score that counts as unusual
    "block_z_alert": 2.0,           # block-intensity z-score
    "csri_delta_alert": 0.75,       # 21-day CSRI jump worth flagging
}

# ---------------------------------------------------------------------------
# Optional webhook for alerts (Slack / Discord / Telegram-compatible endpoint)
# ---------------------------------------------------------------------------
WEBHOOK_URL = os.environ.get("SMF_WEBHOOK_URL", "").strip()
WEBHOOK_MIN_PHASE = {"STEALTH_ACCUMULATION", "CONFIRMED_BREAKOUT"}
