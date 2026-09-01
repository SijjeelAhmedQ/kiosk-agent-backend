# Money

## One currency, one form

Every figure anywhere in this system — menu prices, subtotals, tax, totals,
coupon values, the amount due, the cash limit — is **Pakistani rupees**, and the
tools hand them to the agent already written as `Rs 1,093`.

So, in a tool result, in a report, in a script's output, in a commit message:

* Never divide by 100. Nothing here is in paisa.
* Never convert. `Rs 1,093` is one thousand and ninety-three rupees — not
  1,093 paisa, and not $10.93.
* Never write `$`, `USD`, `PKR`, `₨`, or a bare number where an amount belongs.

`agent/wallet.rupees()` is the single formatter: `f"Rs {amount:,.2f}"` with a
trailing `.00` dropped, because the menu is priced in whole rupees and
`Rs 3,000.00` invites the same misread a bare number does.

The raw numbers stay in two places — `agent/tools/api_tools._order` and the
wallet — because that is where the arithmetic happens. They also cross the wire
raw in `wallet` on `GET /api/agent/runs/{id}`, so the console can render them
itself. **Anything that prints those numbers must format them.**

## What the wallet actually enforces

`agent/wallet.py`, read by `authorize_payment` in `agent/tools/api_tools.py`.

The limit is enforced by the payment tool, **not by the prompt**. A model that
talks itself into an expensive order still cannot buy it. What the tool does
when the charge is over the ceiling is refuse, with a sentence saying so.

The correct response to that refusal is to change the order — remove items, or
apply the coupon — and try again. The two wrong responses, both stated in
`agent/prompts.py` and both worth watching for in a trace:

* retrying the same payment unchanged (it will refuse identically), and
* placing a second order to get round the first refusal.

## The three wallets an errand can carry

Set by `--coupon` and `--limit`, and described to the agent in
`agent/prompts.py`:

| Wallet | Brief |
| --- | --- |
| Coupon **and** cash | Use the coupon — that is the point of the errand — and treat the cash as the fallback. |
| Cash only | One ceiling for the whole order. |
| Coupon only, no cash | The coupon must cover everything. If it does not, do not place an order that cannot be paid for: stop and report the shortfall. |

Neither is refused at the edge, with 422, before an agent is built.

## Reading a wallet summary

```json
{"couponCode": "FK-8H2K-9QW1", "couponRedeemed": 850, "cashLimit": 2500,
 "cashSpent": 243, "cashRemaining": 2257}
```

Read aloud: *the coupon covered Rs 850, Rs 243 was charged to cash, and
Rs 2,257 of the limit is unused.* `cashSpent` is what actually moved, not what
was quoted — an order that was placed but never paid for leaves it at 0, and
that gap is the thing to notice.
