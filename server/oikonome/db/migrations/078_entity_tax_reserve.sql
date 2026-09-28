-- Which account holds this entity's tax money.
--
-- Setting estimated tax aside in a separate account is ordinary practice
-- for a self-employed filer; this records which account holds it, so "Set
-- aside for tax" can compare what is owed against that account.
--
-- Nullable: unset compares against all business cash, and the card says
-- that is what it is measuring.
ALTER TABLE business_entity
    ADD COLUMN IF NOT EXISTS tax_reserve_account_id TEXT;
