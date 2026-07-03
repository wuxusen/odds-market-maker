"""End-to-end demo: simulated match -> pricing -> market making -> dashboard.

Run:

    python -m odds_mm.demo                 # real-time-ish (60x speed) + dashboard
    python -m odds_mm.demo --speed 0       # as fast as possible
    python -m odds_mm.demo --no-dashboard  # headless, exits at full time
    python -m odds_mm.demo --seed 42       # different (but reproducible) match

The demo wires the deterministic :class:`SimulatedFeed` into the same
pipeline a live TxLINE feed would use. All randomness (match script, book
noise, taker flow) is derived from ``--seed``; the wall clock is only used
for pacing, never for decisions, so every run with the same seed produces
identical fills, PnL and audit chain.
"""

from __future__ import annotations

import argparse
import random
import time
from typing import Optional

from .anchor import AnchorPolicy, LedgerAnchorScheduler, NullAnchor, SolanaAnchor
from .dashboard import DashboardServer, StateStore
from .feeds import SimulatedFeed
from .ledger import PaperLedger
from .mm import BreakerState, CircuitBreaker, InventoryBook, MarketMaker, MMConfig
from .pricing import ConsensusPricer
from .types import (
    EventKind,
    Fill,
    MatchEvent,
    MatchPhase,
    OddsTick,
    QuoteSet,
    ScoreTick,
    Side,
)

NARRATIVE_LOG_SIZE = 16  # entries kept for the dashboard's timeline panel


class TakerFlow:
    """Seeded synthetic order flow that trades against our quotes.

    Takers hold a private value = latent true probability + noise. They lift
    our ask when it is below their value and hit our bid when above — i.e.
    flow is *informed on average*, the worst case for a market maker, which
    is exactly what the spread/skew/breaker stack must survive.
    """

    def __init__(self, seed: int, arrival_prob: float = 0.35) -> None:
        self.rng = random.Random(seed ^ 0x7A6B)
        self.arrival_prob = arrival_prob
        self._next_id = 1

    def generate(
        self, quotes: QuoteSet, true_probs: dict[str, float], ts: int
    ) -> list[Fill]:
        fills: list[Fill] = []
        if quotes.halted:
            return fills
        for outcome, true_p in true_probs.items():
            if self.rng.random() > self.arrival_prob:
                continue
            value = true_p + self.rng.gauss(0.0, 0.02)
            qty = round(self.rng.uniform(1.0, 8.0), 2)
            ask = quotes.get(outcome, Side.ASK)
            bid = quotes.get(outcome, Side.BID)
            if ask and ask.price < value:  # taker buys from us
                fills.append(self._fill(quotes, outcome, Side.ASK, ask.price, min(qty, ask.size), ts))
            elif bid and bid.price > value:  # taker sells to us
                fills.append(self._fill(quotes, outcome, Side.BID, bid.price, min(qty, bid.size), ts))
        return fills

    def _fill(self, qs: QuoteSet, outcome: str, side: Side, price: float, qty: float, ts: int) -> Fill:
        f = Fill(
            fill_id=self._next_id,
            fixture_id=qs.fixture_id,
            market=qs.quotes[0].market,
            outcome=outcome,
            side=side,
            price=price,
            qty=qty,
            ts=ts,
        )
        self._next_id += 1
        return f


