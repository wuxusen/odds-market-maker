"""Abstract feed adapter interface.

A feed produces a single, time-ordered stream of heterogeneous items
(:class:`OddsTick`, :class:`ScoreTick`, :class:`MatchEvent`). The market
making engine is feed-agnostic: swapping the deterministic simulator for the
live TxLINE adapter requires no engine changes.
"""

from __future__ import annotations

import abc
from typing import Iterator, Union

from ..types import MatchEvent, OddsTick, ScoreTick

FeedItem = Union[OddsTick, ScoreTick, MatchEvent]


class FeedAdapter(abc.ABC):
    """Source of normalised in-play market data for one or more fixtures."""

    @abc.abstractmethod
    def stream(self) -> Iterator[FeedItem]:
        """Yield feed items in non-decreasing ``ts`` order.

        Implementations must be resumable-friendly: raising is allowed on
        transport failure, and callers treat a silent stream as *stale* —
        the circuit breaker watchdog handles gaps, so adapters should not
        fabricate data to fill them.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Release transport resources. Idempotent."""
