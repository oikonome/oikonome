-- Reject TOTP code REPLAY within the ~90s
-- validity window. Remember the last accepted step-counter per user; a
-- code whose counter is <= the stored one is refused, so an intercepted
-- code (phishing relay / MITM) can't be reused to mint a second session.
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_last_counter BIGINT;
