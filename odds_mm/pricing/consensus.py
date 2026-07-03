"""Multi-bookmaker consensus fair price with a confidence score.

Each bookmaker tick is de-vigged to that book's probability estimate; the
consensus is a staleness-weighted average across books. Confidence in the
consensus feeds directly into the quoted spread (low confidence -> wide or
no quotes), and is built from three observable ingredients:

* **coverage** — how many distinct books contribute (more books, more trust);
* **agreement** — cross-book dispersion of de-vigged probabilities
  (books disagreeing means someone is slow or something is happening);
* **freshness** — age of the contributing ticks vs a max-age horizon.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..types import OddsTick
from .devig import devig_multiplicative, devig_power


@dataclass(frozen=True)
class FairPrice:
    """Consensus output for one market at one instant."""

    fixture_id: int
    ts: int
    outcomes: Sequence[str]
    probabilities: Sequence[float]  # de-vigged consensus, sums to 1
    confidence: float  # 0..1
    n_books: int
    dispersion: float  # mean per-outcome stdev across books

    def prob(self, outcome: str) -> float:
        return self.probabilities[list(self.outcomes).index(outcome)]


class ConsensusPricer:
    """Maintains the latest tick per bookmaker and computes consensus."""

    def __init__(
        self,
        method: str = "power",
        max_age_ms: int = 60_000,
        min_books: int = 2,
        full_confidence_books: int = 4,
        dispersion_scale: float = 0.03,
    ) -> None:
        if method not in ("power", "multiplicative"):
            raise ValueError(f"unknown de-vig method: {method}")
        self.method = method
        self.max_age_ms = max_age_ms
        self.min_books = min_books
        self.full_confidence_books = full_confidence_books
        self.dispersion_scale = dispersion_scale
        self._latest: dict[int, OddsTick] = {}  # bookmaker_id -> tick

    def on_tick(self, tick: OddsTick) -> None:
        cur = self._latest.get(tick.bookmaker_id)
        if cur is None or tick.ts >= cur.ts:
            self._latest[tick.bookmaker_id] = tick

    def _devig(self, tick: OddsTick) -> list[float]:
        fn = devig_power if self.method == "power" else devig_multiplicative
        return fn(tick.prices)

    def fair_price(self, now_ms: int) -> Optional[FairPrice]:
        """Consensus as of ``now_ms``; ``None`` if too few fresh books."""
        fresh = [
            t
            for t in self._latest.values()
            if now_ms - t.ts <= self.max_age_ms
        ]
        if len(fresh) < self.min_books:
            return None

        outcomes = fresh[0].outcomes
        per_book: list[list[float]] = []
        weights: list[float] = []
        for t in fresh:
            per_book.append(self._devig(t))
            age = max(0, now_ms - t.ts)
            weights.append(1.0 - 0.5 * (age / self.max_age_ms))  # linear decay

        wz = sum(weights)
        consensus = [
            sum(w * probs[i] for w, probs in zip(weights, per_book)) / wz
            for i in range(len(outcomes))
        ]
        z = sum(consensus)
        consensus = [p / z for p in consensus]

        if len(per_book) >= 2:
            dispersion = statistics.fmean(
                statistics.pstdev(probs[i] for probs in per_book)
                for i in range(len(outcomes))
            )
        else:
            dispersion = 0.0

        coverage = min(1.0, len(fresh) / self.full_confidence_books)
        agreement = max(0.0, 1.0 - dispersion / self.dispersion_scale)
        freshness = statistics.fmean(
            max(0.0, 1.0 - (now_ms - t.ts) / self.max_age_ms) for t in fresh
        )
        confidence = coverage * (0.6 * agreement + 0.4 * freshness)

        return FairPrice(
            fixture_id=fresh[0].fixture_id,
            ts=now_ms,
            outcomes=tuple(outcomes),
            probabilities=tuple(consensus),
            confidence=max(0.0, min(1.0, confidence)),
            n_books=len(fresh),
            dispersion=dispersion,
        )

    def reset(self) -> None:
        """Drop all cached book state (used after long circuit-breaker halts)."""
        self._latest.clear()
