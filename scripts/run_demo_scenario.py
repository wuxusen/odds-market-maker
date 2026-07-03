#!/usr/bin/env python3
"""Run one curated, deterministic match end to end — built for recording.

    python scripts/run_demo_scenario.py

This is a thin wrapper around :func:`odds_mm.demo.run` that fixes the seed
to a hand-picked match with a clean five-minute story arc, so a single
command reliably produces the same narrative every time it's run:

    3'  Away FC scores first — fair value jumps, the breaker trips instantly,
        every resting quote is cancelled before the price even finishes moving.
    13' Home FC equalises. Same reflex: trip, cancel, cool down, re-quote.
    21' Home FC takes the lead 2-1. Third trip/re-arm cycle.
    28' Home FC go down to ten men (red card) while protecting the lead —
        one more trip, then a long quiet stretch (28'-90') where the maker
        just earns spread against two-sided informed flow with no incidents
        at all: the boring, profitable common case.
    90' Full time, 2-1 to Home FC. Positions settle, PnL and the audit chain
        are printed.

Same seed -> byte-identical incidents, fills, PnL and audit hash chain every
run (see ``odds_mm.feeds.simulated.SimulatedFeed`` and
``tests/test_feed_and_engine.py::test_demo_is_reproducible``). Pass --seed to
watch a different (still fully reproducible) match instead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/run_demo_scenario.py` from a source checkout without
# installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odds_mm.demo import run  # noqa: E402

CURATED_SEED = 299  # see module docstring for the story this seed produces

BANNER = r"""
================================================================================
 In-Play Odds Market Maker — curated demo scenario (seed={seed})
--------------------------------------------------------------------------------
 One deterministic 90-minute simulated match: five bookmakers quoting with
 realistic vig/noise/occasional mispricing -> de-vig + consensus -> two-sided
 quoting with inventory skew -> circuit breaker cancelling everything on any
 goal/red card/feed anomaly -> paper fills booked to a hash-chained ledger.

 Dashboard: {url}
 Speed:     {speed}x simulated time (0 = as fast as possible)
================================================================================
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=CURATED_SEED, help="scenario seed (default: curated match)")
    ap.add_argument("--speed", type=float, default=25.0, help="time compression; 0 = max speed (default: 25x)")
    ap.add_argument("--port", type=int, default=8765, help="dashboard port")
    ap.add_argument("--no-dashboard", action="store_true", help="headless, exits at full time")
    ap.add_argument("--quiet", action="store_true", help="suppress the incident log lines")
    args = ap.parse_args()

    dashboard = not args.no_dashboard
    url = f"http://127.0.0.1:{args.port}/" if dashboard else "(disabled)"
    print(BANNER.format(seed=args.seed, url=url, speed=args.speed))

    final = run(
        seed=args.seed,
        speed=args.speed,
        port=args.port,
        dashboard=dashboard,
        hold_open=dashboard,  # keep serving after full time so the recording can linger
        quiet=args.quiet,
    )

    breaker = final.get("breaker", {})
    print("\n" + "=" * 80)
    print(" SCENARIO SUMMARY")
    print("-" * 80)
    print(f" fills            {final['n_fills']}")
    print(f" realized pnl     {final['pnl_realized']:+.2f}  (started with 1,000.00)")
    print(f" ending equity    {final['equity']:.2f}")
    print(f" open positions   {final['positions'] or '(flat — fully settled)'}")
    print(f" breaker trips    {breaker.get('trip_count', 0)}")
    for t in breaker.get("last_trips", []):
        print(f"   - {t['reason']}")
    print(f" audit chain      {final['audit_len']} records, head {final['audit_head'][:16]}...")
    print("=" * 80)


if __name__ == "__main__":
    main()
