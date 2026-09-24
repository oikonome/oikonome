# Reimbursements

When someone pays you back — an insurance check, a split dinner, an
expense report — the charge shouldn't count as your spending. The
Reimburse page pairs the charge with the deposit so the math nets out.

Pairing is always **explicit**: you pick the exact charge and the exact
deposit. There are no pattern rules that guess — a wrong guess here
corrupts spending history silently, so the product doesn't guess.

## Full reimbursement

The whole amount came back, and the deposit is that repayment and
nothing more. Both sides become transfers (your own money moving), and
the pair drops out of spending and income math entirely. A deposit that
is clearly larger than what it repays — a paycheck carrying an expense
repayment, one insurance check covering several visits — is linked as
**partial** instead, and the page says so; nothing is refused, the rest
of the deposit simply stays income. The other way round, a deposit
smaller than the charge — a $150 check against a $500 bill — is linked
as partial too, and the $350 nobody paid back stays spend. A deposit
within a dollar of the charge still pairs in full; the odd cents are
treated as repaid.

## Partial reimbursement

Only part came back — a co-pay plus an insurance check, or a dinner
where friends covered half — or the deposit carries more than the
repayment. The charge **keeps its category**; spending math nets the
received amount off the row and the remainder stays real spend. So a
$500 vet bill with $300 reimbursed counts as $200 of spending, still
under its category. The deposit **keeps counting as income for its
unclaimed part**: a $3,000 paycheck that repays a $200 hotel bill is
$2,800 of income. Only when partial links use the whole deposit up does
it become a transfer; undoing one of them makes it income again.

Several deposits can pair to one charge, and one deposit can split
across several charges — each link takes the smaller of what the charge
still needs and what the deposit still has (full links included), so
the amounts can never sum past either side.

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

The deposits offered come from **every checking and savings account**
in the household (plus the primary checking account pinned on the
Accounts page, whatever its type), and from **the charge's own
account**, because a merchant refund is credited back to the card that
was charged and never lands in checking. Payments *to* that card are
credits too but are not refunds, so they stay out of the list; so do
credits on some other card. An exact amount heads the list; after that
the nearest in time comes before the nearest in amount, so one deposit
that repays several charges at once — one insurance check covering a
visit, a prescription and a lab bill — sits near the top of each of
those charges' lists.

## Gotchas

- Pick one charge and one deposit — two rows flowing the same direction
  can't pair.
- A partial pair can't receive more than the charge's amount; the
  remainder of a large deposit stays available for other charges.
- A view-only login can see the awaiting list; linking and flagging
  take the owner or a member.
- Deposits on a hidden account, on the non-primary copy of a linked
  account, or tagged to a different business entity than the charge are
  not offered and cannot be linked; posted deposits are listed before
  pending ones.
