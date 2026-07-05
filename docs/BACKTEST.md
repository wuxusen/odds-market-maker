# Tournament backtest — the risk engine across a whole World Cup

This is the market-making agent replayed over **24 real World Cup fixtures**,
using the real in-play 1X2 odds recorded from the TxODDS TxLINE feed. The goal
here is not a profit claim — it's to show that the risk engine behaves the same
way across an entire tournament of real, messy, goal-driven price action, not
just on the one match in the demo reel.

**What this is:** paper execution on real recorded odds. Single demarginalised
book (the TxLINE devnet free tier, ~60s delayed), de-vig/consensus pricing, the
guarded two-sided market maker, the circuit breaker, and the hash-chained paper
ledger — the exact same code path the live demo runs. Every fair value, quote,
fill, breaker trip and exposure number below is what the engine actually did
consuming the real feed.

**What this is not:** it is not a bet on match outcomes, and it is not a "this
prints money" claim. The free-tier feed carries no score channel, so positions
are never settled against the final result — PnL is spread capture, marked to
the real closing de-vigged line. The counterparty flow is a seeded synthetic
model that trades around the real fair value (informed-on-average, the worst
case for a maker). It does not model a real book's markup, real fill latency,
queue position, or the adverse selection of a genuinely informed crowd. So read
the PnL as "did the quoting/skew/breaker loop capture spread on this real line",
not as a live-tradeable edge. The number worth trusting is the **risk-engine
consistency**: exposure caps held, and every breaker trip was explained by a
real move, on all 24 matches.

## How to reproduce

```bash
# 1. Pull the real in-play odds for up to 24 finished World Cup fixtures
#    (needs TxLINE creds; writes de-identified backtest/real_odds/{id}.json).
python backtest/capture_tournament.py --max 24

# 2. Replay every capture through the engine, offline and deterministic.
python backtest/run_backtest.py
```

The captures under `backtest/real_odds/` are odds and timestamps only — no
token, JWT or wallet material — so step 2 is fully offline and is what CI runs
(`tests/test_backtest.py`). `backtest/tournament_results.json` is the
machine-readable output.

## Method

For each fixture:

1. Take the full-time 1X2 in-play line off the TxLINE odds-update cache, keep
   only the in-running ticks, and down-sample the dense sub-second stream onto
   a fixed 5-second grid (keeping a real median tick per bucket — no synthesised
   averages). This is the same filter the demo capture uses.
2. Replay the ticks through de-vig → consensus fair value → market maker →
   circuit breaker → paper ledger, in the single-book free-tier configuration.
3. A discontinuous jump in the fair value between consecutive ticks (L1 > 0.16)
   is treated as a real in-play dislocation — a goal or sharp move — and trips
   the breaker. On a feed with no separate score channel, the price move *is*
   the signal. Every trip in this backtest comes from a real recorded move; none
   are hand-authored.
4. Record realised PnL, mark-to-market PnL, fills, breaker trips, max drawdown,
   and peak fixture exposure vs. the hard cap.

Starting cash is 1,000 units per fixture; the hard exposure cap is 200 units per
fixture. Fixtures are captured knockout-stage first (the richest in-play lines).

## Aggregate results

| Metric | Value |
|---|---|
| Fixtures backtested | **24** |
| Real in-play ticks replayed | 18,347 |
| Paper fills against the real line | 9,863 |
| Total PnL (mark-to-market) | **+439.54** |
| Mean / median PnL per fixture (MTM) | +18.31 / +18.46 |
| Total PnL (realised, cash basis) | +769.92 |
| Fixtures profitable (MTM) | **24 / 24 (100%)** |
| Circuit-breaker trips (total) | **35** |
| Trips driven by a real in-play move | **35 / 35 (100%)** |
| Exposure hard cap (per fixture) | 200 |
| Peak fixture exposure observed | 73.3 (37% of cap) |
| Exposure-cap breaches | **0** |
| Fixtures with exposure inside the cap for the entire session | **24 / 24 (100%)** |
| Audit chain verified | 24 / 24 |
| Mean / worst max drawdown | 5.89 / 34.64 |
| Best fixture | Canada v Morocco (+37.92) |
| Worst fixture | Paraguay v France (+3.19) |

The headline for the judges is the middle block: **exposure never once breached
the hard cap on any tick of any match, and every single breaker trip was
explained by a real recorded line move.** That is the invariant the whole
architecture exists to hold, and it held identically across 24 different games.

The PnL being positive on every fixture is a consequence of the setup, not a
market edge: with the breaker pulling all quotes through each real dislocation,
the maker isn't picked off at the goals, and between incidents the seeded flow
is symmetric around the real fair value, so the quoted spread is captured. Swap
in a real book's markup, real latency, and genuinely informed flow and this
compresses hard — very possibly negative. Treat these numbers as "the loop is
internally consistent and doesn't leak risk", not as a live P&L forecast.

