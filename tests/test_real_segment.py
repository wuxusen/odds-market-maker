"""Offline regression test for the real-TxLINE-odds demo segment.

Replays the committed real odds capture (``demo/real_odds_capture.json`` —
odds/timestamps only, no secrets) through the full pricing / market-making /
breaker / paper-ledger stack and asserts the engine produces the expected
real-data story: two genuine goal-driven circuit-breaker trips, real fills,
and a well-formed hash-chained audit trail. Fully deterministic and
network-free (the capture is a static file), so it gates the demo pipeline in
CI without touching the live feed.
"""

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CAPTURE = REPO / "demo" / "real_odds_capture.json"

# Load demo/gen_frames.py by path (the demo/ dir is not an importable package).
_spec = importlib.util.spec_from_file_location("gen_frames", REPO / "demo" / "gen_frames.py")
gen_frames = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_frames)


def test_real_capture_is_credential_free_and_real():
    data = json.loads(CAPTURE.read_text())
    # The prose fields legitimately talk *about* secrets; scan only the values
    # that could carry a real credential (auth tokens / wallet material) and
    # look for the actual markers, not the English words.
    blob = json.dumps(data).lower()
    for marker in ("jwt", "apitoken", "api-token", "bearer ", "authorization",
                   "mnemonic", "private key", "x-api"):
        assert marker not in blob, f"capture unexpectedly contains {marker!r}"
    # A guest JWT is three base64url segments joined by dots; assert none leaked.
    import re
    assert not re.search(r"ey[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}\.", blob), "JWT-like string leaked"
    assert data["meta"]["competition"] == "World Cup"
    assert data["ticks"], "capture has no real odds ticks"
    # Every stored price is a real decimal-odds-x1000 integer strictly > 1.0.
    for tk in data["ticks"]:
        assert len(tk["prices"]) == 3
        assert all(p > 1000 for p in tk["prices"])


def test_real_segment_replay_is_deterministic_and_trips_on_real_goals():
    raw, final, meta = gen_frames.run_real_segment(CAPTURE)

    # The two real in-play dislocations (goal against the favourite, then the
    # equaliser) must each trip the breaker — no hand-authored events.
    assert final["breaker"]["trip_count"] == 2

    # Real fills booked against the real line, into a verifiable audit chain.
    assert final["n_fills"] > 0
    assert final["audit_len"] == final["n_fills"]
    assert len(final["audit_head"]) == 64  # sha256 hex

    # State captured for every real tick plus the closing frame.
    assert len(raw) == len(json.loads(CAPTURE.read_text())["ticks"]) + 1
    assert meta["home"] and meta["away"]
