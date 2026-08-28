"""What the agent is allowed to trade, and the sector map the exposure limit
needs.

Two universes:

  WIDE (default)   the S&P 500, frozen into config/universe_sp500.json
  NARROW           the 85 hand-listed mega caps below, as a fallback

SURVIVORSHIP WARNING, and it is worse for the wide list. Both are present-day
membership snapshots, so every name in them survived. But index membership is
also *awarded after a company has already done well*, so the wide list adds
index-addition bias on top. Backtests on either are optimistic; the wide one
more so. What stays valid is the RELATIVE comparison -- narrow vs wide, setup vs
setup, filter vs no filter -- because both carry the same bias in the same
direction (README section 10).
"""

from __future__ import annotations

import json
import os
from datetime import date

from .models import BarSeries

WIDE_UNIVERSE_FILE = os.path.join("config", "universe_sp500.json")

BENCHMARK = "SPY"

# 13-week T-bill discount rate. Not tradeable -- it is what idle cash earns.
# A backtest that pays 0% on cash badly understates a strategy that is only
# deployed part of the time, which is exactly what a defensive profile is.
CASH_RATE = "^IRX"

# Liquid US large caps with a broad sector spread. Sector labels are the
# coarse GICS-style buckets the exposure cap operates on; precision beyond
# this does not change any decision.
SECTORS: dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AVGO": "Technology", "AMD": "Technology", "CRM": "Technology",
    "ORCL": "Technology", "ADBE": "Technology", "CSCO": "Technology",
    "INTC": "Technology", "QCOM": "Technology", "TXN": "Technology",
    "MU": "Technology", "NOW": "Technology", "INTU": "Technology",
    "IBM": "Technology", "AMAT": "Technology", "LRCX": "Technology",
    # Communication services
    "GOOGL": "Communication", "META": "Communication", "NFLX": "Communication",
    "DIS": "Communication", "TMUS": "Communication", "CMCSA": "Communication",
    # Consumer discretionary
    "AMZN": "Discretionary", "TSLA": "Discretionary", "HD": "Discretionary",
    "MCD": "Discretionary", "NKE": "Discretionary", "LOW": "Discretionary",
    "SBUX": "Discretionary", "BKNG": "Discretionary", "TJX": "Discretionary",
    # Consumer staples
    "WMT": "Staples", "PG": "Staples", "KO": "Staples", "PEP": "Staples",
    "COST": "Staples", "PM": "Staples", "MDLZ": "Staples", "CL": "Staples",
    # Financials
    "BRK.B": "Financials", "JPM": "Financials", "V": "Financials",
    "MA": "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS": "Financials", "MS": "Financials", "AXP": "Financials",
    "SCHW": "Financials", "BLK": "Financials", "SPGI": "Financials",
    # Health care
    "UNH": "Healthcare", "JNJ": "Healthcare", "LLY": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "PFE": "Healthcare",
    "TMO": "Healthcare", "ABT": "Healthcare", "DHR": "Healthcare",
    "AMGN": "Healthcare", "ISRG": "Healthcare", "CVS": "Healthcare",
    # Industrials
    "CAT": "Industrials", "HON": "Industrials", "UNP": "Industrials",
    "BA": "Industrials", "GE": "Industrials", "RTX": "Industrials",
    "LMT": "Industrials", "DE": "Industrials", "UPS": "Industrials",
    "ADP": "Industrials",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy",
    "EOG": "Energy", "PSX": "Energy", "MPC": "Energy",
    # Utilities / real estate / materials
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "AMT": "RealEstate", "PLD": "RealEstate",
    "LIN": "Materials", "SHW": "Materials", "FCX": "Materials",
}

NARROW_SECTORS: dict[str, str] = dict(SECTORS)


def _load_wide() -> dict[str, str] | None:
    """The frozen index snapshot, if it has been captured."""
    try:
        with open(WIDE_UNIVERSE_FILE, encoding="utf-8") as fh:
            return dict(json.load(fh)["sectors"])
    except (OSError, KeyError, ValueError):
        return None


def load_sectors(wide: bool = True) -> dict[str, str]:
    """Ticker -> sector. Falls back to the narrow list when the snapshot is
    missing, rather than silently trading a universe with no sector map."""
    if wide:
        loaded = _load_wide()
        if loaded:
            return loaded
    return dict(NARROW_SECTORS)


# Module-level default, so existing callers pick up the wide list automatically.
SECTORS = load_sectors(wide=True)
DEFAULT_UNIVERSE: list[str] = sorted(SECTORS)


def all_symbols(include_benchmark: bool = True) -> list[str]:
    extras = [BENCHMARK, CASH_RATE] if include_benchmark else []
    return extras + DEFAULT_UNIVERSE


def is_tradeable(symbol: str) -> bool:
    """Index and rate series are inputs, never positions.

    The benchmark is excluded separately by callers, which know their own
    benchmark symbol; this covers the `^`-prefixed series.
    """
    return not symbol.startswith("^") and symbol != BENCHMARK


# ---------------------------------------------------------------------- #


def liquidity_screen(
    universe: dict[str, BarSeries],
    day: date,
    min_price: float = 10.0,
    min_dollar_volume: float = 20_000_000.0,
    lookback: int = 20,
) -> list[str]:
    """Names tradeable AS OF `day`.

    Computed from the bars themselves at that date rather than from a
    present-day snapshot, so the screen itself is at least point-in-time even
    though the ticker list is not.
    """
    passing: list[str] = []
    for symbol, series in universe.items():
        i = series.index_asof(day)
        if i is None or i < lookback:
            continue
        bar = series[i]
        if bar.close < min_price:
            continue
        window = series.bars[i - lookback + 1 : i + 1]
        avg_dollar_volume = sum(b.dollar_volume for b in window) / len(window)
        if avg_dollar_volume < min_dollar_volume:
            continue
        passing.append(symbol)
    return sorted(passing)
