# Ledger Arena: protocol specification

> ## SUPERSEDED - do not implement from this file
>
> This snapshot is materially out of date against the served specification at
> **https://hiring-arena.twocc.in/protocol**, which the note below says wins.
> It is kept only because it shipped with the kit.
>
> What it gets wrong: **11 accounts instead of 20** (no per-broker payables
> 2411/2412/2413, no 2420, 2430, 4010, 5000, 5010, 5100); **`commission`
> handed to you in the fill payload** instead of six amounts derived from a
> broker tariff; **no broker routing**; **no `open_order_routes` or as-of
> checkpoints**; **no settlement events**; **no systematic defect**; and
> scoring given as postings 45 / checkpoints 25 rather than **30 / 40**.
>
> This implementation follows the live specification. See `NOTES.md`.

> This copy is a snapshot taken when the kit was published. The canonical
> version is served at **https://hiring-arena.twocc.in/protocol**, and if the
> two ever disagree, that one wins. Any clarification made in Discord is folded
> into it.

You are building a server that keeps a double-entry book of record. We stream you
a broker's event feed; you post the events into your book and send us back the
journal legs each one produced. We score correctness continuously.

**Your server competes, not you.** The stream is resumable, so a crash at 3am
costs you very little. There is nothing to stay awake for.

---

## 1. What you need

Outbound HTTPS only. You do **not** need a public address, a domain, a
certificate or a cloud account. Your server connects to ours.

Any language. Everything below is plain HTTP and JSON.

---

## 2. Accounts

| Account | Name | Type |
| --- | --- | --- |
| `1100` | Omnibus Cash at Broker | Asset |
| `1150` | Settlement Receivable | Asset |
| `1200` | Omnibus Custody | Asset |
| `2010` | Customer Wallet | Liability |
| `2100` | Customer Securities Claim | Liability |
| `2300` | Withdrawals In Transit | Liability |
| `2350` | Unsettled Trade Payable | Liability |
| `2400` | Regulatory Fees Payable | Liability |
| `4000` | Commission Revenue | Income |
| `4100` | FX Spread Revenue | Income |
| `4200` | Interest Income | Income |

**The firm has money of its own.** Commission and FX spread are the firm's
income, not any customer's. Regulatory fees are owed onward to the venue. So
the sum of customer wallets does **not** equal omnibus cash, and any check you
write assuming it does will be wrong on a correct book.

Every leg carries a `customer_id`. Assets increase on the debit side,
liabilities on the credit side. Debits must equal credits on every transaction.

Money has 2 decimal places. Share quantities have up to 6. **Do not use binary
floating point for money.**

---

## 3. Endpoints

Base URL and your API key are issued to you. Authenticate every request with
`Authorization: Bearer <key>`.

### `GET /v1/stream?mode=<mode>&from=<offset>`

Server-Sent Events. Each message is one event. Reconnect with the next offset
you have not processed. **Always send `from`**; the server may rewind you.

```
event: order_filled
id: 4213
data: {"offset":4213,"event_id":"evt_...","type":"order_filled","payload":{...}}
```

Three control events are not ledger events:

- `stream_open` — sent the moment you connect, before any ledger event.
  Carries your run id, how far in you are, and how long until the next
  event. Your stream is staggered against everyone else's, so the first
  event may be up to a minute and a half away; this frame is how you know
  the connection is healthy while you wait.


- `stream_reset` — reconnect, resuming from the offset in the payload. The
  server may deliberately send you events you have already seen.
- `stream_end` — the run is over.

### `POST /v1/postings?mode=<mode>`

Send one posting or a batch. Batching is strongly recommended at high rates.

```json
{"postings": [
  {"event_id": "evt_...", "legs": [
    {"account": "1100", "customer_id": "CUST-1001", "debit": "1000.00", "credit": "0.00"},
    {"account": "2010", "customer_id": "CUST-1001", "debit": "0.00", "credit": "1000.00"}
  ]}
]}
```

Maximum 500 postings per request. **The first submission for an event wins**;
later ones for the same event are ignored.

Some events correctly produce **no legs**. Send `"legs": []`. Not submitting at
all scores zero for that event.

### `POST /v1/checkpoint?mode=<mode>`

When you receive a `checkpoint_request`, reply within the stated grace period
with your full state.

```json
{
  "checkpoint_id": "cp3",
  "trial_balance": {"1100": "5913.85", "1200": "2479.00",
                    "2010": "-5913.85", "2100": "-2479.00"},
  "customers": {
    "CUST-1001": {
      "wallet_cash": "3777.53",
      "cash_hold": "320.00",
      "positions": {"ACME": {"quantity": "8", "cost_basis": "960.00"}}
    }
  }
}
```

