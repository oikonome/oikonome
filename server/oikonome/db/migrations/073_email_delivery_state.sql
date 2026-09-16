-- Without this the app has no idea when its mail stops arriving.
--
-- The failure mode is concrete: an account created with a typo'd domain
-- (`…@gmaill.com`). The first daily verdict hard-bounces, the provider then
-- marks the address inactive, and every send after that is refused at the
-- API before it leaves the building — nothing delivered, and not one byte of
-- it reaching the person whose money it is about. The failure gets QUIETER
-- over time, not louder: once an address is suppressed there is no bounce
-- any more, just a rejected send nobody reads.
--
-- This table is the app's memory of that. One row per address, and a row only
-- exists while something is WRONG — absence means "no reason to think this
-- address is broken", which is the honest default for an address we have never
-- had trouble with. Clearing (a successful send, or a fresh verification)
-- deletes the row rather than flipping a flag, so the hot-path read stays a
-- single indexed lookup and there is no "cleared but still here" state to get
-- subtly wrong.
--
-- Control-plane, no RLS — addresses are not tenant-scoped (the same address can
-- be a recipient on two tenants, and the provider's verdict is about the
-- MAILBOX, not the household). WRITES are admin-only, the way the
-- spam-defense tables work: a row here pauses an address's scheduled mail, so
-- injected app-role SQL must not be able to invent a bounce to silence someone
-- else's verdict, nor scrub its own. READS are granted to the app role
-- (migrate.py) on purpose — they sit on the /api/me hot path and expose
-- nothing the app role cannot already see on users.
-- No tenant_id also means tenant erasure cannot discover this table —
-- tenancy.delete_tenant_rows sweeps it BY ADDRESS instead.

CREATE TABLE IF NOT EXISTS email_delivery_state (
    email        TEXT PRIMARY KEY,          -- lowercased
    state        TEXT NOT NULL,             -- 'bouncing' | 'complained'
    -- the provider's own words, kept verbatim: a person fixing this needs to
    -- see "unknown user" or "mailbox full", not our paraphrase of it
    bounce_type  TEXT,                      -- HardBounce, SpamNotification, …
    reason       TEXT,
    -- the provider's bounce id, so re-verifying an address can REACTIVATE it
    -- upstream. Without this a corrected address stays dead on the provider's
    -- suppression list no matter what we do locally.
    provider_id  TEXT,
    -- true while the provider is refusing sends to this address on its own
    -- (Postmark "Inactive"). Distinct from state: a soft failure we recorded
    -- ourselves is not a suppression.
    suppressed   BOOLEAN NOT NULL DEFAULT FALSE,
    fail_count   INTEGER NOT NULL DEFAULT 1,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- the console's "who is not receiving mail" view, and nothing else — the hot
-- path is the primary key
CREATE INDEX IF NOT EXISTS idx_email_delivery_state_last
  ON email_delivery_state (last_seen DESC);
