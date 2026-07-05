#!/usr/bin/env python3
"""Generate demo/reel_frames.json — a two-segment reel of real engine output.

The reel has two honestly-labelled segments, both driven by the *same*
pricing / market-making / circuit-breaker / paper-ledger stack:

* **Segment 1 — Live data (real):** the agent runs on a captured, real
  TxODDS TxLINE World Cup odds timeline (``demo/real_odds_capture.json``,
  produced by ``demo/capture_real_odds.py``). Every fair value, quote, fill,
  PnL figure and breaker trip in this segment is what the engine actually
  produced consuming the real in-play odds of one real match. The two circuit
  breaker trips are driven by genuine goal-time line dislocations in that
  feed — nothing is hand-authored. On the devnet free tier this is a single
  demarginalised book with ~60s delay, so the de-vig/consensus layer runs
  over one source; captions say so plainly.

* **Segment 2 — Controlled stress test:** the deterministic seed-299
  multi-book scenario (goals at 3', 13', 21' + a red card at 28'), used to
  exercise the risk engine harder than a single real book on the free tier
  can. Labelled on-screen as a controlled scenario, not live data.

Nothing on screen is hand-authored: every score, quote, PnL figure and
audit-chain length is what the engine actually produced. The only editorial
layer is the *video timeline* — re-sampling the dense engine states onto a
compact schedule that lingers on the interesting moments and compresses the
quiet stretches, exactly as a human editor would cut real screen capture.

Usage:

    python demo/capture_real_odds.py   # refresh the real odds capture (online)
    python demo/gen_frames.py          # build reel_frames.json (offline replay)
    python demo/build_reel.py          # inline into demo/reel.html
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
from odds_mm.types import (  # noqa: E402
    EventKind,
    Market,
    MatchEvent,
    MatchPhase,
    OddsTick,
    QuoteSet,
    ScoreTick,
)

HERE = Path(__file__).parent
CAPTURE_PATH = HERE / "real_odds_capture.json"

SEED = 299  # the curated stress-test scenario — see scripts/run_demo_scenario.py
REAL_TAKER_SEED = 4041  # deterministic paper counterparties for the real segment

# The single demarginalised book on the TxLINE devnet free tier.
REAL_BOOK_ID = 10021
# A fair-value move of this L1 magnitude between consecutive real ticks is a
# discontinuity no smooth re-price explains — i.e. a goal. That is exactly the
# "score shock" the breaker exists to catch; on a feed with no separate score
# channel we sense it from the price move itself.
DISLOCATION_L1 = 0.16

# On-screen data-source badges (honest labelling, per segment).
REAL_BADGE = "Live odds — TxODDS TxLINE (World Cup free tier) · paper execution"
STRESS_BADGE = "Controlled multi-book scenario — risk-engine stress test"

# Real, previously-verified devnet anchor transaction (see README.md
# "On-chain audit anchoring") — a genuine on-chain Memo tx committing a
# paper-ledger audit-chain head, cited verbatim, not invented here.
ON_CHAIN_TX = "a1HPa9V9k8Q8ckFJoNLckSjCC7yNRsEUU8NSsfpnj6UfTxuWtpPDn4rpGLZegmMifLak7zwADh9LNB5s598D2MG"
ON_CHAIN_HEAD = "3bec697170a5369a1395e79d4462d50f898d89850d5f5a5afca403cd44b05b48"
GITHUB_URL = "github.com/wuxusen/odds-market-maker"


class _RealFeedShim:
    """Minimal feed-like object so ``_build_state`` renders the real segment."""

    def __init__(self, fixture_id: int, home: str, away: str) -> None:
        self.fixture_id = fixture_id
        self.home = home
        self.away = away


# --------------------------------------------------------------------------
# Segment 1 — real TxLINE odds replay
# --------------------------------------------------------------------------


def run_real_segment(capture_path: Path = CAPTURE_PATH):
    """Replay a captured real TxLINE odds timeline through the full stack.

    Returns ``(raw_states, final_snapshot, capture_meta)``. Everything is real
    engine output over the real odds series; the only synthetic element is the
    paper counterparty flow (seeded), which trades around the real fair value —
    exactly what "paper execution against live prices" means.
    """
    data = json.loads(Path(capture_path).read_text())
    meta = data["meta"]
    ticks = data["ticks"]
    fid = meta["fixture_id"]
    home, away = meta["home"], meta["away"]
    kickoff = meta["kickoff_ts"]
    scale = meta.get("price_scale", 1000)

    # Single-book configuration: with one authoritative demarginalised book the
    # cross-book dispersion is zero, so confidence is driven by freshness alone.
    pricer = ConsensusPricer(
        method="power", min_books=1, full_confidence_books=1, max_age_ms=120_000
    )
    inventory = InventoryBook()
    breaker = CircuitBreaker()
    maker = MarketMaker(MMConfig(), inventory, breaker)
    ledger = PaperLedger(starting_cash=1_000.0)
    takers = TakerFlow(seed=REAL_TAKER_SEED)
    shim = _RealFeedShim(fid, home, away)

    narrative: list[dict] = []
    prev_breaker_state = breaker.state
    prev_trip_count = 0
    last_fair = None
    raw: list[dict] = []

    def _note(ts: int, text: str) -> None:
        minute = max(0, ts - kickoff) // 60_000
        narrative.append({"minute": int(minute), "text": text})
        del narrative[:-16]

    _note(kickoff, f"in-play — live TxLINE odds, {home} v {away}")

    for tk in ticks:
        now = tk["ts"]
        tick = OddsTick(
            fixture_id=fid,
            message_id=str(tk.get("message_id", "")),
            ts=now,
            bookmaker=meta["bookmaker"],
            bookmaker_id=REAL_BOOK_ID,
            market=Market.MATCH_ODDS,
            outcomes=("HOME", "DRAW", "AWAY"),
            prices=[p / scale for p in tk["prices"]],
            in_running=True,
            market_period="FT",
        )
        breaker.on_feed_item(now, now)
        pricer.on_tick(tick)
        fair = pricer.fair_price(now)

        # Real goal detection: a discontinuous fair-value jump trips the breaker.
        if fair is not None and last_fair is not None:
            l1 = sum(abs(a - b) for a, b in zip(fair.probabilities, last_fair.probabilities))
            if l1 > DISLOCATION_L1:
                breaker.trip(now, f"sharp line dislocation Δ{l1:.2f} — real in-play move")

        if breaker.state == BreakerState.TRIPPED and len(breaker.trips) > prev_trip_count:
            hp = fair.probabilities[0] if fair else 0.0
            php = last_fair.probabilities[0] if last_fair else hp
            _note(now, f"line dislocation — home win {php*100:.0f}%→{hp*100:.0f}% (real move) — breaker TRIPPED")
        elif prev_breaker_state == BreakerState.TRIPPED and breaker.state == BreakerState.ACTIVE:
            _note(now, "fresh, stable data — breaker re-armed, quoting resumed")
        prev_breaker_state = breaker.state
        prev_trip_count = len(breaker.trips)

        if fair is None:
            quotes = QuoteSet(fid, now, halted=True, halt_reason="warming up")
        else:
            quotes = maker.quote(fair, now)
            true_probs = dict(zip(fair.outcomes, fair.probabilities))
            for fill in takers.generate(quotes, true_probs, now):
                inventory.on_fill(fill)
                ledger.record_fill(fill)

        marks = {}
        if fair is not None:
            marks = {(fid, oc): p for oc, p in zip(fair.outcomes, fair.probabilities)}
        ledger.mark(now, marks)

        score = ScoreTick(fid, now, int((now - kickoff) / 1000), MatchPhase.SECOND_HALF, 0, 0)
        state = _build_state(shim, score, quotes, fair, breaker, inventory, ledger, narrative)
        cooldown_remaining_ms = 0
        if breaker.state == BreakerState.TRIPPED:
            cooldown_remaining_ms = max(0, breaker.cooldown_ms - (now - breaker._tripped_at))
        raw.append(
            {
                "minute": (now - kickoff) / 60_000.0,
                "state": state,
                "cooldown_remaining_ms": cooldown_remaining_ms,
            }
        )
        if fair is not None:
            last_fair = fair

    _note(
        ticks[-1]["ts"],
        "regulation captured — live paper session, positions marked to the real line",
    )
    final = ledger.snapshot()
    final["breaker"] = breaker.status()
    final_state = _build_state(
        shim,
        ScoreTick(fid, ticks[-1]["ts"], int((ticks[-1]["ts"] - kickoff) / 1000), MatchPhase.SECOND_HALF, 0, 0),
        QuoteSet(fid, ticks[-1]["ts"], halted=True, halt_reason="session end"),
        None,
        breaker,
        inventory,
        ledger,
        narrative,
    )
    raw.append({"minute": (ticks[-1]["ts"] - kickoff) / 60_000.0, "state": final_state, "cooldown_remaining_ms": 0})
    return raw, final, meta


# --------------------------------------------------------------------------
# Segment 2 — deterministic multi-book stress test (seed 299)
# --------------------------------------------------------------------------


def run_scenario_segment(seed: int = SEED):
    """Replay the curated multi-book stress scenario, capturing every state."""
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


# --------------------------------------------------------------------------
# Video-timeline sampling shared by both segments
# --------------------------------------------------------------------------


def _nearest(raw: list[dict], minute: float) -> dict:
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


def _emit_frame(t, entry, caption, chapter, segment, source_badge, score=None, clock=None) -> dict:
    s = entry["state"]
    m = s["match"]
    breaker = s["breaker"]
    ledger = s["ledger"]
    return {
        "t": round(t, 2),
        "segment": segment,
        "source_badge": source_badge,
        "chapter": chapter,
        "caption": caption,
        "clock": clock if clock is not None else f"{int(entry['minute'])}'",
        "score": score if score is not None else m["score"],
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


def _sample(plan, raw, segment, source_badge, score=None):
    """Turn a (v0, v1, m0, m1, caption, chapter, fps) plan into frames."""
    frames: list[dict] = []
    for v0, v1, m0, m1, caption, chapter, fps in plan:
        m0, m1 = min(m0, m1), max(m0, m1)
        window = _slice(raw, m0, m1) or [_nearest(raw, m0)]
        n = max(2, int((v1 - v0) * fps))
        for i in range(n):
            frac = i / (n - 1) if n > 1 else 0.0
            entry = window[min(len(window) - 1, int(frac * len(window)))]
            t = v0 + frac * (v1 - v0)
            frames.append(_emit_frame(t, entry, caption, chapter, segment, source_badge, score=score, clock=clock_for(segment, entry)))
    frames.sort(key=lambda f: f["t"])
    return frames


def clock_for(segment, entry):
    return f"{int(entry['minute'])}'"


def _trip_minutes(raw: list[dict]) -> list[float]:
    """Match-minutes at which the breaker trip count increments."""
    out, prev = [], 0
    for r in raw:
        tc = r["state"]["breaker"]["trip_count"]
        if tc > prev:
            out.append(r["minute"])
        prev = tc
    return out


def build_real_schedule(raw: list[dict], v_start: float) -> list[dict]:
    """Video schedule for the real-odds segment (starts at ``v_start`` s)."""
    trips = _trip_minutes(raw)
    g1 = trips[0] if len(trips) > 0 else 60.0
    g2 = trips[1] if len(trips) > 1 else g1 + 12.0
    last = raw[-1]["minute"]

    CAP_INTRO = "Live TxLINE World Cup odds — the agent quotes a two-sided market around the de-vigged fair value of the real in-play line."
    CAP_QUIET = "Single demarginalised book on the free tier: the de-vig/consensus layer runs over one real source, spread scaled by freshness."
    CAP_GOAL1 = "Real goal — the line dislocates hard. No score feed needed: the breaker trips on the price move itself and pulls every quote."
    CAP_REARM = "Cool-down elapses AND fresh, stable data confirms it — only then does quoting resume."
    CAP_MID = "Back to two-sided quoting on the recovered line, inventory inside its hard caps the whole time."
    CAP_GOAL2 = "The equaliser — another genuine dislocation, same reflex: trip, cancel, cool down, re-quote."
    CAP_END = "Regulation captured live: real fills, real PnL marked to the real line, every fill hash-chained into a tamper-evident audit log."

    v = v_start
    plan = [
        (v, v + 14, 0.0, max(1.0, g1 - 1.0), CAP_INTRO, "real_intro", 2.2),
        (v + 14, v + 24, max(0.0, g1 - 8.0), g1 - 1.0, CAP_QUIET, "real_quiet", 1.6),
        (v + 24, v + 36, g1 - 0.6, g1 + 0.8, CAP_GOAL1, "real_goal1", 5.0),
        (v + 36, v + 46, g1 + 0.8, min(g2 - 0.6, g1 + 6.0), CAP_REARM, "real_rearm1", 2.2),
        (v + 46, v + 58, g1 + 6.0, g2 - 0.6, CAP_MID, "real_mid", 1.4),
        (v + 58, v + 70, g2 - 0.6, g2 + 0.8, CAP_GOAL2, "real_goal2", 5.0),
        (v + 70, v + 82, g2 + 0.8, last, CAP_END, "real_end", 1.8),
    ]
    return _sample(plan, raw, "real", REAL_BADGE, score="live")


def build_scenario_schedule(raw: list[dict], incidents: list[tuple], v_start: float) -> list[dict]:
    """Compact video schedule for the stress-test segment."""
    goals = [m for (m, k, _t) in incidents if k == "GOAL"]
    reds = [m for (m, k, _t) in incidents if k == "RED_CARD"]
    g1, g2, g3 = (goals + [None, None, None])[:3]
    rc = reds[0] if reds else None
    last = raw[-1]["minute"]

    CAP_INTRO = "Now a controlled multi-book scenario — five simulated books, informed order flow — to stress the risk engine harder than one real book can."
    CAP_GOAL = "GOAL → circuit breaker trips — every quote pulled, inventory flat, before the new fair value even finishes computing."
    CAP_REARM = "Cool-down elapses AND fresh data confirms it's safe — only then does quoting resume."
    CAP_QUIET = "Between incidents: consensus pricing, confidence-scaled spreads, inventory kept inside its hard caps."
    CAP_RED = "A red card mid-quote — no naked exposure, ever: the breaker fires before the strategy can think."
    CAP_BORING = "The boring, profitable common case: an hour of live play, zero incidents, risk limits holding the whole time."
    CAP_FULLTIME = "Full time. Positions settle against the result, PnL is finalized, every fill hash-chained into a tamper-evident audit log."

    v = v_start
    plan = [
        (v, v + 12, 0.0, max(0.5, (g1 or 3) - 0.3), CAP_INTRO, "s_intro", 2.0),
        (v + 12, v + 22, (g1 or 3) - 0.6, (g1 or 3) + 0.7, CAP_GOAL, "s_goal1", 5.0),
        (v + 22, v + 30, (g1 or 3) + 0.7, (g2 or 13) - 0.6, CAP_QUIET, "s_quiet1", 1.4),
        (v + 30, v + 40, (g2 or 13) - 0.6, (g2 or 13) + 0.7, CAP_GOAL, "s_goal2", 5.0),
        (v + 40, v + 48, (g2 or 13) + 0.7, (g3 or 21) - 0.6, CAP_REARM, "s_quiet2", 1.4),
        (v + 48, v + 58, (g3 or 21) - 0.6, (g3 or 21) + 0.7, CAP_GOAL, "s_goal3", 5.0),
        (v + 58, v + 68, (g3 or 21) + 0.7, (rc or 28) - 0.6, CAP_QUIET, "s_quiet3", 1.4),
        (v + 68, v + 78, (rc or 28) - 0.6, (rc or 28) + 1.0, CAP_RED, "s_red", 5.0),
        (v + 78, v + 90, (rc or 28) + 1.0, last - 0.5, CAP_BORING, "s_boring", 0.8),
        (v + 90, v + 102, last - 0.5, last, CAP_FULLTIME, "s_fulltime", 3.0),
    ]
    return _sample(plan, raw, "stress", STRESS_BADGE)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(HERE / "reel_frames.json"))
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--capture", default=str(CAPTURE_PATH))
    args = ap.parse_args()

    # -- segment 1: real data --
    real_raw, real_final, cap_meta = run_real_segment(Path(args.capture))
    real_start = 7.0  # after the opening title card
    real_frames = build_real_schedule(real_raw, real_start)
    real_end = real_frames[-1]["t"]

    # -- segment 2: stress test --
    seg2_card_start = real_end + 0.4
    seg2_card_end = seg2_card_start + 8.0
    stress_start = seg2_card_end + 0.2
    stress_raw, stress_final, incidents, feed = run_scenario_segment(args.seed)
    stress_frames = build_scenario_schedule(stress_raw, incidents, stress_start)
    stress_end = stress_frames[-1]["t"]

    frames = real_frames + stress_frames

    closing_start = stress_end + 1.5
    video_end_t = closing_start + 15.0

    real_pnl = real_final["pnl_realized"]
    real_equity = real_final["equity"]

    cards = [
        {
            "start": 0.0,
            "end": real_start,
            "kind": "title",
            "kicker": "Autonomous market making · live odds · paper trading",
            "h1": "An in-play odds market maker, run on real World Cup prices",
            "h2": "Real TxODDS TxLINE odds · de-vig consensus pricing · guarded two-sided quoting · paper execution, real audit trail",
        },
        {
            "start": seg2_card_start,
            "end": stress_start,
            "kind": "divider",
            "kicker": "Segment 2 · controlled stress test",
            "h1": "Same engine, harder test",
            "h2": "A deterministic multi-book scenario — goals and a red card on demand — to stress the risk engine past what one real free-tier book can.",
        },
        {
            "start": closing_start,
            "end": video_end_t,
            "kind": "closing",
            "kicker": "odds-market-maker",
            "h1": "Real odds in. Real risk gates. Real audit trail.",
            "h2": GITHUB_URL,
        },
    ]

    payload = {
        "meta": {
            "github_url": GITHUB_URL,
            "on_chain_tx": ON_CHAIN_TX,
            "on_chain_tx_short": ON_CHAIN_TX[:16],
            "on_chain_head": ON_CHAIN_HEAD,
            "video_end_t": round(video_end_t, 2),
            "cards": cards,
            # Segment 1 (real) headline facts.
            "real": {
                "source": cap_meta["source"],
                "competition": cap_meta["competition"],
                "fixture": f"{cap_meta['home']} v {cap_meta['away']}",
                "home": cap_meta["home"],
                "away": cap_meta["away"],
                "bookmaker": cap_meta["bookmaker"],
                "n_real_ticks": cap_meta["n_ticks"],
                "n_raw_updates": cap_meta["n_raw_1x2_ticks"],
                "n_fills": real_final["n_fills"],
                "pnl_realized": round(real_pnl, 2),
                "equity": round(real_equity, 2),
                "audit_len": real_final["audit_len"],
                "audit_head": real_final["audit_head"][:16],
                "breaker_trips": real_final["breaker"]["trip_count"],
            },
            # Segment 2 (stress) headline facts.
            "stress": {
                "home": feed.home,
                "away": feed.away,
                "final_score": f"{feed.final_score()[0]}-{feed.final_score()[1]}",
                "n_fills": stress_final["n_fills"],
                "pnl_realized": round(stress_final["pnl_realized"], 2),
                "equity": round(stress_final["equity"], 2),
                "audit_len": stress_final["audit_len"],
                "audit_head": stress_final["audit_head"][:16],
                "breaker_trips": stress_final["breaker"]["trip_count"],
            },
        },
        "frames": frames,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, separators=(",", ":")))
    print(
        f"wrote {out_path} ({len(frames)} frames; real seg {real_start:.0f}-{real_end:.0f}s, "
        f"stress seg {stress_start:.0f}-{stress_end:.0f}s, video ~{video_end_t:.0f}s)"
    )
    print(
        f"  real:   {cap_meta['home']} v {cap_meta['away']} | "
        f"fills={real_final['n_fills']} pnl={real_pnl:+.2f} equity={real_equity:.2f} "
        f"trips={real_final['breaker']['trip_count']}"
    )
    print(
        f"  stress: seed {args.seed} {feed.final_score()[0]}-{feed.final_score()[1]} | "
        f"fills={stress_final['n_fills']} pnl={stress_final['pnl_realized']:+.2f} "
        f"trips={stress_final['breaker']['trip_count']}"
    )


if __name__ == "__main__":
    main()
