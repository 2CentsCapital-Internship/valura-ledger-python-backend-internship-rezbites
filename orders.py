"""Order lifecycle: holds, routing, and the trades awaiting settlement.

None of this is ever posted. A placement moves no money - it makes money
unspendable, which is a different thing - so holds appear only in checkpoints.
They are still worth having exactly right: `cash_hold` and `open_order_routes`
together are 13% of the checkpoint score.

Two lifecycle rules from the spec drive the shape here:

  * A fill releases a proportional share of the hold the order placed, and the
    final fill or a cancellation releases whatever remains, so a closed order
    always returns its hold to exactly zero.
  * Reversing a fill does **not** restore the hold. A released hold stays
    released; a reversal undoes postings and the lot book, not the lifecycle.
    That is why nothing in this module has an undo.

A fill can also arrive before its placement, so an order is created by whichever
event mentions it first and filled in later.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

import tariff
from money import ZERO, dec, money, qty


class Order:
    """One order, however it first reached us."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        self.customer_id: Optional[str] = None
        self.side: Optional[str] = None
        self.symbol: Optional[str] = None
        self.asset_class: Optional[str] = None

        self.placed = False           # have we seen the order_placed event
        self.quantity = ZERO          # quantity ordered
        self.limit_price = ZERO
        self.est_charges = ZERO
        self.hold_total = ZERO        # cash for a buy, shares for a sell
        self.route: Optional[str] = None

        self.filled_qty = ZERO
        self.fills: list[Decimal] = []      # quantities, in delivery order
        self.closed = False

    @property
    def is_open(self) -> bool:
        """Open until a final fill, a cancellation or a rejection closes it."""
        return not self.closed

    @property
    def hold_remaining(self) -> Decimal:
        """What is still held back.

        Each fill releases its own proportional share, and that share is
        rounded to the cent like every other derived amount, so what remains is
        the total less the sum of the releases. Working the remainder out
        directly from the unfilled quantity instead is a cent adrift whenever
        the releases do not divide evenly - confirmed against practice, which
        disagreed on exactly the two part-filled orders where the two formulas
        differ.

        Unknown until the placement arrives: a fill seen first tells us nothing
        about the limit price the hold was struck at.
        """
        if self.closed or not self.placed or self.quantity <= ZERO:
            return ZERO
        released = sum((money(self.hold_total * f / self.quantity)
                        for f in self.fills), ZERO)
        return max(ZERO, self.hold_total - released)

    @property
    def cash_hold(self) -> Decimal:
        """Only a buy holds cash. A sell holds shares."""
        return self.hold_remaining if self.side == "buy" else ZERO


class OrderBook:
    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        # trade_id -> what its fill obliged, for trade_settled to discharge
        self.trades: dict[str, dict] = {}

    def get(self, order_id: str) -> Order:
        order = self.orders.get(order_id)
        if order is None:
            order = self.orders[order_id] = Order(order_id)
        return order

    # -- lifecycle -----------------------------------------------------------
    def place(self, p: dict) -> Order:
        """order_placed. No legs; this creates the hold and picks the route."""
        o = self.get(p["order_id"])
        o.customer_id = p["customer_id"]
        o.side = p["side"]
        o.symbol = p["symbol"]
        o.asset_class = p.get("asset_class")
        o.quantity = qty(p["quantity"])
        o.limit_price = dec(p["limit_price"])
        o.est_charges = dec(p.get("est_charges", "0"))
        o.placed = True

        notional = o.quantity * o.limit_price
        if o.side == "buy":
            # Cash of quantity x limit_price + est_charges is no longer
            # spendable. est_charges is a conservative estimate from the feed,
            # used as given rather than recomputed from the tariff.
            o.hold_total = money(notional + o.est_charges)
        else:
            o.hold_total = o.quantity          # shares, not cash

        if o.asset_class:
            o.route = tariff.route(o.asset_class, notional)
        return o

    def fill(self, p: dict, final: bool) -> Order:
        """A partial or final fill. Releases hold in proportion to quantity."""
        o = self.get(p["order_id"])
        if o.customer_id is None:
            # The fill beat its placement here. Take what the fill knows; the
            # placement will supply the hold and the route when it arrives.
            o.customer_id = p.get("customer_id")
            o.side = p.get("side")
            o.symbol = p.get("symbol")
            o.asset_class = p.get("asset_class")
        filled = qty(p["quantity"])
        o.filled_qty += filled
        o.fills.append(filled)
        if final:
            o.closed = True
        return o

    def close(self, order_id: str) -> Order:
        """order_cancelled / order_rejected. Releases whatever remains."""
        o = self.get(order_id)
        o.closed = True
        return o

    # -- reporting -----------------------------------------------------------
    def cash_holds(self) -> dict[str, Decimal]:
        """{customer: cash still held back by open buy orders}."""
        out: dict[str, Decimal] = {}
        for o in self.orders.values():
            if o.customer_id and o.cash_hold > ZERO:
                out[o.customer_id] = out.get(o.customer_id, ZERO) + o.cash_hold
        return out

    def open_routes(self) -> dict[str, str]:
        """{order_id: broker} for every order we believe is still open.

        Orders seen filled or cancelled do not belong here, and neither does an
        order whose placement we never received - without its limit price there
        is no notional to route on, so we would be guessing.
        """
        return {oid: o.route for oid, o in self.orders.items()
                if o.is_open and o.placed and o.route}

    # -- settlement ----------------------------------------------------------
    def record_trade(self, trade_id: str, customer_id: str, side: str,
                     principal: Decimal) -> None:
        self.trades[trade_id] = {"customer_id": customer_id, "side": side,
                                 "principal": principal}

    def trade(self, trade_id: str) -> Optional[dict]:
        return self.trades.get(trade_id)
