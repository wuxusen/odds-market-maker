"""AnchorSink: pluggable on-chain (or no-op) commitment for the ledger's
tamper-evident hash chain.

Anchoring is deliberately kept outside the trust boundary of the ledger and
market-making core: it is pure external I/O, and external I/O fails — RPC
nodes rate-limit, devnet faucets run dry, connections time out. The
``AnchorSink.anchor()`` contract therefore *never* raises for expected
failure modes: it always returns an :class:`AnchorResult`, and callers
(:class:`odds_mm.anchor.scheduler.LedgerAnchorScheduler`) degrade to "not
anchored yet, retry next window" rather than interrupting quoting or
trading. This is the same fail-safe-first doctrine as the
:class:`~odds_mm.mm.circuit_breaker.CircuitBreaker`: the core keeps running
on its own clock; only the optional, best-effort feature backs off.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AnchorResult:
    """Outcome of one attempt to commit a hash to an :class:`AnchorSink`."""

    ok: bool
    head_hash: str
    tx_sig: Optional[str] = None
    slot: Optional[int] = None
    error: Optional[str] = None


class AnchorSink(abc.ABC):
    """Something that can durably, publicly commit to a hash."""

    @abc.abstractmethod
    def anchor(self, head_hash: str) -> AnchorResult:
        """Commit ``head_hash`` (hex SHA-256). Must not raise."""


class NullAnchor(AnchorSink):
    """Anchoring disabled/unconfigured. Always reports failure (not fatal)
    so a scheduler wired to it never mistakes "off" for "confirmed
    on-chain" — it just keeps quoting and never anchors, which is exactly
    the degraded-but-correct behaviour we want when no sink is configured.
    """

    def anchor(self, head_hash: str) -> AnchorResult:
        return AnchorResult(ok=False, head_hash=head_hash, error="anchoring disabled (NullAnchor)")