Trial balance values are **debit-positive**: an asset balance is positive, a
liability balance is negative.

### `GET /v1/me?mode=<mode>` and `GET /v1/leaderboard?mode=<mode>`

Your stats and the standings. In practice mode `/v1/me` includes a full
breakdown; in the qualifier and the main event it does not.

### `GET /v1/rules`

The scoring weights, live. They are not a secret.

---

## 4. Posting rules

### Cash

**`deposit`** — `amount`
```
Dr 1100 amount        Cr 2010 amount
```

**`fee_charged`** — `amount`
```
Dr 2010 amount        Cr 1100 amount
```

**`withdrawal_requested`** — `withdrawal_id`, `amount`. The money has left the
customer's wallet but not yet the broker.
```
Dr 2010 amount        Cr 2300 amount
```

**`withdrawal_settled`** — `withdrawal_id`. Look up the amount from the request.
```
Dr 2300 amount        Cr 1100 amount
```

**`withdrawal_rejected`** — `withdrawal_id`. Returns it to the wallet.
```
Dr 2300 amount        Cr 2010 amount
```

**`fee_refund`** — `refunds_source_id`, `customer_id`. Refunds a fee charged
earlier. **The amount is not in this payload**: look it up from the
`fee_charged` event being refunded. Refunding the same fee twice is an error.
```
Dr 1100 amount        Cr 2010 amount
```

**`interest_credited`** — `gross_amount`, `customer_share`. Interest earned on
the omnibus balance and shared with the customer. The firm keeps the remainder,
so this is not a pass-through.
```
Dr 1100 gross         Cr 2010 customer_share
                      Cr 4200 gross - customer_share
```

**`transfer_between_customers`** — `from_customer_id`, `to_customer_id`,
`amount`. No external cash moves. Both legs land on `2010`, so **the account
nets to zero**: a book that tracks balances per account rather than per
(customer, account) will show nothing wrong at all.
```
Dr 2010 amount  (from_customer_id)
                      Cr 2010 amount  (to_customer_id)
```

### Orders

**`order_placed`** — `order_id`, `customer_id`, `side`, `symbol`, `quantity`,
`limit_price`, `est_commission`.

**No legs.** A placement moves no money. It creates a *hold*: for a buy, cash of
`quantity × limit_price + est_commission` is no longer spendable; for a sell, the
shares are no longer sellable. Holds are reported at checkpoints, never posted.

**`order_partially_filled`** and **`order_filled`** — `order_id`, `customer_id`,
`side`, `symbol`, `quantity`, `price`, `principal`, `commission`. An order may
fill many times. `order_filled` is the last fill and closes the order, releasing
any unfilled remainder of the hold.

Commission is the **firm's income**. It is not part of the cost basis, and it
does not leave the broker account: it is ours.

**Cash does not move on the trade date.** Trades settle two days later, so a
fill creates an obligation and a separate `trade_settled` event discharges it.
A book that touches `1100` here will disagree with the broker for exactly as
long as anything remains unsettled.

Buy, carrying `trade_id`:
```
Dr 2010 principal + commission     Cr 2350 principal
Dr 1200 principal                  Cr 2100 principal
                                   Cr 4000 commission
```

Sell, where `cost` is the **first-in-first-out cost of the shares sold** and
`reg` is a regulatory fee of `principal x 0.0008` rounded to the cent, levied on
the seller and owed onward to the venue:
```
Dr 1150 principal                  Cr 2010 principal - commission - reg
Dr 2100 cost                       Cr 1200 cost
                                   Cr 4000 commission
                                   Cr 2400 reg
```

**`trade_settled`** with `trade_id` discharges the obligation from that fill:
```
buy    Dr 2350 principal     Cr 1100 principal
sell   Dr 1100 principal     Cr 1150 principal
```

Realised profit and loss is `(principal - commission - reg) - cost`. Never post
it directly; it is the residual.

**`order_cancelled`**, **`order_rejected`** — `order_id`. **No legs.** Release
the remaining hold.

### Corporate actions

**`dividend_cash`** — `gross_amount`, `withholding_tax`, `net_amount`. Tax is
withheld at source, so only the net ever reaches us and we owe the tax to nobody.
**Do not raise a tax payable.**
```
Dr 1100 net           Cr 2010 net
```

**`dividend_reinvested`** — as above plus `reinvest_price`, `reinvest_quantity`.
The broker reinvests the net. **Cash is not involved at all.**
```
Dr 1200 net           Cr 2100 net       and add a lot of reinvest_quantity at cost net
```

**`stock_split`** — `ratio_from`, `ratio_to`. **No legs.** Quantity scales by
`ratio_to / ratio_from`; total cost is unchanged, so cost per share moves.

