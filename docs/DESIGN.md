# Design: Pricing Mathematics & Risk Architecture

## 1. Instruments

All quoting and accounting is done on **binary outcome contracts**: a
contract on outcome *o* pays 1.0 if *o* wins, else 0.0. Prices live in
probability space `(0, 1)`; decimal odds are the reciprocal (`price 0.40 ⇔
odds 2.50`). This makes three things trivial that odds-space makes messy:
PnL is linear in price, the three 1X2 legs are mutually exclusive by
construction, and spreads/skews are additive.

## 2. Margin removal (de-vig)

A bookmaker quoting decimal odds `o_i` over `n` outcomes implies raw
probabilities `q_i = 1/o_i` with **overround** `M = Σ q_i − 1 > 0`
(the vig). We recover the book's underlying estimate two ways:

**Multiplicative** (proportional):

```
p_i = q_i / Σ_j q_j
```

**Power**: find `k ≥ 1` such that

```
Σ_i q_i^k = 1        then        p_i = q_i^k
```

`f(k) = Σ q_i^k` is strictly decreasing for `q_i < 1`, so `k` is found by
bracketed bisection (`k = 1` recovers a fair book exactly). The power
method removes relatively more margin from longshots — the empirically
observed favourite–longshot bias — and is the default. Property tested:
both methods return simplex vectors, preserve odds ranking, and power
shaves longshots harder than multiplicative on skewed markets.

## 3. Consensus & confidence

Latest de-vigged vector per bookmaker `p^(b)`, staleness weight
`w_b = 1 − age_b / (2 · maxAge)`; consensus is the weighted mean,
renormalised. Confidence multiplies three observable factors:

```
coverage  = min(1, n_books / N_full)
agreement = max(0, 1 − dispersion / D_scale)   dispersion = mean per-outcome stdev
freshness = mean(1 − age_b / maxAge)
confidence = coverage · (0.6·agreement + 0.4·freshness)
```

Fewer than `min_books` fresh books ⇒ no fair price at all ⇒ no quotes.
Books disagreeing sharply (someone is slow after a goal, or knows
something) collapses confidence and widens or withdraws our market.

## 4. Quote construction

Per outcome with consensus fair `p`:

```
half_spread = clamp(base + v·σ + c·(1 − confidence), min_hs, max_hs)
σ           = rolling mean |Δp| of the fair price   (realised volatility)
skew        = −clamp(inventory / maxQty, −1, 1) · s · half_spread
bid         = clamp(p + skew − half_spread, 0.01, 0.99)
ask         = clamp(p + skew + half_spread, 0.01, 0.99)
```

Long inventory shifts *both* sides down: our ask becomes attractive (we get
lifted and unwind) while our bid backs away (we stop accumulating). The
spread widens with fair-value volatility and with consensus uncertainty.

## 5. Inventory & exposure

Position of `qty` contracts (± = long/short) at average price `avg`:

* long worst case: outcome loses ⇒ lose `qty · avg` (premium paid)
* short worst case: outcome wins ⇒ lose `|qty| · (1 − avg)`

**Fixture exposure** is scenario-based, not additive: for each candidate
winner (plus the "no held leg wins" scenario), sum every leg's settlement
PnL; exposure is the worst scenario's loss. Short HOME + short AWAY
therefore hedge each other — only one can win — and the book correctly
gets credit for it.

Hard gates, in order, on every quote side:

1. Sides that **reduce** inventory are always allowed, capped at flat.
2. Extending sides shrink linearly with headroom:
   `size = base · min(1 − fx/maxFixture, 1 − tx/maxTotal)`, zero at a cap.
3. Per-outcome quantity cap is uncrossable: allowed size never exceeds
   `maxQty − |qty|`, so even a full fill cannot breach it.

No pricing signal, confidence level or PnL argument can override a gate.

## 6. Circuit breaker (fail-safe first)

State machine `ACTIVE → TRIPPED → ACTIVE` with an injectable clock
(`now_ms` is a parameter everywhere — deterministic in tests and sim).

Trip conditions (any one):

| Trigger | Detection | Why |
|---|---|---|
| Goal / red card | `MatchEvent` from the feed | fair value jumps 10–30 pts discontinuously |
| Score delta | score-channel diff, independent of events | defence in depth: missed event message still trips |
| Feed staleness | no item for `stale_after_ms` (watchdog) | blind ⇒ quotes are free options for takers |
| Transport latency | `now − item.ts > max_latency_ms` | quoting on the past |

Tripping cancels **all** quotes immediately (the engine's gate 1 runs before
pricing). Re-arm requires *all* of: cool-down elapsed since the most recent
trip (re-trips extend it), at least one feed item received since the trip,
and the feed currently non-stale. Time alone never re-arms.

This is a deliberate transplant of the risk doctrine from our production
perpetual-futures stack (binance-perp-toolkit): *no naked exposure windows,
hard gates that nothing overrides, safe state on any anomaly*. The failure
modes differ (there: an order resting without its protective stop; here: a
quote resting through a goal) but the architecture is the same shape —
which is exactly the point of reusing it.

## 7. Ledger & Solana anchoring

