#!/usr/bin/env python3
"""Capture a real World Cup 1X2 odds timeline from the live TxLINE feed.

Pulls the full in-play odds-update cache for one finished World Cup fixture
off the TxODDS TxLINE devnet (free World Cup tier), keeps the full-time 1X2
line only, down-samples the raw sub-second stream onto a fixed time grid, and
writes a self-contained, credential-free replay file to
``demo/real_odds_capture.json``.

Everything written out is the *real* feed: real decimal odds (fixed-point
x1000, exactly as TxLINE serves them), real millisecond timestamps, the real
bookmaker label and the real in-play moves the match produced. No token, JWT,
wallet, or other secret ever touches the output — only public sporting data
(team names, competition, the demarginalised book's public label).

Usage::

    python demo/capture_real_odds.py                         # default fixture
    python demo/capture_real_odds.py --fixture-id 18179763
    python demo/capture_real_odds.py --list                  # browse fixtures

The default fixture is a real World Cup knockout tie (Portugal v Croatia)
whose in-play line contains two clean, dramatic dislocations — a goal against
the favourite and the equaliser — which the risk engine reacts to downstream
without any hand-authored events.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odds_mm.feeds.txline import TxLineAdapter  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "real_odds_capture.json"

WORLD_CUP_COMPETITION_ID = 72
# World Cup fixtures span several devnet "epoch days"; these cover the tier.
EPOCH_DAYS = (20608, 20624, 20638)

# A real World Cup knockout tie with a clean, dramatic in-play line:
# the favourite (Portugal) is quoted ~0.57 for the win, concedes around the
# 77th minute (line collapses to ~0.21), then equalises around 91' (line
# snaps back). Two genuine goal-driven dislocations, nothing scripted.
DEFAULT_FIXTURE_ID = 18179763

# Full-time 1X2 market only. TxLINE also publishes a first-half 1X2 line under
# the same SuperOddsType (MarketPeriod "half=1"); mixing the two would look
# like phantom jumps, so we pin to the regulation full-time line.
MATCH_ODDS_SUPERTYPE = "1x2_participant_result"
FULLTIME_PERIOD = "None"

# Down-sample the dense (multi-Hz) raw stream onto a 5-second grid, taking the
# median-home-probability tick in each bucket. That strips the book's
# sub-second bid/ask flicker (a feed artefact) while preserving every real
# price level and every real move — the tick kept in each bucket is a real,
# unmodified TxLINE payload, not a synthesised average.
BUCKET_MS = 5_000
# Keep the clean regulation window (0..95'); the deep extra-time tail on this
# tie is a noisy flicker between two book states and carries no extra story.
WINDOW_MIN = 95


def _valid_1x2(payload: dict) -> bool:
    if str(payload.get("SuperOddsType", "")).lower() != MATCH_ODDS_SUPERTYPE:
        return False
    prices = payload.get("Prices") or []
    return len(prices) == 3 and all(p > 1000 for p in prices)


def _implied_home(prices: list[int]) -> float:
    inv = [1000.0 / p for p in prices]
    return inv[0] / sum(inv)


def _fixture_meta(adapter: TxLineAdapter, fixture_id: int) -> dict:
    for ed in EPOCH_DAYS:
        for f in adapter.list_fixtures(WORLD_CUP_COMPETITION_ID, ed):
            if f.get("FixtureId") == fixture_id:
                return f
    raise SystemExit(f"fixture {fixture_id} not found in World Cup tier")


def _list_fixtures(adapter: TxLineAdapter) -> None:
    seen: dict[int, dict] = {}
    for ed in EPOCH_DAYS:
        for f in adapter.list_fixtures(WORLD_CUP_COMPETITION_ID, ed):
            seen[f["FixtureId"]] = f
    for f in sorted(seen.values(), key=lambda x: x["StartTime"]):
        start = datetime.datetime.fromtimestamp(
            f["StartTime"] / 1000, datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        print(f"  {f['FixtureId']}  {start}  {f['Participant1']} v {f['Participant2']}")


def capture(fixture_id: int) -> dict:
    adapter = TxLineAdapter.from_creds_file(fixture_id=fixture_id)
    fx = _fixture_meta(adapter, fixture_id)
    p1_home = bool(fx.get("Participant1IsHome", True))
    home = fx["Participant1"] if p1_home else fx["Participant2"]
    away = fx["Participant2"] if p1_home else fx["Participant1"]

    raw = adapter._get(f"/odds/updates/{fixture_id}")
    if not isinstance(raw, list):
        raise SystemExit("unexpected odds-updates payload shape")

    onex2 = [p for p in raw if _valid_1x2(p)]
    n_raw_1x2 = len(onex2)
    ft = [
        p
        for p in onex2
        if p.get("InRunning") and str(p.get("MarketPeriod")) == FULLTIME_PERIOD
    ]
    ft.sort(key=lambda p: p["Ts"])
    if not ft:
        raise SystemExit("no in-running full-time 1X2 ticks in this fixture")

    t0 = ft[0]["Ts"]
    ft = [p for p in ft if (p["Ts"] - t0) <= WINDOW_MIN * 60_000]

    # Bucket onto the fixed grid; keep the median-home real tick per bucket.
    buckets: dict[int, list[dict]] = {}
    for p in ft:
        buckets.setdefault((p["Ts"] - t0) // BUCKET_MS, []).append(p)
    ticks: list[dict] = []
    for b in sorted(buckets):
        grp = sorted(buckets[b], key=lambda p: _implied_home(p["Prices"]))
        chosen = grp[len(grp) // 2]
        ticks.append(
            {
                "ts": int(chosen["Ts"]),
                "prices": [int(x) for x in chosen["Prices"]],  # decimal odds x1000
                "in_running": True,
                "market_period": "FT",
                "message_id": str(chosen.get("MessageId", "")),
            }
        )

    bookmaker = Counter(p["Bookmaker"] for p in ft).most_common(1)[0][0]
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "meta": {
            "source": "TxODDS TxLINE devnet — World Cup free tier",
            "competition": fx.get("Competition", "World Cup"),
            "competition_id": WORLD_CUP_COMPETITION_ID,
            "fixture_id": fixture_id,
            "home": home,
            "away": away,
            "bookmaker": bookmaker,
            "market": "1X2 full-time (match result)",
            "captured_at": now_iso,
            "kickoff_ts": int(t0),
            "bucket_ms": BUCKET_MS,
            "window_min": WINDOW_MIN,
            "n_raw_1x2_ticks": n_raw_1x2,
            "n_ticks": len(ticks),
            "price_scale": 1000,
            "tier_note": (
                "Devnet free tier: a single demarginalised synthetic book "
                "(public label above) with ~60s delay. The odds values, "
                "millisecond timestamps and in-play moves below are the real "
                "TxLINE World Cup feed; no token or secret is stored here."
            ),
        },
        "ticks": ticks,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture-id", type=int, default=DEFAULT_FIXTURE_ID)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--list", action="store_true", help="list World Cup fixtures and exit")
    args = ap.parse_args()

    if args.list:
        _list_fixtures(TxLineAdapter.from_creds_file(fixture_id=0))
        return

    payload = capture(args.fixture_id)
    Path(args.out).write_text(json.dumps(payload, indent=1))
    m = payload["meta"]
    print(
        f"wrote {args.out}: {m['home']} v {m['away']} "
        f"({m['n_ticks']} ticks from {m['n_raw_1x2_ticks']} raw 1X2 updates, "
        f"{BUCKET_MS // 1000}s grid, {WINDOW_MIN}' window)"
    )


if __name__ == "__main__":
    main()
