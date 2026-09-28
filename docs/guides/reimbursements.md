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
the pair drops out of spending and income math entirely.

A full pair allows only rounding — a dollar either way:

- **A larger deposit** still pairs in full when it exceeds what it
  repays by no more than $1; those cents drop out of income with the
  rest of the deposit. Beyond that — a paycheck carrying an expense
  repayment, one insurance check covering several visits, interest on a
  late repayment — the link is made **partial** instead, and the page
  says so; nothing is refused, the rest of the deposit simply stays
  income: a $1,000 charge and a $1,040 deposit leave $40 of income.
- **A smaller deposit** pairs in full only within a dollar of the
  charge; the odd cents are treated as repaid. Anything shorter — a
  $150 check against a $500 bill — is linked as partial, and the $350
  nobody paid back stays spend.

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
amount — at most the charge itself) and it joins the awaiting list on
the Reimburse page. When you link deposits, the flag clears once what
came back covers what you expected (or the whole charge if you didn't
set an expectation).

## How to link

On the Reimburse page, open an awaiting charge with **match…** to see
candidate deposits — opposite-direction transactions, narrowed by the
**filter by merchant…** box. A deposit other charges have already
used up is not offered, and one they have partly used is matched on
what is left of it. Pick one or several and link; one
insurance check often covers many charges in one shot. **undo match**
on a linked pair restores each side's original category — the one it had
before the link — unless you have set a new category on that row since,
which is kept. The **Matched** list shows the newest pairs first; **show
more** pages back, and **find a pair by merchant…** searches either side,
so an older pair can still be undone.

To start from the charge itself: on the web, **↔ mark reimbursed…** in
a row's category menu flags the charge (if it isn't already) and opens
the Reimburse page with that charge's candidates showing. In the app,
flag the charge from its transaction screen, then **mark reimbursed —
pick the deposit** takes you to the Reimburse screen, where the charge
waits on the awaiting list.

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
