"""Consensus pricer: aggregation, staleness and confidence behaviour."""

from odds_mm.pricing import ConsensusPricer
from odds_mm.types import Market, MATCH_ODDS_OUTCOMES, OddsTick


def tick(book_id: int, prices, ts: int) -> OddsTick:
    return OddsTick(
        fixture_id=1,
        message_id=f"m{book_id}-{ts}",
        ts=ts,
        bookmaker=f"book{book_id}",
        bookmaker_id=book_id,
        market=Market.MATCH_ODDS,
        outcomes=MATCH_ODDS_OUTCOMES,
        prices=tuple(prices),
        in_running=True,
    )


def test_requires_min_books():
    p = ConsensusPricer(min_books=2)
    p.on_tick(tick(1, [1.9, 3.6, 3.8], 1000))
    assert p.fair_price(now_ms=1500) is None
    p.on_tick(tick(2, [1.95, 3.5, 3.9], 1200))
    assert p.fair_price(now_ms=1500) is not None


def test_consensus_sums_to_one_and_tracks_books():
    p = ConsensusPricer()
    p.on_tick(tick(1, [1.9, 3.6, 3.8], 1000))
    p.on_tick(tick(2, [2.0, 3.4, 3.7], 1000))
    p.on_tick(tick(3, [1.95, 3.5, 3.85], 1000))
    fp = p.fair_price(now_ms=1000)
    assert fp is not None
    assert sum(fp.probabilities) - 1.0 < 1e-9
    assert fp.n_books == 3
    assert fp.probabilities[0] > 0.45  # de-vigged favourite around 1/1.95


def test_stale_books_are_dropped():
    p = ConsensusPricer(min_books=2, max_age_ms=10_000)
    p.on_tick(tick(1, [1.9, 3.6, 3.8], 1_000))
    p.on_tick(tick(2, [2.0, 3.4, 3.7], 2_000))
    assert p.fair_price(now_ms=5_000) is not None
    # 60 seconds later both ticks are stale -> no price
    assert p.fair_price(now_ms=65_000) is None


def test_disagreement_lowers_confidence():
    agree = ConsensusPricer()
    for b in (1, 2, 3, 4):
        agree.on_tick(tick(b, [1.95, 3.5, 3.85], 1000))
    disagree = ConsensusPricer()
    disagree.on_tick(tick(1, [1.60, 4.2, 5.0], 1000))
    disagree.on_tick(tick(2, [2.40, 3.1, 3.2], 1000))
    disagree.on_tick(tick(3, [1.95, 3.5, 3.85], 1000))
    disagree.on_tick(tick(4, [2.80, 3.4, 2.6], 1000))
    fa = agree.fair_price(1000)
    fd = disagree.fair_price(1000)
    assert fa.confidence > fd.confidence


def test_more_books_raise_confidence():
    two = ConsensusPricer()
    four = ConsensusPricer()
    for b in (1, 2):
        two.on_tick(tick(b, [1.95, 3.5, 3.85], 1000))
    for b in (1, 2, 3, 4):
        four.on_tick(tick(b, [1.95, 3.5, 3.85], 1000))
    assert four.fair_price(1000).confidence > two.fair_price(1000).confidence
