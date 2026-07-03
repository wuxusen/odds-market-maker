"""Drives periodic anchoring of a :class:`~odds_mm.ledger.PaperLedger`'s
audit-chain head into an :class:`~odds_mm.anchor.base.AnchorSink`.

Kept out of the ledger and the market-making loop on purpose: the ledger
must stay a pure, synchronous, network-free accounting object (it is
exercised byte-for-byte in deterministic replay tests), and the mm loop
must never stall on external I/O. This scheduler is the only thing that
calls out; it is a thin policy layer the caller pumps once per cycle with
:meth:`maybe_anchor`, and every failure mode collapses to "try again next
window" rather than an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .base import AnchorResult, AnchorSink, NullAnchor


@dataclass
class AnchorPolicy:
    """When a new anchor attempt is *due*. Either condition is sufficient
    (an OR, not an AND) — a busy session anchors on fill count, a quiet one
    still anchors on a wall-clock cadence so the chain is never silently
    unanchored for long stretches. A hash that hasn't changed since the
    last successful anchor is never re-anchored, regardless of cadence.
    """

    min_fills_between: int = 25
    min_interval_ms: int = 5 * 60_000  # 5 minutes

    # The very first attempt (nothing has ever been anchored yet) fires as
    # soon as there is at least one audit record, ignoring both thresholds
    # below — there is no reason to wait out a 25-fill or 5-minute warm-up
    # before the chain has ever been anchored at all. Every attempt after
    # that respects min_fills_between / min_interval_ms normally.


@dataclass
class LedgerAnchorScheduler:
    sink: AnchorSink = field(default_factory=NullAnchor)
    policy: AnchorPolicy = field(default_factory=AnchorPolicy)

    _last_anchor_ms: Optional[int] = field(default=None, init=False)
    _last_anchor_audit_len: int = field(default=0, init=False)
    # Dedup marker: the ledger's chain head as of our last touch (successful
    # append included) — *not* necessarily what's embedded on-chain, see
    # ``_last_committed_hash`` for that. Comparing against this (rather than
    # the hash we actually anchored) avoids re-triggering forever on our own
    # ``record_anchor`` bookkeeping entry, which itself advances the head.
    _last_seen_head: Optional[str] = field(default=None, init=False)
    # The exact hash embedded in the most recent *successful* on-chain memo
    # — what a verifier should expect to find in that transaction right now.
    _last_committed_hash: Optional[str] = field(default=None, init=False)
    history: list[AnchorResult] = field(default_factory=list, init=False)

    def is_due(self, ledger, now_ms: int) -> bool:
        if not ledger.audit:
            return False  # nothing has happened yet — nothing to anchor
        if ledger.chain_head == self._last_seen_head:
            return False  # nothing new since we last touched the chain
        if self._last_anchor_ms is None:
            return True  # first-ever attempt: fire as soon as there's data
        by_count = (len(ledger.audit) - self._last_anchor_audit_len) >= self.policy.min_fills_between
        by_time = (now_ms - self._last_anchor_ms) >= self.policy.min_interval_ms
        return by_count or by_time

    def maybe_anchor(self, ledger, now_ms: int) -> Optional[AnchorResult]:
        """Anchor the current chain head if due. Returns ``None`` when not
        due (the common case, cheap and side-effect-free), otherwise the
        :class:`AnchorResult` of the attempt (success or failure — both are
        recorded, neither raises).
        """
        if not self.is_due(ledger, now_ms):
            return None

        head = ledger.chain_head
        result = self.sink.anchor(head)
        self.history.append(result)
        if result.ok:
            self._last_committed_hash = head
            ledger.record_anchor(tx_sig=result.tx_sig, slot=result.slot, anchored_hash=head, ts=now_ms)
            # Captured only on success, *after* the append, so the anchor
            # record's own presence is folded into the dedup baseline
            # instead of being miscounted as "new activity" next round. On
            # failure we deliberately leave this alone so the same
            # unanchored head keeps being considered due (gated by the
            # count/time thresholds below, so it doesn't spin every call).
            self._last_seen_head = ledger.chain_head
        self._last_anchor_ms = now_ms
        self._last_anchor_audit_len = len(ledger.audit)
        return result

    def status(self) -> dict:
        last = self.history[-1] if self.history else None
        return {
            "sink": type(self.sink).__name__,
            "last_anchored_hash": self._last_committed_hash,
            "attempts": len(self.history),
            "last_result": None
            if last is None
            else {"ok": last.ok, "tx_sig": last.tx_sig, "slot": last.slot, "error": last.error},
        }
