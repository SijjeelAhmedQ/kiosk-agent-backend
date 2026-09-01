# Money in this system

Read this when a figure looks a hundred times too large or too small.

## The unit

Every amount anywhere in a Friends Kitchen errand is **Pakistani rupees, as a
whole rupee amount**. Menu prices, subtotals, tax, order totals, coupon values,
the amount due, the cash limit, what a rider is paid — all of it, one unit.

There are no paisa in this system. There are no cents. `1093` is one thousand
and ninety-three rupees.

This is the mistake the whole rule exists to stop: a model shown a bare `1093`
reads it as a minor unit and reports "$10.93" to a customer who is about to be
charged a hundred times that. So every figure that reaches an agent is written
`Rs 1,093` before it gets there, and the ones this script prints are written
that way too.

**Never** divide an amount by 100, convert it to another currency, or restate
it in cents, paisa or dollars. Never write `$`, `USD`, `PKR` or a bare number
where an amount belongs. The only form is `Rs` followed by the figure.

## What the script accepts

Either form, for convenience when you are passing back something you were just
handed:

```
Rs 1,240      Rs1240      1240      1,240      Rs 1,240.50
```

Commas, spaces and an `Rs` / `PKR` / `₨` prefix are stripped. Anything left
that is not a number is refused rather than guessed at — a silent zero in an
arithmetic script is worse than an error.

## What it prints back

Always `Rs 1,240`: grouped in thousands, and with `.00` dropped, because the
menu is priced in whole rupees and `Rs 3,000.00` invites exactly the misread
above. A genuine half-rupee keeps its decimals — `Rs 2,500.75`.

Use those strings exactly as given. They are already in the form the customer
should see, and rewriting one is how a correct figure becomes a wrong one.

## Rounding

The script does not round. It adds, subtracts and takes minimums, and prints
the result to two decimal places. Percentages — the one place it does round —
go to the nearest whole percent, because "62.8% of the cash limit" is a figure
nobody asked for.

The rule this follows is the same one in `agent/wallet.py:rupees()`. The two
are deliberately separate copies: this script has to run under whatever Python
is to hand, and importing the application package to format a number would make
it need the whole repository importable just to print a total. If one changes,
change the other.
