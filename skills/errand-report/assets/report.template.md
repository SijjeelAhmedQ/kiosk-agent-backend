# The closing summary, sentence by sentence

Each line in the block below is one sentence of the report, with the fields it
needs in braces. `render_report.py` fills the ones it was given and drops the
rest, so the wording is fixed while the shape follows what actually happened.

    ordered           Ordered {ordered}.
    orderedOn         Ordered {ordered} on order {orderNumber}.
    coupon            The coupon covered {couponCovered}.
    charged           {cashSpent} was charged.
    chargedRemaining  {cashSpent} was charged, leaving {cashRemaining} of the {cashLimit} cash limit.
    delivery          It is going to {deliveredTo}.
    exception         {exception}
    failed            The errand ended without a paid order.
    clean             Nothing went wrong.

Which sentence is picked, which is also part of the wording:

* `orderedOn` when there is an order number, `ordered` when there is not. Two
  short sentences about the same fact read like a list.
* `chargedRemaining` when both the remaining cash and the limit are known,
  `charged` when they are not. One of those two figures on its own is a number
  without a scale.
* `coupon` is skipped when the coupon covered nothing. "The coupon covered
  Rs 0" is worse than silence.
* `exception` is repeated once per thing that did not go to plan, in the order
  they were given, each already a sentence of its own.
* `clean` appears only when there are no exceptions and the errand did not
  fail. It is what makes a clean run legible as clean rather than as a report
  that trailed off.

Change nothing here except deliberately. This file is the wording; the script
is only the machine that fills it.
