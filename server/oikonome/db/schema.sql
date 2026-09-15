-- oikonome schema v1 — Postgres 16+, row-level tenancy.
--
-- TENANCY RULES (load-bearing; see CLAUDE.md → Multi-tenancy):
--   * every domain table: tenant_id UUID NOT NULL
--       DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
--     so ported engine INSERTs need no tenant_id changes — the ambient
--     session setting fills it.
--   * composite PKs (tenant_id, id); composite FKs.
--   * RLS enabled on every domain table; the app connects as the
--     NON-superuser role `oikonome_app` (created by migrate.py), so RLS is
--     always enforced. Policies read app.tenant_id; unset ⇒ zero rows.
--   * amount sign follows Plaid everywhere: POSITIVE = money out.

-- ---------- control tables (no RLS; only admin code touches them) ----------

CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT NOT NULL DEFAULT 'active'   -- active | suspended | deleting
);

CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,                 -- argon2id
    totp_secret   TEXT,                          -- NULL = 2FA off
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at   TIMESTAMPTZ,
    role TEXT NOT NULL DEFAULT 'owner' -- owner | viewer
);
-- pre-012 databases lack role (schema.sql runs before migrations)
ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'owner';

-- passkeys (WebAuthn) — control-plane like users/sessions
CREATE TABLE IF NOT EXISTS passkeys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id TEXT NOT NULL UNIQUE,          -- base64url
    public_key    TEXT NOT NULL,                 -- base64url COSE
    sign_count    BIGINT NOT NULL DEFAULT 0,
    transports    TEXT NOT NULL DEFAULT '',      -- comma-joined hints
    label         TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_passkeys_user ON passkeys(user_id);

CREATE TABLE IF NOT EXISTS webauthn_challenges (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose    TEXT NOT NULL,                    -- register | login
    user_id    UUID,                             -- register: whose; login: NULL
    challenge  TEXT NOT NULL,                    -- base64url
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
-- a step-up ticket records WHICH credential signed the assertion that
-- minted it (migration 120) — a credential rotation keeps exactly that key
-- and evicts every other, including one an attacker keeps freshly used.
ALTER TABLE webauthn_challenges
    ADD COLUMN IF NOT EXISTS passkey_id UUID
        REFERENCES passkeys(id) ON DELETE CASCADE;

-- one-time share-link invites — control-plane like users
CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,                 -- sha256 of the URL token
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'viewer',
    label      TEXT NOT NULL DEFAULT '',         -- who it's for (display only)
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_invites_tenant ON invites(tenant_id);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,                -- sha256 of the cookie token
    id          UUID NOT NULL DEFAULT gen_random_uuid(),  -- API-safe handle
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    last_seen   TIMESTAMPTZ,
    user_agent  TEXT
);
-- pre-006 databases lack id/last_seen; schema.sql runs BEFORE migrations,
-- so the index below needs the column to exist either way (idempotent)
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS id UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ip TEXT,
    -- when this session last re-proved identity (migration 116): the
    -- ten-minute "sudo" window the account-security doors read instead
    -- of asking for the password on every request
    ADD COLUMN IF NOT EXISTS elevated_at TIMESTAMPTZ,
    -- and WHICH passkey proved it (migration 120), so a credential
    -- rotation keeps that key and no other. NULL means the proof was a
    -- password or a recovery code, and no key is kept.
    ADD COLUMN IF NOT EXISTS elevated_passkey_id UUID;
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_id ON sessions(id);

-- script tokens — long-lived bearer credentials for host-side
-- collector scripts; control-plane like sessions (lookup precedes tenant
-- scope). Restricted server-side to the import doors.
CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash   TEXT PRIMARY KEY,               -- sha256 of the oik_ token
    id           UUID NOT NULL DEFAULT gen_random_uuid(),  -- API-safe handle
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    name         TEXT NOT NULL DEFAULT '',       -- which script (display only)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_tenant ON api_tokens(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_tokens_id ON api_tokens(id);

-- mobile device tokens: long-lived bearer credentials for the native
-- apps — full-session access (unlike script tokens), minted by an
-- authenticated session, revocable from the sessions panel. Mirrors
-- migration 084.
CREATE TABLE IF NOT EXISTS device_tokens (
    token_hash   TEXT PRIMARY KEY,               -- sha256 of the oikd_ token
    id           UUID NOT NULL DEFAULT gen_random_uuid(),  -- API-safe handle
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_name  TEXT NOT NULL DEFAULT '',       -- e.g. "Pixel 8" (display only)
    platform     TEXT NOT NULL DEFAULT '',       -- ios | android
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,           -- sliding idle deadline
    last_seen    TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    -- native push routing (opaque platform token; no account data)
    push_token    TEXT,
    push_platform TEXT,                          -- fcm | apns
    push_updated  TIMESTAMPTZ,
    push_dead_at  TIMESTAMPTZ                    -- push service said gone
);
-- the phone's own "sudo" window (migration 116) — a device token is a
-- session by another name, and an elevation must not leak across rows
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS elevated_at TIMESTAMPTZ;
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS elevated_passkey_id UUID;
CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_tokens_id ON device_tokens(id);

CREATE TABLE IF NOT EXISTS password_resets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,            -- sha256 of the link token
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,             -- 1 hour from creation
    used_at     TIMESTAMPTZ                       -- one-time: set on consume
);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id);

