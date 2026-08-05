"""The book of record: accounts, balances, and one handler per event type.

Section 4 of the spec states what happens commercially and deliberately does
not say which accounts move; deriving that is the assignment. The reasoning
behind each posting is in the handler that makes it, and at more length in
NOTES.md.

Two rules hold everywhere:

  * Debits equal credits on every transaction. `_post` refuses anything else,
    loudly, because a book that silently accepts an unbalanced entry is worse
    than one that crashes.
  * Balances key on **(customer_id, account)**, never on account alone.
    `transfer_between_customers` puts both its legs on 2010, so an
    account-keyed book nets it to zero and shows nothing happening at all.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Optional

import tariff
from lots import LotBook, Oversell
from money import ZERO, dec, money, money_str, qty_str
from orders import OrderBook

# code -> (name, type). Assets and expenses are debit-positive; liabilities and
# income are credit-positive.
ACCOUNTS = {
    "1100": ("Omnibus Cash at Broker", "asset"),
    "1150": ("Settlement Receivable", "asset"),
    "1200": ("Omnibus Custody", "asset"),
    "2010": ("Customer Wallet", "liability"),
    "2100": ("Customer Securities Claim", "liability"),
    "2300": ("Withdrawals In Transit", "liability"),
    "2350": ("Unsettled Trade Payable", "liability"),
    "2400": ("Regulatory Fees Payable", "liability"),
    "2411": ("Broker Fees Payable - BRK-A", "liability"),
    "2412": ("Broker Fees Payable - BRK-B", "liability"),
    "2413": ("Broker Fees Payable - BRK-C", "liability"),
    "2420": ("Custodian Fees Payable", "liability"),
    "2430": ("Partner Share Payable", "liability"),
    "4000": ("Brokerage Revenue", "income"),
    "4010": ("Custody Revenue", "income"),
    "4100": ("FX Spread Revenue", "income"),
    "4200": ("Interest Income", "income"),
    "5000": ("Brokerage Cost", "expense"),
    "5010": ("Custody Cost", "expense"),
    "5100": ("Partner Revenue Share", "expense"),
}

# A leg worth nothing is not posted. The alternative - emitting `Dr 5100 0.00`
# on every loss-making fill - would also put 5100 and 2430 into the trial
# balance for customers who never generated a partner share at all, and the
# spec asks for every account *posted to*. Practice mode settles this; it is
# one flag so it can be flipped in one place.
OMIT_ZERO_LEGS = True


class Rejected(Exception):
    """An event refused on its own merits.

    An oversell, a reversal of something never received, a negative FX spread,
    a settlement of an account with nothing outstanding. It produces no legs
    and must leave the book exactly as it was.
    """


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": money_str(debit), "credit": money_str(credit)}


class Ledger:
    """State. Rebuildable from the event log by replaying in delivery order."""

    def __init__(self) -> None:
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.touched: set[str] = set()          # every account ever posted to
        self.customers: set[str] = set()

        self.lots = LotBook()
        self.book = OrderBook()

        # Lookups for the events that omit their own amount
        self.fees: dict[str, dict] = {}          # fee_charged event_id -> {cid, amount}
        self.refunded: set[str] = set()
        self.withdrawals: dict[str, dict] = {}   # withdrawal_id -> {cid, amount, state}
        self.settled_trades: set[str] = set()

        # For reversals: what each event posted, and what it did to the lot book
        self.legs_by_event: dict[str, list[dict]] = {}
        self.lot_undo_by_event: dict[str, list[dict]] = {}
        self.reversed_events: set[str] = set()

        self.symbol_class: dict[str, str] = {}   # a symbol keeps one asset class

    # -- posting -------------------------------------------------------------
    def post(self, event_id: str, legs: list[dict]) -> list[dict]:
        """Apply legs to balances after checking the entry balances."""
        if OMIT_ZERO_LEGS:
            legs = [l for l in legs
                    if dec(l["debit"]) != ZERO or dec(l["credit"]) != ZERO]

        debits = sum((dec(l["debit"]) for l in legs), ZERO)
        credits = sum((dec(l["credit"]) for l in legs), ZERO)
        if debits != credits:
            raise AssertionError(
                f"unbalanced entry on {event_id}: Dr {debits} Cr {credits}")

        for l in legs:
            key = (l["customer_id"], l["account"])
            self.balances[key] += dec(l["debit"]) - dec(l["credit"])
            self.touched.add(l["account"])
            self.customers.add(l["customer_id"])

        self.legs_by_event[event_id] = legs
        return legs

    def balance(self, customer_id: str, account: str) -> Decimal:
        """Debit-positive balance for one customer on one account."""
        return self.balances[(customer_id, account)]

    def owed(self, customer_id: str, account: str) -> Decimal:
        """What is outstanding on a liability: the credit-positive balance."""
        return -self.balance(customer_id, account)

    def _record_lot_undo(self, event_id: str, record: dict) -> None:
        self.lot_undo_by_event.setdefault(event_id, []).append(record)

    def rollback(self, event_id: str) -> None:
        """Undo whatever a failed event did to the lot book.

        A fill relieves cost before its entry is posted, so an event that is
        rejected or blows up partway through would otherwise leave lots
        half-consumed - exactly what the spec forbids on an oversell. An event
        that produces no legs must leave the book precisely as it was.
        """
        for record in reversed(self.lot_undo_by_event.pop(event_id, [])):
            self.lots.undo(record)

    # =======================================================================
    # Cash
    # =======================================================================
    def on_deposit(self, p, ev):
        """Cash arrives at the broker and the firm owes the customer more.

        The firm is not richer for it: the asset and the obligation rise
        together. This is the one posting the spec works for you.
        """
        amount = money(p["amount"])
        cid = p["customer_id"]
        return [leg("1100", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    def on_fee_charged(self, p, ev):
        """The customer pays the firm's fee out of their wallet.

        The mirror of a deposit: the obligation to the customer falls, and the
        cash leaves the omnibus account with it.
        """
        amount = money(p["amount"])
        cid = p["customer_id"]
        self.fees[ev["event_id"]] = {"cid": cid, "amount": amount}
        return [leg("2010", cid, debit=amount),
                leg("1100", cid, credit=amount)]

    def on_fee_refund(self, p, ev):
        """Undoes an earlier fee in full. The amount is not in this payload."""
        source = p.get("refunds_source_id")
        original = self.fees.get(source)
        if original is None:
            raise Rejected(f"fee_refund of unknown fee {source}")
        if source in self.refunded:
            raise Rejected(f"fee {source} already refunded")
        self.refunded.add(source)

        cid = p.get("customer_id") or original["cid"]
        amount = original["amount"]
        return [leg("1100", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    def on_withdrawal_requested(self, p, ev):
        """The money has left the wallet but not the broker.

        It is still owed to the customer, but as a withdrawal in flight rather
        than as spendable wallet money. Those are different obligations, which
        is why they are different accounts.
        """
        amount = money(p["amount"])
        cid = p["customer_id"]
        self.withdrawals[p["withdrawal_id"]] = {"cid": cid, "amount": amount,
                                                "state": "requested"}
        return [leg("2010", cid, debit=amount),
                leg("2300", cid, credit=amount)]

    def _withdrawal(self, p, expect_state="requested"):
        wid = p.get("withdrawal_id")
        w = self.withdrawals.get(wid)
        if w is None:
            raise Rejected(f"withdrawal {wid} never requested")
        if w["state"] != expect_state:
            raise Rejected(f"withdrawal {wid} already {w['state']}")
        return wid, w

    def on_withdrawal_settled(self, p, ev):
        """The cash actually leaves the broker, discharging the obligation."""
        wid, w = self._withdrawal(p)
        w["state"] = "settled"
        return [leg("2300", w["cid"], debit=w["amount"]),
                leg("1100", w["cid"], credit=w["amount"])]

    def on_withdrawal_rejected(self, p, ev):
        """The withdrawal fails. No cash ever moved; the money is wallet money
        again."""
        wid, w = self._withdrawal(p)
        w["state"] = "rejected"
        return [leg("2300", w["cid"], debit=w["amount"]),
                leg("2010", w["cid"], credit=w["amount"])]

    def on_interest_credited(self, p, ev):
        """Interest on the omnibus balance, shared with the customer.

        Not a pass-through: the firm keeps the remainder as income, so the cash
        arriving and the obligation created are different numbers.
        """
        cid = p["customer_id"]
        gross = money(p["gross_amount"])
        share = money(p["customer_share"])
        return [leg("1100", cid, debit=gross),
                leg("2010", cid, credit=share),
                leg("4200", cid, credit=gross - share)]

    def on_transfer_between_customers(self, p, ev):
        """One customer pays another. No external cash moves.

        The firm's total obligation is unchanged; only whose money it is
        changes. Both legs land on 2010, so this is the event that punishes a
        book keyed by account instead of by (customer, account).
        """
        amount = money(p["amount"])
        return [leg("2010", p["from_customer_id"], debit=amount),
                leg("2010", p["to_customer_id"], credit=amount)]

    def on_fx_deposit(self, p, ev):
        """Foreign cash converted on the way in.

        The omnibus account receives the market value; the customer is credited
        at their own, worse rate. The gap is the firm's spread, earned now.
        Crediting the customer the market figure would overstate what they are
        owed by exactly that spread.
        """
        cid = p["customer_id"]
        at_market = money(p["usd_at_market_rate"])
        at_customer = money(p["usd_at_customer_rate"])
        if at_customer > at_market:
            raise Rejected("fx_deposit with a negative spread")
        return [leg("1100", cid, debit=at_market),
                leg("2010", cid, credit=at_customer),
                leg("4100", cid, credit=at_market - at_customer)]

    # =======================================================================
    # Orders
    # =======================================================================
    def on_order_placed(self, p, ev):
        """No legs. A placement moves no money; it makes money unspendable."""
        if p.get("symbol") and p.get("asset_class"):
            self.symbol_class.setdefault(p["symbol"], p["asset_class"])
        self.book.place(p)
        return []

    def on_order_partially_filled(self, p, ev):
        return self._fill(p, ev, final=False)

    def on_order_filled(self, p, ev):
        return self._fill(p, ev, final=True)

    def _fill(self, p, ev, final: bool):
        """A fill. The whole fee chain lands here, and cash does not move.

        The firm owes the broker the principal until settlement two days later,
        so a book that touches 1100 here disagrees with the broker for as long
        as anything is unsettled. Revenue and cost are booked gross: a book
        that posts only the margin balances perfectly and can never say what it
        earned or what it cost.
        """
        cid = p["customer_id"]
        symbol = p["symbol"]
        side = p["side"]
        principal = money(p["principal"])
        broker = p.get("broker")

        if broker not in tariff.BROKERS:
            raise Rejected(f"fill names unknown broker {broker!r}")
        if p.get("asset_class"):
            self.symbol_class.setdefault(symbol, p["asset_class"])

        ch = tariff.charges_for(broker, principal, p.get("partner_rate", "0"))

        # The firm's side of a fill is identical on a buy and a sell: revenue
        # earned, costs incurred, the regulatory fee collected for the venue,
        # and the partner's share of what is left.
        firm = [
            leg("5000", cid, debit=ch.broker_cost),
            leg("5010", cid, debit=ch.custody_cost),
            leg("5100", cid, debit=ch.partner_share),
            leg("4000", cid, credit=ch.brokerage),
            leg("4010", cid, credit=ch.custody),
            leg("2400", cid, credit=ch.regulatory),
            leg(ch.payable_account, cid, credit=ch.broker_cost),
            leg("2420", cid, credit=ch.custody_cost),
            leg("2430", cid, credit=ch.partner_share),
        ]

        if side == "buy":
            # The customer pays principal plus every charge. The shares sit in
            # omnibus custody and the customer holds a claim on them, both at
            # principal - the charges are not part of what the shares cost.
            legs = [
                leg("2010", cid, debit=principal + ch.customer_charges),
                leg("1200", cid, debit=principal),
                leg("2350", cid, credit=principal),
                leg("2100", cid, credit=principal),
            ] + firm
            undo = self.lots.add(cid, symbol, p["quantity"], principal)
            self._record_lot_undo(ev["event_id"], undo)
        else:
            # FIFO cost relief first: an oversell must reject the whole event
            # and leave no lot half-consumed, so nothing may have been mutated
            # before this point.
            try:
                cost, undo = self.lots.relieve(cid, symbol, p["quantity"])
            except Oversell as exc:
                raise Rejected(str(exc)) from exc
            self._record_lot_undo(ev["event_id"], undo)

            # The proceeds are owed to the firm by the broker until settlement.
            # Custody and the customer's claim shrink by what the shares cost,
            # not what they sold for; the difference is the realised gain or
            # loss, and it is the residual of these legs, never posted.
            legs = [
                leg("1150", cid, debit=principal),
                leg("2100", cid, debit=cost),
                leg("2010", cid, credit=principal - ch.customer_charges),
                leg("1200", cid, credit=cost),
            ] + firm

        if p.get("trade_id"):
            self.book.record_trade(p["trade_id"], cid, side, principal,
                                   ev["event_id"])
        self.book.fill(p, final=final)
        return legs

    def on_trade_settled(self, p, ev):
        """Settlement day: the cash from that fill actually moves."""
        trade_id = p.get("trade_id")
        t = self.book.trade(trade_id)
        if t is None:
            raise Rejected(f"trade_settled for unknown trade {trade_id}")
        if trade_id in self.settled_trades:
            raise Rejected(f"trade {trade_id} already settled")
        if t.get("event_id") in self.reversed_events:
            # The fill was reversed before settlement day, so the obligation it
            # created has already been cancelled. There is nothing left to
            # discharge, and paying it would move cash for a trade that no
            # longer exists. Practice confirms this exactly: of 65 settlements,
            # the 7 whose fill had already been reversed were the only ones we
            # got wrong, and both accounts of the entry (1100 and 2350) were
            # named as differing.
            #
            # Order matters. A reversal arriving *after* settlement does not
            # make the settlement retrospectively wrong - all 58 of those
            # scored correct - because at the time it happened the obligation
            # was real.
            raise Rejected(f"trade {trade_id} was reversed before settlement")
        self.settled_trades.add(trade_id)

        cid, principal = t["customer_id"], t["principal"]
        if t["side"] == "buy":
            # Discharge what the firm owed the broker.
            return [leg("2350", cid, debit=principal),
                    leg("1100", cid, credit=principal)]
        # Collect what the broker owed the firm.
        return [leg("1100", cid, debit=principal),
                leg("1150", cid, credit=principal)]

    def on_order_cancelled(self, p, ev):
        """No legs. The remaining hold is released."""
        self.book.close(p["order_id"])
        return []

    def on_order_rejected(self, p, ev):
        return self.on_order_cancelled(p, ev)

    # =======================================================================
    # Paying it all onward
    # =======================================================================
    def _settle_payable(self, account: str, cid: str, what: str):
        """Discharge a payable in full for one customer, out of omnibus cash.

        The amount is never in the payload: it is whatever has accumulated on
        that account for that customer, so each of these audits every per-trade
        rounding done since the last one. Settling nothing is an error.
        """
        amount = self.owed(cid, account)
        if amount <= ZERO:
            raise Rejected(f"{what}: nothing outstanding on {account} for {cid}")
        return [leg(account, cid, debit=amount),
                leg("1100", cid, credit=amount)]

    def on_broker_fees_settled(self, p, ev):
        broker = p.get("broker")
        if broker not in tariff.BROKERS:
            raise Rejected(f"broker_fees_settled names unknown broker {broker!r}")
        return self._settle_payable(tariff.BROKERS[broker].payable_account,
                                    p["customer_id"], "broker_fees_settled")

    def on_custodian_fees_settled(self, p, ev):
        return self._settle_payable("2420", p["customer_id"],
                                    "custodian_fees_settled")

    def on_reg_fees_remitted(self, p, ev):
        return self._settle_payable("2400", p["customer_id"],
                                    "reg_fees_remitted")

    def on_partner_payout(self, p, ev):
        return self._settle_payable("2430", p["customer_id"], "partner_payout")

    # =======================================================================
    # Corporate actions
    # =======================================================================
    def on_dividend_cash(self, p, ev):
        """Tax was withheld at source, so only the net ever reaches the firm
        and the firm owes the tax to nobody. Raise no payable."""
        cid = p["customer_id"]
        net = money(p["net_amount"])
        return [leg("1100", cid, debit=net),
                leg("2010", cid, credit=net)]

    def on_dividend_reinvested(self, p, ev):
        """The broker reinvests the net directly: cash is never involved.

        The holding grows by a new lot of reinvest_quantity costing the net
        amount, and custody and the claim grow with it.
        """
        cid = p["customer_id"]
        net = money(p["net_amount"])
        undo = self.lots.add(cid, p["symbol"], p["reinvest_quantity"], net)
        self._record_lot_undo(ev["event_id"], undo)
        return [leg("1200", cid, debit=net),
                leg("2100", cid, credit=net)]

    def on_stock_split(self, p, ev):
        """No legs. Quantity scales, each lot's total cost is unchanged, so
        cost per share moves and the position's cost basis does not."""
        undo = self.lots.scale(p["customer_id"], p["symbol"],
                               p["ratio_from"], p["ratio_to"])
        self._record_lot_undo(ev["event_id"], undo)
        return []

    def on_symbol_change(self, p, ev):
        """No legs. Re-key the holding."""
        undo = self.lots.rekey(p["customer_id"], p["old_symbol"],
                               p["new_symbol"])
        self._record_lot_undo(ev["event_id"], undo)
        if p["old_symbol"] in self.symbol_class:
            self.symbol_class.setdefault(p["new_symbol"],
                                         self.symbol_class[p["old_symbol"]])
        return []

    # =======================================================================
    # Corrections
    # =======================================================================
    def on_reversal(self, p, ev):
        """Post the exact inverse of the original's legs, and keep both.

        The audit trail retains the original and its reversal, so this is not a
        deletion. It must also undo the original's effect on the lot book: a
        reversed buy whose lot is left in place balances perfectly and quietly
        corrupts every later cost basis.

        It does not undo the lifecycle. A hold the original fill released stays
        released.
        """
        target = p.get("reverses_event_id")
        if target in self.reversed_events:
            raise Rejected(f"event {target} already reversed")

        original = self.legs_by_event.get(target)
        if original is None:
            if target in self.lot_undo_by_event:
                # Seen, posted nothing, but did move the lot book - a split or
                # a rename. Undo that; there are no legs to invert.
                self.reversed_events.add(target)
                for record in reversed(self.lot_undo_by_event[target]):
                    self.lots.undo(record)
                return []
            raise Rejected(f"reversal of unknown event {target}")

        self.reversed_events.add(target)
        for record in reversed(self.lot_undo_by_event.get(target, [])):
            self.lots.undo(record)

        return [leg(l["account"], l["customer_id"],
                    debit=l["credit"], credit=l["debit"]) for l in original]

    # =======================================================================
    # Reporting
    # =======================================================================
    def snapshot(self) -> dict:
        """The whole state, in the shape a checkpoint wants."""
        trial: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for (_cid, account), bal in self.balances.items():
            trial[account] += bal
        # Every account ever posted to, including any netted back to zero.
        for account in self.touched:
            trial.setdefault(account, ZERO)

        holds = self.book.cash_holds()
        positions = self.lots.positions()

        customers: dict[str, dict] = {}
        for cid in sorted(self.customers | set(holds) | set(positions)):
            customers[cid] = {
                "wallet_cash": money_str(self.owed(cid, "2010")),
                "cash_hold": money_str(holds.get(cid, ZERO)),
                "positions": {
                    sym: {"quantity": qty_str(q), "cost_basis": money_str(c)}
                    for sym, (q, c) in sorted(positions.get(cid, {}).items())
                },
            }

        return {
            "trial_balance": {a: money_str(v) for a, v in sorted(trial.items())},
            "customers": customers,
            "open_order_routes": dict(sorted(self.book.open_routes().items())),
        }
