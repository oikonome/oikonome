# Alerts

Alerts answer one question: **what needs your attention today?** The same
list renders in several places — the strip at the top of the Today page,
the top of the daily email, the **bell** in the web header (its badge
carries the count, coloured by the worst severity, and opens the Alerts
page), the phone app's Alerts screen, and the Alerts page itself, which
adds the full history and the dismiss controls.

## What can fire

Each alert has a severity — red (bad), amber (warn), neutral (info) — and
the strip always sorts worst first.

- **Anomalies** (warn) — the "is something wrong?" watch. Two signals:
  a **possible double charge** (the same bank descriptor and the same
  amount, twice within 2 days, $10 minimum — pending rows are ignored,
  and two separate orders at a common price point don't count) and a
  **first-ever merchant** charging $200 or more — a merchant absent from
  your entire ledger history. Both scan the budget's own spend rows, so
  transfers and card payments can't false-positive, and accounts you've
  excluded from the budget are skipped.
- **Stale sync** (warn) — bank sync hasn't succeeded in the last
  6 hours, or has never completed. The never-completed alert points
  at the Accounts page; the stale alert reports the date of the last
  successful sync and warns the data may be stale. File-only installs
  with no linked connections are exempt.
- **Connection removed** (warn) — a **Plaid** connection that stayed broken
  for 30 straight days gets released automatically, and this alert asks
  you to reconnect it on the Accounts page (other connection types are
  never auto-removed). Only failures **you** have to fix (expired
  login, revoked consent) count toward the 30 days;
  institution outages and transient errors never cost you a re-link.
  The same alert fires when the bank connection **no longer exists at
  Plaid** (the bank or the aggregator dropped it), and when an operator's
  add-on releases an account's connections.
  The alert stays until a live connection to the same institution
  exists again. Self-hosted installs can change or disable the window
  with `OIKONOME_PLAID_REAP_DAYS`.
- **Source failover** (warn) — a linked account's serving source went
  down and its backup took over (see the Accounts guide). The alert
  lands on the Alerts page and, once each, in an email and a push
  notification; data keeps flowing from the backup while you fix the
  primary.
- **Investment funding** (red) — money moved from an investment
  institution into checking this month, i.e. spending covered by
  selling investments. Shows the month total and year-to-date. A single
  transfer of $15,000 or more is reported separately (info) as a
  one-time asset purchase, never in the shortfall figure. A payroll
  deposit far above your normal paycheck is flagged as a **bonus**
  (info) and excluded from all budget math.
- **Awaiting reimbursement** (info; warn once the oldest charge is
  45+ days old) — count and the amount still owed, net of partial
  receipts already recorded. Points at the Reimburse page (see the
  Reimbursements guide).
- **Smart categorization failed** (warn) — the nightly categorization run
  reached the model backend and got nothing back: the model was removed
  from the server, the endpoint is unreachable, or every call timed out.
  It names the date and the error, counts the merchants and store orders
  waiting on it, and points at the Doctor page (whose **Smart
  categorization → last run** row carries the same verdict). Nothing else
  would tell you — the work simply stops — so this alert exists to make a
  silent backend loud. It clears on the next run that produces answers.
- **Bills housekeeping** (info) — a nudge to run the recurring-bill
  finder when transactions exist but no bills are tracked, a count of
  proposals awaiting review, and a note when a bill's amount drifted
  and was auto-updated (see the Bills guide).

Alerts are **now-facts**: viewing a past day on the Today page shows
none, because they describe the present, not that date.

## Edge-triggered, not repeating

Alerts are built to say a thing once, not every morning:

- Anomalies only report when the newest transaction in the pattern is
  from yesterday or today — the same pair doesn't nag as the scan
  window slides.
- Amount-drift notes appear only the day the change happened.
- The log tracks each alert by kind + message with a **first seen** and
  **last seen** date. When the condition clears, the alert goes
  inactive — and any dismissal on it resets, so a later recurrence
  shows again. **Recurrence is news.**

Separately from the strip, a broken bank connection sends an
**immediate email** — fired only on the healthy → broken transition
(and again on recovery), so a multi-day outage emails exactly once, not
every sync. It links to the Accounts page to re-link. Break and
recovery notices — along with source-failover and stale-collector
alerts — also go to any push channels you've enabled.

A community-script collector whose **script token was revoked** after its
last push (a password change or reset revokes every token) gets its own
alert on the strip, in the daily email and on the Doctor page, naming the
source and pointing at Settings → Scripts to mint a replacement — a
collector in that state is not stale, it is locked out until you do.

## Dismissing

The ✕ on an alert hides it everywhere — Today, the email, and the
active list — **while its condition persists**. Once the condition
clears, the dismissal resets automatically. Following an alert's link
from the Today strip counts as acting on it: for the owner or a member
the alert is dismissed as you go (a view-only login just follows the
link). Dismissed alerts keep their
row on the Alerts page with a **dismissed** pill and a **restore**
button. Dismissing is household-wide, and the owner or a member
can do it; a view-only login sees alerts but can't dismiss them.

## History

The Alerts page lists the most recent 200 alerts with first-seen and
last-seen dates (MM/DD/YY), the kind, and an **active** or
**dismissed** pill; a condition that has since cleared shows a green
**resolved** pill in place of its kind. The chips above the table —
**all**, **connection**, **bills**, **cash**, **dismissed** — narrow the
log. Each active alert also carries a link to the page that deals with
it: **fix this ›** for something broken (a dead connection, a funding
gap), **review ›** for a queue waiting on your decision (bill proposals,
drift, anomalies, reimbursements).

## Gotchas

- Dismissal matches the alert's **exact message text**. When the facts
  change — a new charge joins the reimbursement total, the stale-sync
  date advances — that's a new message, and it shows up again on
  purpose.
- Accounts excluded from the budget can't fire anomaly alerts; a
  suspicious charge on an excluded card won't be flagged.
- The broken-connection email is not part of the daily strip and can't
  be dismissed — it fires once per break and once per recovery, driven
  by the connection's status change itself.
