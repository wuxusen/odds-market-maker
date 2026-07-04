"""Live TxLINE devnet smoke test.

Skipped by default: it only runs when a real credentials file is present
(``~/.wallets/.txline_creds.json`` or ``$TXLINE_CREDS_FILE``), so CI and
offline runs stay green and network-free. When creds exist it performs a
minimal end-to-end pull against the devnet free World Cup tier: list
fixtures, then normalise a real 1X2 odds snapshot into an OddsTick.
"""

import os

import pytest

from odds_mm.feeds.txline import DEFAULT_CREDS_PATH, ENV_CREDS_FILE, TxLineAdapter
from odds_mm.types import Market, OddsTick

WORLD_CUP_COMPETITION_ID = 72
WORLD_CUP_START_EPOCH_DAY = 20624


def _creds_path():
    return os.environ.get(ENV_CREDS_FILE) or DEFAULT_CREDS_PATH


pytestmark = pytest.mark.skipif(
    not os.path.exists(_creds_path()),
    reason="no TxLINE creds file; live smoke test skipped (offline default)",
)


def test_live_devnet_fixtures_and_odds_snapshot():
    adapter = TxLineAdapter.from_creds_file(fixture_id=0)

    fixtures = adapter.list_fixtures(WORLD_CUP_COMPETITION_ID, WORLD_CUP_START_EPOCH_DAY)
    assert fixtures, "expected at least one World Cup fixture"
    assert all("FixtureId" in f for f in fixtures)

    # Scan fixtures/timestamps until a real 1X2 tick is found (demo data is
    # sparse and pre-match, so not every fixture/asOf has an active line).
    now_ms = 1_790_000_000_000
    found = None
    for f in fixtures[:8]:
        for as_of in (now_ms, 1_779_862_000_000):
            ticks = adapter.match_odds_snapshot(fixture_id=f["FixtureId"], as_of=as_of)
            if ticks:
                found = ticks[0]
                break
        if found:
            break

    assert found is not None, "no 1X2 odds returned from any probed fixture"
    assert isinstance(found, OddsTick)
    assert found.market is Market.MATCH_ODDS
    assert len(found.prices) == 3 and all(p > 1.0 for p in found.prices)
    assert tuple(found.outcomes) == ("HOME", "DRAW", "AWAY")
