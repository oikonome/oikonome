-- Indexes admin_audit by action/time and target: machine writers look rows
-- up by action and target and scan by time, and with only the PK each of
-- those reads is a sequential scan over an unbounded, append-only log.
CREATE INDEX IF NOT EXISTS admin_audit_action_at_idx
    ON admin_audit (action, at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_target_idx
    ON admin_audit (target)
    WHERE target IS NOT NULL;