def run(
    seed: int = 9,
    speed: float = 60.0,
    port: int = 8765,
    dashboard: bool = True,
    hold_open: bool = True,
    quiet: bool = False,
    anchor_scheduler: Optional[LedgerAnchorScheduler] = None,
) -> dict:
    """Run one full simulated match. Returns the final ledger snapshot.

    ``anchor_scheduler``, if given, is pumped once per feed item with the
    ledger's current chain head; it decides on its own cadence whether an
    attempt is due (see ``odds_mm.anchor.scheduler.AnchorPolicy``) and never
    raises — anchoring is best-effort and must never stall quoting.
    """
    feed = SimulatedFeed(seed=seed)
    pricer = ConsensusPricer(method="power")
    inventory = InventoryBook()
    breaker = CircuitBreaker()
    maker = MarketMaker(MMConfig(), inventory, breaker)
    ledger = PaperLedger(starting_cash=1_000.0)
    takers = TakerFlow(seed=seed)
    store = StateStore()

    server = None
    if dashboard:
        server = DashboardServer(store, port=port)
        server.start()
        print(f"[demo] dashboard: {server.url}")

    score = ScoreTick(feed.fixture_id, feed.kickoff_ts_ms, 0, MatchPhase.NOT_STARTED, 0, 0)
    quotes = QuoteSet(fixture_id=feed.fixture_id, ts=0, halted=True, halt_reason="warmup")
    last_ts = feed.kickoff_ts_ms
    log = (lambda *a: None) if quiet else print
    narrative: list[dict] = []
    prev_breaker_state = breaker.state
    prev_trip_count = 0

    def _note(ts: int, text: str) -> None:
        minute = max(0, (ts - feed.kickoff_ts_ms)) // 60_000
        narrative.append({"minute": int(minute), "text": text})
        del narrative[:-NARRATIVE_LOG_SIZE]

    _note(feed.kickoff_ts_ms, "kick-off — books warming up, no quotes yet")

    for item in feed.stream():
        now = item.ts  # simulated clock — decisions never read the wall clock

        if speed > 0:  # pacing only
            time.sleep(max(0.0, (item.ts - last_ts) / 1000.0 / speed))
        last_ts = item.ts

        # 1) route the item
        if isinstance(item, OddsTick):
            breaker.on_feed_item(item.ts, now)
            pricer.on_tick(item)
        elif isinstance(item, ScoreTick):
            breaker.on_score(item, now)
            score = item
        elif isinstance(item, MatchEvent):
            breaker.on_event(item, now)
            if item.kind in (EventKind.GOAL, EventKind.RED_CARD):
                minute_now = (item.ts - feed.kickoff_ts_ms) // 60_000
                log(f"[{minute_now:>3}'] !! {item.kind.value} {item.team or ''} {item.detail}")
                _note(item.ts, f"{item.kind.value} {item.team or ''} {item.detail}".strip())

        # 1b) narrate circuit-breaker transitions (trip / re-arm) for the
        # dashboard timeline — this is the story the whole system exists to
        # tell: quote normally, go safe on any anomaly, resume only once
        # cool-down + fresh data both say it is safe to.
        if breaker.state == BreakerState.TRIPPED and len(breaker.trips) > prev_trip_count:
            _note(now, f"circuit breaker TRIPPED — {breaker.reason}")
        elif prev_breaker_state == BreakerState.TRIPPED and breaker.state == BreakerState.ACTIVE:
            _note(now, "circuit breaker re-armed — quoting resumed")
        prev_breaker_state = breaker.state
        prev_trip_count = len(breaker.trips)

        # 2) reprice + requote
        fair = pricer.fair_price(now)
        if fair is None:
            quotes = QuoteSet(feed.fixture_id, now, halted=True, halt_reason="no consensus")
        else:
            quotes = maker.quote(fair, now)

        # 3) synthetic taker flow against our quotes
        minute = score.clock_seconds / 60.0
        true_probs = dict(
            zip(
                ("HOME", "DRAW", "AWAY"),
                feed.true_probabilities(
                    score.home_goals, score.away_goals, minute,
                    score.home_red_cards, score.away_red_cards,
                ),
            )
        )
        for fill in takers.generate(quotes, true_probs, now):
            inventory.on_fill(fill)
            ledger.record_fill(fill)

        # 4) mark + publish state
        marks = {}
        if fair is not None:
            marks = {
                (feed.fixture_id, oc): p
                for oc, p in zip(fair.outcomes, fair.probabilities)
            }
        ledger.mark(now, marks)

        # 5) best-effort chain-head anchoring — never allowed to affect
        # quoting/fills; a failure here is logged and retried next window.
        if anchor_scheduler is not None:
            try:
                anchor_result = anchor_scheduler.maybe_anchor(ledger, now)
            except Exception as exc:  # belt-and-braces: see module doctrine
                anchor_result = None
                log(f"[demo] anchor scheduler error (ignored, market making continues): {exc}")
            if anchor_result is not None:
                if anchor_result.ok:
                    log(f"[demo] anchored chain head {anchor_result.head_hash[:12]}... -> tx {anchor_result.tx_sig}")
                else:
                    log(f"[demo] anchor attempt failed (ignored, will retry): {anchor_result.error}")

        store.update(
            _build_state(feed, score, quotes, fair, breaker, inventory, ledger, narrative, anchor_scheduler=anchor_scheduler)
        )

    # settlement
    winner = feed.winning_outcome()
    ledger.settle(feed.fixture_id, winner, last_ts)
    if anchor_scheduler is not None:
        try:
            anchor_scheduler.maybe_anchor(ledger, last_ts)
        except Exception as exc:
            log(f"[demo] final anchor attempt error (ignored): {exc}")
    final = ledger.snapshot()
    final["breaker"] = breaker.status()
    _note(last_ts, f"full time {feed.final_score()[0]}-{feed.final_score()[1]} — settled ({winner})")
    store.update(
        _build_state(feed, score, quotes, None, breaker, inventory, ledger, narrative, final=True, anchor_scheduler=anchor_scheduler)
    )

    log(
        f"[demo] full time {feed.final_score()[0]}-{feed.final_score()[1]} "
        f"({winner}) | fills={final['n_fills']} pnl={final['pnl_realized']:+.2f} "
        f"| breaker trips={len(breaker.trips)} | audit ok={ledger.verify_audit_chain()}"
    )

    if server and hold_open:
        print("[demo] match finished — dashboard still serving, Ctrl+C to exit")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    if server:
        server.stop()
    return final


