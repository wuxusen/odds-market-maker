"""Paper-trading ledger: fills, cash, mark-to-market PnL, audit trail.

Accounting model (binary outcome contracts, probability-space prices):

* We SELL a contract at price ``p`` for qty ``q``  -> cash += p*q, position -q.
* We BUY  a contract at price ``p`` for qty ``q``  -> cash -= p*q, position +q.
* Mark-to-market: equity = cash + sum(position_qty * fair_prob).
* Settlement: the winning outcome's contracts pay 1.0, all others 0.0.

Audit trail: every fill is appended as a JSON record carrying a SHA-256
hash chained to the previous record (tamper-evident, git-style). The
rolling chain head is periodically committed to Solana devnet in a Memo
Program transaction by ``odds_mm.anchor`` (``SolanaAnchor`` +
``LedgerAnchorScheduler``), mirroring how TxLINE itself anchors odds
batches on-chain — see :meth:`PaperLedger.record_anchor` and
``docs/DESIGN.md`` section 7. A judge (or anyone) can independently verify
the paper track record was not rewritten after the fact from nothing more
than a transaction signature.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

from ..types import Fill, Side


@dataclass
class _Pos:
    qty: float = 0.0


class PaperLedger:
    """Cash + positions + tamper-evident audit log. No real money, ever."""

    def __init__(self, starting_cash: float = 1_000.0) -> None:
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[tuple[int, str], float] = {}  # (fixture, outcome) -> qty
        self.fills: list[Fill] = []
        self.audit: list[dict] = []
        self._chain_head = "0" * 64  # genesis
        self.settled_fixtures: dict[int, str] = {}
        self.equity_curve: list[tuple[int, float]] = []  # (ts, equity)

    # -- trading ---------------------------------------------------------

    def record_fill(self, fill: Fill) -> dict:
        """Book a fill and append a chained audit record. Returns the record."""
        key = (fill.fixture_id, fill.outcome)
        signed = fill.qty if fill.side == Side.BID else -fill.qty
        self.positions[key] = self.positions.get(key, 0.0) + signed
        self.cash += -fill.price * fill.qty if fill.side == Side.BID else fill.price * fill.qty
        self.fills.append(fill)
        return self._append_audit(
            {
                "type": "fill",
                "fill_id": fill.fill_id,
                "ts": fill.ts,
                "fixture_id": fill.fixture_id,
                "market": fill.market.value,
                "outcome": fill.outcome,
                "side": fill.side.value,
                "price": fill.price,
                "qty": fill.qty,
                "counterparty": fill.counterparty,
                "cash_after": round(self.cash, 6),
            }
        )

    def settle(self, fixture_id: int, winning_outcome: str, ts: int) -> float:
        """Settle all legs of a fixture. Returns the settlement cash flow."""
        if fixture_id in self.settled_fixtures:
            raise ValueError(f"fixture {fixture_id} already settled")
        flow = 0.0
        for (fid, outcome), qty in list(self.positions.items()):
            if fid != fixture_id:
                continue
            payout = qty * (1.0 if outcome == winning_outcome else 0.0)
            flow += payout
            del self.positions[(fid, outcome)]
        self.cash += flow
        self.settled_fixtures[fixture_id] = winning_outcome
        self._append_audit(
            {
                "type": "settlement",
                "ts": ts,
                "fixture_id": fixture_id,
                "winning_outcome": winning_outcome,
                "cash_flow": round(flow, 6),
                "cash_after": round(self.cash, 6),
            }
        )
        return flow

    # -- valuation ---------------------------------------------------------

    def equity(self, fair_probs: dict[tuple[int, str], float]) -> float:
        """Cash plus positions marked at the supplied fair probabilities.

        Unmarked open positions are conservatively valued at 0 — an unknown
        mark is treated as worthless rather than optimistically carried.
        """
        mtm = sum(
            qty * fair_probs.get(key, 0.0) for key, qty in self.positions.items()
        )
        return self.cash + mtm

    def mark(self, ts: int, fair_probs: dict[tuple[int, str], float]) -> float:
        eq = self.equity(fair_probs)
        self.equity_curve.append((ts, eq))
        return eq

    @property
    def pnl(self) -> float:
        """Realised-only PnL when flat; call :meth:`equity` for MTM."""
        return self.cash - self.starting_cash

    def record_anchor(
        self, tx_sig: Optional[str], slot: Optional[int], anchored_hash: str, ts: int
    ) -> dict:
        """Append an on-chain anchor confirmation as a first-class chain
        record (see ``odds_mm.anchor`` and ``docs/DESIGN.md`` section 7).

        Records are immutable once hashed into the chain, so an anchor that
        lands *after* the fact cannot be written back into the record it
        anchors — instead it is a new ``type: "anchor"`` entry, chained
        onward like any other, that names the hash it commits
        (``anchored_hash``). ``verify_audit_chain`` needs no special case
        for it; ``odds_mm.anchor.solana_anchor.verify_anchor`` independently
        cross-checks it against the live Solana transaction.
        """
        return self._append_audit(
            {
                "type": "anchor",
                "ts": ts,
                "anchored_hash": anchored_hash,
                "solana_tx_sig": tx_sig,
                "solana_slot": slot,
            }
        )

    @property
    def chain_head(self) -> str:
        """Current audit-chain head hash — what an :class:`AnchorSink` commits."""
        return self._chain_head

    # -- audit chain ---------------------------------------------------------

    def _append_audit(self, record: dict) -> dict:
        record = dict(record)
        record["prev_hash"] = self._chain_head
        if record.get("type") != "anchor":
            # Reserved on fill/settlement records for symmetry with the wire
            # schema; always null. The actual anchor is committed as its own
            # chained ``type: "anchor"`` record via :meth:`record_anchor` —
            # records are immutable once hashed, so this placeholder can
            # never be filled in after the fact without breaking the chain.
            record["anchor"] = {
                "solana_tx_sig": None,
                "solana_slot": None,
                "merkle_root": None,
            }
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
        record["hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self._chain_head = record["hash"]
        self.audit.append(record)
        return record

    def verify_audit_chain(self) -> bool:
        """Recompute the hash chain; True iff no record was tampered with."""
        prev = "0" * 64
        for rec in self.audit:
            body = {k: v for k, v in rec.items() if k != "hash"}
            if body.get("prev_hash") != prev:
                return False
            payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != rec["hash"]:
                return False
            prev = rec["hash"]
        return True

    # -- reporting -------------------------------------------------------

    def snapshot(self, fair_probs: Optional[dict[tuple[int, str], float]] = None) -> dict:
        fair_probs = fair_probs or {}
        return {
            "cash": round(self.cash, 4),
            "equity": round(self.equity(fair_probs), 4),
            "pnl_realized": round(self.pnl, 4),
            "n_fills": len(self.fills),
            "positions": {
                f"{fid}:{oc}": round(qty, 4)
                for (fid, oc), qty in sorted(self.positions.items())
                if abs(qty) > 1e-9
            },
            "audit_head": self._chain_head,
            "audit_len": len(self.audit),
        }
