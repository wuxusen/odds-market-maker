"""Quoting engine: fair value in, guarded two-sided quotes out.

Quote construction (probability space, per outcome):

    half_spread = clamp(base + vol_mult * sigma + conf_mult * (1 - confidence),
                        min_half_spread, max_half_spread)
    mid'        = fair + inventory_skew          (skew from InventoryBook)
    bid         = mid' - half_spread
    ask         = mid' + half_spread

Order of gates (evaluated every cycle, earliest wins):

1. circuit breaker — if not ACTIVE, cancel everything, quote nothing;
2. confidence floor — consensus too weak, quote nothing;
3. inventory hard caps — per-side size gating (may zero one side).

The engine never places anything real: it publishes a QuoteSet that the
paper-trading loop (or a future exchange adapter) consumes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..pricing.consensus import FairPrice
from ..types import Market, Quote, QuoteSet, Side
from .circuit_breaker import CircuitBreaker
from .inventory import InventoryBook


@dataclass
class MMConfig:
    base_half_spread: float = 0.010  # 1 prob-point each side
    vol_mult: float = 3.0  # extra spread per unit of fair-value volatility
    conf_mult: float = 0.030  # extra spread at zero confidence
    min_half_spread: float = 0.005
    max_half_spread: float = 0.060
    min_confidence: float = 0.25  # below this, stand down
    base_size: float = 20.0  # contracts per side before inventory gating
    min_price: float = 0.01  # never quote outside (1%, 99%)
    max_price: float = 0.99
    vol_window: int = 12  # fair-price samples for realised volatility


class _VolEstimator:
    """Realised volatility of the fair probability (per outcome, rolling)."""

    def __init__(self, window: int) -> None:
        self.window = window
        self._hist: dict[str, deque[float]] = {}

    def update(self, outcome: str, prob: float) -> float:
        h = self._hist.setdefault(outcome, deque(maxlen=self.window))
        h.append(prob)
        if len(h) < 3:
            return 0.0
        diffs = [abs(b - a) for a, b in zip(list(h), list(h)[1:])]
        return sum(diffs) / len(diffs)


class MarketMaker:
    """Stateless-per-cycle quoter over stateful risk components."""

    def __init__(
        self,
        config: MMConfig,
        inventory: InventoryBook,
        breaker: CircuitBreaker,
    ) -> None:
        self.config = config
        self.inventory = inventory
        self.breaker = breaker
        self._vol = _VolEstimator(config.vol_window)

    def quote(self, fair: FairPrice, now_ms: int) -> QuoteSet:
        """Produce the current quote set (possibly halted/empty)."""
        qs = QuoteSet(fixture_id=fair.fixture_id, ts=now_ms)
        qs.fair = dict(zip(fair.outcomes, fair.probabilities))
        qs.confidence = fair.confidence

        # Gate 1: circuit breaker (fail safe first).
        if not self.breaker.can_quote(now_ms):
            qs.halted, qs.halt_reason = True, f"breaker: {self.breaker.reason}"
            return qs

        # Gate 2: consensus confidence floor.
        if fair.confidence < self.config.min_confidence:
            qs.halted = True
            qs.halt_reason = (
                f"low confidence {fair.confidence:.2f} < {self.config.min_confidence}"
            )
            return qs

        cfg = self.config
        for outcome, p in zip(fair.outcomes, fair.probabilities):
            sigma = self._vol.update(outcome, p)
            half = cfg.base_half_spread + cfg.vol_mult * sigma
            half += cfg.conf_mult * (1.0 - fair.confidence)
            half = max(cfg.min_half_spread, min(cfg.max_half_spread, half))

            mid = p + self.inventory.skew(fair.fixture_id, outcome, half)

            for side, px in (
                (Side.BID, mid - half),
                (Side.ASK, mid + half),
            ):
                px = max(cfg.min_price, min(cfg.max_price, px))
                # Gate 3: inventory hard caps (may zero a side).
                size = self.inventory.allowed_size(
                    fair.fixture_id, outcome, side, cfg.base_size
                )
                if size <= 0:
                    continue
                qs.quotes.append(
                    Quote(
                        fixture_id=fair.fixture_id,
                        market=Market.MATCH_ODDS,
                        outcome=outcome,
                        side=side,
                        price=round(px, 5),
                        size=round(size, 2),
                        ts=now_ms,
                    )
                )
        return qs
