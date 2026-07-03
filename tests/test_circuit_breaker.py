"""Circuit breaker: every trip condition and the re-arm handshake."""

from odds_mm.mm import BreakerState, CircuitBreaker
from odds_mm.types import EventKind, MatchEvent, MatchPhase, ScoreTick


def make_breaker() -> CircuitBreaker:
    return CircuitBreaker(stale_after_ms=20_000, max_latency_ms=5_000, cooldown_ms=15_000)


def score(ts, hg, ag, hr=0, ar=0):
    return ScoreTick(1, ts, ts // 1000, MatchPhase.FIRST_HALF, hg, ag, hr, ar)


def test_starts_active():
    b = make_breaker()
    b.on_feed_item(item_ts=1_000, now_ms=1_000)
    assert b.can_quote(1_000)


def test_goal_event_trips():
    b = make_breaker()
    b.on_feed_item(1_000, 1_000)
    b.on_event(MatchEvent(1, 2_000, EventKind.GOAL, "HOME"), now_ms=2_000)
    assert b.state == BreakerState.TRIPPED
    assert not b.can_quote(2_001)
    assert "GOAL" in b.reason


def test_red_card_trips():
    b = make_breaker()
    b.on_event(MatchEvent(1, 2_000, EventKind.RED_CARD, "AWAY"), now_ms=2_000)
    assert not b.can_quote(2_001)


def test_kickoff_does_not_trip():
    b = make_breaker()
    b.on_event(MatchEvent(1, 1_000, EventKind.KICK_OFF), now_ms=1_000)
    assert b.can_quote(1_001)


def test_score_delta_trips_even_without_event():
    """Defence in depth: missed GOAL message, score channel still catches it."""
    b = make_breaker()
    b.on_score(score(1_000, 0, 0), now_ms=1_000)
    assert b.can_quote(1_001)
    b.on_score(score(9_000, 1, 0), now_ms=9_000)
    assert b.state == BreakerState.TRIPPED


def test_red_card_count_change_on_score_channel_trips():
    b = make_breaker()
    b.on_score(score(1_000, 0, 0), now_ms=1_000)
    b.on_score(score(5_000, 0, 0, hr=1), now_ms=5_000)
    assert b.state == BreakerState.TRIPPED


def test_stale_feed_trips_via_watchdog():
    b = make_breaker()
    b.on_feed_item(1_000, 1_000)
    assert b.can_quote(10_000)  # 9s silence: fine
    assert not b.can_quote(25_000)  # 24s silence: stale
    assert "stale" in b.reason


def test_latency_trips():
    b = make_breaker()
    b.on_feed_item(item_ts=1_000, now_ms=10_000)  # 9s transport lag
    assert b.state == BreakerState.TRIPPED
    assert "latency" in b.reason


def test_rearm_requires_cooldown_and_fresh_data():
    b = make_breaker()
    b.on_feed_item(1_000, 1_000)
    b.on_event(MatchEvent(1, 2_000, EventKind.GOAL, "HOME"), now_ms=2_000)

    # fresh data arrives immediately, but cooldown not yet elapsed
    b.on_feed_item(3_000, 3_000)
    assert not b.can_quote(10_000)

    # cooldown elapsed AND fresh data seen -> re-arm
    b.on_feed_item(16_500, 16_500)
    assert b.can_quote(17_100)
    assert b.state == BreakerState.ACTIVE


def test_no_rearm_without_fresh_data():
    b = make_breaker()
    b.on_feed_item(1_000, 1_000)
    b.trip(2_000, "manual")
    # far beyond cooldown, but no data since the trip -> stay safe
    assert not b.can_quote(60_000)


def test_retrip_extends_cooldown():
    b = make_breaker()
    b.on_feed_item(1_000, 1_000)
    b.trip(2_000, "first")
    b.on_feed_item(10_000, 10_000)
    b.trip(10_000, "second")
    b.on_feed_item(12_000, 12_000)
    assert not b.can_quote(17_500)  # 15s after FIRST trip is not enough
    b.on_feed_item(24_900, 24_900)
    assert b.can_quote(25_100)  # 15s after the second trip


def test_trips_are_recorded():
    b = make_breaker()
    b.on_event(MatchEvent(1, 2_000, EventKind.GOAL, "HOME"), now_ms=2_000)
    status = b.status()
    assert status["trip_count"] == 1
    assert status["state"] == "TRIPPED"
