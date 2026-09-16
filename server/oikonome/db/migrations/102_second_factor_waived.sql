-- An account on an instance that requires a second factor cannot use the
-- app until it enrols one. A demonstration household is the one deliberate
-- exception: its login is shared as a password, it has no phone to run an
-- authenticator, and it opens nothing but synthetic data. The waiver is a
-- per-user column only the demonstration-household seed (`demo-seed`)
-- writes; no request can set it, so the rule stays the rule for every
-- ordinary account.
ALTER TABLE users ADD COLUMN IF NOT EXISTS second_factor_waived BOOLEAN NOT NULL DEFAULT FALSE;
