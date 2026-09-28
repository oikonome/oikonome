# Continuity packet (spec)

One PDF, printed on demand from what the instance already holds, that a
household's survivor can read cold: every account and where it is held,
the balances that day, the recurring bills and what pays them, the
income, the businesses, and a note in the owner's own words about where
the rest is. Decisions:

1. **A map, never a key.** No credential of any kind is in the packet —
   no password, no bank token, no full EIN (the last four only, with a
   sentence saying where the full one is). The packet is meant to be
   printed and left in a drawer; what makes that safe is that it opens
   nothing. The owner's note is where the keys' *location* is written.
2. **Generated, not maintained.** Everything but the note and the name
   comes from the ledger at print time: accounts (`accounts` × `items`,
   hidden and shadow rows out), net worth (`reporting.compute_networth`),
   debts with rates and minimums (`debt.load_debts`; a mortgage prints
   the servicer's reported payment, escrow included, because that is
   what must be paid — the planner's P&I minimum sits beside it), active
   bills and income (`bills`, with the paying account named), businesses
   (`entities` + `compliance.obligations`), and the household roster
   (`users`). A stale packet is one click from fresh; the footer carries
   the print date.
3. **Two owner-written fields**, in `tenant_settings.config`:
   `continuity_note` (≤ 4000 chars, printed first) and `continuity_for`
   (≤ 120, goes in the title). No new table — they are household
   settings like any other, mirrored and exported with the rest.
4. **Leaves through the export door.** The PDF is the whole shape of the
   household's money, so it is fetched exactly like the ZIP: owner only,
   `POST /api/export/token {kind:"continuity"}` after a FRESH elevation,
   then `GET /export/continuity?t=` redeems the one-shot ticket. A bare
   session cookie never starts the download.
5. **The emailed copy follows the household mail rule.**
   `POST /api/continuity/email {to}` steps up the same way, then
   `mailguard.check_recipients`: hosted mails balances only to an address
   with an account on the tenant; self-host sends to any address the
   operator's relay carries. One address per call, 6 per hour per IP,
   attachment through `report.send`. Without a configured relay the door
   says to download instead.
6. **Every copy is logged.** `activity_log` rows `settings /
   continuity_downloaded` and `settings / continuity_emailed <address>`,
   so the household can see when a copy left and where it went.

## Mechanics

- `engine/continuity.py`: `gather(conn, members=, base_url=)` → plain
  dict (tests read this, never the PDF); `render_pdf(dict)` → bytes
  (reportlab platypus, letter, tables that repeat their header, a
  KeepTogether per institution and per business); `clean_note` /
  `clean_for` are the field validators; `filename(date)`.
- `web/pages.py`: `"continuity"` in `_EXPORT_KINDS`;
  `GET /export/continuity`; `_continuity_pdf(user)` builds the packet
  for both doors (roster from the control plane, everything else on the
  tenant connection).
- `web/api.py`: `GET /api/continuity` (fields + `mail_configured`,
  `members_only`, `members`, `note_max` — every role reads; it is
  household truth like the pages), `PUT /api/continuity` (owner; a save
  of one field leaves the other; empty clears), `POST /api/continuity/email`.
- Clients: `ContinuityCard` on Settings → Data (web) and under the
  "Your data" card in More → Settings (mobile; PDF to the share sheet via
  the existing authenticated `download`). On hosted the recipient is a
  pick from the roster; on self-host a free address with the roster as
  suggestions.
- Docs: `docs/guides/continuity-packet.md` (after Security in the guide
  order).
