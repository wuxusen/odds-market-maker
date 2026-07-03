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

Python 3.10+, **zero runtime dependencies** (stdlib only). One command:

```bash
python -m odds_mm.demo
```

This starts a full simulated World-Cup-style match (goals, a red card,
five bookmakers quoting with realistic vig and lag), runs the market maker
against seeded synthetic taker flow, and serves a live dashboard at
**http://127.0.0.1:8765/** — current quotes, positions, PnL curve, and
circuit-breaker state.

Other modes:

```bash
python -m odds_mm.demo --speed 0 --no-dashboard   # headless, instant, exits
python -m odds_mm.demo --seed 42                  # different reproducible match
pip install pytest && pytest                      # 63 tests
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
  demo.py         # one-command end-to-end demo
tests/            # 63 pytest cases: math, risk gates, breaker, accounting
docs/DESIGN.md    # formulas and risk design
```
