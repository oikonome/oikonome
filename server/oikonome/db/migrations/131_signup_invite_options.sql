-- An invite carries whatever an installed add-on wants a new account born
-- with, as one JSON object the add-on writes and reads; the core table
-- knows only that the field exists.
ALTER TABLE signup_invites
    ADD COLUMN IF NOT EXISTS options JSONB NOT NULL DEFAULT '{}'::jsonb;
