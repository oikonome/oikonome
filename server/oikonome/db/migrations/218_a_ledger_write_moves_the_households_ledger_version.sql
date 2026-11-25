-- Which transactions last wrote a household's dated ledger rows.
--
-- The Cash Flow picture keeps each household's per-month stamps in memory
-- and serves them again while a probe over the dated tables (the charges,
-- their split parts, their category overrides, the reimbursements on
-- either side of them) is unchanged. The probe was a row count and a sum
-- of row versions per table. A write and its undo bring both back to the
-- values they had: a split filed and then removed while the stamps were
-- being computed left stamps that include the split filed under a probe
-- that says nothing happened, and every later request was served them.
--
-- So the probe is now the set of transactions that wrote those tables,
-- kept per household by a trigger. Every transaction that writes a dated
-- row adds its own id, which no earlier state of the set ever held, so a
-- committed write always moves the probe and an undo is a second move,
-- never a return. Each writer removes the ids before its own, skipping
-- any row another writer has locked, so the set stays at one id or a
-- few; nothing in it is a counter two writers would queue on. The prune
-- runs only under READ COMMITTED, where a row someone else removed first
-- is simply not found; a REPEATABLE READ writer leaves it for the next.
--
-- The triggers fire once per statement, not per row: a restore landing a
-- whole ledger pays one insert that conflicts away after the first. They read
-- the household from the rows written, not from the session, so a write
-- made on the admin connection for a household marks that household. A
-- TRUNCATE has no rows to read and marks every household. Rows of a
-- household that is erased go with its tenants row.
--
-- No SECURITY DEFINER: the app role can write the table, its row security
-- admits exactly the household whose rows the app role could write, and
-- an admin-connection write bypasses row security like every other.
-- Mirrored in schema.sql.

CREATE TABLE IF NOT EXISTS ledger_writes (
    tenant_id UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    xact      XID8 NOT NULL,
    PRIMARY KEY (tenant_id, xact)
);

ALTER TABLE ledger_writes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON ledger_writes;
CREATE POLICY tenant_isolation ON ledger_writes
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));

CREATE OR REPLACE FUNCTION ledger_writes_mark() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    me     xid8 := pg_current_xact_id();
    tids   uuid[];
    marked integer;
BEGIN
    IF TG_OP = 'INSERT' THEN
        tids := ARRAY(SELECT DISTINCT tenant_id FROM ledger_new);
    ELSIF TG_OP = 'DELETE' THEN
        tids := ARRAY(SELECT DISTINCT tenant_id FROM ledger_old);
    ELSE
        -- an update can move a row to another household only on the
        -- admin connection; both are marked
        tids := ARRAY(SELECT tenant_id FROM ledger_new
                      UNION SELECT tenant_id FROM ledger_old);
    END IF;
    IF cardinality(tids) = 0 THEN
        RETURN NULL;
    END IF;
    -- a restore lands its rows one statement each: once this transaction
    -- has marked these households, the rest of its statements stop here
    IF cardinality(tids) = 1 AND current_setting('oikonome.ledger_marked',
            true) = me::text || ':' || tids[1]::text THEN
        RETURN NULL;
    END IF;
    INSERT INTO ledger_writes (tenant_id, xact)
    SELECT t, me FROM unnest(tids) AS t
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS marked = ROW_COUNT;
    IF cardinality(tids) = 1 THEN
        PERFORM set_config('oikonome.ledger_marked',
                           me::text || ':' || tids[1]::text, true);
    END IF;
    IF marked > 0
       AND current_setting('transaction_isolation') = 'read committed' THEN
        DELETE FROM ledger_writes w
         USING (SELECT p.tenant_id, p.xact FROM ledger_writes p
                 WHERE p.tenant_id = ANY (tids) AND p.xact < me
                 FOR UPDATE SKIP LOCKED) prior
         WHERE w.tenant_id = prior.tenant_id AND w.xact = prior.xact;
    END IF;
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION ledger_writes_mark_all() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO ledger_writes (tenant_id, xact)
    SELECT id, pg_current_xact_id() FROM tenants
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION ledger_writes_forget() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM ledger_writes WHERE tenant_id IN (SELECT id FROM gone);
    RETURN NULL;
END $$;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['transactions', 'transaction_splits',
                             'manual_categories', 'reimbursements']
    LOOP
        -- one trigger per event: a trigger with transition tables may
        -- fire on one event only
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER ledger_writes_mark_insert '
            'AFTER INSERT ON %I REFERENCING NEW TABLE AS ledger_new '
            'FOR EACH STATEMENT EXECUTE FUNCTION ledger_writes_mark()', t);
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER ledger_writes_mark_update '
            'AFTER UPDATE ON %I '
            'REFERENCING NEW TABLE AS ledger_new OLD TABLE AS ledger_old '
            'FOR EACH STATEMENT EXECUTE FUNCTION ledger_writes_mark()', t);
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER ledger_writes_mark_delete '
            'AFTER DELETE ON %I REFERENCING OLD TABLE AS ledger_old '
            'FOR EACH STATEMENT EXECUTE FUNCTION ledger_writes_mark()', t);
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER ledger_writes_mark_all '
            'AFTER TRUNCATE ON %I '
            'FOR EACH STATEMENT EXECUTE FUNCTION ledger_writes_mark_all()', t);
    END LOOP;
END $$;

CREATE OR REPLACE TRIGGER ledger_writes_forget AFTER DELETE ON tenants
    REFERENCING OLD TABLE AS gone
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_writes_forget();
