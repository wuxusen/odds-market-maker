"""Deterministic simulated in-play feed for demos and tests.

Simulates one soccer match end-to-end:

* A latent "true" in-play probability model: remaining-goals Poisson model
  driven by per-team scoring intensities, current score and match clock.
* Discrete incidents (goals, red cards) pre-sampled from the seeded RNG;
  a red card shifts both teams' intensities for the rest of the match.
* N simulated bookmakers quoting 1X2 around the true probabilities with
  per-book bias, noise, margin (vig) and update lag — so the de-vig /
  consensus layer has realistic dispersion to chew on.

Everything derives from ``random.Random(seed)`` and a simulated clock.
No wall-clock time, no global RNG: the same seed always produces the
byte-identical event stream.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterator, Optional

from ..types import (
    EventKind,
    Market,
    MATCH_ODDS_OUTCOMES,
    MatchEvent,
    MatchPhase,
    OddsTick,
    ScoreTick,
)
from .base import FeedAdapter, FeedItem

MATCH_MINUTES = 90
MAX_GOALS = 10  # Poisson truncation for the win-probability grid


def _poisson_pmf(lam: float, k: int) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def match_odds_probabilities(
    home_goals: int,
    away_goals: int,
    remaining_minutes: float,
    lambda_home: float,
    lambda_away: float,
) -> tuple[float, float, float]:
    """P(home win), P(draw), P(away win) from a remaining-goals Poisson model.

    ``lambda_*`` are expected goals per 90 minutes; remaining expectation
    scales linearly with the clock. This is the standard in-play baseline
    model (independent Poisson), good enough to generate realistic dynamics.
    """
    rem = max(0.0, remaining_minutes) / MATCH_MINUTES
    lh, la = lambda_home * rem, lambda_away * rem
    p_home = p_draw = p_away = 0.0
    for i in range(MAX_GOALS + 1):
        pi = _poisson_pmf(lh, i)
        for j in range(MAX_GOALS + 1):
            pj = _poisson_pmf(la, j)
            total_h, total_a = home_goals + i, away_goals + j
            if total_h > total_a:
                p_home += pi * pj
            elif total_h == total_a:
                p_draw += pi * pj
            else:
                p_away += pi * pj
    z = p_home + p_draw + p_away  # truncation renormalisation
    return p_home / z, p_draw / z, p_away / z


@dataclass(frozen=True)
class _Incident:
    minute: float
    kind: EventKind
    team: str  # "HOME" | "AWAY"


@dataclass
class _Book:
    name: str
    book_id: int
    margin: float  # total overround, e.g. 0.06
    bias: float  # persistent home-lean in probability space
    noise: float  # per-update prob noise sigma
    update_period_s: float
    next_update_s: float = 0.0


class SimulatedFeed(FeedAdapter):
    """Replayable single-match feed. ``seed`` fully determines the output."""

    def __init__(
        self,
        seed: int = 7,
        fixture_id: int = 500001,
        home: str = "Home FC",
        away: str = "Away FC",
        lambda_home: float = 1.5,
        lambda_away: float = 1.1,
        n_books: int = 5,
        tick_seconds: float = 5.0,
        kickoff_ts_ms: int = 1_780_000_000_000,
    ) -> None:
        self.rng = random.Random(seed)
        self.seed = seed
        self.fixture_id = fixture_id
        self.home, self.away = home, away
        self.base_lambda_home, self.base_lambda_away = lambda_home, lambda_away
        self.tick_seconds = tick_seconds
        self.kickoff_ts_ms = kickoff_ts_ms
        self._closed = False

        self._books = [
            _Book(
                name=f"book{i + 1}",
                book_id=100 + i,
                margin=self.rng.uniform(0.04, 0.08),
                bias=self.rng.uniform(-0.01, 0.01),
                noise=self.rng.uniform(0.004, 0.012),
                update_period_s=self.rng.uniform(10.0, 25.0),
            )
            for i in range(n_books)
        ]
        self._incidents = self._sample_incidents()
        self._msg_seq = 0

    # -- scenario sampling --------------------------------------------------

    def _sample_incidents(self) -> list[_Incident]:
        """Pre-sample goals and red cards for the whole match (seeded)."""
        incidents: list[_Incident] = []
        for team, lam in (("HOME", self.base_lambda_home), ("AWAY", self.base_lambda_away)):
            t = 0.0
            while True:
                t += self.rng.expovariate(lam / MATCH_MINUTES)
                if t >= MATCH_MINUTES:
                    break
                incidents.append(_Incident(minute=t, kind=EventKind.GOAL, team=team))
        if self.rng.random() < 0.20:  # ~1 in 5 matches sees a red card
            incidents.append(
                _Incident(
                    minute=self.rng.uniform(15, 85),
                    kind=EventKind.RED_CARD,
                    team="HOME" if self.rng.random() < 0.5 else "AWAY",
                )
            )
        return sorted(incidents, key=lambda e: e.minute)

    # -- helpers ------------------------------------------------------------

    def _ts(self, match_seconds: float) -> int:
        return self.kickoff_ts_ms + int(match_seconds * 1000)

    def true_probabilities(
        self,
        home_goals: int,
        away_goals: int,
        minute: float,
        home_reds: int,
        away_reds: int,
    ) -> tuple[float, float, float]:
        lh, la = self.base_lambda_home, self.base_lambda_away
        if home_reds:  # a red card is worth roughly -35% own xG, +25% opponent xG
            lh *= 0.65**home_reds
            la *= 1.25**home_reds
        if away_reds:
            la *= 0.65**away_reds
            lh *= 1.25**away_reds
        return match_odds_probabilities(
            home_goals, away_goals, MATCH_MINUTES - minute, lh, la
        )

    def _book_tick(
        self, book: _Book, probs: tuple[float, float, float], match_seconds: float
    ) -> OddsTick:
        # Perturb true probs with per-book bias + noise, renormalise,
        # then apply the book's margin multiplicatively (classic vig).
        raw = [
            max(0.005, probs[0] + book.bias + self.rng.gauss(0.0, book.noise)),
            max(0.005, probs[1] + self.rng.gauss(0.0, book.noise)),
            max(0.005, probs[2] - book.bias + self.rng.gauss(0.0, book.noise)),
        ]
        z = sum(raw)
        quoted = [(p / z) * (1.0 + book.margin) for p in raw]
        prices = tuple(round(1.0 / q, 3) for q in quoted)
        self._msg_seq += 1
        return OddsTick(
            fixture_id=self.fixture_id,
            message_id=f"sim-{self.seed}-{self._msg_seq}",
            ts=self._ts(match_seconds),
            bookmaker=book.name,
            bookmaker_id=book.book_id,
            market=Market.MATCH_ODDS,
            outcomes=MATCH_ODDS_OUTCOMES,
            prices=prices,
            in_running=True,
        )

    # -- FeedAdapter ---------------------------------------------------------

    def stream(self) -> Iterator[FeedItem]:  # noqa: C901 — single replay loop
        fid = self.fixture_id
        hg = ag = hr = ar = 0
        pending = list(self._incidents)

        yield MatchEvent(fid, self._ts(0), EventKind.KICK_OFF)
        yield ScoreTick(fid, self._ts(0), 0, MatchPhase.FIRST_HALF, 0, 0)

        t = 0.0
        while t < MATCH_MINUTES * 60 and not self._closed:
            t += self.tick_seconds
            minute = t / 60.0

            # 1) fire any incidents whose time has come
            while pending and pending[0].minute * 60 <= t:
                inc = pending.pop(0)
                ts = self._ts(inc.minute * 60)
                if inc.kind == EventKind.GOAL:
                    if inc.team == "HOME":
                        hg += 1
                    else:
                        ag += 1
                    yield MatchEvent(fid, ts, EventKind.GOAL, inc.team, f"{hg}-{ag}")
                else:
                    if inc.team == "HOME":
                        hr += 1
                    else:
                        ar += 1
                    yield MatchEvent(fid, ts, EventKind.RED_CARD, inc.team)
                phase = MatchPhase.FIRST_HALF if minute <= 45 else MatchPhase.SECOND_HALF
                yield ScoreTick(fid, ts, int(t), phase, hg, ag, hr, ar)

            # 2) books that are due re-quote around the current true price
            probs = self.true_probabilities(hg, ag, minute, hr, ar)
            for book in self._books:
                if t >= book.next_update_s:
                    book.next_update_s = t + book.update_period_s * self.rng.uniform(
                        0.7, 1.3
                    )
                    yield self._book_tick(book, probs, t)

        yield ScoreTick(
            fid, self._ts(t), int(t), MatchPhase.FINISHED, hg, ag, hr, ar
        )
        yield MatchEvent(fid, self._ts(t), EventKind.FULL_TIME, detail=f"{hg}-{ag}")

    def final_score(self) -> tuple[int, int]:
        """Final score implied by the pre-sampled incidents (for settlement)."""
        hg = sum(1 for i in self._incidents if i.kind == EventKind.GOAL and i.team == "HOME")
        ag = sum(1 for i in self._incidents if i.kind == EventKind.GOAL and i.team == "AWAY")
        return hg, ag

    def winning_outcome(self) -> str:
        hg, ag = self.final_score()
        return "HOME" if hg > ag else ("AWAY" if ag > hg else "DRAW")

    def close(self) -> None:
        self._closed = True