-- A cooling-off request for a factor-clearing reset, for the account that
-- has lost password, authenticator and recovery codes together (migration
-- 122). Control plane, no RLS; the cancel link token is stored hashed.
CREATE TABLE IF NOT EXISTS factor_resets (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    lands_at          TIMESTAMPTZ NOT NULL,
    cancel_token_hash TEXT NOT NULL UNIQUE,
    cancelled_at      TIMESTAMPTZ,
    cancel_reason     TEXT,                      -- link | sign-in | superseded
    consumed_at       TIMESTAMPTZ,               -- the reset that used it
    requested_ip      TEXT
);
CREATE INDEX IF NOT EXISTS idx_factor_resets_user ON factor_resets(user_id);

-- signup gate (mirrored in migration 023 for
-- existing databases). Operator-minted invites: admin role writes, the
-- app role can only read + burn. email_verifications follows the
-- password_resets token discipline exactly.
CREATE TABLE IF NOT EXISTS signup_invites (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash     TEXT NOT NULL UNIQUE,
    email          TEXT NOT NULL,
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    used_at        TIMESTAMPTZ,
    used_by_tenant UUID,
    -- what an installed add-on wants the new account born with
    options        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_signup_invites_email ON signup_invites(email);

CREATE TABLE IF NOT EXISTS email_verifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_email_verifications_user
    ON email_verifications(user_id);

-- Spam defense (mirrored in migration 060): control plane, no RLS; writes
-- are admin-role only, the check runs on an admin connection.
CREATE TABLE IF NOT EXISTS spam_domains_public (
    domain     TEXT PRIMARY KEY,
    sources    TEXT[] NOT NULL DEFAULT '{}',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS spam_domains_blocked (
    domain   TEXT PRIMARY KEY,
    reason   TEXT,
    source   TEXT NOT NULL DEFAULT 'manual',
    added_by TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS spam_addresses_blocked (
    email    TEXT PRIMARY KEY,
    reason   TEXT,
    source   TEXT NOT NULL DEFAULT 'manual',
    added_by TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS spam_reputation_cache (
    domain     TEXT PRIMARY KEY,
    verdict    TEXT NOT NULL,
    score      JSONB NOT NULL DEFAULT '{}'::jsonb,
    source     TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS blocklist_meta (
    id              BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    last_fetched_at TIMESTAMPTZ,
    public_count    INTEGER NOT NULL DEFAULT 0,
    source_state    JSONB NOT NULL DEFAULT '{}'::jsonb
);
INSERT INTO blocklist_meta (id) VALUES (true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS spam_drops (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain     TEXT,
    surface    TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_spam_drops_created ON spam_drops(created_at DESC);

-- ---------- domain tables (RLS) ----------

CREATE TABLE IF NOT EXISTS tenant_settings (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    config     JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id)
);

-- A month's budget, frozen as the month closes: the nightly job upserts the
-- CURRENT month's config here and never touches earlier months, so the last
-- write before the month turns is the budget the month actually ran under.
-- Closed months are judged against their snapshot; a closed month with no
-- row shows net income vs spending instead of a budget verdict.
CREATE TABLE IF NOT EXISTS budget_snapshots (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    year        INT NOT NULL,
    month       INT NOT NULL CHECK (month BETWEEN 1 AND 12),
    config      JSONB NOT NULL,
    -- the bill schedule frozen with the budget: {rows: [active bill rows],
    -- bills_monthly, income_monthly}. Closed months load their fixed
    -- schedule from here so a later bill edit can't re-judge them; NULL
    -- (pre-freeze snapshots) falls back to the live bills table.
    bills       JSONB,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source      TEXT NOT NULL DEFAULT 'close',  -- close | backfill
    PRIMARY KEY (tenant_id, year, month)
);

-- The categorizer's learned overlay: a per-tenant model retrained nightly
-- from this household's own corrections (see engine/model_train.py). In
-- the database so RLS, backups and restore carry it; the read-only
-- /models artifact stays the deployment-wide floor.
CREATE TABLE IF NOT EXISTS categorizer_model (
    tenant_id   UUID PRIMARY KEY DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    artifact    BYTEA NOT NULL,
    trained_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    samples     INT NOT NULL,
    classes     INT NOT NULL,
    sklearn_version TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS items (
    tenant_id        UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id               TEXT NOT NULL,              -- aggregator item/connection id
    aggregator       TEXT NOT NULL DEFAULT 'plaid',  -- plaid | simplefin | csv
    institution_id   TEXT,
    institution_name TEXT,
    access_token     TEXT,                       -- encrypted at rest (app-layer envelope)
    tx_cursor        TEXT,
    status           TEXT DEFAULT 'ok',
    linked_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- a document, always: every writer extends it with `||` / jsonb_set,
    -- which raise on a scalar and silently append to an array
    raw              JSONB CONSTRAINT items_raw_is_an_object
                     CHECK (raw IS NULL OR jsonb_typeof(raw) = 'object'),
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS accounts (
    tenant_id         UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id                TEXT NOT NULL,
    item_id           TEXT,
    name              TEXT,
    display_name      TEXT,                      -- user override; sync never touches
    official_name     TEXT,
    type              TEXT,
    subtype           TEXT,
    type_user_set     BOOLEAN NOT NULL DEFAULT FALSE,  -- user classified; sync never overwrites type/subtype
    mask              TEXT,
    balance_current   DOUBLE PRECISION,
    balance_available DOUBLE PRECISION,
    balance_limit     DOUBLE PRECISION,
    currency          TEXT,
    updated_at        TIMESTAMPTZ,
    raw               JSONB,
    entity_id UUID, -- account belongs to a business entity (whole-account assignment)
    user_removed_at   TIMESTAMPTZ,               -- soft-remove: hide this account, keep connection
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, item_id) REFERENCES items(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transactions (
    tenant_id              UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id                     TEXT NOT NULL,
    account_id             TEXT,
    date                   DATE,
    authorized_date        DATE,
    amount                 DOUBLE PRECISION,     -- positive = money out (Plaid)
    name                   TEXT,
    merchant_name          TEXT,
    category_primary       TEXT,                 -- working machine category
    category_detailed      TEXT,
    category_plaid         TEXT,                 -- last Plaid PFC primary (sync-only)
    category_plaid_detailed TEXT,                -- last Plaid PFC detailed
    category_plaid_confidence TEXT,              -- VERY_HIGH|HIGH|MEDIUM|LOW|…
    category_override      TEXT,                 -- user-sacred; sync never touches
    pending                INTEGER DEFAULT 0,
    pending_transaction_id TEXT,
    payment_channel        TEXT,
    removed                INTEGER NOT NULL DEFAULT 0,
    raw                    JSONB,
    entity_id UUID, -- per-transaction business override (e.g. business cost on a personal card)
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, account_id) REFERENCES accounts(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_txn_tenant_date ON transactions(tenant_id, date);
CREATE INDEX IF NOT EXISTS idx_txn_tenant_account ON transactions(tenant_id, account_id);

CREATE TABLE IF NOT EXISTS liabilities (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    account_id TEXT NOT NULL,
    as_of      TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw        JSONB,
    PRIMARY KEY (tenant_id, account_id)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id        BIGINT GENERATED ALWAYS AS IDENTITY,
    tenant_id UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    ran_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    item_id   TEXT,
    added     INTEGER,
    modified  INTEGER,
    removed   INTEGER,
    error     TEXT,
    PRIMARY KEY (tenant_id, id)
);

-- push-heartbeats for host-side collector scripts (sync doors
-- stamp; Doctor renders staleness; worker sends opt-in stale emails).
-- expected_hours: NULL = undecided (auto-watches at 24 on a second-day
-- push), 0 = never warn.
CREATE TABLE IF NOT EXISTS script_heartbeats (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    source         TEXT NOT NULL,          -- item_id or a script-chosen slug
    label          TEXT,                   -- display name for the panel
    first_push     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_push      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_rows      INTEGER,
    expected_hours INTEGER,                -- NULL undecided · 0 off · N watch
    alerts         INTEGER NOT NULL DEFAULT 0,
    alerted_at     TIMESTAMPTZ,            -- edge-trigger guard, cleared on push
    via            TEXT,                   -- token | exec | app (push mechanism)
    kind           TEXT,                   -- api | scrape (collection half)
    PRIMARY KEY (tenant_id, source)
);

-- feedback submissions (number + message; the package itself is
-- emailed or downloaded, never stored)
CREATE TABLE IF NOT EXISTS feedback_reports (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    number     TEXT NOT NULL,            -- FB-YYYYMMDD-xxxx
    message    TEXT NOT NULL DEFAULT '',
    delivery   TEXT NOT NULL,            -- emailed | downloaded
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind       TEXT NOT NULL DEFAULT 'feedback',   -- feedback | bug
    PRIMARY KEY (tenant_id, number)
);

-- live progress for user-triggered background jobs (the setup
-- wizard's Sync step polls this); one row per tenant per job kind
CREATE TABLE IF NOT EXISTS job_progress (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         TEXT NOT NULL,            -- job kind: 'sync'
    state      TEXT NOT NULL,            -- running | done | error
    progress   JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

-- multi-source accounts — one group_id = one real-world
-- account fed by several sources; effective primary computed at read
-- time (health-aware), home_rank is the user's preference order
CREATE TABLE IF NOT EXISTS account_links (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    group_id   UUID NOT NULL,
    account_id TEXT NOT NULL,
    home_rank  INTEGER NOT NULL DEFAULT 0,
    failover_alerted TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, account_id),
    -- migration 100: a link is meaningless without its account, and the
    -- collector unlink paths delete accounts without clearing links.
    -- Collector account ids are derived, so the orphan re-attaches on the
    -- next import and silently shadow-excludes the new account from every
    -- money aggregate.
    CONSTRAINT account_links_account_fk FOREIGN KEY (tenant_id, account_id)
        REFERENCES accounts (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_account_links_group
    ON account_links(tenant_id, group_id);

-- receipts on transactions (images in-db; line items taggable)
CREATE TABLE IF NOT EXISTS receipts (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    txn_id     TEXT NOT NULL,
    image      BYTEA NOT NULL,
    mime       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'receipt',  -- receipt | check
    status     TEXT NOT NULL DEFAULT 'uploaded', -- uploaded | parsed | failed
    parsed     JSONB,
    error      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    parsed_at  TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, id),
    -- migration 071: the FK migration 067 gave every other transaction-child
    -- table. Without it a purge/resync (collector ids are content hashes, so
    -- the same id comes back) re-attached an orphaned receipt IMAGE to a
    -- transaction that never had one.
    CONSTRAINT receipts_txn_fk FOREIGN KEY (tenant_id, txn_id)
        REFERENCES transactions (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_receipts_txn ON receipts(tenant_id, txn_id);

CREATE TABLE IF NOT EXISTS receipt_items (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    receipt_id  UUID NOT NULL,
    line        INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    qty         DOUBLE PRECISION,
    amount      DOUBLE PRECISION,
    tag         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, receipt_id, line),
    -- migration 076: the cascade migration 071's comment assumed — without
    -- it, receipts' own txn cascade stranded line items forever
    CONSTRAINT receipt_items_receipt_fk FOREIGN KEY (tenant_id, receipt_id)
        REFERENCES receipts (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bills (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id             TEXT NOT NULL,                -- rec:<slug>
    type           TEXT,                         -- BILL | INCOME
    payee          TEXT,
    amount         DOUBLE PRECISION,             -- per occurrence; negative = money out
    frequency      TEXT,
    monthly_amount DOUBLE PRECISION,
    due_on         DATE,
    category       TEXT,
    account_id     TEXT,
    merchant       TEXT,                         -- canonical match key
    is_completed   INTEGER,
    active         INTEGER,
    synced_at      TIMESTAMPTZ,
    -- a document, always (see items.raw)
    raw            JSONB CONSTRAINT bills_raw_is_an_object
                   CHECK (raw IS NULL OR jsonb_typeof(raw) = 'object'),
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS bill_proposals (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         TEXT NOT NULL,                    -- prop:<sha1 evidence signature>
    kind       TEXT,
    bill_type  TEXT DEFAULT 'occurrence',
    payee      TEXT,
    amount     DOUBLE PRECISION,
    frequency  TEXT,
    interval   INTEGER,
    next_due   DATE,
    evidence   JSONB,
    llm_tag    TEXT,
    status     TEXT DEFAULT 'pending',
    created_at TIMESTAMPTZ,
    decided_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, id)
);

-- merchants the ledger thinks are one business, offered to the person
-- (engine/merchant_merge.py): id is deterministic per pair so a decision
-- sticks and a re-run inserts nothing
-- a pending charge a person retired by hand: removed=1 like any soft
-- retirement, but stamped so the next sync's upsert leaves it retired
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS merchant_merge_proposals (
    tenant_id        UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id               TEXT NOT NULL,
    from_merchant_id UUID NOT NULL,
    into_merchant_id UUID NOT NULL,
    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS reimbursements (
    tenant_id    UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    expense_id   TEXT NOT NULL,
    reimburse_id TEXT NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0, -- nets amount, expense stays spend
    amount       DOUBLE PRECISION,            -- received credited to the expense
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, expense_id, reimburse_id)
);

CREATE TABLE IF NOT EXISTS reimburse_flags (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    txn_id     TEXT NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0, -- expect only a fraction back
    expected   DOUBLE PRECISION,              -- optional expected-back value
    flagged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, txn_id)
);

CREATE TABLE IF NOT EXISTS business_flags (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    txn_id     TEXT NOT NULL,
    flagged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, txn_id)
);

-- free-text per-transaction notes (see migration 059)
CREATE TABLE IF NOT EXISTS transaction_notes (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    txn_id     TEXT NOT NULL,
    note       TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, txn_id)
);

-- business entities + members (see migration 054 for the full note).
CREATE TABLE IF NOT EXISTS business_entity (
    tenant_id           UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    structure           TEXT NOT NULL,           -- single_member_llc | multi_member_llc | sole_prop | s_corp
    state               TEXT,
    formation_date      DATE,
    business_start_date DATE,                     -- §195 start-up hinge
    ein_enc             TEXT,                     -- encrypted EIN (crypto.encrypt)
    ein_last4           TEXT,
    registered_agent    TEXT,
    fiscal_year_end     TEXT,                     -- 'MM-DD'
    status              TEXT NOT NULL DEFAULT 'active',
    home_office_sqft INTEGER, -- simplified home-office
    income_tax_rate NUMERIC(5,2), -- assumed effective % for estimated tax
    filing_status TEXT,
    -- The account this entity keeps its tax money in. NULL = not nominated,
    -- and the set-aside card then measures all business cash and says so.
    -- Deliberately TEXT, not a FK: accounts.id is the aggregator's own id
    -- and an account can be purged out from under this without taking the
    -- entity's tax settings with it.
    tax_reserve_account_id TEXT,
    archived_at         TIMESTAMPTZ,             -- when the business was closed (status='archived')
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS entity_membership (
    tenant_id     UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id            UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id     UUID NOT NULL,
    member_name   TEXT NOT NULL,
    ownership_pct NUMERIC(6,3),
    is_manager    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entity_membership_entity ON entity_membership(tenant_id, entity_id);

-- Owner-equity movements (see migration 055).
CREATE TABLE IF NOT EXISTS equity_movement (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id  UUID NOT NULL,
    kind       TEXT NOT NULL,
    amount     NUMERIC(14,2) NOT NULL,
    date       DATE NOT NULL,
    member_id  UUID,
    txn_id     TEXT,
    form       TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_equity_entity ON equity_movement(tenant_id, entity_id);

-- Deduction bucket / Schedule C classification (see mig 056).
CREATE TABLE IF NOT EXISTS business_txn_class (
    tenant_id    UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    txn_id       TEXT NOT NULL,
    bucket       TEXT NOT NULL,
    sched_c_line TEXT,
    note         TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, txn_id)
);

-- Compliance calendar custom obligations (see migration 057).
CREATE TABLE IF NOT EXISTS compliance_obligation (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id  UUID NOT NULL,
    title      TEXT NOT NULL,
    due_date   DATE NOT NULL,
    recurrence TEXT NOT NULL DEFAULT 'yearly',
    fee        NUMERIC(10,2),
    url        TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_compliance_entity ON compliance_obligation(tenant_id, entity_id);

-- self-employed tax pack — mileage log + 1099 vendor marks (mig 058).
CREATE TABLE IF NOT EXISTS mileage_log (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id  UUID NOT NULL,
    date       DATE NOT NULL,
    miles      NUMERIC(10,1) NOT NULL,
    purpose    TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mileage_entity ON mileage_log(tenant_id, entity_id);

CREATE TABLE IF NOT EXISTS vendor_1099 (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id  UUID NOT NULL,
    merchant   TEXT NOT NULL,
    reportable BOOLEAN NOT NULL DEFAULT TRUE,
    tin_last4  TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, entity_id, merchant),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vendor1099_entity ON vendor_1099(tenant_id, entity_id);

-- entity_id assignment FKs on accounts/transactions (defined above; entity
-- table exists now). ON DELETE SET NULL: dropping an entity reverts its money
-- to personal, never deletes transactions.
-- Column-guarded: on a fresh DB the CREATE TABLEs above already carry
-- entity_id, so this adds the FK + index; on an EXISTING DB the column is
-- absent here (CREATE TABLE IF NOT EXISTS is a no-op) and migration 054 adds
-- the column + FK + index — so this block must skip cleanly, not error.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='accounts' AND column_name='entity_id') THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='accounts_entity_fk') THEN
            ALTER TABLE accounts ADD CONSTRAINT accounts_entity_fk
                FOREIGN KEY (tenant_id, entity_id)
                REFERENCES business_entity(tenant_id, id) ON DELETE SET NULL (entity_id);
        END IF;
        CREATE INDEX IF NOT EXISTS idx_accounts_entity ON accounts(tenant_id, entity_id);
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='entity_id') THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='transactions_entity_fk') THEN
            ALTER TABLE transactions ADD CONSTRAINT transactions_entity_fk
                FOREIGN KEY (tenant_id, entity_id)
                REFERENCES business_entity(tenant_id, id) ON DELETE SET NULL (entity_id);
        END IF;
        CREATE INDEX IF NOT EXISTS idx_txn_entity ON transactions(tenant_id, entity_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS manual_categories (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    transaction_id TEXT NOT NULL,
    category       TEXT NOT NULL,
    set_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- set when the override came from a BILL's transaction category (the
    -- rows the bill matches are categorized as the bill says) rather than
    -- from a person; NULL = a person's own correction, which always wins
    bill_id        TEXT,
    PRIMARY KEY (tenant_id, transaction_id)
);
ALTER TABLE manual_categories ADD COLUMN IF NOT EXISTS bill_id TEXT;

CREATE TABLE IF NOT EXISTS alerts_log (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         BIGINT GENERATED ALWAYS AS IDENTITY,
    kind       TEXT NOT NULL,
    severity   TEXT NOT NULL,
    message    TEXT NOT NULL,
    first_seen DATE NOT NULL,
    last_seen  DATE NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    dismissed  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, kind, message)
);

CREATE TABLE IF NOT EXISTS job_runs (
    tenant_id UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    job       TEXT NOT NULL,
    ran_at    TIMESTAMPTZ NOT NULL,
    note      TEXT,
    PRIMARY KEY (tenant_id, job)
);

-- ---------- history / reference tables (migration 008) ----------
-- Snapshot + reference data: investment &
-- crypto positions, net-worth history, the tax-return income spine, merchant
-- display/category maps, and the Amazon order pipeline's tables. Populated
-- by restore (export ZIPs). No FKs to accounts/transactions on purpose:
-- these are snapshot/reference rows that must survive account re-links and
-- removed transactions.

CREATE TABLE IF NOT EXISTS holdings (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    account_id TEXT NOT NULL,
    symbol     TEXT NOT NULL,              -- ticker or CUSIP
    name       TEXT,                       -- fund name
    quantity   DOUBLE PRECISION,           -- net shares
    price      DOUBLE PRECISION,           -- last known share price
    value      DOUBLE PRECISION,           -- quantity * price
    as_of      TIMESTAMPTZ,
    raw        JSONB,
    PRIMARY KEY (tenant_id, account_id, symbol)
);

CREATE TABLE IF NOT EXISTS crypto_holdings (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    account_id TEXT NOT NULL,
    currency   TEXT NOT NULL,
    quantity   DOUBLE PRECISION,
    native_usd DOUBLE PRECISION,
    as_of      TIMESTAMPTZ,
    raw        JSONB,
    PRIMARY KEY (tenant_id, account_id, currency)
);

-- Human-recorded net-worth anchors (statements and archives from whatever
-- tool came before). month stays TEXT 'YYYY-MM' — it is a label, not a date.
CREATE TABLE IF NOT EXISTS networth_recorded (
    tenant_id UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    month     TEXT NOT NULL,               -- 'YYYY-MM'
    total     DOUBLE PRECISION NOT NULL,
    source    TEXT,
    PRIMARY KEY (tenant_id, month)
);

-- Nightly live snapshots (the recorded trend accrues here).
CREATE TABLE IF NOT EXISTS networth_snapshot (
    tenant_id UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    date      DATE NOT NULL,
    total     DOUBLE PRECISION,
    by_class  JSONB,
    PRIMARY KEY (tenant_id, date)
);

-- Authoritative yearly income from tax returns / W-2s / SSA. Reference
-- overlay, NOT transactions. joint=1 rows are MFJ totals incl. spouse.
CREATE TABLE IF NOT EXISTS income_annual (
    tenant_id         UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    year              INTEGER NOT NULL,
    total_income      DOUBLE PRECISION,    -- 1040 total income line (joint if MFJ)
    agi               DOUBLE PRECISION,
    taxable_income    DOUBLE PRECISION,
    tax_paid          DOUBLE PRECISION,
    wages             DOUBLE PRECISION,    -- 1040 wages line (joint if MFJ)
    primary_wages     DOUBLE PRECISION,    -- primary's own W-2 wages
    investment_income DOUBLE PRECISION,    -- taxable interest + ordinary dividends
    capital_gain      DOUBLE PRECISION,    -- may be negative
    spouse_wages      DOUBLE PRECISION,
    ss_earnings       DOUBLE PRECISION,    -- SSA SS-taxed earnings (wage-base capped)
    medicare_earnings DOUBLE PRECISION,    -- SSA Medicare-taxed (uncapped ~= gross)
    filing_status     TEXT,
    joint             INTEGER DEFAULT 0,
    source            TEXT,
    note              TEXT,
    PRIMARY KEY (tenant_id, year)
);

-- Itemized income documents (W-2 / 1099 / 5498 / 1098) — full box detail in
-- amounts (JSONB). Original integer ids travel in restores, so no identity.
CREATE TABLE IF NOT EXISTS income_documents (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id             BIGINT NOT NULL,
    year           INTEGER NOT NULL,
    form           TEXT NOT NULL,          -- W-2, 1099-INT, 1099-DIV, ...
    owner          TEXT DEFAULT 'primary',
    payer          TEXT,
    -- Always NULL (migration 077): the full EIN is never stored in this
    -- exported table; business_entity keeps that identifier encrypted and
    -- export-excluded. The column stays so restoring an older export still
    -- parses.
    ein            TEXT,
    ein_last4      TEXT,                   -- all a display needs
    primary_amount DOUBLE PRECISION,       -- most-representative figure for the doc
    amounts        JSONB,                  -- all boxes/fields
    notes          TEXT,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_income_docs_tenant_year
    ON income_documents(tenant_id, year);

-- Canonical merchant map for DISPLAY/grouping only. Keyed by the raw
-- descriptor COALESCE(merchant_name, name); display surfaces read
-- COALESCE(canonical, merchant_name, name). method='llm' beats 'layer1'.
CREATE TABLE IF NOT EXISTS merchant_canonical (
    tenant_id    UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    raw_merchant TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    method       TEXT,                     -- 'layer1' (deterministic) | 'llm'
    as_of        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, raw_merchant)
);

-- The merchant as a ROW (migration 097): resolved once per transaction by
-- engine/merchant_identity.py, stored on transactions.merchant_id, read by
-- joining the id. merchant_canonical is the alias table under it (its
-- `canonical` mirrors merchants.name for readers not yet on the join).
CREATE TABLE IF NOT EXISTS merchants (
    tenant_id       UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id              UUID NOT NULL DEFAULT gen_random_uuid(),
    plaid_entity_id TEXT,
    name            TEXT NOT NULL,
    name_source     TEXT NOT NULL DEFAULT 'layer1',
    kind            TEXT NOT NULL DEFAULT 'merchant',
    logo_url        TEXT,
    website         TEXT,
    phone           TEXT,
    mcc             TEXT,
    parent_id       UUID,
    merged_into     UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS merchants_plaid_entity
    ON merchants (tenant_id, plaid_entity_id) WHERE plaid_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS merchants_lower_name
    ON merchants (tenant_id, lower(name));
ALTER TABLE merchant_canonical ADD COLUMN IF NOT EXISTS merchant_id UUID;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS merchant_id UUID;
-- migration 098: what the aggregator knows about the row, as columns
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS check_number      TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payment_processor TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_city     TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_region   TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_address  TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_postal   TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_lat      DOUBLE PRECISION;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_lon      DOUBLE PRECISION;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_store    TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS mcc               TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_source   TEXT;
CREATE INDEX IF NOT EXISTS transactions_check_number
    ON transactions (tenant_id, check_number) WHERE check_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS transactions_merchant ON transactions (tenant_id, merchant_id);
ALTER TABLE items ADD COLUMN IF NOT EXISTS logo TEXT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS brand_color TEXT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS url TEXT;

-- What a person changed a merchant's name to, and what it was before —
-- the undo journal for merchant_canonical, which keeps only the current
-- answer per raw string. Mirrors migration 095 (RLS in the block below;
-- the sequence grant is there because restore setval()s past replayed ids).
CREATE TABLE IF NOT EXISTS merchant_renames (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id             BIGINT GENERATED BY DEFAULT AS IDENTITY,
    raw_merchant   TEXT NOT NULL,
    from_canonical TEXT,
    to_canonical   TEXT NOT NULL,
    at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

CREATE INDEX IF NOT EXISTS merchant_renames_at
    ON merchant_renames (tenant_id, at DESC);

GRANT USAGE, SELECT, UPDATE ON SEQUENCE merchant_renames_id_seq
    TO oikonome_app;

-- LLM-sharpened merchant → category map (refines vague aggregator buckets).
CREATE TABLE IF NOT EXISTS merchant_categories (
    tenant_id        UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    merchant         TEXT NOT NULL,
    category_primary TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'llm',
    classified_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, merchant)
);

CREATE TABLE IF NOT EXISTS amazon_orders (
    tenant_id       UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    dedup_key       TEXT NOT NULL,
    account         TEXT NOT NULL,
    date            DATE NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,
    payee           TEXT NOT NULL DEFAULT 'Amazon',
    seller          TEXT NOT NULL DEFAULT '',
    memo            TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',
    category_source TEXT NOT NULL DEFAULT 'rules',
    order_number    TEXT NOT NULL DEFAULT '',
    is_refund       INTEGER NOT NULL,
    payment_method  TEXT NOT NULL DEFAULT '',
    -- '[]' as the default is the author saying "this column is a
    -- list"; say it to the database too. jsonb_array_elements over a
    -- row holding an object RAISES and aborts the whole statement, so
    -- one bad row would blank a query that reads every row in the
    -- household. Named to match the migration that adds it to an
    -- existing database, which then finds it already present.
    items_json      JSONB NOT NULL DEFAULT '[]'::jsonb
        CONSTRAINT amazon_orders_items_json_is_array
        CHECK (jsonb_typeof(items_json) = 'array'),
    inserted_at     TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_amazon_orders_tenant_date
    ON amazon_orders(tenant_id, date);

CREATE TABLE IF NOT EXISTS amazon_matches (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    transaction_id TEXT NOT NULL,
    dedup_key      TEXT,
    matched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS amazon_summaries (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    dedup_key  TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, dedup_key)
);

-- Costco receipts, pushed by a host-side collector the way Amazon orders
-- are: one row per warehouse, gas-station or costco.com receipt with its
-- line items (item number, the register's abbreviated description,
-- department, amount) and a per-item category, plus the receipt's
-- dominant category and a one-line breakdown. The engine matches each
-- receipt to the card charge that paid for it by amount and date, so a
-- $300 warehouse run reads "groceries $180 · household $60 · apparel $60"
-- instead of one GENERAL_MERCHANDISE row. Same sign convention as
-- amazon_orders: negative = purchase, positive = refund.
CREATE TABLE IF NOT EXISTS costco_receipts (
    tenant_id       UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    dedup_key       TEXT NOT NULL,
    account         TEXT NOT NULL DEFAULT 'main',
    date            DATE NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,
    receipt_type    TEXT NOT NULL DEFAULT 'warehouse',  -- warehouse | gas | online
    warehouse       TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',            -- dominant by amount
    category_source TEXT NOT NULL DEFAULT 'rules',
    summary         TEXT NOT NULL DEFAULT '',            -- "groceries $180 · household $60"
    is_refund       INTEGER NOT NULL DEFAULT 0,
    payment_method  TEXT NOT NULL DEFAULT '',
    -- '[]' as the default is the author saying "this column is a
    -- list"; say it to the database too. jsonb_array_elements over a
    -- row holding an object RAISES and aborts the whole statement, so
    -- one bad row would blank a query that reads every row in the
    -- household. Named to match the migration that adds it to an
    -- existing database, which then finds it already present.
    items_json      JSONB NOT NULL DEFAULT '[]'::jsonb
        CONSTRAINT costco_receipts_items_json_is_array
        CHECK (jsonb_typeof(items_json) = 'array'),
    inserted_at     TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_costco_receipts_tenant_date
    ON costco_receipts(tenant_id, date);

CREATE TABLE IF NOT EXISTS costco_matches (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    transaction_id TEXT NOT NULL,
    dedup_key      TEXT,
    matched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, transaction_id),
    CONSTRAINT costco_matches_txn_fk FOREIGN KEY (tenant_id, transaction_id)
        REFERENCES transactions (tenant_id, id) ON DELETE CASCADE
);

-- staged imports awaiting a second request (bulk plan, CSV column
-- mapping, taxdoc review) — in Postgres so any worker can finish what
-- another started, tenant-bound by RLS. One row per staged file; TTL sweep
-- + per-tenant byte budget enforced in db/staging.py.
CREATE TABLE IF NOT EXISTS import_staging (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    token      TEXT NOT NULL,
    idx        INTEGER NOT NULL DEFAULT 0,
    kind       TEXT NOT NULL,                    -- bulk | csv | taxdoc
    filename   TEXT,
    data       BYTEA,
    meta       JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, token, idx)
);

-- ---------- row-level security ----------
-- The DO block applies a uniform tenant-isolation policy to every domain
-- table. oikonome_app is NOT the table owner and NOT superuser ⇒ RLS applies.
-- The setting is read through a scalar subquery so the planner evaluates it
-- ONCE per statement (an InitPlan) instead of once per row scanned — same
-- predicate, a fraction of the per-row cost on a full ledger scan
-- (migration 110 converted existing installs; keep the two in step).

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'tenant_settings','budget_snapshots','categorizer_model','items','accounts','transactions','liabilities',
        'sync_log','script_heartbeats','feedback_reports','account_links','receipts','receipt_items','bills','bill_proposals','reimbursements',
        'reimburse_flags','business_flags','transaction_notes','manual_categories','alerts_log','job_runs','job_progress',
        'holdings','crypto_holdings','networth_recorded','networth_snapshot',
        'income_annual','income_documents','merchant_canonical','merchant_renames','merchants',
        'merchant_categories','merchant_merge_proposals','amazon_orders','amazon_matches','amazon_summaries',
        'costco_receipts','costco_matches',
        'import_staging','business_entity','entity_membership','equity_movement',
        'business_txn_class','compliance_obligation','mileage_log','vendor_1099']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = (SELECT NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)) '
            'WITH CHECK (tenant_id = (SELECT NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid))', t);
    END LOOP;
END $$;

-- merchant logos cached instance-wide (migration 099): Plaid's public
-- assets served from /api/logo so clients never fetch plaid.com themselves
CREATE TABLE IF NOT EXISTS logo_cache (
    url          TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    bytes        BYTEA NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- control-plane key/value for operator-facing background state (the level
-- a threshold alert last fired on). Mirrors migration 061.
CREATE TABLE IF NOT EXISTS ops_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
