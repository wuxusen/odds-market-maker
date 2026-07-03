"""Simulator determinism, TxLINE payload parsing, engine quoting and the
headless end-to-end demo."""

import pytest

from odds_mm.demo import run
from odds_mm.feeds import SimulatedFeed, TxLineAdapter
from odds_mm.feeds.simulated import match_odds_probabilities
from odds_mm.mm import CircuitBreaker, InventoryBook, MarketMaker, MMConfig
from odds_mm.pricing import ConsensusPricer
from odds_mm.types import EventKind, MatchEvent, OddsTick, Side


# -- simulator ---------------------------------------------------------------


def test_feed_is_deterministic_for_a_seed():
    a = [repr(i) for i in SimulatedFeed(seed=11).stream()]
    b = [repr(i) for i in SimulatedFeed(seed=11).stream()]
    assert a == b


def test_different_seeds_differ():
    a = [repr(i) for i in SimulatedFeed(seed=1).stream()]
    b = [repr(i) for i in SimulatedFeed(seed=2).stream()]
    assert a != b


def test_feed_timestamps_monotonic_and_events_consistent():
    items = list(SimulatedFeed(seed=5).stream())
    ts = [i.ts for i in items]
    assert ts == sorted(ts)
    goals = [i for i in items if isinstance(i, MatchEvent) and i.kind == EventKind.GOAL]
    hg, ag = SimulatedFeed(seed=5).final_score()
    assert len(goals) == hg + ag


def test_inplay_model_probabilities():
    # level game, plenty of time: everything possible
    h, d, a = match_odds_probabilities(0, 0, 90, 1.5, 1.1)
    assert h + d + a == pytest.approx(1.0)
    assert h > a  # stronger home team
    # 2-0 up with a minute left: home is near-certain
    h, d, a = match_odds_probabilities(2, 0, 1, 1.5, 1.1)
    assert h > 0.99
    # goals shift probability mass the right way
    h0, _, _ = match_odds_probabilities(0, 0, 45, 1.5, 1.1)
    h1, _, _ = match_odds_probabilities(1, 0, 45, 1.5, 1.1)
    assert h1 > h0


# -- txline payload parsing ----------------------------------------------------


def test_txline_payload_normalisation():
    payload = {
        "FixtureId": 987654,
        "MessageId": "abc-123",
        "Ts": 1780000000123,
        "Bookmaker": "SomeBook",
        "BookmakerId": 42,
        "SuperOddsType": "1x2",
        "InRunning": True,
        "MarketPeriod": "FT",
        "PriceNames": ["HOME", "DRAW", "AWAY"],
        "Prices": [1900, 3600, 3800],  # decimal odds x1000
        "Pct": ["52.632", "27.778", "26.316"],
    }
    tick = TxLineAdapter.parse_odds_payload(payload)
    assert isinstance(tick, OddsTick)
    assert tick.prices == (1.9, 3.6, 3.8)
    assert tick.bookmaker_id == 42
    assert tick.in_running


def test_txline_skips_unknown_markets_and_bad_prices():
    base = {
        "FixtureId": 1, "MessageId": "m", "Ts": 1, "Bookmaker": "b",
        "BookmakerId": 1, "InRunning": True,
    }
    assert TxLineAdapter.parse_odds_payload({**base, "SuperOddsType": "ahc", "Prices": [1900, 2000]}) is None
    assert TxLineAdapter.parse_odds_payload({**base, "SuperOddsType": "1x2", "Prices": [1900, 3600]}) is None
    assert TxLineAdapter.parse_odds_payload({**base, "SuperOddsType": "1x2", "Prices": [900, 3600, 3800]}) is None


def test_txline_requires_credentials():
    adapter = TxLineAdapter(fixture_id=1)
    with pytest.raises(RuntimeError):
        next(adapter.stream())


# -- engine --------------------------------------------------------------------


def make_stack():
    inv = InventoryBook()
    breaker = CircuitBreaker()
    maker = MarketMaker(MMConfig(), inv, breaker)
    pricer = ConsensusPricer()
    return inv, breaker, maker, pricer


