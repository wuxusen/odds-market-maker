"""Core domain types shared across feeds, pricing, market making and ledger.

All timestamps are Unix epoch milliseconds (int), matching the TxLINE wire
format (`Ts` field). In simulation, timestamps come from the simulated clock,
never from the wall clock, so runs are fully deterministic.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional, Sequence


class Market(str, enum.Enum):
    """Supported market types. v0.1 focuses on the 1X2 (match odds) market."""

    MATCH_ODDS = "1X2"  # home / draw / away


# Canonical outcome names for the 1X2 market, in wire order.
MATCH_ODDS_OUTCOMES = ("HOME", "DRAW", "AWAY")


@dataclass(frozen=True)
class OddsTick:
    """A single bookmaker odds update for one market of one fixture.

    Mirrors the TxLINE ``OddsPayload`` (FixtureId/MessageId/Ts/Bookmaker/
    SuperOddsType/Prices/Pct/InRunning) normalised to decimal odds.
    """

    fixture_id: int
    message_id: str
    ts: int  # epoch ms
    bookmaker: str
    bookmaker_id: int
    market: Market
    outcomes: Sequence[str]  # e.g. ("HOME", "DRAW", "AWAY")
    prices: Sequence[float]  # decimal odds, aligned with `outcomes`
    in_running: bool
    market_period: str = "FT"  # full time by default
    market_parameters: str = ""  # e.g. handicap line; unused for 1X2

    def implied(self) -> list[float]:
        """Raw implied probabilities (contain the bookmaker margin)."""
        return [1.0 / p for p in self.prices]


class MatchPhase(str, enum.Enum):
    NOT_STARTED = "NS"
    FIRST_HALF = "H1"
    HALF_TIME = "HT"
    SECOND_HALF = "H2"
    FINISHED = "F"


@dataclass(frozen=True)
class ScoreTick:
    """Score/state update for a fixture (TxLINE scores feed, normalised)."""

    fixture_id: int
    ts: int  # epoch ms
    clock_seconds: int  # match clock
    phase: MatchPhase
    home_goals: int
    away_goals: int
    home_red_cards: int = 0
    away_red_cards: int = 0


class EventKind(str, enum.Enum):
    GOAL = "GOAL"
    RED_CARD = "RED_CARD"
    KICK_OFF = "KICK_OFF"
    HALF_TIME = "HALF_TIME"
    FULL_TIME = "FULL_TIME"


@dataclass(frozen=True)
class MatchEvent:
    """A discrete match incident (used to trip the circuit breaker)."""

    fixture_id: int
    ts: int
    kind: EventKind
    team: Optional[str] = None  # "HOME" / "AWAY" for goals & cards
    detail: str = ""


class Side(str, enum.Enum):
    """Side of a quote from the *market maker's* point of view.

    Quotes are expressed on binary outcome contracts that pay 1.0 if the
    outcome wins, priced in probability space (0..1):
      * BID  — we buy the contract (taker sells to us).
      * ASK  — we sell the contract (taker buys from us).
    """

    BID = "BID"
    ASK = "ASK"


@dataclass(frozen=True)
class Quote:
    """One side of our two-way market on a single outcome."""

    fixture_id: int
    market: Market
    outcome: str
    side: Side
    price: float  # probability-space price in (0, 1)
    size: float  # max notional (stake units) we accept at this price
    ts: int

    @property
    def decimal_odds(self) -> float:
        return 1.0 / self.price


@dataclass
class QuoteSet:
    """The full set of live quotes for one fixture, plus quoting metadata."""

    fixture_id: int
    ts: int
    quotes: list[Quote] = field(default_factory=list)
    fair: dict[str, float] = field(default_factory=dict)  # outcome -> fair prob
    confidence: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def get(self, outcome: str, side: Side) -> Optional[Quote]:
        for q in self.quotes:
            if q.outcome == outcome and q.side == side:
                return q
        return None


@dataclass(frozen=True)
class Fill:
    """A simulated execution against one of our quotes."""

    fill_id: int
    fixture_id: int
    market: Market
    outcome: str
    side: Side  # our side: ASK means we sold the contract
    price: float  # probability-space price
    qty: float  # number of unit contracts
    ts: int
    counterparty: str = "sim-taker"
