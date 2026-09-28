-- Whether an address was EVER confirmed, kept apart from whether it is
-- confirmed NOW. A hosted email change clears verified_at on a household
-- that once proved a mailbox, so verified_at alone cannot tell an
-- abandoned signup from a household that changed its address and has not
-- clicked the second link yet. The nightly sweep over unconfirmed
-- signups reads both. verify_reminded_at: the one reminder that sweep
-- sends per person, stamped so it is sent once.
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_verified_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS verify_reminded_at TIMESTAMPTZ;
UPDATE users SET first_verified_at = verified_at
 WHERE first_verified_at IS NULL AND verified_at IS NOT NULL;
