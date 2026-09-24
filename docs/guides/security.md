# Account security

The Security section of Settings answers one question: **who can get
into this account, and how do you prove it's you when it matters?**

## Change password

Enter the current password and a new one (**at least 10 characters**).
When two-factor is on you'll also confirm a 6-digit code; a passkey-only
account confirms with the passkey or a recovery code instead.

Changing the password is treated as a full credential rotation:

- **Every other session is signed out** — the one you're using stays.
- **All passkeys are removed** (re-add them after) — with one exception:
  if you confirmed the change *with a passkey* and that passkey is your
  account's only second factor, that one key is kept so the change can't
  leave you with nothing to sign in with. Every other key still goes, and
  the response says how many were kept.
- **Every other signed-in phone or tablet is signed out** (sign in
  again) — the device you made the change from is spared, like the
  browser session you're using.
- **All script tokens you minted are revoked** (re-mint after).
- Pending family invites you created are cancelled.

That's deliberate: if you're changing the password because something was
stolen, nothing enrolled by the thief survives the change. The response
(and the Settings card) tells you how many passkeys, devices and tokens
the change removed, so a passkey that stops working is never a mystery.

Changing the account **email** is the same rotation: the email is your
login identifier, so every other session, passkey, signed-in device,
script token and pending family invite goes with it. Plan to re-add
passkeys and sign phones back in right after.

## The step-up rule

Every security-posture change — changing the password or email, turning
2FA on or off, adding or removing a passkey, regenerating recovery
codes, minting or revoking a script token, creating a family invite,
removing a member or changing what a member can do, signing out other
sessions (all of them or a single browser or phone), granting support
access to the operator, downloading the full export, database dump or
connections bundle, restoring a connections bundle, deleting the account
— demands proof beyond a signed-in browser. Someone who steals a signed-in tab holds the
cookie, not your credentials.

You prove it **once**, not per change: the first protected action opens
a single confirmation, and for the next ten minutes on that device every
other protected action just works. What the confirmation asks for is the
strongest factor you have enrolled:

- **Password only** — your password.
- **TOTP enrolled** — your password plus a current 6-digit code. If the
  authenticator is lost, the same sheet takes your password plus a
  **recovery code** instead — the link is under the code field, on the web
  and in the app.
- **Passkey** — the passkey itself: the browser (or Face ID / fingerprint
  in the app) prompts for an assertion. If the passkey isn't at hand, a
  recovery code works instead. The password alone does not: a passkey-only
  account is refused password proof here exactly as it is at sign-in.
  (An account with a passkey *and* TOTP proves itself with password plus
  code, as above.)

A few doors ask every time, even inside those ten minutes, because
what passes through them is too large to ride a minutes-old proof:
deleting the account; downloading the full export or database dump (the
download itself then runs from a one-minute, single-use link); and
downloading or restoring the connections bundle — the file that carries
every bank credential the household holds. Older versions of the app that still send a password
with each change keep working.

## Two-factor (TOTP)

**Enable 2FA** shows a QR code to scan into an authenticator app (with the
secret spelled out for manual entry); enter the code it produces to confirm. Your existing sign-in keeps working
until you confirm — enrollment can't half-enable and lock you out.
Confirming signs out every other session. To replace the authenticator,
turn 2FA off (password + current code) and enroll again — there is no
in-place re-enroll button.

**Turning 2FA off requires both** your password and a live code — it's a
downgrade, so it's held to the strictest proof — and also signs out
every other session.

## Recovery codes

Enrolling your **first** strong factor (confirming TOTP, or adding your
first passkey) issues a batch of **8 one-time recovery codes**, shown
**once** — save them; only hashes are stored. Each code works once:

- at sign-in, typed into the code field, when the authenticator is lost
  or the passkey is unavailable;
- as the step-up proof for destructive changes: the whole proof on a
  passkey-only account, and — alongside your password — the stand-in for a
  lost authenticator on a password + TOTP account, so a missing
  authenticator never blocks an export or a deletion.

The Security card shows how many remain unused, warns when you're
running low, and offers **regenerate**, which mints a fresh batch and
voids the old set. That line is there whichever strong factor you hold
— a passkey-only account sees the same count and the same control, since
the codes are its escape hatch when the key is lost; either way the
regenerate confirms through the step-up sheet.
Re-enrolling TOTP does not reissue codes — regenerate if you've lost them.

## Lost the password, the authenticator AND the recovery codes

The password reset — the mailed link, and the identical one
`./oikonome.sh reset-password` prints on a self-hosted instance — removes
every second factor, so on an account with one enrolled it also asks for
a recovery code — always, because otherwise anyone who could read your
email could take the account. With
all three gone there are two ways back in, and neither is instant:

