# In-Play Odds Market Maker Agent

A fully automated, production-minded market making agent for **in-play (live)
sports odds**, built for the Solana World Cup Hackathon — *Trading Tools &
Agents* track (TxODDS).

It consumes multi-bookmaker odds streams (TxODDS **TxLINE** or a
deterministic simulator), removes each bookmaker's margin to build a
**consensus fair price**, and quotes a two-sided market around it — with
inventory-aware skew, hard exposure caps, and a **circuit breaker that
cancels everything the instant a goal, red card, feed stall or latency spike
is detected**. All trading is paper-only, recorded in a tamper-evident,
hash-chained audit ledger designed to be anchored on Solana.

**Why it's credible, not just a script:** the risk stack (circuit breaker,
inventory hard caps, no-naked-exposure ordering) is a direct architectural
transplant from a production perpetual-futures execution system, not
something written for this hackathon — see [Lineage](#lineage-a-production-trading-systems-risk-doctrine)
below. The whole agent is zero-dependency standard-library Python, runs
end-to-end from one command, is fully deterministic under a seed, and ships
with 66 passing tests covering the pricing math, every breaker trip
condition, the exposure gates, and a full reproducible match replay.

**See it work in one command:**

```bash
python scripts/run_demo_scenario.py
```

This plays a hand-picked, fully deterministic 90-minute match — early goal,
comeback, a red card, a long incident-free stretch of pure spread-earning,
full time — against a live dashboard at http://127.0.0.1:8765/. See
[`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) for the shot-by-shot narration
this scenario was built for.

```
                        ┌──────────────────────────────────────────────┐
                        │                  feeds/                      │
   TxLINE (SSE/REST) ──▶│  TxLineAdapter ─┐                            │
                        │                 ├─▶ OddsTick / ScoreTick /   │
   Seeded simulator ───▶│  SimulatedFeed ─┘   MatchEvent (normalised)  │
                        └───────────────┬──────────────────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         │
      ┌───────────────┐        ┌────────────────┐                 │
      │   pricing/    │        │ CircuitBreaker │  goal / red card│
      │ de-vig (mult, │        │ score shock    │  stale feed     │
      │ power) →      │        │ staleness      │  latency spike  │
      │ consensus +   │        │ latency        │                 │
      │ confidence    │        └───────┬────────┘                 │
      └──────┬────────┘                │ gate 1: fail safe first  │
             ▼                         ▼                          │
      ┌─────────────────────────────────────────┐                 │
      │                 mm/                     │                 │
      │  spread = f(volatility, confidence)     │                 │
      │  skew   = f(inventory)                  │                 │
      │  size   = hard caps (fixture/global/qty)│                 │
      └──────────────────┬──────────────────────┘                 │
                         ▼                                        │
      ┌──────────────────────────────┐   ┌───────────────────────────────┐
      │           ledger/            │   │          dashboard/           │
      │ paper fills · PnL · settle   │──▶│ stdlib http.server + 1 page   │
      │ SHA-256 hash-chained audit   │   │ quotes · PnL · breaker state  │
      │ (Solana anchor fields ready) │   └───────────────────────────────┘
      └──────────────────────────────┘
```

## Quickstart

Python 3.10+, **zero runtime dependencies** (stdlib only).

**Curated scenario (recommended — this is what the demo video shows):**

```bash
python scripts/run_demo_scenario.py               # dashboard + narrated terminal log
python scripts/run_demo_scenario.py --speed 0 --no-dashboard  # headless, prints a summary, exits
```

**Or the underlying demo directly, with any seed/parameters you like:**

```bash
python -m odds_mm.demo                # real-time-ish (60x speed) + dashboard, random-ish seed
python -m odds_mm.demo --speed 0       # as fast as possible
python -m odds_mm.demo --no-dashboard  # headless, exits at full time
python -m odds_mm.demo --seed 42       # different (but reproducible) match
```

Either way you get: a full simulated World-Cup-style match (goals, a red
card, five bookmakers quoting with realistic vig, lag and occasional
mispriced ticks), the market maker running against seeded synthetic taker
flow, and a live dashboard at **http://127.0.0.1:8765/** — current two-sided
quotes, consensus fair price, positions and exposure vs hard caps, the PnL
curve, circuit-breaker state, and a running timeline of every incident and
breaker trip.

```bash
pip install pytest && pytest      # 66 tests
```

Every run is **deterministic**: match script, bookmaker noise and taker flow
all derive from `--seed`; the wall clock is used only for pacing, never for
decisions. Same seed ⇒ identical fills, PnL and audit hash chain.

## Design decisions

**Why circuit breakers come first.** In-play market making dies from one
thing: quoting stale prices through a discontinuity. A goal moves the fair
price by 10–30 probability points in seconds; a maker whose quotes survive
that window even briefly gets picked off for many spreads' worth of PnL.
So the breaker is *gate 1*, evaluated before pricing or inventory logic:
goal / red card / score-delta (defence in depth via the score channel, in
case the event message is missed), feed staleness, and transport latency
all cancel every quote immediately and enter a safe state. Re-arming
requires both a cool-down *and* fresh healthy data — never just time.

**Why inventory skew instead of position limits alone.** Hard caps stop
catastrophe but leave you pinned at the cap earning nothing. Skewing the
quote mid against inventory (long ⇒ shade both bid and ask down) makes the
market pay us to unwind risk continuously, so the book mean-reverts toward
flat and the caps rarely bind. Caps remain as the uncrossable last line —
no fair-value opinion ever overrides them.

**Why two de-vig methods.** Multiplicative margin removal is the standard
baseline but under-corrects favourite–longshot bias; the power method loads
more margin onto longshots, matching observed bookmaker behaviour. Both are
implemented and property-tested; consensus uses power by default.

**Why confidence-scaled spreads.** The consensus price carries a confidence
score built from book coverage, cross-book agreement, and tick freshness.
Low confidence widens spreads and, below a floor, stops quoting entirely —
disagreement between books usually means someone knows something.

## Lineage: a production trading system's risk doctrine

This project is a sister of our **binance-perp-toolkit** — a production
perpetual-futures execution stack whose core lessons are transplanted here:

* **No naked exposure windows.** There, protective stops are verified and
  re-attached before anything else trades; here, quotes cannot exist while
  the breaker is tripped, and cancellation precedes repricing.
* **Hard risk gates that nothing overrides.** Max-drawdown / position-count
  limits there; per-fixture, global and per-outcome exposure caps here.
  Both are enforced structurally, not by convention.
* **Determinism and auditability.** Injectable clocks, seeded simulation,
  and an append-only hash-chained ledger (fills + settlements) with fields
  reserved for anchoring the chain head on Solana — mirroring how TxLINE
  itself commits odds batches on-chain via Merkle roots.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the mathematics (de-vig, quote
construction, exposure) and the full risk design.

## Architecture decisions at a glance

| Decision | Alternative considered | Why this way |
|---|---|---|
| Breaker is gate 1, evaluated before pricing | Reprice through incidents with a wider spread | A wider spread is still a *guess*; cancelling is the only response that can't be picked off. Speed of reaction matters more than gracefulness. |
| Re-arm needs cooldown **and** fresh data | Fixed timer only | A quiet feed during cooldown means we still don't know the new price — arming on a timer alone would be trading blind again. |
| Inventory skew, not caps alone | Hard caps only | Caps stop catastrophe but pin the book at the cap earning nothing; skew makes the market pay us to de-risk continuously, so caps rarely bind in practice. |
| Probability-space pricing throughout | Decimal-odds-space math | PnL is linear, the three legs sum to a simplex, and spreads/skews are additive — decimal odds make all three combinatorially messy. |
| Stdlib-only dashboard (`http.server` + one polled page) | A frontend framework / websockets | Zero build step, zero extra dependency surface to audit, works over a bare SSH port-forward — matches the "production-minded, not demo-flashy" thesis. |
| Deterministic seeded simulator, not recorded fixtures | Replaying a recorded real match | A seed gives byte-identical reproducibility for regression tests and for judges re-running the same scenario, while still letting the model produce genuinely adversarial (informed) taker flow. |
| Hash-chained audit ledger with reserved Solana anchor fields | No audit trail / plain log file | Tamper-evidence is worth little if judges have to trust the log file; anchoring the chain head on-chain (same pattern TxLINE itself uses for its own odds batches) makes the paper track record independently checkable later. |

## TxLINE integration status

`feeds/txline.py` implements the real TxLINE surface from the published
OpenAPI spec: dual-token auth headers (`Authorization: Bearer <jwt>` +
`X-Api-Token`), `GET /api/odds/updates/{fixtureId}` polling,
`GET /api/odds/stream` (SSE) as the target transport, and `OddsPayload`
normalisation (`FixtureId/MessageId/Ts/Bookmaker/SuperOddsType/Prices/Pct/
InRunning`). Remaining TODOs (wallet-signed token activation, price-scale
confirmation, scores mapping) are marked in the module docstring. The
engine is feed-agnostic: swapping the simulator for TxLINE is a one-line
change in `demo.py`.

## Compliance

Paper trading only — this agent never places real-money bets and holds no
exchange or wallet credentials. It is a pricing/market-making research and
demonstration tool.

## Repository layout

```
odds_mm/
  types.py        # OddsTick, ScoreTick, MatchEvent, Quote, Fill
  feeds/          # FeedAdapter ABC, TxLineAdapter, SimulatedFeed
  pricing/        # de-vig (multiplicative & power), consensus + confidence
  mm/             # CircuitBreaker, InventoryBook, MarketMaker
  ledger/         # paper ledger, settlement, hash-chained audit trail
  dashboard/      # stdlib HTTP dashboard (single page, JSON polling)
  demo.py         # end-to-end demo runner (feed -> pricing -> mm -> ledger -> dashboard)
scripts/
  run_demo_scenario.py  # one-command curated, narrated demo scenario (for recording)
tests/            # 66 pytest cases: math, risk gates, breaker, accounting, demo/dashboard state
docs/
  DESIGN.md       # formulas and risk design
  DEMO_SCRIPT.md  # shot-by-shot narration for the demo video
```
