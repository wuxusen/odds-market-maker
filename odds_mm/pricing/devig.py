"""Margin removal (de-vig) for bookmaker odds.

Bookmaker decimal odds ``o_i`` imply raw probabilities ``q_i = 1/o_i`` whose
sum exceeds 1 by the *overround* (the margin, or "vig"). Recovering the
book's underlying probability estimate requires removing that margin. Two
standard methods are implemented:

* **Multiplicative** — ``p_i = q_i / sum(q)``. Assumes the margin is applied
  proportionally to every outcome. Simple, but known to under-correct
  favourite–longshot bias.
* **Power** — find ``k >= 1`` such that ``sum(q_i ** k) = 1``, then
  ``p_i = q_i ** k``. Loads relatively more margin onto longshots, which
  matches observed bookmaker behaviour better on markets with skewed prices.

Both are pure functions on sequences of decimal odds; both return
probabilities that sum to 1 (within float tolerance).
"""

from __future__ import annotations

from typing import Sequence


def overround(odds: Sequence[float]) -> float:
    """Total margin: ``sum(1/o) - 1``. 0.06 means a 6% vig."""
    _validate(odds)
    return sum(1.0 / o for o in odds) - 1.0


def devig_multiplicative(odds: Sequence[float]) -> list[float]:
    """Proportional margin removal: normalise raw implied probabilities."""
    _validate(odds)
    raw = [1.0 / o for o in odds]
    z = sum(raw)
    return [q / z for q in raw]


def devig_power(
    odds: Sequence[float], tol: float = 1e-12, max_iter: int = 200
) -> list[float]:
    """Power-method margin removal.

    Solves ``sum((1/o_i) ** k) = 1`` for ``k`` by bisection. For a fair book
    (overround 0) the solution is ``k = 1`` and the raw probabilities are
    returned unchanged. ``f(k) = sum(q_i^k)`` is strictly decreasing in ``k``
    for ``q_i < 1``, so bisection on ``[1, hi]`` converges unconditionally.
    """
    _validate(odds)
    raw = [1.0 / o for o in odds]
    if any(q >= 1.0 for q in raw):
        # An odds-on-certainty (o <= 1) breaks the power transform;
        # fall back to proportional normalisation.
        return devig_multiplicative(odds)

    def f(k: float) -> float:
        return sum(q**k for q in raw) - 1.0

    if abs(f(1.0)) <= tol:
        return raw
    # f is strictly decreasing in k; bracket the root then bisect.
    if f(1.0) > 0:  # overround > 0 -> k > 1
        lo, hi = 1.0, 2.0
        while f(hi) > 0 and hi < 1e6:
            hi *= 2.0
    else:  # underround (rare) -> k < 1; f -> n-1 > 0 as k -> 0
        lo, hi = 0.5, 1.0
        while f(lo) < 0 and lo > 1e-9:
            lo *= 0.5
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        v = f(mid)
        if abs(v) <= tol:
            break
        if v > 0:
            lo = mid
        else:
            hi = mid
    k = 0.5 * (lo + hi)
    probs = [q**k for q in raw]
    z = sum(probs)  # mop up residual tolerance
    return [p / z for p in probs]


def _validate(odds: Sequence[float]) -> None:
    if len(odds) < 2:
        raise ValueError("need at least two outcomes")
    if any(o <= 1.0 for o in odds):
        # Decimal odds of 1.0 or below imply p >= 1: reject upstream.
        if any(o <= 0 for o in odds):
            raise ValueError(f"non-positive odds: {odds}")
