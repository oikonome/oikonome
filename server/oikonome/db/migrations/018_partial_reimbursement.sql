-- Partial reimbursement. A partial pair records how much of the
-- expense came back (amount); the expense KEEPS its category — spend math
-- nets the received amount off the row (reporting.NET_AMOUNT) and the
-- remainder stays real spend. Flags can carry an optional expected value
-- so the awaiting badge knows when a charge is fully covered.
-- (Mirrored in schema.sql.)

ALTER TABLE reimbursements
    ADD COLUMN IF NOT EXISTS partial INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS amount  DOUBLE PRECISION;
ALTER TABLE reimburse_flags
    ADD COLUMN IF NOT EXISTS partial  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS expected DOUBLE PRECISION;
