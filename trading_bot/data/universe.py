"""What the agent is allowed to trade, and the sector map the exposure limit
needs.

SURVIVORSHIP WARNING: this is a present-day list of names that survived and
stayed liquid. Backtesting on it is biased upward and there is no cheap fix at
this scale. Use results for RELATIVE comparisons -- setup vs setup, regime vs
regime, filter vs no filter -- and discount absolute returns (README section 10).
"""

from __future__ import annotations

from datetime import date

from .models import BarSeries

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

DEFAULT_UNIVERSE: list[str] = sorted(SECTORS)


def all_symbols(include_benchmark: bool = True) -> list[str]:
    extras = [BENCHMARK, CASH_RATE] if include_benchmark else []
    return extras + DEFAULT_UNIVERSE


def is_tradeable(symbol: str) -> bool:
    """Index and rate series are inputs, never positions."""
    return not symbol.startswith("^") and symbol != BENCHMARK


def sector_of(symbol: str) -> str:
    return SECTORS.get(symbol, "UNKNOWN")


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