**`symbol_change`** — `old_symbol`, `new_symbol`. **No legs.** Re-key the holding.

### Foreign currency

**`fx_deposit`** — `amount_foreign`, `currency`, `market_rate`, `customer_rate`,
`usd_at_market_rate`, `usd_at_customer_rate`.

Money arrives in another currency and is converted. The customer is credited at
their rate; the gap between that and the market rate is the firm's spread.
Crediting the wallet with the market figure overstates what is owed to the
customer by exactly that spread.
```
Dr 1100 usd_at_market_rate     Cr 2010 usd_at_customer_rate
                               Cr 4100 the difference
```

### Corrections

**`reversal`** — `reverses_event_id`. Post the exact inverse of the original's
legs. Keep both: the audit trail retains the original and its reversal.

A reversal must also undo the original's effect on your **lot book**, not just on
the accounts. A reversed buy whose lot you leave in place will balance perfectly
and quietly corrupt every subsequent cost basis.

---

## 5. Things the stream will do to you

All of these are deliberate. None are bugs.

| | What happens | What is expected |
| --- | --- | --- |
| **Duplicates** | The same `event_id` arrives more than once | Post it once. Re-delivery must not change any balance |
| **Replay** | At an unannounced point we drop your connection and rewind you several hundred events | An idempotent consumer notices nothing |
| **Out of order** | A fill may arrive before its placement | Handle it or record it; do not stall the stream |
| **Backdated** | An event carries `backdated_days` | It still posts. Only ordering assumptions break |
| **Unknown reference** | A reversal of an event you never received | Record it as rejected, carry on |
| **Oversell** | A sale larger than the position | Reject it. **Do not** leave lots half-consumed |
| **Malformed** | A payload that will not parse | Reject it, carry on |

An event you correctly reject produces **no legs**. Submit `"legs": []`.

Three rules that would otherwise be guesswork, stated so they are not:

- **FIFO means delivery order, not trade date.** The stream is deliberately not
  date-ordered. Lots are consumed in the order the buys reached you.
- **A conflicting duplicate is not a rejection.** The same `event_id` arriving
  with *different* content is reported separately: first delivery wins.
  Rejections are events you refused to post on their own merits.
- **A redelivered event you already rejected stays one rejection**, not two. An
  id you have seen is an id you have seen, whatever you did with it.
- **A checkpoint reports your state as at the checkpoint's offset**, not as at
  the moment you reply. The grace period is for the network, not for processing
  further events first.
- **Report every account you have ever posted to**, including any that have
  netted back to zero.
- **An `fx_deposit` whose customer rate is better than the market rate is
  rejected.** A negative spread is bad data, not a gift.
- **A fill releases a proportional share of the hold that order placed**, and
  the final fill or a cancellation releases whatever remains, so a closed order
  always returns its hold to exactly zero.
- **Reversing a fill does not restore the hold.** A released hold stays
  released; a reversal undoes the postings and the lot book, not the lifecycle.

The single most expensive mistake is stopping. A server that rejects one event
and keeps consuming beats one that crashes and misses a thousand.

---

## 6. Scoring

| Component | Weight |
| --- | --- |
| Per-event posting correctness | 45 |
| Checkpoint state correctness | 25 |
| Resilience (idempotency, recovery, coverage) | 15 |
| Liveness (checkpoints on time, p95 latency) | 10 |
| Final reconciliation | 5 |

Correctness is 70 of 100. Latency is scored generously: anything under 5 seconds
at p95 is full marks, and it degrades linearly to zero at 2 minutes. This is not
a speed contest.

---

## 7. Modes

| Mode | Length | Events | Feedback |
| --- | --- | --- | --- |
| `practice` | ~7 min | 800 | **Full.** Every response tells you the correct legs and diffs them against yours. Unlimited reruns |
| `submission` | ~60 min | 4,000 | **Score shown**, no per-event diffs. 3 attempts, each a fresh dataset |
| `final` | ~3 h | 12,000 | Score withheld until submissions close. 1 attempt |

Practice is the executable specification. If anything here is ambiguous, run
against practice and believe what it tells you.

Your stream is generated from a seed unique to you. Two candidates never receive
the same events, so comparing answers with someone else will actively mislead you.

---

## 8. Suggested order of work

1. Consume the stream and print event types. Do not post anything yet.
2. Implement deposits and fees. Watch the trial balance stay at zero.
3. Implement the order lifecycle. Remember that placements and cancellations
   produce no legs.
4. Implement FIFO cost relief on sells. This is the largest single source of lost
   marks.
5. Implement reversals, including the lot-book effect.
6. Make ingestion idempotent, then reconnect mid-run and confirm nothing changes.
7. Implement checkpoints.
8. Only then worry about batching and latency.
