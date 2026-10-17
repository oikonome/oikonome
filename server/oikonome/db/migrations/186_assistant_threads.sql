-- Every question starts a conversation, and a follow-up continues it: the
-- assistant keeps threads, not a flat list. A turn's thread is the first
-- turn's id; a follow-up carries its thread and the model is handed that
-- thread's earlier questions and answers — and nothing from any other —
-- as its context. Every turn written before this column was asked on its
-- own, so each becomes a thread of one. Mirrored in schema.sql.
ALTER TABLE assistant_turns ADD COLUMN IF NOT EXISTS thread_id UUID NOT NULL DEFAULT gen_random_uuid();
-- rows written before the column were each asked on their own: a thread of one
UPDATE assistant_turns SET thread_id = id WHERE thread_id <> id;

CREATE INDEX IF NOT EXISTS assistant_turns_thread
    ON assistant_turns (tenant_id, thread_id, asked_at);
