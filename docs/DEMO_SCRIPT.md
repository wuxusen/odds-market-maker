# Demo Video Script (~5 minutes)

First-person engineer walkthrough. One command, one dashboard, one match.
Every timestamp below refers to the match clock shown on screen, not the
video's real-time clock — at `--speed 25` (the default in
`scripts/run_demo_scenario.py`) the whole 90-minute match plays out in about
3.5 minutes of wall-clock time, which is why this fits a 5-minute video with
room to talk over the quiet stretches.

Record the terminal and the browser dashboard side by side (or screen-record
the browser full-width with the terminal log visible in a small strip) —
judges should see the printed incident log and the live dashboard agree with
each other in real time.

---

## Shot 1 — Cold open (0:00–0:30)

**Screen:** terminal, empty prompt.

**Say:**
> This is an automated market maker for live sports odds — it consumes
> multiple bookmakers' in-play prices, builds a consensus fair value, and
> quotes a two-sided market against it, fully autonomously, with a circuit
> breaker that's designed to make bad fills structurally impossible rather
> than just unlikely. Everything you're about to see is paper trading — no
> real money, no exchange credentials — but the risk architecture is the
> same one I run in production on a live perpetual-futures trading stack.
> Let me start it.

**Do:** run

```bash
python scripts/run_demo_scenario.py
```

**Say (while it boots):**
> One command. No config file, no API keys, no build step — this is pure
> Python standard library, deliberately, so anyone judging this can run it
> in thirty seconds without installing anything.

## Shot 2 — Dashboard tour (0:30–1:15)

**Screen:** switch to `http://127.0.0.1:8765/`.

**Say, pointing at each panel:**
> Top left, the circuit breaker — green means active and quoting. Next to
> it, PnL and the equity curve, marked to the consensus fair price every
> tick. Then the match panel: score, red cards, clock. Below that, our live
> two-sided quotes per outcome — home, draw, away — in both probability
> space and decimal odds, because that's what a bettor actually sees.
> Positions and an exposure panel show our current risk against the hard
> caps — those caps are a line quoting math never gets to cross. And
> at the bottom, a timeline of everything that's happened — goals, red
> cards, and every time the breaker has tripped or re-armed.

## Shot 3 — Quiet market making (1:15–1:45)

**Screen:** dashboard, match clock ticking through the first couple of
minutes before kickoff incidents land.

**Say:**
> Right now, five simulated bookmakers are quoting around a true win
> probability model, each with its own bias, noise, and occasionally a
> flat-out bad tick — a fat-fingered line, like real feeds actually produce.
> My consensus layer de-vigs every book's price, and weighs them by
> freshness and cross-book agreement — when books disagree, confidence
> drops and my spread widens automatically, before anything goes wrong.
> Synthetic order flow is trading against my quotes right now, and because
> that flow is *informed on average* — it knows the true probability better
> than my quotes do — this is deliberately the hardest case for a market
> maker. I'm not demoing a favorable script.

## Shot 4 — The goal (≈ match minute 3′, first incident)

**Screen:** terminal log line `!! GOAL AWAY` appears; dashboard breaker
badge flips red simultaneously.

**Say:**
> There it is — a goal. Watch the breaker, not the score. The instant that
> event lands, every resting quote is cancelled — not repriced, cancelled —
> before the new fair value has even been computed. That ordering is the
> whole point: gate one, every cycle, is the breaker; pricing and inventory
> logic run *after*. A market maker that tries to reprice through a
> discontinuity gets picked off; one that refuses to quote at all until it's
> safe again doesn't.

## Shot 5 — Re-arm discipline (a few seconds later)

**Screen:** breaker badge sits red through the cool-down, then flips back
to green; timeline shows "circuit breaker re-armed".

**Say:**
> Re-arming isn't just a timer. It requires the cool-down to fully elapse
> *and* fresh, non-stale data to have arrived since the trip. If the feed
> went quiet right when the goal happened, time alone would never bring
> quoting back — that's a deliberate design choice: never assume you're safe
> just because the clock says so.

## Shot 6 — The comeback and the red card (minutes 13′–28′)

**Screen:** dashboard through the equalizer, the go-ahead goal, and the red
card; PnL curve keeps climbing between incidents.

**Say:**
> Home fights back — 13th minute, equalizer; 21st minute, they're ahead.
> Same reflex both times: trip, cancel, cool down, re-arm. Then, 28 minutes
> in, Home go down to ten men protecting the lead — another trip. Notice
> the PnL curve isn't flat during any of this: between incidents, the
> market maker is doing its actual job, earning spread against informed
> flow, skewing its quotes against inventory so the book pays to unwind
> risk instead of just sitting at a hard cap doing nothing.

## Shot 7 — The boring middle (30′–85′, fast-forward on camera)

**Screen:** dashboard equity curve climbing steadily, exposure gauges
staying well under their caps, no more timeline entries.

**Say:**
> This is deliberately the least exciting part of the video, and it's the
> most important — an hour of live play with zero incidents. No goals to
> react to, so the story is just: consensus pricing, confidence-scaled
> spreads, inventory hard caps holding. This is what "production-minded"
> actually means to me — not that it does something clever on camera, but
> that most of its life is this boring, and it stays inside its risk limits
> the entire time without anyone watching it.

## Shot 8 — Full time and the audit trail (final minutes)

**Screen:** terminal prints the final-time summary block.

**Say:**
> Full time. Positions settle against the actual result, PnL is finalized,
> and every fill and settlement in this match is chained into a SHA-256
> hash-linked audit log — tamper-evident, git-style. It's built with fields
> reserved to anchor the chain head on Solana, the same pattern the
> underlying odds provider uses to anchor its own price feed on-chain —
> so eventually the paper track record *and* the input data it traded on
> are both independently verifiable, not just self-reported.

## Shot 9 — Close (last 20–30s)

**Screen:** README architecture diagram or the repo file tree.

**Say:**
> The whole thing is about 1,500 lines of dependency-free Python, with a
> pytest suite covering the pricing math, every breaker trip condition, the
> exposure gates, and a full end-to-end reproducible match. Same seed always
> produces the same fills, the same PnL, the same audit chain — which is
> also why this exact scenario is one command:
> `python scripts/run_demo_scenario.py`.

---

## Notes for the person recording

* Run `python scripts/run_demo_scenario.py --speed 0 --no-dashboard` once
  beforehand to sanity-check the seeded scenario still plays out as
  described (goal 3′, goal 13′, goal 21′, red card 28′, final 2-1 Home) — if
  a future change to `SimulatedFeed` shifts the RNG stream, re-pick a seed
  with `python -c "..."` scanning `SimulatedFeed(seed=...)._incidents` before
  recording, and update this script.
* `--speed 25` is a good default for recording — fast enough that the quiet
  stretch doesn't dominate the video, slow enough that dashboard numbers
  visibly move rather than jumping. Lower it (e.g. `--speed 8`) if you want
  more screen time immediately after each incident.
* The dashboard binds to `127.0.0.1` only — no extra flags needed for local
  screen recording; do not expose it publicly.
