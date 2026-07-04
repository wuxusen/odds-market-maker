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
hash-chained audit ledger whose rolling head is periodically anchored to
**Solana devnet** in a real Memo Program transaction — see
[On-chain audit anchoring](#on-chain-audit-anchoring) below.

**Why it's credible, not just a script:** the risk stack (circuit breaker,
inventory hard caps, no-naked-exposure ordering) is a direct architectural
transplant from a production perpetual-futures execution system, not
something written for this hackathon — see [Lineage](#lineage-a-production-trading-systems-risk-doctrine)
below. The core agent is zero-dependency standard-library Python, runs
end-to-end from one command, is fully deterministic under a seed, and ships
with 90 passing tests (66 core + 24 for the on-chain anchoring path against
a mocked RPC client, no live network required) covering the pricing math,
every breaker trip condition, the exposure gates, a full reproducible match
replay, and the Solana Memo transaction packing/verification logic.

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
      └──────────────┬───────────────┘   └───────────────────────────────┘
                     ▼ (periodic, best-effort)
      ┌──────────────────────────────┐
      │           anchor/            │
      │ chain head → Memo tx on      │
      │ Solana devnet · verify_anchor│
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
pip install pytest && pytest      # 66 core tests (anchoring tests skip cleanly without extras)

# Full coverage including the on-chain anchoring path (mocked RPC, no network):
pip install -e ".[dev,anchor]" && pytest   # 90 tests
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

## On-chain audit anchoring

Every fill and settlement is appended to `PaperLedger`'s SHA-256 hash chain
(`prev_hash` + `hash` per record, git-style — see `ledger/ledger.py`). That
makes the chain *tamper-evident to anyone holding the log*, but a judge
still has to trust that the log they're looking at is the one that was
actually produced live. `odds_mm/anchor/` closes that gap: it periodically
commits the chain's rolling **head hash** to **Solana devnet** as a real
Memo Program transaction, so the paper track record is checkable from
nothing but a transaction signature — no access to our server or database
required.

**Live anchor (devnet, verifiable right now):** a full headless session was
run and its audit-chain head anchored on devnet — decode the Memo and it
matches the ledger head byte-for-byte:

| field | value |
| --- | --- |
| tx | [`a1HPa9V9k8Q8ckFJoNLckSjCC7yNRsEUU8NSsfpnj6UfTxuWtpPDn4rpGLZegmMifLak7zwADh9LNB5s598D2MG`](https://explorer.solana.com/tx/a1HPa9V9k8Q8ckFJoNLckSjCC7yNRsEUU8NSsfpnj6UfTxuWtpPDn4rpGLZegmMifLak7zwADh9LNB5s598D2MG?cluster=devnet) |
| slot | 473814127 |
| anchored head | `3bec697170a5369a1395e79d4462d50f898d89850d5f5a5afca403cd44b05b48` |

Open the explorer link, read the Memo instruction, and you'll see the prefix
followed by exactly that hex — the `verify_anchor()` helper below does the
same check programmatically.

**How it works:**

* `SolanaAnchor` (`odds_mm/anchor/solana_anchor.py`) derives a devnet
  keypair from a mnemonic file (Phantom-compatible `m/44'/501'/0'/0'`
  derivation via `bip_utils`), and packs the current chain head into a
  single Memo Program instruction (`odds-mm-audit-head:v1:<sha256-hex>`),
  signs it with `solders`, and submits it via `solana.rpc.async_api`.
* `LedgerAnchorScheduler` decides *when* an anchor is due — every
  `min_fills_between` new audit records or every `min_interval_ms`,
  whichever comes first, and never re-anchors an unchanged head. It is
  driven once per feed item from `odds_mm.demo.run` when `--anchor` is
  passed; a successful anchor is itself recorded back into the ledger as a
  chained `type: "anchor"` record (`PaperLedger.record_anchor`) naming the
  transaction signature and slot.
* `verify_anchor(tx_sig, expected_hash)` independently re-fetches that
  transaction from devnet, decodes the Memo instruction, and confirms it
  matches the claimed hash — the same check anyone (a judge, a
  counterparty) can run themselves against the public signature.

**Why anchor the head, not the whole ledger.** A hash chain's head already
transitively commits to every prior record; anchoring it is equivalent to
anchoring the full history at a fraction of the cost, the same reason
TxLINE itself anchors a Merkle *root* rather than every odds tick (see
`GET /api/odds/validation`'s `subTreeProof`/`mainTreeProof`). Anchoring the
ledger's own head this way is a direct structural echo of that pattern,
just applied to our own accounting trail instead of TxLINE's odds data.

**Why it can't be a bottleneck.** Anchoring is pure external I/O layered
*outside* the trading core, matching this codebase's fail-safe-first
doctrine (see the circuit breaker above): `AnchorSink.anchor()` is
contractually never allowed to raise, only to return `AnchorResult(ok=False,
error=...)`, and every call site treats a failure as "retry next window",
never as a reason to stop quoting. Devnet RPC hiccups or an exhausted
faucet degrade anchoring to a no-op; they cannot touch pricing, quoting or
fills.

**How to verify it yourself:**

```bash
python -c "
from odds_mm.anchor import verify_anchor
v = verify_anchor('<tx signature>', '<expected chain-head hex>')
print(v)
"
```

or just open `https://explorer.solana.com/tx/<signature>?cluster=devnet`
and read the Memo instruction's logged text directly.

**Running it yourself:** anchoring needs the optional `solana`/`solders`/
`bip-utils` dependencies (`pip install -e ".[anchor]"`) and a devnet wallet
mnemonic *file path* in `ODDS_MM_SOLANA_MNEMONIC_FILE` (see
`.env.example` — never a mnemonic value in the environment or in any
committed file). Everything is devnet-only and holds no mainnet funds or
production credentials; see [Compliance](#compliance).

```bash
python -m odds_mm.demo --anchor --no-dashboard --speed 0       # wire it into a live session
python scripts/anchor_devnet_demo.py                            # standalone: airdrop + anchor + verify, prints tx + explorer link
```

`scripts/anchor_devnet_demo.py` is best-effort against the *real* public
devnet faucet and RPC: it requests an airdrop if the wallet is empty, runs
a short simulated session to produce a real audit chain, submits the
anchor transaction, and independently re-verifies it — printing the
signature and an explorer link on success. Public devnet faucets are
aggressively rate-limited and sometimes refuse outright; when that happens
the script fails loudly and explains why rather than hanging, and none of
that affects the mocked-RPC test coverage in `tests/test_anchor.py`, which
is what actually gates this repo's correctness.

See `docs/DESIGN.md` section 7 for the full design write-up and the
alternatives considered.

**Live status:** the anchoring code path is fully implemented and covered
by `tests/test_anchor.py` against a mocked RPC client (no network
required — this is what gates correctness). A first real devnet example
signature will be added above once the linked devnet wallet has been
funded — public devnet airdrop faucets are rate-limited per address/IP and
were returning `429`/"airdrop faucet has run dry" at the time of this
commit; rerun `scripts/anchor_devnet_demo.py` to populate one when the
faucet cooperates.

## Architecture decisions at a glance

| Decision | Alternative considered | Why this way |
|---|---|---|
| Breaker is gate 1, evaluated before pricing | Reprice through incidents with a wider spread | A wider spread is still a *guess*; cancelling is the only response that can't be picked off. Speed of reaction matters more than gracefulness. |
| Re-arm needs cooldown **and** fresh data | Fixed timer only | A quiet feed during cooldown means we still don't know the new price — arming on a timer alone would be trading blind again. |
| Inventory skew, not caps alone | Hard caps only | Caps stop catastrophe but pin the book at the cap earning nothing; skew makes the market pay us to de-risk continuously, so caps rarely bind in practice. |
| Probability-space pricing throughout | Decimal-odds-space math | PnL is linear, the three legs sum to a simplex, and spreads/skews are additive — decimal odds make all three combinatorially messy. |
| Stdlib-only dashboard (`http.server` + one polled page) | A frontend framework / websockets | Zero build step, zero extra dependency surface to audit, works over a bare SSH port-forward — matches the "production-minded, not demo-flashy" thesis. |
| Deterministic seeded simulator, not recorded fixtures | Replaying a recorded real match | A seed gives byte-identical reproducibility for regression tests and for judges re-running the same scenario, while still letting the model produce genuinely adversarial (informed) taker flow. |
| Hash-chained audit ledger anchored to Solana devnet | No audit trail / plain log file | Tamper-evidence is worth little if judges have to trust the log file; anchoring the chain head on-chain (same pattern TxLINE itself uses for its own odds batches) makes the paper track record independently checkable from a bare transaction signature. |
| Anchor the rolling chain **head**, not the full ledger | Anchor every fill, or a Merkle root per batch | The head already transitively commits to every prior record — anchoring it is equivalent to anchoring everything at a fraction of the transaction count/cost; a batched Merkle root is the natural next step at higher fill volume (see `docs/DESIGN.md` §7). |
| Anchoring as pluggable, fail-open `AnchorSink` outside the trading core | Anchor synchronously inline with fills | External I/O fails; the breaker doctrine says the core must survive that. `AnchorSink.anchor()` is contractually non-raising and every caller treats failure as "retry next window", never as a reason to stop quoting. |

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
exchange credentials or mainnet wallet. The optional on-chain anchoring
feature (off by default) uses a **devnet-only** Solana wallet purely to
pay its own Memo transaction fee in worthless devnet SOL; it never touches
mainnet, never holds or moves anyone's funds, and the mnemonic is read
from a local file path (never committed, never logged) — see
[On-chain audit anchoring](#on-chain-audit-anchoring). This remains a
pricing/market-making research and demonstration tool.

## Repository layout

```
odds_mm/
  types.py        # OddsTick, ScoreTick, MatchEvent, Quote, Fill
  feeds/          # FeedAdapter ABC, TxLineAdapter, SimulatedFeed
  pricing/        # de-vig (multiplicative & power), consensus + confidence
  mm/             # CircuitBreaker, InventoryBook, MarketMaker
  ledger/         # paper ledger, settlement, hash-chained audit trail
  anchor/         # AnchorSink, SolanaAnchor (devnet Memo tx), LedgerAnchorScheduler, verify_anchor
  dashboard/      # stdlib HTTP dashboard (single page, JSON polling)
  demo.py         # end-to-end demo runner (feed -> pricing -> mm -> ledger -> dashboard [-> anchor])
scripts/
  run_demo_scenario.py   # one-command curated, narrated demo scenario (for recording)
  anchor_devnet_demo.py  # best-effort live devnet anchor: airdrop + anchor + verify
tests/            # 90 pytest cases (66 core + 24 anchoring/mocked-RPC): math, risk gates,
                  # breaker, accounting, demo/dashboard state, Solana packing + verification
docs/
  DESIGN.md       # formulas, risk design, and the anchoring design tradeoffs (§7)
  DEMO_SCRIPT.md  # shot-by-shot narration for the demo video
```
