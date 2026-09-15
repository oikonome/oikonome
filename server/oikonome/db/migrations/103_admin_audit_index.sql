-- admin_audit grew machine writers (checkout-session bookkeeping, webhook
-- failure rows, refund idempotency checks) whose lookups filter on action
-- and target and scan by time. The table previously had only its PK, so
-- every one of those reads was a sequential scan over an unbounded,
-- append-only log.
CREATE INDEX IF NOT EXISTS admin_audit_action_at_idx
    ON admin_audit (action, at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_target_idx
    ON admin_audit (target)
    WHERE target IS NOT NULL;