- **A 7-day recovery you start yourself.** Request a reset link as usual
  — the sign-in screen's code step points at it ("Lost your recovery
  codes too?"), on the phone as well as the web. When the reset page asks
  for a recovery code you do not have, choose **Start the 7-day
  recovery**. Nothing changes yet: the account is told
  by email (and by push notification and text message where those are set
  up) that a reset which removes the second factor becomes possible on a
  named date, with a one-click **cancel** in the message — and **any
  sign-in cancels it too**. After the date, request a fresh reset link
  and it works without a code, removing the authenticator, passkeys and
  recovery codes exactly as a coded reset would; enrol a new
  authenticator once you are in. You have **30 days** from that date to
  use it — after that the recovery goes stale and a code is asked for
  again, so that a request started and forgotten cannot sit open on the
  account forever. Asking again restarts the wait, it never shortens
  it.
- **Ask the operator.** The instance operator can clear the second factor
  after checking who you are by other means (for example, a message from
  the account's own address). The clear removes
  the authenticator, every passkey and every recovery code, signs out
  every session and device, and emails the account that it happened; you
  then set a new password through the ordinary reset. Self-hosted:
  `./oikonome.sh clear-2fa <email>`.

If you receive a recovery notice you did not ask for, cancel it from the
message or simply sign in — either stops it — then change your password.

## Passkeys

Passkeys are WebAuthn credentials, so they need a **secure origin**: a
real domain served over https (`./oikonome.sh https <domain>`, or your own
reverse proxy), or `localhost` — a browser refuses them over plain
`http://`. The passkey routes come with **hosted mode**
(`OIKONOME_HOSTED`, an operator running the instance for other people),
which is the deployment that has that domain and certificate; a
single-household install reached over the LAN stays on password + optional
TOTP and the section is absent there. In the phone apps, a passkey
additionally needs the app build to list the server's domain.
**Add a passkey** confirms your password, prompts your device, and lets
you name the key; each entry shows when it was created and last used.
Removing one is a step-up action like any other.

An account whose only strong factor is a passkey **cannot sign in with
the password alone** — sign in with the passkey, or a recovery code.
Where the instance requires a second factor, you **can't remove your last
strong factor** — add another passkey or TOTP first.

The **Android app** signs in with passkeys too: against any https
server that offers them, the login screen shows **Sign in with a
passkey** and raises the phone's own credential sheet (keys synced by
Google Password Manager just work). Plain-http servers can't do
WebAuthn, so the button doesn't appear there.

## Sessions

The Sessions card lists everywhere you're signed in — device and
browser, sign-in date, and last activity (dates as MM/DD/YY), with
**this device** marked. A session dies after **30 days idle** (activity
slides the deadline) and never outlives **90 days** from sign-in.
**Sign out everywhere else** confirms your password — kicking sessions
is a step-up action too. To drop a single device instead, each row but
this one has its own **sign out** button; fill in the same password box
first, because revoking one session is the same step-up action as
revoking them all.

## Mobile devices

Signing in on a phone mints a device credential that stays in the
device's secure store — that is what keeps you signed in without a
browser session. It expires after **90 days without use** (opening the
app slides the deadline), and an account holds at most **10 signed-in
devices** at once.

Reaching that limit is not a dead end. Sign in as usual and the app
lists your signed-in devices with the last time each was used; pick one
to sign out and your sign-in continues. Nothing is deleted — that device
simply signs in again next time it is opened. You can also manage the
list any time from **Settings** on a device that is already signed in.

## Script tokens

Owner-only bearer credentials (prefix `oik_`) for programs, not people.
Minted once and **shown once**, and each one has exactly one of two
scopes:

- **push** (Settings → Connections → Script tokens): a host-side collector
  script — see the import history guide. It can push data through the
  import doors, never read or manage the account.
- **read** (Settings → Integrations → Integration tokens): a Prometheus
  scrape, a Home Assistant sensor, the local MCP server. It can read the
  integrations endpoints — a bounded picture of the books — and nothing
  else: no import, no settings, no credentials. See the integrations
  guide.

Mint and revoke are step-up actions (above), and a password or email
change revokes every token you minted. Creating a webhook — a URL the
instance posts your ledger's events to — steps up the same way.

## When sign-in asks for a security check

On hosted instances, repeated failed sign-ins arm a browser security
check (Cloudflare Turnstile). A normal sign-in never sees it — it is
armed by failures from your address, against your account, or by a burst
across the whole instance.

The check itself is JavaScript, so it cannot be completed at all with
JavaScript disabled, or when an ad blocker, privacy browser, company
network or DNS filter blocks the Cloudflare script. In that case the
sign-in page offers **"Email me a sign-in link"**:

1. Give your email address; a one-time link is sent, valid 30 minutes.
2. Open it. This does **not** sign you in — it grants your account one
   exemption from the security check, good for ten minutes and spent by
   your next sign-in attempt (one try, not ten minutes of guessing).
3. Sign in normally with your password and your second factor.

Because it is not a sign-in, someone reading your mail still cannot get
into your account without your password. The link works once, and you can
request at most three per hour. Self-hosted instances without email
configured print the link to the server log instead.

## Household members

**Invite** (Settings → Users on the web; **Security & household** in the
mobile app) gives someone their own login on this household — their own
password, their own second factor, the same accounts and history.
Invitations are one-time and expire in 7 days. You pick what the new login
can do when you create one, and can change it afterwards from the same
list.

How the invitation reaches them differs by where you're running:

