#!/usr/bin/env python3
"""Replay a recorded run offline and prove the book is internally consistent.

Practice attempts are limited to twelve and cost twenty minutes each, so almost
all iteration happens here: a run is recorded once, then replayed as often as
we like. This cannot tell us whether a posting agrees with the reference - only
the server knows that - but it can tell us the book contradicts itself, and a
run is not worth spending until it does not.

    python tools/replay.py runs/<run_id>/events.jsonl

The checks are the properties the spec demands, restated as assertions:

  balanced      every entry has debits equal to credits
  zero-sum      the whole trial balance sums to zero
  deterministic replaying the log twice gives the same state
  idempotent    re-delivering a window of events changes nothing
  rewind        resuming from an earlier offset reaches the same state
  holds         a closed order has released its hold to exactly zero
  lots          no negative quantity, no negative cost, no phantom position
  as-of         as-of at the final event equals the live snapshot
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import Book                                          # noqa: E402
from money import ZERO, dec                                    # noqa: E402


def load(path: str) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def feed(events: list[dict]) -> Book:
    book = Book()
    for ev in events:
        if ev.get("type") == "checkpoint_request":
            continue
        book.apply(ev)
    return book


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")

    def report(self) -> int:
        for f in self.failures:
            print(f"  FAIL  {f}")
        print(f"\n{self.passed} passed, {len(self.failures)} failed")
        return 1 if self.failures else 0


def main(path: str) -> int:
    events = load(path)
    ledger_events = [e for e in events if e.get("type") != "checkpoint_request"]
    print(f"{len(events)} records, {len(ledger_events)} ledger events\n")

    book = feed(events)
    state = book.state
    c = Checks()

    # Every entry balanced. post() would have raised, so this re-checks what
    # was actually submitted rather than trusting that it did.
    for eid, legs in book.legs.items():
        dr = sum((dec(l["debit"]) for l in legs), ZERO)
        cr = sum((dec(l["credit"]) for l in legs), ZERO)
        c.check("balanced", dr == cr, f"{eid} Dr {dr} Cr {cr}")

    total = sum(state.balances.values(), ZERO)
    c.check("zero-sum", total == ZERO, f"trial balance sums to {total}")

    # Determinism: the same log folded twice reaches the same place.
    c.check("deterministic", book.replay_state().snapshot() == state.snapshot())

    # Idempotency: re-delivering a window must not move anything.
    before = state.snapshot()
    window = ledger_events[len(ledger_events) // 3:][:300]
    for ev in window:
        book.apply(ev)
    c.check("idempotent", book.state.snapshot() == before,
            f"{len(window)} re-delivered events changed the book")

    # Rewind: what the deliberate mid-run reset does to us.
    cut = len(ledger_events) // 2
    rewound = feed(ledger_events[:cut] + ledger_events[cut - 200:])
    c.check("rewind", rewound.state.snapshot() == feed(ledger_events).state.snapshot(),
            "resuming from an earlier offset diverged")

    # A closed order always returns its hold to exactly zero.
    for oid, order in state.book.orders.items():
        if order.closed:
            c.check("holds", order.hold_remaining == ZERO,
                    f"{oid} closed with {order.hold_remaining} still held")

    # The lot book cannot hold a negative, and must not report a position that
    # should not exist.
    for (cid, symbol), lots in state.lots._lots.items():
        for lot in lots:
            c.check("lots", lot.quantity >= ZERO,
                    f"{cid}/{symbol} negative quantity {lot.quantity}")
            c.check("lots", lot.cost >= ZERO,
                    f"{cid}/{symbol} negative cost {lot.cost}")
    for cid, positions in state.lots.positions().items():
        for symbol, (q, _cost) in positions.items():
            c.check("lots", q > ZERO, f"{cid}/{symbol} reported at {q}")

    # As-of at the last event is the present.
    if book.log:
        c.check("as-of", book.snapshot(book.log[-1]["event_id"]) == state.snapshot(),
                "as-of at the final event differs from live state")

    print("run report:")
    print(json.dumps(book.report(), indent=2)[:3000])
    print()
    return c.report()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
