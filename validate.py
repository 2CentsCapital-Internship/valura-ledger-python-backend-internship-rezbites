"""Invariant checks, and the hunt for the systematic defect.

The spec guarantees that the feed contains at least one systematic defect: a
class of event that is internally well-formed and wrong. It says nothing else
about it. Our own invariants are the only way to find it, and events we
identify as the defect are bad data to be rejected.

The trap is that rejecting wrongly is expensive twice over - the event's own
posting score is lost, and the state that should have followed from it is
missing from every checkpoint afterwards. So every detector here is written
first and armed second: by default they only **count**, and a name is added to
`ARMED` once practice diagnostics show that we are systematically wrong on
exactly the events it fires on.

The rejections the spec states outright (oversell, negative FX spread, a
reversal of an unknown event, settling an account with nothing outstanding)
are not here - they need ledger state, and they live with the handlers that
have it.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

import tariff
from money import ZERO, dec, money

# Detector names that actually cause a rejection. Everything else is counted
# and reported, so the evidence arrives before the behaviour changes.
#
# `duplicate_fill` is armed on direct evidence: over 83 fills in practice run
# run_deb24bbe5b3c it flagged 22, the server expected no legs for every one of
# them, and it flagged nothing else.
ARMED: set[str] = {"duplicate_fill"}

# Money fields that must never be negative, by event type.
NON_NEGATIVE = {
    "deposit": ["amount"],
    "fee_charged": ["amount"],
    "withdrawal_requested": ["amount"],
    "interest_credited": ["gross_amount", "customer_share"],
    "transfer_between_customers": ["amount"],
    "dividend_cash": ["gross_amount", "net_amount"],
    "dividend_reinvested": ["gross_amount", "net_amount", "reinvest_quantity"],
    "order_placed": ["quantity", "limit_price"],
    "order_partially_filled": ["quantity", "price", "principal"],
    "order_filled": ["quantity", "price", "principal"],
}


class Malformed(Exception):
    """A payload that will not parse. Reject it and carry on."""


def structural(ev: dict) -> None:
    """The parse gate. Anything that gets past this is at least readable."""
    if not isinstance(ev, dict):
        raise Malformed("event is not an object")
    if not ev.get("event_id"):
        raise Malformed("no event_id")
    if not ev.get("type"):
        raise Malformed("no type")
    payload = ev.get("payload")
    if not isinstance(payload, dict):
        raise Malformed("payload is not an object")

    for field in NON_NEGATIVE.get(ev["type"], []):
        if field not in payload:
            continue
        try:
            if dec(payload[field]) < ZERO:
                raise Malformed(f"negative {field}: {payload[field]!r}")
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise Malformed(f"unparseable {field}: {payload[field]!r}") from exc


# ---------------------------------------------------------------------------
# Detectors. Each returns a reason when it fires, or None.
# ---------------------------------------------------------------------------
def _fill_principal(ev, led) -> Optional[str]:
    """principal should be quantity x price, to the cent."""
    p = ev["payload"]
    try:
        expected = money(dec(p["quantity"]) * dec(p["price"]))
    except (KeyError, InvalidOperation, TypeError):
        return None
    actual = money(p["principal"])
    if expected != actual:
        return f"principal {actual} != quantity x price {expected}"
    return None


def _fill_broker_asset(ev, led) -> Optional[str]:
    """A broker cannot execute an asset class it does not trade."""
    p = ev["payload"]
    broker, asset_class = p.get("broker"), p.get("asset_class")
    if not broker or not asset_class:
        return None
    if not tariff.trades(broker, asset_class):
        return f"{broker} does not trade {asset_class}"
    return None


def _symbol_class_conflict(ev, led) -> Optional[str]:
    """Every symbol belongs to one asset class for the whole run."""
    p = ev["payload"]
    symbol, asset_class = p.get("symbol"), p.get("asset_class")
    if not symbol or not asset_class:
        return None
    known = led.symbol_class.get(symbol)
    if known and known != asset_class:
        return f"{symbol} was {known}, now {asset_class}"
    return None


def _dividend_net(ev, led) -> Optional[str]:
    """net should be gross less the tax withheld at source."""
    p = ev["payload"]
    try:
        gross = money(p["gross_amount"])
        tax = money(p.get("withholding_tax", "0"))
        net = money(p["net_amount"])
    except (KeyError, InvalidOperation, TypeError):
        return None
    if gross - tax != net:
        return f"net {net} != gross {gross} - tax {tax}"
    return None


def _interest_share(ev, led) -> Optional[str]:
    """The customer's share cannot exceed the interest actually earned."""
    p = ev["payload"]
    try:
        gross = money(p["gross_amount"])
        share = money(p["customer_share"])
    except (KeyError, InvalidOperation, TypeError):
        return None
    if share > gross:
        return f"customer_share {share} > gross {gross}"
    return None


