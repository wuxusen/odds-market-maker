"""TxODDS TxLINE adapter — live on/off-chain in-play odds feed.

Endpoints, headers and payload fields below are taken from the published
TxLINE OpenAPI spec (``https://txline-dev.txodds.com/docs/docs.yaml``) and
confirmed against live devnet World Cup data:

* Base URLs:
    mainnet: https://txline.txodds.com/api
    devnet:  https://txline-dev.txodds.com/api
* Auth: dual token —
    1. ``POST /auth/guest/start``   -> short-lived (30 day) guest session JWT.
       Renewable anonymously — no re-subscription needed.
    2. ``POST /api/token/activate`` -> long-lived API token, tied to an
       on-chain ``subscribe(serviceLevelId, weeks)`` transaction on the
       TxLINE anchor program (free World Cup tier charges 0 TxL). Activation
       posts ``{txSig, walletSignature, leagues}`` with the JWT as Bearer,
       where ``walletSignature`` is ed25519 over ``"{txSig}:{leagues_csv}:{jwt}"``.
    Every data request carries BOTH headers:
        Authorization: Bearer <jwt>
        X-Api-Token:   <apiToken>
    The on-chain subscribe + activation is done once out-of-band (see
    ``.txline-build/auth.js``); this adapter just consumes the resulting
    credentials, which are read from a local creds file — never from source.
* Data endpoints (all GET):
    /api/fixtures/snapshot?competitionId=&startEpochDay=
    /api/odds/snapshot/{fixtureId}?asOf=<ms>
    /api/odds/updates/{fixtureId}                      (live cache)
    /api/odds/stream?fixtureId=<id>                    (SSE, Last-Event-ID resume)

Confirmed against live devnet data (World Cup, competitionId 72):

* The 1X2 (match-odds) market is ``SuperOddsType == "1X2_PARTICIPANT_RESULT"``
  with ``PriceNames == ["part1", "draw", "part2"]`` (home / draw / away).
* ``Prices`` are fixed-point decimal odds x1000 (2264 -> 2.264). Verified
  against the sibling ``Pct`` field: 1/2.264 = 0.44170 == "44.170".
* Devnet demo quotes come from a single demarginalised synthetic book,
  ``"TXLineStablePriceDemargined"`` (BookmakerId 10021); the consensus/
  de-vig layer downstream handles one *or* many books unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator, Optional

from ..types import Market, MATCH_ODDS_OUTCOMES, OddsTick
from .base import FeedAdapter, FeedItem

log = logging.getLogger(__name__)

API_ORIGIN_DEVNET = "https://txline-dev.txodds.com"
API_ORIGIN_MAINNET = "https://txline.txodds.com"
MAINNET_BASE = f"{API_ORIGIN_MAINNET}/api"
DEVNET_BASE = f"{API_ORIGIN_DEVNET}/api"
GUEST_START_PATH = "/auth/guest/start"

# Default location of the credentials file produced by the one-off auth flow
# (see ``.txline-build/auth.js``). Contains the guest JWT + activated API
# token; kept outside the repo and git-ignored. Overridable via the
# ``TXLINE_CREDS_FILE`` environment variable.
ENV_CREDS_FILE = "TXLINE_CREDS_FILE"
DEFAULT_CREDS_PATH = os.path.expanduser("~/.wallets/.txline_creds.json")

# OddsPayload.Prices are decimal odds x1000 (confirmed against live Pct field).
PRICE_SCALE = 1000.0

# SuperOddsType values that denote the 1X2 / match-odds market. The historical
# ``"1x2"`` literal is kept for backward compatibility with early fixtures/tests;
# live devnet World Cup data uses ``"1X2_PARTICIPANT_RESULT"``.
MATCH_ODDS_TYPES = frozenset({"1x2", "1x2_participant_result"})

# Live PriceNames use participant-relative labels; normalise to canonical
# HOME / DRAW / AWAY (already-canonical names pass through unchanged).
_PRICE_NAME_MAP = {
    "part1": "HOME",
    "draw": "DRAW",
    "part2": "AWAY",
    "home": "HOME",
    "away": "AWAY",
    "1": "HOME",
    "x": "DRAW",
    "2": "AWAY",
}


def _normalise_outcome(name: str) -> str:
    return _PRICE_NAME_MAP.get(str(name).strip().lower(), str(name).upper())


def load_credentials(path: Optional[str] = None) -> dict:
    """Read the TxLINE creds JSON (guest JWT + activated API token).

    Resolution order: explicit ``path`` -> ``$TXLINE_CREDS_FILE`` ->
    :data:`DEFAULT_CREDS_PATH`. Raises ``FileNotFoundError`` if none exists,
    so callers can cleanly fall back to :class:`SimulatedFeed` for offline
    demos. The secret values are never logged by this module.
    """
    resolved = path or os.environ.get(ENV_CREDS_FILE) or DEFAULT_CREDS_PATH
    with open(resolved, "r", encoding="utf-8") as fh:
        creds = json.load(fh)
    if not creds.get("jwt") or not creds.get("apiToken"):
        raise ValueError(f"creds file {resolved!r} missing jwt/apiToken")
    return creds


class TxLineAdapter(FeedAdapter):
    """Pull/stream adapter for the TxLINE hybrid on/off-chain data service.

    Payload normalisation (:meth:`parse_odds_payload`) is a pure function and
    unit-testable offline; the authenticated transport reads credentials from
    a local creds file (never from source). Use :class:`SimulatedFeed` when no
    credentials are provisioned.
    """

    def __init__(
        self,
        fixture_id: int,
        jwt: Optional[str] = None,
        api_token: Optional[str] = None,
        base_url: str = DEVNET_BASE,
        poll_interval_s: float = 2.0,
        as_of: Optional[int] = None,
    ) -> None:
        self.fixture_id = fixture_id
        self._jwt = jwt
        self._api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.poll_interval_s = poll_interval_s
        self.as_of = as_of
        self._closed = False

    @classmethod
    def from_creds_file(
        cls,
        fixture_id: int,
        path: Optional[str] = None,
        poll_interval_s: float = 2.0,
        as_of: Optional[int] = None,
    ) -> "TxLineAdapter":
        """Build an authenticated adapter from a saved creds file.

        Uses the ``apiBaseUrl`` recorded in the creds file when present so the
        adapter targets the same network the subscription was activated on.
        """
        creds = load_credentials(path)
        base_url = creds.get("apiBaseUrl", DEVNET_BASE)
        return cls(
            fixture_id=fixture_id,
            jwt=creds["jwt"],
            api_token=creds["apiToken"],
            base_url=base_url,
            poll_interval_s=poll_interval_s,
            as_of=as_of,
        )

    @property
    def _origin(self) -> str:
        # Strip a trailing ``/api`` to reach the auth origin for guest/start.
        return self.base_url[: -len("/api")] if self.base_url.endswith("/api") else self.base_url

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self._jwt or not self._api_token:
            raise RuntimeError(
                "TxLineAdapter needs a guest JWT and an activated API token. "
                "Use TxLineAdapter.from_creds_file(...) or SimulatedFeed for "
                "offline demos. See the module docstring for the auth flow."
            )
        return {
            "Authorization": f"Bearer {self._jwt}",
            "X-Api-Token": self._api_token,
            "Accept": "application/json",
        }

    def _renew_jwt(self) -> None:
        """Re-acquire an anonymous guest JWT (the API token stays valid).

        The guest session token expires after 30 days but is renewable with
        no re-subscription — a plain POST to ``/auth/guest/start``.
        """
        req = urllib.request.Request(self._origin + GUEST_START_PATH, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        self._jwt = data["token"]
        log.info("txline: renewed guest JWT")

    def _get(self, path: str, _retried: bool = False) -> object:
        req = urllib.request.Request(self.base_url + path, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 401 => expired guest JWT: renew once and retry transparently.
            if exc.code == 401 and not _retried:
                self._renew_jwt()
                return self._get(path, _retried=True)
            raise

    # -- data access -------------------------------------------------------

    def list_fixtures(self, competition_id: int, start_epoch_day: int) -> list[dict]:
        """List fixtures for a competition (e.g. World Cup ``competitionId=72``)."""
        q = urllib.parse.urlencode(
            {"competitionId": competition_id, "startEpochDay": start_epoch_day}
        )
        data = self._get(f"/fixtures/snapshot?{q}")
        return data if isinstance(data, list) else []

    def fetch_odds_snapshot(
        self, fixture_id: Optional[int] = None, as_of: Optional[int] = None
    ) -> list[dict]:
        """Raw ``OddsPayload`` list for a fixture as of a timestamp (ms)."""
        fid = fixture_id if fixture_id is not None else self.fixture_id
        as_of = as_of if as_of is not None else self.as_of
        path = f"/odds/snapshot/{fid}"
        if as_of is not None:
            path += f"?asOf={int(as_of)}"
        data = self._get(path)
        if isinstance(data, list):
            return data
        return [data] if isinstance(data, dict) else []

    def match_odds_snapshot(
        self, fixture_id: Optional[int] = None, as_of: Optional[int] = None
    ) -> list[OddsTick]:
        """1X2 ticks only, normalised. Non-1X2 markets are dropped."""
        ticks = [self.parse_odds_payload(p) for p in self.fetch_odds_snapshot(fixture_id, as_of)]
        return [t for t in ticks if t is not None]

    # -- normalisation -----------------------------------------------------

    @staticmethod
    def parse_odds_payload(payload: dict) -> Optional[OddsTick]:
        """Normalise a TxLINE ``OddsPayload`` dict into an :class:`OddsTick`.

        Returns ``None`` for market types we do not trade yet (anything but
        1X2) or malformed/suspended lines. Pure function — unit-testable with
        no network access.
        """
        if str(payload.get("SuperOddsType", "")).lower() not in MATCH_ODDS_TYPES:
            return None  # TODO: handicap & totals markets
        prices_raw = payload.get("Prices") or []
        if len(prices_raw) != 3 or any(p <= PRICE_SCALE for p in prices_raw):
            return None  # malformed or suspended line (odds must be > 1.0)
        names = payload.get("PriceNames") or list(MATCH_ODDS_OUTCOMES)
        outcomes = tuple(_normalise_outcome(n) for n in names)
        return OddsTick(
            fixture_id=int(payload["FixtureId"]),
            message_id=str(payload["MessageId"]),
            ts=int(payload["Ts"]),
            bookmaker=str(payload["Bookmaker"]),
            bookmaker_id=int(payload["BookmakerId"]),
            market=Market.MATCH_ODDS,
            outcomes=outcomes,
            prices=tuple(p / PRICE_SCALE for p in prices_raw),
            in_running=bool(payload.get("InRunning", False)),
            market_period=str(payload.get("MarketPeriod") or "FT"),
            market_parameters=str(payload.get("MarketParameters") or ""),
        )

    # -- FeedAdapter -------------------------------------------------------

    def stream(self) -> Iterator[FeedItem]:
        """Poll ``/api/odds/updates/{fixtureId}``, yielding new 1X2 ticks.

        Falls back to ``/api/odds/snapshot`` if the updates cache is empty
        (e.g. pre-match). TODO: switch to ``GET /api/odds/stream`` (SSE) with
        Last-Event-ID resume for lower latency once needed; polling is the
        resilient fallback path.
        """
        seen: set[str] = set()
        while not self._closed:
            try:
                batch = self._get(f"/odds/updates/{self.fixture_id}")
            except urllib.error.URLError as exc:
                # Transport failure: surface as a gap (the circuit breaker
                # watchdog handles stale streams); back off and retry.
                log.warning("txline: updates fetch failed: %s", exc)
                time.sleep(self.poll_interval_s)
                continue
            payloads = batch if isinstance(batch, list) else []
            if not payloads:
                payloads = self.fetch_odds_snapshot()
            ticks = []
            for payload in payloads:
                tick = self.parse_odds_payload(payload)
                if tick is not None and tick.message_id not in seen:
                    seen.add(tick.message_id)
                    ticks.append(tick)
            for tick in sorted(ticks, key=lambda t: t.ts):
                yield tick
            time.sleep(self.poll_interval_s)

    def fetch_validation_proof(self, message_id: str, ts: int) -> dict:
        """Fetch the Merkle proof anchoring one odds update to Solana.

        ``GET /api/fixtures/validation`` returns the sub-tree / main-tree
        proofs reconstructing the on-chain Merkle root committed by the TxODDS
        oracle program, so every fill's input data stays verifiable.
        """
        q = urllib.parse.urlencode({"messageId": message_id, "ts": ts})
        return self._get(f"/fixtures/validation?{q}")  # type: ignore[return-value]

    def close(self) -> None:
        self._closed = True
