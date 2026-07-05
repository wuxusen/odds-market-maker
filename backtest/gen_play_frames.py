#!/usr/bin/env python3
"""Generate per-fixture playback frames for the interactive site (docs/play/).

For every captured real TxODDS TxLINE line in ``backtest/real_odds/{id}.json``
this replays the timeline through the *exact same* production stack the
tournament backtest and the demo reel use — de-vig/consensus pricing, the
guarded two-sided market maker, the circuit breaker and the paper ledger, in
the single-book free-tier configuration — and records a compact, downsampled
frame timeline the browser dashboard can play back.

Every number in every frame (fair value, quotes, exposure, PnL, audit-chain
length/head, breaker state) is real engine output over the real recorded odds.
The only editorial layer is the *playback schedule*: dense engine states are
re-sampled onto a compact timeline that lingers on the goal-time breaker trips
and compresses the quiet stretches — exactly what a human editor does with
screen capture. The synthetic element is the seeded paper counterparty flow,
which trades around the real fair value (informed-on-average, the worst case
for a maker); it is identical to the flow used by the tournament backtest.

Outputs:
    docs/play/{fixtureId}.json   one downsampled timeline per fixture
    docs/play/index.json         fixture directory + tournament invariants

Usage:
    python backtest/gen_play_frames.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from odds_mm.demo import TakerFlow, _build_state  # noqa: E402
from odds_mm.ledger import PaperLedger  # noqa: E402
from odds_mm.mm import (  # noqa: E402
    BreakerState,
    CircuitBreaker,
    InventoryBook,
    MarketMaker,
    MMConfig,
)
from odds_mm.pricing import ConsensusPricer  # noqa: E402
from odds_mm.types import Market, MatchPhase, OddsTick, QuoteSet, ScoreTick  # noqa: E402

REAL_ODDS_DIR = HERE / "real_odds"
OUT_DIR = REPO / "docs" / "play"

STARTING_CASH = 1_000.0
REAL_TAKER_SEED = 4041  # same seeded flow as the backtest / demo real segment
REAL_BOOK_ID = 10021
DISLOCATION_L1 = 0.16  # L1 fair-value jump that flags a real in-play move

# Real, previously-verified devnet anchor tx (README "On-chain audit anchoring").
ON_CHAIN_TX = "a1HPa9V9k8Q8ckFJoNLckSjCC7yNRsEUU8NSsfpnj6UfTxuWtpPDn4rpGLZegmMifLak7zwADh9LNB5s598D2MG"
GITHUB_URL = "github.com/wuxusen/odds-market-maker"

# Playback pacing (frames-per-second density per chapter kind).
FPS_CALM = 3.0   # intro / outro — compressed
FPS_QUIET = 3.0  # between incidents — compressed
FPS_TRIP = 9.0   # goal-time trip — lingered, so the red light is readable


class _FeedShim:
    """Minimal feed-like object so ``_build_state`` renders a real fixture."""

    def __init__(self, fixture_id: int, home: str, away: str) -> None:
        self.fixture_id = fixture_id
        self.home = home
        self.away = away


def replay_fixture(capture_path: Path) -> dict:
    """Replay one captured fixture, capturing every intermediate visual state.

    Mirrors ``backtest/run_backtest.py::backtest_one`` call-for-call so the
    numbers are identical, while additionally recording the per-tick dashboard
    state and a human-readable incident/breaker narrative.
    """
    data = json.loads(Path(capture_path).read_text())
    meta = data["meta"]
    ticks = data["ticks"]
    fid = meta["fixture_id"]
    home, away = meta["home"], meta["away"]
    kickoff = meta["kickoff_ts"]
    scale = meta.get("price_scale", 1000)

    pricer = ConsensusPricer(
        method="power", min_books=1, full_confidence_books=1, max_age_ms=120_000
    )
    inventory = InventoryBook()
    breaker = CircuitBreaker()
    maker = MarketMaker(MMConfig(), inventory, breaker)
    ledger = PaperLedger(starting_cash=STARTING_CASH)
    takers = TakerFlow(seed=REAL_TAKER_SEED)
    shim = _FeedShim(fid, home, away)

    narrative: list[dict] = []   # rolling window handed to the dashboard state
    events: list[dict] = []      # full incident/breaker timeline (kept for UI)
    prev_trip_count = 0
    last_fair = None
    raw: list[dict] = []
    trip_minutes: list[float] = []
    dislocation_trips = 0
    max_fixture_exposure = 0.0
    exposure_cap = inventory.max_fixture_exposure
    exposure_breaches = 0
    trip_log: list[dict] = []

    def _note(ts: int, text: str, kind: str = "normal") -> None:
        minute = int(max(0, ts - kickoff) // 60_000)
        narrative.append({"minute": minute, "text": text})
        del narrative[:-16]
        events.append({"m": minute, "t": text, "k": kind})

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
        pre_state = breaker.state
        breaker.on_feed_item(now, now)
        pricer.on_tick(tick)
        fair = pricer.fair_price(now)

        is_dislocation = False
        if fair is not None and last_fair is not None:
            l1 = sum(abs(a - b) for a, b in zip(fair.probabilities, last_fair.probabilities))
            if l1 > DISLOCATION_L1:
                breaker.trip(now, f"sharp line dislocation d{l1:.2f} — real in-play move")
                is_dislocation = True

        if len(breaker.trips) > prev_trip_count:
            minute = round((now - kickoff) / 60_000.0, 1)
            trip_minutes.append(minute)
            trip_log.append(
                {
                    "minute": minute,
                    "reason": breaker.trips[-1].reason,
                    "real_dislocation": is_dislocation,
                }
            )
            if is_dislocation:
                dislocation_trips += 1
            hp = fair.probabilities[0] if fair else 0.0
            php = last_fair.probabilities[0] if last_fair else hp
            _note(
                now,
                f"line dislocation — home win {php*100:.0f}%->{hp*100:.0f}% "
                f"(real move) — breaker TRIPPED",
                kind="trip",
            )
            prev_trip_count = len(breaker.trips)

        if fair is None:
            quotes = QuoteSet(fid, now, halted=True, halt_reason="warming up")
            marks: dict = {}
        else:
            # maker.quote() runs the breaker re-arm transition internally.
            quotes = maker.quote(fair, now)
            true_probs = dict(zip(fair.outcomes, fair.probabilities))
            for fill in takers.generate(quotes, true_probs, now):
                inventory.on_fill(fill)
                ledger.record_fill(fill)
            marks = {(fid, oc): p for oc, p in zip(fair.outcomes, fair.probabilities)}

        # Re-arm is detected *after* quoting, since that is where the breaker
        # flips TRIPPED -> ACTIVE once cool-down and fresh data both agree.
        if pre_state == BreakerState.TRIPPED and breaker.state == BreakerState.ACTIVE:
            _note(now, "fresh, stable data — breaker re-armed, quoting resumed", kind="rearm")

        ledger.mark(now, marks)

        fx_exp = inventory.fixture_exposure(fid)
        max_fixture_exposure = max(max_fixture_exposure, fx_exp)
        if fx_exp > exposure_cap + 1e-6:
            exposure_breaches += 1

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
                "ne": len(events),
            }
        )
        last_quotes = quotes
        if fair is not None:
            last_fair = fair

    _note(ticks[-1]["ts"], "regulation captured — positions marked to the real line")
    # One closing frame so the outro shows the final ledger + audit state.
    end_ts = ticks[-1]["ts"]
    end_score = ScoreTick(fid, end_ts, int((end_ts - kickoff) / 1000), MatchPhase.SECOND_HALF, 0, 0)
    end_state = _build_state(
        shim, end_score, last_quotes, last_fair, breaker, inventory, ledger, narrative
    )
    raw.append(
        {
            "minute": (end_ts - kickoff) / 60_000.0,
            "state": end_state,
            "cooldown_remaining_ms": 0,
            "ne": len(events),
        }
    )

    final_marks: dict = {}
    if last_fair is not None:
        final_marks = {(fid, oc): p for oc, p in zip(last_fair.outcomes, last_fair.probabilities)}
    final_equity = ledger.equity(final_marks)
    snap = ledger.snapshot(final_marks)

    fixture_meta = {
        "fixture_id": fid,
        "home": home,
        "away": away,
        "label": f"{home} v {away}",
        "start_time": meta.get("start_time"),
        "competition": meta.get("competition"),
        "bookmaker": meta.get("bookmaker"),
        "n_ticks": meta["n_ticks"],
        "n_raw_updates": meta["n_raw_1x2_ticks"],
        "n_fills": snap["n_fills"],
        "pnl_realized": round(ledger.pnl, 2),
        "pnl_mtm": round(final_equity - STARTING_CASH, 2),
        "final_equity": round(final_equity, 2),
        "breaker_trips": len(breaker.trips),
        "dislocation_trips": dislocation_trips,
        "all_trips_from_real_moves": dislocation_trips == len(breaker.trips),
        "trip_minutes": trip_minutes,
        "trip_log": trip_log,
        "max_fixture_exposure": round(max_fixture_exposure, 2),
        "exposure_cap": exposure_cap,
        "exposure_within_cap": exposure_breaches == 0,
        "exposure_breaches": exposure_breaches,
        "audit_len": snap["audit_len"],
        "audit_head": snap["audit_head"],
        "audit_head_short": snap["audit_head"][:16],
        "audit_ok": ledger.verify_audit_chain(),
    }
    return {"meta": fixture_meta, "raw": raw, "events": events, "trip_minutes": trip_minutes}


# --------------------------------------------------------------------------
# Playback schedule — lingers on trips, compresses the quiet stretches.
# --------------------------------------------------------------------------


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def build_schedule(raw: list[dict], trips: list[float]) -> list[tuple]:
    """Return a list of (m_lo, m_hi, seconds, fps, chapter) playback windows."""
    last = raw[-1]["minute"]
    plan: list[tuple] = []
    cursor = 0.0

    intro_hi = max(cursor, (trips[0] - 0.8)) if trips else last
    intro_secs = _clamp((intro_hi - cursor) * 0.22, 4.0, 10.0)
    plan.append((cursor, intro_hi, intro_secs, FPS_CALM, "intro"))
    cursor = intro_hi

    for g in trips:
        dwell_lo = max(cursor, g - 0.8)
        dwell_hi = min(last, g + 1.4)
        if dwell_lo - cursor > 0.3:
            gap_secs = _clamp((dwell_lo - cursor) * 0.2, 2.0, 7.0)
            plan.append((cursor, dwell_lo, gap_secs, FPS_QUIET, "quiet"))
        plan.append((dwell_lo, dwell_hi, 6.5, FPS_TRIP, "trip"))
        cursor = dwell_hi

    if last - cursor > 0.3:
        outro_secs = _clamp((last - cursor) * 0.2, 3.0, 9.0)
        plan.append((cursor, last, outro_secs, FPS_CALM, "outro"))

    return plan


def _nearest(raw: list[dict], minute: float) -> dict:
    return min(raw, key=lambda r: abs(r["minute"] - minute))


def _light(entry: dict) -> str:
    st = entry["state"]["breaker"]["state"]
    if st == "ACTIVE":
        return "green"
    if entry["cooldown_remaining_ms"] <= 0:
        return "amber"
    return "red"


def _q(q: dict) -> dict:
    out = {"o": q["outcome"]}
    if q.get("fair") is not None:
        out["f"] = round(q["fair"], 4)
    if "bid" in q and q.get("bid_size", 0) > 0:
        out["b"] = round(q["bid"], 4)
        out["bs"] = round(q["bid_size"], 1)
    if "ask" in q and q.get("ask_size", 0) > 0:
        out["a"] = round(q["ask"], 4)
        out["as"] = round(q["ask_size"], 1)
    return out


def _compact(t: float, entry: dict, chapter: str) -> dict:
    s = entry["state"]
    b = s["breaker"]
    led = s["ledger"]
    ex = s["exposure"]
    fair = s["fair"]
    return {
        "t": round(t, 2),
        "clock": f"{int(entry['minute'])}'",
        "ch": chapter,
        "light": _light(entry),
        "bs": b["state"],
        "rn": b["reason"],
        "tc": b["trip_count"],
        "q": [_q(x) for x in s["quotes"]],
        "fp": {k: round(v, 4) for k, v in fair["probs"].items()},
        "cf": round(fair["confidence"], 4) if fair["confidence"] is not None else None,
        "nb": fair["n_books"],
        "ef": round(ex["fixture"], 2),
        "efm": ex["fixture_max"],
        "et": round(ex["total"], 2),
        "etm": ex["total_max"],
        "np": len(s["positions"]),
        "eq": round(led["equity"], 2),
        "pl": round(led["pnl_realized"], 2),
        "nf": led["n_fills"],
        "al": led["audit_len"],
        "ah": led["audit_head"][:16],
        "ei": entry.get("ne", 0),
    }


def sample(raw: list[dict], plan: list[tuple]) -> tuple[list[dict], float]:
    frames: list[dict] = []
    t = 0.0
    for (m_lo, m_hi, secs, fps, chapter) in plan:
        m_lo, m_hi = min(m_lo, m_hi), max(m_lo, m_hi)
        window = [r for r in raw if m_lo <= r["minute"] <= m_hi] or [_nearest(raw, m_lo)]
        n = max(2, int(round(secs * fps)))
        for i in range(n):
            frac = i / (n - 1) if n > 1 else 0.0
            entry = window[min(len(window) - 1, int(frac * len(window)))]
            frames.append(_compact(t + frac * secs, entry, chapter))
        t += secs
    frames.sort(key=lambda f: f["t"])
    return frames, round(t, 2)


def build_fixture_payload(replay: dict) -> dict:
    raw = replay["raw"]
    trips = replay["trip_minutes"]
    plan = build_schedule(raw, trips)
    frames, video_end = sample(raw, plan)
    return {
        "meta": {**replay["meta"], "video_end_t": video_end, "on_chain_tx": ON_CHAIN_TX},
        "events": replay["events"],
        "frames": frames,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=str(REAL_ODDS_DIR))
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    real_dir = Path(args.dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in real_dir.glob("*.json") if not p.name.startswith("_"))
    if not files:
        raise SystemExit(f"no captures in {real_dir}; run capture_tournament.py first")

    index_fixtures: list[dict] = []
    total_bytes = 0
    for path in files:
        replay = replay_fixture(path)
        payload = build_fixture_payload(replay)
        fid = payload["meta"]["fixture_id"]
        out_path = out_dir / f"{fid}.json"
        blob = json.dumps(payload, separators=(",", ":"))
        out_path.write_text(blob)
        total_bytes += len(blob)
        m = payload["meta"]
        index_fixtures.append(
            {
                "fixture_id": fid,
                "label": m["label"],
                "home": m["home"],
                "away": m["away"],
                "start_time": m["start_time"],
                "n_ticks": m["n_ticks"],
                "n_fills": m["n_fills"],
                "breaker_trips": m["breaker_trips"],
                "dislocation_trips": m["dislocation_trips"],
                "all_trips_from_real_moves": m["all_trips_from_real_moves"],
                "trip_minutes": m["trip_minutes"],
                "max_fixture_exposure": m["max_fixture_exposure"],
                "exposure_cap": m["exposure_cap"],
                "exposure_within_cap": m["exposure_within_cap"],
                "audit_head_short": m["audit_head_short"],
                "audit_ok": m["audit_ok"],
                "pnl_mtm": m["pnl_mtm"],
                "pnl_realized": m["pnl_realized"],
                "video_end_t": m["video_end_t"],
                "n_frames": len(payload["frames"]),
                "file": f"play/{fid}.json",
            }
        )
        print(
            f"  {m['label'][:32]:<33} frames={len(payload['frames']):>4} "
            f"trips={m['breaker_trips']} maxExp={m['max_fixture_exposure']:.1f} "
            f"audit_ok={m['audit_ok']} ({len(blob)/1024:.0f} KB)"
        )

    # Chronological order for the picker.
    index_fixtures.sort(key=lambda f: (f["start_time"] or 0, f["label"]))

    n = len(index_fixtures)
    index = {
        "config": {
            "starting_cash": STARTING_CASH,
            "books": 1,
            "tier": "TxODDS TxLINE devnet — World Cup free tier "
            "(single demarginalised book, ~60s delay)",
            "market": "1X2 full-time (match result)",
            "settlement": "none — spread capture marked to the real closing de-vigged line",
            "taker_seed": REAL_TAKER_SEED,
            "dislocation_l1": DISLOCATION_L1,
            "exposure_cap_fixture": InventoryBook().max_fixture_exposure,
        },
        "summary": {
            "n_fixtures": n,
            "total_ticks": sum(f["n_ticks"] for f in index_fixtures),
            "total_fills": sum(f["n_fills"] for f in index_fixtures),
            "total_breaker_trips": sum(f["breaker_trips"] for f in index_fixtures),
            "pct_trips_from_real_moves": 100.0
            if all(f["all_trips_from_real_moves"] for f in index_fixtures)
            else round(
                100.0
                * sum(f["dislocation_trips"] for f in index_fixtures)
                / max(1, sum(f["breaker_trips"] for f in index_fixtures)),
                1,
            ),
            "max_fixture_exposure_seen": round(
                max(f["max_fixture_exposure"] for f in index_fixtures), 2
            ),
            "exposure_cap": index_fixtures[0]["exposure_cap"],
            "exposure_within_cap_all": all(f["exposure_within_cap"] for f in index_fixtures),
            "n_exposure_breaches": 0
            if all(f["exposure_within_cap"] for f in index_fixtures)
            else -1,
            "audit_ok_all": all(f["audit_ok"] for f in index_fixtures),
        },
        "on_chain_tx": ON_CHAIN_TX,
        "github_url": GITHUB_URL,
        "fixtures": index_fixtures,
    }
    (out_dir / "index.json").write_text(json.dumps(index, separators=(",", ":")))

    print(
        f"\nwrote {n} fixture timelines + index.json to {out_dir} "
        f"(~{total_bytes/1024:.0f} KB total, ~{total_bytes/1024/n:.0f} KB each)"
    )
    print(
        f"invariants: exposure within cap all={index['summary']['exposure_within_cap_all']} | "
        f"trips={index['summary']['total_breaker_trips']} "
        f"({index['summary']['pct_trips_from_real_moves']}% from real moves) | "
        f"audit ok all={index['summary']['audit_ok_all']} | "
        f"peak exposure={index['summary']['max_fixture_exposure_seen']:.1f}"
    )


if __name__ == "__main__":
    main()