def feed_books(pricer, breaker, ts=1_000):
    from odds_mm.types import Market, MATCH_ODDS_OUTCOMES

    for b, prices in enumerate(
        [(1.90, 3.60, 3.80), (1.95, 3.50, 3.85), (1.92, 3.55, 3.82), (1.98, 3.45, 3.90)],
        start=1,
    ):
        tick = OddsTick(1, f"m{b}", ts, f"book{b}", b, Market.MATCH_ODDS,
                        MATCH_ODDS_OUTCOMES, prices, True)
        breaker.on_feed_item(tick.ts, tick.ts)
        pricer.on_tick(tick)


def test_engine_quotes_two_sided_around_fair():
    inv, breaker, maker, pricer = make_stack()
    feed_books(pricer, breaker)
    fair = pricer.fair_price(1_000)
    qs = maker.quote(fair, 1_000)
    assert not qs.halted
    for outcome in ("HOME", "DRAW", "AWAY"):
        bid, ask = qs.get(outcome, Side.BID), qs.get(outcome, Side.ASK)
        assert bid and ask
        p = qs.fair[outcome]
        assert bid.price < p < ask.price
        assert bid.decimal_odds > 1.0 and ask.decimal_odds > 1.0


def test_engine_halts_when_breaker_tripped():
    inv, breaker, maker, pricer = make_stack()
    feed_books(pricer, breaker)
    breaker.trip(1_000, "test trip")
    qs = maker.quote(pricer.fair_price(1_000), 1_000)
    assert qs.halted and qs.quotes == []
    assert "breaker" in qs.halt_reason


def test_engine_halts_on_low_confidence():
    inv, breaker, maker, pricer = make_stack()
    feed_books(pricer, breaker)
    maker.config.min_confidence = 1.01  # unreachable on purpose
    qs = maker.quote(pricer.fair_price(1_000), 1_000)
    assert qs.halted and "confidence" in qs.halt_reason


def test_inventory_skew_moves_quotes():
    from odds_mm.types import Fill, Market

    inv, breaker, maker, pricer = make_stack()
    feed_books(pricer, breaker)
    fair = pricer.fair_price(1_000)
    before = maker.quote(fair, 1_000)
    inv.on_fill(Fill(1, 1, Market.MATCH_ODDS, "HOME", Side.BID, 0.5, 150, ts=1_000))  # big long
    after = maker.quote(fair, 1_001)
    # long inventory pushes both sides DOWN so takers buy from us
    assert after.get("HOME", Side.ASK).price < before.get("HOME", Side.ASK).price
    assert after.get("HOME", Side.BID).price < before.get("HOME", Side.BID).price


# -- end to end ------------------------------------------------------------------


def test_headless_demo_runs_and_balances():
    final = run(seed=7, speed=0, dashboard=False, quiet=True)
    assert final["n_fills"] > 0
    assert final["positions"] == {}  # everything settled
    assert final["audit_len"] == final["n_fills"] + 1  # fills + settlement


def test_demo_is_reproducible():
    a = run(seed=13, speed=0, dashboard=False, quiet=True)
    b = run(seed=13, speed=0, dashboard=False, quiet=True)
    assert a == b


def test_demo_final_snapshot_includes_breaker_status():
    final = run(seed=13, speed=0, dashboard=False, quiet=True)
    assert "breaker" in final
    assert final["breaker"]["state"] in ("ACTIVE", "TRIPPED")
    assert final["breaker"]["trip_count"] >= 0


def test_demo_state_snapshot_has_exposure_and_timeline():
    """The dashboard needs exposure gauges + a narrative log; both must be
    present on every published state, not just the final one."""
    import odds_mm.demo as demo_mod

    captured = []
    orig_build_state = demo_mod._build_state

    def spy(*args, **kwargs):
        state = orig_build_state(*args, **kwargs)
        captured.append(state)
        return state

    demo_mod._build_state = spy
    try:
        run(seed=44, speed=0, dashboard=False, quiet=True)
    finally:
        demo_mod._build_state = orig_build_state

    assert captured, "no state was ever published"
    mid = captured[len(captured) // 2]
    for key in ("exposure", "log", "positions", "equity_curve"):
        assert key in mid
    assert set(mid["exposure"]) == {"fixture", "fixture_max", "total", "total_max"}
    # the kick-off entry is always first and always present
    assert captured[0]["log"][0]["text"].startswith("kick-off")
    # incidents accumulate narrative entries as the match progresses
    assert len(captured[-1]["log"]) >= len(captured[0]["log"])
