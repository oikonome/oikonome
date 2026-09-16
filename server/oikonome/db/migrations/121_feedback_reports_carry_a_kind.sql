-- The feedback form takes two kinds — feedback and a bug report — and the
-- stored row says which, so a later reading of the table can tell an
-- opinion from a defect without re-parsing the message. Older rows are
-- feedback.

ALTER TABLE feedback_reports
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'feedback';
