# Reimbursements

When someone pays you back — an insurance check, a split dinner, an
expense report — the charge shouldn't count as your spending. The
Reimburse page pairs the charge with the deposit so the math nets out.

Pairing is always **explicit**: you pick the exact charge and the exact
deposit. There are no pattern rules that guess — a wrong guess here
corrupts spending history silently, so the product doesn't guess.

## Full reimbursement

The whole amount came back. Both sides become transfers (your own money
moving), and the pair drops out of spending and income math entirely.

## Partial reimbursement

Only part came back — a co-pay plus an insurance check, or a dinner
where friends covered half. The deposit becomes a transfer, but the
charge **keeps its category**; spending math nets the received amount
off the row and the remainder stays real spend. So a $500 vet bill with
$300 reimbursed counts as $200 of spending, still under its category.

Several deposits can pair to one charge, and one deposit can split
across several charges — each link takes the smaller of what the charge
still needs and what the deposit still has, so the amounts can never
sum past either side.

## The awaiting list

Flag a charge as "reimbursement expected" (optionally with the expected
amount) and it joins the awaiting list on the Reimburse page. When you
link deposits, the flag clears once the received total covers what you
expected (or the whole charge if you didn't set an expectation).

## How to link

From the Reimburse page (or a transaction's detail), open a charge with
**match…** to see candidate deposits — opposite-direction transactions,
narrowed by the **filter by merchant…** box. Pick one or several and
link; one insurance check often covers many charges in one shot. **undo
match** on a linked pair restores each side's original category.

The deposits offered come from two places: your **primary checking
account** (set it on the Accounts page — otherwise any checking account
counts), and **the charge's own account**, because a merchant refund is
credited back to the card that was charged and never lands in checking.
Payments *to* that card are credits too but are not refunds, so they
stay out of the list; so do credits on some other card.

## Gotchas

- Pick one charge and one deposit — two rows flowing the same direction
  can't pair.
- A partial pair can't receive more than the charge's amount; the
  remainder of a large deposit stays available for other charges.
- A view-only login can see the awaiting list; linking and flagging
  take the owner or a member.
