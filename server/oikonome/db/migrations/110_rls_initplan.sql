-- Tenant isolation reads its GUC once per statement, not once per row.
--
-- Every domain table's tenant_isolation policy compared tenant_id against
-- NULLIF(current_setting('app.tenant_id', true), '')::uuid. Written that
-- way the planner treats current_setting() as a volatile-looking call and
-- re-evaluates it for every row a scan visits — on a large ledger the policy
-- alone is most of a full scan's cost, several times the same count with the
-- setting read once.
--
-- Wrapping the read in a scalar subquery makes it an InitPlan: evaluated
-- once, then compared as a constant. The predicate is identical — same
-- setting, same NULLIF guard (an unset or empty setting still matches no
-- row), same WITH CHECK on writes — so isolation does not change, only the
-- per-row work does.
--
-- Applied to every table that carries the policy at the time this runs,
-- including tables whose policy came from their own migration rather than
-- schema.sql; schema.sql's DO block (re-run on every migrate) creates
-- the same form, so fresh installs and re-runs agree with this.

DO $$
DECLARE t TEXT;
BEGIN
    FOR t IN
        SELECT tablename FROM pg_policies
         WHERE schemaname = 'public' AND policyname = 'tenant_isolation'
    LOOP
        EXECUTE format(
            'ALTER POLICY tenant_isolation ON %I '
            'USING (tenant_id = (SELECT NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)) '
            'WITH CHECK (tenant_id = (SELECT NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid))',
            t);
    END LOOP;
END $$;