def _fx_conversion(ev, led) -> Optional[str]:
    """The stated USD figures should follow from the stated rates."""
    p = ev["payload"]
    try:
        foreign = dec(p["amount_foreign"])
        at_market = money(p["usd_at_market_rate"])
        at_customer = money(p["usd_at_customer_rate"])
        market = dec(p["market_rate"])
        customer = dec(p["customer_rate"])
    except (KeyError, InvalidOperation, TypeError):
        return None
    problems = []
    if money(foreign * market) != at_market:
        problems.append(f"market {at_market} != {foreign} x {market}")
    if money(foreign * customer) != at_customer:
        problems.append(f"customer {at_customer} != {foreign} x {customer}")
    return "; ".join(problems) or None


def _duplicate_fill(ev, led) -> Optional[str]:
    """A fill re-sent under a new event_id. **This is the systematic defect.**

    The spec guarantees a class of event that is internally well-formed and
    wrong, and says nothing else about it. This is it: the same fill delivered
    twice, byte-identical in every field including `trade_id`, but carrying a
    fresh `event_id` so that ordinary event-level deduplication waves it
    straight through. It is well-formed in isolation and only wrong in the
    context of the trade it double-books - which is precisely the shape the
    spec describes.

    One trade settles once, so a `trade_id` already recorded is the invariant
    that catches it. Note this is *not* the duplicate delivery the spec warns
    about elsewhere: those repeat the `event_id` and are handled in `Book`.

    Verified against practice over 83 fills: 22 flagged, every one of which the
    server expected no legs for, no false positives, and nothing missed.

    A quantity-based version of this check - "have the fills exceeded the order"
    - was tried first and is wrong. It misses a duplicate that lands inside the
    ordered quantity, and it false-positives on the legitimate final fill that
    follows one, because the double-booked shares inflate the running total.
    """
    trade_id = ev["payload"].get("trade_id")
    if not trade_id:
        return None
    if led.book.trade(trade_id) is not None:
        return f"trade {trade_id} already filled: fill re-sent under a new id"
    return None


def _fill_exceeds_order(ev, led) -> Optional[str]:
    """Fills totalling more than the order. Counted, not armed.

    Superseded by `duplicate_fill`, which catches the same defect at its cause
    rather than at one of its symptoms. Kept as an independent tripwire: if the
    duplicate rule ever stops catching everything, this fires and says so.
    """
    p = ev["payload"]
    order = led.book.orders.get(p.get("order_id"))
    if order is None or not order.placed or order.quantity <= ZERO:
        return None
    try:
        after = order.filled_qty + dec(p["quantity"])
    except (KeyError, InvalidOperation, TypeError):
        return None
    if after > order.quantity:
        return f"fills total {after}, order is for {order.quantity}"
    return None


def _self_transfer(ev, led) -> Optional[str]:
    p = ev["payload"]
    if p.get("from_customer_id") and p["from_customer_id"] == p.get("to_customer_id"):
        return "transfer to self"
    return None


FILL_TYPES = ("order_partially_filled", "order_filled")

DETECTORS: dict[str, tuple[tuple[str, ...], Callable]] = {
    "duplicate_fill": (FILL_TYPES, _duplicate_fill),
    "fill_exceeds_order": (FILL_TYPES, _fill_exceeds_order),
    "fill_principal": (FILL_TYPES, _fill_principal),
    "fill_broker_asset": (FILL_TYPES, _fill_broker_asset),
    "symbol_class_conflict": (FILL_TYPES + ("order_placed",), _symbol_class_conflict),
    "dividend_net": (("dividend_cash", "dividend_reinvested"), _dividend_net),
    "interest_share": (("interest_credited",), _interest_share),
    "fx_conversion": (("fx_deposit",), _fx_conversion),
    "self_transfer": (("transfer_between_customers",), _self_transfer),
}


def inspect(ev: dict, led) -> list[tuple[str, str]]:
    """Run every detector that applies. Returns [(name, reason), ...]."""
    fired = []
    for name, (types, check) in DETECTORS.items():
        if ev["type"] not in types:
            continue
        try:
            reason = check(ev, led)
        except Exception as exc:                     # a detector must never stop the run
            reason = f"detector error: {exc!r}"
        if reason:
            fired.append((name, reason))
    return fired
