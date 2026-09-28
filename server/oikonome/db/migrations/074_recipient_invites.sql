-- A daily-email recipient must agree before anything is mailed to them.
--
-- Two reasons. Consent: this is financial mail, and the recipient is the
-- only party who can say yes to it. And a typo'd address never announces
-- itself: the mail goes somewhere, the person it was meant for never
-- mentions it, and the owner cannot tell "they're reading it" from "it is
-- landing in a stranger's inbox".
--
-- So an added recipient is an INVITE. It is mailed a note that explains what
-- the daily email is, who added them and when it arrives, and it carries a
-- link. Until that link is opened, the address is on the list but NOT mailed
-- anything else — `worker._recipients_raw` gates on `accepted_at`.
--
-- One row per (tenant, address), not a log. A resend rotates the token in
-- place, which makes "only the newest link works" a property of the schema
-- rather than a rule some code has to remember (email_verifications needs an
-- explicit burn-the-others UPDATE for exactly this reason).
--
-- Control-plane posture, no RLS, and the app role gets NOTHING — every read
-- and write runs on the admin connection, like the spam-defense tables. The
-- asymmetry matters more here than on `email_delivery_state`: a forged
-- `accepted_at` row is a way to make a household's financial mail flow to an
-- attacker's mailbox, so an injected app-role INSERT must not be able to
-- create one. `email_recipients` itself stays app-writable in tenant_settings
-- (RLS-scoped) — writing config alone only proposes a recipient; it
-- cannot enrol one.

CREATE TABLE IF NOT EXISTS recipient_invites (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,               -- lowercased
    -- The hash SURVIVES being answered: `answered()` looks a spent link up
    -- by it, so someone re-opening their own invite mail is told what they
    -- already chose instead of "invalid or expired", which would send a
    -- recipient who had DECLINED off to ask the household to re-add them,
    -- something invite() refuses by design. NULL only on the rows the
    -- backfill below writes, which stand for consent that predates this
    -- table and never had a link.
    token_hash   TEXT,
    invited_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ,
    accepted_at  TIMESTAMPTZ,
    -- "no thanks", from the same link — an invite with no way to refuse is
    -- not a request, and the person who declines is precisely the person we
    -- must never mail again.
    declined_at  TIMESTAMPTZ,
    last_sent_at TIMESTAMPTZ,
    send_count   INTEGER NOT NULL DEFAULT 0
);

-- one invite per address per household; re-adding someone finds their row
CREATE UNIQUE INDEX IF NOT EXISTS idx_recipient_invites_tenant_email
    ON recipient_invites (tenant_id, email);
-- the link lookup, and the guarantee that one token means one invite
CREATE UNIQUE INDEX IF NOT EXISTS idx_recipient_invites_token
    ON recipient_invites (token_hash) WHERE token_hash IS NOT NULL;

-- Backfill: every address already configured as a recipient is marked
-- ACCEPTED, with no mail sent.
--
-- The alternative — inviting everyone on upgrade — would stop mail that is
-- working for people who never asked for it to stop, and would send a
-- surprise "confirm you want this" to households already reading the
-- verdict together. A migration must not silently pause working mail.
-- accepted_at is stamped as the
-- migration time, and token_hash stays NULL, so these rows are honestly
-- distinguishable from a link someone actually opened.
INSERT INTO recipient_invites (tenant_id, email, accepted_at)
SELECT s.tenant_id, lower(trim(r)), now()
  FROM tenant_settings s,
       LATERAL jsonb_array_elements_text(
           CASE WHEN jsonb_typeof(s.config->'email_recipients') = 'array'
                THEN s.config->'email_recipients' ELSE '[]'::jsonb END) AS r
 WHERE trim(r) <> ''
ON CONFLICT (tenant_id, email) DO NOTHING;
