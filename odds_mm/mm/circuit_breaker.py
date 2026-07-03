"""Circuit breaker: fail-safe first, quotes second.

Design lineage: this is the direct transplant of the "no naked exposure /
hard risk gates" doctrine from our production perpetual-futures trading
stack (binance-perp-toolkit): any anomaly cancels all resting quotes
*immediately* and the system enters a safe state; re-arming requires both a
cool-down and fresh, healthy data. Quoting through a goal announcement or a
stale feed is how market makers get picked off — the breaker makes that
structurally impossible rather than merely unlikely.

Trip conditions (any one is sufficient):

* **Score shock** — goal, red card or score mismatch: fair value just
  jumped discontinuously; stale quotes are free money for takers.
* **Feed staleness** — no tick for ``stale_after_ms``: we are blind.
* **Feed latency** — tick timestamps lag our clock by more than
  ``max_latency_ms``: we are quoting on the past.

State machine: ``ACTIVE -> TRIPPED -> (cooldown elapsed AND fresh data seen)
-> ACTIVE``. All transitions take an explicit ``now_ms`` so behaviour is
deterministic in simulation and tests.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from ..types import EventKind, MatchEvent, ScoreTick


class BreakerState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    TRIPPED = "TRIPPED"


@dataclass
class TripRecord:
    ts: int
    reason: str


@dataclass
class CircuitBreaker:
    """Deterministic, injectable-clock circuit breaker."""

    stale_after_ms: int = 20_000
    max_latency_ms: int = 5_000
    cooldown_ms: int = 15_000

    state: BreakerState = BreakerState.ACTIVE
    trips: list[TripRecord] = field(default_factory=list)
    _tripped_at: int = 0
    _last_reason: str = ""
    _last_tick_ts: Optional[int] = None  # local receipt time of last feed item
    _fresh_since_trip: bool = False
    _last_score: Optional[tuple[int, int, int, int]] = None

    # -- inputs --------------------------------------------------------------

    def on_feed_item(self, item_ts: int, now_ms: int) -> None:
        """Record any feed item arrival; checks transport latency."""
        self._last_tick_ts = now_ms
        if self.state == BreakerState.TRIPPED:
            self._fresh_since_trip = True
        if now_ms - item_ts > self.max_latency_ms:
            self.trip(now_ms, f"feed latency {now_ms - item_ts}ms > {self.max_latency_ms}ms")

    def on_event(self, event: MatchEvent, now_ms: int) -> None:
        self.on_feed_item(event.ts, now_ms)
        if event.kind in (EventKind.GOAL, EventKind.RED_CARD):
            self.trip(now_ms, f"match incident: {event.kind.value} {event.team or ''}".strip())

    def on_score(self, score: ScoreTick, now_ms: int) -> None:
        self.on_feed_item(score.ts, now_ms)
        key = (score.home_goals, score.away_goals, score.home_red_cards, score.away_red_cards)
        if self._last_score is not None and key != self._last_score:
            # Defence in depth: even if the discrete MatchEvent was missed,
            # a score delta on the snapshot channel still trips the breaker.
            self.trip(now_ms, f"score change {self._last_score} -> {key}")
        self._last_score = key

    def heartbeat(self, now_ms: int) -> None:
        """Watchdog; call periodically even when no data arrives."""
        if (
            self._last_tick_ts is not None
            and now_ms - self._last_tick_ts > self.stale_after_ms
        ):
            self.trip(now_ms, f"feed stale for {now_ms - self._last_tick_ts}ms")

    # -- state ---------------------------------------------------------------

    def trip(self, now_ms: int, reason: str) -> None:
        if self.state != BreakerState.TRIPPED:
            self.trips.append(TripRecord(now_ms, reason))
        # Re-tripping while tripped extends the cooldown window.
        self.state = BreakerState.TRIPPED
        self._tripped_at = now_ms
        self._last_reason = reason
        self._fresh_since_trip = False

    def can_quote(self, now_ms: int) -> bool:
        """True iff quoting is safe. Also performs the re-arm transition."""
        self.heartbeat(now_ms)
        if self.state == BreakerState.ACTIVE:
            return True
        if (
            now_ms - self._tripped_at >= self.cooldown_ms
            and self._fresh_since_trip
            and self._last_tick_ts is not None
            and now_ms - self._last_tick_ts <= self.stale_after_ms
        ):
            self.state = BreakerState.ACTIVE
            self._last_reason = ""
            return True
        return False

    @property
    def reason(self) -> str:
        return self._last_reason

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "reason": self._last_reason,
            "trip_count": len(self.trips),
            "last_trips": [
                {"ts": t.ts, "reason": t.reason} for t in self.trips[-5:]
            ],
        }
