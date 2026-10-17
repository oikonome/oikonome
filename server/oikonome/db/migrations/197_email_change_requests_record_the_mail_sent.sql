-- A login-email change request records whether its confirmation mail
-- actually went out.
--
-- The confirmation goes to an address the caller typed, so it is capped
-- per recipient and per account. Counting every recorded request let the
-- cap be filled by requests whose mail was never sent (cancelled,
-- superseded, or themselves withheld) and by other households' requests
-- for the same address, so one household could silence another's
-- confirmation for a day. The caps now count only requests whose mail was
-- sent, and the per-recipient one never counts the caller's own.
--
-- NULL: no mail went out for this request (withheld by a cap, or a row
-- from before this column, which counted nothing it has not already).

ALTER TABLE email_change_requests
    ADD COLUMN IF NOT EXISTS mail_sent_at TIMESTAMPTZ;
