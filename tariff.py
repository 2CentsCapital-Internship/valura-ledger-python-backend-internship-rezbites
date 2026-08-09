"""The tariff: what a fill costs, who pays it, and which broker gets the order.

The feed does not tell you any fee amount. It tells you the principal, the
broker that took the fill, and the partner rate; the tariff turns those into
six separate amounts, three charged to the customer and three costing the firm:

    b   brokerage      customer pays -> firm income          (4000)
    c   custody        customer pays -> firm income          (4010)
    r   regulatory     customer pays -> owed onward          (2400)
    bc  broker cost    firm pays the executing broker        (5000 / 241x)
    cc  custody cost   firm pays the custodian               (5010 / 2420)
    ps  partner share  firm pays the introducing partner     (5100 / 2430)

Each is rounded to the cent on its own before it is used anywhere else.
"""
from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple, Optional

from money import D, ZERO, bps, dec, money

REG_BPS = D("8")          # regulatory fee, charged to the customer on every fill


# One broker's price list: their rates, minimum fee and flat per-trade fee.
class Broker(NamedTuple):
    broker_id: str
    asset_classes: frozenset
    brokerage_bps: Decimal
    custody_bps: Decimal
    broker_cost_bps: Decimal
    custody_cost_bps: Decimal
    min_fee: Decimal
    ticket: Decimal
    payable_account: str


BROKERS = {
    "BRK-A": Broker("BRK-A", frozenset({"equity", "etf"}),
                    D("20"), D("4"), D("9"), D("2"), D("1.00"), D("0.35"), "2411"),
    "BRK-B": Broker("BRK-B", frozenset({"equity", "bond"}),
                    D("15"), D("5"), D("8"), D("3"), D("2.50"), D("3.00"), "2412"),
    "BRK-C": Broker("BRK-C", frozenset({"etf", "bond"}),
                    D("25"), D("3"), D("12"), D("1"), D("0.50"), D("0.20"), "2413"),
}

ASSET_CLASSES = frozenset({"equity", "etf", "bond"})
BROKER_PAYABLES = {b.payable_account for b in BROKERS.values()}


# The six fee amounts worked out for one trade.
class Charges(NamedTuple):
    """The six derived amounts for one fill."""
    brokerage: Decimal
    custody: Decimal
    regulatory: Decimal
    broker_cost: Decimal
    custody_cost: Decimal
    partner_share: Decimal
    payable_account: str

    # What the customer pays in total, on top of the share price.
    @property
    def customer_charges(self) -> Decimal:
        """What the customer pays on top of (buy) or out of (sell) principal."""
        return self.brokerage + self.custody + self.regulatory

    # What we earned on this trade.
    @property
    def revenue(self) -> Decimal:
        return self.brokerage + self.custody

    # What this trade cost us.
    @property
    def cost(self) -> Decimal:
        return self.broker_cost + self.custody_cost


# What the customer would pay if we used this broker - used to compare brokers.
def customer_charge(broker_id: str, notional) -> Decimal:
    """Brokerage + custody for this notional. The quantity the routing rule ranks.

    The minimum fee floors the brokerage charge only: the spec says "the
    brokerage charge is floored at the broker's minimum fee", and says nothing
    of the sort about custody.
    """
    b = BROKERS[broker_id]
    notional = dec(notional)
    brokerage = max(bps(notional, b.brokerage_bps), b.min_fee)
    custody = bps(notional, b.custody_bps)
    return brokerage + custody


# Pick the cheapest broker that handles this kind of investment.
def route(asset_class: str, notional) -> Optional[str]:
    """The broker this order goes to.

    Lowest total customer charge (brokerage + custody) on `notional`, among the
    brokers that trade this asset class, ties broken on broker id ascending -
    so there is always exactly one answer. Sorting the candidates by id first
    makes `min` resolve ties correctly without a second sort key.
    """
    candidates = sorted(bid for bid, b in BROKERS.items()
                        if asset_class in b.asset_classes)
    if not candidates:
        return None
    return min(candidates, key=lambda bid: customer_charge(bid, notional))


# Work out all six fees for one trade from the price list.
def charges_for(broker_id: str, principal, partner_rate) -> Charges:
    """The full fee chain for one fill.

    Note the order of operations: every component is rounded to the cent before
    the partner share is computed from it, because the spec rounds each derived
    amount independently. Computing the partner share off unrounded revenue and
    cost disagrees by a cent on exactly the fills the spec warns about.
    """
    b = BROKERS[broker_id]
    principal = dec(principal)

    brokerage = max(bps(principal, b.brokerage_bps), b.min_fee)
    custody = bps(principal, b.custody_bps)
    regulatory = bps(principal, REG_BPS)

    # Every fill costs the firm the broker's flat ticket fee whatever its size.
    # That ticket is what makes roughly a quarter of all fills loss-making, and
    # a loss-making fill pays the partner nothing: there is no clawback.
    broker_cost = bps(principal, b.broker_cost_bps) + b.ticket
    custody_cost = bps(principal, b.custody_cost_bps)

    margin = (brokerage + custody) - (broker_cost + custody_cost)
    partner_share = money(dec(partner_rate) * margin) if margin > ZERO else ZERO

    return Charges(brokerage, custody, regulatory,
                   broker_cost, custody_cost, partner_share, b.payable_account)


# Does this broker deal in this kind of investment at all?
def trades(broker_id: str, asset_class: str) -> bool:
    """Whether this broker deals in this asset class at all."""
    b = BROKERS.get(broker_id)
    return bool(b) and asset_class in b.asset_classes
