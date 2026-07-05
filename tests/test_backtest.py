"""Offline regression tests for the tournament backtest.

Replays the committed real-odds captures (``backtest/real_odds/*.json`` —
odds/timestamps only, no secrets) through the full pricing / market-making /
breaker / paper-ledger stack and asserts the risk-engine invariants that the
published backtest report leans on: exposure never breaches the hard cap,
every breaker trip is driven by a real recorded line move, and the audit
chain verifies. Fully deterministic and network-free.
"""

import importlib.util
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REAL_ODDS = REPO / "backtest" / "real_odds"
RESULTS = REPO / "backtest" / "tournament_results.json"

_spec = importlib.util.spec_from_file_location(
    "run_backtest", REPO / "backtest" / "run_backtest.py"
)
run_backtest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_backtest)

_CAPTURES = sorted(p for p in REAL_ODDS.glob("*.json") if not p.name.startswith("_"))


def test_captures_exist():
    assert len(_CAPTURES) >= 12, "expected a tournament's worth of captures"


def test_captures_are_credential_free_and_real():
    for path in _CAPTURES:
        blob = json.dumps(json.loads(path.read_text())).lower()
        for marker in ("jwt", "apitoken", "api-token", "bearer ", "authorization",
                       "mnemonic", "private key", "x-api"):
            assert marker not in blob, f"{path.name} contains {marker!r}"
        assert not re.search(r"ey[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}\.", blob), \
            f"JWT-like string leaked in {path.name}"
        data = json.loads(path.read_text())
        assert data["meta"]["competition"] == "World Cup"
        assert data["ticks"], f"{path.name} has no ticks"
        for tk in data["ticks"]:
            assert len(tk["prices"]) == 3
            assert all(p > 1000 for p in tk["prices"])


def test_backtest_invariants_hold_across_the_tournament():
    payload = run_backtest.run(REAL_ODDS)
    s = payload["summary"]

    # The risk engine's core promise: exposure never breaches the hard cap,
    # on any fixture, on any tick.
    assert s["exposure_within_cap_all"] is True
    assert s["n_exposure_breaches"] == 0
    assert s["max_fixture_exposure_seen"] <= s["exposure_cap"]

    # Every breaker trip must be explained by a real recorded line dislocation.
    assert s["all_trips_from_real_moves"] is True

    # Tamper-evident audit chain verifies for every fixture.
    assert s["audit_ok_all"] is True

    # Sanity: real fills booked, per-fixture metrics well-formed.
    assert s["total_fills"] > 0
    for r in payload["fixtures"]:
        assert r["n_fills"] >= 0
        # No settlement leg on this feed, so every audit record is a fill.
        assert r["audit_len"] == r["n_fills"]
        assert r["max_fixture_exposure"] <= r["exposure_cap"]
        assert len(r["audit_head"]) == 64


def test_committed_results_match_a_fresh_replay():
    """The committed results file must be reproducible from the captures."""
    assert RESULTS.exists(), "run backtest/run_backtest.py to generate results"
    committed = json.loads(RESULTS.read_text())
    fresh = run_backtest.run(REAL_ODDS)
    assert committed["summary"]["n_fixtures"] == fresh["summary"]["n_fixtures"]
    assert committed["summary"]["total_pnl_mtm"] == fresh["summary"]["total_pnl_mtm"]
    assert committed["summary"]["total_breaker_trips"] == fresh["summary"]["total_breaker_trips"]
