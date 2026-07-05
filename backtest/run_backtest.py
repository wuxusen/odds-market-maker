#!/usr/bin/env python3
"""Tournament backtest: replay every captured real World Cup odds line.

For each ``backtest/real_odds/{fixtureId}.json`` capture (real TxODDS TxLINE
in-play 1X2 odds, credential-free), this replays the timeline through the
exact same production stack the demo uses — de-vig/consensus pricing, the
guarded two-sided market maker, the circuit breaker and the paper ledger —
in the single-book free-tier configuration, and records what the risk engine
actually did:

* realised PnL (booked round-trip spread, cash basis) and mark-to-market PnL
  (open inventory marked to the real closing de-vigged line);
* number of paper fills against the real line;
* circuit-breaker trips, and whether each was driven by a real in-play line
  dislocation (a goal / sharp move in the recorded odds);
* max drawdown of the session equity curve;
* peak fixture exposure vs. the hard cap, and whether exposure stayed inside
  the hard cap for the entire session (the whole point of the risk engine).

There is no settlement to the match result (the free-tier feed carries no
score channel), so PnL is spread capture marked to the real line, not a bet
on the outcome. Everything is real engine output over real recorded odds; the
only synthetic element is the seeded paper counterparty flow, which trades
around the real fair value (informed-on-average — the worst case for a maker).

Outputs ``backtest/tournament_results.json`` (machine-readable) and prints a
summary table. Fully offline and deterministic.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from odds_mm.ledger import PaperLedger  # noqa: E402
from odds_mm.mm import (  # noqa: E402
    BreakerState,
    CircuitBreaker,
    InventoryBook,
    MarketMaker,
    MMConfig,
)
from odds_mm.pricing import ConsensusPricer  # noqa: E402
from odds_mm.types import Market, OddsTick  # noqa: E402

# Reuse the demo's seeded paper counterparty so the backtest and the demo reel
# share one execution model.
from odds_mm.demo import TakerFlow  # noqa: E402

REAL_ODDS_DIR = HERE / "real_odds"
OUT_PATH = HERE / "tournament_results.json"

STARTING_CASH = 1_000.0
REAL_TAKER_SEED = 4041  # same seeded flow as the demo real segment
REAL_BOOK_ID = 10021
# An L1 fair-value jump this large between consecutive real ticks is a
# discontinuity no smooth reprice explains — a real goal / sharp in-play move.
# Same threshold the demo uses; this is what the breaker senses on a feed with
# no separate score channel.
DISLOCATION_L1 = 0.16


def _max_drawdown(equity_curve: list[tuple[int, float]]) -> float:
    """Largest peak-to-trough decline of the equity curve (absolute units)."""
    peak = -float("inf")
    max_dd = 0.0
    for _ts, eq in equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    return max_dd


def backtest_one(capture_path: Path) -> dict:
    """Replay one captured fixture; return its per-fixture metrics dict."""
    data = json.loads(Path(capture_path).read_text())
    meta = data["meta"]
    ticks = data["ticks"]
    fid = meta["fixture_id"]
    kickoff = meta["kickoff_ts"]
    scale = meta.get("price_scale", 1000)

    # Single-book free-tier configuration — identical to the demo real segment.
    pricer = ConsensusPricer(
        method="power", min_books=1, full_confidence_books=1, max_age_ms=120_000
    )
    inventory = InventoryBook()
    breaker = CircuitBreaker()
    maker = MarketMaker(MMConfig(), inventory, breaker)
    ledger = PaperLedger(starting_cash=STARTING_CASH)
    takers = TakerFlow(seed=REAL_TAKER_SEED)

    last_fair = None
    prev_trip_count = 0
    max_fixture_exposure = 0.0
    exposure_cap = inventory.max_fixture_exposure
    exposure_breaches = 0
    dislocation_trips = 0
    trip_log: list[dict] = []

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

        # Real in-play move detection: a discontinuous fair-value jump trips
        # the breaker — this is a real recorded odds move, not a scripted event.
        is_dislocation = False
        if fair is not None and last_fair is not None:
            l1 = sum(abs(a - b) for a, b in zip(fair.probabilities, last_fair.probabilities))
            if l1 > DISLOCATION_L1:
                breaker.trip(now, f"sharp line dislocation d{l1:.2f} — real in-play move")
                is_dislocation = True

        if len(breaker.trips) > prev_trip_count:
            minute = round((now - kickoff) / 60_000.0, 1)
            trip_log.append(
                {
                    "minute": minute,
                    "reason": breaker.trips[-1].reason,
                    "real_dislocation": is_dislocation,
                }
            )
            if is_dislocation:
                dislocation_trips += 1
            prev_trip_count = len(breaker.trips)

        if fair is None:
            marks: dict = {}
        else:
            quotes = maker.quote(fair, now)
            true_probs = dict(zip(fair.outcomes, fair.probabilities))
            for fill in takers.generate(quotes, true_probs, now):
                inventory.on_fill(fill)
                ledger.record_fill(fill)
            marks = {(fid, oc): p for oc, p in zip(fair.outcomes, fair.probabilities)}

        ledger.mark(now, marks)

        # Hard-cap invariant: track peak fixture exposure and count any breach.
        fx_exp = inventory.fixture_exposure(fid)
        max_fixture_exposure = max(max_fixture_exposure, fx_exp)
        if fx_exp > exposure_cap + 1e-6:
            exposure_breaches += 1

        if fair is not None:
            last_fair = fair

    # Final mark on the last real fair value.
    final_marks: dict = {}
    if last_fair is not None:
        final_marks = {(fid, oc): p for oc, p in zip(last_fair.outcomes, last_fair.probabilities)}
    final_equity = ledger.equity(final_marks)

    snap = ledger.snapshot(final_marks)
    return {
        "fixture_id": fid,
        "home": meta["home"],
        "away": meta["away"],
        "label": f"{meta['home']} v {meta['away']}",
        "start_time": meta.get("start_time"),
        "n_ticks": meta["n_ticks"],
        "n_raw_updates": meta["n_raw_1x2_ticks"],
        "n_fills": snap["n_fills"],
        "pnl_realized": round(ledger.pnl, 4),
        "pnl_mtm": round(final_equity - STARTING_CASH, 4),
        "final_equity": round(final_equity, 4),
        "breaker_trips": len(breaker.trips),
        "dislocation_trips": dislocation_trips,
        "all_trips_from_real_moves": dislocation_trips == len(breaker.trips),
        "trip_log": trip_log,
        "max_drawdown": round(_max_drawdown(ledger.equity_curve), 4),
        "max_fixture_exposure": round(max_fixture_exposure, 4),
        "exposure_cap": exposure_cap,
        "exposure_within_cap": exposure_breaches == 0,
        "exposure_breaches": exposure_breaches,
        "audit_len": snap["audit_len"],
        "audit_head": snap["audit_head"],
        "audit_ok": ledger.verify_audit_chain(),
    }


def aggregate(results: list[dict]) -> dict:
    n = len(results)
    pnls_mtm = [r["pnl_mtm"] for r in results]
    pnls_real = [r["pnl_realized"] for r in results]
    total_trips = sum(r["breaker_trips"] for r in results)
    disloc_trips = sum(r["dislocation_trips"] for r in results)
    profitable = [r for r in results if r["pnl_mtm"] > 0]
    best = max(results, key=lambda r: r["pnl_mtm"])
    worst = min(results, key=lambda r: r["pnl_mtm"])
    return {
        "n_fixtures": n,
        "total_ticks": sum(r["n_ticks"] for r in results),
        "total_fills": sum(r["n_fills"] for r in results),
        "total_pnl_mtm": round(sum(pnls_mtm), 4),
        "mean_pnl_mtm": round(statistics.mean(pnls_mtm), 4),
        "median_pnl_mtm": round(statistics.median(pnls_mtm), 4),
        "total_pnl_realized": round(sum(pnls_real), 4),
        "mean_pnl_realized": round(statistics.mean(pnls_real), 4),
        "median_pnl_realized": round(statistics.median(pnls_real), 4),
        "n_profitable": len(profitable),
        "pct_profitable": round(100.0 * len(profitable) / n, 1),
        "total_breaker_trips": total_trips,
        "dislocation_trips": disloc_trips,
        "pct_trips_from_real_moves": round(100.0 * disloc_trips / total_trips, 1) if total_trips else None,
        "all_trips_from_real_moves": disloc_trips == total_trips,
        "exposure_cap": results[0]["exposure_cap"] if results else None,
        "max_fixture_exposure_seen": round(max(r["max_fixture_exposure"] for r in results), 4),
        "exposure_within_cap_all": all(r["exposure_within_cap"] for r in results),
        "n_exposure_breaches": sum(r["exposure_breaches"] for r in results),
        "audit_ok_all": all(r["audit_ok"] for r in results),
        "mean_max_drawdown": round(statistics.mean([r["max_drawdown"] for r in results]), 4),
        "worst_max_drawdown": round(max(r["max_drawdown"] for r in results), 4),
        "best_fixture": {"label": best["label"], "fixture_id": best["fixture_id"], "pnl_mtm": best["pnl_mtm"]},
        "worst_fixture": {"label": worst["label"], "fixture_id": worst["fixture_id"], "pnl_mtm": worst["pnl_mtm"]},
    }


def run(real_odds_dir: Path = REAL_ODDS_DIR) -> dict:
    files = sorted(p for p in real_odds_dir.glob("*.json") if not p.name.startswith("_"))
    if not files:
        raise SystemExit(f"no captures in {real_odds_dir}; run capture_tournament.py first")
    results = [backtest_one(p) for p in files]
    results.sort(key=lambda r: r["pnl_mtm"], reverse=True)
    agg = aggregate(results)
    return {
        "config": {
            "starting_cash": STARTING_CASH,
            "books": 1,
            "tier": "TxODDS TxLINE devnet — World Cup free tier (single demarginalised book, ~60s delay)",
            "market": "1X2 full-time (match result)",
            "settlement": "none — spread capture marked to the real closing de-vigged line",
            "taker_seed": REAL_TAKER_SEED,
            "dislocation_l1": DISLOCATION_L1,
            "exposure_cap_fixture": InventoryBook().max_fixture_exposure,
        },
        "summary": agg,
        "fixtures": results,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=str(REAL_ODDS_DIR))
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    payload = run(Path(args.dir))
    Path(args.out).write_text(json.dumps(payload, indent=1))
    s = payload["summary"]

    print(f"\nTournament backtest — {s['n_fixtures']} real World Cup fixtures\n")
    print(f"{'fixture':<34}{'fills':>7}{'pnl_mtm':>10}{'trips':>7}{'maxDD':>9}{'maxExp':>9}")
    print("-" * 76)
    for r in payload["fixtures"]:
        print(
            f"{r['label'][:33]:<34}{r['n_fills']:>7}{r['pnl_mtm']:>10.2f}"
            f"{r['breaker_trips']:>7}{r['max_drawdown']:>9.2f}{r['max_fixture_exposure']:>9.2f}"
        )
    print("-" * 76)
    print(
        f"total fixtures={s['n_fixtures']} fills={s['total_fills']} | "
        f"total pnl_mtm={s['total_pnl_mtm']:+.2f} mean={s['mean_pnl_mtm']:+.2f} "
        f"median={s['median_pnl_mtm']:+.2f}"
    )
    print(
        f"profitable={s['n_profitable']}/{s['n_fixtures']} ({s['pct_profitable']}%) | "
        f"breaker trips={s['total_breaker_trips']} ({s['pct_trips_from_real_moves']}% from real moves)"
    )
    print(
        f"exposure cap={s['exposure_cap']} | peak seen={s['max_fixture_exposure_seen']:.2f} | "
        f"within cap all fixtures={s['exposure_within_cap_all']} | audit ok all={s['audit_ok_all']}"
    )
    print(f"best: {s['best_fixture']['label']} ({s['best_fixture']['pnl_mtm']:+.2f})")
    print(f"worst: {s['worst_fixture']['label']} ({s['worst_fixture']['pnl_mtm']:+.2f})")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
