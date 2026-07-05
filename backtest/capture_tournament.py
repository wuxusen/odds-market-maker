#!/usr/bin/env python3
"""Batch-capture real World Cup in-play 1X2 odds timelines for the backtest.

Walks the World Cup fixture list on the TxODDS TxLINE devnet (free tier),
and for every finished fixture that actually has an in-play full-time 1X2
line it pulls the odds-update cache, applies the exact same filtering as
``demo/capture_real_odds.py`` (full-time 1X2 line only, in-running window,
5-second grid down-sample keeping a real median tick per bucket) and writes
a self-contained, credential-free replay file to
``backtest/real_odds/{fixtureId}.json``.

Fixtures are processed knockout-stage first (most recent completed matches),
which is where the richest, most dislocated in-play lines live. Fixtures with
no in-running 1X2 ticks are skipped and logged. A hard cap (default 24) keeps
the pull bounded in time and bandwidth.

Nothing written out contains a token, JWT, wallet or any other secret — only
public sporting data (team names, competition, the demarginalised book label,
real odds values and real millisecond timestamps).

Usage::

    python backtest/capture_tournament.py                 # capture up to 24
    python backtest/capture_tournament.py --max 12
    python backtest/capture_tournament.py --plan          # list candidates only
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# Reuse the exact capture filtering/constants from the demo capturer so the
# tournament pull and the single-fixture demo pull are byte-for-byte identical
# in method.
import demo.capture_real_odds as cro  # noqa: E402
from odds_mm.feeds.txline import TxLineAdapter  # noqa: E402

OUT_DIR = HERE / "real_odds"
# A fixture is treated as "finished" (safe to replay a full in-play line) once
# its kickoff is at least this far in the past.
FINISHED_AFTER_S = 3 * 3600


def _all_fixtures(adapter: TxLineAdapter) -> list[dict]:
    seen: dict[int, dict] = {}
    for ed in cro.EPOCH_DAYS:
        for f in adapter.list_fixtures(cro.WORLD_CUP_COMPETITION_ID, ed):
            seen[f["FixtureId"]] = f
    return list(seen.values())


def _finished(fx: dict, now_ms: int) -> bool:
    return (now_ms - fx["StartTime"]) >= FINISHED_AFTER_S * 1000


def _capture_one(adapter: TxLineAdapter, fx: dict) -> dict | None:
    """Pull + filter one fixture's in-play 1X2 line. None if no in-play data."""
    fixture_id = fx["FixtureId"]
    p1_home = bool(fx.get("Participant1IsHome", True))
    home = fx["Participant1"] if p1_home else fx["Participant2"]
    away = fx["Participant2"] if p1_home else fx["Participant1"]

    raw = adapter._get(f"/odds/updates/{fixture_id}")
    if not isinstance(raw, list):
        return None

    onex2 = [p for p in raw if cro._valid_1x2(p)]
    n_raw_1x2 = len(onex2)
    ft = [
        p
        for p in onex2
        if p.get("InRunning") and str(p.get("MarketPeriod")) == cro.FULLTIME_PERIOD
    ]
    ft.sort(key=lambda p: p["Ts"])
    if not ft:
        return None

    t0 = ft[0]["Ts"]
    ft = [p for p in ft if (p["Ts"] - t0) <= cro.WINDOW_MIN * 60_000]

    buckets: dict[int, list[dict]] = {}
    for p in ft:
        buckets.setdefault((p["Ts"] - t0) // cro.BUCKET_MS, []).append(p)
    ticks: list[dict] = []
    for b in sorted(buckets):
        grp = sorted(buckets[b], key=lambda p: cro._implied_home(p["Prices"]))
        chosen = grp[len(grp) // 2]
        ticks.append(
            {
                "ts": int(chosen["Ts"]),
                "prices": [int(x) for x in chosen["Prices"]],
                "in_running": True,
                "market_period": "FT",
                "message_id": str(chosen.get("MessageId", "")),
            }
        )
    if not ticks:
        return None

    bookmaker = Counter(p["Bookmaker"] for p in ft).most_common(1)[0][0]
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "meta": {
            "source": "TxODDS TxLINE devnet — World Cup free tier",
            "competition": fx.get("Competition", "World Cup"),
            "competition_id": cro.WORLD_CUP_COMPETITION_ID,
            "fixture_id": fixture_id,
            "home": home,
            "away": away,
            "bookmaker": bookmaker,
            "market": "1X2 full-time (match result)",
            "captured_at": now_iso,
            "kickoff_ts": int(t0),
            "start_time": int(fx["StartTime"]),
            "bucket_ms": cro.BUCKET_MS,
            "window_min": cro.WINDOW_MIN,
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
    ap.add_argument("--max", type=int, default=24, help="cap on fixtures captured")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--plan", action="store_true", help="print the candidate order and exit")
    ap.add_argument("--sleep", type=float, default=0.4, help="pause between pulls (politeness)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    adapter = TxLineAdapter.from_creds_file(fixture_id=0)
    now_ms = int(time.time() * 1000)
    fixtures = [f for f in _all_fixtures(adapter) if _finished(f, now_ms)]
    # Knockout stage sits at the tail of the calendar; process most-recent
    # first so the richest in-play lines land inside the cap.
    fixtures.sort(key=lambda f: f["StartTime"], reverse=True)

    if args.plan:
        for f in fixtures:
            start = datetime.datetime.fromtimestamp(
                f["StartTime"] / 1000, datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
            print(f"  {f['FixtureId']}  {start}  {f['Participant1']} v {f['Participant2']}")
        print(f"[plan] {len(fixtures)} finished fixtures; will capture up to {args.max}")
        return

    captured: list[dict] = []
    skipped: list[dict] = []
    for fx in fixtures:
        if len(captured) >= args.max:
            break
        fid = fx["FixtureId"]
        label = f"{fx['Participant1']} v {fx['Participant2']}"
        try:
            payload = _capture_one(adapter, fx)
        except Exception as exc:  # transport / shape issue: skip, keep going
            skipped.append({"fixture_id": fid, "label": label, "reason": f"error: {exc}"})
            print(f"[skip] {fid} {label}: error {exc}")
            time.sleep(args.sleep)
            continue
        if payload is None:
            skipped.append({"fixture_id": fid, "label": label, "reason": "no in-play 1X2 ticks"})
            print(f"[skip] {fid} {label}: no in-play 1X2 ticks")
            time.sleep(args.sleep)
            continue
        out = out_dir / f"{fid}.json"
        out.write_text(json.dumps(payload, indent=1))
        m = payload["meta"]
        captured.append({"fixture_id": fid, "label": label, "n_ticks": m["n_ticks"]})
        print(f"[ok]   {fid} {label}: {m['n_ticks']} ticks from {m['n_raw_1x2_ticks']} raw updates")
        time.sleep(args.sleep)

    index = {
        "captured_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_captured": len(captured),
        "n_skipped": len(skipped),
        "captured": captured,
        "skipped": skipped,
    }
    (out_dir / "_capture_index.json").write_text(json.dumps(index, indent=1))
    print(f"\ncaptured {len(captured)} fixtures, skipped {len(skipped)} → {out_dir}")


if __name__ == "__main__":
    main()
