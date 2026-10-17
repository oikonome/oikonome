-- The assistant keeps what it was asked and what it answered.
--
-- The conversation used to live in the browser tab alone: a reload lost
-- it, the phone and the web never saw each other's questions, and a
-- chart drawn yesterday could not be found again. One row per answered
-- question: who asked, when, the question, the prose, the validated
-- chart/table blocks and the summaries that fed the answer. A failed or
-- empty answer writes nothing. The household's own record, so every
-- member reads it; removing a row or clearing the log is an edit.
-- Instance-local: it does not travel in an export (the figures in it are
-- the ledger's, which does). Mirrored in schema.sql.

CREATE TABLE IF NOT EXISTS assistant_turns (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id          UUID NOT NULL DEFAULT gen_random_uuid(),
    asked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    asked_by_id UUID,
    asked_by    TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    blocks      JSONB NOT NULL DEFAULT '[]'::jsonb
        CONSTRAINT assistant_turns_blocks_is_a_list
            CHECK (jsonb_typeof(blocks) = 'array'),
    tools_used  TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS assistant_turns_recent
    ON assistant_turns (tenant_id, asked_at DESC);

ALTER TABLE assistant_turns ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON assistant_turns;
CREATE POLICY tenant_isolation ON assistant_turns
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
