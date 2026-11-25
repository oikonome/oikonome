# Settings status lines: one server answer each (spec)

The Settings list, the header chip and the phone's Settings rows each make a
claim about the household's setup ("needs attention", "daily 9am", "setup
unfinished", "340 model"). Each claim below is decided by ONE server
function, and every client prints what it returns. No client re-derives one
from raw fields, and none carries a fallback chain for a server that did not
send it.

## A connection that needs a person's hand

**Decided by** `web/pages._connections` (served on `GET /api/connections`).

- Each connection carries `needs_attention` (bool) and `replaced` (bool), and
  the response carries `attention` (int, how many need attention).
- `needs_attention` is true when `status_kind` is `reauth`, `gone` or
  `attention`, and the connection is not `replaced`. A bank that is merely
  down (`retrying`) never counts.
- `replaced` is true for a `gone` connection when a live (not gone)
  connection to the same institution exists. Same institution: same
  `institution_id` when both carry one, otherwise the same name ignoring
  case and spacing. A removed link that was linked again is history, not
  work: it no longer counts, and it is listed once as `removed_text`
  ("removed by the bank feed on 06/03/26 — replaced") with a **forget**
  button.
- **Forget** is the existing owner-only disconnect door
  (`POST /api/connections/{id}/disconnect`). On a removed connection it
  clears the archive reason, and the list stops serving the row. History is
  kept.
- Readers: the header chip (web), the Settings list's Connections row (web
  and phone), the Accounts page badge, and the hosted connection rows (web
  and phone).
- Older client: still counts any non-ok status, as before. Stale tab: shows
  the count it last read; the chip polls the same endpoint every minute.
  Restore: the flags are derived from the restored `items` rows, never
  stored.

## Who receives a scheduled email

**Decided by** `notify/reach.cadence_reach` (served as `email_reach` on
`GET /api/settings` and on every settings save reply, owner only, and read
by System health).

- Per cadence (`daily`, `monthly`, `yearly`): `on` (any channel scheduled),
  `email` (addresses the worker would mail: `worker._recipients`, so mutes,
  per-kind choices, invitations and held addresses all apply), `push`
  (browsers and phones the fan-out would reach, only when the cadence's push
  channel is on), `sms` (a verified, consented number on a cadence with SMS
  on), `receiving` (any of those), and `text`: the list line, for example
  `daily 9am`, `daily 9am · +1 recipient`, `daily 9am · nobody receiving it`,
  or `daily email off`.
- A cadence that is on with nobody receiving it is shown in the warning tone
  on web Settings, the phone's Settings row and the cadence editor, and
  System health's daily email row warns with the same words.
- The setup wizard reads the same row (web and phone): the Done step's
  "Email & push" line is `daily.text`, warning-marked when on with nobody
  receiving it; the Done blurb promises "the first email arrives tomorrow"
  only when `receiving` is true, naming the channel that reaches someone
  (`email` > 0 → email, else `push` > 0 → notification, else a text); the
  Email & push step shows the warning line, and "Push goes to the browsers
  and phones you have enabled" only when `push` counts one. Without
  `email_reach` (an older server) nothing is promised.
- Older client: keeps composing `daily 9am` from the schedule. Stale tab:
  the line refreshes with the settings read (a save reply carries it). A mute
  or a device registered elsewhere shows on the next settings read. Restore:
  derived from the restored settings and the instance's devices.

## Setup complete

**Decided by** `/api/onboarding` as `setup_complete`.

- True when the household dismissed the wizard (`wizard_done`) OR every step
  the wizard sets up already exists: accounts, transactions, bills, budgets,
  and a primary checking (or no checking to pick). It is the Today
  checklist's own test, made once on the server.
- Readers: the Wizards row of the Settings list and the "Finish setup"
  button (web and phone).
- Older client: reads `wizard_done` as before.

## Where a categorization rule came from

**Decided by** `GET /api/rules` as `counts_text`.

- `llm` rules are "from an AI pass", with "(none set up now)" when no AI
  endpoint answers the categorize role; `model` rules are "from your
  household's trained model". The word "model" alone is not used for either.
- Readers: the Categorization and Rules heroes (web and phone), whose group
  titles use the same words. System health's merchant map row says "by an
  AI pass".

## Merge offers that may go together

**Decided by** `engine/merchant_merge.pending` as `bulk` and `caution`.

- `bulk` is true when one name is the other cut short (a terminal cut,
  mid-word or after three or more words) or the two are one name spelled
  differently. "Merge all" covers only `bulk` offers.
- Any other offer (two names that share a word, a kind of business, a city)
  carries `caution`: "share a word — check the address", and is merged one at
  a time.
- When one name is the other cut short, the merge keeps the longer, full
  name: the proposer never picks the cut name as the survivor, and an offer
  stored the other way round is turned around when it is listed and when it
  is approved.
