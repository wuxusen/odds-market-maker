"""Inventory management: exposure limits and quote skew.

Positions are held in binary outcome contracts (pay 1.0 if the outcome
wins). For a position of ``qty`` contracts at average cost ``avg``:

* long  (qty > 0): worst-case loss = qty * avg          (outcome loses)
* short (qty < 0): worst-case loss = |qty| * (1 - avg)  (outcome wins)

Fixture exposure is the *worst case across mutually exclusive outcomes*:
for each candidate winner, sum the realised loss of every leg under that
result; take the max. Hard limits are enforced per fixture and globally —
no signal, no fair-value opinion, ever overrides them (hard-gate doctrine
carried over from our production futures risk module).

Skew: inventory shifts our quote mid against the position so the market is
paid to take risk off us — long inventory lowers both bid and ask (we buy
less eagerly, sell more eagerly), short inventory raises them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..types import Fill, Side


@dataclass
class Position:
    qty: float = 0.0  # +long / -short in unit contracts
    avg_price: float = 0.0  # probability-space average entry
    realized_pnl: float = 0.0

    def apply(self, side: Side, price: float, qty: float) -> None:
        """Apply a fill from OUR side (ASK = we sell -> qty decreases)."""
        signed = qty if side == Side.BID else -qty
        if self.qty * signed >= 0:  # extending or opening
            new_qty = self.qty + signed
            if new_qty != 0:
                self.avg_price = (
                    self.avg_price * abs(self.qty) + price * abs(signed)
                ) / abs(new_qty)
            self.qty = new_qty
        else:  # reducing / flipping
            closed = min(abs(signed), abs(self.qty))
            direction = 1.0 if self.qty > 0 else -1.0
            self.realized_pnl += closed * direction * (price - self.avg_price)
            self.qty += signed
            if self.qty == 0:
                self.avg_price = 0.0
            elif self.qty * direction < 0:  # flipped through zero
                self.avg_price = price

    def worst_case_loss_if(self, outcome_wins: bool) -> float:
        """Realised PnL of this leg under a settlement scenario (negative = loss)."""
        settle = 1.0 if outcome_wins else 0.0
        return self.qty * (settle - self.avg_price)


class InventoryBook:
    """Positions per (fixture, outcome) with hard exposure gates and skew."""

    def __init__(
        self,
        max_fixture_exposure: float = 200.0,
        max_total_exposure: float = 500.0,
        max_outcome_qty: float = 300.0,
        skew_intensity: float = 0.5,
    ) -> None:
        self.max_fixture_exposure = max_fixture_exposure
        self.max_total_exposure = max_total_exposure
        self.max_outcome_qty = max_outcome_qty
        self.skew_intensity = skew_intensity
        self._pos: dict[tuple[int, str], Position] = {}

    # -- bookkeeping ---------------------------------------------------------

    def position(self, fixture_id: int, outcome: str) -> Position:
        return self._pos.setdefault((fixture_id, outcome), Position())

    def on_fill(self, fill: Fill) -> None:
        self.position(fill.fixture_id, fill.outcome).apply(
            fill.side, fill.price, fill.qty
        )

    # -- exposure ------------------------------------------------------------

    def fixture_exposure(self, fixture_id: int) -> float:
        """Worst-case loss across mutually exclusive settlement scenarios."""
        legs = {
            oc: p for (fid, oc), p in self._pos.items() if fid == fixture_id
        }
        if not legs:
            return 0.0
        worst = 0.0
        for winner in legs:  # each outcome winning is one scenario
            pnl = sum(
                p.worst_case_loss_if(oc == winner) for oc, p in legs.items()
            )
            worst = max(worst, -pnl)
        # Also consider "none of our legs wins" (e.g. we only hold HOME).
        pnl_none = sum(p.worst_case_loss_if(False) for p in legs.values())
        return max(worst, -pnl_none, 0.0)

    def total_exposure(self) -> float:
        return sum(
            self.fixture_exposure(fid) for fid in {k[0] for k in self._pos}
        )

    # -- quote shaping -------------------------------------------------------

    def skew(self, fixture_id: int, outcome: str, half_spread: float) -> float:
        """Probability-space mid shift for this outcome (long -> negative)."""
        pos = self.position(fixture_id, outcome)
        ratio = max(-1.0, min(1.0, pos.qty / self.max_outcome_qty))
        return -ratio * self.skew_intensity * half_spread

    def allowed_size(
        self, fixture_id: int, outcome: str, side: Side, base_size: float
    ) -> float:
        """Hard-gate a quote size. Returns 0 if the side must not be quoted.

        Sides that *reduce* inventory are always allowed (up to base size);
        sides that extend it shrink linearly as exposure approaches the
        fixture/global caps and cut to zero at the cap.
        """
        pos = self.position(fixture_id, outcome)
        extending = (side == Side.BID and pos.qty >= 0) or (
            side == Side.ASK and pos.qty <= 0
        )
        if not extending:
            return min(base_size, abs(pos.qty))  # only ever reduce to flat

        qty_headroom = self.max_outcome_qty - abs(pos.qty)
        if qty_headroom <= 0.0:
            return 0.0
        fx = self.fixture_exposure(fixture_id)
        tx = self.total_exposure()
        head_f = max(0.0, 1.0 - fx / self.max_fixture_exposure)
        head_t = max(0.0, 1.0 - tx / self.max_total_exposure)
        headroom = min(head_f, head_t)
        if headroom <= 0.0:
            return 0.0
        # The quantity cap is hard: a full fill at this size can never
        # push the position past max_outcome_qty.
        return min(base_size * headroom, qty_headroom)

    def snapshot(self) -> dict:
        return {
            f"{fid}:{oc}": {
                "qty": round(p.qty, 4),
                "avg_price": round(p.avg_price, 5),
                "realized_pnl": round(p.realized_pnl, 4),
            }
            for (fid, oc), p in sorted(self._pos.items())
            if p.qty or p.realized_pnl
        }
