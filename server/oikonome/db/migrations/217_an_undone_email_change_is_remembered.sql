-- When this login's address was last put back by the link mailed to the
-- address it had moved away from. An undone change is the account saying
-- that someone else held it a moment ago, and for a while afterwards the
-- one recovery that lets inbox control plus a quiet week clear every
-- factor (the cooling-off reset) is refused. NULL: never undone. Written
-- only by the undo itself, on the admin connection; the app role cannot
-- update users columns it is not granted, so it cannot clear the stamp.
ALTER TABLE users ADD COLUMN IF NOT EXISTS reverted_at TIMESTAMPTZ;
