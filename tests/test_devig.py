"""De-vig math: both methods must recover proper probability vectors."""

import math

import pytest

from odds_mm.pricing.devig import devig_multiplicative, devig_power, overround


FAIR = [2.0, 4.0, 4.0]  # exactly 0.5/0.25/0.25, zero margin
VIGGED = [1.90, 3.60, 3.80]  # typical 1X2 with a few % overround


def test_overround_zero_for_fair_book():
    assert overround(FAIR) == pytest.approx(0.0)


def test_overround_positive_for_real_book():
    assert overround(VIGGED) > 0.02


@pytest.mark.parametrize("fn", [devig_multiplicative, devig_power])
def test_devig_sums_to_one(fn):
    probs = fn(VIGGED)
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)
    assert all(0.0 < p < 1.0 for p in probs)


@pytest.mark.parametrize("fn", [devig_multiplicative, devig_power])
def test_fair_book_unchanged(fn):
    probs = fn(FAIR)
    assert probs == pytest.approx([0.5, 0.25, 0.25], abs=1e-9)


@pytest.mark.parametrize("fn", [devig_multiplicative, devig_power])
def test_order_preserved(fn):
    probs = fn(VIGGED)
    assert probs[0] > probs[1] > probs[2]  # ranking by odds is preserved


def test_multiplicative_known_value():
    # q = [1/2, 1/3], sum = 5/6 -> p = [3/5, 2/5]
    probs = devig_multiplicative([2.0, 3.0])
    assert probs == pytest.approx([0.6, 0.4], abs=1e-12)


def test_power_shaves_longshots_harder_than_multiplicative():
    """The power method's defining property: relatively more margin is
    removed from longshots than from favourites."""
    odds = [1.30, 5.0, 12.0]  # heavy favourite + longshots, with margin
    mult = devig_multiplicative(odds)
    powr = devig_power(odds)
    # Longshot probability must end lower under power than multiplicative.
    assert powr[2] < mult[2]
    # And the favourite correspondingly higher.
    assert powr[0] > mult[0]


def test_power_solves_exponent_equation():
    odds = VIGGED
    raw = [1.0 / o for o in odds]
    probs = devig_power(odds)
    # recover k from the first component and check it fits the others
    k = math.log(probs[0]) / math.log(raw[0])
    for q, p in zip(raw, probs):
        assert q**k == pytest.approx(p, rel=1e-6)
    assert k > 1.0  # positive margin -> k > 1


def test_power_underround_book():
    odds = [2.2, 4.4, 4.4]  # sum(1/o) < 1 (arb book) -> k < 1 branch
    probs = devig_power(odds)
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("fn", [devig_multiplicative, devig_power])
def test_rejects_garbage(fn):
    with pytest.raises(ValueError):
        fn([2.0])
    with pytest.raises(ValueError):
        fn([2.0, -1.0])
