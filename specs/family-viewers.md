# Family members with view-only access (spec)

This spec also settles how authentication evolves. The decisions:

1. **Auth model: email+password stays the floor** — it works
   on every instance with zero SMTP/infra. **Passkeys become an optional
   per-user upgrade** (WebAuthn; phishing-resistant, nicer than TOTP) in
   a follow-up chunk. Magic links rejected: they hard-depend on SMTP,
   which most self-hosted instances don't configure.
2. **Invites are share-a-link on self-host, ADDRESSED on hosted**:
   the owner generates a one-time invite URL (same pattern as the setup
   link) — single use, 7-day expiry, carries the role. Self-hosted, it is
   shared over any channel and the instance also emails it when SMTP is
   configured. Hosted, the mint REQUIRES the invitee's address, the link
   is mailed to that address and never returned to the owner, and only
   that address may claim it. The claim form is pre-auth and proves
   nothing about who is typing, so on hosted — where `users.email` is one
   namespace shared by every household — an unaddressed link let any
   account holder create a row bearing a stranger's address inside their
   own household. Delivery is the mailbox proof that door otherwise has
   none of. Either way the invitee picks their own password.
3. **Viewers see everything, read-only.** Full household transparency —
   every page visible, every mutating action blocked server-side (the
   hard guarantee) and progressively hidden in the UI. Own-account
   actions stay allowed: password change, TOTP, their sessions, sign
   out, and sending feedback.
4. **users.role: 'owner' | 'viewer', editor-ready.** Enforcement keys
   off "role != 'owner' means no writes", so a future 'editor' tier is
   one value + a finer allowlist, not a redesign.

## Mechanics

- Migration 012: `users.role TEXT NOT NULL DEFAULT 'owner'` (existing
  users become owners); `invites` control-plane table (token_hash PK,
  tenant_id, role, label, created_by, created_at, expires_at, used_at).
- `lookup_session` returns the role; `current_user` — the single choke
  point every protected route depends on — 403s non-GET requests from
  non-owners except the own-account allowlist.
- Owner APIs: POST/GET/DELETE `/api/invites`, GET `/api/users`,
  DELETE `/api/users/{id}` (not self). Public: GET `/invite/{token}`
  (redirects into the SPA claim page), POST `/api/invite/claim`
  {token, email, password} → creates the user with the invite's role,
  burns the token, signs them in. On hosted the member starts
  **unverified** (the claim form takes any address, so mailbox control
  is earned via the emailed link, exactly like signup — until then the
  address gets no household mail); self-host members are verified at
  birth. A household holds at most **10 members**; a claim against a
  full household is refused (and, per the burn-first doctrine, still
  spends the invite).
- SPA: Family card on Settings (members + pending invites, create/copy/
  revoke — owner only); public claim page; "view-only" pill in the
  header for viewers; owner-gated chrome (sync button, Import nav,
  Finish-setup pill, tenant-config Settings cards).
