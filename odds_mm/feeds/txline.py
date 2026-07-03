"""TxODDS TxLINE adapter (skeleton, v0.1).

Endpoints, headers and payload fields below are taken verbatim from the
published TxLINE OpenAPI spec (https://txline.txodds.com/docs/docs.yaml,
retrieved 2026-07) and quickstart docs. What is real:

* Base URLs:
    mainnet: https://txline.txodds.com/api
    devnet:  https://txline-dev.txodds.com/api
* Auth: dual token —
    1. ``POST /auth/guest/start``  -> short-lived guest session JWT
    2. ``POST /api/token/activate`` -> long-lived API token; requires a
       Solana wallet signature over ``"{txSig}:{leagues_csv}:{jwt}"`` after
       an on-chain subscription (``subscribe(serviceLevelId, durationWeeks,
       leagues)`` on the TxLINE anchor program; free World Cup tier exists).
    Every data request carries BOTH headers:
        Authorization: Bearer <jwt>
        X-Api-Token:   <apiToken>
* Data endpoints (all GET):
    /api/fixtures/snapshot
    /api/odds/snapshot/{fixtureId}?asOf=<ms>
    /api/odds/updates/{fixtureId}                      (live 5-min cache)
    /api/odds/updates/{epochDay}/{hourOfDay}/{interval} (historical replay)
    /api/odds/stream?fixtureId=<id>                    (SSE, Last-Event-ID resume)
    /api/scores/snapshot/{fixtureId}
    /api/scores/stream
    /api/odds/validation?messageId=&ts=   (Merkle proof vs on-chain root)
* OddsPayload fields (verbatim): FixtureId, MessageId, Ts, Bookmaker,
  BookmakerId, SuperOddsType, GameState, InRunning, MarketParameters,
  MarketPeriod, PriceNames[], Prices[] (int), Pct[] (str, 3dp).
  ``Pct`` example 52.632 alongside integer Prices implies Prices are
  fixed-point decimal odds x1000 (1900 -> 1.90); confirm on first live pull.

TODO (needs live credentials / hackathon key):
* Wallet message signing for /api/token/activate (solders/solana-py or a
  pre-provisioned token from the hackathon organisers).
* Confirm Prices scaling factor and SuperOddsType vocabulary ("1x2", AH, OU).
* Map ScoresPayload (sport-specific SoccerData/SoccerFixtureScore) fields.
* SSE reconnect with Last-Event-ID and jittered backoff.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Iterator, Optional

from ..types import Market, MATCH_ODDS_OUTCOMES, OddsTick
from .base import FeedAdapter, FeedItem

log = logging.getLogger(__name__)

MAINNET_BASE = "https://txline.txodds.com/api"
DEVNET_BASE = "https://txline-dev.txodds.com/api"

# Assumed fixed-point scale for OddsPayload.Prices (decimal odds x1000).
# TODO: verify against a live payload before first real run.
PRICE_SCALE = 1000.0

# SuperOddsType value for the 1X2 / match-odds market.
# TODO: confirm exact vocabulary from live data ("1x2" assumed).
SUPER_ODDS_TYPE_1X2 = "1x2"


class TxLineAdapter(FeedAdapter):
    """Pull/stream adapter for the TxLINE hybrid on/off-chain data service.

    v0.1 implements payload normalisation and the polling skeleton; the
    authenticated transport is stubbed until credentials are provisioned.
    Never put tokens in code — read them from the environment at call sites.
    """

    def __init__(
        self,
        fixture_id: int,
        jwt: Optional[str] = None,
        api_token: Optional[str] = None,
        base_url: str = DEVNET_BASE,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.fixture_id = fixture_id
        self._jwt = jwt
        self._api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.poll_interval_s = poll_interval_s
        self._closed = False

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self._jwt or not self._api_token:
            raise RuntimeError(
                "TxLineAdapter needs a guest JWT and an activated API token. "
                "See module docstring for the auth flow; use SimulatedFeed "
                "for offline demos."
            )
        return {
            "Authorization": f"Bearer {self._jwt}",
            "X-Api-Token": self._api_token,
            "Accept": "application/json",
        }

    def _get(self, path: str) -> object:
        req = urllib.request.Request(self.base_url + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    # -- normalisation -----------------------------------------------------

    @staticmethod
    def parse_odds_payload(payload: dict) -> Optional[OddsTick]:
        """Normalise a TxLINE ``OddsPayload`` dict into an :class:`OddsTick`.

        Returns ``None`` for market types we do not trade yet. Pure function
        so it is unit-testable without any network access.
        """
        if payload.get("SuperOddsType", "").lower() != SUPER_ODDS_TYPE_1X2:
            return None  # TODO: handicap & totals markets
        prices_raw = payload.get("Prices") or []
        if len(prices_raw) != 3 or any(p <= PRICE_SCALE for p in prices_raw):
            return None  # malformed or suspended line
        names = payload.get("PriceNames") or list(MATCH_ODDS_OUTCOMES)
        return OddsTick(
            fixture_id=int(payload["FixtureId"]),
            message_id=str(payload["MessageId"]),
            ts=int(payload["Ts"]),
            bookmaker=str(payload["Bookmaker"]),
            bookmaker_id=int(payload["BookmakerId"]),
            market=Market.MATCH_ODDS,
            outcomes=tuple(names),
            prices=tuple(p / PRICE_SCALE for p in prices_raw),
            in_running=bool(payload.get("InRunning", False)),
            market_period=str(payload.get("MarketPeriod", "FT")),
            market_parameters=str(payload.get("MarketParameters", "")),
        )

    # -- FeedAdapter -------------------------------------------------------

    def stream(self) -> Iterator[FeedItem]:
        """Poll ``/api/odds/updates/{fixtureId}``, yielding new ticks.

        TODO: switch to ``GET /api/odds/stream`` (SSE) with Last-Event-ID
        resume once transport auth is wired; polling is the fallback path.
        """
        import time

        seen: set[str] = set()
        while not self._closed:
            batch = self._get(f"/odds/updates/{self.fixture_id}")
            ticks = []
            for payload in batch if isinstance(batch, list) else []:
                tick = self.parse_odds_payload(payload)
                if tick is not None and tick.message_id not in seen:
                    seen.add(tick.message_id)
                    ticks.append(tick)
            for tick in sorted(ticks, key=lambda t: t.ts):
                yield tick
            # TODO: interleave /api/scores/stream for ScoreTick + MatchEvent.
            time.sleep(self.poll_interval_s)

    def fetch_validation_proof(self, message_id: str, ts: int) -> dict:
        """Fetch the Merkle proof anchoring one odds update to Solana.

        ``GET /api/odds/validation`` returns ``{odds, summary, subTreeProof,
        mainTreeProof}`` where the proofs are lists of ``{hash,
        isRightSibling}`` nodes reconstructing the on-chain Merkle root
        committed by the TxODDS oracle program. The ledger stores this next
        to the audit record so every fill's input data is verifiable.
        """
        return self._get(f"/odds/validation?messageId={message_id}&ts={ts}")  # type: ignore[return-value]

    def close(self) -> None:
        self._closed = True
