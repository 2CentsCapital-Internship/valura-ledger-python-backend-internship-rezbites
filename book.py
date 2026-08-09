"""The book: ingestion, idempotency, and answering questions about history.

`client.py` hands one event in and takes its journal legs back. Everything about
*what* the legs are lives in `ledger.py`; this module is about *when* they are
produced and how the same question can be asked of the past.

The design decision that shapes the rest is that **state is a fold of the
delivery-ordered event log**. Every first delivery is appended to a log, and
the live ledger is that log applied incrementally. Three requirements fall out
of it rather than being bolted on:

  * **As-of checkpoints.** Some checkpoint requests ask what the book looked
    like once a named event had been processed "and nothing after it". Current
    state cannot answer that; a log prefix replayed into a fresh ledger can.
  * **Idempotency.** A duplicate never reaches the ledger, so a re-delivery
    cannot move a balance.
  * **The replay.** At an unannounced point the server drops the connection and
    rewinds several hundred events. Since those arrive as duplicates, an
    idempotent consumer notices nothing.

Nothing here is allowed to stop the stream. The single most expensive mistake
available is crashing: one event refused costs one event, and a server that
stops misses everything after it.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import InvalidOperation
from typing import Optional

import validate
from ledger import Ledger, Rejected


# Send one event to the right handler, and catch every failure so we never stop.
def _dispatch(state: Ledger, ev: dict, counters: Optional[dict] = None) -> list[dict]:
    """Apply one event to a ledger and return its legs.

    Deterministic in (ledger state, event), which is what makes replay produce
    exactly the state the live pass produced.
    """
    # Add one to a tally so the end-of-run summary can show it.
    def count(bucket: str, key: str) -> None:
        if counters is not None:
            counters[bucket][key] += 1

    event_id, etype = ev["event_id"], ev["type"]

    # The defect hunt. Detectors count always and reject only once armed.
    for name, reason in validate.inspect(ev, state):
        count("detected", f"{etype}/{name}")
        if name in validate.ARMED:
            count("rejected", f"{etype}: {name}")
            return []

    handler = getattr(state, "on_" + etype, None)
    if handler is None:
        count("unhandled", etype)
        return []

    try:
        legs = handler(ev["payload"], ev) or []
        return state.post(event_id, legs)
    except Rejected as exc:
        # Refused on its own merits. No legs, and the book is as it was.
        state.rollback(event_id)
        count("rejected", f"{etype}: {exc}")
        return []
    except (KeyError, IndexError, InvalidOperation, TypeError, ValueError) as exc:
        # A payload that will not parse. Reject it, carry on.
        state.rollback(event_id)
        count("malformed", f"{etype}: {type(exc).__name__} {exc}")
        return []
    except Exception as exc:                       # noqa: BLE001 - never stop the stream
        state.rollback(event_id)
        count("errors", f"{etype}: {type(exc).__name__} {exc}")
        return []


# The doorman: decides whether an event gets processed, and remembers the history.
class Book:
    # Start with an empty book and an empty list of events.
    def __init__(self) -> None:
        self.state = Ledger()
        self.log: list[dict] = []                 # first deliveries, in order
        self.legs: dict[str, list[dict]] = {}     # what we answered, per event
        self.counters = {b: defaultdict(int) for b in
                         ("applied", "detected", "rejected", "malformed",
                          "unhandled", "errors")}
        self.duplicates = 0

    # -- ingestion -----------------------------------------------------------
    # The main entrance: is it readable, have we seen it before, then process it.
    def apply(self, ev: dict) -> list[dict]:
        """Post one event and return its legs. Safe to call with anything."""
        try:
            validate.structural(ev)
        except validate.Malformed as exc:
            eid = (ev or {}).get("event_id") if isinstance(ev, dict) else None
            self.counters["malformed"][f"structural: {exc}"] += 1
            if eid:
                self.legs.setdefault(eid, [])
            return []

        event_id = ev["event_id"]
        if event_id in self.legs:
            # An id we have seen is an id we have seen, whatever we did with
            # it. Re-delivery must not move a balance.
            self.duplicates += 1
            return self.legs[event_id]

        self.log.append(ev)
        legs = _dispatch(self.state, ev, self.counters)
        self.counters["applied"][ev["type"]] += 1
        self.legs[event_id] = legs
        return legs

    # Have we already dealt with this event?
    def seen(self, event_id: str) -> bool:
        return event_id in self.legs

    # -- reporting -----------------------------------------------------------
    # The report - either as things are now, or as they stood at an event in the past.
    def snapshot(self, as_of_event_id: Optional[str] = None) -> dict:
        """The state a checkpoint wants.

        With `as_of_event_id`, the state as it stood once that event had been
        processed in delivery order and nothing after it - so a backdated event
        that arrived later is not in the answer.
        """
        if not as_of_event_id:
            return self.state.snapshot()

        cut = None
        for i, ev in enumerate(self.log):
            if ev["event_id"] == as_of_event_id:
                cut = i + 1
                break
        if cut is None:
            # Asked about an event we never received. Current state is the
            # closest honest answer; answering nothing scores nothing.
            self.counters["errors"][f"as_of unknown: {as_of_event_id}"] += 1
            return self.state.snapshot()

        past = Ledger()
        for ev in self.log[:cut]:
            _dispatch(past, ev)
        return past.snapshot()

    # Rebuild the whole book from scratch using the saved list of events.
    def replay_state(self, upto: Optional[int] = None) -> Ledger:
        """A fresh ledger folded from the log. Used by the offline harness to
        prove that incremental state and replayed state agree."""
        past = Ledger()
        for ev in self.log[:upto]:
            _dispatch(past, ev)
        return past

    # Summary of what we handled, refused, and could not read.
    def report(self) -> dict:
        """A run summary: what we handled, refused, and could not read."""
        return {
            "events": len(self.log),
            "duplicates": self.duplicates,
            **{bucket: dict(sorted(counts.items(), key=lambda kv: -kv[1]))
               for bucket, counts in self.counters.items()},
        }
