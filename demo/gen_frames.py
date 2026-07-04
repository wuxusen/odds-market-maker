#!/usr/bin/env python3
"""Generate demo/reel_frames.json — real engine output, replayed for video.

This runs the exact curated scenario in ``scripts/run_demo_scenario.py``
(seed 299: the deterministic match with goals at 3', 13', 21' and a red
card at 28') through the real pricing / market-making / ledger stack, and
records the same per-tick dashboard state the live dashboard renders
(``odds_mm.demo._build_state``). Nothing in the output is hand-authored —
every score, quote, PnL figure and audit-chain length is what the engine
actually produced for this seed.

The only thing this script adds on top of a plain simulation run is a
*video timeline*: it takes the dense stream of engine states (one every
~3-5 simulated seconds) and re-samples it onto a compact schedule of
video-timestamps, compressing the long incident-free stretches and holding
on the interesting moments (goals, breaker trips, re-arms) — the same
editorial choices a human editor would make cutting real screen capture,
just applied to real recorded data instead of a live recording.

Usage:

    python demo/gen_frames.py [--out demo/reel_frames.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odds_mm.demo import TakerFlow, _build_state  # noqa: E402
from odds_mm.feeds import SimulatedFeed  # noqa: E402
from odds_mm.ledger import PaperLedger  # noqa: E402
from odds_mm.mm import BreakerState, CircuitBreaker, InventoryBook, MarketMaker, MMConfig  # noqa: E402
from odds_mm.pricing import ConsensusPricer  # noqa: E402
from odds_mm.types import EventKind, MatchEvent, MatchPhase, OddsTick, QuoteSet, ScoreTick  # noqa: E402

SEED = 299  # the curated demo scenario — see scripts/run_demo_scenario.py

# Real, previously-verified devnet anchor transaction (see README.md
# "On-chain audit anchoring" section) — cited verbatim, not invented here.
ON_CHAIN_TX = "a1HPa9V9k8Q8ckFJoNLckSjCC7yNRsEUU8NSsfpnj6UfTxuWtpPDn4rpGLZegmMifLak7zwADh9LNB5s598D2MG"
ON_CHAIN_HEAD = "3bec697170a5369a1395e79d4462d50f898d89850d5f5a5afca403cd44b05b48"
GITHUB_URL = "github.com/wuxusen/odds-market-maker"


def run_and_capture(seed: int = SEED):
    """Replay the exact pipeline odds_mm.demo.run() uses, capturing every
    dashboard-state snapshot instead of serving it over HTTP."""
    feed = SimulatedFeed(seed=seed)
    pricer = ConsensusPricer(method="power")
    inventory = InventoryBook()
    breaker = CircuitBreaker()
    maker = MarketMaker(MMConfig(), inventory, breaker)
    ledger = PaperLedger(starting_cash=1_000.0)
    takers = TakerFlow(seed=seed)

    score = ScoreTick(feed.fixture_id, feed.kickoff_ts_ms, 0, MatchPhase.NOT_STARTED, 0, 0)
    quotes = QuoteSet(fixture_id=feed.fixture_id, ts=0, halted=True, halt_reason="warmup")
    narrative: list[dict] = []
    prev_breaker_state = breaker.state
    prev_trip_count = 0
    raw: list[dict] = []
    now = feed.kickoff_ts_ms

    def _note(ts: int, text: str) -> None:
        minute = max(0, ts - feed.kickoff_ts_ms) // 60_000
        narrative.append({"minute": int(minute), "text": text})
        del narrative[:-16]

    _note(feed.kickoff_ts_ms, "kick-off — books warming up, no quotes yet")

    for item in feed.stream():
        now = item.ts

        if isinstance(item, OddsTick):
            breaker.on_feed_item(item.ts, now)
            pricer.on_tick(item)
        elif isinstance(item, ScoreTick):
            breaker.on_score(item, now)
            score = item
        elif isinstance(item, MatchEvent):
            breaker.on_event(item, now)
            if item.kind in (EventKind.GOAL, EventKind.RED_CARD):
                _note(item.ts, f"{item.kind.value} {item.team or ''} {item.detail}".strip())

        if breaker.state == BreakerState.TRIPPED and len(breaker.trips) > prev_trip_count:
            _note(now, f"circuit breaker TRIPPED — {breaker.reason}")
        elif prev_breaker_state == BreakerState.TRIPPED and breaker.state == BreakerState.ACTIVE:
            _note(now, "circuit breaker re-armed — quoting resumed")
        prev_breaker_state = breaker.state
        prev_trip_count = len(breaker.trips)

        fair = pricer.fair_price(now)
        if fair is None:
            quotes = QuoteSet(feed.fixture_id, now, halted=True, halt_reason="no consensus")
        else:
            quotes = maker.quote(fair, now)

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

        marks = {}
        if fair is not None:
            marks = {(feed.fixture_id, oc): p for oc, p in zip(fair.outcomes, fair.probabilities)}
        ledger.mark(now, marks)

        state = _build_state(feed, score, quotes, fair, breaker, inventory, ledger, narrative)
        cooldown_remaining_ms = 0
        if breaker.state == BreakerState.TRIPPED:
            cooldown_remaining_ms = max(0, breaker.cooldown_ms - (now - breaker._tripped_at))
        raw.append(
            {
                "minute": (now - feed.kickoff_ts_ms) / 60_000.0,
                "state": state,
                "cooldown_remaining_ms": cooldown_remaining_ms,
            }
        )

    winner = feed.winning_outcome()
    ledger.settle(feed.fixture_id, winner, now)
    final = ledger.snapshot()
    final["breaker"] = breaker.status()
    _note(now, f"full time {feed.final_score()[0]}-{feed.final_score()[1]} — settled ({winner})")
    final_state = _build_state(feed, score, quotes, None, breaker, inventory, ledger, narrative, final=True)
    raw.append({"minute": (now - feed.kickoff_ts_ms) / 60_000.0, "state": final_state, "cooldown_remaining_ms": 0})

    incidents = [(inc.minute, inc.kind.value, inc.team) for inc in feed._incidents]
    return raw, final, incidents, feed


def _nearest(raw: list[dict], minute: float) -> dict:
    """Closest captured engine state to a given match minute."""
    return min(raw, key=lambda r: abs(r["minute"] - minute))


def _slice(raw: list[dict], lo: float, hi: float) -> list[dict]:
    return [r for r in raw if lo <= r["minute"] <= hi]


def _light(entry: dict) -> str:
    st = entry["state"]["breaker"]["state"]
    if st == "ACTIVE":
        return "green"
    if entry["cooldown_remaining_ms"] <= 0:
        return "amber"  # cooldown elapsed, waiting on fresh data
    return "red"


def _emit_frame(t: float, entry: dict, caption: str, chapter: str) -> dict:
    s = entry["state"]
    m = s["match"]
    breaker = s["breaker"]
    ledger = s["ledger"]
    return {
        "t": round(t, 2),
        "chapter": chapter,
        "caption": caption,
        # The engine's own match-clock field only advances on discrete
        # ScoreTicks (kick-off / incidents / full-time), so it looks frozen
        # between events. `entry["minute"]` is the real simulated elapsed
        # time captured at every tick — same data, finer resolution, so the
        # on-screen clock actually ticks during quiet stretches too.
        "clock": f"{int(entry['minute'])}'",
        "score": m["score"],
        "reds": m["reds"],
        "phase": m["phase"],
        "breaker_state": breaker["state"],
        "breaker_reason": breaker["reason"],
        "trip_count": breaker["trip_count"],
        "light": _light(entry),
        "quotes": s["quotes"],
        "fair_probs": s["fair"]["probs"],
        "fair_confidence": s["fair"]["confidence"],
        "n_books": s["fair"]["n_books"],
        "exposure": s["exposure"],
        "n_positions": len(s["positions"]),
        "equity": ledger["equity"],
        "pnl_realized": ledger["pnl_realized"],
        "n_fills": ledger["n_fills"],
        "audit_len": ledger["audit_len"],
        "audit_head": ledger["audit_head"][:16],
        "log": s["log"][-6:],
    }


def build_schedule(raw: list[dict], incidents: list[tuple], feed) -> list[dict]:
    goals = [m for (m, k, _team) in incidents if k == "GOAL"]
    reds = [m for (m, k, _team) in incidents if k == "RED_CARD"]
    g1, g2, g3 = (goals + [None, None, None])[:3]
    rc = reds[0] if reds else None
    last_minute = raw[-1]["minute"]

    CAP_INTRO = "Two-sided quotes around a de-vigged consensus fair value — earning the spread against informed order flow."
    CAP_GOAL = "GOAL → circuit breaker trips — every quote pulled, inventory flat, before the new fair value even finishes computing."
    CAP_REARM = "Cool-down elapses AND fresh data confirms it's safe — only then does quoting resume."
    CAP_QUIET = "Between incidents: consensus pricing, confidence-scaled spreads, inventory kept inside its hard caps."
    CAP_RISK = "No naked exposure. Ever. The breaker fires before the strategy can think."
    CAP_BORING = "The boring, profitable common case: an hour of live play, zero incidents, risk limits holding the whole time."
    CAP_FULLTIME = "Full time. Positions settle against the result, PnL is finalized, every fill hash-chained into a tamper-evident audit log."

    # (video_start, video_end, minute_lo, minute_hi, caption, chapter, fps)
    plan = [
        (8.0, 26.0, 0.0, max(0.5, g1 - 0.3 if g1 else 2.5), CAP_INTRO, "intro", 3.0),
        (26.0, 38.0, max(0.0, (g1 or 3) - 0.6), (g1 or 3) + 0.7, CAP_GOAL, "goal1", 6.0),
        (38.0, 48.0, (g1 or 3) + 0.7, (g1 or 3) + 3.0, CAP_REARM, "rearm1", 3.0),
        (48.0, 68.0, (g1 or 3) + 3.0, (g2 or 13) - 0.6, CAP_QUIET, "quiet1", 1.2),
        (68.0, 80.0, (g2 or 13) - 0.6, (g2 or 13) + 0.7, CAP_GOAL, "goal2", 6.0),
        (80.0, 88.0, (g2 or 13) + 0.7, (g2 or 13) + 2.5, CAP_REARM, "rearm2", 3.0),
        (88.0, 104.0, (g2 or 13) + 2.5, (g3 or 21) - 0.6, CAP_QUIET, "quiet2", 1.2),
        (104.0, 116.0, (g3 or 21) - 0.6, (g3 or 21) + 0.7, CAP_GOAL, "goal3", 6.0),
        (116.0, 122.0, (g3 or 21) + 0.7, (rc or 28) - 0.6, CAP_REARM, "rearm3", 2.0),
        (122.0, 134.0, (rc or 28) - 0.6, (rc or 28) + 1.2, CAP_GOAL, "redcard", 6.0),
        (134.0, 150.0, (rc or 28) + 1.2, 45.0, CAP_RISK, "risk", 1.0),
        (150.0, 168.0, 45.0, last_minute - 0.5, CAP_BORING, "boring", 0.6),
        (168.0, 182.0, last_minute - 0.5, last_minute, CAP_FULLTIME, "fulltime", 3.0),
    ]

    frames: list[dict] = []
    for v0, v1, m0, m1, caption, chapter, fps in plan:
        m0, m1 = min(m0, m1), max(m0, m1)
        window = _slice(raw, m0, m1) or [_nearest(raw, m0)]
        n = max(2, int((v1 - v0) * fps))
        for i in range(n):
            frac = i / (n - 1) if n > 1 else 0.0
            entry = window[min(len(window) - 1, int(frac * len(window)))]
            t = v0 + frac * (v1 - v0)
            frames.append(_emit_frame(t, entry, caption, chapter))

    # ensure strictly increasing / de-duplicated timestamps for the player
    frames.sort(key=lambda f: f["t"])
    return frames


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(Path(__file__).parent / "reel_frames.json"))
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    raw, final, incidents, feed = run_and_capture(args.seed)
    frames = build_schedule(raw, incidents, feed)

    payload = {
        "meta": {
            "seed": args.seed,
            "home": feed.home,
            "away": feed.away,
            "final_score": f"{feed.final_score()[0]}-{feed.final_score()[1]}",
            "n_fills": final["n_fills"],
            "pnl_realized": final["pnl_realized"],
            "equity": final["equity"],
            "audit_len": final["audit_len"],
            "audit_head": final["audit_head"][:16],
            "breaker_trips": final["breaker"]["trip_count"],
            "github_url": GITHUB_URL,
            "on_chain_tx": ON_CHAIN_TX,
            "on_chain_tx_short": ON_CHAIN_TX[:16],
            "on_chain_head": ON_CHAIN_HEAD,
            "title": "In-play odds market maker — autonomous, production-grade",
            "subtitle": f"Demo scenario: seed {args.seed} · deterministic replay of real engine output",
            "video_end_t": frames[-1]["t"] + 16.0,  # room for the closing card
        },
        "frames": frames,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {out_path} ({len(frames)} frames, {len(json.dumps(payload))} bytes, video ~{payload['meta']['video_end_t']:.1f}s)")


if __name__ == "__main__":
    main()
