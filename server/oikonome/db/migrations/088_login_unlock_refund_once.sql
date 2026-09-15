-- An emailed unlock covers ONE sign-in attempt; the refund exists solely so
-- the two-step password→code form can spend that one attempt across its two
-- posts. Unmarked, the refund re-armed the exemption on EVERY empty-code
-- post, so consume→refund could cycle for the whole window — the "one
-- attempt" spend became a pass for unlimited password-verified posts.
-- Stamp the refund on the row: a second refund of the same unlock finds the
-- stamp and declines, so the exemption dies with its next spend.
ALTER TABLE login_unlocks
    ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;