def _build_state(feed, score, quotes, fair, breaker, inventory, ledger, narrative=(), final=False, anchor_scheduler=None) -> dict:
    by_outcome: dict[str, dict] = {}
    for q in quotes.quotes:
        d = by_outcome.setdefault(q.outcome, {"outcome": q.outcome, "fair": quotes.fair.get(q.outcome)})
        if q.side == Side.BID:
            d["bid"], d["bid_size"] = q.price, q.size
        else:
            d["ask"], d["ask_size"] = q.price, q.size
    marks = {}
    if fair is not None:
        marks = {(feed.fixture_id, oc): p for oc, p in zip(fair.outcomes, fair.probabilities)}
    phase = "SETTLED" if final else score.phase.value
    fixture_exp = inventory.fixture_exposure(feed.fixture_id)
    total_exp = inventory.total_exposure()
    return {
        "status": "final" if final else "running",
        "match": {
            "home": feed.home,
            "away": feed.away,
            "score": f"{score.home_goals}-{score.away_goals}",
            "reds": f"{score.home_red_cards}-{score.away_red_cards}",
            "phase": phase,
            "clock": f"{score.clock_seconds // 60}'",
        },
        "breaker": breaker.status(),
        "halt_reason": quotes.halt_reason if quotes.halted else "",
        "quotes": list(by_outcome.values()),
        "fair": {
            "probs": dict(zip(fair.outcomes, [round(p, 4) for p in fair.probabilities])) if fair else {},
            "confidence": fair.confidence if fair else None,
            "n_books": fair.n_books if fair else 0,
        },
        "positions": inventory.snapshot(),
        "exposure": {
            "fixture": round(fixture_exp, 2),
            "fixture_max": inventory.max_fixture_exposure,
            "total": round(total_exp, 2),
            "total_max": inventory.max_total_exposure,
        },
        "ledger": ledger.snapshot(marks),
        "equity_curve": ledger.equity_curve[-600:],
        "log": list(narrative),
        "anchor": anchor_scheduler.status() if anchor_scheduler is not None else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="In-play odds market maker demo")
    ap.add_argument("--seed", type=int, default=9, help="deterministic scenario seed")
    ap.add_argument("--speed", type=float, default=60.0, help="time compression; 0 = max speed")
    ap.add_argument("--port", type=int, default=8765, help="dashboard port")
    ap.add_argument("--no-dashboard", action="store_true", help="headless run, exit at full time")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--anchor",
        action="store_true",
        help=(
            "anchor the ledger's audit-chain head to Solana devnet periodically "
            "(needs ODDS_MM_SOLANA_MNEMONIC_FILE, see .env.example; degrades to "
            "a logged no-op if the wallet/RPC isn't available)"
        ),
    )
    ap.add_argument("--anchor-min-fills", type=int, default=25, help="anchor at least every N new audit records")
    ap.add_argument("--anchor-min-interval-s", type=float, default=300.0, help="anchor at least every M seconds")
    args = ap.parse_args()

    anchor_scheduler = None
    if args.anchor:
        policy = AnchorPolicy(
            min_fills_between=args.anchor_min_fills,
            min_interval_ms=int(args.anchor_min_interval_s * 1000),
        )
        try:
            sink = SolanaAnchor.from_env()
        except Exception as exc:  # missing wallet file / bad mnemonic / etc.
            print(f"[demo] --anchor requested but Solana sink unavailable ({exc}); anchoring disabled")
            sink = NullAnchor()
        anchor_scheduler = LedgerAnchorScheduler(sink=sink, policy=policy)

    run(
        seed=args.seed,
        speed=args.speed,
        port=args.port,
        dashboard=not args.no_dashboard,
        quiet=args.quiet,
        anchor_scheduler=anchor_scheduler,
    )


if __name__ == "__main__":
    main()