Every fill/settlement appends a JSON audit record containing the previous
record's SHA-256 (`prev_hash`), forming a tamper-evident chain
(`verify_audit_chain()` re-derives it). Records reserve a vestigial
`anchor` sub-block for wire-schema symmetry (always `null` — see below for
why it can't literally be back-filled), and periodically the chain's
rolling head is committed to **Solana devnet** as a real Memo Program
transaction by `odds_mm.anchor`, mirroring the pattern TxLINE itself uses
to anchor odds batches (`/api/odds/validation` returns `subTreeProof` +
`mainTreeProof` Merkle branches to an on-chain root).

**Anchor the head, not the whole ledger, not per-fill.** A hash chain's
head is, by construction, a commitment to every record before it — walking
`prev_hash` back from the head reproduces (and can verify) the entire
history. Anchoring it is therefore equivalent to anchoring the full ledger,
at the cost of one Memo transaction instead of one per fill. This is the
same design choice TxLINE makes anchoring a Merkle *root* over an odds
batch rather than every tick — we just apply it to our own hash chain
instead of a Merkle tree, since a chain already gives us that property for
free at our fill volumes. (At much higher volume, a batched Merkle root
over, say, every 10k records, with the head chain unchanged underneath,
would be the natural next step — the interface doesn't need to change,
only what `LedgerAnchorScheduler` feeds `AnchorSink.anchor()`.)

**Why a *new* chained record, not back-filling the historical one.**
Records are immutable the instant they're hashed into the chain — that
immutability is the whole point. An anchor transaction necessarily lands
*after* the record whose head it commits to, so there is no way to write
the resulting `tx_sig`/`slot` back into that historical record without
recomputing its hash and breaking every hash after it. Instead,
`PaperLedger.record_anchor()` appends a new `type: "anchor"` record —
itself chained onward exactly like a fill or settlement — naming the hash
it anchors (`anchored_hash`) plus the transaction signature and slot.
`verify_audit_chain()` needs no special case for it.

**Why anchoring must be structurally unable to affect trading.** This is
the same fail-safe-first doctrine as the circuit breaker (§6): quoting and
risk gates are internal, deterministic, network-free computation; on-chain
anchoring is external I/O layered strictly outside that boundary.
`AnchorSink.anchor()` is a contract that *cannot raise* — every real
failure mode (RPC timeout, devnet faucet exhausted, malformed response)
comes back as `AnchorResult(ok=False, error=...)`, and
`LedgerAnchorScheduler` simply leaves the head un-anchored and retries on
the next window. `odds_mm.demo.run` additionally wraps the scheduler call
in a belt-and-braces `try/except` even though the contract shouldn't need
it. The alternative — anchoring synchronously as part of the fill path —
would mean a slow or down RPC node could stall or crash the market maker
over a feature that exists purely for auditability, which is exactly
backwards.

**Cadence: fill-count OR wall-clock, whichever comes first.**
`AnchorPolicy(min_fills_between, min_interval_ms)` — a busy session anchors
on activity, a quiet one still gets a fresh anchor on a timer so the chain
is never silently unanchored for long stretches. The very first-ever
attempt fires as soon as there is one audit record, ignoring both
thresholds — there's no reason to wait out a warm-up window before the
chain has ever been anchored at all.

**Verification.** `verify_anchor(tx_sig, expected_hash)` independently
re-fetches the transaction from devnet (`encoding="base64"`, decoded with
`solders`), locates the Memo Program instruction, and checks its decoded
text against `odds-mm-audit-head:v1:<expected_hash>`. This is exactly what
a judge (or anyone) can run themselves from nothing but a public
transaction signature — no access to our server, database, or process
required. See `README.md`'s "On-chain audit anchoring" section for the
worked example and the current live-anchoring status.

**Why the wallet is devnet-only and file-path-only.** The anchoring wallet
holds no funds beyond what it needs to pay its own Memo transaction fee
(devnet SOL, worthless), never appears in this repository, and is never
read from an environment variable's *value* — only from a file path
(`ODDS_MM_SOLANA_MNEMONIC_FILE`), so the mnemonic itself can't leak into
`.env` files, process listings, or shell history captured elsewhere.

## 8. Simulation model

The demo feed is a seeded generative model, not recorded data:

* Latent truth: independent-Poisson remaining-goals model —
  `P(result | score, t)` from team intensities `λ_h, λ_a` (per 90'),
  scaled by remaining time; a red card multiplies own λ by 0.65 and the
  opponent's by 1.25.
* Incidents (goal times, red card) pre-sampled from `random.Random(seed)`.
* Five bookmakers quote around the truth with per-book bias, Gaussian
  noise, 4–8% multiplicative margin, and 10–25s update lag. Each book also
  has a small (1–3.5%) per-update chance of a fat-fingered/stale outlier
  tick (noise inflated 6–14x for that one update) — real multi-bookmaker
  feeds are never uniformly clean, and this is exactly the disagreement
  the consensus/confidence layer exists to shrug off.
* Taker flow is *informed on average* (private value = truth + noise),
  the adversarial case for a maker; it trades whenever our quote crosses
  its value.

No wall-clock reads anywhere in decision paths; `--speed` only paces
output. Identical seed ⇒ identical stream, fills, PnL, audit chain.
