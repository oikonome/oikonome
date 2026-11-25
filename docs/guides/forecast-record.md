# The forecast's track record

The cash forecast on Today is a prediction: what your checking balance
will be over the next 60 days if you keep spending and paying as you have
been. The app grades that prediction against what really happened, on
your household, and tells you how good it has been.

## What is kept

Every night, after it looks for new bills, the app keeps what the forecast
said: your checking balance that night and the balance it predicted 7, 14,
30, 45 and 60 days ahead for each card scenario ("Your usual", "Pay all
cards today", "Pay on the due date") and for a heavier month. Nothing is
filled in for a night the app did not run, and nothing is replayed from
your past: the record starts the night the feature first runs.

## How it is graded

When a day the forecast pointed at arrives, the balance the app kept that
night is the real balance. A prediction is graded only when your checking
balance was updated by your bank (or by you) in the day and a half before
both nights, and the checking account is the same one. Reconnecting
your bank keeps the record: the nights already kept move to the
reconnected account with its history, so they are still graded. A night
kept by an older version before a reconnect is not graded. A missed night or
two is filled in from the next night and the transactions between; a
longer gap is not graded. Only "Your usual" is graded for accuracy — the
other two scenarios are what-ifs, compared instead with the card payments
you really made.

## What you see

- **The line under the chart**, once there are at least 30 results spread
  over at least 45 days: "Over the last 3 months, the 14-day forecast
  landed within $… of your real balance 4 times in 5." Results that
  overlap are not separate results — thirty 30-day predictions over three
  months cover only about four separate months — so a "4 times in 5" is
  said only for a distance ahead with enough separate results behind it.
  For the 30-day forecast the line says its typical miss instead: "Over
  your last 13 weeks the 30-day forecast was typically within $…". When
  money you moved between your own accounts (to your savings, say)
  explains most of the misses, the figures leave it out and say so.
- **A lean**, only when the misses keep falling on one side: "Lately the
  30-day forecast has come out about $… above your real balance." The
  forecast line itself is not moved; the band leans instead.
- **The band** around each scenario: the range the forecast's own past
  misses suggest. It is built from all of the forecast's recent
  misses together, at every distance ahead, scaled by how fast your own
  misses grow with the distance, with the most recent three months counted
  twice. While there is not yet much history for the longer distances, the
  band is never narrower than the heavier-month line was. Until about five
  weeks of results exist, the dashed heavier-month line is drawn instead.
- **How has the forecast done?** (tap the line): the last 90 days of 30-day
  predictions against your real balance, how often the band held when
  each day was checked against the band as it stood when that forecast
  was made, how
  your card payments compared with the scenarios, and why any days were
  not graded. Tap **Why** beside a day for **Why the forecast missed**: the miss split
  into paychecks, bills, card payments, spending, money moved between
  accounts, refunds and other, and charges still pending — the parts add
  up to the miss — with the largest causes in words.

The record travels with your export and comes back on a restore (it is
then graded only against itself). Nothing about it is sent anywhere. The
night is kept once a day, early in your morning in your own time zone; a
night the app did not run is simply not graded.
