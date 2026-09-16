-- An emailed unlock covers ONE sign-in attempt; the refund exists solely so
-- the two-step password→code form can spend that one attempt across its two
-- posts. Stamp the refund so each emailed unlock can be refunded at most
-- once: a second refund of the same unlock finds the stamp and declines, so
-- the exemption dies with its next spend.
ALTER TABLE login_unlocks
    ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;
