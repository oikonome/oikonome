-- The merchant-rename journal's ids come from one sequence the whole
-- instance shares. The app role was once allowed to move it (UPDATE, for
-- setval) so a restore could replay an archive's ids; a restore now gives
-- each journal row a fresh id instead, so nothing the app does needs to
-- move the counter, and a role that can is one an archive or an injected
-- statement could use to push it to the end of its range for every
-- household. Taking a value (USAGE) and reading it (SELECT) stay, from the
-- blanket sequence grant migrate.py applies on every start.
REVOKE UPDATE ON SEQUENCE merchant_renames_id_seq FROM oikonome_app;
