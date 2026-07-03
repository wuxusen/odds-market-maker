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

## 7. Ledger & Solana anchoring plan

Every fill/settlement appends a JSON audit record containing the previous
record's SHA-256 (`prev_hash`), forming a tamper-evident chain
(`verify_audit_chain()` re-derives it). Each record reserves an `anchor`
block:

```json
"anchor": { "solana_tx_sig": null, "solana_slot": null, "merkle_root": null }
```

Planned iteration: periodically commit the chain head (or a Merkle root of
a record batch) to Solana in a memo transaction — the same pattern TxLINE
uses to anchor odds batches (`/api/odds/validation` returns `subTreeProof` +
`mainTreeProof` Merkle branches to the on-chain root). Result: the paper
track record and its *input data* are both third-party verifiable.

## 8. Simulation model

The demo feed is a seeded generative model, not recorded data:

* Latent truth: independent-Poisson remaining-goals model —
  `P(result | score, t)` from team intensities `λ_h, λ_a` (per 90'),
  scaled by remaining time; a red card multiplies own λ by 0.65 and the
  opponent's by 1.25.
* Incidents (goal times, red card) pre-sampled from `random.Random(seed)`.
* Five bookmakers quote around the truth with per-book bias, Gaussian
  noise, 4–8% multiplicative margin, and 10–25s update lag.
* Taker flow is *informed on average* (private value = truth + noise),
  the adversarial case for a maker; it trades whenever our quote crosses
  its value.

No wall-clock reads anywhere in decision paths; `--speed` only paces
output. Identical seed ⇒ identical stream, fills, PnL, audit chain.
