-- A tenant can be scheduled for deletion while SUSPENDED (the abuse
-- case), and restoring it must return it to suspended, not silently
-- un-suspend it to active. Capture the status the
-- tenant held before the grace-delete flip so restore can put it back.
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS status_before_delete TEXT;
