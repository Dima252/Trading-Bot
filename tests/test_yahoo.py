"""Yahoo adapter parsing. No network -- payloads are constructed inline.

The back-adjustment is the part worth testing: get it wrong and a 4:1 split
reads as a 75% crash, which would fire every mean-reversion setup in the
universe on the same day and poison the entire backtest.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading_bot.data.yahoo import _parse, to_yahoo_symbol


def stamp(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def payload(days, opens, highs, lows, closes, volumes, adjclose=None) -> dict:
    indicators = {
        "quote": [
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            }
        ]
    }
    if adjclose is not None:
        indicators["adjclose"] = [{"adjclose": adjclose}]
    return {
        "chart": {
            "result": [
                {"timestamp": [stamp(d) for d in days], "indicators": indicators}
            ],
            "error": None,
        }
    }


DAYS = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)]


# --- symbol mapping ------------------------------------------------------- #


def test_dotted_symbols_are_mapped() -> None:
    assert to_yahoo_symbol("BRK.B") == "BRK-B"
    assert to_yahoo_symbol("AAPL") == "AAPL"


# --- back-adjustment ------------------------------------------------------ #


def test_prices_are_back_adjusted_by_the_adjclose_ratio() -> None:
    series = _parse(
        "AAA",
        payload(
            DAYS,
            opens=[100.0, 100.0, 100.0],
            highs=[110.0, 110.0, 110.0],
            lows=[90.0, 90.0, 90.0],
            closes=[100.0, 100.0, 100.0],
            volumes=[1_000_000, 1_000_000, 1_000_000],
            adjclose=[50.0, 50.0, 100.0],  # a 2:1 factor on the first two bars
        ),
    )
    assert series[0].close == 50.0
    assert series[0].open == 50.0
    assert series[0].high == 55.0
    assert series[0].low == 45.0
    assert series[2].close == 100.0  # unadjusted bar untouched


def test_a_split_does_not_read_as_a_crash() -> None:
    """The failure this whole function exists to prevent."""
    raw_closes = [400.0, 404.0, 101.0, 102.0]  # 4:1 split before the third bar
    adj = [100.0, 101.0, 101.0, 102.0]
    days = DAYS + [date(2026, 3, 5)]

    series = _parse(
        "AAA",
        payload(
            days,
            opens=raw_closes,
            highs=[c * 1.01 for c in raw_closes],
            lows=[c * 0.99 for c in raw_closes],
            closes=raw_closes,
            volumes=[1_000_000] * 4,
            adjclose=adj,
        ),
    )

    closes = [b.close for b in series.bars]
    assert closes == [100.0, 101.0, 101.0, 102.0]
    # no day-to-day move larger than 2%
    moves = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    assert max(moves) < 0.02


def test_dollar_volume_survives_adjustment() -> None:
    """The liquidity screen reads close * volume; adjusting one and not the
    other would silently distort it."""
    series = _parse(
        "AAA",
        payload(
            DAYS[:1],
            opens=[100.0],
            highs=[100.0],
            lows=[100.0],
            closes=[100.0],
            volumes=[1_000_000],
            adjclose=[25.0],  # 4:1
        ),
    )
    bar = series[0]
    assert bar.close == 25.0
    assert bar.volume == pytest.approx(4_000_000)
    assert bar.dollar_volume == pytest.approx(100.0 * 1_000_000)


def test_missing_adjclose_leaves_prices_untouched() -> None:
    series = _parse(
        "AAA",
        payload(
            DAYS[:1], [100.0], [110.0], [90.0], [105.0], [1_000], adjclose=None
        ),
    )
    assert series[0].close == 105.0


# --- messy payloads ------------------------------------------------------- #


def test_null_bars_are_dropped_not_zero_filled() -> None:
    series = _parse(
        "AAA",
        payload(
            DAYS,
            opens=[100.0, None, 102.0],
            highs=[101.0, None, 103.0],
            lows=[99.0, None, 101.0],
            closes=[100.0, None, 102.0],
            volumes=[1_000, None, 1_000],
            adjclose=[100.0, None, 102.0],
        ),
    )
    assert len(series) == 2
    assert series.days == [DAYS[0], DAYS[2]]


def test_duplicate_sessions_are_collapsed() -> None:
    days = [DAYS[0], DAYS[0], DAYS[1]]
    series = _parse(
        "AAA",
        payload(
            days,
            [100.0] * 3,
            [101.0] * 3,
            [99.0] * 3,
            [100.0] * 3,
            [1_000] * 3,
            [100.0] * 3,
        ),
    )
    assert len(series) == 2  # BarSeries would reject duplicates outright


def test_bars_come_back_in_chronological_order() -> None:
    days = [DAYS[2], DAYS[0], DAYS[1]]
    series = _parse(
        "AAA",
        payload(
            days,
            [100.0] * 3,
            [101.0] * 3,
            [99.0] * 3,
            [100.0] * 3,
            [1_000] * 3,
            [100.0] * 3,
        ),
    )
    assert series.days == sorted(series.days)


def test_an_error_payload_raises() -> None:
    with pytest.raises(RuntimeError):
        _parse("AAA", {"chart": {"error": {"code": "Not Found"}, "result": None}})


def test_an_empty_result_returns_none() -> None:
    assert _parse("AAA", {"chart": {"result": [], "error": None}}) is None


def test_a_result_with_no_usable_bars_returns_none() -> None:
    assert (
        _parse(
            "AAA",
            payload(DAYS[:1], [None], [None], [None], [None], [None], [None]),
        )
        is None
    )
