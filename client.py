#!/usr/bin/env python3
"""The arena client: transport, and the instrumentation the transport needs.

The starter kit shipped this finished, and most of it still is. Four things had
to change, and one of them is the reason the rest of the work is affordable:

  * **It records.** Every event that arrives is written to a fixture, and every
    response the server sends back is written to a diagnostics log. Practice
    mode grades each posting as it lands and names the accounts it disagrees
    with; the original client threw all of that away. With 12 practice runs in
    total and a 20-minute wait for each, iterating against a recorded run
    rather than a live one is the difference between a dozen experiments and
    hundreds.
  * **`&new=true`.** On submission and final an attempt is scarce, so the
    server will not start one on a bare reconnect - it answers 409. Without
    this flag a graded run cannot be started at all.
  * **As-of checkpoints.** Some checkpoint requests ask about a past event
    rather than the present, so the whole payload is handed to the book.
  * **It does not die.** The SSE frame parse was unguarded and only
    `httpx.HTTPError` was caught around the consume loop, so one malformed
    frame ended the run. The most expensive mistake available here is stopping.

    python client.py --key ak_... --mode practice
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

from book import Book

# Nominal run lengths are just that: the stream is staggered and drains its
# tail afterwards, so let stream_end end the run and keep a wide margin.
RUN_SECONDS = {"practice": 2400, "submission": 5400, "final": 6600}

# Never sleep longer than this on a throttle, however large the Retry-After.
MAX_BACKOFF = 10.0

# The first event can be 90 seconds out, so a read timeout has to clear that
# comfortably. Without one httpx waits forever, and a silently stalled socket
# would take the whole run down with it - we would sit reading a dead
# connection while the server counted our latency. On timeout we reconnect
# from the cursor, which costs nothing because the stream is resumable.
STREAM_READ_TIMEOUT = 180.0


class Recorder:
    """Writes the run to disk: the events in, the gradings back.

    The run id only arrives with the first frame, so records are buffered until
    there is a directory to name after it.
    """

    def __init__(self, root: str = "runs", enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled
        self.dir: str | None = None
        self._buffer: list[tuple[str, dict]] = []
        self._files: dict[str, object] = {}

    def bind(self, run_id: str) -> None:
        if not self.enabled or self.dir:
            return
        self.dir = os.path.join(self.root, run_id or "unknown")
        os.makedirs(self.dir, exist_ok=True)
        buffered, self._buffer = self._buffer, []
        for stream, record in buffered:
            self.write(stream, record)

    def write(self, stream: str, record: dict) -> None:
        if not self.enabled:
            return
        if not self.dir:
            self._buffer.append((stream, record))
            return
        fh = self._files.get(stream)
        if fh is None:
            fh = self._files[stream] = open(
                os.path.join(self.dir, f"{stream}.jsonl"), "a", encoding="utf-8")
        fh.write(json.dumps(record, default=str) + "\n")
        fh.flush()

    def close(self) -> None:
        for fh in self._files.values():
            fh.close()
        self._files.clear()


class ArenaClient:
    def __init__(self, url: str, key: str, mode: str, batch: int = 100,
                 flush_ms: int = 400, record: bool = True) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.mode = mode
        self.batch = batch
        self.flush_ms = flush_ms
        self.book = Book()
        self.rec = Recorder(enabled=record)
        self.pending: list[dict] = []
        self.cursor = 0
        self.run_id = ""
        self.started_run = False        # has &new=true been spent yet
        self._reset = False             # set by stream_reset, cleared on reconnect
        self.stats = {"events": 0, "posted": 0, "checkpoints": 0,
                      "reconnects": 0, "resets": 0, "errors": 0,
                      "duplicates": 0, "graded_ok": 0, "graded_bad": 0,
                      "throttled": 0}
        self.sent_at: dict[str, float] = {}   # event_id -> when we received it
        self.timing: list[tuple] = []         # (request_s, queue_wait_s, batch)
        self.done = False

    # -- submitting ---------------------------------------------------------
    def flush(self, http: httpx.Client) -> None:
        """Postings go up in batches; one request per event falls behind.

        Latency is measured by the server from when it sent an event to when
        our posting for it arrives, so anything that parks this loop is paid
        for in liveness. Two things could park it and both are now bounded:
        a 429 with a large `Retry-After`, and a slow response. Every outcome
        is timed and recorded, because a run that loses liveness without
        leaving evidence cannot be debugged afterwards.
        """
        if not self.pending:
            return
        body, self.pending = {"postings": self.pending[:500]}, self.pending[500:]
        oldest = min((self.sent_at.get(p["event_id"], time.time())
                      for p in body["postings"]), default=time.time())
        started = time.time()
        try:
            r = http.post(f"{self.url}/v1/postings", params={"mode": self.mode},
                          json=body, timeout=30)
            elapsed = time.time() - started
            self.timing.append((elapsed, started - oldest, len(body["postings"])))

            if r.status_code == 429:
                # Honour the server, but never disappear for minutes: the
                # queue keeps growing while we sleep and every event in it is
                # accruing latency.
                wait = min(float(r.headers.get("Retry-After", 2) or 2), MAX_BACKOFF)
                self.stats["throttled"] += 1
                self.rec.write("diag", {"kind": "throttled", "retry_after":
                                        r.headers.get("Retry-After"), "slept": wait,
                                        "queued": len(body["postings"])})
                self.pending = body["postings"] + self.pending
                time.sleep(wait)
                return
            r.raise_for_status()
            self.stats["posted"] += len(body["postings"])
            if elapsed > 5:
                self.rec.write("diag", {"kind": "slow_post", "seconds": round(elapsed, 1),
                                        "batch": len(body["postings"])})
            self._record_grades(r)
        except httpx.HTTPError as exc:
            self.stats["errors"] += 1
            self.rec.write("diag", {"kind": "post_error", "error": repr(exc),
                                    "seconds": round(time.time() - started, 1),
                                    "batch": len(body["postings"])})
            self.pending = body["postings"] + self.pending
            time.sleep(1)

    def _record_grades(self, response: httpx.Response) -> None:
        """Keep whatever the server says about what we just sent.

        In practice this is per-event: correct or not, balanced or not, and the
        accounts we disagree on. That is the entire feedback loop, so it is
        written to disk before anything is done with it.
        """
        try:
            body = response.json()
        except ValueError:
            return
        self.rec.write("diag", {"kind": "postings", "body": body})
        for item in (body.get("results") or body.get("postings") or []):
            if not isinstance(item, dict):
                continue
            if item.get("duplicate"):
                continue
            correct = item.get("correct")
            if correct is True:
                self.stats["graded_ok"] += 1
            elif correct is False:
                self.stats["graded_bad"] += 1

    def checkpoint(self, http: httpx.Client, payload: dict) -> None:
        """Snapshot first, send second.

        The reply must describe the book as at the checkpoint's place in the
        stream. Taking the snapshot after the round trip reports a later state
        than the one being asked about. An as-of request asks about a named
        past event instead, which the book answers by replaying its log.
        """
        cp_id = payload.get("checkpoint_id")
        as_of = payload.get("as_of_event_id")
        try:
            snap = self.book.snapshot(as_of)
        except Exception as exc:                    # noqa: BLE001
            self.stats["errors"] += 1
            print(f"  checkpoint {cp_id} snapshot failed: {exc!r}", flush=True)
            return

        self.flush(http)
        try:
            r = http.post(f"{self.url}/v1/checkpoint", params={"mode": self.mode},
                          json={"checkpoint_id": cp_id, **snap}, timeout=30)
            self.stats["checkpoints"] += 1
            try:
                body = r.json()
            except ValueError:
                body = {"status": r.status_code}
            self.rec.write("diag", {"kind": "checkpoint", "checkpoint_id": cp_id,
                                    "as_of": as_of, "sent": snap, "body": body})
        except httpx.HTTPError:
            self.stats["errors"] += 1

    # -- consuming ----------------------------------------------------------
    def handle(self, ev: dict) -> None:
        """One ledger event in, one submission out.

        An event we have already answered is not resubmitted: the first
        submission is the one scored, so a re-delivery during the replay would
        only add traffic.
        """
        self.rec.write("events", ev)
        already = self.book.seen(ev.get("event_id"))
        legs = self.book.apply(ev)
        if already:
            self.stats["duplicates"] += 1
            return
        self.sent_at[ev["event_id"]] = time.time()
        self.pending.append({"event_id": ev["event_id"], "legs": legs or []})
        self.stats["events"] += 1

    def consume(self, http: httpx.Client, deadline: float) -> None:
        params = {"mode": self.mode, "from": self.cursor}
        if self.mode != "practice" and not self.started_run:
            # An attempt here is scarce: the server refuses to start one unless
            # asked explicitly, so a dropped connection can never spend it.
            params["new"] = "true"

        last_flush = time.time()
        with http.stream("GET", f"{self.url}/v1/stream", params=params,
                         timeout=httpx.Timeout(None, connect=20,
                                               read=STREAM_READ_TIMEOUT)) as r:
            r.raise_for_status()
            self.started_run = True
            etype = data = None
            for line in r.iter_lines():
                if time.time() > deadline:
                    return
                if line.startswith("event:"):
                    etype = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                elif line == "" and data is not None:
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        # A payload that will not parse. Reject it, carry on.
                        self.stats["errors"] += 1
                        self.rec.write("diag", {"kind": "unparseable", "raw": data[:2000]})
                        etype = data = None
                        continue

                    try:
                        self.dispatch(http, etype, ev)
                    except Exception as exc:        # noqa: BLE001
                        self.stats["errors"] += 1
                        print(f"  event error {type(exc).__name__}: {exc}", flush=True)
                    if self.done or self._reset:
                        self.flush(http)
                        return

                    if (len(self.pending) >= self.batch
                            or (time.time() - last_flush) * 1000 > self.flush_ms):
                        self.flush(http)
                        last_flush = time.time()
                    etype = data = None

    def dispatch(self, http: httpx.Client, etype: str, ev: dict) -> None:
        if etype == "stream_open":
            self.run_id = ev.get("run_id") or self.run_id
            self.rec.bind(self.run_id)
            self.rec.write("diag", {"kind": "stream_open", "body": ev})
            print(f"  connected: run {self.run_id}, "
                  f"resumed at {ev.get('resumed_from')}, "
                  f"next event in {ev.get('next_event_in_seconds')}s", flush=True)
        elif etype == "stream_reset":
            # A deliberate rewind: the server re-sends events already seen. An
            # idempotent consumer notices nothing.
            self.cursor = ev.get("resume_from", self.cursor)
            self.stats["resets"] += 1
            self._reset = True
            print(f"  stream reset, resuming from {self.cursor}", flush=True)
        elif etype == "stream_end":
            self.done = True
            print("  stream ended", flush=True)
        else:
            self.cursor = max(self.cursor, ev.get("offset", 0) + 1)
            if ev.get("type") == "checkpoint_request":
                self.rec.write("events", ev)
                self.checkpoint(http, ev.get("payload") or {})
            else:
                self.handle(ev)

    def run(self, max_seconds: float) -> dict:
        deadline = time.time() + max_seconds
        headers = {"Authorization": f"Bearer {self.key}"}
        with httpx.Client(headers=headers) as http:
            while time.time() < deadline and not self.done:
                self._reset = False
                try:
                    self.consume(http, deadline)
                except httpx.HTTPError as exc:
                    self.stats["reconnects"] += 1
                    print(f"  reconnecting after {type(exc).__name__}", flush=True)
                    time.sleep(1)
            self.flush(http)
            try:
                me = http.get(f"{self.url}/v1/me", params={"mode": self.mode},
                              timeout=20).json()
            except httpx.HTTPError:
                me = {}
        self.rec.write("diag", {"kind": "me", "body": me})
        self.rec.write("diag", {"kind": "report", "body": self.book.report()})
        self.rec.close()
        return {"stats": self.stats, "me": me}


def show(out: dict, book: Book, client: "ArenaClient" = None) -> None:
    print("\nstats:", json.dumps(out["stats"]))

    # Liveness is 10 points and the server measures it from its own send time,
    # so our side of that latency is worth printing every run rather than
    # reconstructing it after a bad one.
    if client and client.timing:
        waits = sorted(w for _req, w, _n in client.timing)
        reqs = sorted(r for r, _w, _n in client.timing)
        p95 = lambda xs: xs[min(len(xs) - 1, int(len(xs) * 0.95))]
        print(f"\nour posting latency: queue wait p95 {p95(waits):.1f}s "
              f"max {waits[-1]:.1f}s | request p95 {p95(reqs):.1f}s "
              f"max {reqs[-1]:.1f}s | flushes {len(client.timing)}")
        if waits[-1] > 30:
            print("  WARNING: events sat in the queue for a long time - "
                  "check diag.jsonl for throttled / slow_post / post_error")

    report = book.report()
    for bucket in ("unhandled", "rejected", "malformed", "errors", "detected"):
        entries = report.get(bucket) or {}
        if not entries:
            continue
        total = sum(entries.values())
        print(f"\n{bucket} ({total}):")
        for key, n in list(entries.items())[:15]:
            print(f"  {n:>5}  {key}")

    me = out.get("me") or {}
    latest = me.get("latest_run") or me
    if latest.get("score") is not None:
        print(f"\nscore: {latest['score']}")
        for k, v in (latest.get("breakdown") or {}).items():
            print(f"  {k:<26} {v['points']:>7} / {v['max']}")
    else:
        print("\nscore: withheld on this tier")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get(
        "ARENA_URL", "https://hiring-arena.twocc.in"))
    ap.add_argument("--key", default=os.environ.get("ARENA_KEY"),
                    help="your API key from the portal")
    ap.add_argument("--mode", default="practice",
                    choices=["practice", "submission", "final"])
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation on a graded tier")
    a = ap.parse_args()

    if not a.key:
        print("no API key: pass --key or set ARENA_KEY")
        return 2

    if a.mode != "practice" and not a.yes:
        print(f"\n  You are about to start a {a.mode.upper()} run.")
        print("  Attempts are limited and this one will count.")
        if input("  Type the mode name to continue: ").strip() != a.mode:
            print("  Cancelled.")
            return 1

    seconds = a.seconds or RUN_SECONDS[a.mode]
    c = ArenaClient(a.url, a.key, a.mode, record=not a.no_record)
    print(f"connecting to {a.url} as {a.mode} ...", flush=True)
    out = c.run(seconds)
    show(out, c.book, c)
    if c.rec.dir:
        print(f"\nrecorded to {c.rec.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