Realised (cash) PnL and mark-to-market PnL differ per fixture because open
inventory is carried at the real closing line rather than settled — e.g. Spain
v Austria books +24.44 MTM but −28.43 realised, the gap being open positions
marked to the line. Both are reported so nothing is cherry-picked.

## Per-fixture detail

| Fixture | Ticks | Fills | PnL (MTM) | Realised | Breaker trips | Max DD | Peak exposure |
|---|--:|--:|--:|--:|--:|--:|--:|
| Canada v Morocco | 893 | 482 | +37.92 | +25.79 | 1 | 0.31 | 44.8 |
| Portugal v Croatia | 846 | 443 | +30.28 | +37.95 | 2 | 5.50 | 29.4 |
| Croatia v Ghana | 839 | 455 | +28.81 | +64.92 | 1 | 0.38 | 43.3 |
| Netherlands v Morocco | 880 | 484 | +28.16 | +32.80 | 0 | 0.36 | 55.1 |
| Spain v Austria | 659 | 381 | +24.44 | −28.43 | 2 | 0.37 | 70.9 |
| Australia v Egypt | 762 | 392 | +23.31 | +29.46 | 2 | 8.82 | 36.4 |
| Algeria v Austria | 830 | 388 | +23.23 | +44.39 | 4 | 34.64 | 56.3 |
| Colombia v Ghana | 793 | 439 | +21.33 | +45.72 | 1 | 0.44 | 25.0 |
| Belgium v Senegal | 808 | 435 | +20.67 | +76.65 | 2 | 4.97 | 40.2 |
| Germany v Paraguay | 833 | 449 | +20.12 | +49.24 | 2 | 11.12 | 45.2 |
| Ivory Coast v Norway | 904 | 479 | +20.02 | +6.68 | 1 | 6.27 | 54.8 |
| Panama v England | 751 | 424 | +19.21 | +19.65 | 1 | 0.48 | 58.3 |
| South Africa v Canada | 837 | 451 | +17.71 | +59.69 | 0 | 0.93 | 52.2 |
| Congo DR v Uzbekistan | 809 | 422 | +17.28 | +20.07 | 2 | 3.69 | 32.3 |
| England v Congo DR | 897 | 459 | +17.22 | +7.50 | 1 | 2.18 | 44.6 |
| Argentina v Cape Verde | 685 | 410 | +16.98 | +42.61 | 2 | 4.16 | 53.0 |
| Colombia v Portugal | 870 | 466 | +14.42 | +29.12 | 0 | 2.17 | 59.7 |
| USA v Bosnia & Herzegovina | 776 | 426 | +14.09 | +39.04 | 2 | 3.70 | 44.7 |
| France v Sweden | 658 | 369 | +13.07 | +32.57 | 2 | 3.80 | 28.9 |
| Switzerland v Algeria | 747 | 408 | +9.41 | +21.11 | 2 | 11.25 | 19.3 |
| Brazil v Japan | 847 | 437 | +9.31 | +29.00 | 2 | 15.51 | 43.4 |
| Jordan v Argentina | 307 | 163 | +5.74 | +10.06 | 1 | 1.29 | 24.9 |
| Mexico v Ecuador | 292 | 153 | +3.58 | +0.40 | 1 | 4.59 | 29.0 |
| Paraguay v France | 824 | 448 | +3.19 | +73.92 | 1 | 14.41 | 73.3 |

Two fixtures (Mexico v Ecuador, Jordan v Argentina) have far fewer in-play ticks
— the recorded in-running window was short — so they book fewer fills and
smaller numbers. They are kept in rather than dropped, because a backtest that
quietly discards its thin samples isn't an honest one.

## Caveats, stated plainly

- **Single book, free tier.** One demarginalised synthetic book with ~60s delay.
  The de-vig/consensus layer runs over one source; cross-book dispersion is zero,
  so confidence is driven by freshness alone. A real deployment would run several
  books and the dispersion term would matter.
- **No settlement.** PnL is spread capture marked to the real line, not a payout
  on the match result. There is no directional outcome bet anywhere in here.
- **Synthetic counterparty.** The flow that fills our quotes is seeded and
  symmetric around the real fair value. It is deliberately informed-on-average
  (the hard case) but it is not a real informed crowd, and there are no exchange
  fees, no queue priority and no partial-fill latency modelled.
- **What is real:** the odds, the millisecond timestamps, the in-play moves, and
  every decision the pricing / market-making / breaker / ledger stack made in
  response to them.

The bottom line we stand behind: across 24 real matches and ~18k real ticks, the
risk engine did exactly one thing every time — quote the spread, and pull
everything the instant the real line dislocated, never once exceeding its
exposure cap.
