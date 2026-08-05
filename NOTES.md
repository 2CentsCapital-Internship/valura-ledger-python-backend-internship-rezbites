# Notes on this implementation

This is the reasoning behind the code, written to be read start to finish. It
assumes no accounting background, because the interesting decisions here are
accounting decisions rather than programming ones.

> **The specification this is built against is the live one at
> <https://hiring-arena.twocc.in/protocol>, not the `PROTOCOL.md` in this
> repository.** The two disagree materially - see [Which spec](#which-spec) at
> the end.

---

## 1. What a book of record is

A broker holds other people's money and other people's shares. At any moment it
has to be able to say, and prove, how much cash it holds, how much of that it
owes to each customer, what shares each customer owns and what those shares
cost them, and what the firm itself has earned and owes to other businesses.

The method for keeping that straight is **double-entry bookkeeping**, and it is
about 500 years old. Every transaction is recorded twice, in two or more
*accounts*, as equal **debits** and **credits**. Money is never created or
destroyed by an entry; it only moves.

A customer deposits $1,000:

```
Dr 1100  Omnibus Cash at Broker   1000.00      the firm's cash goes up
Cr 2010  Customer Wallet          1000.00      what the firm owes them goes up
```

The firm is not $1,000 richer. It is holding $1,000 that it owes back. That is
why both sides move. If the debits and credits of an entry ever fail to match,
something is wrong by construction - which is the whole point of the method,
and why `Ledger.post` refuses to apply an unbalanced entry at all.

**Debit and credit are not "plus" and "minus".** Which one increases an account
depends on what kind of account it is:

| Account type | Examples | Increased by |
| --- | --- | --- |
| Asset - things the firm has | cash (1100), custody (1200) | debit |
| Liability - things the firm owes | wallets (2010), payables (24xx) | credit |
| Income - what the firm earns | brokerage (4000), FX spread (4100) | credit |
| Expense - what the firm spends | broker cost (5000), partner share (5100) | debit |

Throughout this code balances are stored **debit-positive**: an asset with money
in it is positive, and a liability the firm owes is negative. That is also the
convention the checkpoint asks for. `Ledger.owed()` flips the sign when what we
want is "how much is outstanding on this payable".

## 2. The two mistakes that are easy to make and hard to see

**Balances are keyed by `(customer_id, account)`, never by account alone.**
`transfer_between_customers` moves money from one customer to another, and both
of its legs land on account 2010. Summed by account, the two cancel and the
event looks like it never happened - and the trial balance still agrees, so
nothing complains. Only a per-customer key shows it. This is why
`Ledger.balances` is a dict keyed by a tuple.

**Money is `Decimal`, never `float`.** The spec rounds every derived amount to
the cent *independently* and *half away from zero*, and it deliberately creates
cases that land exactly on a half cent - `partner_rate` can be 0.50, so an odd
number of cents halves onto one. Python's built-in `round()` is half-to-*even*
and would send those the other way, and a float cannot represent 0.01 exactly
in the first place. Everything routes through `money.money()`, and `money.dec()`
raises if a float ever reaches it, because by then the damage is invisible.

## 3. How an event becomes journal legs

```
client.py   SSE frame  ->  book.Book.apply(event)  ->  legs  ->  POST /v1/postings
                                    |
                                    v
                            ledger.Ledger.on_<type>()
```

`client.py` is transport: it subscribes, survives the deliberate mid-run
disconnect, batches submissions and answers checkpoints. `book.py` decides
*whether* an event is processed. `ledger.py` decides *what* it posts.

### The design decision everything else follows from

**State is a fold of the delivery-ordered event log.** `Book` keeps every
first delivery in `self.log`, and the live `Ledger` is that log applied one
event at a time. This is not tidiness for its own sake; it is forced by one
requirement in the spec:

> Some checkpoint requests carry `as_of_event_id`, and must be answered
> "as it stood once you had processed that event, in delivery order, and
> nothing after it."

Current state cannot answer that question. A log prefix replayed into a fresh
`Ledger` can, and that is exactly what `Book.snapshot(as_of)` does. At 6,000
events a full replay takes milliseconds, so nothing cleverer is warranted.

Two more requirements then come for free rather than needing their own
machinery:

- **Idempotency.** `Book.apply` checks `self.legs` before anything else, so a
  duplicate never reaches the ledger and cannot move a balance.
- **The replay.** At an unannounced point the server drops the connection and
  rewinds several hundred events. They arrive as duplicates, so an idempotent
  consumer notices nothing. `tools/replay.py` asserts this rather than hoping.

### Nothing is allowed to stop the stream

`_dispatch` catches three kinds of failure and keeps going:

| Outcome | Meaning | Result |
| --- | --- | --- |
| `Rejected` | refused on its own merits - an oversell, a negative FX spread | `legs: []`, book unchanged |
| parse errors | a payload that will not parse | `legs: []`, counted as malformed |
| anything else | a bug in this code | `legs: []`, counted and printed |

All three roll the lot book back first, because a fill relieves cost *before*
its entry is posted, and the spec is explicit that a rejected oversell must not
leave lots half-consumed. An event that produces no legs must leave the book
exactly as it was.

Every one of these still **submits** `"legs": []`. About one event in seven
correctly produces no legs, and submitting nothing scores zero for that event.

## 4. The fee chain

This is the largest single piece of derivation in the assignment, because the
feed contains **no fee amounts at all**. A fill tells us the principal, which
broker took it, and the partner rate. The tariff turns those into six amounts.

| Broker | Trades | Brokerage | Custody | Broker cost | Custody cost | Min fee | Ticket |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BRK-A | equity, etf | 20 bps | 4 bps | 9 bps | 2 bps | 1.00 | 0.35 |
| BRK-B | equity, bond | 15 bps | 5 bps | 8 bps | 3 bps | 2.50 | 3.00 |
| BRK-C | etf, bond | 25 bps | 3 bps | 12 bps | 1 bps | 0.50 | 0.20 |

("bps" is a basis point: one hundredth of one percent. 20 bps of 1,000 is 2.00.)

Three of the six are **charged to the customer**, and three are **costs the firm
bears**:

```
b   brokerage      customer pays  ->  firm income        4000
c   custody        customer pays  ->  firm income        4010
r   regulatory     customer pays  ->  owed to the venue  2400
bc  broker cost    firm pays the executing broker        5000 / 241x
cc  custody cost   firm pays the custodian               5010 / 2420
ps  partner share  firm pays the introducing partner     5100 / 2430
```

Note the shape of the last three. Each is booked **twice**: as an expense (what
it cost) and as a payable (what is still owed). That is what lets the four
settlement events later pay off the payable without touching the expense - the
cost was incurred when the trade happened, and paying it is a separate event.

The spec is emphatic that revenue and cost are booked **gross**: a book that
posts only the margin balances perfectly and can never say what it earned or
what it cost.

Three readings of the tariff text were judgement calls, all three now
**confirmed by practice** - every non-duplicate fill scored correct:

1. **The minimum fee floors brokerage only.** The text says "the brokerage
   charge is floored at the broker's minimum fee" and says nothing of the kind
   about custody.
2. **The ticket fee is part of broker cost.** The text says every fill "costs
   the firm the broker's flat ticket fee". Putting it in the firm's cost is
   what makes the spec's own remark true - that the ticket makes roughly a
   quarter of fills loss-making - since without it almost none would be.
3. **Partner share is computed from already-rounded components.** The spec
   rounds each derived amount independently before use, so revenue and cost are
   rounded first and the rate applied to the difference. This is exactly the
   case the spec warns lands on a half cent.

`partner_rate x (revenue - cost)`, and where cost exceeds revenue the share is
**zero, with no clawback** - the partner does not refund the firm for a
loss-making trade.

### Routing

Among the brokers that trade the order's asset class, the order goes to the one
with the lowest total customer charge (brokerage + custody) on
`quantity x limit_price`, ties broken on broker id ascending. Fills name their
own broker, so this decision only ever surfaces in `open_order_routes` at a
checkpoint - it is the one place the routing choice is ours to make.

## 5. Fills, and why cash does not move

A buy, for principal `P`:

```
Dr 2010  P + b + c + r      the customer pays principal and every charge
Dr 1200  P                  the firm now holds the shares in omnibus custody
Cr 2350  P                  ...and owes the broker for them until settlement
Cr 2100  P                  the customer has a claim on those shares
Dr 5000 bc / Cr 241x bc     the firm's cost, and what it owes the broker
Dr 5010 cc / Cr 2420 cc     the firm's cost, and what it owes the custodian
Dr 5100 ps / Cr 2430 ps     the firm's cost, and what it owes the partner
Cr 4000 b, Cr 4010 c        the firm's revenue
Cr 2400 r                   collected for the venue, owed onward
```

**Cash (1100) is untouched.** Trades settle two days later, so the fill creates
an obligation and a separate `trade_settled` discharges it. A book that moves
cash on the trade date disagrees with the broker for exactly as long as anything
is unsettled.

Note also that the shares go into custody at `P`, not at `P + charges`. The
charges are not part of what the shares cost; they are consumed immediately.

A sell is the same economics in reverse, and it is the one entry the spec does
**not** work for you:

```
Dr 1150  P                  the broker owes the firm the proceeds
Cr 2010  P - b - c - r      the customer is credited net of their charges
Dr 2100  k / Cr 1200  k     custody and the claim shrink by COST, not proceeds
   ... the firm's revenue, cost, regulatory and partner legs, identical to a buy
```

The asymmetry is deliberate and is where the money is. Custody shrinks by `k`,
the **cost** of the shares sold, while the customer is credited the **proceeds**.
The difference between them is the customer's realised gain or loss, and it is
the residual of these legs. It is never posted directly.

## 6. The lot book

`lots.py` is the highest-value file here: cost basis is 64% of the checkpoint
score, and the checkpoint block is 40 of 100.

A **lot** is one purchase: a quantity of shares and what the whole parcel cost.
When a customer sells, which shares did they sell? The convention is
**first-in-first-out**, and two details are graded exactly:

- **FIFO means delivery order, not trade date.** The stream is deliberately not
  date-ordered, so lots are consumed in the order the buys reached us. This is
  why the lot book is a plain list appended to as events arrive, and never
  sorted.
- **Partial consumption relieves `round(lot_total x sold_qty / lot_qty)`**, with
  the remainder staying on the lot. Keeping a cost *per share* and multiplying
  it out is also FIFO and disagrees by a cent, so it would be wrong here.

Four events mutate the lot book without any of them being a purchase:

| Event | Effect on lots |
| --- | --- |
| `dividend_reinvested` | a new lot of `reinvest_quantity` costing the net amount; no cash involved |
| `stock_split` | quantities scale by `ratio_to / ratio_from`; each lot's **total cost is unchanged**, so cost per share moves and the position's cost basis does not |
| `symbol_change` | the holding is re-keyed; order preserved |
| `reversal` | the original's lot effect is undone |

That last one is why every mutation returns an **undo record**. A reversal must
undo the original event's effect on the lot book and not merely on the accounts
- a reversed buy whose lot is left in place balances perfectly and quietly
corrupts every cost basis after it.

Two implementation details make undo safe:

- **Consumed lots are kept in place at zero quantity rather than deleted**, so
  restoring one puts it back in its original FIFO position instead of at the end.
- **Undo records hold `Lot` objects, not list indices.** A `symbol_change` moves
  lots between lists, so an index recorded before a rename points somewhere else
  afterwards. The objects survive the move; indices do not.

## 7. Holds

A placement moves no money. It makes money unspendable, which is a different
thing, and it is never posted - holds appear only in checkpoints.

- A buy holds `quantity x limit_price + est_charges` in cash. `est_charges` is a
  conservative estimate supplied by the feed and is used as given.
- A sell holds shares, not cash, so it contributes nothing to `cash_hold`.
- Each fill releases a share of the hold proportional to the quantity filled,
  and the final fill, a cancellation or a rejection releases whatever remains,
  so **a closed order always returns its hold to exactly zero**. `tools/replay.py`
  asserts this over the whole run.
- **The released share is computed on the cumulative quantity filled and
  rounded once.** There are three plausible formulas and they disagree by a
  cent:

  | | Formula |
  | --- | --- |
  | A | total less the sum of each fill's separately rounded release |
  | B | `round(total x unfilled / quantity)` |
  | **C** | **`total - round(total x cumulative_filled / quantity)`** |

  Only C fits the evidence. A was right at the first checkpoint of run
  `run_463ab2612b8c` and wrong at every later one; B was the reverse; C agrees
  with each where it was right. Switching to C changes the answer for exactly
  the customers the server flagged, at exactly the checkpoints it flagged them,
  and for nobody else - checked across all seven checkpoints.

  The intuition is that there is one hold, revalued once against how much of
  the order has filled, rather than a series of independently rounded releases
  each carrying its own rounding error.
- **A reversal does not restore a hold.** A released hold stays released; a
  reversal undoes postings and the lot book, not the lifecycle. This is why
  nothing in `orders.py` has an undo.

An order can also be filled before we ever see it placed, so an `Order` is
created by whichever event mentions it first and completed later.

## 8. The four settlement events

`broker_fees_settled`, `custodian_fees_settled`, `reg_fees_remitted` and
`partner_payout` each discharge one payable in full, for one customer, out of
omnibus cash. **The amount is never in the payload**: it is whatever has
accumulated on that account for that customer.

That makes them an audit. If any per-trade rounding since the last settlement
was wrong, the accumulated balance is wrong, and the settlement posts the wrong
number. They are the spec checking our arithmetic with our own figures.

Settling an account with nothing outstanding is an error, so
`_settle_payable` rejects a non-positive balance.

### Settling a trade that was reversed

`trade_settled` discharges the obligation a fill created. If that fill has since
been **reversed**, the obligation was cancelled with it and there is nothing
left to discharge - paying it would move cash for a trade that no longer exists.
So the settlement posts nothing.

**Delivery order decides this.** A reversal arriving *after* a settlement does
not make the settlement retrospectively wrong: at the moment it happened the
obligation was real. Only a reversal that arrives *first* empties it.

Practice separated the two cleanly. Of 65 `trade_settled` events in run
`run_463ab2612b8c`, the 7 whose fill had already been reversed were the only
ones graded wrong, with both accounts of the entry (`1100` and `2350`) named as
differing; the 6 whose fill was reversed *later* all scored correct. That same
mistake was also the entire trial-balance gap at three checkpoints, which named
`1100` and `2350` and nothing else.

## 9. Rejections, and the systematic defect

The spec guarantees that the feed contains at least one **systematic defect**: a
class of event that is internally well-formed and wrong. It says nothing else
about it. Our own invariants are the only way to find it.

Rejecting wrongly is expensive twice over - the event's own posting score is
lost, *and* the state that should have followed from it is missing from every
checkpoint afterwards. So `validate.py` is built to gather evidence before it
changes behaviour: every detector **counts** by default, and a name is added to
`validate.ARMED` only once a practice run shows we are systematically wrong on
exactly the events it fires on.

### What the defect turned out to be

**A fill re-sent under a fresh `event_id`.** The same fill arrives twice,
byte-identical in every field - same order, quantity, price, principal, broker,
and the same `trade_id` - but with a new `event_id`, so ordinary event-level
deduplication passes it straight through and the trade is booked twice. Each
copy is perfectly well-formed on its own; it is only wrong in the context of the
trade it double-books. That is exactly the shape the spec describes.

The invariant that catches it is that **one trade settles once**, so a
`trade_id` already recorded means the fill is a repeat. Over the 83 fills in
practice run `run_deb24bbe5b3c` this flagged 22 events, the server expected no
legs for every one of them, and it flagged nothing else.

Getting there took one wrong turn worth recording. The first rule tried was
quantity-based - "have this order's fills exceeded the quantity ordered" - which
looked right on the first two examples and is wrong twice over: it misses a
duplicate that lands *inside* the ordered quantity, and it then false-positives
on the legitimate final fill afterwards, because the double-booked shares have
inflated the running total. It is a rule about a symptom. `trade_id` is a rule
about the cause. The quantity check is kept unarmed as an independent tripwire.

The knock-on was larger than the 22 events. The bogus fills accrued fees to the
firm's payable accounts, so the settlement events that later paid those payables
off found a balance where the reference found none: 15 settlement events were
wrong for that reason alone. With the duplicates rejected, all 57 settlement
events agree.

Other detectors written but **not armed**: principal disagreeing with
quantity x price, a broker executing an asset class it does not trade, a symbol
changing asset class mid-run, a dividend whose net is not gross less tax, an
interest share exceeding the interest earned, and a transfer to oneself. One,
`fx_conversion`, fires on 50 of 50 `fx_deposit` events while the server marks
every one of them correct - so the feed's USD figures simply are not the product
of its quoted rates, and that detector is measuring something the feed never
promised. It stays unarmed and is a candidate for deletion.

The rejections the spec states outright are not detectors - they need ledger
state, so they live with the handlers that have it: oversell, a negative FX
spread, a reversal or refund of something never received, a double refund or
double settlement, and settling an empty payable.

## 10. How this is tested

`tools/replay.py` replays a recorded run offline and asserts the properties the
spec demands. It cannot tell us whether a posting agrees with the reference -
only the server knows that - but it can tell us the book contradicts itself, and
a practice attempt is not worth spending until it does not.

```
python tools/replay.py runs/<run_id>/events.jsonl
```

Checks: every entry balanced; the whole trial balance sums to zero; replaying
the log twice gives the same state; re-delivering a 300-event window changes
nothing; resuming from an earlier offset reaches the same state; every closed
order has zero hold; no negative lot quantity or cost and no phantom position;
and as-of at the final event equals the live snapshot.

`tools/diag.py` reads the diagnostics the server returned during a practice run
and prints accuracy per event type plus a histogram of the accounts we disagree
on. That table is what decides the next fix.

```
python tools/diag.py runs/<run_id>/diag.jsonl
```

Recorded runs live under `runs/` and are not committed: they are regenerated by
any run, they accumulate, and `diag.jsonl` contains the grader's own disclosed
`expected_legs`, which does not belong in a graded repository.

## 11. Changes to `client.py`

The kit ships this finished and most of it is untouched. Four things changed:

1. **It records.** Every event in, every grading back, written to
   `runs/<run_id>/`. Practice grades each posting as it lands and names the
   accounts it disagrees with, and the original client discarded all of it.
   With twelve practice attempts at twenty minutes each, iterating against a
   recorded run instead of a live one is the difference between a dozen
   experiments and hundreds.
2. **`&new=true` on submission and final.** An attempt there is scarce, so the
   server will not start one on a bare reconnect - it answers 409. Without the
   flag a graded run cannot be started at all. This copy of the kit predates it.
3. **As-of checkpoints.** The whole checkpoint payload is passed through, so
   `as_of_event_id` reaches the book.
4. **It does not die.** The SSE frame parse was unguarded and only
   `httpx.HTTPError` was caught around the consume loop, so a single malformed
   frame ended the run. The most expensive mistake available here is stopping.

## Which spec

`PROTOCOL.md` in this repository is the snapshot that shipped with the starter
kit, and it is materially out of date against the served specification, which
the document itself says wins. It describes 11 accounts rather than 20, hands
`commission` to you in the fill payload rather than making you derive six
amounts from a tariff, has no broker routing, no settlement events, no as-of
checkpoints and no systematic defect, and gives the scoring weights as 45/25
rather than 30/40. `book.py`'s original stub docstrings described those older
rules too.

Everything in this implementation follows the live specification.

## Where this stands

| Run | Score | Postings /30 | Checkpoints /40 | Resilience /15 | Liveness /10 | Recon /5 |
| --- | --- | --- | --- | --- | --- | --- |
| starter kit | 18.45 | 6.29 | 1.42 | 3.07 | 7.50 | 0.18 |
| `run_deb24bbe5b3c` | 77.21 | 28.92 | 24.18 | 11.08 | 10.00 | 3.02 |
| `run_463ab2612b8c` | **99.41** | 29.85 | 39.78 | 14.81 | 10.00 | 4.97 |

Run 2 settled the three tariff readings in section 4, confirmed
`OMIT_ZERO_LEGS`, and identified the duplicate-fill defect. Run 3 measured
those fixes and left 7 wrong events out of 782, all of them the reversed-trade
settlement described in section 8, which also accounted for the whole
trial-balance gap; plus one customer's `cash_hold`, which produced the hold
formula above.

Both of those are now fixed. Replaying run 3's recorded stream, all 7 events the
server graded wrong are now suppressed and the 960 offline invariant checks
pass. **Not yet measured live** - that is the next practice run.

### Not done yet

- The two fixes above need a live run to confirm.
- `fx_conversion` is a false-positive detector and should probably be deleted:
  it fires on every `fx_deposit` while the server marks them all correct.
