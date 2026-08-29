"""Versioned configuration.

Every trade records the `version` that produced it. Without that, attribution
breaks the moment anything changes -- see README section 9.5.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

# How well each setup type works in each regime, 0.0 - 1.0.
# These are priors. The adaptive layer replaces them with measured expectancy
# once there are enough trades per bucket (README section 9.5).
DEFAULT_REGIME_FIT: dict[str, dict[str, float]] = {
    "breakout": {"trend": 1.0, "chop": 0.30, "high_vol": 0.50},
    "pullback": {"trend": 0.90, "chop": 0.60, "high_vol": 0.40},
    "mean_reversion": {"trend": 0.40, "chop": 0.90, "high_vol": 0.50},
}


@dataclass(frozen=True)
class Policy:
    version: str = "v1"

    # --- risk limits (the constitution) ---
    max_risk_per_trade: float = 0.01
    max_portfolio_heat: float = 0.06
    max_sector_exposure: float = 0.25
    max_position_pct: float = 0.15
    min_cash_reserve: float = 0.10
    max_new_positions_per_day: int = 3
    daily_loss_breaker: float = -0.03
    weekly_loss_breaker: float = -0.06

    # --- exit rules ---
    time_stop_days: int = 10
    time_stop_min_r: float = 0.5
    max_hold_days: int = 40
    trail_trigger_r: float = 1.5
    trail_atr_mult: float = 2.0

    # Per-setup exit horizons, overriding the two above. One global horizon has
    # to be wrong for at least one setup: a mean-reversion entry targets the
    # 20-day mean because "holding for 3R turns a good win rate into a bad one"
    # -- its thesis resolves in days -- while a breakout ride needs weeks. The
    # shipped config held both for up to 60 days.
    #
    # Empty means "use the global values", so an unset policy behaves exactly as
    # it did before.
    setup_time_stop_days: dict[str, int] = field(default_factory=dict)
    setup_max_hold_days: dict[str, int] = field(default_factory=dict)

    # Which regimes each setup may OPEN in, overriding `tradeable_regimes`.
    #
    # The global list is all-or-nothing across setups, which forces a choice the
    # system should not have to make: `regime_fit` rates mean_reversion 0.90 in
    # chop and 0.40 in trend, and `tradeable_regimes: [trend]` then lets it fire
    # only where it is rated worst. Opening chop globally to fix that would also
    # admit the breakout setup, rated 0.30 there.
    #
    # Empty means "use tradeable_regimes for everything", so an unset policy is
    # unchanged.
    setup_regimes: dict[str, list[str]] = field(default_factory=dict)

    # --- rotation ---
    switching_premium: float = 1.3
    min_candidate_score: float = 40.0

    # A position must reach this fraction of the per-trade risk budget to be
    # worth taking. Starved allocations pay full spread and commission for a
    # fraction of the intended exposure, and burn a daily slot doing it.
    min_risk_fraction: float = 0.5

    # How far above last night's trigger the close-confirmation job will still
    # buy. A buy limit sitting below the market never fills, so the entry has to
    # be lifted to the live price -- but chasing an extended move pays away the
    # edge, so past this the setup is abandoned rather than bought late.
    max_entry_chase_pct: float = 0.02

    # --- regime gate ---
    # Which regimes the agent may OPEN new positions in. Defensive exits and
    # stop maintenance always run regardless -- a gate that could trap you in a
    # position would be a liability, not a safeguard.
    #
    # The first backtest measured trend +0.129R against chop -0.144R over ~535
    # trades each. Restricting to trend is variant A1; the default stays open so
    # the baseline is unchanged.
    tradeable_regimes: list[str] = field(
        default_factory=lambda: ["trend", "chop", "high_vol"]
    )

    # --- setup tunables ---
    # The ranking diagnostic measured depth_atr at z=-12.1 and rsi at z=+16.4
    # over 25k candidates: the pullback quality score rewards DEEP dips and LOW
    # RSI, and the data says both signs are backwards. This flips them so the
    # hypothesis can be tested out of sample rather than assumed.
    pullback_favour_shallow: bool = False

    # Sub-weights inside the pullback quality score. The diagnostic measured
    # rsi at z=+16.4 and depth at z=-12.1 over 24k candidates, while the trend
    # spread came in at z=+2.2 -- so the weight belongs on the RSI reset, not
    # spread across three inputs as if they were equally informative.
    pullback_w_trend: float = 0.40
    pullback_w_reset: float = 0.35
    pullback_w_depth: float = 0.25

    # --- scoring weights (w_setup + w_regime + w_rr must sum to 1.0) ---
    w_setup: float = 0.60
    w_regime: float = 0.20
    w_rr: float = 0.20
    w_event: float = 0.50
    rr_cap: float = 3.0

    # --- holding re-score ---
    stale_score_decay: float = 0.90
    w_performance: float = 5.0

    regime_fit: dict[str, dict[str, float]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_REGIME_FIT.items()}
    )

    def __post_init__(self) -> None:
        total = self.w_setup + self.w_regime + self.w_rr
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"scoring weights must sum to 1.0, got {total:.4f} "
                "(w_setup + w_regime + w_rr)"
            )
        if not 0 < self.max_risk_per_trade <= 0.05:
            raise ValueError("max_risk_per_trade must be in (0, 0.05]")
        if self.max_portfolio_heat < self.max_risk_per_trade:
            raise ValueError("max_portfolio_heat cannot be below max_risk_per_trade")
        if self.switching_premium < 1.0:
            raise ValueError("switching_premium below 1.0 guarantees churn")

    def may_open_in(self, regime: str, setup_type: str | None = None) -> bool:
        """Whether a position may be OPENED. Exits are never gated by regime."""
        if setup_type is not None and setup_type in self.setup_regimes:
            return regime in self.setup_regimes[setup_type]
        return regime in self.tradeable_regimes

    def any_setup_may_open_in(self, regime: str) -> bool:
        """Whether ANY setup can trade here -- the cheap check before ranking."""
        if regime in self.tradeable_regimes:
            return True
        return any(regime in allowed for allowed in self.setup_regimes.values())

    def fit_for(self, setup_type: str, regime: str) -> float:
        return self.regime_fit.get(setup_type, {}).get(regime, 0.5)

    def time_stop_for(self, setup_type: str) -> int:
        """Days before an unresolved position is cut, for this setup."""
        return self.setup_time_stop_days.get(setup_type, self.time_stop_days)

    def max_hold_for(self, setup_type: str) -> int:
        """Days before a position is closed regardless, for this setup."""
        return self.setup_max_hold_days.get(setup_type, self.max_hold_days)

    def with_changes(self, **kwargs: Any) -> Policy:
        """Produce a new version. Never mutate a policy in place -- trades
        reference it by version."""
        return replace(self, **kwargs)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown policy keys: {sorted(unknown)}")
        return cls(**data)

    @classmethod
    def from_yaml(cls, path: str) -> Policy:
        import yaml  # optional dependency; only needed to load from disk

        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})