- **An instance an operator runs for other people** — you enter the
  person's **email address** and the instance mails the link there. The
  link is not shown back to you, and **only that address can use it**:
  someone typing a different address on the claim page is told so, and the
  invitation stays unspent for the right person.
- **Self-hosted** — the invitation is a one-time link shown once, to copy
  and pass along however you like (the instance also emails it when SMTP
  is configured). One household, one address book, so a shareable link is
  the right shape there.

A member's login is theirs, not a shared one:

- **They enrol their own second factor.** On an instance that requires
  one, a member is prompted to set up an authenticator app or a passkey
  on first sign-in exactly as the owner is; membership is not a way
  around the requirement.
- **They control their own copy of the email.** A member's Email card
  turns *their* address on or off in the household's scheduled summaries.
  The schedule itself — the hour, the shape, who else is on it — stays
  the owner's.
- **They can leave.** **Delete my login** removes that person's sign-in
  and nothing else: the household's accounts, transactions and settings
  are untouched, and it re-authenticates like any other destructive door.
  The owner can remove a member from the same page (a step-up action).
- An invite link opened in a browser where **another account is already
  signed in** offers **sign out and accept the invite**, rather than
  quietly landing in the session that was open.

### What each role can do

Everyone in a household **sees everything** — balances, transactions,
bills, reports. The roles differ in what they can change.

| | **owner** | **member** | **view-only** |
|---|---|---|---|
| See the household's money | ✓ | ✓ | ✓ |
| Ask the Assistant about it (it only reads) | ✓ | ✓ | ✓ |
| Edit transactions, bills, budgets, rules, categories, receipts | ✓ | ✓ | |
| Import files, run a sync, act on alerts | ✓ | ✓ | |
| Their own password, 2FA, sessions, email delivery | ✓ | ✓ | ✓ |
| Bank connections — link, repair, disconnect, remove an account | ✓ | | |
| Export the data, restore a backup, connections bundle | ✓ | | |
| Invite, remove, or change what another member can do | ✓ | | |
| SMTP, the daily-email recipient list, AI backend | ✓ | | |
| The account section of Settings, and deleting the account | ✓ | | |

**View-only is the default** — an invite creates a view-only login unless
you choose *member · can edit*.

A member can change anything about the household's money, and none of
the things that would end the account, take its data out, change who is
in it, or touch a bank connection. Those stay with the owner even for
someone you trust completely, because they are the acts that are hard to
undo or that reach outside the instance.

There is no way to make somebody else the owner. A household has exactly
one, and transferring it is not something an invite or a dropdown does.

## Activity — who changed what

When two people keep one set of books, "who changed this?" needs an
answer. **Settings → Users → Activity** on the web, and **More →
Activity** in the mobile app, list the household's hand-made changes,
newest first, each with the person and the time:

> **you** changed the category of Corner Market $42.50 on 09/12/26 from
> General Merchandise to Food and Drink
>
> **sam@example.com** edited the bill FiberLink Internet: amount $65.00 →
> $70.00
>
> **you** added a note on Hillside Hardware $123.45 on 09/10/26: "warranty in
> the folder"

What lands there: category corrections (including a "teach the merchant"
and a bulk file), splits, notes, bills — added, edited, paused, archived,
restored, and the finder's offers you confirm or dismiss (from the app or
from the buttons in the daily email) — rules, receipts attached or
removed, reimbursement flags and matches, business flags, merchant
renames and merges, account nicknames and budget exclusions, and a
settings save (which names the settings that changed and never their
values).

What does not: anything the app does on its own — a sync, the nightly
bill pass stamping its rows, the categorizer, the store matchers. The log
answers "who did this", and for the app's own work the answer is always
the app.

Filter by person or by kind; **show older** pages back. A change made
through a script token is listed as that script. Someone removed from the
household keeps their name on the changes they made — that is the point
of a log. The log travels in your data export and restores with it, and
keeps two years.

## Delete account

Owner-only, and permanent: every account, transaction, budget, and
setting in the household is erased atomically, and everyone is signed
out. Confirm with your password, your 2FA code if enrolled (passkey or
recovery code for passkey-only), and by typing **delete everything**.
Anything the household holds at an external service — bank connections,
and whatever an operator's add-on holds for the account — is released
before the rows go. Export your data first
(Your data section) if you want a copy; there is no undo.

## Gotchas

- A password change **removes all passkeys, signs out every other device
  and revokes all script tokens** on purpose — plan to re-enroll and re-mint
  right after. An **email change** does exactly the same (passkeys
  included) — the address is the login identifier.
- Recovery codes appear exactly once. If they're gone and TOTP is on,
  regenerate — the old set is voided the moment a new one is issued.
- A rejected 6-digit code usually means the authenticator app's clock
  is off, not a wrong secret.
- Security doors are rate-limited — repeated wrong passwords lock the
  door for a while rather than allowing unlimited guesses.
- On a demo instance the password, 2FA, and passkey controls are
  disabled — the credentials are shared, so changing them would only
  lock out the next visitor. Settings are read-only there for the same
  reason; preferences that only change what you see are kept on your own
  device instead.
