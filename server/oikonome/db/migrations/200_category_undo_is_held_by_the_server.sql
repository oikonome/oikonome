-- A merchant-wide category write keeps its undo on the server.
--
-- The undo used to be a snapshot the client held and posted back, so it
-- had to be validated like any other body, with caps on how many rows
-- and how long a name it may carry. A write on a merchant past those caps
-- answered with an undo the undo door then refused. The write now stores
-- its full snapshot here and hands the client an opaque reference; the
-- undo loads it back, so the snapshot never travels and needs no cap.
-- A reference is spent once (`used_at`), and the writes prune what is
-- older than a week or past the newest few per household. Instance-local:
-- it does not travel in an export, and it goes with the household on
-- erasure. Mirrored in schema.sql (the index lives here only).

CREATE TABLE IF NOT EXISTS category_undo (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id          UUID NOT NULL DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload     JSONB NOT NULL
        CONSTRAINT category_undo_payload_is_an_object
            CHECK (jsonb_typeof(payload) = 'object'),
    used_at     TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS category_undo_recent
    ON category_undo (tenant_id, created_at DESC);

ALTER TABLE category_undo ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON category_undo;
CREATE POLICY tenant_isolation ON category_undo
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
