-- Two records that let the server finish Plaid work no client is waiting on.
--
-- plaid_link_sessions: every Hosted Link session a household opens. The
-- public-token exchange used to happen only inside the browser's or the
-- phone's status poll, and the session lived only in the web process's
-- memory. A tab closed while the person was still in the bank's window, a
-- phone that dropped the app during an OAuth hop, a deploy restarting the
-- web process, or a client giving up before the Hosted Link URL expired
-- all left a bank the person had authorised at Plaid with nothing here to
-- exchange its token, so the Item existed (and billed) with no access token
-- anyone could use to remove it. With the session on record, the Plaid LINK
-- webhook and a worker sweep finish it server-side, and a client that comes
-- back picks up its open sessions.
--
-- `exchanged` maps sha256(public token) to what that exchange came to
-- ({"item_id": ...} or {"error": ...}): a public token is single-use, so a
-- second exchange can only fail, and the record is what stops a later
-- finisher from trying. The tokens themselves are never stored. `reply` is
-- the answer the finished session gives every later poll.
--
-- No tenant_id column and no row-level security, like plaid_item_ledger:
-- the webhook that names a link token arrives with no tenant, and every
-- read and write here runs on the admin connection with the tenant named
-- in the WHERE clause. `tenant` is matched against the caller every time.
-- Rows are short-lived: the sweep deletes them a day after they open.
CREATE TABLE IF NOT EXISTS plaid_link_sessions (
    link_token      TEXT PRIMARY KEY,
    tenant          UUID NOT NULL,
    kind            TEXT NOT NULL,                 -- add | update
    item_id         TEXT,                          -- update mode only
    manage_accounts BOOLEAN NOT NULL DEFAULT false,
    institution     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    exchanged       JSONB NOT NULL DEFAULT '{}'::jsonb,
    done_at         TIMESTAMPTZ,
    reply           JSONB
);
CREATE INDEX IF NOT EXISTS plaid_link_sessions_open
    ON plaid_link_sessions (tenant, created_at) WHERE done_at IS NULL;

-- plaid_release_queue: Plaid Items whose /item/remove failed while their
-- household was being erased or reset. The household's rows, and with them
-- the only copy of each access token, are deleted right after the release,
-- so a single timeout at that moment left an Item billing at Plaid that
-- nothing on the instance could ever remove. The token is kept here,
-- encrypted under the instance master key, only until a nightly retry
-- removes the Item; the row is deleted on success, and given up on after a
-- bounded number of days. `client_id`/`base_url` name the Plaid keys the
-- Item was made under (an access token works only with them); a
-- household's own secret rides along encrypted, because those keys are
-- deleted with the household. Admin connection only, like the table above.
CREATE TABLE IF NOT EXISTS plaid_release_queue (
    item_id       TEXT PRIMARY KEY,
    tenant        TEXT,
    access_token  TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    secret        TEXT,
    base_url      TEXT NOT NULL,
    reason        TEXT NOT NULL,
    queued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_try_at   TIMESTAMPTZ,
    last_error    TEXT
);
