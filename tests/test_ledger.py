"""Ledger accounting: cash flows, marks, settlement and audit chain."""

import pytest

from odds_mm.ledger import PaperLedger
from odds_mm.types import Fill, Market, Side


def fill(fid_seq: int, outcome: str, side: Side, price: float, qty: float, fixture: int = 1) -> Fill:
    return Fill(fid_seq, fixture, Market.MATCH_ODDS, outcome, side, price, qty, ts=fid_seq * 1000)


def test_sell_collects_premium_buy_pays_it():
    led = PaperLedger(starting_cash=100.0)
    led.record_fill(fill(1, "HOME", Side.ASK, 0.60, 10))  # sell: +6
    assert led.cash == pytest.approx(106.0)
    led.record_fill(fill(2, "DRAW", Side.BID, 0.20, 5))  # buy: -1
    assert led.cash == pytest.approx(105.0)
    assert led.positions[(1, "HOME")] == -10
    assert led.positions[(1, "DRAW")] == 5


def test_equity_marks_positions_at_fair():
    led = PaperLedger(starting_cash=100.0)
    led.record_fill(fill(1, "HOME", Side.BID, 0.50, 10))  # cash 95, +10 HOME
    marks = {(1, "HOME"): 0.70}
    assert led.equity(marks) == pytest.approx(95.0 + 7.0)
    # unknown marks value positions at zero (conservative)
    assert led.equity({}) == pytest.approx(95.0)


def test_settlement_winner_pays_one():
    led = PaperLedger(starting_cash=100.0)
    led.record_fill(fill(1, "HOME", Side.BID, 0.50, 10))  # cash 95
    led.record_fill(fill(2, "AWAY", Side.ASK, 0.30, 10))  # cash 98, short AWAY
    flow = led.settle(1, "HOME", ts=99_000)
    assert flow == pytest.approx(10.0)  # long HOME pays, short AWAY expires
    assert led.cash == pytest.approx(108.0)
    assert led.positions == {}
    assert led.pnl == pytest.approx(8.0)


def test_settlement_loser_side():
    led = PaperLedger(starting_cash=100.0)
    led.record_fill(fill(1, "AWAY", Side.ASK, 0.30, 10))  # short AWAY, cash 103
    led.settle(1, "AWAY", ts=99_000)  # AWAY wins: we pay 10
    assert led.cash == pytest.approx(93.0)
    assert led.pnl == pytest.approx(-7.0)


def test_double_settlement_rejected():
    led = PaperLedger()
    led.record_fill(fill(1, "HOME", Side.BID, 0.5, 1))
    led.settle(1, "HOME", ts=1)
    with pytest.raises(ValueError):
        led.settle(1, "HOME", ts=2)


def test_audit_chain_verifies_and_detects_tampering():
    led = PaperLedger()
    led.record_fill(fill(1, "HOME", Side.BID, 0.50, 10))
    led.record_fill(fill(2, "HOME", Side.ASK, 0.55, 10))
    led.settle(1, "HOME", ts=3_000)
    assert led.verify_audit_chain()
    led.audit[0]["price"] = 0.01  # tamper with history
    assert not led.verify_audit_chain()


def test_audit_records_reserve_anchor_fields():
    led = PaperLedger()
    rec = led.record_fill(fill(1, "HOME", Side.BID, 0.5, 1))
    assert rec["anchor"] == {
        "solana_tx_sig": None,
        "solana_slot": None,
        "merkle_root": None,
    }
    assert len(rec["hash"]) == 64


def test_equity_curve_marks():
    led = PaperLedger(starting_cash=100.0)
    led.mark(1_000, {})
    led.record_fill(fill(1, "HOME", Side.BID, 0.50, 10))
    led.mark(2_000, {(1, "HOME"): 0.50})
    assert led.equity_curve == [(1_000, 100.0), (2_000, 100.0)]
