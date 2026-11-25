-- Whether two receipts are the same slip becomes a stored fact
-- (specs/receipts.md, "A receipt is one slip"). It used to be worked out
-- on every request from where each copy sat and what it read as, and the
-- answer changed whenever a hold posted, a receipt was released or a read
-- landed: a copy released to wait was counted beside its twin, and a real
-- purchase was held on a retired row because a look-alike counted.
--
-- Two receipts are one slip for exactly two reasons: the same image
-- (equal bytes), or a person said so. Neither depends on where a receipt
-- sits or what it was read as, so moving or re-reading a receipt never
-- changes its slip, and nothing here watches for either.
--
-- image_sha256 is made by the database from the image, which is never
-- rewritten, so every writer (an upload, a restore, a whole-row copy, a
-- build older than this one) gets it without knowing about it. Adding it
-- rewrites the whole receipts table, image bytes included, under an
-- ACCESS EXCLUSIVE lock (no read or write of receipts meanwhile) for the
-- length of this file: about 0.6 s per 100 MB of stored receipts in a
-- rehearsal on a local Postgres, so size the deploy window by the
-- table's size (pg_total_relation_size('receipts')).
--
-- slip_id is a label, not a foreign key: deleting the receipt whose id it
-- is leaves the rest of the slip together. NULL = a slip of its own. The
-- labels are not filled in here: every existing receipt starts undecided
-- (slip_decided_at NULL) and receipt_identity.decide_backlog decides them
-- oldest first on the next matcher pass or nightly, as it does a receipt
-- any writer that does not decide (an older build, a whole-row copy) inserts.

ALTER TABLE receipts
    ADD COLUMN IF NOT EXISTS image_sha256 BYTEA
        GENERATED ALWAYS AS (sha256(image)) STORED,
    ADD COLUMN IF NOT EXISTS slip_id UUID,
    ADD COLUMN IF NOT EXISTS slip_source TEXT
        CONSTRAINT receipts_slip_source_known
        CHECK (slip_source IS NULL OR slip_source IN ('image', 'person')),
    ADD COLUMN IF NOT EXISTS slip_decided_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS not_same_slip UUID[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_receipts_sha
    ON receipts (tenant_id, image_sha256);
CREATE INDEX IF NOT EXISTS idx_receipts_slip
    ON receipts (tenant_id, slip_id) WHERE slip_id IS NOT NULL;
-- the reading two receipts are compared by when the person is asked
-- whether they are one purchase, and by the nightly release's hold-back
-- (receipt_identity._STORE / _DAY / _TOTAL spell the same expressions)
CREATE INDEX IF NOT EXISTS idx_receipts_reading
    ON receipts (tenant_id,
                 lower(btrim(coalesce(parsed->>'merchant', ''))),
                 (parsed->>'date'),
                 (CASE WHEN jsonb_typeof(parsed->'total') = 'number'
                       THEN round((parsed->>'total')::numeric, 2) END))
    WHERE kind = 'receipt' AND parsed ? 'total';
CREATE INDEX IF NOT EXISTS idx_receipts_undecided
    ON receipts (tenant_id, created_at) WHERE slip_decided_at IS NULL;
