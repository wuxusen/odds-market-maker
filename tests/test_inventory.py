"""Inventory: exposure math, hard caps and skew direction."""

import pytest

from odds_mm.mm import InventoryBook
from odds_mm.types import Fill, Market, Side


def fill(outcome: str, side: Side, price: float, qty: float, fid: int = 1) -> Fill:
    return Fill(0, fid, Market.MATCH_ODDS, outcome, side, price, qty, ts=0)


def make_book(**kw) -> InventoryBook:
    defaults = dict(
        max_fixture_exposure=100.0,
        max_total_exposure=200.0,
        max_outcome_qty=50.0,
        skew_intensity=0.5,
    )
    defaults.update(kw)
    return InventoryBook(**defaults)


def test_long_position_worst_case_is_premium_paid():
    inv = make_book()
    inv.on_fill(fill("HOME", Side.BID, 0.50, 10))  # bought 10 @ 0.50
    # HOME loses -> we lose the 5.0 premium
    assert inv.fixture_exposure(1) == pytest.approx(5.0)


def test_short_position_worst_case_is_payout():
    inv = make_book()
    inv.on_fill(fill("HOME", Side.ASK, 0.40, 10))  # sold 10 @ 0.40
    # HOME wins -> pay 10, collected 4 -> lose 6
    assert inv.fixture_exposure(1) == pytest.approx(6.0)


def test_offsetting_legs_reduce_exposure():
    """Short HOME + short AWAY hedge each other: only one can win."""
    inv = make_book()
    inv.on_fill(fill("HOME", Side.ASK, 0.50, 10))
    only_home = inv.fixture_exposure(1)
    inv.on_fill(fill("AWAY", Side.ASK, 0.30, 10))
    both = inv.fixture_exposure(1)
    # worst case: HOME wins -> lose 5 on HOME, keep 3 from AWAY -> 2 net
    assert both == pytest.approx(only_home - 3.0)


def test_skew_direction():
    inv = make_book()
    assert inv.skew(1, "HOME", 0.02) == 0.0  # flat -> no skew
    inv.on_fill(fill("HOME", Side.BID, 0.50, 25))  # long
    assert inv.skew(1, "HOME", 0.02) < 0  # long -> shift mid DOWN to sell
    inv.on_fill(fill("HOME", Side.ASK, 0.50, 50))  # now net short 25
    assert inv.skew(1, "HOME", 0.02) > 0  # short -> shift mid UP to buy


def test_skew_magnitude_scales_with_position():
    inv = make_book()
    inv.on_fill(fill("HOME", Side.BID, 0.50, 10))
    small = abs(inv.skew(1, "HOME", 0.02))
    inv.on_fill(fill("HOME", Side.BID, 0.50, 30))
    large = abs(inv.skew(1, "HOME", 0.02))
    assert large > small


def test_reducing_side_always_allowed():
    inv = make_book(max_outcome_qty=10.0)
    inv.on_fill(fill("HOME", Side.BID, 0.50, 10))  # at the qty cap
    assert inv.allowed_size(1, "HOME", Side.BID, 20.0) == 0.0  # can't extend
    assert inv.allowed_size(1, "HOME", Side.ASK, 20.0) == 10.0  # reduce to flat


def test_size_shrinks_as_fixture_exposure_grows():
    inv = make_book(max_fixture_exposure=10.0)
    full = inv.allowed_size(1, "HOME", Side.BID, 20.0)
    inv.on_fill(fill("HOME", Side.BID, 0.50, 10))  # exposure 5 of 10
    half = inv.allowed_size(1, "HOME", Side.BID, 20.0)
    assert full == pytest.approx(20.0)
    assert half == pytest.approx(10.0)


def test_hard_cap_zeroes_extending_side():
    inv = make_book(max_fixture_exposure=5.0)
    inv.on_fill(fill("HOME", Side.BID, 0.50, 10))  # exposure 5 == cap
    assert inv.allowed_size(1, "HOME", Side.BID, 20.0) == 0.0
    assert inv.allowed_size(1, "DRAW", Side.BID, 20.0) == 0.0  # same fixture


def test_global_cap_spans_fixtures():
    inv = make_book(max_fixture_exposure=100.0, max_total_exposure=6.0)
    inv.on_fill(fill("HOME", Side.BID, 0.60, 10, fid=1))  # 6.0 exposure
    assert inv.allowed_size(2, "HOME", Side.BID, 20.0) == 0.0


def test_realized_pnl_on_round_trip():
    inv = make_book()
    inv.on_fill(fill("HOME", Side.BID, 0.40, 10))
    inv.on_fill(fill("HOME", Side.ASK, 0.50, 10))
    pos = inv.position(1, "HOME")
    assert pos.qty == 0.0
    assert pos.realized_pnl == pytest.approx(1.0)  # 10 * (0.50 - 0.40)


def test_qty_cap_is_never_overshootable():
    """A full fill at the allowed size can never breach max_outcome_qty."""
    inv = make_book(max_outcome_qty=50.0)
    inv.on_fill(fill("HOME", Side.BID, 0.10, 45))  # low price: exposure tiny
    allowed = inv.allowed_size(1, "HOME", Side.BID, 20.0)
    assert 0 < allowed <= 5.0
