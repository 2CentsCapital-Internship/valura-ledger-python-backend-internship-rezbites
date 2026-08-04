#!/usr/bin/env python3
"""Turn a recorded diagnostics log into the table that decides the next fix.

Practice grades every posting as it lands and names the accounts it disagrees
with. This reads that back and answers two questions: which event types are we
wrong on, and which accounts do we disagree about. Those two together point at
a cause; anything else is guessing.

    python tools/diag.py runs/<run_id>/diag.jsonl

The server's exact response shape is not documented, so this discovers it: the
first posting response is printed verbatim, then parsed with a tolerant reader.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def event_types(run_dir: str) -> dict[str, str]:
    """event_id -> type, from the recorded event stream alongside the log."""
    path = os.path.join(run_dir, "events.jsonl")
    types: dict[str, str] = {}
    if not os.path.exists(path):
        return types
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("event_id"):
                types[ev["event_id"]] = ev.get("type", "?")
    return types


def results_in(body) -> list[dict]:
    """Pull per-event verdicts out of whatever shape the response takes."""
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("results", "postings", "graded", "events", "detail", "details"):
        value = body.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    # A single verdict, unwrapped.
    if "event_id" in body:
        return [body]
    return []


def verdict(item: dict):
    for key in ("correct", "ok", "is_correct", "passed"):
        if key in item:
            return bool(item[key])
    if "score" in item and isinstance(item["score"], (int, float)):
        return item["score"] > 0
    return None


def accounts_in(item: dict) -> list[str]:
    for key in ("accounts", "disagree", "disagreements", "mismatched_accounts",
                "wrong_accounts", "diff", "differences"):
        value = item.get(key)
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, dict):
            return [str(k) for k in value]
    return []


def main(path: str) -> int:
    records = load(path)
    run_dir = os.path.dirname(os.path.abspath(path))
    types = event_types(run_dir)

    postings = [r for r in records if r.get("kind") == "postings"]
    checkpoints = [r for r in records if r.get("kind") == "checkpoint"]

    if postings:
        print("=" * 70)
        print("first posting response, verbatim (to learn the shape)")
        print("=" * 70)
        print(json.dumps(postings[0]["body"], indent=2)[:2500])
        print()

    per_type = defaultdict(lambda: Counter())
    bad_accounts = Counter()
    unknown_shape = 0

    for rec in postings:
        items = results_in(rec.get("body"))
        if not items:
            unknown_shape += 1
        for item in items:
            etype = types.get(item.get("event_id"), item.get("type", "?"))
            if item.get("duplicate"):
                per_type[etype]["duplicate"] += 1
                continue
            v = verdict(item)
            if v is True:
                per_type[etype]["correct"] += 1
            elif v is False:
                per_type[etype]["wrong"] += 1
                for account in accounts_in(item):
                    bad_accounts[account] += 1
            else:
                per_type[etype]["ungraded"] += 1

    if per_type:
        print("=" * 70)
        print(f"{'event type':<30}{'correct':>9}{'wrong':>8}{'rate':>8}")
        print("=" * 70)
        rows = sorted(per_type.items(), key=lambda kv: -kv[1]["wrong"])
        for etype, counts in rows:
            ok, bad = counts["correct"], counts["wrong"]
            total = ok + bad
            rate = f"{100 * ok / total:.0f}%" if total else "-"
            print(f"{etype:<30}{ok:>9}{bad:>8}{rate:>8}")
        tot_ok = sum(c["correct"] for c in per_type.values())
        tot_bad = sum(c["wrong"] for c in per_type.values())
        print("-" * 70)
        print(f"{'TOTAL':<30}{tot_ok:>9}{tot_bad:>8}"
              f"{(100 * tot_ok / (tot_ok + tot_bad)) if tot_ok + tot_bad else 0:>7.0f}%")

    if bad_accounts:
        print("\naccounts we disagree on:")
        for account, n in bad_accounts.most_common(20):
            print(f"  {n:>5}  {account}")

    if unknown_shape:
        print(f"\n{unknown_shape} posting responses had no readable verdicts")

    for rec in checkpoints:
        body = rec.get("body")
        print(f"\ncheckpoint {rec.get('checkpoint_id')}"
              f"{' (as-of ' + rec['as_of'] + ')' if rec.get('as_of') else ''}:")
        print(json.dumps(body, indent=2)[:2000])

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
